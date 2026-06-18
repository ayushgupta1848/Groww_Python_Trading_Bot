#!/usr/bin/env python3
"""
PATTERN_ANALYZER.py
══════════════════════════════════════════════════════════════════════════════
Deep analysis of last week's option premium charts to discover which patterns
consistently precede big profitable moves.

Fetches last week's 5-min data for ~26 NIFTY options (ATM ± 6 strikes, CE+PE)
and runs 5 pattern detectors on each symbol each day.

PATTERNS TESTED
───────────────
1. MULTI_BOUNCE   — 3+ candle touches on ascending support → bounce entry
2. MULTI_BREAK    — 3+ touch ascending support breaks → enter opposite option
3. COMPRESSION    — 4+ candles with H-L < 10 pts then breakout (explosive move)
4. MORNING_FLAG   — First-hour high/low established → afternoon channel break
5. TREND_RIDE     — 3+ consecutive higher lows (CE) or lower highs (PE) → momentum

OUTPUT
──────
• Day-wise P&L for each pattern type
• Win rate, avg P&L, max single trade P&L
• Best performing strikes and premium ranges
• Time-of-day analysis: which hours fire most profitable signals

Usage:
    python3 PATTERN_ANALYZER.py
    python3 PATTERN_ANALYZER.py --lots 18 --strikes 8 --days 7
══════════════════════════════════════════════════════════════════════════════
"""

import sys, json, os, argparse, math
from datetime import datetime, date, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
import requests

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════
CFG = {
    "index":        "NIFTY",
    "exchange":     "NSE",
    "expiry_date":  "2026-06-23",
    "strike_step":  50,
    "scan_strikes": 8,          # ATM ± N strikes
    "premium_min":  50.0,       # wider range to catch more patterns
    "premium_max":  300.0,
    "lots":         1,          # override with --lots
    "interval_min": 5,
    "days_back":    10,         # last ~7-8 trading days

    # Trendline engine params (same as live bot)
    "pivot_lookback": 3,
    "proximity_pts":  6.0,
    "break_pts":      3.0,

    # Multi-touch thresholds
    "min_candle_touches": 3,    # must touch trendline this many times to be valid

    # BOUNCE params
    "slippage_pts":     0.5,
    "target_buffer":    2.0,
    "trendline_sl_buf": 3.0,
    "bounce_trail_act": 5.0,
    "bounce_trail_by":  4.0,

    # BREAK params
    "break_initial_sl": 5.0,
    "break_trail_act":  4.0,
    "break_trail_by":   3.0,

    # COMPRESSION params
    "comp_range_max":   10.0,   # H-L < this over 4 candles = compression
    "comp_candles":     4,      # min candles in compression zone
    "comp_break_pts":   2.0,    # breakout confirmation

    # TREND_RIDE params
    "trend_consecutive": 3,     # N consecutive higher lows (CE) / lower highs (PE)
    "trend_trail_act":   6.0,
    "trend_trail_by":    5.0,

    "BEARER_TOKEN": "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjE3ODE3NDI2MDAsImlhdCI6MTc4MTcyNTQ2NywibmJmIjoxNzgxNzI1NDY3LCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCI0NjM2YjM2Yy04Njc3LTQ0YjgtOGI1OC0yM2UwMTU3YmEyYzBcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZXNzaW9uSWRcIjpcIjBmMWEyNjM0LTU1NmQtNGQyMy04YjlhLTllMDc4OTFlZjYzZlwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcIm9yZGVyLWJhc2ljLGxpdmVfZGF0YS1iYXNpYyxub25fdHJhZGluZy1iYXNpYyxvcmRlcl9yZWFkX29ubHktYmFzaWMsYmFja190ZXN0XCIsXCJzb3VyY2VJcEFkZHJlc3NcIjpudWxsLFwidHdvRmFFeHBpcnlUc1wiOjE3ODE3NDI2MDAwMDAsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.oXD0VsXs5IcnxbuMD6maLoegnrKbFqh2rFedw64CI50R2OG17JXTSwmSfuo14TvW3kIVRvx7zkzGjjIPiV14lw",
    "COOKIES": "_gcl_au=1.1.863312266.1778482315; _ga=GA1.1.389629575.1778482316; AUTH_SESSION_ID=U2FsdGVkX1%2Fm%2B7z%2FG12g6BvXLSCnQzZynC%2FGqjaCZD8dD9Wfuga8Z%2Ba9etM8FsuG78M9bPunW8xQVIg3A%2FPuDQ%3D%3D",
    "device_id": "6063193d-efd0-59eb-83c4-5d64fd77d747",
}

LOT_SIZES  = {"NIFTY": 75, "BANKNIFTY": 15, "FINNIFTY": 40, "SENSEX": 20, "BANKEX": 15}
IST_OFFSET = 19800

# ═══════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class PatternTrade:
    date:        str
    symbol:      str
    pattern:     str         # MULTI_BOUNCE | MULTI_BREAK | COMPRESSION | TREND_RIDE
    entry_time:  str
    entry_price: float
    exit_time:   str
    exit_price:  float
    exit_reason: str         # SL | TRAIL_SL | TARGET | EOD
    pts:         float
    pnl:         float       # ₹ with lots
    qty:         int
    touches:     int = 0     # trendline touch count (for trendline patterns)
    premium_at_entry: float = 0.0

# ═══════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════
def _ist_dt(ts: int) -> datetime:
    return datetime.utcfromtimestamp(ts + IST_OFFSET)

def _ist_date(ts: int) -> date:
    return _ist_dt(ts).date()

def lot_size(index: str) -> int:
    return LOT_SIZES.get(index.upper(), 75)

def make_symbol(index: str, expiry_str: str, strike: int, opt_type: str) -> str:
    exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    return f"{index}{exp.year % 100}{exp.month}{exp.day:02d}{int(strike)}{opt_type}"

# ═══════════════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════════════
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept":        "application/json, text/plain, */*",
        "authorization": f"Bearer {CFG['BEARER_TOKEN']}",
        "x-app-id":      "growwWeb",
        "x-device-id":   CFG["device_id"],
        "x-platform":    "web",
        "referer":       "https://groww.in/charts/options/nifty/",
        "user-agent":    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    })
    for part in CFG["COOKIES"].split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            s.cookies.set(k.strip(), v.strip())
    return s

def fetch_candles(sess: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list:
    url = (f"https://groww.in/v1/api/stocks_fo_data/v4/charting_service/chart"
           f"/exchange/{CFG['exchange']}/segment/FNO/{symbol}"
           f"?startTimeInMillis={start_ms}&endTimeInMillis={end_ms}"
           f"&intervalInMinutes={CFG['interval_min']}")
    try:
        r = sess.get(url, timeout=12)
        r.raise_for_status()
        return [{"ts": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                 "l": float(c[3]), "c": float(c[4])} for c in r.json().get("candles", [])]
    except Exception:
        return []

def fetch_spot(sess: requests.Session) -> float:
    url = (f"https://groww.in/v1/api/stocks_data/v1/tr_live_indices"
           f"/exchange/{CFG['exchange']}/segment/CASH/{CFG['index']}/latest")
    try:
        r = requests.get(url, headers={"x-app-id": "growwWeb", "user-agent": "Mozilla/5.0"}, timeout=8)
        return float(r.json().get("value", 24100))
    except Exception:
        return 24100.0

# ═══════════════════════════════════════════════════════════════════════════
# TRENDLINE ENGINE — MULTI-TOUCH
# ═══════════════════════════════════════════════════════════════════════════
def _find_pivot_lows(candles: list, lb: int) -> List[Tuple[int, float]]:
    """Return (idx, low_price) for each swing low."""
    pivots, n = [], len(candles)
    for i in range(lb, n - lb):
        lo = candles[i]["l"]
        if (all(candles[i-j]["l"] > lo for j in range(1, lb+1)) and
                all(candles[i+j]["l"] > lo for j in range(1, lb+1))):
            pivots.append((i, lo))
    return pivots

def _find_pivot_highs(candles: list, lb: int) -> List[Tuple[int, float]]:
    pivots, n = [], len(candles)
    for i in range(lb, n - lb):
        hi = candles[i]["h"]
        if (all(candles[i-j]["h"] < hi for j in range(1, lb+1)) and
                all(candles[i+j]["h"] < hi for j in range(1, lb+1))):
            pivots.append((i, hi))
    return pivots

def compute_multi_touch_tl(candles: list, lb: int, min_touches: int, prox: float):
    """
    Find the ascending support trendline with the most CANDLE LOW touches.

    Tries all pairs of pivot lows as anchor lines.
    Counts how many candle lows (not just pivots) are within ±prox of the line.
    Returns (slope, base_idx, base_price, touch_count, projected_support) or None.
    """
    if len(candles) < lb * 2 + 3:
        return None

    pivots  = _find_pivot_lows(candles, lb)
    cur_idx = len(candles) - 1

    if len(pivots) < 2:
        return None

    best = None
    best_n = 0

    for a in range(len(pivots)):
        for b in range(a + 1, len(pivots)):
            i1, p1 = pivots[a]
            i2, p2 = pivots[b]

            if p2 <= p1:          # only ascending
                continue
            if (i2 - i1) == 0:
                continue

            slope = (p2 - p1) / (i2 - i1)

            # Count every candle low that touches (or nearly touches) this line
            touch_count = 0
            for i in range(i1, cur_idx + 1):
                line_val = p1 + slope * (i - i1)
                c = candles[i]
                # Touch: low within ±prox of trendline AND candle didn't close far below
                if abs(c["l"] - line_val) <= prox and c["c"] >= line_val - prox:
                    touch_count += 1

            if touch_count >= min_touches and touch_count > best_n:
                projected = p1 + slope * (cur_idx - i1)
                if projected > 0:
                    best   = (slope, i1, p1, touch_count, projected)
                    best_n = touch_count

    return best  # None if not found

# ═══════════════════════════════════════════════════════════════════════════
# TRADE EXECUTION HELPER
# ═══════════════════════════════════════════════════════════════════════════
def _simulate_forward(candles: list, entry_idx: int, entry_price: float,
                      sl: float, target: Optional[float],
                      trail_act: float, trail_by: float, qty: int,
                      day_str: str, symbol: str, pattern: str,
                      touches: int, premium_at_entry: float) -> PatternTrade:
    """Walk candles forward from entry, apply exit rules, return PatternTrade."""
    peak        = entry_price
    trail_active = False

    for i in range(entry_idx + 1, len(candles)):
        c    = candles[i]
        h, l = c["h"], c["l"]

        if h > peak:
            peak = h

        profit = h - entry_price
        if not trail_active and profit >= trail_act:
            trail_active = True
            new_sl = round(h - trail_by, 2)
            if new_sl > sl:
                sl = new_sl

        if trail_active:
            new_sl = round(peak - trail_by, 2)
            if new_sl > sl:
                sl = new_sl

        if target and h >= target:
            pts = round(target - entry_price, 2)
            return PatternTrade(
                date=day_str, symbol=symbol, pattern=pattern,
                entry_time=_ist_dt(candles[entry_idx]["ts"]).strftime("%H:%M"),
                entry_price=entry_price,
                exit_time=_ist_dt(c["ts"]).strftime("%H:%M"),
                exit_price=target, exit_reason="TARGET",
                pts=pts, pnl=round(pts * qty, 2), qty=qty,
                touches=touches, premium_at_entry=premium_at_entry,
            )

        if l <= sl:
            exit_price = sl
            reason     = "TRAIL_SL" if trail_active else "SL"
            pts        = round(exit_price - entry_price, 2)
            return PatternTrade(
                date=day_str, symbol=symbol, pattern=pattern,
                entry_time=_ist_dt(candles[entry_idx]["ts"]).strftime("%H:%M"),
                entry_price=entry_price,
                exit_time=_ist_dt(c["ts"]).strftime("%H:%M"),
                exit_price=exit_price, exit_reason=reason,
                pts=pts, pnl=round(pts * qty, 2), qty=qty,
                touches=touches, premium_at_entry=premium_at_entry,
            )

    # EOD
    last  = candles[-1]
    pts   = round(last["c"] - entry_price, 2)
    return PatternTrade(
        date=day_str, symbol=symbol, pattern=pattern,
        entry_time=_ist_dt(candles[entry_idx]["ts"]).strftime("%H:%M"),
        entry_price=entry_price,
        exit_time=_ist_dt(last["ts"]).strftime("%H:%M"),
        exit_price=round(last["c"], 2), exit_reason="EOD",
        pts=pts, pnl=round(pts * qty, 2), qty=qty,
        touches=touches, premium_at_entry=premium_at_entry,
    )

# ═══════════════════════════════════════════════════════════════════════════
# PATTERN DETECTORS
# ═══════════════════════════════════════════════════════════════════════════

def detect_multi_bounce(candles: list, day_str: str, symbol: str,
                        opt_type: str, qty: int, cfg: dict) -> List[PatternTrade]:
    """
    MULTI_BOUNCE: 3+ candle touches on ascending support → close near support → bounce.
    Same as current bot but requires min_candle_touches >= 3.
    """
    trades    = []
    lb        = cfg["pivot_lookback"]
    prox      = cfg["proximity_pts"]
    min_t     = cfg["min_candle_touches"]
    p_min     = cfg["premium_min"]
    p_max     = cfg["premium_max"]
    n         = len(candles)
    in_trade  = False
    trade_end_idx = -1

    for idx in range(lb * 2 + 3, n - 1):
        if in_trade and idx <= trade_end_idx:
            continue
        in_trade = False

        close = candles[idx]["c"]
        if not (p_min <= close <= p_max):
            continue

        tl = compute_multi_touch_tl(candles[:idx + 1], lb, min_t, prox)
        if not tl:
            continue

        slope, base_i, base_p, touch_count, support = tl
        dist  = close - support

        if not (0.0 <= dist <= prox):
            continue

        # Confirm: next candle high >= close + 2
        next_c = candles[idx + 1]
        if next_c["h"] < close + 2.0:
            continue

        # Last swing high for target
        pivot_highs = _find_pivot_highs(candles[:idx + 1], lb)
        swing_high  = max((p for _, p in pivot_highs), default=0.0)

        entry    = round(next_c["o"] + cfg["slippage_pts"], 2)
        init_sl  = round(support - cfg["trendline_sl_buf"], 2)
        if entry <= init_sl:
            continue

        target = round(swing_high - cfg["target_buffer"], 2) if swing_high > entry else None
        if target and target <= entry:
            target = None

        t = _simulate_forward(
            candles, idx + 1, entry, init_sl, target,
            cfg["bounce_trail_act"], cfg["bounce_trail_by"],
            qty, day_str, symbol, "MULTI_BOUNCE",
            touch_count, close,
        )
        trades.append(t)
        in_trade      = True
        trade_end_idx = t.exit_time and next((i for i in range(idx+2, n)
                                              if _ist_dt(candles[i]["ts"]).strftime("%H:%M") >= t.exit_time), n-1)
    return trades


def detect_multi_break(candles: list, opp_candles: list, day_str: str,
                       symbol: str, opp_symbol: str, qty: int, cfg: dict) -> List[PatternTrade]:
    """
    MULTI_BREAK: 3+ touch ascending support breaks → enter OPPOSITE option.
    The key high-conviction signal: well-tested support breaks = big move.
    """
    trades   = []
    lb       = cfg["pivot_lookback"]
    prox     = cfg["proximity_pts"]
    brk      = cfg["break_pts"]
    min_t    = cfg["min_candle_touches"]
    p_min    = cfg["premium_min"]
    p_max    = cfg["premium_max"]
    n        = len(candles)
    fired    = set()   # prevent duplicate entries same candle
    in_trade = False
    trade_end_idx = -1

    for idx in range(lb * 2 + 3, n - 1):
        if in_trade and idx <= trade_end_idx:
            continue
        in_trade = False

        if idx >= len(opp_candles):
            continue

        close = candles[idx]["c"]
        if not (p_min <= close <= p_max):
            continue

        tl = compute_multi_touch_tl(candles[:idx + 1], lb, min_t, prox)
        if not tl:
            continue

        slope, base_i, base_p, touch_count, support = tl

        # Break: close is more than break_pts below support
        if close >= support - brk:
            continue

        if idx in fired:
            continue
        fired.add(idx)

        # Enter opposite option
        if idx + 1 >= len(opp_candles):
            continue
        opp_next = opp_candles[idx + 1]
        opp_close = opp_candles[idx]["c"]

        if not (p_min <= opp_close <= p_max):
            continue

        # Opposite must be moving up (confirm)
        if opp_next["h"] < opp_next["o"] + 1.5:
            continue

        entry   = round(opp_next["o"] + cfg["slippage_pts"], 2)
        init_sl = round(entry - cfg["break_initial_sl"], 2)

        t = _simulate_forward(
            opp_candles, idx + 1, entry, init_sl, None,
            cfg["break_trail_act"], cfg["break_trail_by"],
            qty, day_str, opp_symbol, "MULTI_BREAK",
            touch_count, opp_close,
        )
        trades.append(t)
        in_trade      = True
        trade_end_idx = next((i for i in range(idx+2, len(opp_candles))
                              if _ist_dt(opp_candles[i]["ts"]).strftime("%H:%M") >= t.exit_time), len(opp_candles)-1)
    return trades


def detect_compression_break(candles: list, day_str: str, symbol: str,
                              qty: int, cfg: dict) -> List[PatternTrade]:
    """
    COMPRESSION: Option trades in tight range (H-L < comp_range_max) for
    comp_candles+ consecutive candles. Then breaks out = explosive move.
    Enter in direction of break, SL = opposite end of range.
    """
    trades    = []
    p_min     = cfg["premium_min"]
    p_max     = cfg["premium_max"]
    min_c     = cfg["comp_candles"]
    rng_max   = cfg["comp_range_max"]
    brk_pts   = cfg["comp_break_pts"]
    n         = len(candles)
    in_trade  = False
    trade_end_idx = -1

    for idx in range(min_c + 1, n - 1):
        if in_trade and idx <= trade_end_idx:
            continue
        in_trade = False

        # Time filter: compression before 10:30 is too noisy (opening volatility)
        if _ist_dt(candles[idx]["ts"]).strftime("%H:%M") < "10:30":
            continue

        close = candles[idx]["c"]
        if not (p_min <= close <= p_max):
            continue

        # Check if last min_c candles form a tight range
        window = candles[idx - min_c : idx + 1]
        hi = max(c["h"] for c in window)
        lo = min(c["l"] for c in window)
        if hi - lo > rng_max:
            continue

        # Next candle breaks out
        next_c = candles[idx + 1]
        if next_c["h"] > hi + brk_pts:   # breakout UP
            entry   = round(next_c["o"] + cfg["slippage_pts"], 2)
            init_sl = round(lo - 1.0, 2)
            target  = round(entry + (hi - lo) * 2, 2)
            t = _simulate_forward(
                candles, idx + 1, entry, init_sl, target,
                cfg["bounce_trail_act"], cfg["bounce_trail_by"],
                qty, day_str, symbol, "COMPRESSION",
                0, close,
            )
            trades.append(t)
            in_trade      = True
            trade_end_idx = next((i for i in range(idx+2, n)
                                  if _ist_dt(candles[i]["ts"]).strftime("%H:%M") >= t.exit_time), n-1)

        elif next_c["l"] < lo - brk_pts:  # breakout DOWN — treat this option as falling
            entry   = round(next_c["o"] - cfg["slippage_pts"], 2)
            # For a "short" on option premium — not realistic for longs
            # We skip this for now (can't short options easily in this framework)
            pass

    return trades


def detect_trend_ride(candles: list, day_str: str, symbol: str,
                      opt_type: str, qty: int, cfg: dict) -> List[PatternTrade]:
    """
    TREND_RIDE: N consecutive higher lows + higher highs (CE) after 13:00 only.
    Time-filtered to afternoon when trending moves are more reliable.
    Max risk cap: skip if entry - SL > 12 pts.
    """
    trades    = []
    p_min     = cfg["premium_min"]
    p_max     = cfg["premium_max"]
    consec    = cfg["trend_consecutive"]
    n         = len(candles)
    in_trade  = False
    trade_end_idx = -1

    if opt_type == "PE":
        return []   # only ride CE trends upward

    for idx in range(consec + 1, n - 1):
        if in_trade and idx <= trade_end_idx:
            continue
        in_trade = False

        # Time filter: only after 13:00 IST
        candle_time = _ist_dt(candles[idx]["ts"]).strftime("%H:%M")
        if candle_time < "13:00":
            continue

        close = candles[idx]["c"]
        if not (p_min <= close <= p_max):
            continue

        # Must have BOTH consecutive higher lows AND higher highs (stronger trend)
        lows  = [candles[i]["l"] for i in range(idx - consec, idx + 1)]
        highs = [candles[i]["h"] for i in range(idx - consec, idx + 1)]
        if not all(lows[i]  > lows[i-1]  for i in range(1, len(lows))):
            continue
        if not all(highs[i] > highs[i-1] for i in range(1, len(highs))):
            continue

        next_c  = candles[idx + 1]
        entry   = round(next_c["o"] + cfg["slippage_pts"], 2)
        init_sl = round(candles[idx]["l"] - 3.0, 2)

        # Max risk cap: skip wide SL
        if entry - init_sl > 12.0:
            continue

        t = _simulate_forward(
            candles, idx + 1, entry, init_sl, None,
            cfg["trend_trail_act"], cfg["trend_trail_by"],
            qty, day_str, symbol, "TREND_RIDE",
            0, close,
        )
        trades.append(t)
        in_trade      = True
        trade_end_idx = next((i for i in range(idx+2, n)
                              if _ist_dt(candles[i]["ts"]).strftime("%H:%M") >= t.exit_time), n-1)

    return trades


def detect_morning_flag(candles: list, day_str: str, symbol: str,
                        qty: int, cfg: dict) -> List[PatternTrade]:
    """
    MORNING_FLAG: First-hour forms a flag channel (higher lows + lower highs).
    Then breakout of the flag = strong directional move.
    Entry: candle that closes above flag high, SL: flag low - 2 pts.
    """
    trades   = []
    p_min    = cfg["premium_min"]
    p_max    = cfg["premium_max"]
    n        = len(candles)

    # First hour = candles 0..11 (9:15-10:10 = 11 candles of 5-min)
    first_hour_end = min(12, n - 2)
    if first_hour_end < 6:
        return []

    fh = candles[:first_hour_end]
    fh_high = max(c["h"] for c in fh)
    fh_low  = min(c["l"] for c in fh)

    # Flag: range should be bounded (not too wide)
    if fh_high - fh_low > 50:
        return []

    in_trade = False
    trade_end_idx = -1

    for idx in range(first_hour_end, n - 1):
        if in_trade and idx <= trade_end_idx:
            continue
        in_trade = False

        close = candles[idx]["c"]
        if not (p_min <= close <= p_max):
            continue

        # Breakout above first-hour high
        if close > fh_high + 1.0:
            next_c  = candles[idx + 1]
            entry   = round(next_c["o"] + cfg["slippage_pts"], 2)
            init_sl = round(fh_low - 2.0, 2)
            target  = round(entry + (fh_high - fh_low) * 1.5, 2)

            if entry <= init_sl:
                continue
            # Max risk cap: skip if SL is too wide
            if entry - init_sl > 15.0:
                continue

            t = _simulate_forward(
                candles, idx + 1, entry, init_sl, target,
                cfg["bounce_trail_act"], cfg["bounce_trail_by"],
                qty, day_str, symbol, "MORNING_FLAG",
                0, close,
            )
            trades.append(t)
            in_trade      = True
            trade_end_idx = next((i for i in range(idx+2, n)
                                  if _ist_dt(candles[i]["ts"]).strftime("%H:%M") >= t.exit_time), n-1)
            break  # only one morning flag per day

    return trades

# ═══════════════════════════════════════════════════════════════════════════
# DAILY ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════
def analyze_day(day_str: str, symbols: List[str], all_candles: Dict[str, list],
                cfg: dict) -> List[PatternTrade]:
    """Run all pattern detectors on all symbols for one trading day."""
    qty   = lot_size(cfg["index"]) * cfg["lots"]
    trades = []
    exp   = cfg["expiry_date"]
    n_sym = cfg["scan_strikes"] * 2 + 2  # approx

    for sym in symbols:
        candles = all_candles.get(sym, [])
        if len(candles) < 20:
            continue

        # Determine opt_type from symbol ending
        opt_type = "PE" if sym.endswith("PE") else "CE"

        # MULTI_BOUNCE
        trades.extend(detect_multi_bounce(candles, day_str, sym, opt_type, qty, cfg))

        # MULTI_BREAK → needs opposite option
        # Parse strike correctly: prefix = INDEX + YY(2) + M(1or2) + DD(2)
        from datetime import datetime as _dt
        _exp = _dt.strptime(cfg["expiry_date"], "%Y-%m-%d").date()
        _pfx = f"{cfg['index']}{_exp.year % 100}{_exp.month}{_exp.day:02d}"
        if sym.startswith(_pfx):
            strike_str = sym[len(_pfx):]   # e.g. "23800CE"
        else:
            strike_str = ""
        if strike_str.endswith("CE") or strike_str.endswith("PE"):
            strike_val = int(strike_str[:-2])
            opp_type   = "PE" if opt_type == "CE" else "CE"
            opp_sym    = make_symbol(cfg["index"], exp, strike_val, opp_type)
            opp_candles = all_candles.get(opp_sym, [])
            if len(opp_candles) >= 20:
                trades.extend(detect_multi_break(candles, opp_candles, day_str,
                                                 sym, opp_sym, qty, cfg))

        # COMPRESSION
        trades.extend(detect_compression_break(candles, day_str, sym, qty, cfg))

        # TREND_RIDE (CE only)
        trades.extend(detect_trend_ride(candles, day_str, sym, opt_type, qty, cfg))

        # MORNING_FLAG
        trades.extend(detect_morning_flag(candles, day_str, sym, qty, cfg))

    return trades

# ═══════════════════════════════════════════════════════════════════════════
# SEQUENTIAL FILTER — enforces one trade at a time across ALL symbols
# ═══════════════════════════════════════════════════════════════════════════
def apply_sequential_filter(all_trades: List[PatternTrade],
                            daily_max_loss: float = -20000.0):
    """
    Strict sequential execution: once in a trade, ALL other signals on ALL
    symbols are skipped until that trade exits.  Entry at exit-candle = skip
    (can't enter during the candle that just closed the previous trade).

    Daily max-loss cap: if cumulative P&L for the day drops below
    `daily_max_loss` (default -₹20,000), all further signals are blocked
    for that day.  This prevents runaway losses on bad days without
    blocking the very next trade after two SL hits (which is too aggressive).

    Returns (filtered_trades, skipped_count).
    """
    sorted_trades = sorted(all_trades, key=lambda t: (t.date, t.entry_time))
    result        = []
    skipped       = 0
    day_exit: Dict[str, str]   = {}   # date → exit_time of current open trade
    day_pnl:  Dict[str, float] = {}   # date → running P&L

    for t in sorted_trades:
        prev_exit = day_exit.get(t.date, "00:00")
        if t.entry_time <= prev_exit:
            skipped += 1
            continue

        # Daily max-loss cap
        if day_pnl.get(t.date, 0.0) <= daily_max_loss:
            skipped += 1
            continue

        result.append(t)
        day_exit[t.date]  = t.exit_time
        day_pnl[t.date]   = day_pnl.get(t.date, 0.0) + t.pnl

    return result, skipped


# ═══════════════════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════════════════
def print_report(all_trades: List[PatternTrade], cfg: dict):
    if not all_trades:
        print("\n  No trades found across all patterns.")
        return

    lots   = cfg["lots"]
    ls     = lot_size(cfg["index"])

    print()
    print("═" * 78)
    print("  PATTERN ANALYSIS REPORT  —  SEQUENTIAL (one trade at a time)")
    print(f"  Lots: {lots}  |  Qty per trade: {lots * ls}  |  Trades taken: {len(all_trades)}")
    print("═" * 78)

    # ── By pattern ────────────────────────────────────────────────────────
    by_pattern: Dict[str, List[PatternTrade]] = defaultdict(list)
    for t in all_trades:
        by_pattern[t.pattern].append(t)

    print()
    print(f"  {'PATTERN':<18}  {'#':>4}  {'WIN%':>6}  {'AVG P&L':>10}  {'MAX':>8}  {'MIN':>8}  {'TOTAL':>10}")
    print(f"  {'─'*18}  {'─'*4}  {'─'*6}  {'─'*10}  {'─'*8}  {'─'*8}  {'─'*10}")

    pattern_order = ["MULTI_BOUNCE", "MULTI_BREAK", "COMPRESSION", "TREND_RIDE", "MORNING_FLAG"]
    for pat in pattern_order:
        ts = by_pattern.get(pat, [])
        if not ts:
            continue
        wins     = [t for t in ts if t.pnl > 0]
        win_rate = len(wins) / len(ts) * 100 if ts else 0
        total_pnl = sum(t.pnl for t in ts)
        avg_pnl   = total_pnl / len(ts)
        max_pnl   = max(t.pnl for t in ts)
        min_pnl   = min(t.pnl for t in ts)
        print(f"  {pat:<18}  {len(ts):>4}  {win_rate:>5.0f}%  {avg_pnl:>+10,.0f}  "
              f"{max_pnl:>+8,.0f}  {min_pnl:>+8,.0f}  {total_pnl:>+10,.0f}")

    # ── Total ──────────────────────────────────────────────────────────────
    total_pnl = sum(t.pnl for t in all_trades)
    print(f"  {'─'*18}  {'─'*4}  {'─'*6}  {'─'*10}  {'─'*8}  {'─'*8}  {'─'*10}")
    wins_all   = [t for t in all_trades if t.pnl > 0]
    print(f"  {'COMBINED':<18}  {len(all_trades):>4}  {len(wins_all)/len(all_trades)*100:>5.0f}%"
          f"  {'':>10}  {'':>8}  {'':>8}  {total_pnl:>+10,.0f}")

    # ── Day-wise P&L ──────────────────────────────────────────────────────
    print()
    print("  DAY-WISE P&L  (all patterns combined, sequential — one trade at a time)")
    print(f"  {'DATE':<12}  {'TRADES':>6}  {'WINS':>5}  {'P&L':>12}  BREAKDOWN")
    print(f"  {'─'*12}  {'─'*6}  {'─'*5}  {'─'*12}  {'─'*30}")

    by_day: Dict[str, List[PatternTrade]] = defaultdict(list)
    for t in all_trades:
        by_day[t.date].append(t)

    total_by_day = 0
    for day_str in sorted(by_day.keys()):
        ts      = by_day[day_str]
        wins    = sum(1 for t in ts if t.pnl > 0)
        day_pnl = sum(t.pnl for t in ts)
        total_by_day += day_pnl
        sign    = "✅" if day_pnl >= 0 else "❌"
        breakdown = "  ".join(f"{t.pattern[:6]}:{t.pts:+.0f}" for t in sorted(ts, key=lambda x: x.entry_time)[:4])
        print(f"  {day_str:<12}  {len(ts):>6}  {wins:>5}  ₹{day_pnl:>+10,.0f}  {sign} {breakdown}")

    print(f"  {'─'*12}  {'─'*6}  {'─'*5}  {'─'*12}")
    print(f"  {'TOTAL':<12}  {len(all_trades):>6}  {sum(1 for t in all_trades if t.pnl>0):>5}  ₹{total_by_day:>+10,.0f}")

    # ── Time of day analysis ────────────────────────────────────────────────
    print()
    print("  TIME-OF-DAY  (which hour fires the most profitable signals)")
    print(f"  {'HOUR':>8}  {'TRADES':>6}  {'WIN%':>6}  {'AVG P&L':>10}  {'TOTAL':>10}")
    print(f"  {'─'*8}  {'─'*6}  {'─'*6}  {'─'*10}  {'─'*10}")
    by_hour: Dict[str, list] = defaultdict(list)
    for t in all_trades:
        hr = t.entry_time[:5]
        by_hour[hr].append(t)
    for hr in sorted(by_hour.keys()):
        ts     = by_hour[hr]
        wins   = sum(1 for t in ts if t.pnl > 0)
        t_pnl  = sum(t.pnl for t in ts)
        a_pnl  = t_pnl / len(ts)
        print(f"  {hr:>8}  {len(ts):>6}  {wins/len(ts)*100:>5.0f}%  {a_pnl:>+10,.0f}  {t_pnl:>+10,.0f}")

    # ── Premium range analysis ──────────────────────────────────────────────
    print()
    print("  PREMIUM RANGE AT ENTRY  (which premium level gives best results)")
    ranges = [(50,100), (100,150), (150,200), (200,250), (250,300)]
    print(f"  {'RANGE':>12}  {'TRADES':>6}  {'WIN%':>6}  {'AVG':>10}  {'TOTAL':>10}")
    print(f"  {'─'*12}  {'─'*6}  {'─'*6}  {'─'*10}  {'─'*10}")
    for lo, hi in ranges:
        ts = [t for t in all_trades if lo <= t.premium_at_entry < hi]
        if not ts:
            continue
        wins   = sum(1 for t in ts if t.pnl > 0)
        t_pnl  = sum(t.pnl for t in ts)
        a_pnl  = t_pnl / len(ts)
        print(f"  ₹{lo:>4}–{hi:<4}     {len(ts):>6}  {wins/len(ts)*100:>5.0f}%  {a_pnl:>+10,.0f}  {t_pnl:>+10,.0f}")

    # ── Multi-touch quality analysis ────────────────────────────────────────
    tl_trades = [t for t in all_trades if t.touches > 0]
    if tl_trades:
        print()
        print("  TRENDLINE TOUCH COUNT  (how many touches → how profitable?)")
        print(f"  {'TOUCHES':>8}  {'TRADES':>6}  {'WIN%':>6}  {'AVG P&L':>10}")
        print(f"  {'─'*8}  {'─'*6}  {'─'*6}  {'─'*10}")
        for tc in range(3, 10):
            ts = [t for t in tl_trades if t.touches == tc]
            if not ts:
                continue
            wins  = sum(1 for t in ts if t.pnl > 0)
            a_pnl = sum(t.pnl for t in ts) / len(ts)
            print(f"  {tc:>8}  {len(ts):>6}  {wins/len(ts)*100:>5.0f}%  {a_pnl:>+10,.0f}")

    # ── Top 10 individual trades ────────────────────────────────────────────
    print()
    print("  TOP 10 TRADES BY P&L")
    print(f"  {'DATE':<10}  {'SYMBOL':<26}  {'PAT':<14}  {'ENTRY':>7}  {'EXIT':>7}  {'PTS':>6}  {'P&L':>10}  {'REASON':<10}  {'T'}")
    print(f"  {'─'*10}  {'─'*26}  {'─'*14}  {'─'*7}  {'─'*7}  {'─'*6}  {'─'*10}  {'─'*10}  {'─'*4}")
    for t in sorted(all_trades, key=lambda x: x.pnl, reverse=True)[:10]:
        tc = f"{t.touches}" if t.touches else "-"
        print(f"  {t.date:<10}  {t.symbol:<26}  {t.pattern:<14}  "
              f"{t.entry_time:>7}  {t.exit_time:>7}  {t.pts:>+6.1f}  ₹{t.pnl:>+9,.0f}  "
              f"{t.exit_reason:<10}  {tc}")

    print()
    print("═" * 78)
    print(f"  BEST PATTERN    : {max(by_pattern, key=lambda p: sum(t.pnl for t in by_pattern[p]))}")
    best_pt = [p for p in pattern_order if by_pattern.get(p)]
    if best_pt:
        best_wrate = max(best_pt, key=lambda p: len([t for t in by_pattern[p] if t.pnl > 0]) / max(len(by_pattern[p]),1))
        print(f"  HIGHEST WIN RATE: {best_wrate}")
    print(f"  TOTAL P&L (week): ₹{total_by_day:+,.0f}")
    print("═" * 78)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots",    type=int,   default=CFG["lots"])
    ap.add_argument("--strikes", type=int,   default=CFG["scan_strikes"])
    ap.add_argument("--days",    type=int,   default=CFG["days_back"])
    ap.add_argument("--min_touches", type=int, default=CFG["min_candle_touches"])
    ap.add_argument("--premium_min",  type=float, default=CFG["premium_min"])
    ap.add_argument("--premium_max",  type=float, default=CFG["premium_max"])
    args = ap.parse_args()

    CFG["lots"]               = args.lots
    CFG["scan_strikes"]       = args.strikes
    CFG["days_back"]          = args.days
    CFG["min_candle_touches"] = args.min_touches
    CFG["premium_min"]        = args.premium_min
    CFG["premium_max"]        = args.premium_max

    print("═" * 78)
    print("  PATTERN ANALYZER  —  MULTI-PATTERN DEEP ANALYSIS")
    print(f"  Index   : {CFG['index']}  ({CFG['exchange']})")
    print(f"  Expiry  : {CFG['expiry_date']}")
    print(f"  Lots    : {CFG['lots']}  (qty = {lot_size(CFG['index']) * CFG['lots']})")
    print(f"  Premium : ₹{CFG['premium_min']}–₹{CFG['premium_max']}")
    print(f"  Min touches for trendline signals: {CFG['min_candle_touches']}")
    print("═" * 78)

    sess = _build_session()

    # ── Spot → ATM → strikes ───────────────────────────────────────────────
    print("\n  Fetching spot price...")
    spot  = fetch_spot(sess)
    atm   = int(round(spot / CFG["strike_step"]) * CFG["strike_step"])
    r     = CFG["scan_strikes"]
    ss    = CFG["strike_step"]
    strikes = [atm + i * ss for i in range(-r, r + 1)]

    symbols = [make_symbol(CFG["index"], CFG["expiry_date"], s, ot)
               for s in strikes for ot in ["CE", "PE"]]

    print(f"  Spot: ₹{spot:,.2f}  →  ATM: {atm}")
    print(f"  Strikes: {strikes[0]}–{strikes[-1]}  ({len(strikes)} strikes × 2 = {len(symbols)} symbols)")

    # ── Fetch all candles ─────────────────────────────────────────────────
    now_utc  = datetime.utcnow()
    end_ms   = int(now_utc.timestamp() * 1000)
    start_ms = int((now_utc - timedelta(days=CFG["days_back"] + 1)).timestamp() * 1000)

    print(f"\n  Fetching {len(symbols)} symbols × {CFG['days_back']} days of data...")
    all_candles: Dict[str, list] = {}
    found = 0
    for sym in symbols:
        c = fetch_candles(sess, sym, start_ms, end_ms)
        all_candles[sym] = c
        if c:
            found += 1

    print(f"  {found}/{len(symbols)} symbols have data")

    # ── Group candles by date ─────────────────────────────────────────────
    date_to_candles: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for sym, candles in all_candles.items():
        for c in candles:
            d = str(_ist_date(c["ts"]))
            date_to_candles[d][sym].append(c)

    trading_dates = sorted(date_to_candles.keys())
    print(f"  Trading dates found: {trading_dates}")

    # ── Run pattern analysis day by day ───────────────────────────────────
    print(f"\n  Running {len(trading_dates)} days × {len(symbols)} symbols × 5 patterns...")
    print(f"  (Raw signal counts below — sequential filter applied after all days)\n")
    all_trades: List[PatternTrade] = []

    for day_str in trading_dates:
        day_c = dict(date_to_candles[day_str])   # sym → candles (sorted)
        for sym in day_c:
            day_c[sym].sort(key=lambda x: x["ts"])

        day_trades = analyze_day(day_str, symbols, day_c, CFG)
        all_trades.extend(day_trades)
        day_pnl = sum(t.pnl for t in day_trades)
        sign    = "✅" if day_pnl >= 0 else "❌"
        pat_counts = defaultdict(int)
        for t in day_trades:
            pat_counts[t.pattern] += 1
        breakdown = "  ".join(f"{p}:{n}" for p, n in sorted(pat_counts.items()))
        print(f"  {sign}  {day_str}  {len(day_trades):2d} trades  ₹{day_pnl:+,.0f}  |  {breakdown}")

    # ── Apply sequential filter ───────────────────────────────────────────
    total_opportunities = len(all_trades)
    seq_trades, skipped = apply_sequential_filter(all_trades)
    print(f"\n  Opportunities (parallel) : {total_opportunities}")
    print(f"  Trades taken (sequential): {len(seq_trades)}  (skipped {skipped} overlapping signals)")

    # ── Print full report ─────────────────────────────────────────────────
    print_report(seq_trades, CFG)

    # ── Save JSON ──────────────────────────────────────────────────────────
    os.makedirs("logs/analysis", exist_ok=True)
    out = f"logs/analysis/pattern_analysis_{now_utc.strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w") as f:
        json.dump([{k: v for k, v in t.__dict__.items()} for t in seq_trades], f, indent=2)
    print(f"\n  Sequential trade log saved → {out}")


if __name__ == "__main__":
    main()
