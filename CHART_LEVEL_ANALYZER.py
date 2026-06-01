#!/usr/bin/env python3
"""
CHART_LEVEL_ANALYZER.py
=======================
Multi-timeframe chart-based support/resistance analyzer for NIFTY / SENSEX / BANKNIFTY.
Read-only companion bot — run alongside PROD10FEB_ManualBOT or MASTER_SIGNAL_BOT.

PURPOSE:
  Identify key price levels BEFORE taking a trade.
  Each level shows: type, distance, strength (0–10), efficiency (%), and trade guidance.
  Use this to avoid entering trades right into a wall of resistance/support.

HOW TO USE:
  Run in a separate terminal:  python3 CHART_LEVEL_ANALYZER.py

  ✅ Green zone  — price is in open space (≥0.35% to nearest level)   → CAN TRADE
  🟡 Caution     — price within 0.15–0.35% of a moderate level (4-6)  → TRADE WITH CARE
  ⛔ Wait        — price within 0.15% of a strong level (7+/10)        → WAIT / CONFIRM BREAK

LEVELS DETECTED:
  ── Long-term  (D)  : Previous Day H/L, Previous Week H/L, Daily Swing H/L
  ── Mid-term   (1H) : 1-Hour Swing Highs/Lows (last 20 days)
  ── Short-term (15M): 15-min Swing Highs/Lows (last 5 days)
  ── Intraday   (ID) : Standard Pivots (PP, R1–R3, S1–S3)
                       Camarilla Pivots (H3, H4, L3, L4)
                       Opening Range High/Low (9:15–9:30)
                       VWAP (approximate, range-weighted)
  ── Psychological   : Round numbers at configurable step intervals

STRENGTH SCORE (0–10):
  = timeframe base weight
  + touch frequency bonus  (0–3)   — how many times tested
  + rejection quality bonus (0–2)  — wick-to-body ratio at level
  + efficiency bonus        (0–1)  — hold-rate ≥ 65%
  + recency bonus           (0–0.5)— touched in last 5 candles
  + confluence bonus        (0–1.5)— level clusters from 2+ sources

EFFICIENCY (%):
  Percentage of times price respected (held) this level vs broke through it.

Run:  python3 CHART_LEVEL_ANALYZER.py
"""

from __future__ import annotations
import os
import sys
import csv
import json
import time
import math
import re as _re

import subprocess
import threading

import requests
import numpy as np
import pyotp

from datetime import datetime, timedelta
from collections import deque
from typing import Optional

try:
    from growwapi import GrowwAPI
except ImportError:
    print("❗ growwapi not found. Install it or add to PYTHONPATH.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
#  ANSI COLORS
# ─────────────────────────────────────────────────────────────
class C:
    RESET    = "\033[0m";   BOLD     = "\033[1m";    DIM      = "\033[2m"
    RED      = "\033[91m";  GREEN    = "\033[92m";   YELLOW   = "\033[93m"
    BLUE     = "\033[94m";  CYAN     = "\033[96m";   WHITE    = "\033[97m"
    MAGENTA  = "\033[95m";  ORANGE   = "\033[38;5;214m"; LIME = "\033[38;5;154m"
    B_RED    = "\033[1;91m"; B_GREEN = "\033[1;92m"; B_YELLOW = "\033[1;93m"
    B_CYAN   = "\033[1;96m"; B_WHITE = "\033[1;97m"; B_MAGENTA= "\033[1;95m"
    B_ORANGE = "\033[1;38;5;214m"


_ANSI = _re.compile(r'\x1b\[[0-9;]*m')


def vlen(s: str) -> int:
    return len(_ANSI.sub("", s))


def rpad(s: str, w: int) -> str:
    return s + " " * max(0, w - vlen(s))


def lpad(s: str, w: int) -> str:
    pad = max(0, w - vlen(s))
    return " " * pad + s


def _play_alarm(repeat: int = 3, gap: float = 0.4) -> None:
    """Play alert sound `repeat` times sequentially in a background thread.
    Total duration ≈ repeat × 0.7s + (repeat-1) × gap  (default ≈ 3s).
    """
    def _run():
        for i in range(repeat):
            try:
                proc = subprocess.Popen(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                proc.wait()
            except Exception:
                sys.stdout.write("\a")
                sys.stdout.flush()
            if i < repeat - 1:
                time.sleep(gap)
    threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────────────────────
#  CONFIG  — only INDEX usually needs editing
# ─────────────────────────────────────────────────────────────
CLA_CONFIG: dict = {
    # ── Index ────────────────────────────────────────────────
    "INDEX":           "NIFTY",      # "NIFTY" | "SENSEX" | "BANKNIFTY" | "FINNIFTY"

    # ── Timing ───────────────────────────────────────────────
    "REFRESH_SEC":     30,           # Dashboard refresh interval (seconds)
    "MARKET_OPEN":     "09:15",
    "MARKET_CLOSE":    "15:30",

    # ── Telegram ─────────────────────────────────────────────
    "TELEGRAM_ALERTS": True,

    # ── Level clustering / display ───────────────────────────
    "CONFLUENCE_TOL_PCT":  0.25,     # levels within 0.25% → merge into cluster
    "NEAR_LEVEL_PCT":      0.15,     # ⛔ AT-LEVEL alert when price within this %
    "CAUTION_LEVEL_PCT":   0.35,     # 🟡 WATCH zone up to this %

    # ── Strength thresholds ───────────────────────────────────
    "STRONG_SCORE":    7.0,          # ≥ this → strong level (wait/confirm)
    "MODERATE_SCORE":  4.0,          # ≥ this → moderate (caution)

    # ── Candle lookbacks ─────────────────────────────────────
    "LOOKBACK_5M_HRS":    48,        # 5-min: 2 days (~100 candles)
    "LOOKBACK_15M_HRS":   120,       # 15-min: 5 days (~100 candles)
    "LOOKBACK_1H_HRS":    480,       # 1-hour: 20 days (~140 candles)
    "LOOKBACK_1D_HRS":    2160,      # daily: 90 days (~65 candles)

    # ── Swing detection window (candles each side) ────────────
    "SWING_WIN_15M":   3,
    "SWING_WIN_1H":    3,
    "SWING_WIN_1D":    2,

    # ── Touch tolerance per timeframe (% of level price) ──────
    "TOL_5M":   0.06,                # ~15 pts on 24500
    "TOL_15M":  0.10,                # ~25 pts
    "TOL_1H":   0.15,                # ~37 pts
    "TOL_1D":   0.25,                # ~60 pts

    # ── Round number step size per index ─────────────────────
    "ROUND_STEP": {
        "NIFTY":     50,
        "SENSEX":    200,
        "BANKNIFTY": 100,
        "FINNIFTY":  50,
    },

    # ── Dashboard display count ───────────────────────────────
    "DISPLAY_ABOVE":  5,
    "DISPLAY_BELOW":  5,

    # ── Max distance from spot to include a level ─────────────
    "MAX_DIST_PCT":   4.0,           # ignore levels > 4% away from spot
}

# ─────────────────────────────────────────────────────────────
#  CREDENTIALS  (same as main bot)
# ─────────────────────────────────────────────────────────────
API_KEY = (
    "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9"
    ".eyJleHAiOjI1NjQ2NTczODEsImlhdCI6MTc3NjI1NzM4MSwibmJmIjoxNzc2MjU3MzgxLCJzdWIiO"
    "iJ7XCJ0b2tlblJlZklkXCI6XCJjMjAzMmM5MS04ZGYzLTRkZDUtYjc5NS0yMGVlOWRhZDhhZjlcIix"
    "cInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM"
    "1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2Rk"
    "XCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZ"
    "XNzaW9uSWRcIjpcIjI0OWQ2OGRlLTNjZTgtNGQ4OS05ODJkLWM0N2NmYmI1YzdlNFwiLFwiYWRkaXRpb"
    "25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdT"
    "lLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90"
    "cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIyNDA5OjQwYzQ6MTBhMzozN2UzOjE4NGI6N2IyOTpiMzBlOj"
    "IwZTUsMTcyLjcwLjIxOC4xMzUsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjQ2NTcz"
    "ODE2ODYsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9"
    ".3kotfZI_EC0lzszHKlXiRdqEQv-O8ubYFh0pgoAT0KsSfdQ1sHmts5UtlaAq4PB6DEwY4X2jZUCD8uBgc2nwXQ"
)
TOTP_SECRET = "SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4"
BOT_TOKEN   = "8666941668:AAEObDodwWqDwdVJVXy8WvFx_lyreq8p7fI"
CHAT_ID     = "6012308856"

_session = requests.Session()
_opt_chain_cache: dict = {"data": {}, "ts": 0.0}


# ─────────────────────────────────────────────────────────────
#  INIT / TELEGRAM
# ─────────────────────────────────────────────────────────────
def init_groww() -> tuple:
    totp = pyotp.TOTP(TOTP_SECRET).now()
    access_token = GrowwAPI.get_access_token(api_key=API_KEY, totp=totp)
    client = GrowwAPI(access_token)
    print(f"{C.B_GREEN}✅ Chart Level Analyzer: Groww API initialized{C.RESET}")
    return client, access_token


def send_telegram(msg: str) -> None:
    if not CLA_CONFIG.get("TELEGRAM_ALERTS"):
        return
    try:
        _session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=4,
        )
    except Exception:
        pass


def is_market_open() -> bool:
    now = datetime.now()
    o = datetime.strptime(CLA_CONFIG["MARKET_OPEN"],  "%H:%M").replace(
        year=now.year, month=now.month, day=now.day)
    c = datetime.strptime(CLA_CONFIG["MARKET_CLOSE"], "%H:%M").replace(
        year=now.year, month=now.month, day=now.day)
    return o <= now <= c


# ─────────────────────────────────────────────────────────────
#  INSTRUMENTS / EXPIRY  (same logic as FIBONACCI_TREND_ANALYZER)
# ─────────────────────────────────────────────────────────────
_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instrument.csv")
_instruments_cache: list[dict] = []
_instruments_loaded_at: float = 0.0


def _load_instruments() -> list[dict]:
    global _instruments_cache, _instruments_loaded_at
    age_h = (time.time() - _instruments_loaded_at) / 3600
    if _instruments_cache and age_h < 6:
        return _instruments_cache
    should_dl = not os.path.exists(_CSV_PATH)
    if not should_dl:
        age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(_CSV_PATH))
        should_dl = age > timedelta(days=1)
    if should_dl:
        try:
            r = _session.get("https://growwapi-assets.groww.in/instruments/instrument.csv", timeout=30)
            r.raise_for_status()
            with open(_CSV_PATH, "wb") as f:
                f.write(r.content)
            print("✅ instrument.csv updated")
        except Exception as e:
            print(f"⚠️ instrument.csv download failed: {e}")
    if not os.path.exists(_CSV_PATH):
        return []
    rows: list[dict] = []
    with open(_CSV_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    _instruments_cache = rows
    _instruments_loaded_at = time.time()
    return rows


def get_active_expiry(index_name: str) -> tuple[Optional[str], Optional[str]]:
    instruments = _load_instruments()
    expiries: set[str] = set()
    for item in instruments:
        if item.get("underlying_symbol", "").upper() == index_name.upper():
            e = item.get("expiry_date", "").strip()
            if e:
                expiries.add(e)
    today = datetime.now().date()
    future = sorted(e for e in expiries if datetime.strptime(e, "%Y-%m-%d").date() >= today)
    return (future[0] if len(future) >= 1 else None,
            future[1] if len(future) >= 2 else None)


# ─────────────────────────────────────────────────────────────
#  DATA FETCHING  (mirrors FIBONACCI_TREND_ANALYZER exactly)
# ─────────────────────────────────────────────────────────────
def get_spot_price(index_name: str, access_token: str) -> Optional[float]:
    expiry, _ = get_active_expiry(index_name)
    if not expiry:
        return None
    try:
        exchange = "BSE" if index_name.upper() == "SENSEX" else "NSE"
        url = (f"https://api.groww.in/v1/option-chain/exchange/{exchange}"
               f"/underlying/{index_name.upper()}?expiry_date={expiry}")
        resp = _session.get(url, headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0",
        }, timeout=6)
        if resp.status_code == 200:
            ltp = resp.json().get("payload", {}).get("underlying_ltp")
            return float(ltp) if ltp else None
    except Exception:
        pass
    return None


def fetch_candles(groww, index_name: str, interval: str, hours_back: int) -> list[dict]:
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(hours=hours_back)
    idx = index_name.upper()
    if idx == "NIFTY":
        exchange = groww.EXCHANGE_NSE
        symbols  = ["NSE-NIFTY 50", "NSE-NIFTY"]
    elif idx == "SENSEX":
        exchange = groww.EXCHANGE_BSE
        symbols  = ["BSE-SENSEX", "BSE-S&P BSE SENSEX"]
    elif idx == "BANKNIFTY":
        exchange = groww.EXCHANGE_NSE
        symbols  = ["NSE-NIFTY BANK", "NSE-BANKNIFTY"]
    elif idx == "FINNIFTY":
        exchange = groww.EXCHANGE_NSE
        symbols  = ["NSE-NIFTY FIN SERVICE"]
    else:
        return []

    for sym in symbols:
        try:
            result = groww.get_historical_candles(
                groww_symbol=sym,
                exchange=exchange,
                segment="CASH",
                start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=interval,
            )
            if result and result.get("candles") and len(result["candles"]) >= 3:
                return [
                    {"ts": c[0], "open": float(c[1]), "high": float(c[2]),
                     "low": float(c[3]), "close": float(c[4])}
                    for c in result["candles"]
                ]
        except Exception:
            pass
    return []


# ─────────────────────────────────────────────────────────────
#  CANDLE UTILITIES
# ─────────────────────────────────────────────────────────────
def _ts_to_dt(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromtimestamp(int(ts) / 1000)
    except Exception:
        return datetime.now()


def today_candles(candles: list[dict]) -> list[dict]:
    today = datetime.now().date()
    return [c for c in candles if _ts_to_dt(c["ts"]).date() == today]


def prev_day_ohlc(candles_1d: list[dict]) -> Optional[dict]:
    """Return the most recent past trading day's full OHLC candle."""
    today = datetime.now().date()
    past  = [c for c in candles_1d if _ts_to_dt(c["ts"]).date() < today]
    if not past:
        return None
    return sorted(past, key=lambda c: _ts_to_dt(c["ts"]))[-1]


def prev_week_hl(candles_1d: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """High and low of the previous calendar week."""
    today    = datetime.now().date()
    mon      = today - timedelta(days=today.weekday())
    prev_mon = mon - timedelta(days=7)
    prev_fri = mon - timedelta(days=3)
    pw = [c for c in candles_1d
          if prev_mon <= _ts_to_dt(c["ts"]).date() <= prev_fri]
    if not pw:
        return None, None
    return max(c["high"] for c in pw), min(c["low"] for c in pw)


# ─────────────────────────────────────────────────────────────
#  SWING DETECTION  (identical algo to FIBONACCI_TREND_ANALYZER)
# ─────────────────────────────────────────────────────────────
def detect_swings(candles: list[dict], window: int = 3) -> list[dict]:
    """
    Return alternating swing highs and lows.
    Each entry: {"type": "HIGH"|"LOW", "price": float, "idx": int}
    """
    n = len(candles)
    if n < window * 2 + 1:
        return []
    raw: list[dict] = []
    for i in range(window, n - window):
        w = candles[i - window: i + window + 1]
        c = candles[i]
        if c["high"] == max(b["high"] for b in w):
            raw.append({"type": "HIGH", "price": c["high"], "idx": i})
        elif c["low"] == min(b["low"] for b in w):
            raw.append({"type": "LOW",  "price": c["low"],  "idx": i})
    validated: list[dict] = []
    for s in raw:
        if not validated:
            validated.append(s)
        elif validated[-1]["type"] == s["type"]:
            if s["type"] == "HIGH" and s["price"] > validated[-1]["price"]:
                validated[-1] = s
            elif s["type"] == "LOW" and s["price"] < validated[-1]["price"]:
                validated[-1] = s
        else:
            validated.append(s)
    return validated


# ─────────────────────────────────────────────────────────────
#  LEVEL CALCULATIONS
# ─────────────────────────────────────────────────────────────
def calc_standard_pivots(high: float, low: float, close: float) -> dict:
    pp  = (high + low + close) / 3
    rng = high - low
    return {
        "PP": round(pp,          2),
        "R1": round(2*pp - low,  2),
        "R2": round(pp + rng,    2),
        "R3": round(high + 2*(pp - low), 2),
        "S1": round(2*pp - high, 2),
        "S2": round(pp - rng,    2),
        "S3": round(low  - 2*(high - pp), 2),
    }


def calc_camarilla_pivots(high: float, low: float, close: float) -> dict:
    rng = high - low
    return {
        "H4": round(close + rng * 1.1 / 2, 2),
        "H3": round(close + rng * 1.1 / 4, 2),
        "L3": round(close - rng * 1.1 / 4, 2),
        "L4": round(close - rng * 1.1 / 2, 2),
    }


def calc_opening_range(candles_5m: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """High/low of the 9:15–9:30 opening range (first 3 five-min candles of day)."""
    tc = today_candles(candles_5m)
    or_c = []
    for c in tc:
        dt = _ts_to_dt(c["ts"])
        if dt.hour == 9 and dt.minute <= 30:
            or_c.append(c)
    if not or_c:
        return None, None
    return max(c["high"] for c in or_c), min(c["low"] for c in or_c)


def calc_vwap(candles: list[dict]) -> Optional[float]:
    """
    Approximate VWAP for today using typical price weighted by candle range
    (range is a reasonable proxy for volume when actual volume is unavailable).
    """
    tc = today_candles(candles)
    if not tc:
        return None
    num = sum((c["high"] + c["low"] + c["close"]) / 3 * (c["high"] - c["low"]) for c in tc)
    den = sum(c["high"] - c["low"] for c in tc)
    return round(num / den, 2) if den > 0 else None


def get_round_numbers(spot: float, index_name: str, radius_pct: float = 3.0) -> list[float]:
    """Return round-number levels within radius_pct% of spot."""
    step = CLA_CONFIG["ROUND_STEP"].get(index_name.upper(), 100)
    base = round(spot / step) * step
    levels = []
    for i in range(-20, 21):
        lvl = round(base + i * step, 2)
        if lvl > 0 and abs(lvl - spot) / spot * 100 <= radius_pct:
            levels.append(lvl)
    return levels


# ─────────────────────────────────────────────────────────────
#  LEVEL STRENGTH ANALYSIS
# ─────────────────────────────────────────────────────────────
def analyze_level_strength(
    level: float,
    candles: list[dict],
    tol_pct: float,
    tf_base_weight: float,
) -> tuple[float, float, int]:
    """
    Evaluate strength and reliability of a price level.

    Returns:
        strength  : float 0–10
        efficiency: float 0–100  (% of times price held vs broke)
        touches   : int          (number of times price tested this level)

    Strength components:
        tf_base_weight  — base score from timeframe importance
        +0–3            — touch frequency (more touches = stronger memory)
        +0–2            — rejection quality (big wick = strong rejection)
        +0–1            — efficiency bonus (high hold-rate)
        +0–0.5          — recency (tested recently = still active)
    """
    if not candles:
        return round(tf_base_weight, 1), 55.0, 0

    tol    = level * tol_pct / 100
    touches = 0
    holds   = 0
    breaks  = 0
    strong_rejections = 0

    for i, c in enumerate(candles):
        # A "touch": the candle's range overlaps the level ± tolerance
        if not (c["low"] <= level + tol and c["high"] >= level - tol):
            continue
        touches += 1

        # Rejection quality: how far the close is from the level vs total range
        c_range = c["high"] - c["low"]
        if c_range > 0:
            if c["close"] >= level:
                # closed above — level acted as support; strong if close well above
                rej = (c["close"] - level) / c_range
            else:
                # closed below — level acted as resistance; strong if close well below
                rej = (level - c["close"]) / c_range
            if rej > 0.45:
                strong_rejections += 1

        # Hold vs break: look at the following candle's close
        if i + 1 < len(candles):
            nxt = candles[i + 1]
            if c["close"] >= level:
                # level was support; break = next close slips below
                if nxt["close"] < level - tol:
                    breaks += 1
                else:
                    holds += 1
            else:
                # level was resistance; break = next close pushes above
                if nxt["close"] > level + tol:
                    breaks += 1
                else:
                    holds += 1

    total      = holds + breaks
    efficiency = (holds / total * 100) if total > 0 else 55.0

    score = tf_base_weight

    # Touch frequency bonus (0–3)
    if   touches >= 6: score += 3.0
    elif touches >= 4: score += 2.0
    elif touches >= 2: score += 1.0
    elif touches == 1: score += 0.5

    # Rejection quality bonus (0–2)
    score += min(2.0, strong_rejections * 0.5)

    # Efficiency bonus (0–1)
    if   efficiency >= 80: score += 1.0
    elif efficiency >= 65: score += 0.5

    # Recency bonus: tested in the last 5 candles of this timeframe (0–0.5)
    for c in candles[-min(5, len(candles)):]:
        if c["low"] <= level + tol and c["high"] >= level - tol:
            score += 0.5
            break

    return min(10.0, round(score, 1)), round(efficiency, 1), touches


# ─────────────────────────────────────────────────────────────
#  BUILD ALL LEVELS
# ─────────────────────────────────────────────────────────────
def build_all_levels(
    spot: float,
    index_name: str,
    candles_5m:  list[dict],
    candles_15m: list[dict],
    candles_1h:  list[dict],
    candles_1d:  list[dict],
) -> list[dict]:
    """
    Collect all S/R levels from every source.
    Each level dict:
      price, label, tf, strength, efficiency, touches, confluent, extra_types
    """
    idx    = index_name.upper()
    levels: list[dict] = []
    max_d  = CLA_CONFIG["MAX_DIST_PCT"]

    def _add(price: float, label: str, tf: str,
             ref_candles: list[dict], tol_pct: float, tf_weight: float) -> None:
        if price <= 0:
            return
        if abs(price - spot) / spot * 100 > max_d:
            return  # too far away to be relevant now
        strength, eff, touches = analyze_level_strength(price, ref_candles, tol_pct, tf_weight)
        levels.append({
            "price":      round(price, 2),
            "label":      label,
            "tf":         tf,
            "strength":   strength,
            "efficiency": eff,
            "touches":    touches,
            "confluent":  False,
            "extra_types": [],
        })

    # ── Previous Day OHLC ─────────────────────────────────────
    pd = prev_day_ohlc(candles_1d)
    if pd:
        _add(pd["high"],  "PDH",   "D",  candles_15m, CLA_CONFIG["TOL_15M"], 2.5)
        _add(pd["low"],   "PDL",   "D",  candles_15m, CLA_CONFIG["TOL_15M"], 2.5)
        _add(pd["open"],  "PDO",   "D",  candles_15m, CLA_CONFIG["TOL_15M"], 1.5)
        _add(pd["close"], "PDC",   "D",  candles_15m, CLA_CONFIG["TOL_15M"], 1.5)

    # ── Previous Week High/Low ────────────────────────────────
    pwh, pwl = prev_week_hl(candles_1d)
    if pwh:
        _add(pwh, "PWH", "D", candles_1h, CLA_CONFIG["TOL_1H"], 3.0)
    if pwl:
        _add(pwl, "PWL", "D", candles_1h, CLA_CONFIG["TOL_1H"], 3.0)

    # ── Daily Swing Highs / Lows (last 30 swings ≈ 3 months) ──
    d_swings = detect_swings(candles_1d, window=CLA_CONFIG["SWING_WIN_1D"])
    for s in d_swings[-30:]:
        lbl = "D Swing H" if s["type"] == "HIGH" else "D Swing L"
        _add(s["price"], lbl, "D", candles_1d, CLA_CONFIG["TOL_1D"], 3.0)

    # ── Standard Pivot Points (from prev day OHLC) ─────────────
    if pd:
        piv = calc_standard_pivots(pd["high"], pd["low"], pd["close"])
        _add(piv["PP"], "Pivot PP", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 2.5)
        _add(piv["R1"], "Pivot R1", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 2.0)
        _add(piv["R2"], "Pivot R2", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 1.5)
        _add(piv["R3"], "Pivot R3", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 1.0)
        _add(piv["S1"], "Pivot S1", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 2.0)
        _add(piv["S2"], "Pivot S2", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 1.5)
        _add(piv["S3"], "Pivot S3", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 1.0)

        # ── Camarilla Pivots ──────────────────────────────────
        cam = calc_camarilla_pivots(pd["high"], pd["low"], pd["close"])
        _add(cam["H4"], "Cam H4", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 2.5)
        _add(cam["H3"], "Cam H3", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 2.0)
        _add(cam["L3"], "Cam L3", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 2.0)
        _add(cam["L4"], "Cam L4", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 2.5)

    # ── Opening Range High/Low ────────────────────────────────
    orh, orl = calc_opening_range(candles_5m)
    if orh:
        _add(orh, "OR High", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 1.5)
    if orl:
        _add(orl, "OR Low",  "ID", candles_5m, CLA_CONFIG["TOL_5M"], 1.5)

    # ── VWAP ──────────────────────────────────────────────────
    vwap = calc_vwap(candles_5m if candles_5m else candles_15m)
    if vwap:
        _add(vwap, "VWAP", "ID", candles_5m, CLA_CONFIG["TOL_5M"], 1.5)

    # ── 1-Hour Swing Highs/Lows ───────────────────────────────
    h1_swings = detect_swings(candles_1h, window=CLA_CONFIG["SWING_WIN_1H"])
    for s in h1_swings[-30:]:
        lbl = "1H Swing H" if s["type"] == "HIGH" else "1H Swing L"
        _add(s["price"], lbl, "1H", candles_1h, CLA_CONFIG["TOL_1H"], 2.0)

    # ── 15-Min Swing Highs/Lows ───────────────────────────────
    m15_swings = detect_swings(candles_15m, window=CLA_CONFIG["SWING_WIN_15M"])
    for s in m15_swings[-40:]:
        lbl = "15M Swing H" if s["type"] == "HIGH" else "15M Swing L"
        _add(s["price"], lbl, "15M", candles_15m, CLA_CONFIG["TOL_15M"], 1.5)

    # ── Round / Psychological Numbers ────────────────────────
    for rn in get_round_numbers(spot, idx):
        if abs(rn - spot) < 1:
            continue  # skip if it IS the current spot
        lbl = f"Round {rn:,.0f}"
        _add(rn, lbl, "ALL", candles_15m, CLA_CONFIG["TOL_15M"], 2.0)

    return levels


# ─────────────────────────────────────────────────────────────
#  CONFLUENCE MERGE
# ─────────────────────────────────────────────────────────────
def merge_confluence_levels(levels: list[dict], spot: float) -> list[dict]:
    """
    Levels within CONFLUENCE_TOL_PCT of each other are merged into one cluster.
    The cluster keeps the strongest member's price and gets a +1.5 bonus
    (capped at 10.0) to represent multi-source agreement.
    """
    tol = spot * CLA_CONFIG["CONFLUENCE_TOL_PCT"] / 100
    if not levels:
        return []

    sorted_lvls = sorted(levels, key=lambda x: x["price"])
    merged: list[dict] = []

    for lvl in sorted_lvls:
        placed = False
        for cluster in merged:
            if abs(cluster["price"] - lvl["price"]) <= tol:
                # Merge: keep stronger member's price; add bonus
                if lvl["strength"] > cluster["strength"]:
                    cluster["price"] = lvl["price"]
                    cluster["label"] = lvl["label"]
                    cluster["tf"]    = lvl["tf"]
                cluster["extra_types"].append(f"{lvl['label']}[{lvl['tf']}]")
                cluster["confluent"]  = True
                cluster["strength"]   = min(10.0, round(
                    max(cluster["strength"], lvl["strength"]) + 1.5, 1))
                cluster["efficiency"] = max(cluster["efficiency"], lvl["efficiency"])
                cluster["touches"]   += lvl["touches"]
                placed = True
                break
        if not placed:
            merged.append(dict(lvl))

    return merged


# ─────────────────────────────────────────────────────────────
#  TRADE DECISION ENGINE
# ─────────────────────────────────────────────────────────────
def generate_trade_decision(
    spot: float,
    above: list[dict],
    below: list[dict],
) -> tuple[str, str, str]:
    """
    Returns (signal, one-line summary, color).
    signal: "WAIT" | "CAUTION" | "OK"
    """
    near_pct    = CLA_CONFIG["NEAR_LEVEL_PCT"]
    caution_pct = CLA_CONFIG["CAUTION_LEVEL_PCT"]
    strong_th   = CLA_CONFIG["STRONG_SCORE"]
    moderate_th = CLA_CONFIG["MODERATE_SCORE"]

    def dist_pct(lvl: dict) -> float:
        return abs(lvl["price"] - spot) / spot * 100

    at_res = [l for l in above[:3] if dist_pct(l) <= near_pct]
    at_sup = [l for l in below[:3] if dist_pct(l) <= near_pct]
    near_res = [l for l in above[:3] if near_pct < dist_pct(l) <= caution_pct]
    near_sup = [l for l in below[:3] if near_pct < dist_pct(l) <= caution_pct]

    # ── AT a strong level ──────────────────────────────────────
    strong_at = [l for l in at_res + at_sup if l["strength"] >= strong_th]
    if strong_at:
        labels = "  +  ".join(
            f"{l['label']}({l['strength']:.1f}/10)" for l in strong_at[:2])
        return ("WAIT",
                f"⛔ Price AT strong level: {labels}  →  Wait for break/bounce confirmation",
                C.B_RED)

    # ── AT a moderate level ────────────────────────────────────
    if at_res or at_sup:
        all_at = at_res + at_sup
        labels = "  +  ".join(f"{l['label']}({l['strength']:.1f})" for l in all_at[:2])
        return ("CAUTION",
                f"🟡 Price touching level: {labels}  →  Confirm direction before entry",
                C.B_YELLOW)

    # ── Between levels: check space ───────────────────────────
    if above and below:
        top = above[0]
        bot = below[0]
        gap_above = (top["price"] - spot) / spot * 100
        gap_below = (spot - bot["price"]) / spot * 100
        pts_above = top["price"] - spot
        pts_below = spot - bot["price"]

        # Squeezed between two meaningful levels
        if gap_above < 0.3 and gap_below < 0.3:
            if top["strength"] >= moderate_th or bot["strength"] >= moderate_th:
                return ("CAUTION",
                        f"🟡 Squeezed: ▲{top['label']} +{pts_above:.0f}pts  "
                        f"▼{bot['label']} -{pts_below:.0f}pts  →  Wait for breakout",
                        C.B_YELLOW)

        # Near strong upcoming level
        if near_res:
            strong_near = [l for l in near_res if l["strength"] >= strong_th]
            if strong_near:
                l = strong_near[0]
                return ("CAUTION",
                        f"👀 Approaching strong resistance {l['label']}({l['strength']:.1f}/10) "
                        f"+{l['price']-spot:.0f}pts  →  Plan re-entry above or short at level",
                        C.B_YELLOW)
        if near_sup:
            strong_near = [l for l in near_sup if l["strength"] >= strong_th]
            if strong_near:
                l = strong_near[0]
                return ("CAUTION",
                        f"👀 Approaching strong support {l['label']}({l['strength']:.1f}/10) "
                        f"-{spot-l['price']:.0f}pts  →  Plan re-entry below or long at level",
                        C.B_YELLOW)

        # Good open space
        if gap_above >= 0.4 and gap_below >= 0.4:
            return ("OK",
                    f"✅ Open space: ▲{pts_above:.0f}pts to {top['label']}  "
                    f"▼{pts_below:.0f}pts to {bot['label']}  →  Can trade freely",
                    C.B_GREEN)

    return ("CAUTION",
            "🟡 Level picture unclear — monitor closely",
            C.B_YELLOW)


# ─────────────────────────────────────────────────────────────
#  OPTION SUGGESTION ENGINE  (multi-source: chart + MASTER_BOT)
# ─────────────────────────────────────────────────────────────

def fetch_option_chain_data(access_token: str, index_name: str, spot: float) -> dict:
    """
    Fetch live LTPs for strikes near spot using the correct Groww FNO endpoint.
    Builds instrument list from instrument.csv, then batch-fetches via /v1/live-data/ltp.
    Returns {strike: {ce_ltp, pe_ltp}}. Cached 5 minutes.
    """
    global _opt_chain_cache
    now = time.time()
    if now - _opt_chain_cache["ts"] < 300 and _opt_chain_cache["data"]:
        return _opt_chain_cache["data"]

    instruments = _load_instruments()
    expiry, _   = get_active_expiry(index_name)
    if not expiry or not instruments:
        return _opt_chain_cache["data"]

    idx      = index_name.upper()
    exchange = "BSE" if idx == "SENSEX" else "NSE"
    step     = 200 if idx == "SENSEX" else 100 if idx == "BANKNIFTY" else 50
    nearest  = round(spot / step) * step
    radius   = step * 7      # ±7 strikes around ATM

    # Build {strike: {CE: item, PE: item}} from instrument.csv
    strikes_map: dict[float, dict] = {}
    for item in instruments:
        if item.get("underlying_symbol", "").upper() != idx:
            continue
        if item.get("expiry_date", "").strip() != expiry:
            continue
        try:
            s = float(item.get("strike_price", 0))
        except (ValueError, TypeError):
            continue
        if abs(s - nearest) > radius:
            continue
        opt_type = item.get("instrument_type", "").upper()
        if opt_type not in ("CE", "PE"):
            continue
        if s not in strikes_map:
            strikes_map[s] = {}
        strikes_map[s][opt_type] = item

    if not strikes_map:
        return _opt_chain_cache["data"]

    # Build exchange_symbol list → "NSE_NIFTY26JUN23800CE"
    sym_to_key: dict[str, tuple] = {}    # symbol → (strike, opt_type)
    for strike, opts in strikes_map.items():
        for opt_type, item in opts.items():
            ts = item.get("trading_symbol", "")
            if ts:
                sym = f"{exchange}_{ts}"
                sym_to_key[sym] = (strike, opt_type)

    if not sym_to_key:
        return _opt_chain_cache["data"]

    chain: dict = {}
    all_syms = list(sym_to_key.keys())
    headers  = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0",
    }

    # Groww supports batching; send in chunks of 20 to stay safe
    for i in range(0, len(all_syms), 20):
        chunk = all_syms[i: i + 20]
        try:
            url  = (f"https://api.groww.in/v1/live-data/ltp"
                    f"?segment=FNO&exchange_symbols={','.join(chunk)}")
            resp = _session.get(url, headers=headers, timeout=8)
            if resp.status_code != 200:
                continue
            ltp_payload = resp.json().get("payload", {})
            for sym in chunk:
                val = ltp_payload.get(sym)
                if val is None:
                    continue
                try:
                    ltp_val = float(val)
                except (ValueError, TypeError):
                    continue
                strike, opt_type = sym_to_key[sym]
                if strike not in chain:
                    chain[strike] = {"ce_ltp": 0.0, "pe_ltp": 0.0}
                if opt_type == "CE":
                    chain[strike]["ce_ltp"] = ltp_val
                else:
                    chain[strike]["pe_ltp"] = ltp_val
        except Exception:
            continue

    # Keep only strikes with at least one non-zero LTP
    chain = {k: v for k, v in chain.items() if v["ce_ltp"] > 0 or v["pe_ltp"] > 0}
    if chain:
        _opt_chain_cache = {"data": chain, "ts": now}
    return chain if chain else _opt_chain_cache.get("data", {})


def _read_master_signal(index_name: str) -> Optional[dict]:
    """Read the latest MASTER_SIGNAL_BOT JSON entry (< 5 min old). Returns None if unavailable."""
    base    = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base, "logs", "master_signal")
    if not os.path.isdir(log_dir):
        return None
    try:
        files = sorted(
            [f for f in os.listdir(log_dir)
             if f.startswith("Master_Signal_") and f.endswith(".log")],
            reverse=True,
        )
        for fname in files[:3]:
            fpath = os.path.join(log_dir, fname)
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("index", "").upper() != index_name.upper():
                    continue
                ts_str = data.get("ts", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if (datetime.now() - ts).total_seconds() > 300:
                            return None   # stale
                    except ValueError:
                        pass
                return data
    except Exception:
        pass
    return None


def _recent_momentum(candles_5m: list[dict], n: int = 4) -> str:
    """'BULLISH' | 'BEARISH' | 'NEUTRAL' from last n five-min candles."""
    recent = candles_5m[-n:] if len(candles_5m) >= n else candles_5m
    if not recent:
        return "NEUTRAL"
    closes  = [c["close"] for c in recent]
    opens   = [c["open"]  for c in recent]
    bullish = sum(1 for o, c in zip(opens, closes) if c > o)
    bearish = sum(1 for o, c in zip(opens, closes) if c < o)
    net     = closes[-1] - closes[0]
    if bullish >= 3 and net > 0:
        return "BULLISH"
    if bearish >= 3 and net < 0:
        return "BEARISH"
    if bullish > bearish and net > 8:
        return "BULLISH"
    if bearish > bullish and net < -8:
        return "BEARISH"
    return "NEUTRAL"


def analyze_option_signal(
    spot: float,
    above: list[dict],
    below: list[dict],
    vwap: Optional[float],
    decision: str,
    candles_5m: list[dict],
    index_name: str = "",
) -> dict:
    """
    Multi-source option signal engine.
    Sources: chart S/R levels + MASTER_SIGNAL_BOT log + VWAP + 5m momentum.
    Returns a signal dict with direction, spot target/SL, R:R, confidence, and sources used.
    """
    def _none(reason: str) -> dict:
        return {
            "direction": "NONE", "reason": reason,
            "spot_entry": spot, "spot_target": 0.0, "spot_sl": 0.0,
            "target_pts": 0.0, "sl_pts": 0.0,
            "rr_ratio": 0.0, "confidence": "NONE",
            "target_label": "", "sl_label": "", "sources": [],
        }

    if not above or not below:
        return _none("Insufficient S/R level data")

    MIN_TARGET = 120   # pts of space needed for a worthwhile intraday option trade
    MIN_SL     = 25    # minimum SL in pts
    MAX_SL     = 115   # too wide → skip
    MIN_RR     = 2.0   # reward:risk floor

    top = above[0]
    bot = below[0]
    gap_above = top["price"] - spot
    gap_below = spot - bot["price"]

    momentum   = _recent_momentum(candles_5m)
    above_vwap = (spot > vwap) if vwap else None

    last_c = candles_5m[-1] if candles_5m else None

    def _strong_bull(c: Optional[dict]) -> bool:
        if not c:
            return False
        rng = c["high"] - c["low"]
        return rng > 0 and c["close"] > c["open"] and (c["close"] - c["open"]) / rng > 0.5

    def _strong_bear(c: Optional[dict]) -> bool:
        if not c:
            return False
        rng = c["high"] - c["low"]
        return rng > 0 and c["close"] < c["open"] and (c["open"] - c["close"]) / rng > 0.5

    bull_candle = _strong_bull(last_c)
    bear_candle = _strong_bear(last_c)

    near_pct   = CLA_CONFIG["NEAR_LEVEL_PCT"]
    strong_thr = CLA_CONFIG["STRONG_SCORE"]

    at_strong_sup = any(
        abs(l["price"] - spot) / spot * 100 <= near_pct and l["strength"] >= strong_thr
        for l in below[:3])
    at_strong_res = any(
        abs(l["price"] - spot) / spot * 100 <= near_pct and l["strength"] >= strong_thr
        for l in above[:3])

    # ── Chart-level vote system ────────────────────────────────
    bull_votes = 0
    if momentum == "BULLISH":                  bull_votes += 2
    elif momentum == "NEUTRAL":                bull_votes += 1
    if above_vwap:                             bull_votes += 1
    if bull_candle:                            bull_votes += 1
    if decision == "OK":                       bull_votes += 1
    if at_strong_sup and bull_candle:          bull_votes += 2   # confirmed bounce
    if at_strong_res:                          bull_votes -= 3   # resistance wall

    bear_votes = 0
    if momentum == "BEARISH":                  bear_votes += 2
    elif momentum == "NEUTRAL":               bear_votes += 1
    if above_vwap is not None and not above_vwap:
        bear_votes += 1
    if bear_candle:                            bear_votes += 1
    if decision == "OK":                       bear_votes += 1
    if at_strong_res and bear_candle:          bear_votes += 2   # confirmed rejection
    if at_strong_sup:                          bear_votes -= 3   # support floor

    # ── Read MASTER_SIGNAL_BOT ─────────────────────────────────
    master = _read_master_signal(index_name) if index_name else None
    master_dir    = ""
    master_conf   = 0.0
    sources_used: list[str] = ["chart"]

    if master:
        master_dir  = master.get("direction", "")
        master_conf = float(master.get("confidence", 0))
        sources_used.append(f"MASTER_BOT({master_dir}@{master_conf:.0f}%)")

        if master_dir == "CE" and master_conf >= 60:
            bull_votes += 3 if master_conf >= 75 else 1
        elif master_dir == "PE" and master_conf >= 60:
            bear_votes += 3 if master_conf >= 75 else 1
        elif master_dir == "WAIT":
            bull_votes -= 1
            bear_votes -= 1

    # ── Resolve target / SL geometry ──────────────────────────
    def _resolve_ce() -> Optional[dict]:
        tgt_lvl = above[1] if (gap_above < MIN_TARGET and len(above) > 1) else top
        tgt_pts = tgt_lvl["price"] - spot
        if tgt_pts < MIN_TARGET:
            return None
        sl_raw = spot - bot["price"]
        if sl_raw < MIN_SL:
            sl_v, spot_sl_v, sl_lbl = float(MIN_SL), spot - MIN_SL, f"Fixed -{MIN_SL:.0f}pts"
        elif sl_raw > MAX_SL:
            if len(below) > 1 and (spot - below[1]["price"]) <= MAX_SL:
                sl_v, spot_sl_v = spot - below[1]["price"], below[1]["price"]
                sl_lbl = below[1]["label"]
            else:
                return None
        else:
            sl_v, spot_sl_v, sl_lbl = sl_raw, bot["price"], bot["label"]
        rr = tgt_pts / max(sl_v, 1)
        if rr < MIN_RR:
            return None
        return {"spot_target": tgt_lvl["price"], "spot_sl": spot_sl_v,
                "target_pts": round(tgt_pts, 0), "sl_pts": round(sl_v, 0),
                "rr_ratio": round(rr, 1), "target_label": tgt_lvl["label"], "sl_label": sl_lbl}

    def _resolve_pe() -> Optional[dict]:
        tgt_lvl = below[1] if (gap_below < MIN_TARGET and len(below) > 1) else bot
        tgt_pts = spot - tgt_lvl["price"]
        if tgt_pts < MIN_TARGET:
            return None
        sl_raw = top["price"] - spot
        if sl_raw < MIN_SL:
            sl_v, spot_sl_v, sl_lbl = float(MIN_SL), spot + MIN_SL, f"Fixed +{MIN_SL:.0f}pts"
        elif sl_raw > MAX_SL:
            if len(above) > 1 and (above[1]["price"] - spot) <= MAX_SL:
                sl_v, spot_sl_v = above[1]["price"] - spot, above[1]["price"]
                sl_lbl = above[1]["label"]
            else:
                return None
        else:
            sl_v, spot_sl_v, sl_lbl = sl_raw, top["price"], top["label"]
        rr = tgt_pts / max(sl_v, 1)
        if rr < MIN_RR:
            return None
        return {"spot_target": tgt_lvl["price"], "spot_sl": spot_sl_v,
                "target_pts": round(tgt_pts, 0), "sl_pts": round(sl_v, 0),
                "rr_ratio": round(rr, 1), "target_label": tgt_lvl["label"], "sl_label": sl_lbl}

    def _bull_reason() -> str:
        parts = []
        if at_strong_sup and bull_candle:
            sup = next((l["label"] for l in below[:3]
                        if abs(l["price"] - spot) / spot * 100 <= near_pct), bot["label"])
            parts.append(f"Bounce off {sup}")
        if momentum == "BULLISH":  parts.append("bullish momentum")
        if above_vwap:             parts.append("above VWAP")
        if decision == "OK":       parts.append("open space")
        if master and master_dir == "CE":
            parts.append(f"MASTER_BOT CE ({master_conf:.0f}%)")
        return " | ".join(parts) if parts else "moderate bullish bias"

    def _bear_reason() -> str:
        parts = []
        if at_strong_res and bear_candle:
            res = next((l["label"] for l in above[:3]
                        if abs(l["price"] - spot) / spot * 100 <= near_pct), top["label"])
            parts.append(f"Rejection at {res}")
        if momentum == "BEARISH":  parts.append("bearish momentum")
        if above_vwap is not None and not above_vwap:
            parts.append("below VWAP")
        if decision == "OK":       parts.append("open space")
        if master and master_dir == "PE":
            parts.append(f"MASTER_BOT PE ({master_conf:.0f}%)")
        return " | ".join(parts) if parts else "moderate bearish bias"

    THRESHOLD = 3

    if bull_votes >= THRESHOLD and bull_votes >= bear_votes:
        geo = _resolve_ce()
        if not geo:
            return _none("Bullish bias — insufficient target/SL structure")
        conf = "HIGH" if (bull_votes >= 5 and (not master or master_dir != "PE")) else "MEDIUM"
        # "NOW" when spot is in open space with confirmed bull candle — buy at market
        # "BREAK" when at/near a level — wait for option premium to break above trigger
        entry_type = "NOW" if (decision == "OK" and bull_candle) else "BREAK"
        return {"direction": "CE", "reason": _bull_reason(), "entry_type": entry_type,
                "spot_entry": spot, "confidence": conf, "sources": sources_used, **geo}

    if bear_votes >= THRESHOLD and bear_votes > bull_votes:
        geo = _resolve_pe()
        if not geo:
            return _none("Bearish bias — insufficient target/SL structure")
        conf = "HIGH" if (bear_votes >= 5 and (not master or master_dir != "CE")) else "MEDIUM"
        entry_type = "NOW" if (decision == "OK" and bear_candle) else "BREAK"
        return {"direction": "PE", "reason": _bear_reason(), "entry_type": entry_type,
                "spot_entry": spot, "confidence": conf, "sources": sources_used, **geo}

    if decision == "WAIT":
        near = [l["label"] for l in above[:2] + below[:2]
                if abs(l["price"] - spot) / spot * 100 <= near_pct and l["strength"] >= 7]
        suffix = f" ({', '.join(near[:2])})" if near else ""
        return _none(f"At strong level{suffix} — wait for break/bounce")
    return _none("No confirmed directional signal — monitor for setup")


def find_best_option(
    spot: float,
    option_chain: dict,
    direction: str,
    min_prem: float = 90.0,
    max_prem: float = 160.0,
) -> Optional[dict]:
    """Return the closest-to-ATM option with premium in [min_prem, max_prem]."""
    if not option_chain:
        return None
    candidates = []
    for strike, prices in option_chain.items():
        ltp = prices["ce_ltp"] if direction == "CE" else prices["pe_ltp"]
        if min_prem <= ltp <= max_prem:
            candidates.append({"strike": int(strike), "ltp": ltp,
                                "dist": abs(strike - spot)})
    if not candidates:
        return None
    # CE: prefer ATM-or-OTM (strike ≥ spot); PE: prefer ATM-or-OTM (strike ≤ spot)
    side = [c for c in candidates
            if (c["strike"] >= spot if direction == "CE" else c["strike"] <= spot)]
    return min(side if side else candidates, key=lambda x: x["dist"])


def _opt_trigger(ltp: float) -> float:
    """Nearest 5-multiple strictly above current option LTP (breakout confirmation level)."""
    t = math.ceil(ltp / 5) * 5
    return t if t > ltp else t + 5


def _opt_limit(ltp: float) -> float:
    """Nearest 5-multiple strictly below current option LTP (pullback/dip entry level)."""
    t = math.floor(ltp / 5) * 5
    return t if t < ltp else t - 5


def render_option_section(
    spot: float,
    option_signal: dict,
    option_info: Optional[dict],
    index_name: str,
    expiry: str,
    W: int = 108,
) -> None:
    """Option BUY suggestion panel — clean 4-line format."""
    box_w = W - 6
    col_w = box_w - 2

    direction  = option_signal.get("direction", "NONE")
    confidence = option_signal.get("confidence", "NONE")

    print()

    if direction == "NONE":
        reason = option_signal.get("reason", "")
        hdr = (f"  💡 OPTION SUGGESTION  │  "
               f"{C.YELLOW}No confirmation{C.RESET}  →  {C.DIM}{reason}{C.RESET}")
        print(f"  {C.DIM}┌{'─' * box_w}┐{C.RESET}")
        print(f"  {C.DIM}│{C.RESET}  {rpad(hdr, col_w)}  {C.DIM}│{C.RESET}")
        print(f"  {C.DIM}└{'─' * box_w}┘{C.RESET}")
        return

    bdr        = C.B_GREEN if direction == "CE" else C.B_RED
    conf_col   = C.B_GREEN if confidence == "HIGH" else C.B_YELLOW
    arrow      = "▲" if direction == "CE" else "▼"
    tgt_pts    = option_signal["target_pts"]
    sl_pts     = option_signal["sl_pts"]
    rr         = option_signal["rr_ratio"]
    reason     = option_signal["reason"]
    s_tgt      = option_signal["spot_target"]
    s_sl       = option_signal["spot_sl"]
    t_lbl      = option_signal.get("target_label", "")
    sl_lbl     = option_signal.get("sl_label", "")
    entry_type = option_signal.get("entry_type", "BREAK")

    # delta ≈ 0.40 for slightly-OTM intraday option
    delta        = 0.40
    expiry_short = expiry[5:] if len(expiry) == 10 else expiry   # "2026-06-02" → "06-02"

    if option_info:
        strike = option_info["strike"]
        ltp    = option_info["ltp"]
        trig   = _opt_trigger(ltp)    # nearest ₹5 break-above level
        dip    = _opt_limit(ltp)      # nearest ₹5 dip/limit level

        # Option target & SL — use trigger price as entry basis for BREAK, ltp for NOW
        entry_basis = ltp if entry_type == "NOW" else trig
        o_tgt  = max(1.0, round(entry_basis + tgt_pts * delta, 0))
        o_sl   = max(5.0, round(entry_basis - sl_pts  * delta, 0))
        o_gain = int(o_tgt - entry_basis)
        o_loss = int(entry_basis - o_sl)

        # Line 1 ── strike, live LTP, expiry, confidence
        l1 = (f"  💡 BUY {index_name.upper()} {strike} {direction}"
              f"  │  LTP {bdr}₹{ltp:.0f}{C.RESET}"
              f"  │  Expiry {C.DIM}{expiry_short}{C.RESET}"
              f"  │  Confidence {conf_col}{confidence}{C.RESET}")

        # Line 2 ── reason
        l2 = f"  WHY: {C.DIM}{reason}{C.RESET}"

        # Line 3 ── entry trigger (the key actionable line)
        if entry_type == "NOW":
            l3 = (f"  ENTRY: {bdr}BUY NOW ₹{ltp:.0f}{C.RESET}"
                  f"  OR dip to ₹{dip:.0f}"
                  f"  │  {bdr}Target ₹{o_tgt:.0f}{C.RESET} (+₹{o_gain})"
                  f"  │  SL ₹{o_sl:.0f} (-₹{o_loss})")
        else:
            l3 = (f"  ENTRY: {C.YELLOW}Once {direction} breaks ₹{trig:.0f} → BUY{C.RESET}"
                  f"  OR limit ₹{dip:.0f}"
                  f"  │  {bdr}Target ₹{o_tgt:.0f}{C.RESET} (+₹{o_gain})"
                  f"  │  SL ₹{o_sl:.0f} (-₹{o_loss})")

        # Line 4 ── SPOT levels + R:R
        l4 = (f"  SPOT: {bdr}Target {s_tgt:,.0f}{C.RESET} ({arrow}{tgt_pts:.0f}pts → {t_lbl})"
              f"  │  SL {s_sl:,.0f} ({sl_pts:.0f}pts ← {sl_lbl})"
              f"  │  R:R {C.B_GREEN}{rr:.1f}:1{C.RESET}")

    else:
        # Chain unavailable — show SPOT levels only, no fake option maths
        step       = 200 if index_name.upper() == "SENSEX" else 100 if index_name.upper() == "BANKNIFTY" else 50
        atm_strike = int(round(spot / step) * step)

        l1 = (f"  💡 BUY {index_name.upper()} ~{atm_strike} {direction}"
              f"  │  LTP {C.YELLOW}fetching…{C.RESET}"
              f"  │  Expiry {C.DIM}{expiry_short}{C.RESET}"
              f"  │  Confidence {conf_col}{confidence}{C.RESET}")
        l2 = f"  WHY: {C.DIM}{reason}{C.RESET}"
        l3 = (f"  ENTRY: {C.YELLOW}Wait for LTP — use current option price for trigger{C.RESET}"
              f"  │  Find {direction} near {atm_strike} with ₹100–₹150 premium")
        l4 = (f"  SPOT: {bdr}Target {s_tgt:,.0f}{C.RESET} ({arrow}{tgt_pts:.0f}pts → {t_lbl})"
              f"  │  SL {s_sl:,.0f} ({sl_pts:.0f}pts ← {sl_lbl})"
              f"  │  R:R {C.B_GREEN}{rr:.1f}:1{C.RESET}")

    print(f"  {bdr}┌{'─' * box_w}┐{C.RESET}")
    for ln in (l1, l2, l3, l4):
        print(f"  {bdr}│{C.RESET}  {rpad(ln, col_w)}  {bdr}│{C.RESET}")
    print(f"  {bdr}└{'─' * box_w}┘{C.RESET}")


# ─────────────────────────────────────────────────────────────
#  DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────
def _strength_bar(score: float) -> str:
    filled = min(10, max(0, round(score)))
    bar    = "█" * filled + "░" * (10 - filled)
    if   score >= 8: col = C.B_RED
    elif score >= 6: col = C.B_ORANGE
    elif score >= 4: col = C.B_YELLOW
    else:            col = C.DIM
    return f"{col}{bar}{C.RESET}"


def _tf_badge(tf: str, confluent: bool) -> str:
    colors = {
        "D":   C.B_RED,
        "1H":  C.B_ORANGE,
        "15M": C.B_YELLOW,
        "5M":  C.CYAN,
        "ID":  C.CYAN,
        "ALL": C.B_MAGENTA,
    }
    col   = colors.get(tf, C.WHITE)
    badge = f"{col}[{tf}]{C.RESET}"
    if confluent:
        badge += f"{C.B_YELLOW}★{C.RESET}"
    return badge


def _status_tag(dist_pct: float, score: float) -> str:
    strong = CLA_CONFIG["STRONG_SCORE"]
    near   = CLA_CONFIG["NEAR_LEVEL_PCT"]
    caut   = CLA_CONFIG["CAUTION_LEVEL_PCT"]
    if dist_pct <= near and score >= strong:
        return f"{C.B_RED}⛔ AT LEVEL{C.RESET}"
    if dist_pct <= near:
        return f"{C.ORANGE}⚠  NEAR   {C.RESET}"
    if dist_pct <= caut and score >= CLA_CONFIG["MODERATE_SCORE"]:
        return f"{C.B_YELLOW}👀 WATCH  {C.RESET}"
    return f"{C.DIM}·        {C.RESET}"


def _level_row(lvl: dict, spot: float, is_above: bool) -> str:
    price    = lvl["price"]
    dist_pts = price - spot
    dist_pct = abs(dist_pts) / spot * 100
    arrow    = "▲" if is_above else "▼"

    dist_str = f"{arrow}{dist_pts:+.0f}pts ({dist_pct:.2f}%)"

    label = lvl["label"]
    if lvl["extra_types"]:
        extras = lvl["extra_types"][:2]
        label  = label + " +" + "+".join(e.split("[")[0].strip() for e in extras)

    if   dist_pct <= CLA_CONFIG["NEAR_LEVEL_PCT"] and lvl["strength"] >= CLA_CONFIG["STRONG_SCORE"]:
        price_col = C.B_RED
    elif dist_pct <= CLA_CONFIG["NEAR_LEVEL_PCT"]:
        price_col = C.ORANGE
    elif dist_pct <= CLA_CONFIG["CAUTION_LEVEL_PCT"] and lvl["strength"] >= CLA_CONFIG["MODERATE_SCORE"]:
        price_col = C.B_YELLOW
    else:
        price_col = C.WHITE

    bar       = _strength_bar(lvl["strength"])
    tf_badge  = _tf_badge(lvl["tf"], lvl["confluent"])
    tag       = _status_tag(dist_pct, lvl["strength"])
    score_str = f"{lvl['strength']:.1f}/10"
    eff_str   = f"{lvl['efficiency']:.0f}%"
    touch_str = f"t:{lvl['touches']}"

    row  = f"  {price_col}{price:>10,.2f}{C.RESET}"
    row += f"  {price_col}{rpad(dist_str, 20)}{C.RESET}"
    row += f"  {rpad(label, 22)}"
    row += f"  {rpad(tf_badge, 12)}"
    row += f"  {bar}  {C.WHITE}{score_str:>6}{C.RESET}"
    row += f"  {C.DIM}{eff_str:>4} {touch_str:>4}{C.RESET}"
    row += f"  {tag}"
    return row


def _score_legend() -> str:
    return (
        f"  {C.DIM}Strength bar:  "
        f"{C.B_RED}██{C.RESET}{C.DIM}=Strong(8+)  "
        f"{C.B_ORANGE}██{C.RESET}{C.DIM}=Good(6-7)  "
        f"{C.B_YELLOW}██{C.RESET}{C.DIM}=Moderate(4-5)  "
        f"{C.DIM}░░{C.RESET}{C.DIM}=Weak(<4)  "
        f"★=Confluence{C.RESET}"
    )


def render_dashboard(
    spot: float,
    prev_close: float,
    index_name: str,
    above: list[dict],
    below: list[dict],
    decision: str,
    decision_text: str,
    decision_color: str,
    vwap: Optional[float],
    candle_counts: dict,
    last_refresh: datetime,
    option_signal: Optional[dict] = None,
    option_info: Optional[dict] = None,
    expiry: str = "",
) -> None:
    os.system("clear")
    W = 108

    idx = index_name.upper()
    chg     = spot - prev_close
    chg_pct = chg / prev_close * 100
    chg_col = C.B_GREEN if chg >= 0 else C.B_RED
    chg_str = f"{'+' if chg >= 0 else ''}{chg:,.2f}  ({'+' if chg_pct >= 0 else ''}{chg_pct:.2f}%)"
    mkt_col = C.B_GREEN if is_market_open() else C.YELLOW
    mkt_str = "OPEN" if is_market_open() else "CLOSED"
    ts_str  = last_refresh.strftime("%H:%M:%S")
    dt_str  = last_refresh.strftime("%a %d-%b-%Y")

    border = C.B_CYAN + "═" * W + C.RESET
    sep    = C.DIM   + "  " + "─" * (W - 4) + C.RESET

    print(border)
    hdr = (f"  📊 CHART LEVEL ANALYZER  ─  {C.B_WHITE}{idx}{C.RESET}  │  {dt_str}"
           f"  │  Market: {mkt_col}{mkt_str}{C.RESET}  │  {C.DIM}{ts_str}{C.RESET}")
    print(hdr)
    print(border)
    print()

    # ── Spot + VWAP ────────────────────────────────────────────
    vwap_str = ""
    if vwap:
        vwap_rel = "▲ above VWAP" if spot > vwap else "▼ below VWAP"
        vwap_col = C.GREEN if spot > vwap else C.RED
        vwap_str = f"    VWAP: {C.CYAN}{vwap:,.2f}{C.RESET}  {vwap_col}({vwap_rel}){C.RESET}"
    print(f"  SPOT: {C.B_WHITE}{spot:>10,.2f}{C.RESET}   {chg_col}{chg_str}{C.RESET}{vwap_str}")
    print()

    # ── Column header ──────────────────────────────────────────
    hcol = (f"  {'PRICE':>10}  {'DISTANCE':20}  {'LEVEL':22}  {'TF':10}"
            f"  {'STRENGTH BAR':14}  {'SCORE':>6}  {'EFF  TCH':9}  STATUS")
    print(C.DIM + hcol + C.RESET)
    print(sep)

    # ── Resistance ────────────────────────────────────────────
    disp_above = CLA_CONFIG["DISPLAY_ABOVE"]
    sorted_above = sorted(above, key=lambda x: x["price"])[:disp_above]
    # Show farthest → nearest (so nearest is just above the SPOT line)
    for lvl in reversed(sorted_above):
        print(_level_row(lvl, spot, is_above=True))

    if not sorted_above:
        print(f"  {C.DIM}  (no resistance levels detected within ±{CLA_CONFIG['MAX_DIST_PCT']:.0f}%){C.RESET}")

    # ── Spot marker ───────────────────────────────────────────
    marker = (f"  {C.DIM}{'─' * 10}{C.RESET}  ◄◄◄  "
              f"SPOT {C.B_WHITE}{spot:>10,.2f}{C.RESET}  ◄◄◄  "
              f"{C.DIM}{'─' * 28}{C.RESET}")
    print(marker)

    # ── Support ───────────────────────────────────────────────
    disp_below = CLA_CONFIG["DISPLAY_BELOW"]
    sorted_below = sorted(below, key=lambda x: x["price"], reverse=True)[:disp_below]
    for lvl in sorted_below:
        print(_level_row(lvl, spot, is_above=False))

    if not sorted_below:
        print(f"  {C.DIM}  (no support levels detected within ±{CLA_CONFIG['MAX_DIST_PCT']:.0f}%){C.RESET}")

    print()
    print(sep)

    # ── Trade decision box ────────────────────────────────────
    box_w = W - 6
    print(f"  {decision_color}┌{'─' * box_w}┐{C.RESET}")
    line = f"  TRADE DECISION  │  {decision_text}"
    print(f"  {decision_color}│{C.RESET}  {decision_color}{rpad(line, box_w - 2)}{C.RESET}  {decision_color}│{C.RESET}")
    print(f"  {decision_color}└{'─' * box_w}┘{C.RESET}")

    print()
    print(_score_legend())
    print()
    cmt = (f"  {C.DIM}Candles: 5m={candle_counts.get('5m',0)} "
           f"15m={candle_counts.get('15m',0)} "
           f"1h={candle_counts.get('1h',0)} "
           f"1d={candle_counts.get('1d',0)}"
           f"  │  Refresh {CLA_CONFIG['REFRESH_SEC']}s  │  Ctrl+C to quit  "
           f"│  INDEX={idx}{C.RESET}")
    print(cmt)

    # ── Option suggestion panel ───────────────────────────────
    if option_signal is not None:
        render_option_section(spot, option_signal, option_info, index_name, expiry, W)


# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
_signal_log_path: str = ""


def setup_logger() -> str:
    """Tee all print() output to a dated session log file (ANSI-stripped).
    Also sets _signal_log_path for structured signal events.
    """
    import builtins as _builtins
    global _signal_log_path

    base  = os.path.dirname(os.path.abspath(__file__))
    log_d = os.path.join(base, "logs", "chart_level")
    os.makedirs(log_d, exist_ok=True)

    ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(log_d, f"Chart_Level_{ts}.log")

    _ANSI_STRIP = _re.compile(r'\033\[[0-9;]*[mKHFABCDEFGJRSTihlnpu]')
    lf          = open(path, "a", buffering=1, encoding="utf-8")
    _real       = sys.__stdout__
    _orig_print = _builtins.print

    def _tee_print(*args, sep=" ", end="\n", file=None, flush=False):
        if file is None:
            _orig_print(*args, sep=sep, end=end, file=_real, flush=True)
            text = sep.join(str(a) for a in args) + end
            try:
                lf.write(_ANSI_STRIP.sub("", text))
                lf.flush()
            except Exception:
                pass
        else:
            _orig_print(*args, sep=sep, end=end, file=file, flush=flush)

    _builtins.print = _tee_print

    _signal_log_path = os.path.join(
        log_d, f"signals_{datetime.now().strftime('%Y-%m-%d')}.jsonl")

    print(f"📝 Session log : {path}")
    print(f"📝 Signals log : {_signal_log_path}")
    return path


def _log_signal_event(
    index_name: str,
    spot: float,
    option_signal: dict,
    option_info: Optional[dict],
) -> None:
    """Append one JSON record to the daily signals log when an alarm fires."""
    if not _signal_log_path:
        return
    record = {
        "ts":          datetime.now().isoformat(timespec="seconds"),
        "index":       index_name,
        "spot":        spot,
        "direction":   option_signal.get("direction"),
        "confidence":  option_signal.get("confidence"),
        "reason":      option_signal.get("reason"),
        "entry_type":  option_signal.get("entry_type"),
        "target_pts":  option_signal.get("target_pts"),
        "sl_pts":      option_signal.get("sl_pts"),
        "rr_ratio":    option_signal.get("rr_ratio"),
        "spot_target": option_signal.get("spot_target"),
        "spot_sl":     option_signal.get("spot_sl"),
        "strike":      option_info["strike"] if option_info else None,
        "option_ltp":  option_info["ltp"]    if option_info else None,
    }
    try:
        with open(_signal_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
def main() -> None:
    setup_logger()

    print(f"\n{C.B_CYAN}{'═' * 70}{C.RESET}")
    print(f"{C.B_WHITE}  📊 CHART LEVEL ANALYZER  — Initialising…{C.RESET}")
    print(f"{C.B_CYAN}{'═' * 70}{C.RESET}\n")

    groww, access_token = init_groww()
    idx = CLA_CONFIG["INDEX"]

    candles_5m: list[dict]  = []
    candles_15m: list[dict] = []
    candles_1h: list[dict]  = []
    candles_1d: list[dict]  = []

    prev_close: Optional[float] = None
    alerted_keys: set[str]      = set()   # avoid repeat Telegram alerts
    alarm_keys:   set[str]      = set()   # avoid repeat sound alarms

    cycle = 0

    while True:
        try:
            now = datetime.now()
            cycle += 1

            # ── Fetch candles ─────────────────────────────────
            if cycle == 1 or cycle % 10 == 0:
                print(f"\n{C.CYAN}⟳ Fetching all timeframe candles…{C.RESET}", end="", flush=True)
                candles_5m  = fetch_candles(groww, idx, "5minute",  CLA_CONFIG["LOOKBACK_5M_HRS"])
                candles_15m = fetch_candles(groww, idx, "15minute", CLA_CONFIG["LOOKBACK_15M_HRS"])
                candles_1h  = fetch_candles(groww, idx, "1hour",    CLA_CONFIG["LOOKBACK_1H_HRS"])
                candles_1d  = fetch_candles(groww, idx, "1day",     CLA_CONFIG["LOOKBACK_1D_HRS"])
                print(f"  5m:{len(candles_5m)}  15m:{len(candles_15m)}"
                      f"  1h:{len(candles_1h)}  1d:{len(candles_1d)}")
            elif cycle % 3 == 0:
                # Light refresh: update 5-min candles every 3rd cycle
                fresh5m = fetch_candles(groww, idx, "5minute", 2)
                if fresh5m:
                    candles_5m = fresh5m

            # ── Spot price ────────────────────────────────────
            spot = get_spot_price(idx, access_token)
            if not spot:
                print(f"{C.YELLOW}⚠  Spot price unavailable — retrying in 10s{C.RESET}")
                time.sleep(10)
                continue

            # ── Prev close (for P&L % display) ────────────────
            if prev_close is None:
                pd = prev_day_ohlc(candles_1d)
                prev_close = pd["close"] if pd else spot

            # ── Build + merge levels ──────────────────────────
            raw_levels  = build_all_levels(
                spot, idx, candles_5m, candles_15m, candles_1h, candles_1d)
            all_levels  = merge_confluence_levels(raw_levels, spot)

            above = sorted([l for l in all_levels if l["price"] > spot],
                           key=lambda x: x["price"])
            below = sorted([l for l in all_levels if l["price"] < spot],
                           key=lambda x: x["price"], reverse=True)

            # ── Decision ──────────────────────────────────────
            decision, decision_text, decision_color = generate_trade_decision(
                spot, above, below)

            # ── VWAP ──────────────────────────────────────────
            vwap = calc_vwap(candles_5m if candles_5m else candles_15m)

            # ── Option suggestion ─────────────────────────────
            option_signal = analyze_option_signal(
                spot, above, below, vwap, decision, candles_5m, idx)

            expiry, _ = get_active_expiry(idx)
            # Always fetch chain (cached 5 min) so dashboard gets live LTPs for all strikes
            opt_chain = fetch_option_chain_data(access_token, idx, spot)
            try:
                _chain_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "logs", "chart_level", "live_chain.json")
                with open(_chain_path, "w", encoding="utf-8") as _cf:
                    json.dump({
                        "ts":    now.isoformat(timespec="seconds"),
                        "spot":  spot,
                        "chain": {str(k): v for k, v in opt_chain.items()},
                    }, _cf)
            except Exception:
                pass
            option_info: Optional[dict] = None
            if option_signal["direction"] in ("CE", "PE"):
                option_info = find_best_option(spot, opt_chain, option_signal["direction"])

            # ── Render ────────────────────────────────────────
            render_dashboard(
                spot=spot,
                prev_close=prev_close,
                index_name=idx,
                above=above,
                below=below,
                decision=decision,
                decision_text=decision_text,
                decision_color=decision_color,
                vwap=vwap,
                candle_counts={
                    "5m":  len(candles_5m),
                    "15m": len(candles_15m),
                    "1h":  len(candles_1h),
                    "1d":  len(candles_1d),
                },
                last_refresh=now,
                option_signal=option_signal,
                option_info=option_info,
                expiry=expiry or "",
            )

            # ── Sound alarm for any new CE/PE option signal ───
            if option_signal["direction"] in ("CE", "PE"):
                alarm_key = (f"alarm_{option_signal['direction']}_"
                             f"{int(round(spot / 50) * 50)}")
                if alarm_key not in alarm_keys:
                    _play_alarm()
                    _log_signal_event(idx, spot, option_signal, option_info)
                    print(f"\n{C.B_GREEN}🔔 ALARM fired: {option_signal['direction']} "
                          f"signal @ {spot:,.2f}  [{option_signal['confidence']}]{C.RESET}")
                    alarm_keys.add(alarm_key)

            # Clear alarm keys once price moves far enough away
            near_pct = CLA_CONFIG["NEAR_LEVEL_PCT"]
            for akey in list(alarm_keys):
                try:
                    key_price = float(akey.rsplit("_", 1)[-1])
                    if abs(key_price - spot) / spot * 100 > near_pct * 4:
                        alarm_keys.discard(akey)
                except ValueError:
                    pass

            # ── Telegram: HIGH confidence option signal ───────
            if option_signal["direction"] in ("CE", "PE") and option_signal["confidence"] == "HIGH":
                sig_key = f"opt_{option_signal['direction']}_{int(round(spot / 50) * 50)}"
                if sig_key not in alerted_keys:
                    ot = option_signal["direction"]
                    base_msg = (
                        f"💡 OPTION SIGNAL — {idx}\n"
                        f"Confidence: {option_signal['confidence']}\n"
                        f"Signal: {option_signal['reason']}\n"
                        f"SPOT Target: {option_signal['spot_target']:,.0f}"
                        f"  (+{option_signal['target_pts']:.0f}pts)\n"
                        f"SPOT SL: {option_signal['spot_sl']:,.0f}"
                        f"  (-{option_signal['sl_pts']:.0f}pts)\n"
                        f"R:R {option_signal['rr_ratio']:.1f}:1"
                    )
                    if option_info:
                        opt_msg = (f"💡 OPTION SIGNAL — {idx}\n"
                                   f"BUY {idx} {option_info['strike']} {ot}"
                                   f"  @  ₹{option_info['ltp']:.0f}\n"
                                   + base_msg[base_msg.index("Confidence:"):])
                    else:
                        opt_msg = f"BUY {ot} | " + base_msg
                    send_telegram(opt_msg)
                    alerted_keys.add(sig_key)

            # ── Telegram alerts for strong nearby levels ──────
            near_pct   = CLA_CONFIG["NEAR_LEVEL_PCT"]
            strong_thr = CLA_CONFIG["STRONG_SCORE"]
            for lvl in above[:3] + below[:3]:
                dp = abs(lvl["price"] - spot) / spot * 100
                akey = f"{lvl['label']}_{round(lvl['price'])}"
                if dp <= near_pct and lvl["strength"] >= strong_thr and akey not in alerted_keys:
                    side = "RESISTANCE" if lvl["price"] > spot else "SUPPORT"
                    msg = (
                        f"📊 CHART LEVEL ALERT — {idx}\n"
                        f"{side}: {lvl['label']}  @  {lvl['price']:,.2f}\n"
                        f"Spot: {spot:,.2f}  │  Dist: {lvl['price']-spot:+.0f}pts  ({dp:.2f}%)\n"
                        f"Strength: {lvl['strength']:.1f}/10  │  Efficiency: {lvl['efficiency']:.0f}%  │  Touches: {lvl['touches']}\n"
                        f"→ {'⛔ WAIT — confirm before trading' if lvl['strength'] >= 7 else '🟡 Caution — watch this level'}"
                    )
                    send_telegram(msg)
                    alerted_keys.add(akey)
                    print(f"\n{C.B_YELLOW}📨 Telegram alert sent: {lvl['label']} @ {lvl['price']:,.2f}{C.RESET}")

            # Clear alerts once price moves >2× near_pct away
            for akey in list(alerted_keys):
                try:
                    key_price = float(akey.rsplit("_", 1)[-1])
                    if abs(key_price - spot) / spot * 100 > near_pct * 2:
                        alerted_keys.discard(akey)
                except ValueError:
                    pass

        except KeyboardInterrupt:
            print(f"\n{C.B_YELLOW}📊 Chart Level Analyzer stopped.{C.RESET}\n")
            break
        except Exception as exc:
            print(f"{C.RED}⚠  Unexpected error: {exc}{C.RESET}")

        time.sleep(CLA_CONFIG["REFRESH_SEC"])


if __name__ == "__main__":
    main()
