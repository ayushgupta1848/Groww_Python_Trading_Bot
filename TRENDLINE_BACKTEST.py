#!/usr/bin/env python3
"""
TRENDLINE_BACKTEST.py
──────────────────────────────────────────────────────────────────────────────
Backtests the TRENDLINE_SCANNER_BOT strategy on historical 5-min candle data
from Groww charting v4 API (returns full option chain history with date range).

API endpoint discovered:
  https://groww.in/v1/api/stocks_fo_data/v4/charting_service/chart/exchange/
  NSE/segment/FNO/{SYMBOL}?startTimeInMillis=...&endTimeInMillis=...&intervalInMinutes=5

AUTH: Update BEARER_TOKEN + COOKIES at the top of BT_CONFIG before running.
      Copy from Chrome DevTools → Network → any charting request → Copy as cURL.
      Token expires every ~24 h — refresh daily.

Usage:
    python3 TRENDLINE_BACKTEST.py
    python3 TRENDLINE_BACKTEST.py --days 7
    python3 TRENDLINE_BACKTEST.py --expiry 2026-06-23 --days 14 --premium_min 90 --premium_max 200

Signal simulation:
  BOUNCE — trendline support ascending → close within proximity_pts of support
            Entry at next-candle open + slippage | Target = swing_high − buffer
            Trailing SL activates after trail_activate pts profit

  BREAK  — close breaks break_pts below support → opposite instrument (CE↔PE)
            Entry at opposite instrument's next-candle open + slippage
            Trailing SL only (no hard target)
──────────────────────────────────────────────────────────────────────────────
"""

import sys, json, os, argparse, math
from datetime import datetime, date, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
import requests

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG  — update BEARER_TOKEN and COOKIES before running
# ═══════════════════════════════════════════════════════════════════════════
BT_CONFIG: dict = {
    # ── Instrument ──────────────────────────────────────────────────────────
    "index":        "NIFTY",
    "exchange":     "NSE",
    "expiry_date":  "2026-06-23",
    "strike_step":  50,
    "scan_range":   20,      # fetch ATM ± N strikes (wide net for data)
    "premium_min":  80.0,    # only trade options with premium >= this
    "premium_max":  200.0,   # only trade options with premium <= this

    # ── Data range ──────────────────────────────────────────────────────────
    "interval_min": 5,       # candle size in minutes
    "days_back":    31,      # how many calendar days of history to fetch

    # ── Trendline detection (must match live bot) ────────────────────────────
    "pivot_lookback":   3,
    "min_pivots":       2,
    "proximity_pts":    6.0,
    "break_pts":        3.0,

    # ── Entry ────────────────────────────────────────────────────────────────
    "slippage_pts":     0.5,  # added to entry price (conservative)

    # ── BOUNCE exit ──────────────────────────────────────────────────────────
    "target_buffer":    2.0,
    "trendline_sl_buf": 3.0,
    "bounce_trail_act": 5.0,
    "bounce_trail_by":  4.0,

    # ── BREAK exit ───────────────────────────────────────────────────────────
    "break_initial_sl": 5.0,
    "break_trail_act":  4.0,
    "break_trail_by":   3.0,

    # ── Trade sizing ─────────────────────────────────────────────────────────
    "lots":  1,

    # ── Auth — paste from Chrome DevTools / Copy as cURL ─────────────────────
    # Bearer token expires every ~24 h — refresh daily.
    "BEARER_TOKEN":  "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjE3ODE3NDI2MDAsImlhdCI6MTc4MTcyMTg5MiwibmJmIjoxNzgxNzIxODkyLCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCIwM2RhOGZkYi05NWQ5LTQ0ZGItYjk1NC00NzRlOWI3NTExN2FcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZXNzaW9uSWRcIjpcIjBmMWEyNjM0LTU1NmQtNGQyMy04YjlhLTllMDc4OTFlZjYzZlwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcIm9yZGVyLWJhc2ljLGxpdmVfZGF0YS1iYXNpYyxub25fdHJhZGluZy1iYXNpYyxvcmRlcl9yZWFkX29ubHktYmFzaWMsYmFja190ZXN0XCIsXCJzb3VyY2VJcEFkZHJlc3NcIjpudWxsLFwidHdvRmFFeHBpcnlUc1wiOjE3ODE3NDI2MDAwMDAsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.uu_0J84ykd8emcu97mWJ0M8xq1yF4092m60shRdNjNeUmpMtUaObSfxBmprJ_-tSAl70X18pXr4ZorMWkSXlKw",
    "COOKIES": "_gcl_au=1.1.863312266.1778482315; _ga=GA1.1.389629575.1778482316; we_luid=c3a360c8e013fb719f633243686d1c99c659a861; N_KEY_SEED=U2FsdGVkX1%2FNHnG%2BrEHtYepJ3EblcHADpZJzsMyigncOx%2BPibemropgN8xq%2BtO5G%2FCeuiFzLbyVqDoIkioFhwDXzZ1ycMg6r5MTDND6bbRT8OdFgWRPj2edtwCmfi1fY8spQGBrRRkvYOqJhHJOdDw%3D%3D; SOCKET_TOKEN=U2FsdGVkX18g8Yy0hnKnBtXFo6RkzPNPOA%2BHiGnKKu6HsoaCpFH0CoHqsvtlRB2yHQ9I1%2FxHMBrU6eyk3n1AuWjVqi%2Bx%2BPzl%2FUkrUNjC4F5tONlflNkgzTbDDt0VOKk15hqh%2Fr27S%2BNVXowU2hmF8LYQc5L%2FX5wNHhBHGTTwxQD379RrACcd97%2BpBb%2BcWLIiUIX8o0yNqxzCl3%2Fg3Eflh2N0RSD%2Bgs%2FDb7WPENl2oQncqbnpHLXC8FeMjs37k0yiSnFOzaxmgdSutua20urriZjpP306Q%2BFv1TJtZRFeMjlZwqNyq2NZR82XaOozstcD6GlMNeTeUVpJ7YENTctikid1JWq8WwWbrDhWBjry4u9DFFHf8gDD9663tHWwxdehZ%2FfG1iT51KIx%2F1Mh7MEsLg1Ff0FHoXxNkWFQZkkSp70IcxyXRrsOzKUizVFrYx2917QDrp2AegTDM5PWGbJ0%2Fjxf9yTLnITd%2BqA3NBAENiQLiodJk2E8rQ99m%2FKedumFKF3PwQoKBcktrNWd7AGI9%2FdigJtConBBOvaQD8ZA%2FoiaeMrizjy37wcavLWVjaY71JvcZiduqBl2yJf6GWbL4VCRs%2FrHwzTmAiJ3DZl5emdUebr2f%2BF%2BElZYj3vA%2FQcqxWfpE08lawi6OKcmjFLObRJvT%2FkIykd4VIEtjTdXSBwM6Eu%2BeLjnWXSc%2BjHrKFUgFMfx04BaU%2Bikwe7NQgsS2OOnVW1ADPJ7I7iRH%2B0AnmBw9Sm8EDYxxhhDbfjl8aww%2FhzkzWcBQqZG%2BF%2BYqqyX9obAfDve6nCsNkARgLP4yQHTwYWBGN9wJWaqLA3sbml30uiV8Jud%2B7z4GBuODjBa1Q%3D%3D; AUTH_SESSION_ID=U2FsdGVkX1%2Fm%2B7z%2FG12g6BvXLSCnQzZynC%2FGqjaCZD8dD9Wfuga8Z%2Ba9etM8FsuG78M9bPunW8xQVIg3A%2FPuDQ%3D%3D",
    "device_id": "8cea1d25-588a-5eff-9699-5e7fd20a6ca9",
}

LOT_SIZES    = {"NIFTY": 65, "BANKNIFTY": 15, "FINNIFTY": 40, "SENSEX": 20, "BANKEX": 15}
IST_OFFSET   = 19800   # UTC+5:30 in seconds
MARKET_OPEN_MIN  = 9 * 60 + 15
MARKET_CLOSE_MIN = 15 * 60 + 30

# ═══════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Pivot:
    idx:   int
    ts:    int
    price: float

@dataclass
class TrendlineState:
    valid:           bool  = False
    support:         float = 0.0
    slope:           float = 0.0
    pivots:          List[Pivot] = field(default_factory=list)
    last_swing_high: float = 0.0

@dataclass
class BTTrade:
    date:        str
    symbol:      str
    strike:      int
    opt_type:    str
    signal:      str      # "BOUNCE" | "BREAK"
    entry_idx:   int      # candle index of entry
    entry_time:  str
    entry_price: float
    qty:         int
    target:      Optional[float]
    sl:          float
    trail_activate: float
    trail_by:    float
    # filled on close
    exit_idx:    int   = 0
    exit_time:   str   = ""
    exit_price:  float = 0.0
    exit_reason: str   = ""
    pts:         float = 0.0   # pts per share
    pnl:         float = 0.0   # ₹ P&L
    peak:        float = 0.0
    trail_active: bool = False

# ═══════════════════════════════════════════════════════════════════════════
# AUTO TOKEN  — fetch fresh Bearer from TOTP (same as PROD10 bot)
# ═══════════════════════════════════════════════════════════════════════════
def _auto_token() -> str:
    """Fetch a fresh Groww access token using api_key + TOTP from ai_config.json."""
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_config.json")
        with open(cfg_path) as f:
            ai = json.load(f)
        api_key  = ai.get("groww_api_key", "")
        totp_sec = ai.get("groww_totp_secret", "")
        if not api_key or not totp_sec:
            return ""
        import pyotp
        from growwapi import GrowwAPI
        totp = pyotp.TOTP(totp_sec).now()
        token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
        print("✅  Fresh Bearer token fetched via TOTP")
        return token
    except Exception as e:
        print(f"⚠️   Auto-token fetch failed: {e}  (using hardcoded token)")
        return ""

# ═══════════════════════════════════════════════════════════════════════════
# HTTP SESSION
# ═══════════════════════════════════════════════════════════════════════════
def _build_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept":         "application/json, text/plain, */*",
        "authorization":  f"Bearer {cfg['BEARER_TOKEN']}",
        "x-app-id":       "growwWeb",
        "x-device-id":    cfg["device_id"],
        "x-device-id-v2": cfg["device_id"],
        "x-device-type":  "charts",
        "x-platform":     "web",
        "referer":        "https://groww.in/charts/options/nifty/",
        "user-agent":     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/142.0.0.0 Safari/537.36",
    })
    # parse cookie string
    for part in cfg["COOKIES"].split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            s.cookies.set(k.strip(), v.strip())
    return s

# ═══════════════════════════════════════════════════════════════════════════
# SYMBOL UTILITIES
# ═══════════════════════════════════════════════════════════════════════════
def make_symbol(index: str, expiry: date, strike: int, opt_type: str) -> str:
    yy = expiry.year % 100
    m  = expiry.month
    dd = f"{expiry.day:02d}"
    return f"{index}{yy}{m}{dd}{int(strike)}{opt_type}"

def parse_expiry(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def weekly_expiry_for_day(d: date) -> date:
    """Return the nearest upcoming Thursday (weekly/monthly expiry) for a trading day."""
    days_ahead = 3 - d.weekday()   # Thursday = weekday 3 (Mon=0)
    if days_ahead < 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)

def lot_size(index: str) -> int:
    return LOT_SIZES.get(index.upper(), 75)

# ═══════════════════════════════════════════════════════════════════════════
# V4 CANDLE API
# ═══════════════════════════════════════════════════════════════════════════
def fetch_candles_v4(sess: requests.Session, symbol: str, exchange: str,
                     start_ms: int, end_ms: int, interval: int = 5) -> list:
    """
    Fetch multi-day 5-min candles using v4 charting API.
    Returns list of {'ts', 'o', 'h', 'l', 'c', 'v'} dicts.
    """
    url = (f"https://groww.in/v1/api/stocks_fo_data/v4/charting_service/chart"
           f"/exchange/{exchange}/segment/FNO/{symbol}")
    params = {
        "startTimeInMillis": start_ms,
        "endTimeInMillis":   end_ms,
        "intervalInMinutes": interval,
    }
    try:
        r = sess.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        candles = data.get("candles", [])
        out = []
        for c in candles:
            out.append({
                "ts": int(c[0]),
                "o":  float(c[1]),
                "h":  float(c[2]),
                "l":  float(c[3]),
                "c":  float(c[4]),
                "v":  int(c[5]) if c[5] is not None else 0,
            })
        return out
    except Exception as ex:
        print(f"  [API] ✗ {symbol}: {ex}")
        return []

def fetch_spot(sess: requests.Session, index: str, exchange: str) -> float:
    url  = (f"https://groww.in/v1/api/stocks_data/v1/tr_live_indices"
            f"/exchange/{exchange}/segment/CASH/{index}/latest")
    try:
        # spot uses regular headers
        headers = {
            "x-app-id":    "growwWeb",
            "x-device-id": BT_CONFIG["device_id"],
            "x-platform":  "web",
            "user-agent":  "Mozilla/5.0",
        }
        r = requests.get(url, headers=headers, timeout=8)
        r.raise_for_status()
        return float(r.json().get("value", 0))
    except Exception:
        return 0.0

def fetch_candles_cash_v4(sess: requests.Session, index: str, exchange: str,
                           start_ms: int, end_ms: int, interval: int = 5) -> list:
    """Fetch NIFTY spot candles from CASH segment (for trendline + direction confirmation)."""
    url = (f"https://groww.in/v1/api/charting_service/v4/chart"
           f"/exchange/{exchange}/segment/CASH/{index}")
    params = {"startTimeInMillis": start_ms, "endTimeInMillis": end_ms,
              "intervalInMinutes": interval}
    try:
        r = sess.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        out = []
        for c in data.get("candles", []):
            out.append({"ts": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                        "l": float(c[3]), "c": float(c[4]),
                        "v": int(c[5]) if c[5] is not None else 0})
        return out
    except Exception as ex:
        print(f"  [API] ✗ SPOT {index}: {ex}")
        return []

# ═══════════════════════════════════════════════════════════════════════════
# TRENDLINE ENGINE  (identical to scanner bot)
# ═══════════════════════════════════════════════════════════════════════════
def _ist_dt(ts_utc: int) -> datetime:
    return datetime.utcfromtimestamp(ts_utc + IST_OFFSET)

def _ist_date(ts_utc: int) -> date:
    return _ist_dt(ts_utc).date()

def find_swing_lows(candles: list, lb: int) -> List[Pivot]:
    pivots, n = [], len(candles)
    for i in range(lb, n - lb):
        lo = candles[i]["l"]
        if (all(candles[i-j]["l"] > lo for j in range(1, lb+1)) and
                all(candles[i+j]["l"] > lo for j in range(1, lb+1))):
            pivots.append(Pivot(idx=i, ts=candles[i]["ts"], price=lo))
    return pivots

def find_swing_highs(candles: list, lb: int) -> List[Pivot]:
    pivots, n = [], len(candles)
    for i in range(lb, n - lb):
        hi = candles[i]["h"]
        if (all(candles[i-j]["h"] < hi for j in range(1, lb+1)) and
                all(candles[i+j]["h"] < hi for j in range(1, lb+1))):
            pivots.append(Pivot(idx=i, ts=candles[i]["ts"], price=hi))
    return pivots

def project_trendline(pivots: List[Pivot], cur_idx: int) -> Optional[float]:
    if len(pivots) < 2:
        return None
    p1, p2 = pivots[-2], pivots[-1]
    d_idx = p2.idx - p1.idx
    if d_idx == 0:
        return None
    slope = (p2.price - p1.price) / d_idx
    return p2.price + slope * (cur_idx - p2.idx)

def compute_trendline(candles: list, lb: int, min_p: int) -> TrendlineState:
    tl = TrendlineState()
    if len(candles) < lb * 2 + 2:
        return tl

    swing_lows  = find_swing_lows(candles,  lb)
    swing_highs = find_swing_highs(candles, lb)
    tl.last_swing_high = max((p.price for p in swing_highs), default=0.0)

    if len(swing_lows) < min_p:
        return tl

    last = swing_lows[-min_p:]
    ascending = all(last[i].price > last[i-1].price for i in range(1, len(last)))
    if not ascending:
        return tl

    cur_idx   = len(candles) - 1
    projected = project_trendline(swing_lows, cur_idx)
    if not projected or projected <= 0:
        return tl

    p1, p2 = swing_lows[-2], swing_lows[-1]
    slope  = (p2.price - p1.price) / max(p2.idx - p1.idx, 1)

    tl.valid           = True
    tl.pivots          = swing_lows
    tl.support         = round(projected, 2)
    tl.slope           = round(slope, 4)
    return tl


def compute_ascending_resistance(candles: list, lb: int, min_p: int) -> TrendlineState:
    """Ascending upper rail from rising swing HIGHS (channel top)."""
    tl = TrendlineState()
    if len(candles) < lb * 2 + 2:
        return tl
    swing_highs = find_swing_highs(candles, lb)
    if len(swing_highs) < min_p:
        return tl
    last = swing_highs[-min_p:]
    if not all(last[i].price > last[i-1].price for i in range(1, len(last))):
        return tl
    cur_idx = len(candles) - 1
    projected = project_trendline(swing_highs, cur_idx)
    if not projected or projected <= 0:
        return tl
    p1, p2 = swing_highs[-2], swing_highs[-1]
    tl.valid = True
    tl.pivots = swing_highs
    tl.support = round(projected, 2)
    tl.slope = round((p2.price - p1.price) / max(p2.idx - p1.idx, 1), 4)
    return tl


def compute_descending_trendline(candles: list, lb: int, min_p: int) -> TrendlineState:
    """Descending upper rail from falling swing HIGHS (bearish channel top)."""
    tl = TrendlineState()
    if len(candles) < lb * 2 + 2:
        return tl
    swing_highs = find_swing_highs(candles, lb)
    if len(swing_highs) < min_p:
        return tl
    last = swing_highs[-min_p:]
    if not all(last[i].price < last[i-1].price for i in range(1, len(last))):
        return tl
    cur_idx = len(candles) - 1
    projected = project_trendline(swing_highs, cur_idx)
    if not projected or projected <= 0:
        return tl
    p1, p2 = swing_highs[-2], swing_highs[-1]
    tl.valid = True
    tl.pivots = swing_highs
    tl.support = round(projected, 2)
    tl.slope = round((p2.price - p1.price) / max(p2.idx - p1.idx, 1), 4)
    return tl


def compute_descending_support(candles: list, lb: int, min_p: int) -> TrendlineState:
    """Descending lower rail from falling swing LOWS (bearish channel bottom / SL ref)."""
    tl = TrendlineState()
    if len(candles) < lb * 2 + 2:
        return tl
    swing_lows = find_swing_lows(candles, lb)
    if len(swing_lows) < min_p:
        return tl
    last = swing_lows[-min_p:]
    if not all(last[i].price < last[i-1].price for i in range(1, len(last))):
        return tl
    cur_idx = len(candles) - 1
    projected = project_trendline(swing_lows, cur_idx)
    if not projected or projected <= 0:
        return tl
    p1, p2 = swing_lows[-2], swing_lows[-1]
    tl.valid = True
    tl.pivots = swing_lows
    tl.support = round(projected, 2)
    tl.slope = round((p2.price - p1.price) / max(p2.idx - p1.idx, 1), 4)
    return tl


def detect_horizontal_zone(candles: list, lb: int, tol_pct: float = 0.0015) -> Optional[float]:
    """Flat zone: both swing highs AND lows within tol_pct of each other."""
    if len(candles) < lb * 2 + 2:
        return None
    swing_highs = find_swing_highs(candles, lb)
    swing_lows  = find_swing_lows(candles,  lb)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None
    lh = swing_highs[-2:]
    ll = swing_lows[-2:]
    hi_mid = (lh[-1].price + lh[-2].price) / 2
    lo_mid = (ll[-1].price + ll[-2].price) / 2
    if hi_mid <= 0 or lo_mid <= 0:
        return None
    if abs(lh[-1].price - lh[-2].price) / hi_mid > tol_pct:
        return None
    if abs(ll[-1].price - ll[-2].price) / lo_mid > tol_pct:
        return None
    return round((hi_mid + lo_mid) / 2, 2)

# ═══════════════════════════════════════════════════════════════════════════
# TRADE SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════
def _simulate_trade_forward(trade: BTTrade, candles: list) -> BTTrade:
    """
    Walk candles from entry_idx+1 to end of day, applying SL/target/trailing.
    Modifies trade in place, returns it.
    """
    cfg = BT_CONFIG
    for i in range(trade.entry_idx + 1, len(candles)):
        c    = candles[i]
        h, l = c["h"], c["l"]

        # Update peak using this candle's high
        if h > trade.peak:
            trade.peak = h

        # Activate trailing
        profit = h - trade.entry_price
        if not trade.trail_active and profit >= trade.trail_activate:
            trade.trail_active = True
            new_sl = round(h - trade.trail_by, 2)
            if new_sl > trade.sl:
                trade.sl = new_sl

        # Move trailing SL
        if trade.trail_active:
            new_sl = round(trade.peak - trade.trail_by, 2)
            if new_sl > trade.sl:
                trade.sl = new_sl

        # Check target hit (candle high reaches target)
        if trade.target and h >= trade.target:
            trade.exit_idx    = i
            trade.exit_time   = _ist_dt(c["ts"]).strftime("%H:%M")
            trade.exit_price  = trade.target
            trade.exit_reason = "TARGET"
            trade.pts         = round(trade.exit_price - trade.entry_price, 2)
            trade.pnl         = round(trade.pts * trade.qty, 2)
            return trade

        # Check SL hit (candle low hits SL)
        if l <= trade.sl:
            trade.exit_idx    = i
            trade.exit_time   = _ist_dt(c["ts"]).strftime("%H:%M")
            trade.exit_price  = trade.sl
            trade.exit_reason = "TRAIL_SL" if trade.trail_active else "SL"
            trade.pts         = round(trade.exit_price - trade.entry_price, 2)
            trade.pnl         = round(trade.pts * trade.qty, 2)
            return trade

    # End-of-day close
    last = candles[-1]
    trade.exit_idx    = len(candles) - 1
    trade.exit_time   = _ist_dt(last["ts"]).strftime("%H:%M")
    trade.exit_price  = round(last["c"], 2)
    trade.exit_reason = "EOD"
    trade.pts         = round(trade.exit_price - trade.entry_price, 2)
    trade.pnl         = round(trade.pts * trade.qty, 2)
    return trade


def _try_open_bounce(candles: list, idx: int, symbol: str, strike: int,
                     opt_type: str, day_str: str, tl: TrendlineState,
                     qty: int, cfg: dict) -> Optional[BTTrade]:
    """
    BOUNCE signal at candle idx.
    Confirmation: next candle (idx+1) must close >= current close + confirm_pts.
    Entry: next candle's open + slippage.
    """
    if idx + 1 >= len(candles):
        return None

    current_close = candles[idx]["c"]
    next_c        = candles[idx + 1]

    # Simulate confirmation window: next candle's high must reach baseline + 2 pts
    baseline = current_close
    if next_c["h"] < baseline + 2.0:    # proxy for bounce_confirm_pts
        return None

    entry = round(next_c["o"] + cfg["slippage_pts"], 2)

    target = (round(tl.last_swing_high - cfg["target_buffer"], 2)
              if tl.last_swing_high > entry else None)
    init_sl = round(tl.support - cfg["trendline_sl_buf"], 2)

    if entry <= init_sl:
        return None
    if target and target <= entry:
        target = None
    # Experiment B: reject if risk (entry - SL) exceeds cap
    max_risk = cfg.get("max_entry_risk", 0.0)
    if max_risk > 0 and (entry - init_sl) > max_risk:
        return None

    t = BTTrade(
        date=day_str, symbol=symbol, strike=strike, opt_type=opt_type,
        signal="BOUNCE", entry_idx=idx + 1,
        entry_time=_ist_dt(next_c["ts"]).strftime("%H:%M"),
        entry_price=entry, qty=qty,
        target=target, sl=init_sl,
        trail_activate=cfg["bounce_trail_act"],
        trail_by=cfg["bounce_trail_by"],
        peak=entry,
    )
    return t


def _try_open_break(candles_opp: list, idx: int, symbol_opp: str,
                    strike: int, opp_type: str, day_str: str,
                    qty: int, cfg: dict) -> Optional[BTTrade]:
    """
    BREAK → buy opposite.
    Entry: opposite instrument's next candle open + slippage.
    Confirmation: opposite's next candle high >= open + break_confirm_pts (1.5).
    """
    if idx + 1 >= len(candles_opp):
        return None

    next_c = candles_opp[idx + 1]

    # Check that opposite is actually moving up
    if next_c["h"] < next_c["o"] + 1.5:   # proxy for break_confirm_pts
        return None

    entry   = round(next_c["o"] + cfg["slippage_pts"], 2)
    init_sl = round(entry - cfg["break_initial_sl"], 2)

    t = BTTrade(
        date=day_str, symbol=symbol_opp, strike=strike, opt_type=opp_type,
        signal="BREAK", entry_idx=idx + 1,
        entry_time=_ist_dt(next_c["ts"]).strftime("%H:%M"),
        entry_price=entry, qty=qty,
        target=None, sl=init_sl,
        trail_activate=cfg["break_trail_act"],
        trail_by=cfg["break_trail_by"],
        peak=entry,
    )
    return t


def _try_open_breakout(candles: list, idx: int, symbol: str, strike: int,
                        opt_type: str, day_str: str, tl_resist: TrendlineState,
                        tl_desc_low: TrendlineState, qty: int, cfg: dict) -> Optional[BTTrade]:
    """BREAKOUT: option price crossing above descending resistance level."""
    if idx + 1 >= len(candles):
        return None
    current_close = candles[idx]["c"]
    next_c = candles[idx + 1]
    if next_c["h"] < current_close + 2.0:
        return None
    entry = round(next_c["o"] + cfg["slippage_pts"], 2)
    if tl_desc_low.valid and 0 < tl_desc_low.support < entry:
        init_sl = round(tl_desc_low.support - cfg["trendline_sl_buf"], 2)
    else:
        init_sl = round(tl_resist.support - cfg["trendline_sl_buf"], 2)
    if entry <= init_sl:
        return None
    max_risk = cfg.get("max_entry_risk", 0.0)
    if max_risk > 0 and (entry - init_sl) > max_risk:
        return None
    return BTTrade(
        date=day_str, symbol=symbol, strike=strike, opt_type=opt_type,
        signal="BREAKOUT", entry_idx=idx + 1,
        entry_time=_ist_dt(next_c["ts"]).strftime("%H:%M"),
        entry_price=entry, qty=qty, target=None, sl=init_sl,
        trail_activate=cfg["break_trail_act"], trail_by=cfg["break_trail_by"],
        peak=entry,
    )


def _try_open_horiz_bounce(candles: list, idx: int, symbol: str, strike: int,
                            opt_type: str, day_str: str, zone_mid: float,
                            qty: int, cfg: dict) -> Optional[BTTrade]:
    """HORIZ_BOUNCE: option price bouncing off flat horizontal zone."""
    if idx + 1 >= len(candles):
        return None
    current_close = candles[idx]["c"]
    next_c = candles[idx + 1]
    if next_c["h"] < current_close + 2.0:
        return None
    entry = round(next_c["o"] + cfg["slippage_pts"], 2)
    init_sl = round(zone_mid - cfg["trendline_sl_buf"], 2)
    if entry <= init_sl:
        return None
    max_risk = cfg.get("max_entry_risk", 0.0)
    if max_risk > 0 and (entry - init_sl) > max_risk:
        return None
    return BTTrade(
        date=day_str, symbol=symbol, strike=strike, opt_type=opt_type,
        signal="HORIZ_BOUNCE", entry_idx=idx + 1,
        entry_time=_ist_dt(next_c["ts"]).strftime("%H:%M"),
        entry_price=entry, qty=qty, target=None, sl=init_sl,
        trail_activate=cfg["bounce_trail_act"], trail_by=cfg["bounce_trail_by"],
        peak=entry,
    )


def _spot_dir_ok(opt_type: str, spot_slice: list, cfg: dict) -> bool:
    """True if NIFTY spot direction matches the option type being traded.
    CE → spot should be rising (ascending trendline valid or recent close > 3-bar-ago close).
    PE → spot should be falling (descending trendline valid or recent close < 3-bar-ago close).
    """
    if not spot_slice or len(spot_slice) < 4:
        return True  # no data → don't filter
    lb    = cfg["pivot_lookback"]
    min_p = cfg["min_pivots"]
    if opt_type == "CE":
        asc = compute_trendline(spot_slice, lb, min_p)
        if asc.valid:
            return spot_slice[-1]["c"] >= asc.support * 0.998
        return spot_slice[-1]["c"] > spot_slice[-4]["c"]
    else:  # PE
        desc = compute_descending_trendline(spot_slice, lb, min_p)
        if desc.valid:
            return spot_slice[-1]["c"] <= desc.support * 1.002
        return spot_slice[-1]["c"] < spot_slice[-4]["c"]


def simulate_day(
    day_str: str,
    strikes: List[int],
    expiry: date,
    index: str,
    exchange: str,
    candles_by_sym: Dict[str, list],
    cfg: dict,
    spot_day_candles: list = None,
) -> List[BTTrade]:
    """
    Simulate one trading day — SEQUENTIAL mode: one trade at a time.
    Scans all instruments every candle but enters only if no trade is open.
    First valid signal (in strike order) wins the candle.
    """
    lb    = cfg["pivot_lookback"]
    min_p = cfg["min_pivots"]
    prox  = cfg["proximity_pts"]
    brk   = cfg["break_pts"]
    qty   = lot_size(index) * cfg["lots"]
    p_min = cfg.get("premium_min", 0.0)
    p_max = cfg.get("premium_max", 999999.0)

    # Build ordered list of valid symbols
    sym_list  = []
    valid_syms: set = set()
    for s in strikes:
        for ot in ["CE", "PE"]:
            sym = make_symbol(index, expiry, s, ot)
            if sym in candles_by_sym and len(candles_by_sym[sym]) > 0:
                sym_list.append((sym, s, ot))
                valid_syms.add(sym)

    if not sym_list:
        return []

    n_candles = max(len(candles_by_sym[sym]) for sym, _, _ in sym_list)

    trades:         List[BTTrade]        = []
    pending_breaks: Dict[str, int]       = {}
    # Sequential state: at most one trade open at any time
    active_trade: Optional[BTTrade]  = None
    active_sym:   Optional[str]      = None
    # Experiment flags
    no_break      = cfg.get("no_break", False)
    max_daily_sl  = cfg.get("max_daily_sl", 0)
    daily_sl_hits = 0
    desc_enabled  = cfg.get("desc_enabled",  True)
    horiz_enabled = cfg.get("horiz_enabled", True)
    spot_confirm  = cfg.get("spot_confirm",  False)
    spot_day      = spot_day_candles or []
    # Build ts→idx map for spot candles to allow per-candle lookup
    spot_ts_to_idx = {c["ts"]: i for i, c in enumerate(spot_day)}

    for idx in range(n_candles):
        # ── Manage the single active trade ────────────────────────────────
        if active_trade is not None and idx > active_trade.entry_idx:
            candles = candles_by_sym[active_sym]

            if idx >= len(candles):
                last = candles[-1]
                active_trade.exit_idx    = len(candles) - 1
                active_trade.exit_time   = _ist_dt(last["ts"]).strftime("%H:%M")
                active_trade.exit_price  = round(last["c"], 2)
                active_trade.exit_reason = "EOD"
                active_trade.pts = round(active_trade.exit_price - active_trade.entry_price, 2)
                active_trade.pnl = round(active_trade.pts * active_trade.qty, 2)
                trades.append(active_trade)
                active_trade = None
                active_sym   = None
            else:
                c    = candles[idx]
                h, l = c["h"], c["l"]

                if h > active_trade.peak:
                    active_trade.peak = h

                profit = h - active_trade.entry_price
                if not active_trade.trail_active and profit >= active_trade.trail_activate:
                    active_trade.trail_active = True
                    new_sl = round(h - active_trade.trail_by, 2)
                    if new_sl > active_trade.sl:
                        active_trade.sl = new_sl

                if active_trade.trail_active:
                    new_sl = round(active_trade.peak - active_trade.trail_by, 2)
                    if new_sl > active_trade.sl:
                        active_trade.sl = new_sl

                closed = False
                if active_trade.target and h >= active_trade.target:
                    active_trade.exit_idx    = idx
                    active_trade.exit_time   = _ist_dt(c["ts"]).strftime("%H:%M")
                    active_trade.exit_price  = active_trade.target
                    active_trade.exit_reason = "TARGET"
                    active_trade.pts = round(active_trade.exit_price - active_trade.entry_price, 2)
                    active_trade.pnl = round(active_trade.pts * active_trade.qty, 2)
                    trades.append(active_trade)
                    active_trade = None
                    active_sym   = None
                    closed = True

                if not closed and l <= active_trade.sl:
                    active_trade.exit_idx    = idx
                    active_trade.exit_time   = _ist_dt(c["ts"]).strftime("%H:%M")
                    active_trade.exit_price  = active_trade.sl
                    active_trade.exit_reason = "TRAIL_SL" if active_trade.trail_active else "SL"
                    active_trade.pts = round(active_trade.exit_price - active_trade.entry_price, 2)
                    active_trade.pnl = round(active_trade.pts * active_trade.qty, 2)
                    trades.append(active_trade)
                    # Experiment C: count hard SL hits (not trail SL)
                    if not active_trade.trail_active:
                        daily_sl_hits += 1
                    active_trade = None
                    active_sym   = None

        # ── Skip signal scan while a trade is active ──────────────────────
        if active_trade is not None:
            continue

        # ── Experiment C: daily circuit breaker ───────────────────────────
        if max_daily_sl > 0 and daily_sl_hits >= max_daily_sl:
            continue   # no more new trades today

        # ── Scan for signals — take the FIRST valid one ───────────────────
        for sym, strike, opt_type in sym_list:
            candles = candles_by_sym[sym]
            if idx + 1 >= len(candles):
                continue

            close = candles[idx]["c"]
            if not (p_min <= close <= p_max):
                continue

            tl = compute_trendline(candles[:idx + 1], lb, min_p)

            # ── NIFTY spot slice at this candle ──────────────────────────────
            spot_slice = []
            if spot_confirm and spot_day:
                cur_ts = candles[idx]["ts"]
                s_idx  = spot_ts_to_idx.get(cur_ts)
                if s_idx is None:
                    # find nearest earlier
                    for si in range(len(spot_day) - 1, -1, -1):
                        if spot_day[si]["ts"] <= cur_ts:
                            s_idx = si; break
                if s_idx is not None:
                    spot_slice = spot_day[:s_idx + 1]

            # ── BOUNCE (ascending support) ───────────────────────────────────
            if tl.valid:
                dist = close - tl.support
                if 0.0 <= dist <= prox:
                    if not spot_confirm or _spot_dir_ok(opt_type, spot_slice, cfg):
                        tl_top = compute_ascending_resistance(candles[:idx + 1], lb, min_p)
                        target = None
                        if tl_top.valid and tl_top.support > close:
                            target = round(tl_top.support - cfg["target_buffer"], 2)
                        elif tl.last_swing_high > close:
                            target = round(tl.last_swing_high - cfg["target_buffer"], 2)
                        tl_for_bounce = TrendlineState(
                            valid=tl.valid, support=tl.support, slope=tl.slope,
                            pivots=tl.pivots, last_swing_high=tl.last_swing_high
                        )
                        t = _try_open_bounce(candles, idx, sym, strike, opt_type,
                                             day_str, tl_for_bounce, qty, cfg)
                        if t:
                            if target:
                                t.target = target
                            active_trade = t
                            active_sym   = sym
                            break

                # ── BREAK (ascending support broken) ─────────────────────────
                elif not no_break and dist < -brk:
                    opp_type = "CE" if opt_type == "PE" else "PE"
                    opp_sym  = make_symbol(index, expiry, strike, opp_type)
                    if opp_sym not in valid_syms:
                        continue
                    candles_opp = candles_by_sym.get(opp_sym, [])
                    if not candles_opp or idx >= len(candles_opp):
                        continue
                    opp_close = candles_opp[idx]["c"]
                    if not (p_min <= opp_close <= p_max):
                        continue
                    if not spot_confirm or _spot_dir_ok(opp_type, spot_slice, cfg):
                        if pending_breaks.get(opp_sym, -99) != idx:
                            pending_breaks[opp_sym] = idx
                            t = _try_open_break(candles_opp, idx, opp_sym,
                                                strike, opp_type, day_str, qty, cfg)
                            if t:
                                active_trade = t
                                active_sym   = opp_sym
                                break

            # ── BREAKOUT (descending resistance crossed) ─────────────────────
            if desc_enabled and active_trade is None:
                tl_resist = compute_descending_trendline(candles[:idx + 1], lb, min_p)
                if tl_resist.valid:
                    dist_r = close - tl_resist.support
                    if -prox <= dist_r <= prox:
                        if not spot_confirm or _spot_dir_ok(opt_type, spot_slice, cfg):
                            tl_desc_low = compute_descending_support(candles[:idx + 1], lb, min_p)
                            t = _try_open_breakout(candles, idx, sym, strike, opt_type,
                                                   day_str, tl_resist, tl_desc_low, qty, cfg)
                            if t:
                                active_trade = t
                                active_sym   = sym
                                break

            # ── HORIZ_BOUNCE (horizontal zone) ──────────────────────────────
            if horiz_enabled and active_trade is None:
                zone_mid = detect_horizontal_zone(candles[:idx + 1], lb)
                if zone_mid is not None:
                    dist_h = close - zone_mid
                    if 0.0 <= dist_h <= prox:
                        if not spot_confirm or _spot_dir_ok(opt_type, spot_slice, cfg):
                            t = _try_open_horiz_bounce(candles, idx, sym, strike, opt_type,
                                                       day_str, zone_mid, qty, cfg)
                            if t:
                                active_trade = t
                                active_sym   = sym
                                break

    # ── Force-close remaining trade at EOD ────────────────────────────────
    if active_trade is not None:
        candles = candles_by_sym[active_sym]
        if candles:
            last = candles[-1]
            active_trade.exit_idx    = len(candles) - 1
            active_trade.exit_time   = _ist_dt(last["ts"]).strftime("%H:%M")
            active_trade.exit_price  = round(last["c"], 2)
            active_trade.exit_reason = "EOD"
            active_trade.pts = round(active_trade.exit_price - active_trade.entry_price, 2)
            active_trade.pnl = round(active_trade.pts * active_trade.qty, 2)
            trades.append(active_trade)

    return trades

# ═══════════════════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════════════════
def print_report(all_trades: List[BTTrade], cfg: dict):
    if not all_trades:
        print("\n  No trades generated.")
        return

    total   = len(all_trades)
    wins    = [t for t in all_trades if t.pnl > 0]
    losses  = [t for t in all_trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in all_trades)
    win_rate  = len(wins) / total * 100 if total else 0

    by_signal = defaultdict(list)
    for t in all_trades:
        by_signal[t.signal].append(t)

    by_day: Dict[str, list] = defaultdict(list)
    for t in all_trades:
        by_day[t.date].append(t)

    # by entry hour
    by_hour: Dict[int, list] = defaultdict(list)
    for t in all_trades:
        hr = int(t.entry_time.split(":")[0])
        by_hour[hr].append(t)

    W = 80
    print("\n" + "═" * W)
    print("  TRENDLINE STRATEGY BACKTEST RESULTS")
    print(f"  Index  : {cfg['index']}  |  Expiry: {cfg['expiry_date']}")
    print(f"  Period : {min(t.date for t in all_trades)}  →  {max(t.date for t in all_trades)}")
    print(f"  Filter  : premium ₹{cfg['premium_min']}–₹{cfg['premium_max']}  |  scan ±{cfg['scan_range']} strikes")
    print(f"  Mode    : SEQUENTIAL (1 trade at a time — realistic live execution)")
    print("═" * W)

    # ── Per-trade table ────────────────────────────────────────────────────
    print(f"\n{'DATE':10s}  {'SYMBOL':24s}  {'SIG':6s}  "
          f"{'ENTRY':7s}  {'EXIT':7s}  {'PTS':7s}  "
          f"{'P&L':10s}  {'REASON':12s}")
    print("─" * W)

    running_pnl = 0.0
    for t in sorted(all_trades, key=lambda x: (x.date, x.entry_time)):
        running_pnl += t.pnl
        sign = "✅" if t.pnl > 0 else "❌"
        print(f"{t.date}  {t.symbol:24s}  {t.signal:6s}  "
              f"₹{t.entry_price:6.1f}  ₹{t.exit_price:6.1f}  "
              f"{t.pts:+6.1f}  "
              f"₹{t.pnl:+8,.0f}  {t.exit_reason:12s}  "
              f"{sign}")

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "─" * W)
    print(f"  TOTAL TRADES : {total:3d}  |  WINS: {len(wins):3d}  |  LOSSES: {len(losses):3d}")
    print(f"  WIN RATE     : {win_rate:.1f}%")
    print(f"  TOTAL P&L    : ₹{total_pnl:+,.2f}")
    avg_win  = sum(t.pnl for t in wins)  / len(wins)  if wins  else 0
    avg_loss = sum(t.pnl for t in losses)/ len(losses) if losses else 0
    print(f"  AVG WIN      : ₹{avg_win:+,.2f}  |  AVG LOSS: ₹{avg_loss:+,.2f}")
    if losses:
        print(f"  RISK/REWARD  : {abs(avg_win/avg_loss):.2f}:1" if avg_loss != 0 else "  RISK/REWARD  : ∞")

    # ── By signal type ───────────────────────────────────────────────────────
    print(f"\n{'─':─<{W}}")
    print("  BY SIGNAL TYPE:")
    for sig in ["BOUNCE", "BREAK", "BREAKOUT", "HORIZ_BOUNCE"]:
        ts = by_signal.get(sig, [])
        if not ts:
            continue
        sp = sum(t.pnl for t in ts)
        sw = sum(1 for t in ts if t.pnl > 0)
        print(f"    {sig:6s}  trades={len(ts):3d}  wins={sw:3d}  "
              f"win%={sw/len(ts)*100:.0f}%  total=₹{sp:+,.0f}")

    # ── By exit reason ───────────────────────────────────────────────────────
    print(f"\n{'─':─<{W}}")
    print("  BY EXIT REASON:")
    by_reason: Dict[str, list] = defaultdict(list)
    for t in all_trades:
        by_reason[t.exit_reason].append(t)
    for reason, ts in sorted(by_reason.items()):
        sp = sum(t.pnl for t in ts)
        print(f"    {reason:12s}  count={len(ts):3d}  total=₹{sp:+,.0f}")

    # ── By trading day ───────────────────────────────────────────────────────
    print(f"\n{'─':─<{W}}")
    print("  DAILY P&L:")
    for day in sorted(by_day.keys()):
        ts  = by_day[day]
        dp  = sum(t.pnl for t in ts)
        dw  = sum(1 for t in ts if t.pnl > 0)
        sign = "✅" if dp > 0 else "❌"
        print(f"    {day}  trades={len(ts):2d}  wins={dw:2d}  "
              f"day_pnl=₹{dp:+8,.0f}  {sign}")

    # ── By hour ─────────────────────────────────────────────────────────────
    print(f"\n{'─':─<{W}}")
    print("  BY ENTRY HOUR (IST):")
    for hr in sorted(by_hour.keys()):
        ts = by_hour[hr]
        hp = sum(t.pnl for t in ts)
        hw = sum(1 for t in ts if t.pnl > 0)
        bar = "█" * max(1, int(abs(hp) / 1000))
        sign = "+" if hp >= 0 else "-"
        print(f"    {hr:02d}:00  trades={len(ts):2d}  wins={hw:2d}  "
              f"pnl=₹{hp:+8,.0f}  {sign}{bar}")

    # ── Parameter suggestions ────────────────────────────────────────────────
    print(f"\n{'─':─<{W}}")
    print("  PARAMETER SUGGESTIONS:")
    eod_loss = [t for t in all_trades if t.exit_reason == "EOD" and t.pnl < 0]
    sl_loss  = [t for t in all_trades if t.exit_reason == "SL" and t.pnl < 0]
    trl_win  = [t for t in all_trades if t.exit_reason == "TRAIL_SL" and t.pnl > 0]
    tgt_win  = [t for t in all_trades if t.exit_reason == "TARGET"]

    if eod_loss:
        print(f"    ⚠️  {len(eod_loss)} trades closed at EOD at a loss "
              f"(₹{sum(t.pnl for t in eod_loss):+,.0f}) — consider tighter intraday SL")
    if sl_loss:
        avg_sl_pts = abs(sum(t.pts for t in sl_loss) / len(sl_loss))
        print(f"    ⚠️  {len(sl_loss)} hard SL hits, avg loss {avg_sl_pts:.1f} pts — "
              f"current SL buf = {cfg['trendline_sl_buf']} pts")
    if trl_win:
        print(f"    ✅  {len(trl_win)} profitable exits via trailing SL — trailing logic working")
    if tgt_win:
        print(f"    ✅  {len(tgt_win)} target hits — target_buffer={cfg['target_buffer']} pts ok")
    if win_rate < 40:
        print(f"    💡 Win rate {win_rate:.0f}% is low — "
              f"try tightening proximity_pts (currently {cfg['proximity_pts']} pts) or "
              f"adding a time filter (avoid 9:15–9:45)")
    if win_rate >= 55:
        print(f"    ✅  Win rate {win_rate:.0f}% is solid")

    print("═" * W + "\n")

# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="TRENDLINE_BACKTEST.py")
    ap.add_argument("--expiry",      default=BT_CONFIG["expiry_date"])
    ap.add_argument("--days",        type=int,   default=BT_CONFIG["days_back"])
    ap.add_argument("--scan_range",  type=int,   default=BT_CONFIG["scan_range"])
    ap.add_argument("--premium_min", type=float, default=BT_CONFIG["premium_min"])
    ap.add_argument("--premium_max", type=float, default=BT_CONFIG["premium_max"])
    ap.add_argument("--index",       default=BT_CONFIG["index"])
    ap.add_argument("--exchange",    default=BT_CONFIG["exchange"])
    ap.add_argument("--lots",        type=int,   default=BT_CONFIG["lots"])
    ap.add_argument("--out",         default="", help="JSON output file (optional)")
    # ── Experiment flags ─────────────────────────────────────────────────────
    ap.add_argument("--no_break",       action="store_true",
                    help="Disable BREAK signals (only trade BOUNCE)")
    ap.add_argument("--max_entry_risk", type=float, default=0.0,
                    help="Skip BOUNCE if (entry - SL) > N pts. 0 = no filter.")
    ap.add_argument("--max_daily_sl",   type=int,   default=0,
                    help="Stop trading for the day after N SL hits. 0 = no limit.")
    ap.add_argument("--desc_enabled",  action="store_true", default=True,
                    help="Enable DESCENDING channel BREAKOUT signals (default on)")
    ap.add_argument("--horiz_enabled", action="store_true", default=True,
                    help="Enable HORIZONTAL zone HORIZ_BOUNCE signals (default on)")
    ap.add_argument("--spot_confirm",  action="store_true",
                    help="Require NIFTY spot direction to confirm each signal")
    ap.add_argument("--no_desc",  action="store_true", help="Disable descending signals")
    ap.add_argument("--no_horiz", action="store_true", help="Disable horizontal signals")
    args = ap.parse_args()

    # Auto-refresh Bearer token (expires every ~24h)
    fresh_token = _auto_token()
    if fresh_token:
        BT_CONFIG["BEARER_TOKEN"] = fresh_token

    cfg = {**BT_CONFIG}
    cfg["expiry_date"] = args.expiry
    cfg["days_back"]   = args.days
    cfg["scan_range"]  = args.scan_range
    cfg["premium_min"] = args.premium_min
    cfg["premium_max"] = args.premium_max
    cfg["index"]       = args.index
    cfg["exchange"]    = args.exchange
    cfg["lots"]        = args.lots
    cfg["no_break"]       = args.no_break
    cfg["max_entry_risk"] = args.max_entry_risk
    cfg["max_daily_sl"]   = args.max_daily_sl
    cfg["desc_enabled"]  = not args.no_desc
    cfg["horiz_enabled"] = not args.no_horiz
    cfg["spot_confirm"]  = args.spot_confirm

    index    = cfg["index"]
    exchange = cfg["exchange"]

    print("═" * 80)
    print("  TRENDLINE STRATEGY BACKTEST  —  ROLLING WEEKLY EXPIRY")
    print(f"  Index  : {index}  ({exchange})")
    print(f"  Lots   : {cfg['lots']}  (qty per trade = {lot_size(index) * cfg['lots']})")
    print(f"  Days   : {cfg['days_back']} calendar days back from today")
    print("═" * 80)

    # ── Build HTTP session with auth ────────────────────────────────────────
    sess = _build_session(cfg)

    # ── Fetch current spot to determine ATM ─────────────────────────────────
    print("\n  Fetching spot...")
    spot = fetch_spot(sess, index, exchange)
    if spot <= 0:
        # Try a fallback: use sensible default
        print("  ⚠️  Could not fetch live spot — using mid-range default 24000")
        spot = 24000.0
    atm  = int(round(spot / cfg["strike_step"]) * cfg["strike_step"])
    r    = cfg["scan_range"]
    strikes = [atm + i * cfg["strike_step"] for i in range(-r, r + 1)]
    print(f"  Spot    : ₹{spot:,.2f}  →  ATM: {atm}")
    print(f"  Scanning: {strikes[0]}…{strikes[-1]}  ({len(strikes)} strikes × 2 = {len(strikes)*2} instruments fetched)")
    print(f"  Filter  : premium ₹{cfg['premium_min']}–₹{cfg['premium_max']}  (signals only within this range)")
    sigs = ["BOUNCE/BREAK"]
    if cfg.get("desc_enabled"): sigs.append("BREAKOUT")
    if cfg.get("horiz_enabled"): sigs.append("HORIZ_BOUNCE")
    print(f"  Signals : {' + '.join(sigs)}")
    if cfg.get("spot_confirm"): print("  SpotConf: ENABLED — NIFTY direction must match")

    # ── Date range & expiry grouping ──────────────────────────────────────
    today_ist  = (datetime.utcnow() + timedelta(seconds=IST_OFFSET)).date()
    start_date = today_ist - timedelta(days=cfg["days_back"])
    now_utc_ms = int(datetime.utcnow().timestamp() * 1000)
    start_ms   = int((datetime.utcnow() - timedelta(days=cfg["days_back"] + 3)).timestamp() * 1000)

    # ── Fetch NIFTY spot candles (for trend confirmation) ─────────────────────
    spot_all_candles: list = []
    if cfg.get("spot_confirm"):
        print("  Fetching NIFTY spot candles for confirmation...")
        spot_all_candles = fetch_candles_cash_v4(sess, index, exchange, start_ms, now_utc_ms, cfg["interval_min"])
        print(f"  Spot candles: {len(spot_all_candles)}")

    # If a specific expiry was given, pin ALL days to that expiry.
    # Otherwise map each Mon-Fri to its nearest upcoming Thursday (rolling).
    forced_expiry = parse_expiry(cfg["expiry_date"]) if cfg.get("expiry_date") else None
    expiry_to_days: Dict[date, List[date]] = defaultdict(list)
    cur = start_date
    while cur <= today_ist:
        if cur.weekday() < 5:
            exp = forced_expiry if forced_expiry else weekly_expiry_for_day(cur)
            expiry_to_days[exp].append(cur)
        cur += timedelta(days=1)

    print(f"\n  Period  : {start_date}  →  {today_ist}  ({cfg['days_back']} days)")
    print(f"  Expiries: {len(expiry_to_days)}  ({', '.join(str(e) for e in sorted(expiry_to_days))})")
    print(f"  Lots    : {cfg['lots']}  (qty per trade = {lot_size(index) * cfg['lots']})")

    all_trades: List[BTTrade] = []
    seen_days:  set           = set()

    for expiry in sorted(expiry_to_days.keys()):
        exp_day_set = set(expiry_to_days[expiry])
        all_symbols = [make_symbol(index, expiry, s, ot)
                       for s in strikes for ot in ["CE", "PE"]]

        print(f"\n  ── Expiry {expiry}  ({len(all_symbols)} symbols) ──")
        print(f"  Fetching data ...", end="", flush=True)

        expiry_candles: Dict[str, list] = {}
        found = 0
        for sym in all_symbols:
            c = fetch_candles_v4(sess, sym, exchange, start_ms, now_utc_ms, cfg["interval_min"])
            expiry_candles[sym] = c
            if c:
                found += 1
        print(f"  {found}/{len(all_symbols)} symbols with data")

        # Discover actual trading dates that belong to this expiry group
        trading_dates: set = set()
        for candles in expiry_candles.values():
            for c in candles:
                d_ist = _ist_date(c["ts"])
                if d_ist in exp_day_set and d_ist not in seen_days:
                    trading_dates.add(d_ist)

        if not trading_dates:
            print(f"  ⚠️  No data found for expiry {expiry} — skipping")
            continue

        print(f"  Trading dates: {sorted(trading_dates)}")

        for day in sorted(trading_dates):
            seen_days.add(day)
            day_candles: Dict[str, list] = {
                sym: [c for c in expiry_candles[sym] if _ist_date(c["ts"]) == day]
                for sym in all_symbols
            }
            if not any(day_candles.values()):
                continue

            spot_day_candles = [c for c in spot_all_candles if _ist_date(c["ts"]) == day]
            day_trades = simulate_day(
                day_str=str(day),
                strikes=strikes,
                expiry=expiry,
                index=index,
                exchange=exchange,
                candles_by_sym=day_candles,
                cfg=cfg,
                spot_day_candles=spot_day_candles,
            )
            all_trades.extend(day_trades)
            day_pnl = sum(t.pnl for t in day_trades)
            sign = "✅" if day_pnl >= 0 else "❌"
            print(f"  {sign}  {day} [exp:{expiry}]  →  {len(day_trades):2d} trades  "
                  f"day P&L = ₹{day_pnl:+,.0f}")

    # ── Print report ────────────────────────────────────────────────────────
    print_report(all_trades, cfg)

    # ── Save JSON ────────────────────────────────────────────────────────────
    out_path = args.out
    if not out_path:
        os.makedirs("logs/trade_history", exist_ok=True)
        exp_tag  = cfg.get("expiry_date", "").replace("-", "")
        out_path = f"logs/trade_history/trendline_backtest_{exp_tag}.jsonl"

    with open(out_path, "w") as f:
        for t in all_trades:
            f.write(json.dumps({
                "date":         t.date,
                "symbol":       t.symbol,
                "strike":       t.strike,
                "opt_type":     t.opt_type,
                "signal":       t.signal,
                "entry_time":   t.entry_time,
                "entry_price":  t.entry_price,
                "exit_time":    t.exit_time,
                "exit_price":   t.exit_price,
                "exit_reason":  t.exit_reason,
                "pts":          t.pts,
                "pnl":          t.pnl,
                "qty":          t.qty,
            }) + "\n")

    print(f"  Trade log saved → {out_path}")
    print(f"  Total trades: {len(all_trades)}\n")


if __name__ == "__main__":
    main()
