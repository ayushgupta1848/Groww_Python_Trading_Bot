#!/usr/bin/env python3
"""
TRENDLINE_REPLAY.py
──────────────────────────────────────────────────────────────────────────────
Replays the trendline strategy for a specific date with FULL verbose logging.
Every candle scan, trendline detection, signal, entry, and exit is logged
with exact timestamps and prices — so you can verify each trade on Groww chart.

Output:
    logs/replay/trendline_replay_YYYY-MM-DD.log  ← human-readable
    logs/replay/trendline_replay_YYYY-MM-DD.json ← machine-readable trades

Usage:
    python3 TRENDLINE_REPLAY.py --date 2026-06-12
    python3 TRENDLINE_REPLAY.py --date 2026-06-17
──────────────────────────────────────────────────────────────────────────────
"""

import sys, json, os, argparse
from datetime import datetime, date
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import requests

# ─── paste same config as TRENDLINE_BACKTEST.py ─────────────────────────────
BEARER = (
    "eyJraWQiOiJXTTZDLVEiLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjE3ODE3ODYzMjMsIml"
    "hdCI6MTc4MTQ5MzY3NywibmJmIjoxNzgxNDkzNjI3LCJzdWIiOiJ7XCJwbGF0Zm9ybVwi"
    "Olwid2ViXCIsXCJwbGF0Zm9ybVZlcnNpb25cIjpudWxsLFwib3NcIjpudWxsLFwib3NWZX"
    "JzaW9uXCI6bnVsbCxcImlwQWRkcmVzc1wiOlwiMTIyLjE3Ni4yMTYuMTIwLFwiLFwibWFj"
    "QWRkcmVzc1wiOm51bGwsXCJ1c2VyQWdlbnRcIjpcIk1vemlsbGEvNS4wIChNYWNpbnRvc2"
    "g7IEludGVsIE1hYyBPUyBYIDEwXzE1XzcpIEFwcGxlV2ViS2l0LzUzNy4zNiAoS0hUTUw"
    "sIGxpa2UgR2Vja28pIENocm9tZS8xNDIuMC4wLjAgU2FmYXJpLzUzNy4zNlwiLFwiZ3Jvd"
    "3dVc2VyQWdlbnRcIjpudWxsLFwiZGV2aWNlSWRcIjpcIjhjZWExZDI1LTU4OGEtNWVmZi0"
    "5Njk5LTVlN2ZkMjBhNmNhOVwiLFwic2Vzc2lvbklkXCI6XCI0MDI5ZWNlNy05ZDUwLTQ1Z"
    "DNhNDViLThlNGI4YzA2NWI5ZFwiLFwic2Vzc2lvbklkSXNzdWVkQXRcIjoxNzgwMzAyNjg"
    "1MjYzLFwic3VwZXJBY2NvdW50SWRcIjpcIkFDQzcwODg4MDA1ODY1MjhcIixcInVzZXJBY"
    "2NvdW50SWRcIjpcIkFDQzcwODg4MDA1ODY1MjhcIixcInR5cGVcIjpcIkFUXCIsXCJ0b2t"
    "lbkV4cGlyeVwiOjE3ODE3ODYzMjM2MDgsXCJ0b2tlbklkXCI6XCIxNzBjYjlkYS05NTRkL"
    "TRiZmItYTYyZi00Zjc1ZjllN2E2MmZcIixcImJzZVVzZXJJZFwiOlwiOTAxNTg3MDg0NVwi"
    "LFwib25lRmFNb2RlXCI6XCJLTU9XTEVER0VfRkFDVE9SXCIgfSIsImlzcyI6Imdyb3d3Yml"
    "sbGlvbm1pbGxlbm5pYWwifQ.nQBNt08OAagxLg5vEKB-6dMhPNtV6flq8DnxxiUDUGYxFG"
    "FjLZuEjcNRBHP1YROKvNa7GCD7MKckgVZaWtSH0Q"
)
COOKIES_STR = (
    "_gcl_au=1.1.863312266.1778482315; _ga=GA1.1.389629575.1778482316; "
    "we_luid=c3a360c8e013fb719f633243686d1c99c659a861"
)
DEVICE_ID = "8cea1d25-588a-5eff-9699-5e7fd20a6ca9"

CFG = {
    "index":           "NIFTY",
    "exchange":        "NSE",
    "expiry_date":     "2026-06-23",
    "strike_step":     50,
    "scan_range":      20,   # fetch ATM ± N strikes
    "premium_min":     90.0, # only trade options in this premium range
    "premium_max":     200.0,
    "interval_min":    5,
    "pivot_lookback":  3,
    "min_pivots":      2,
    "proximity_pts":   6.0,
    "break_pts":       3.0,
    "slippage_pts":    0.5,
    "target_buffer":   2.0,
    "trendline_sl_buf":3.0,
    "bounce_trail_act":5.0,
    "bounce_trail_by": 4.0,
    "break_initial_sl":5.0,
    "break_trail_act": 4.0,
    "break_trail_by":  3.0,
    "lots":            1,
}
LOT_SIZES  = {"NIFTY": 75, "BANKNIFTY": 15, "FINNIFTY": 40, "SENSEX": 20}
IST_OFFSET = 19800

# ─── models ─────────────────────────────────────────────────────────────────
@dataclass
class Pivot:
    idx: int; ts: int; price: float

@dataclass
class TrendlineState:
    valid: bool = False
    support: float = 0.0
    slope: float = 0.0
    pivots: List[Pivot] = field(default_factory=list)
    last_swing_high: float = 0.0

@dataclass
class ReplayTrade:
    symbol: str; strike: int; opt_type: str; signal: str
    entry_idx: int; entry_time: str; entry_price: float; qty: int
    target: Optional[float]; sl: float
    trail_activate: float; trail_by: float
    peak: float = 0.0; trail_active: bool = False
    exit_idx: int = 0; exit_time: str = ""; exit_price: float = 0.0
    exit_reason: str = ""; pts: float = 0.0; pnl: float = 0.0
    # for verifying on chart
    tl_pivot1_time: str = ""; tl_pivot1_price: float = 0.0
    tl_pivot2_time: str = ""; tl_pivot2_price: float = 0.0
    tl_support_at_signal: float = 0.0
    signal_candle_time: str = ""; signal_candle_close: float = 0.0

# ─── API ─────────────────────────────────────────────────────────────────────
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {BEARER}",
        "x-app-id": "growwWeb",
        "x-device-id": DEVICE_ID,
        "x-device-id-v2": DEVICE_ID,
        "x-device-type": "charts",
        "x-platform": "web",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    })
    for p in COOKIES_STR.split("; "):
        if "=" in p:
            k, v = p.split("=", 1)
            s.cookies.set(k.strip(), v.strip())
    return s

def fetch_candles_v4(sess, symbol, exchange, start_ms, end_ms, interval=5):
    url = (f"https://groww.in/v1/api/stocks_fo_data/v4/charting_service/chart"
           f"/exchange/{exchange}/segment/FNO/{symbol}")
    try:
        r = sess.get(url, params={"startTimeInMillis": start_ms,
                                   "endTimeInMillis": end_ms,
                                   "intervalInMinutes": interval}, timeout=12)
        r.raise_for_status()
        out = []
        for c in r.json().get("candles", []):
            out.append({"ts": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                        "l": float(c[3]), "c": float(c[4]),
                        "v": int(c[5]) if c[5] else 0})
        return out
    except Exception as ex:
        print(f"  [API] ✗ {symbol}: {ex}")
        return []

# ─── trendline engine ────────────────────────────────────────────────────────
def _ist_dt(ts): return datetime.utcfromtimestamp(ts + IST_OFFSET)
def _ist_date(ts): return _ist_dt(ts).date()
def _t(ts): return _ist_dt(ts).strftime("%H:%M")

def find_swing_lows(candles, lb):
    pivots, n = [], len(candles)
    for i in range(lb, n - lb):
        lo = candles[i]["l"]
        if (all(candles[i-j]["l"] > lo for j in range(1, lb+1)) and
                all(candles[i+j]["l"] > lo for j in range(1, lb+1))):
            pivots.append(Pivot(idx=i, ts=candles[i]["ts"], price=lo))
    return pivots

def find_swing_highs(candles, lb):
    pivots, n = [], len(candles)
    for i in range(lb, n - lb):
        hi = candles[i]["h"]
        if (all(candles[i-j]["h"] < hi for j in range(1, lb+1)) and
                all(candles[i+j]["h"] < hi for j in range(1, lb+1))):
            pivots.append(Pivot(idx=i, ts=candles[i]["ts"], price=hi))
    return pivots

def project_trendline(pivots, cur_idx):
    if len(pivots) < 2: return None
    p1, p2 = pivots[-2], pivots[-1]
    d = p2.idx - p1.idx
    if d == 0: return None
    slope = (p2.price - p1.price) / d
    return p2.price + slope * (cur_idx - p2.idx)

def compute_trendline(candles, lb, min_p):
    tl = TrendlineState()
    if len(candles) < lb * 2 + 2: return tl
    lows  = find_swing_lows(candles, lb)
    highs = find_swing_highs(candles, lb)
    tl.last_swing_high = max((p.price for p in highs), default=0.0)
    if len(lows) < min_p: return tl
    last = lows[-min_p:]
    if not all(last[i].price > last[i-1].price for i in range(1, len(last))): return tl
    cur = len(candles) - 1
    proj = project_trendline(lows, cur)
    if not proj or proj <= 0: return tl
    p1, p2 = lows[-2], lows[-1]
    tl.valid          = True
    tl.pivots         = lows
    tl.support        = round(proj, 2)
    tl.slope          = round((p2.price - p1.price) / max(p2.idx - p1.idx, 1), 4)
    return tl

def make_symbol(index, expiry, strike, opt_type):
    return f"{index}{expiry.year%100}{expiry.month}{expiry.day:02d}{int(strike)}{opt_type}"

# ─── replay engine ───────────────────────────────────────────────────────────
def replay_day(day: date, candles_by_sym: Dict[str, list],
               strikes: List[int], expiry: date,
               index: str, exchange: str, cfg: dict) -> tuple:
    """
    Returns (trades_list, log_lines_list)
    SEQUENTIAL mode: full per-candle verbose simulation, one trade at a time.
    All instruments are scanned every candle, but entry is blocked while a
    trade is open. First valid signal in strike order wins.
    """
    lb    = cfg["pivot_lookback"]
    min_p = cfg["min_pivots"]
    prox  = cfg["proximity_pts"]
    brk   = cfg["break_pts"]
    qty   = LOT_SIZES.get(index.upper(), 75) * cfg["lots"]
    p_min = cfg.get("premium_min", 0.0)
    p_max = cfg.get("premium_max", 999999.0)

    lines: List[str] = []
    trades: List[ReplayTrade] = []
    pending_breaks: Dict[str, int] = {}

    sym_list:  List  = []
    valid_syms: set  = set()
    for s in strikes:
        for ot in ["CE", "PE"]:
            sym = make_symbol(index, expiry, s, ot)
            if sym in candles_by_sym and candles_by_sym[sym]:
                sym_list.append((sym, s, ot))
                valid_syms.add(sym)

    def L(msg): lines.append(msg)

    L("=" * 100)
    L(f"  REPLAY  {day}  |  {index}  |  expiry {expiry}  |  {len(sym_list)} instruments")
    L(f"  MODE: SEQUENTIAL — one trade at a time (first valid signal each candle wins)")
    L("=" * 100)

    if not sym_list:
        L("  No data for this day.")
        return trades, lines

    n_candles = max(len(candles_by_sym[sym]) for sym, _, _ in sym_list)

    L(f"\n  {'TIME':5s}  Scanning {len(sym_list)} instruments, {n_candles} candles × {cfg['interval_min']} min")
    L(f"  Signal thresholds:  BOUNCE prox ≤ {prox} pts  |  BREAK drop ≥ {brk} pts")
    L(f"  Exit params:  BOUNCE SL buf={cfg['trendline_sl_buf']}  target buf={cfg['target_buffer']}  "
      f"trail act={cfg['bounce_trail_act']} trail by={cfg['bounce_trail_by']}")
    L(f"  BREAK:  initial SL={cfg['break_initial_sl']}  trail act={cfg['break_trail_act']} "
      f"trail by={cfg['break_trail_by']}")
    L(f"  Premium filter:  ₹{p_min}–₹{p_max}")
    L("")

    # Sequential state: at most one trade open at any time
    active_trade: Optional[ReplayTrade] = None
    active_sym:   Optional[str]         = None

    for idx in range(n_candles):
        # get representative timestamp from first available symbol
        cur_ts = None
        for sym, _, _ in sym_list:
            cs = candles_by_sym[sym]
            if idx < len(cs):
                cur_ts = cs[idx]["ts"]
                break
        time_str = _t(cur_ts) if cur_ts else "??"

        # ── manage the single active trade ────────────────────────────────
        if active_trade is not None and idx > active_trade.entry_idx:
            t  = active_trade
            cs = candles_by_sym[active_sym]

            if idx >= len(cs):
                last = cs[-1]
                t.exit_idx    = len(cs) - 1
                t.exit_time   = _t(last["ts"])
                t.exit_price  = round(last["c"], 2)
                t.exit_reason = "EOD"
                t.pts = round(t.exit_price - t.entry_price, 2)
                t.pnl = round(t.pts * t.qty, 2)
                sign = "✅" if t.pnl > 0 else "❌"
                L(f"  [{time_str}] {active_sym:24s}  {sign} EXIT EOD"
                  f"  @ ₹{t.exit_price:.1f}  pts={t.pts:+.1f}  P&L=₹{t.pnl:+,.0f}")
                trades.append(t)
                active_trade = None
                active_sym   = None
            else:
                c = cs[idx]
                h, l = c["h"], c["l"]
                if h > t.peak: t.peak = h

                profit = h - t.entry_price
                if not t.trail_active and profit >= t.trail_activate:
                    t.trail_active = True
                    new_sl = round(h - t.trail_by, 2)
                    if new_sl > t.sl:
                        old_sl = t.sl; t.sl = new_sl
                        L(f"  [{time_str}] {active_sym:24s}  🔄 TRAIL ACTIVATED"
                          f"  profit=+{profit:.1f}pts  peak=₹{h:.1f}  SL ₹{old_sl:.1f}→₹{t.sl:.1f}")

                if t.trail_active:
                    new_sl = round(t.peak - t.trail_by, 2)
                    if new_sl > t.sl:
                        old_sl = t.sl; t.sl = new_sl
                        L(f"  [{time_str}] {active_sym:24s}  🔄 TRAIL MOVED"
                          f"  peak=₹{t.peak:.1f}  SL ₹{old_sl:.1f}→₹{t.sl:.1f}")

                closed = False
                if t.target and h >= t.target:
                    t.exit_idx    = idx
                    t.exit_time   = time_str
                    t.exit_price  = t.target
                    t.exit_reason = "TARGET"
                    t.pts = round(t.exit_price - t.entry_price, 2)
                    t.pnl = round(t.pts * t.qty, 2)
                    L(f"  [{time_str}] {active_sym:24s}  ✅ EXIT TARGET"
                      f"  H=₹{h:.1f} ≥ target=₹{t.target:.1f}"
                      f"  entry=₹{t.entry_price:.1f}  pts={t.pts:+.1f}  P&L=₹{t.pnl:+,.0f}")
                    trades.append(t)
                    active_trade = None
                    active_sym   = None
                    closed = True

                if not closed and l <= t.sl:
                    tag = "TRAIL_SL" if t.trail_active else "SL"
                    t.exit_idx    = idx
                    t.exit_time   = time_str
                    t.exit_price  = t.sl
                    t.exit_reason = tag
                    t.pts = round(t.exit_price - t.entry_price, 2)
                    t.pnl = round(t.pts * t.qty, 2)
                    sign = "✅" if t.pnl > 0 else "❌"
                    L(f"  [{time_str}] {active_sym:24s}  {sign} EXIT {tag}"
                      f"  L=₹{l:.1f} ≤ SL=₹{t.sl:.1f}"
                      f"  entry=₹{t.entry_price:.1f}  pts={t.pts:+.1f}  P&L=₹{t.pnl:+,.0f}")
                    trades.append(t)
                    active_trade = None
                    active_sym   = None

        # ── skip scan if a trade is still active ──────────────────────────
        if active_trade is not None:
            continue

        # ── scan for signals — take the FIRST valid one ───────────────────
        for sym, strike, ot in sym_list:
            cs = candles_by_sym[sym]
            if idx + 1 >= len(cs): continue

            close = cs[idx]["c"]
            if not (p_min <= close <= p_max):
                continue

            tl = compute_trendline(cs[:idx + 1], lb, min_p)
            if not tl.valid: continue

            dist = close - tl.support

            # trendline context for log
            p1 = tl.pivots[-2] if len(tl.pivots) >= 2 else None
            p2 = tl.pivots[-1]
            pivot_str = ""
            if p1:
                pivot_str = (f"  pivot1=[{_t(p1.ts)} ₹{p1.price:.1f}]"
                             f"  pivot2=[{_t(p2.ts)} ₹{p2.price:.1f}]"
                             f"  support=₹{tl.support:.1f}  slope={tl.slope:+.3f}/bar"
                             f"  swing_hi=₹{tl.last_swing_high:.1f}")

            # ── BOUNCE ────────────────────────────────────────────────────
            if 0.0 <= dist <= prox:
                next_c = cs[idx + 1]
                confirmed = next_c["h"] >= close + 2.0

                L(f"\n  [{time_str}] ⚡ BOUNCE SIGNAL  {sym}")
                if pivot_str: L(f"          {pivot_str.strip()}")
                L(f"          close=₹{close:.1f}  support=₹{tl.support:.1f}  "
                  f"dist={dist:.1f} pts  (≤{prox})")
                L(f"          Confirm → next candle [{_t(next_c['ts'])}]"
                  f"  H=₹{next_c['h']:.1f}  need ≥ ₹{close+2.0:.1f}  → "
                  f"{'✅ CONFIRMED' if confirmed else '❌ FAILED'}")

                if confirmed:
                    entry   = round(next_c["o"] + CFG["slippage_pts"], 2)
                    target  = (round(tl.last_swing_high - cfg["target_buffer"], 2)
                               if tl.last_swing_high > entry else None)
                    init_sl = round(tl.support - cfg["trendline_sl_buf"], 2)
                    if entry <= init_sl:
                        L(f"          ⏭️  SKIP — entry ₹{entry:.1f} ≤ SL ₹{init_sl:.1f}")
                        continue
                    if target and target <= entry:
                        target = None

                    t = ReplayTrade(
                        symbol=sym, strike=strike, opt_type=ot, signal="BOUNCE",
                        entry_idx=idx + 1, entry_time=_t(next_c["ts"]),
                        entry_price=entry, qty=qty,
                        target=target, sl=init_sl,
                        trail_activate=cfg["bounce_trail_act"],
                        trail_by=cfg["bounce_trail_by"], peak=entry,
                        tl_pivot1_time=_t(p1.ts) if p1 else "",
                        tl_pivot1_price=p1.price if p1 else 0,
                        tl_pivot2_time=_t(p2.ts), tl_pivot2_price=p2.price,
                        tl_support_at_signal=tl.support,
                        signal_candle_time=time_str, signal_candle_close=close,
                    )
                    active_trade = t
                    active_sym   = sym
                    tgt_str = f"₹{target:.1f}" if target else "trailing only"
                    L(f"          📈 ENTRY @ [{_t(next_c['ts'])}]"
                      f"  open=₹{next_c['o']:.1f} + slip=₹{cfg['slippage_pts']}"
                      f"  → entry=₹{entry:.1f}")
                    L(f"          SL=₹{init_sl:.1f}  target={tgt_str}"
                      f"  trail_act=+{cfg['bounce_trail_act']}pts"
                      f"  trail_by={cfg['bounce_trail_by']}pts  qty={qty}")
                    break   # sequential: stop scanning this candle

            # ── BREAK ─────────────────────────────────────────────────────
            elif dist < -brk:
                opp_type = "CE" if ot == "PE" else "PE"
                opp_sym  = make_symbol(index, expiry, strike, opp_type)
                if opp_sym not in valid_syms: continue
                if pending_breaks.get(opp_sym, -99) == idx: continue
                pending_breaks[opp_sym] = idx

                cs_opp = candles_by_sym.get(opp_sym, [])
                if not cs_opp or idx + 1 >= len(cs_opp): continue
                next_opp = cs_opp[idx + 1]
                confirmed = next_opp["h"] >= next_opp["o"] + 1.5

                L(f"\n  [{time_str}] ⚠️  BREAK SIGNAL  {sym}"
                  f"  close=₹{close:.1f}  support=₹{tl.support:.1f}"
                  f"  below by {-dist:.1f} pts (≥{brk})")
                if pivot_str: L(f"          {pivot_str.strip()}")
                L(f"          → Buy OPPOSITE: {opp_sym}")
                L(f"          Confirm → next candle [{_t(next_opp['ts'])}]"
                  f"  O=₹{next_opp['o']:.1f}  H=₹{next_opp['h']:.1f}"
                  f"  need H ≥ O+1.5=₹{next_opp['o']+1.5:.1f}  → "
                  f"{'✅ CONFIRMED' if confirmed else '❌ FAILED'}")

                if not (p_min <= cs_opp[idx]["c"] <= p_max):
                    L(f"          ⏭️  SKIP — opposite premium ₹{cs_opp[idx]['c']:.1f} outside ₹{p_min}–₹{p_max}")
                    continue

                if confirmed:
                    entry   = round(next_opp["o"] + cfg["slippage_pts"], 2)
                    init_sl = round(entry - cfg["break_initial_sl"], 2)

                    t = ReplayTrade(
                        symbol=opp_sym, strike=strike, opt_type=opp_type,
                        signal="BREAK", entry_idx=idx + 1,
                        entry_time=_t(next_opp["ts"]),
                        entry_price=entry, qty=qty,
                        target=None, sl=init_sl,
                        trail_activate=cfg["break_trail_act"],
                        trail_by=cfg["break_trail_by"], peak=entry,
                        tl_pivot1_time=_t(p1.ts) if p1 else "",
                        tl_pivot1_price=p1.price if p1 else 0,
                        tl_pivot2_time=_t(p2.ts), tl_pivot2_price=p2.price,
                        tl_support_at_signal=tl.support,
                        signal_candle_time=time_str, signal_candle_close=close,
                    )
                    active_trade = t
                    active_sym   = opp_sym
                    L(f"          📈 ENTRY @ [{_t(next_opp['ts'])}]"
                      f"  open=₹{next_opp['o']:.1f} + slip=₹{cfg['slippage_pts']}"
                      f"  → entry=₹{entry:.1f}")
                    L(f"          SL=₹{init_sl:.1f}  trailing only"
                      f"  trail_act=+{cfg['break_trail_act']}pts"
                      f"  trail_by={cfg['break_trail_by']}pts  qty={qty}")
                    break   # sequential: stop scanning this candle

    # ── force EOD close ────────────────────────────────────────────────────
    L(f"\n  [15:30] Market close — force-closing open positions")
    if active_trade is not None:
        t  = active_trade
        cs = candles_by_sym[active_sym]
        if cs:
            last = cs[-1]
            t.exit_time   = _t(last["ts"])
            t.exit_price  = round(last["c"], 2)
            t.exit_reason = "EOD"
            t.pts = round(t.exit_price - t.entry_price, 2)
            t.pnl = round(t.pts * t.qty, 2)
            sign = "✅" if t.pnl > 0 else "❌"
            L(f"  [EOD]  {active_sym:24s}  {sign} CLOSED"
              f"  entry=₹{t.entry_price:.1f} → exit=₹{t.exit_price:.1f}"
              f"  pts={t.pts:+.1f}  P&L=₹{t.pnl:+,.0f}")
            trades.append(t)
    else:
        L(f"  No open position at close.")

    return trades, lines


def print_summary(trades: List[ReplayTrade], lines: List[str], day: str):
    total_pnl = sum(t.pnl for t in trades)
    wins      = [t for t in trades if t.pnl > 0]
    losses    = [t for t in trades if t.pnl <= 0]

    lines.append("\n" + "═" * 100)
    lines.append(f"  DAY SUMMARY  {day}  |  {len(trades)} trades  |  "
                 f"wins={len(wins)}  losses={len(losses)}  "
                 f"win%={len(wins)/len(trades)*100:.0f}%  "
                 f"total P&L = ₹{total_pnl:+,.2f}")
    lines.append("─" * 100)
    lines.append(f"  {'#':2s}  {'SYMBOL':24s}  {'SIG':6s}  {'ENTRY_T':6s}  "
                 f"{'ENTRY':7s}  {'EXIT_T':6s}  {'EXIT':7s}  "
                 f"{'PTS':6s}  {'P&L':9s}  {'REASON':12s}  VERIFY ON CHART")
    lines.append("─" * 100)
    for i, t in enumerate(sorted(trades, key=lambda x: x.entry_time), 1):
        sign = "✅" if t.pnl > 0 else "❌"
        verify = (f"open {t.symbol} chart → candle [{t.signal_candle_time}]"
                  f" close=₹{t.signal_candle_close:.1f}"
                  f" | TL pivots [{t.tl_pivot1_time}]₹{t.tl_pivot1_price:.1f}"
                  f"→[{t.tl_pivot2_time}]₹{t.tl_pivot2_price:.1f}")
        lines.append(f"  {i:2d}  {t.symbol:24s}  {t.signal:6s}"
                     f"  {t.entry_time:6s}  ₹{t.entry_price:6.1f}"
                     f"  {t.exit_time:6s}  ₹{t.exit_price:6.1f}"
                     f"  {t.pts:+5.1f}  ₹{t.pnl:+8,.0f}  {t.exit_reason:12s}  {sign}")
        lines.append(f"       VERIFY: {verify}")

    lines.append("═" * 100)

    # group by signal
    for sig in ["BOUNCE", "BREAK"]:
        ts = [t for t in trades if t.signal == sig]
        if ts:
            p = sum(t.pnl for t in ts)
            w = sum(1 for t in ts if t.pnl > 0)
            lines.append(f"  {sig}: {len(ts)} trades  wins={w}  "
                         f"win%={w/len(ts)*100:.0f}%  total=₹{p:+,.0f}")
    lines.append("")

    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date",     required=True, help="YYYY-MM-DD")
    ap.add_argument("--index",    default=CFG["index"])
    ap.add_argument("--exchange", default=CFG["exchange"])
    ap.add_argument("--expiry",   default=CFG["expiry_date"])
    ap.add_argument("--scan_range",  type=int,   default=CFG["scan_range"])
    ap.add_argument("--premium_min", type=float, default=CFG["premium_min"])
    ap.add_argument("--premium_max", type=float, default=CFG["premium_max"])
    args = ap.parse_args()

    target_day = datetime.strptime(args.date, "%Y-%m-%d").date()
    expiry     = datetime.strptime(args.expiry, "%Y-%m-%d").date()
    index      = args.index
    exchange   = args.exchange

    print(f"\n  TRENDLINE REPLAY  {target_day}  |  {index}  expiry={expiry}")

    sess = _build_session()

    # Fetch spot for ATM
    try:
        r = requests.get(
            f"https://groww.in/v1/api/stocks_data/v1/tr_live_indices"
            f"/exchange/{exchange}/segment/CASH/{index}/latest",
            headers={"x-app-id": "growwWeb", "x-device-id": DEVICE_ID,
                     "x-platform": "web", "user-agent": "Mozilla/5.0"},
            timeout=8)
        spot = float(r.json().get("value", 24000))
    except Exception:
        spot = 24000.0
    atm  = int(round(spot / CFG["strike_step"]) * CFG["strike_step"])
    CFG["premium_min"] = args.premium_min
    CFG["premium_max"] = args.premium_max
    r_   = args.scan_range
    strikes = [atm + i * CFG["strike_step"] for i in range(-r_, r_ + 1)]
    print(f"  Spot={spot:.0f}  ATM={atm}  strikes={strikes[0]}..{strikes[-1]}")

    # Date range: fetch enough to cover target_day
    end_ms   = int(datetime.utcnow().timestamp() * 1000)
    start_ms = end_ms - 20 * 24 * 3600 * 1000

    # Fetch all candles
    all_syms = [make_symbol(index, expiry, s, ot)
                for s in strikes for ot in ["CE", "PE"]]
    print(f"  Fetching {len(all_syms)} symbols...")
    raw: Dict[str, list] = {}
    for sym in all_syms:
        raw[sym] = fetch_candles_v4(sess, sym, exchange, start_ms, end_ms)

    # Filter to target day only
    day_candles: Dict[str, list] = {}
    for sym, cs in raw.items():
        day_candles[sym] = [c for c in cs if _ist_date(c["ts"]) == target_day]

    n_data = sum(1 for v in day_candles.values() if v)
    if n_data == 0:
        print(f"  ❌ No candle data for {target_day}. Try a trading day.")
        sys.exit(1)

    sample_bars = next(len(v) for v in day_candles.values() if v)
    print(f"  {n_data} instruments have data  |  ~{sample_bars} candles for {target_day}\n")

    # Run replay
    trades, log_lines = replay_day(
        day=target_day,
        candles_by_sym=day_candles,
        strikes=strikes,
        expiry=expiry,
        index=index,
        exchange=exchange,
        cfg=CFG,
    )

    log_lines = print_summary(trades, log_lines, str(target_day))

    # Save log
    os.makedirs("logs/replay", exist_ok=True)
    log_path  = f"logs/replay/trendline_replay_{target_day}.log"
    json_path = f"logs/replay/trendline_replay_{target_day}.json"

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
    print(f"  Log saved  → {log_path}")

    # Save JSON trades
    def _d(t: ReplayTrade): return {
        "date":               str(target_day),
        "symbol":             t.symbol,
        "strike":             t.strike,
        "opt_type":           t.opt_type,
        "signal":             t.signal,
        "entry_time":         t.entry_time,
        "entry_price":        t.entry_price,
        "exit_time":          t.exit_time,
        "exit_price":         t.exit_price,
        "exit_reason":        t.exit_reason,
        "pts":                t.pts,
        "pnl_1lot":           t.pnl,
        "pnl_18lots":         round(t.pnl * 18, 2),
        "qty_1lot":           t.qty,
        "qty_18lots":         t.qty * 18,
        "chart_verify": {
            "symbol":              t.symbol,
            "signal_candle":       t.signal_candle_time,
            "signal_candle_close": t.signal_candle_close,
            "trendline_pivot1":    {"time": t.tl_pivot1_time, "price": t.tl_pivot1_price},
            "trendline_pivot2":    {"time": t.tl_pivot2_time, "price": t.tl_pivot2_price},
            "trendline_support":   t.tl_support_at_signal,
            "how_to_verify":       (
                f"1. Open {t.symbol} on Groww chart (5-min candles)\n"
                f"2. Find candle at {t.signal_candle_time} → close should be ₹{t.signal_candle_close:.1f}\n"
                f"3. Draw trendline through lows: "
                f"[{t.tl_pivot1_time}]₹{t.tl_pivot1_price:.1f} → "
                f"[{t.tl_pivot2_time}]₹{t.tl_pivot2_price:.1f}\n"
                f"4. At {t.signal_candle_time} the projected trendline = ₹{t.tl_support_at_signal:.1f}\n"
                f"5. {'Price was within 6 pts of support (BOUNCE)' if t.signal == 'BOUNCE' else 'Price broke 3 pts below support (BREAK)'}"
            )
        }
    }

    with open(json_path, "w") as f:
        json.dump([_d(t) for t in trades], f, indent=2)
    print(f"  JSON saved → {json_path}\n")

    # Print short console summary
    total_pnl = sum(t.pnl for t in trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    print(f"  {target_day}  |  {len(trades)} trades  |  "
          f"wins={wins}  win%={wins/len(trades)*100:.0f}%  "
          f"P&L (1 lot) = ₹{total_pnl:+,.0f}  "
          f"P&L (18 lots) = ₹{total_pnl*18:+,.0f}\n")


if __name__ == "__main__":
    main()
