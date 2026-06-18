#!/usr/bin/env python3
"""
LIVE_PATTERN_SCANNER.py
══════════════════════════════════════════════════════════════════════════════
Real-time 5-min signal scanner for 3 high-conviction patterns:

  1. MULTI_BREAK  — ascending trendline (3+ touches) breaks → enter opposite
  2. COMPRESSION  — tight range squeeze breaks out (only after 10:30)
  3. TREND_RIDE   — 3 consecutive HH+HL after 13:00 → momentum entry

Rules enforced:
  • Sequential: one trade at a time across all symbols
  • MULTI_BREAK circuit breaker: 2 consecutive SL hits → blocked for the day
  • Premium filter: ₹100–200 (best win rate zone)
  • COMPRESSION: no trades before 10:30 (opening noise)
  • TREND_RIDE: no trades before 13:00

Usage:
    python3 LIVE_PATTERN_SCANNER.py
    python3 LIVE_PATTERN_SCANNER.py --lots 18 --strikes 8
══════════════════════════════════════════════════════════════════════════════
"""

import sys, os, time, argparse, json
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple
import requests

# ─── import core functions from PATTERN_ANALYZER ────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import PATTERN_ANALYZER as pa

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════
CFG = dict(pa.CFG)   # copy base config, override below
CFG.update({
    "lots":        18,
    "scan_strikes": 8,
    "premium_min": 100.0,   # tighter range for live — best win rate zone
    "premium_max": 200.0,
    "min_candle_touches": 3,
    "scan_interval_sec": 30,   # seconds after 5-min boundary to wait before fetching
})

IST_OFFSET = 19800
LOT_SIZES  = pa.LOT_SIZES

MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"
SCAN_INTERVAL_MIN = 5

# ═══════════════════════════════════════════════════════════════════════════
# LIVE TRADE STATE
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class LiveTrade:
    symbol:       str
    pattern:      str
    entry_price:  float
    sl:           float
    trail_act:    float
    trail_by:     float
    peak:         float      = 0.0
    trail_active: bool       = False
    entry_time:   str        = ""
    qty:          int        = 0
    touches:      int        = 0
    trigger_sym:  str        = ""   # the symbol whose trendline broke (MULTI_BREAK)

DAILY_MAX_LOSS = -20000.0   # stop all trading if day P&L hits this

@dataclass
class DayState:
    active_trade:    Optional[LiveTrade] = None
    completed:       List[dict]          = field(default_factory=list)
    seen_signals:    Set[tuple]          = field(default_factory=set)
    day_pnl:         float               = 0.0
    trading_halted:  bool                = False

# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def _now_ist() -> datetime:
    return datetime.utcnow() + timedelta(seconds=IST_OFFSET)

def _ist_str(ts: int) -> str:
    return datetime.utcfromtimestamp(ts + IST_OFFSET).strftime("%H:%M")

def _today_start_ms() -> int:
    now = _now_ist()
    today_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
    utc_9am   = today_9am - timedelta(seconds=IST_OFFSET)
    return int(utc_9am.timestamp() * 1000)

def _now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)

def _next_candle_time(now: datetime) -> datetime:
    """Return the next 5-min candle boundary."""
    minutes = now.minute
    next_min = ((minutes // SCAN_INTERVAL_MIN) + 1) * SCAN_INTERVAL_MIN
    if next_min >= 60:
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return now.replace(minute=next_min, second=0, microsecond=0)

def _pnl_sign(pnl: float) -> str:
    return "✅" if pnl >= 0 else "❌"

def lot_size(index: str) -> int:
    return LOT_SIZES.get(index.upper(), 75)

# ═══════════════════════════════════════════════════════════════════════════
# SIGNAL DETECTION — wraps PATTERN_ANALYZER detectors, returns new signals
# ═══════════════════════════════════════════════════════════════════════════
def detect_signals_today(symbols: List[str], all_candles: Dict[str, list],
                         cfg: dict, day_str: str) -> List[pa.PatternTrade]:
    """Run all 3 detectors on today's CLOSED candles for all symbols."""
    qty    = lot_size(cfg["index"]) * cfg["lots"]
    trades = []
    _exp   = datetime.strptime(cfg["expiry_date"], "%Y-%m-%d").date()
    _pfx   = f"{cfg['index']}{_exp.year % 100}{_exp.month}{_exp.day:02d}"

    for sym in symbols:
        candles = all_candles.get(sym, [])
        if len(candles) < 20:
            continue

        opt_type = "PE" if sym.endswith("PE") else "CE"

        # MULTI_BREAK
        if sym.startswith(_pfx):
            strike_str = sym[len(_pfx):]
        else:
            strike_str = ""
        if strike_str.endswith("CE") or strike_str.endswith("PE"):
            strike_val  = int(strike_str[:-2])
            opp_type    = "PE" if opt_type == "CE" else "CE"
            opp_sym     = pa.make_symbol(cfg["index"], cfg["expiry_date"], strike_val, opp_type)
            opp_candles = all_candles.get(opp_sym, [])
            if len(opp_candles) >= 20:
                mb = pa.detect_multi_break(candles, opp_candles, day_str,
                                           sym, opp_sym, qty, cfg)
                trades.extend(mb)

        # COMPRESSION (10:30+ filter already inside the function)
        trades.extend(pa.detect_compression_break(candles, day_str, sym, qty, cfg))

        # TREND_RIDE (CE only, 13:00+ filter inside function)
        trades.extend(pa.detect_trend_ride(candles, day_str, sym, opt_type, qty, cfg))

    return trades

# ═══════════════════════════════════════════════════════════════════════════
# TRADE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════
def update_trade(trade: LiveTrade, candle: dict) -> Optional[dict]:
    """
    Update trail SL on latest candle. Returns exit dict if trade closed, else None.
    """
    h, l = candle["h"], candle["l"]
    t    = trade
    time_str = _ist_str(candle["ts"])

    if h > t.peak:
        t.peak = h

    profit = h - t.entry_price
    if not t.trail_active and profit >= t.trail_act:
        t.trail_active = True
        new_sl = round(h - t.trail_by, 2)
        if new_sl > t.sl:
            t.sl = new_sl

    if t.trail_active:
        new_sl = round(t.peak - t.trail_by, 2)
        if new_sl > t.sl:
            t.sl = new_sl

    if l <= t.sl:
        exit_price  = t.sl
        reason      = "TRAIL_SL" if t.trail_active else "SL"
        pts         = round(exit_price - t.entry_price, 2)
        pnl         = round(pts * t.qty, 2)
        return dict(symbol=t.symbol, pattern=t.pattern, entry_time=t.entry_time,
                    exit_time=time_str, entry_price=t.entry_price,
                    exit_price=exit_price, reason=reason, pts=pts, pnl=pnl, qty=t.qty)
    return None

def entry_from_signal(sig: pa.PatternTrade, cfg: dict) -> LiveTrade:
    """Build a LiveTrade from a PatternTrade signal."""
    if sig.pattern == "MULTI_BREAK":
        trail_act, trail_by = cfg["break_trail_act"],  cfg["break_trail_by"]
    elif sig.pattern == "TREND_RIDE":
        trail_act, trail_by = cfg["trend_trail_act"],  cfg["trend_trail_by"]
    else:  # COMPRESSION
        trail_act, trail_by = cfg["bounce_trail_act"], cfg["bounce_trail_by"]

    return LiveTrade(
        symbol      = sig.symbol,
        pattern     = sig.pattern,
        entry_price = sig.entry_price,
        sl          = sig.entry_price - (cfg["break_initial_sl"] if sig.pattern == "MULTI_BREAK" else 5.0),
        trail_act   = trail_act,
        trail_by    = trail_by,
        peak        = sig.entry_price,
        entry_time  = sig.entry_time,
        qty         = sig.qty,
        touches     = sig.touches,
    )

# ═══════════════════════════════════════════════════════════════════════════
# PRINTING
# ═══════════════════════════════════════════════════════════════════════════
def _ts() -> str:
    return _now_ist().strftime("%H:%M:%S")

def print_header(cfg: dict):
    qty = lot_size(cfg["index"]) * cfg["lots"]
    print("═" * 72)
    print("  LIVE PATTERN SCANNER  —  NIFTY  —  3 PATTERNS")
    print(f"  Lots: {cfg['lots']}  |  Qty: {qty}  |  Premium: ₹{cfg['premium_min']:.0f}–{cfg['premium_max']:.0f}")
    print(f"  Patterns: MULTI_BREAK + COMPRESSION(10:30+) + TREND_RIDE(13:00+)")
    print(f"  Risk cap: Day P&L drops below ₹{DAILY_MAX_LOSS:,.0f} → all trading halted")
    print("═" * 72)

def print_signal(sig: pa.PatternTrade, state: DayState):
    tc = f"  [{sig.touches}-touch trendline]" if sig.touches else ""
    print(f"\n{'─'*72}")
    print(f"  🔥  [{_ts()}]  NEW SIGNAL: {sig.pattern}{tc}")
    print(f"  Symbol  : {sig.symbol}")
    print(f"  Entry   : ₹{sig.entry_price:.1f}  |  SL: ₹{sig.entry_price - 5:.1f}")
    print(f"  Premium : ₹{sig.premium_at_entry:.1f}  |  Qty: {sig.qty}")
    print(f"  Action  : BUY {sig.symbol} at MARKET (next candle open ≈ ₹{sig.entry_price:.1f})")
    print(f"{'─'*72}")

def print_trade_status(trade: LiveTrade, current_price: float):
    pnl  = round((current_price - trade.entry_price) * trade.qty, 2)
    sign = "+" if pnl >= 0 else ""
    trail_status = f"Trail SL: ₹{trade.sl:.1f}" if trade.trail_active else f"Init SL: ₹{trade.sl:.1f}"
    print(f"  📊  [{_ts()}]  IN TRADE: {trade.symbol} ({trade.pattern})"
          f"  Entry ₹{trade.entry_price:.1f} → Now ₹{current_price:.1f}"
          f"  |  {trail_status}"
          f"  |  P&L: ₹{sign}{pnl:,.0f}")

def print_exit(result: dict, day_pnl: float):
    sign = _pnl_sign(result["pnl"])
    print(f"\n{'─'*72}")
    print(f"  {sign}  [{_ts()}]  EXIT: {result['symbol']}  ({result['pattern']})")
    print(f"  Entry ₹{result['entry_price']:.1f} → Exit ₹{result['exit_price']:.1f}"
          f"  |  {result['pts']:+.1f} pts  |  P&L: ₹{result['pnl']:+,.0f}  ({result['reason']})")
    print(f"  Day P&L so far: ₹{day_pnl:+,.0f}")
    print(f"{'─'*72}")

def print_halt(day_pnl: float):
    print(f"\n  🛑  [{_ts()}]  DAILY LOSS CAP REACHED: ₹{day_pnl:,.0f}")
    print(f"       All trading HALTED for today. Max allowed loss: ₹{DAILY_MAX_LOSS:,.0f}\n")

def print_scan_header(now: datetime, symbols: int, state: DayState):
    trade_info = (f"IN TRADE: {state.active_trade.symbol}"
                  if state.active_trade else "No active trade")
    halt_info  = "  🛑 HALTED" if state.trading_halted else ""
    print(f"  [{now.strftime('%H:%M:%S')}]  Scanning {symbols} symbols  |  {trade_info}  |  Day P&L: ₹{state.day_pnl:+,.0f}{halt_info}")

# ═══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════
def run_scanner(cfg: dict):
    sess   = pa._build_session()
    state  = DayState()
    today  = _now_ist().date()

    print_header(cfg)

    # ── Spot → ATM → strikes ──────────────────────────────────────────────
    print(f"\n  [{_ts()}]  Fetching spot price...")
    spot   = pa.fetch_spot(sess)
    atm    = int(round(spot / cfg["strike_step"]) * cfg["strike_step"])
    r      = cfg["scan_strikes"]
    ss     = cfg["strike_step"]
    strikes = [atm + i * ss for i in range(-r, r + 1)]
    symbols = [pa.make_symbol(cfg["index"], cfg["expiry_date"], s, ot)
               for s in strikes for ot in ["CE", "PE"]]
    print(f"  Spot: ₹{spot:,.2f}  →  ATM: {atm}")
    print(f"  Scanning {len(symbols)} symbols ({len(strikes)} strikes × CE+PE)")
    print(f"\n  Waiting for first candle close...\n")

    prev_candle_times: Dict[str, str] = {}   # sym → last closed candle HH:MM

    while True:
        now_ist  = _now_ist()
        now_time = now_ist.strftime("%H:%M")

        # Market not open yet
        if now_time < MARKET_OPEN:
            wait = (_next_candle_time(now_ist.replace(hour=9, minute=15)) - now_ist).seconds
            print(f"  [{_ts()}]  Market opens at {MARKET_OPEN}. Waiting {wait//60}m {wait%60}s...")
            time.sleep(min(wait, 60))
            continue

        # Market closed
        if now_time >= MARKET_CLOSE:
            print(f"\n  [{_ts()}]  Market closed.")
            _print_eod_summary(state)
            break

        # ── Wait for next candle boundary + 30s ──────────────────────────
        next_c   = _next_candle_time(now_ist)
        wait_sec = (next_c - now_ist).total_seconds() + cfg["scan_interval_sec"]
        if wait_sec > 0:
            time.sleep(wait_sec)

        now_ist  = _now_ist()
        now_time = now_ist.strftime("%H:%M")
        if now_time >= MARKET_CLOSE:
            break

        # ── Fetch today's candles ─────────────────────────────────────────
        start_ms   = _today_start_ms()
        end_ms     = _now_ms()
        all_candles: Dict[str, list] = {}
        for sym in symbols:
            raw = pa.fetch_candles(sess, sym, start_ms, end_ms)
            if raw:
                all_candles[sym] = sorted(raw, key=lambda c: c["ts"])

        if not all_candles:
            print(f"  [{_ts()}]  No data fetched. Retrying next candle.")
            continue

        day_str = str(today)

        # ── Check if active trade has hit SL/trail on latest candle ──────
        if state.active_trade:
            t       = state.active_trade
            tc_list = all_candles.get(t.symbol, [])
            if tc_list:
                latest = tc_list[-1]   # current (possibly still forming) candle
                result = update_trade(t, latest)
                if result:
                    state.active_trade = None
                    state.day_pnl     += result["pnl"]
                    state.completed.append(result)
                    print_exit(result, state.day_pnl)

                    # Daily max-loss cap check
                    if not state.trading_halted and state.day_pnl <= DAILY_MAX_LOSS:
                        state.trading_halted = True
                        print_halt(state.day_pnl)
                else:
                    cur_price = tc_list[-1]["c"]
                    print_trade_status(t, cur_price)

        # ── Detect signals on closed candles (all except last) ───────────
        closed_candles = {sym: c[:-1] for sym, c in all_candles.items() if len(c) > 1}
        all_signals = detect_signals_today(symbols, closed_candles, cfg, day_str)

        # Find NEW signals (not seen before, at the most recent candle)
        if closed_candles:
            # Latest closed candle time
            sample_sym    = next(iter(closed_candles))
            last_closed_t = _ist_str(closed_candles[sample_sym][-1]["ts"]) if closed_candles[sample_sym] else "00:00"

        new_signals = []
        for sig in all_signals:
            key = (sig.symbol, sig.pattern, sig.entry_time)
            if key not in state.seen_signals:
                state.seen_signals.add(key)
                # Only alert signals that fired on the MOST RECENTLY closed candle
                if sig.entry_time >= last_closed_t:
                    new_signals.append(sig)

        # ── Fire alert for first valid new signal ─────────────────────────
        for sig in sorted(new_signals, key=lambda s: s.entry_time):
            if state.trading_halted:
                print(f"  [{_ts()}]  Signal {sig.pattern} on {sig.symbol} — SKIPPED (daily loss cap hit)")
                break
            if state.active_trade:
                print(f"  [{_ts()}]  Signal {sig.pattern} on {sig.symbol} — SKIPPED (trade active)")
                continue
            # Valid new signal — alert it
            print_signal(sig, state)
            state.active_trade = entry_from_signal(sig, cfg)
            break

        print_scan_header(now_ist, len(all_candles), state)


def _print_eod_summary(state: DayState):
    trades = state.completed
    print(f"\n{'═'*72}")
    print(f"  END OF DAY SUMMARY")
    print(f"{'═'*72}")
    if not trades:
        print("  No trades today.")
    else:
        print(f"  {'TIME':>5}  {'SYMBOL':<26}  {'PATTERN':<13}  {'PTS':>6}  {'P&L':>10}  REASON")
        print(f"  {'─'*5}  {'─'*26}  {'─'*13}  {'─'*6}  {'─'*10}  {'─'*9}")
        for t in trades:
            sign = "✅" if t["pnl"] >= 0 else "❌"
            print(f"  {t['entry_time']:>5}  {t['symbol']:<26}  {t['pattern']:<13}  "
                  f"{t['pts']:>+6.1f}  ₹{t['pnl']:>+9,.0f}  {t['reason']}  {sign}")
        wins = sum(1 for t in trades if t["pnl"] > 0)
        print(f"  {'─'*72}")
        print(f"  Total: {len(trades)} trades  |  {wins}/{len(trades)} wins  |  Day P&L: ₹{state.day_pnl:+,.0f}")

    if state.active_trade:
        t = state.active_trade
        print(f"\n  ⚠️  Open trade at market close: {t.symbol} ({t.pattern})"
              f"  Entry ₹{t.entry_price:.1f}  →  Will close at next day's open")
    print(f"{'═'*72}\n")

    # Save log
    os.makedirs("logs/trade_history", exist_ok=True)
    today_str = _now_ist().strftime("%Y-%m-%d")
    out = f"logs/trade_history/live_scanner_{today_str}.json"
    with open(out, "w") as f:
        json.dump(trades, f, indent=2)
    print(f"  Trade log saved → {out}")


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots",        type=int,   default=CFG["lots"])
    ap.add_argument("--strikes",     type=int,   default=CFG["scan_strikes"])
    ap.add_argument("--premium_min", type=float, default=CFG["premium_min"])
    ap.add_argument("--premium_max", type=float, default=CFG["premium_max"])
    ap.add_argument("--expiry",      type=str,   default=CFG["expiry_date"])
    args = ap.parse_args()

    CFG["lots"]        = args.lots
    CFG["scan_strikes"] = args.strikes
    CFG["premium_min"] = args.premium_min
    CFG["premium_max"] = args.premium_max
    CFG["expiry_date"] = args.expiry

    run_scanner(CFG)


if __name__ == "__main__":
    main()
