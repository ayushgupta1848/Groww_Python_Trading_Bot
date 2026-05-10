#!/usr/bin/env python3
"""
FIBONACCI_TREND_ANALYZER.py
============================
Parallel read-only trend/direction analyzer for NIFTY / SENSEX using Fibonacci levels.
Run this in a SEPARATE terminal alongside PROD10FEB_ManualBOT.
It NEVER places orders — purely analysis & Telegram alerts.

THEORY (Quick Reference):
  Fibonacci Retracement: after a strong move, price often pulls back to key levels
    23.6%, 38.2%, 50%, 61.8% (Golden Ratio), 78.6%
  Fibonacci Extension: targets beyond the swing high/low
    127.2%, 161.8% (Golden Ratio), 261.8%
  Golden Zone: 50%-61.8% retrace is the highest-probability reversal zone
  Confluence: where 2+ fib levels from different timeframe swings cluster = STRONGEST zone

HOW TO USE WITH MAIN BOT:
  1. Run this analyzer in terminal A  →  python FIBONACCI_TREND_ANALYZER.py
  2. Run PROD10FEB bot in terminal B  →  python PROD10FEB_ManualBOT_...py
  3. Before entering a manual trade in the main bot, check this dashboard:
     - BULLISH bias + price near support fib level → buy CE
     - BEARISH bias + price near resistance fib level → buy PE
     - NEUTRAL → wait for clarity
"""

from __future__ import annotations
import os
import sys
import time
import csv
import threading
import requests
import numpy as np
import pyotp
from datetime import datetime, timedelta
from collections import deque

try:
    from growwapi import GrowwAPI
except ImportError:
    print("❗ growwapi not found. Install it or add to PYTHONPATH.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
#  ANSI COLORS  (works on macOS / Linux terminal)
# ─────────────────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"


# ─────────────────────────────────────────────────────────────
#  CONFIG  — only INDEX needs editing; expiry is auto-detected
# ─────────────────────────────────────────────────────────────
FIBO_CONFIG = {
    # ── Index ────────────────────────────────────────────────
    "INDEX":  "NIFTY",           # "NIFTY" | "SENSEX" | "BANKNIFTY" | "FINNIFTY"
    # EXPIRY is auto-detected from instrument.csv — no manual update needed

    # ── Swing detection ──────────────────────────────────────
    "SWING_WINDOW_15M": 3,       # Bars each side for 15-min swing detection (lower=more swings)
    "SWING_WINDOW_1H":  5,       # Bars each side for 1-hr  swing detection (higher=only major swings)

    # ── Candle lookback ──────────────────────────────────────
    "LOOKBACK_15M_HRS": 10,      # Hours of 15-min candle history to fetch (covers full trading day)
    "LOOKBACK_1H_HRS":  120,     # Hours of 1-hr  candle history to fetch  (~5 trading days)

    # ── Alert thresholds ─────────────────────────────────────
    "NEAR_LEVEL_PCT":    0.20,   # Telegram alert when price within 0.20% of a key level
    "CONFLUENCE_TOL_PCT":0.30,   # Levels within 0.30% of each other → same confluence zone

    # ── Loop timing ──────────────────────────────────────────
    "REFRESH_SEC": 90,           # Dashboard refresh interval (seconds)

    # ── Telegram ─────────────────────────────────────────────
    "TELEGRAM_ALERTS": True,

    # ── Market hours (IST) ───────────────────────────────────
    "MARKET_OPEN":  "09:15",
    "MARKET_CLOSE": "15:30",
}


# ─────────────────────────────────────────────────────────────
#  CREDENTIALS  — same as main bot (read-only access here)
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


# ─────────────────────────────────────────────────────────────
#  FIBONACCI CONSTANTS
# ─────────────────────────────────────────────────────────────
FIB_RETRACE = [
    (0.236, "R23.6%"),
    (0.382, "R38.2%"),
    (0.500, "R50.0%"),
    (0.618, "R61.8%"),   # ← Golden Ratio — most important
    (0.786, "R78.6%"),
]
FIB_EXTEND = [
    (1.272, "E127.2%"),
    (1.618, "E161.8%"),  # ← Golden Ratio extension
    (2.618, "E261.8%"),
]


# ─────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────
def init_groww():
    totp = pyotp.TOTP(TOTP_SECRET).now()
    access_token = GrowwAPI.get_access_token(api_key=API_KEY, totp=totp)
    client = GrowwAPI(access_token)
    print(f"{C.GREEN}✅ Fibonacci Analyzer: Groww API initialized{C.RESET}")
    return client, access_token


# ─────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    if not FIBO_CONFIG.get("TELEGRAM_ALERTS"):
        return
    try:
        _session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=3,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
#  UTILS
# ─────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    now = datetime.now()
    open_t  = datetime.strptime(FIBO_CONFIG["MARKET_OPEN"],  "%H:%M").replace(
        year=now.year, month=now.month, day=now.day)
    close_t = datetime.strptime(FIBO_CONFIG["MARKET_CLOSE"], "%H:%M").replace(
        year=now.year, month=now.month, day=now.day)
    return open_t <= now <= close_t


# ── Frozen-price detection ────────────────────────────────────
# Tracks the last N spot prices. If they are all within ±5 pts of each other
# across FROZEN_CYCLES consecutive cycles, we treat the market as closed/holiday.
_recent_spots: deque = deque(maxlen=4)
_FROZEN_CYCLES   = 3    # cycles before declaring frozen
_FROZEN_TOLERANCE = 5.0  # points — price must stay within this band


def record_spot(spot: float) -> None:
    _recent_spots.append(spot)


def is_price_frozen() -> bool:
    """True when the last N spot prices are all within ±_FROZEN_TOLERANCE of each other."""
    if len(_recent_spots) < _FROZEN_CYCLES:
        return False
    lo, hi = min(_recent_spots), max(_recent_spots)
    return (hi - lo) <= _FROZEN_TOLERANCE


def calculate_rsi(closes: list, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    arr = np.array(closes, dtype=float)
    deltas = np.diff(arr)
    gain = np.where(deltas > 0, deltas, 0.0)
    loss = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.mean(gain[:period])
    avg_l = np.mean(loss[:period])
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gain[i]) / period
        avg_l = (avg_l * (period - 1) + loss[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 1)


# ─────────────────────────────────────────────────────────────
#  INSTRUMENTS & EXPIRY  (auto-detection, no manual update needed)
# ─────────────────────────────────────────────────────────────
_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instrument.csv")
_instruments_cache: list[dict] = []
_instruments_loaded_at: float = 0.0
_INSTRUMENTS_RELOAD_HOURS = 6  # reload CSV at most once every 6 hours


def _download_instruments() -> bool:
    """Download fresh instrument.csv from Groww (same URL as command generator)."""
    try:
        url = "https://growwapi-assets.groww.in/instruments/instrument.csv"
        print("📥 Downloading fresh instrument.csv from Groww...")
        resp = _session.get(url, timeout=30)
        resp.raise_for_status()
        with open(_CSV_PATH, "wb") as f:
            f.write(resp.content)
        print("✅ instrument.csv updated")
        return True
    except Exception as e:
        print(f"⚠️ instrument.csv download failed: {e}")
        return False


def _load_instruments() -> list[dict]:
    """
    Load instruments from instrument.csv.
    Auto-downloads if the file is missing or older than 1 day
    (same logic as COMMAND_GENERATOR_option_chain.py).
    Caches in memory for up to 6 hours to avoid repeated disk reads.
    """
    global _instruments_cache, _instruments_loaded_at
    age_hours = (time.time() - _instruments_loaded_at) / 3600
    if _instruments_cache and age_hours < _INSTRUMENTS_RELOAD_HOURS:
        return _instruments_cache

    # Auto-download if missing or stale (>1 day old)
    should_download = not os.path.exists(_CSV_PATH)
    if not should_download:
        file_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(_CSV_PATH))
        should_download = file_age > timedelta(days=1)

    if should_download:
        _download_instruments()

    if not os.path.exists(_CSV_PATH):
        print(f"⚠️ instrument.csv still not found — cannot detect expiry")
        return []

    rows: list[dict] = []
    with open(_CSV_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    _instruments_cache = rows
    _instruments_loaded_at = time.time()
    print(f"✅ Loaded {len(rows):,} instruments from CSV")
    return rows


def get_active_expiry(index_name: str) -> tuple[str | None, str | None]:
    """
    Return (current_expiry, next_expiry) for the given index by scanning instrument.csv.
    Mirrors get_expiry_dates() from COMMAND_GENERATOR_option_chain.py exactly.
    Both dates are in YYYY-MM-DD format.
    """
    instruments = _load_instruments()
    expiries: set[str] = set()
    for item in instruments:
        if item.get("underlying_symbol", "").upper() == index_name.upper():
            expiry = item.get("expiry_date", "").strip()
            if expiry:
                expiries.add(expiry)

    today = datetime.now().date()
    future = sorted(
        e for e in expiries
        if datetime.strptime(e, "%Y-%m-%d").date() >= today
    )
    current = future[0] if len(future) >= 1 else None
    nxt     = future[1] if len(future) >= 2 else None
    if current:
        print(f"📅 Auto-detected expiry for {index_name}: current={current}  next={nxt}")
    else:
        print(f"⚠️ No future expiry found for {index_name} in instrument.csv")
    return current, nxt


# ─────────────────────────────────────────────────────────────
#  DATA FETCHING
# ─────────────────────────────────────────────────────────────
def get_spot_price(index_name: str, access_token: str) -> float | None:
    """
    Fetch live spot price for the underlying index via Groww option chain.
    Expiry is auto-resolved from instrument.csv — no hardcoding needed.
    """
    expiry, _ = get_active_expiry(index_name)
    if not expiry:
        print(f"⚠️ Cannot fetch spot: no active expiry found for {index_name}")
        return None
    try:
        exchange = "BSE" if index_name.upper() == "SENSEX" else "NSE"
        url = (
            f"https://api.groww.in/v1/option-chain/exchange/{exchange}"
            f"/underlying/{index_name.upper()}?expiry_date={expiry}"
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0",
        }
        resp = _session.get(url, headers=headers, timeout=6)
        if resp.status_code == 200:
            payload = resp.json().get("payload", {})
            ltp = payload.get("underlying_ltp")
            if ltp:
                return float(ltp)
        print(f"⚠️ Spot fetch HTTP {resp.status_code}")
    except Exception as e:
        print(f"⚠️ Spot fetch error: {e}")
    return None


def fetch_candles(
    groww, index_name: str, interval: str, hours_back: int
) -> list[dict]:
    """
    Fetch historical OHLC candles for the index (CASH segment).

    Symbol format: GrowwAPI library requires 'Exchange-TradingSymbol' (e.g. 'NSE-NIFTY 50').
    Interval:      library accepts '1minute','5minute','15minute','30minute','1hour','1day'.
                   '60minute' is NOT valid — use '1hour'.
    Returns list of dicts: [{ts, open, high, low, close}, ...]
    """
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(hours=hours_back)
    end_str   = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    idx = index_name.upper()
    if idx == "NIFTY":
        exchange = groww.EXCHANGE_NSE
        # GrowwAPI requires "Exchange-TradingSymbol" format for get_historical_candles
        symbols  = ["NSE-NIFTY 50", "NSE-NIFTY", "NSE-Nifty 50"]
    elif idx == "SENSEX":
        exchange = groww.EXCHANGE_BSE
        symbols  = ["BSE-SENSEX", "BSE-S&P BSE SENSEX"]
    elif idx == "BANKNIFTY":
        exchange = groww.EXCHANGE_NSE
        symbols  = ["NSE-NIFTY BANK", "NSE-BANKNIFTY"]
    elif idx == "FINNIFTY":
        exchange = groww.EXCHANGE_NSE
        symbols  = ["NSE-NIFTY FIN SERVICE", "NSE-FINNIFTY"]
    else:
        return []

    for sym in symbols:
        try:
            result = groww.get_historical_candles(
                groww_symbol=sym,
                exchange=exchange,
                segment="CASH",
                start_time=start_str,
                end_time=end_str,
                candle_interval=interval,
            )
            if result and result.get("candles") and len(result["candles"]) >= 5:
                raw = result["candles"]
                candles = [
                    {
                        "ts":    c[0],
                        "open":  float(c[1]),
                        "high":  float(c[2]),
                        "low":   float(c[3]),
                        "close": float(c[4]),
                    }
                    for c in raw
                ]
                print(
                    f"✅ {len(candles)} {interval} candles fetched "
                    f"for {index_name} (symbol='{sym}')"
                )
                return candles
        except Exception as e:
            print(f"⚠️ Candle fetch failed for '{sym}' ({interval}): {e}")

    print(f"⚠️ No CASH candles available for {index_name} — using LTP buffer fallback")
    return []


def build_synthetic_candles(ltp_buffer: deque, candle_minutes: int = 15) -> list[dict]:
    """
    Build synthetic OHLC candles from an LTP poll buffer.
    Used as fallback when CASH historical candles aren't available via API.
    Each synthetic candle = `candle_minutes` worth of LTP samples aggregated.
    """
    if not ltp_buffer:
        return []
    sorted_buf = sorted(ltp_buffer, key=lambda x: x["ts"])
    samples_per_candle = max(1, candle_minutes)  # assuming 1 poll ≈ 1 minute
    candles = []
    for i in range(0, len(sorted_buf), samples_per_candle):
        chunk = sorted_buf[i : i + samples_per_candle]
        prices = [x["price"] for x in chunk]
        candles.append({
            "ts":    chunk[0]["ts"],
            "open":  prices[0],
            "high":  max(prices),
            "low":   min(prices),
            "close": prices[-1],
        })
    return candles


# ─────────────────────────────────────────────────────────────
#  FIBONACCI LEVEL CALCULATION
# ─────────────────────────────────────────────────────────────
def calc_fib_levels(
    swing_low: float, swing_high: float, is_bullish_swing: bool = True
) -> dict | None:
    """
    Calculate all Fibonacci retracement and extension levels from a swing.

    is_bullish_swing=True:  price moved UP (low → high); retracements fall BELOW swing_high.
    is_bullish_swing=False: price moved DOWN (high → low); retracements rise ABOVE swing_low.
    """
    if swing_high <= swing_low:
        return None

    rng = swing_high - swing_low
    levels: dict = {
        "SWING_HIGH": swing_high,
        "SWING_LOW":  swing_low,
        "_range":     rng,
        "_bullish":   is_bullish_swing,
    }

    if is_bullish_swing:
        # Retracements: price pulls back from swing_high
        for ratio, label in FIB_RETRACE:
            levels[label] = round(swing_high - rng * ratio, 2)
        # Extensions: price continues beyond swing_high
        for ratio, label in FIB_EXTEND:
            levels[label] = round(swing_low + rng * ratio, 2)
    else:
        # Bearish swing: retracements bounce up from swing_low
        for ratio, label in FIB_RETRACE:
            levels[label] = round(swing_low + rng * ratio, 2)
        # Extensions: price continues falling below swing_low
        for ratio, label in FIB_EXTEND:
            levels[label] = round(swing_high - rng * ratio, 2)

    return levels


# ─────────────────────────────────────────────────────────────
#  SWING DETECTION
# ─────────────────────────────────────────────────────────────
def detect_swings(candles: list[dict], window: int = 5) -> list[dict]:
    """
    Detect alternating swing highs and lows from OHLC candle list.
    Only keeps valid alternating H/L sequence (no two highs in a row, etc.).

    Algorithm:
      - Swing HIGH: candle[i].high is the maximum High in [i-window … i+window]
        AND candle closes in the upper half of its range (confirms bullish intent)
      - Swing LOW:  candle[i].low  is the minimum Low  in [i-window … i+window]
        AND candle closes in the lower half of its range
    """
    n = len(candles)
    if n < window * 2 + 1:
        return []

    raw: list[dict] = []
    for i in range(window, n - window):
        window_slice = candles[i - window : i + window + 1]
        c = candles[i]

        bar_range = c["high"] - c["low"]
        if bar_range == 0:
            continue

        # Swing HIGH
        if c["high"] == max(b["high"] for b in window_slice):
            raw.append({"type": "HIGH", "price": c["high"], "idx": i})

        # Swing LOW
        elif c["low"] == min(b["low"] for b in window_slice):
            raw.append({"type": "LOW", "price": c["low"], "idx": i})

    # Enforce strict alternation; keep the more extreme point on consecutive same-types
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


def most_recent_swing_pair(swings: list[dict]) -> dict | None:
    """
    Return the most recent swing high + swing low pair for Fibonacci calculation.
    Returns dict with: swing_low, swing_high, is_bullish, description
    """
    if len(swings) < 2:
        return None
    last, prev = swings[-1], swings[-2]

    if last["type"] == "HIGH":
        return {
            "swing_low":  prev["price"],
            "swing_high": last["price"],
            "is_bullish": True,
            "description": f"↑ {prev['price']:.0f} → {last['price']:.0f}",
        }
    else:
        return {
            "swing_low":  last["price"],
            "swing_high": prev["price"],
            "is_bullish": False,
            "description": f"↓ {prev['price']:.0f} → {last['price']:.0f}",
        }


def second_swing_pair(swings: list[dict]) -> dict | None:
    """Return the second-most-recent swing pair (for secondary Fibonacci grid)."""
    if len(swings) < 4:
        return None
    last, prev = swings[-3], swings[-4]
    if last["type"] == "HIGH":
        return {
            "swing_low":  prev["price"],
            "swing_high": last["price"],
            "is_bullish": True,
            "description": f"↑ {prev['price']:.0f} → {last['price']:.0f} (prev)",
        }
    else:
        return {
            "swing_low":  last["price"],
            "swing_high": prev["price"],
            "is_bullish": False,
            "description": f"↓ {prev['price']:.0f} → {last['price']:.0f} (prev)",
        }


# ─────────────────────────────────────────────────────────────
#  CONFLUENCE ZONE DETECTION
# ─────────────────────────────────────────────────────────────
def find_confluence_zones(fib_dicts: list[dict], tol_pct: float = 0.30) -> list[dict]:
    """
    Find price zones where 2+ Fibonacci levels from different swings cluster.
    Confluence strength = number of levels within the zone.
    Only returns zones with count ≥ 2 (single levels are not "confluence").
    """
    all_levels: list[tuple[float, str]] = []
    for fib in fib_dicts:
        for label, price in fib.items():
            if label.startswith("_") or label in ("SWING_HIGH", "SWING_LOW"):
                continue
            if isinstance(price, (int, float)):
                all_levels.append((float(price), label))

    all_levels.sort(key=lambda x: x[0])
    used = [False] * len(all_levels)
    zones: list[dict] = []

    for i, (price, label) in enumerate(all_levels):
        if used[i]:
            continue
        cluster = [(price, label)]
        for j in range(i + 1, len(all_levels)):
            if used[j]:
                continue
            if abs(all_levels[j][0] - price) / price * 100 <= tol_pct:
                cluster.append(all_levels[j])
                used[j] = True
        used[i] = True

        if len(cluster) >= 2:
            avg = sum(p for p, _ in cluster) / len(cluster)
            zones.append({
                "price":     round(avg, 2),
                "count":     len(cluster),
                "labels":    [lb for _, lb in cluster],
                "min_price": min(p for p, _ in cluster),
                "max_price": max(p for p, _ in cluster),
            })

    return sorted(zones, key=lambda x: x["count"], reverse=True)


# ─────────────────────────────────────────────────────────────
#  CANDLESTICK PATTERN DETECTION
# ─────────────────────────────────────────────────────────────
def detect_pattern(candles: list[dict]) -> str:
    """
    Detect basic reversal / continuation patterns from the last 3 candles.
    Returns a human-readable pattern name with emoji, or "NONE".
    """
    if len(candles) < 3:
        return "NONE"

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]  # c3 = latest

    body3  = abs(c3["close"] - c3["open"])
    rng3   = max(c3["high"] - c3["low"], 0.001)
    low3   = min(c3["close"], c3["open"])
    high3  = max(c3["close"], c3["open"])
    wick_l = low3  - c3["low"]
    wick_u = c3["high"] - high3
    body2  = abs(c2["close"] - c2["open"])

    c2_bull = c2["close"] > c2["open"]
    c1_bull = c1["close"] > c1["open"]
    c3_bull = c3["close"] > c3["open"]

    # ── Hammer: long lower wick, small body, after downtrend
    if (wick_l >= 2.0 * max(body3, 0.001) and wick_u <= body3 * 0.6
            and not c2_bull and not c1_bull):
        return "HAMMER 🔨  (bullish reversal)"

    # ── Shooting Star: long upper wick, small body, after uptrend
    if (wick_u >= 2.0 * max(body3, 0.001) and wick_l <= body3 * 0.6
            and c2_bull and c1_bull):
        return "SHOOTING STAR ⭐ (bearish reversal)"

    # ── Bullish Engulfing
    if (c3_bull and not c2_bull
            and c3["open"] <= c2["close"]
            and c3["close"] >= c2["open"]
            and body3 > body2):
        return "BULL ENGULFING 🐂 (strong bullish)"

    # ── Bearish Engulfing
    if (not c3_bull and c2_bull
            and c3["open"] >= c2["close"]
            and c3["close"] <= c2["open"]
            and body3 > body2):
        return "BEAR ENGULFING 🐻 (strong bearish)"

    # ── Doji: open ≈ close (indecision)
    if body3 / rng3 < 0.08:
        return "DOJI ⚖️  (indecision)"

    # ── Strong bull candle
    if c3_bull and body3 / rng3 > 0.70:
        return "STRONG BULL BAR ↑"

    # ── Strong bear candle
    if not c3_bull and body3 / rng3 > 0.70:
        return "STRONG BEAR BAR ↓"

    return "NONE"


# ─────────────────────────────────────────────────────────────
#  PRICE POSITION ANALYSIS
# ─────────────────────────────────────────────────────────────
def analyze_position(spot: float, fib: dict | None) -> tuple:
    """
    Determine where spot sits inside the Fibonacci grid.
    Returns: (position_label, bias_score, nearest_support_tuple, nearest_resistance_tuple)
    bias_score: -3 (very bearish) → 0 (neutral) → +3 (very bullish)
    """
    if not fib:
        return "NO DATA", 0, None, None

    swing_low  = fib["SWING_LOW"]
    swing_high = fib["SWING_HIGH"]
    rng        = fib["_range"]
    is_bull    = fib["_bullish"]

    if rng <= 0:
        return "INVALID SWING", 0, None, None

    # All tradable levels (exclude metadata keys)
    price_entries = sorted(
        [(label, price) for label, price in fib.items()
         if not label.startswith("_")
         and label not in ("SWING_HIGH", "SWING_LOW")
         and isinstance(price, float)],
        key=lambda x: x[1],
    )

    below = [(l, p) for l, p in price_entries if p < spot]
    above = [(l, p) for l, p in price_entries if p > spot]

    nearest_sup = below[-1] if below else ("SWING_LOW", swing_low)
    nearest_res = above[0]  if above else ("SWING_HIGH", swing_high)

    # Retracement % from relevant extreme
    retrace_pct = (
        (swing_high - spot) / rng * 100 if is_bull
        else (spot - swing_low) / rng * 100
    )

    if is_bull:
        if spot > swing_high:
            label, score = f"ABOVE SWING HIGH (+{spot - swing_high:.0f}pts)", 3
        elif retrace_pct < 23.6:
            label, score = f"SHALLOW PULLBACK {retrace_pct:.1f}% (<23.6%)", 2
        elif retrace_pct < 38.2:
            label, score = f"RETRACE @ 23.6-38.2% ({retrace_pct:.1f}%)", 1
        elif retrace_pct < 50.0:
            label, score = f"RETRACE @ 38.2-50% ({retrace_pct:.1f}%)", 0
        elif retrace_pct < 61.8:
            label, score = f"GOLDEN ZONE 50-61.8% ({retrace_pct:.1f}%) ★", -1
        elif retrace_pct < 78.6:
            label, score = f"DEEP RETRACE 61.8-78.6% ({retrace_pct:.1f}%)", -2
        elif retrace_pct <= 100.0:
            label, score = f"EXTREME RETRACE >78.6% ({retrace_pct:.1f}%)", -3
        else:
            label, score = "BROKEN — BELOW SWING LOW", -4
    else:
        # Bearish swing: bounce levels go upward from swing_low
        if spot < swing_low:
            label, score = f"BELOW SWING LOW (-{swing_low - spot:.0f}pts)", -3
        elif retrace_pct < 23.6:
            label, score = f"WEAK BOUNCE {retrace_pct:.1f}% (<23.6%)", -2
        elif retrace_pct < 38.2:
            label, score = f"BOUNCE @ 23.6-38.2% ({retrace_pct:.1f}%)", -1
        elif retrace_pct < 50.0:
            label, score = f"MID BOUNCE 38.2-50% ({retrace_pct:.1f}%)", 0
        elif retrace_pct < 61.8:
            label, score = f"GOLDEN BOUNCE 50-61.8% ({retrace_pct:.1f}%) ★", 1
        elif retrace_pct < 78.6:
            label, score = f"STRONG BOUNCE 61.8-78.6% ({retrace_pct:.1f}%)", 2
        else:
            label, score = f"FULL RETRACE >78.6% ({retrace_pct:.1f}%)", 3

    return label, score, nearest_sup, nearest_res


# ─────────────────────────────────────────────────────────────
#  TREND BIAS
# ─────────────────────────────────────────────────────────────
BIAS_MAP = {
     3: ("⬆⬆ STRONG BULLISH", C.GREEN),
     2: ("⬆  BULLISH",         C.GREEN),
     1: ("↗  MILD BULLISH",    "\033[92m"),
     0: ("→  NEUTRAL",          C.YELLOW),
    -1: ("↘  MILD BEARISH",    "\033[91m"),
    -2: ("⬇  BEARISH",         C.RED),
    -3: ("⬇⬇ STRONG BEARISH",  C.RED),
}

PATTERN_SCORE = {
    "HAMMER 🔨  (bullish reversal)":     +1.0,
    "BULL ENGULFING 🐂 (strong bullish)":+1.5,
    "STRONG BULL BAR ↑":                 +0.5,
    "SHOOTING STAR ⭐ (bearish reversal)":-1.0,
    "BEAR ENGULFING 🐻 (strong bearish)":-1.5,
    "STRONG BEAR BAR ↓":                 -0.5,
    "DOJI ⚖️  (indecision)":              0.0,
    "NONE":                               0.0,
}


def get_final_bias(score_15m: int, score_1h: int, pattern: str) -> tuple:
    """
    Combine multi-timeframe scores and pattern signal into one bias label.
    Weighting: 1-hr (55%) + 15-min (35%) + pattern (10%)
    """
    p_adj = PATTERN_SCORE.get(pattern, 0.0)
    raw   = score_1h * 0.55 + score_15m * 0.35 + p_adj * 0.10
    score = int(max(-3, min(3, round(raw))))
    label, color = BIAS_MAP.get(score, ("→  NEUTRAL", C.YELLOW))
    return score, label, color


# ─────────────────────────────────────────────────────────────
#  NEXT MOVE PREDICTION
# ─────────────────────────────────────────────────────────────
def predict_next_move(spot: float, bias: int, fib: dict | None) -> dict | None:
    """
    Project the most likely next price target based on Fibonacci grid + bias.
    Returns dict: {direction, target, distance, probability, label}
    """
    if not fib:
        return None

    sh = fib["SWING_HIGH"]
    sl = fib["SWING_LOW"]
    is_bull = fib["_bullish"]

    direction = "UP" if bias >= 1 else "DOWN" if bias <= -1 else "RANGE"
    target    = None
    prob      = 0.45

    if direction == "UP":
        if is_bull:
            if spot < sh:
                target, prob = sh, 0.60          # Target: recover to swing high
            else:
                target = fib.get("E127.2%", fib.get("E161.8%", sh))
                prob   = 0.48
        else:
            target = fib.get("R61.8%", fib.get("R50.0%", sh))
            prob   = 0.52

    elif direction == "DOWN":
        if is_bull:
            target = fib.get("R61.8%", fib.get("R78.6%", sl))
            prob   = 0.52
        else:
            if spot > sl:
                target, prob = sl, 0.58          # Target: reach swing low
            else:
                target = fib.get("E127.2%", fib.get("E161.8%", sl))
                prob   = 0.42

    else:  # RANGE
        d_high = abs(sh - spot)
        d_low  = abs(spot - sl)
        target = sh if d_high < d_low else sl
        prob   = 0.45

    if target is None:
        return None

    dist   = abs(target - spot)
    dist_s = f"+{dist:.0f}" if direction == "UP" else f"-{dist:.0f}" if direction == "DOWN" else f"±{dist:.0f}"

    return {
        "direction":   direction,
        "target":      target,
        "distance":    round(dist, 0),
        "probability": prob,
        "label":       dist_s,
    }


# ─────────────────────────────────────────────────────────────
#  ALERT STATE (prevents repeated alerts for the same level)
# ─────────────────────────────────────────────────────────────
_alert_state: dict[str, float] = {}
_ALERT_COOLDOWN_SEC = 300  # 5 minutes between repeated alerts for same level


def _check_level_alerts(spot: float, fib_dicts: list[dict], index: str) -> list[str]:
    alerts = []
    near_pct = FIBO_CONFIG["NEAR_LEVEL_PCT"]

    for fib in fib_dicts:
        for label, price in fib.items():
            if label.startswith("_") or not isinstance(price, float):
                continue
            dist_pct = abs(spot - price) / price * 100
            if dist_pct <= near_pct:
                key = f"{label}_{price:.0f}"
                last = _alert_state.get(key, 0.0)
                if time.time() - last > _ALERT_COOLDOWN_SEC:
                    _alert_state[key] = time.time()
                    direction = "▲ above" if spot > price else "▼ below"
                    alerts.append(
                        f"📍 {index} @ {spot:.0f}  is {dist_pct:.2f}% from  "
                        f"{label} = {price:.0f}  ({direction})"
                    )
    return alerts


# ─────────────────────────────────────────────────────────────
#  FULL ANALYSIS CYCLE
# ─────────────────────────────────────────────────────────────
def run_analysis(groww, access_token: str, ltp_buffer: deque) -> dict | None:
    cfg   = FIBO_CONFIG
    index = cfg["INDEX"].upper()

    # 1. Live spot price
    spot = get_spot_price(index, access_token)
    if spot is None:
        return None
    ltp_buffer.append({"price": spot, "ts": datetime.now()})
    record_spot(spot)
    frozen = is_price_frozen()

    # 2. Candle data — 15-min and 1-hr timeframes
    c15m = fetch_candles(groww, index, "15minute", cfg["LOOKBACK_15M_HRS"])
    c1h  = fetch_candles(groww, index, "1hour",    cfg["LOOKBACK_1H_HRS"])

    # Synthetic fallback when API doesn't serve CASH candles
    if not c15m and len(ltp_buffer) >= 10:
        c15m = build_synthetic_candles(ltp_buffer, candle_minutes=15)
    if not c1h and len(ltp_buffer) >= 5:
        c1h = build_synthetic_candles(ltp_buffer, candle_minutes=60)

    # 3. Swing detection
    sw15m = detect_swings(c15m, window=cfg["SWING_WINDOW_15M"]) if c15m else []
    sw1h  = detect_swings(c1h,  window=cfg["SWING_WINDOW_1H"])  if c1h  else []

    pair15m = most_recent_swing_pair(sw15m)
    pair1h  = most_recent_swing_pair(sw1h)

    pair15m_prev = second_swing_pair(sw15m)
    pair1h_prev  = second_swing_pair(sw1h)

    # 4. Fibonacci levels
    fib15m      = calc_fib_levels(pair15m["swing_low"],      pair15m["swing_high"],      pair15m["is_bullish"])      if pair15m      else None
    fib1h       = calc_fib_levels(pair1h["swing_low"],       pair1h["swing_high"],       pair1h["is_bullish"])       if pair1h       else None
    fib15m_prev = calc_fib_levels(pair15m_prev["swing_low"], pair15m_prev["swing_high"], pair15m_prev["is_bullish"]) if pair15m_prev else None
    fib1h_prev  = calc_fib_levels(pair1h_prev["swing_low"],  pair1h_prev["swing_high"],  pair1h_prev["is_bullish"])  if pair1h_prev  else None

    # 5. Confluence zones (merge all grids)
    all_fibs   = [f for f in [fib15m, fib1h, fib15m_prev, fib1h_prev] if f]
    confluence = find_confluence_zones(all_fibs, tol_pct=cfg["CONFLUENCE_TOL_PCT"])

    # 6. Price position in each timeframe
    pos15m, sc15m, sup15m, res15m = analyze_position(spot, fib15m)
    pos1h,  sc1h,  sup1h,  res1h  = analyze_position(spot, fib1h)

    # 7. Pattern from 15-min candles
    pattern = detect_pattern(c15m) if len(c15m) >= 3 else "NONE"

    # 8. RSI
    rsi = calculate_rsi([c["close"] for c in c15m]) if len(c15m) >= 15 else None

    # 9. RSI signal note
    rsi_note = ""
    if rsi is not None:
        if rsi >= 70:
            rsi_note = "OVERBOUGHT ⚠️"
        elif rsi <= 30:
            rsi_note = "OVERSOLD ⚠️"
        elif rsi >= 55:
            rsi_note = "bullish zone"
        elif rsi <= 45:
            rsi_note = "bearish zone"
        else:
            rsi_note = "neutral"

    # 10. Final bias
    bias_score, bias_label, bias_color = get_final_bias(sc15m, sc1h, pattern)

    # 11. Next move prediction
    prediction = predict_next_move(spot, bias_score, fib15m)

    # 12. Alerts
    # Suppress level alerts on holidays / frozen market to avoid spam
    alerts = [] if frozen else _check_level_alerts(spot, all_fibs, index)

    return {
        "spot":        spot,
        "index":       index,
        "ts":          datetime.now(),
        "fib15m":      fib15m,
        "fib1h":       fib1h,
        "pair15m":     pair15m,
        "pair1h":      pair1h,
        "confluence":  confluence,
        "pos15m":      pos15m,
        "pos1h":       pos1h,
        "score15m":    sc15m,
        "score1h":     sc1h,
        "sup15m":      sup15m,
        "res15m":      res15m,
        "sup1h":       sup1h,
        "res1h":       res1h,
        "pattern":     pattern,
        "rsi":         rsi,
        "rsi_note":    rsi_note,
        "bias_score":  bias_score,
        "bias_label":  bias_label,
        "bias_color":  bias_color,
        "prediction":  prediction,
        "alerts":      alerts,
        "frozen":      frozen,
        "src_15m": f"LIVE ({len(c15m)}c)" if c15m and len(c15m) >= 5 else "LTP-buffer (building…)",
        "src_1h":  f"LIVE ({len(c1h)}c)"  if c1h  and len(c1h)  >= 5 else "LTP-buffer (building…)",
    }


# ─────────────────────────────────────────────────────────────
#  DASHBOARD DISPLAY
# ─────────────────────────────────────────────────────────────
def _level_row(label: str, price: float, spot: float, width: int = 10) -> str:
    dist      = price - spot
    dist_s    = f"{dist:+.0f}"
    dist_pct  = dist / spot * 100
    near      = abs(dist_pct) < 0.5
    color     = C.GREEN if price > spot else C.RED
    if label in ("SWING_HIGH", "SWING_LOW"):
        color = C.CYAN
    # Golden Zone labels get a distinct yellow marker
    golden_tag = f"  {C.YELLOW}🟡 GOLDEN ZONE{C.RESET}" if label in ("R50.0%", "R61.8%") else ""
    near_tag   = f"  {C.YELLOW}◄ NEAR ({dist_pct:+.2f}%){C.RESET}" if near else ""
    return f"  {color}{label:<12}{C.RESET}  {price:>10.2f}  ({dist_s:>6} pts){golden_tag}{near_tag}"


def generate_reading(r: dict) -> list[str]:
    """
    Translate Fibonacci data into plain-English market interpretation.
    Returns list of lines to print in the MARKET READING section.
    """
    lines = []
    spot      = r["spot"]
    fib       = r.get("fib1h") or r.get("fib15m")
    conf      = r.get("confluence", [])
    bias      = r.get("bias_score", 0)
    pos       = r.get("pos1h") or r.get("pos15m", "NO DATA")
    sup       = r.get("sup1h") or r.get("sup15m")
    res       = r.get("res1h") or r.get("res15m")
    pattern   = r.get("pattern", "NONE")
    swing     = r.get("pair1h") or r.get("pair15m")

    if not fib or not swing:
        lines.append("⏳ Collecting data — check back in a few minutes.")
        return lines

    sh = fib["SWING_HIGH"]
    sl = fib["SWING_LOW"]
    rng = fib["_range"]
    is_bull = fib["_bullish"]

    # ── 1. What is the market doing right now ──────────────
    if is_bull:
        lines.append(
            f"📌 NIFTY made a bullish swing from {sl:.0f} → {sh:.0f} "
            f"(range: {rng:.0f} pts). It is now pulling back."
        )
    else:
        lines.append(
            f"📌 NIFTY made a bearish swing from {sh:.0f} → {sl:.0f} "
            f"(range: {rng:.0f} pts). It is now bouncing."
        )
    lines.append(f"   Current position: {pos}")

    # ── 2. Nearest confluence zone ─────────────────────────
    nearby_conf = [z for z in conf if abs(z["price"] - spot) < rng * 0.20]
    if nearby_conf:
        z = nearby_conf[0]
        dist = z["price"] - spot
        side = "above" if dist > 0 else "below"
        lines.append(
            f"⭐ Strong confluence zone at {z['price']:.0f} "
            f"({abs(dist):.0f} pts {side}) — {'★' * z['count']} "
            f"[{', '.join(z['labels'][:3])}]"
        )
        if abs(dist) < 30:
            lines.append(
                f"   ⚠️  Price is RIGHT INSIDE this zone — high probability reaction expected."
            )

    # ── 3. Key levels to watch ─────────────────────────────
    if sup and res:
        lines.append(
            f"🔑 Key levels: Support {sup[0]} @ {sup[1]:.0f}   │   "
            f"Resistance {res[0]} @ {res[1]:.0f}"
        )

    # ── 4. Golden Zone analysis ────────────────────────────
    gz_50  = fib.get("R50.0%")
    gz_618 = fib.get("R61.8%")
    if gz_50 and gz_618:
        gz_hi = max(gz_50, gz_618)
        gz_lo = min(gz_50, gz_618)
        in_zone    = gz_lo <= spot <= gz_hi
        near_zone  = abs(spot - gz_hi) <= 50 or abs(spot - gz_lo) <= 50
        lines.append("")
        lines.append("🟡 GOLDEN ZONE  (50% – 61.8% retracement):")
        lines.append(f"   Range: {gz_lo:.0f} → {gz_hi:.0f}  (width: {gz_hi - gz_lo:.0f} pts)")
        lines.append( "   Theory: Institutional money accumulates/distributes here.")
        lines.append( "   This is the highest-probability reversal zone in Fibonacci.")
        if in_zone:
            lines.append(f"   🔴 PRICE IS INSIDE THE GOLDEN ZONE RIGHT NOW!")
            if is_bull:
                lines.append( "   → Expect strong buying support. Bulls defending this zone.")
                lines.append( "   → A bounce here = high-conviction CE entry opportunity.")
                lines.append( "   → A CLOSE BELOW the zone = trend is weakening, avoid CE.")
            else:
                lines.append( "   → Expect strong selling pressure. Bears defending this zone.")
                lines.append( "   → A rejection here = high-conviction PE entry opportunity.")
                lines.append( "   → A CLOSE ABOVE the zone = bearish pressure ending, avoid PE.")
        elif near_zone:
            dist_to_zone = min(abs(spot - gz_hi), abs(spot - gz_lo))
            lines.append(f"   ⚠️  Price approaching Golden Zone ({dist_to_zone:.0f} pts away). Watch closely.")
        else:
            dist_to_zone = min(abs(spot - gz_hi), abs(spot - gz_lo))
            lines.append(f"   Price is {dist_to_zone:.0f} pts away from the Golden Zone.")

    # ── 5. What happens if support holds vs breaks ─────────
    if is_bull and sup and res:
        # Find the next level above resistance for target
        all_px = sorted(
            [px for lb, px in fib.items()
             if not lb.startswith("_") and isinstance(px, float) and px > res[1]],
        )
        next_target = all_px[0] if all_px else sh

        lines.append(f"   ✅ If holds above {sup[1]:.0f} → expect move to {res[1]:.0f}, then {next_target:.0f}")

        all_px_below = sorted(
            [px for lb, px in fib.items()
             if not lb.startswith("_") and isinstance(px, float) and px < sup[1]],
            reverse=True,
        )
        next_support = all_px_below[0] if all_px_below else sl
        lines.append(f"   ❌ If breaks below {sup[1]:.0f} → next support at {next_support:.0f}")

    elif not is_bull and sup and res:
        all_px_below = sorted(
            [px for lb, px in fib.items()
             if not lb.startswith("_") and isinstance(px, float) and px < sup[1]],
            reverse=True,
        )
        next_support = all_px_below[0] if all_px_below else sl

        lines.append(f"   ✅ If holds above {sup[1]:.0f} → bounce continues to {res[1]:.0f}")
        lines.append(f"   ❌ If breaks below {sup[1]:.0f} → next support at {next_support:.0f}")

    # ── 5. Pattern signal ──────────────────────────────────
    if pattern != "NONE":
        lines.append(f"🕯️  Pattern at current level: {pattern}")

    # ── 6. Trade idea for manual bot ──────────────────────
    lines.append("")
    lines.append("💡 TRADE IDEA (for PROD10FEB manual bot):")
    if bias >= 2:
        lines.append(f"   → BUY CE  │  Bias is BULLISH. Enter near {sup[1]:.0f} support.")
        lines.append(f"     SL below {sl:.0f} (swing low). Target: {sh:.0f} → extensions above.")
    elif bias <= -2:
        lines.append(f"   → BUY PE  │  Bias is BEARISH. Enter near {res[1]:.0f} resistance.")
        lines.append(f"     SL above {sh:.0f} (swing high). Target: {sl:.0f} → extensions below.")
    elif bias == 1:
        lines.append(f"   → WATCH for CE  │  Mild bullish. Wait for price to close above {res[1]:.0f}")
        lines.append(f"     before entering. Don't chase — let level confirm.")
    elif bias == -1:
        lines.append(f"   → WATCH for PE  │  Mild bearish. Wait for price to break below {sup[1]:.0f}")
        lines.append(f"     before entering. Don't chase — let level confirm.")
    else:
        lines.append(f"   → WAIT  │  Neutral zone ({sup[1]:.0f}–{res[1]:.0f}).")
        lines.append(
            f"     Price between R38.2% and R50.0% — market deciding direction."
        )
        lines.append(
            f"     CE trigger: close above {res[1]:.0f}  │  "
            f"PE trigger: break below {sup[1]:.0f}"
        )

    return lines


def print_dashboard(r: dict) -> None:
    clear_cmd = "cls" if os.name == "nt" else "clear"
    os.system(clear_cmd)

    spot  = r["spot"]
    idx   = r["index"]
    ts    = r["ts"].strftime("%Y-%m-%d %H:%M:%S")
    rsi_s = f"{r['rsi']:.1f}  [{r['rsi_note']}]" if r["rsi"] else "calculating…"
    src_15m = r["src_15m"]
    src_1h  = r["src_1h"]
    src_15m_color = C.GREEN if "LIVE" in src_15m else C.YELLOW
    src_1h_color  = C.GREEN if "LIVE" in src_1h  else C.YELLOW

    print(f"{C.CYAN}{'═'*68}{C.RESET}")
    print(f"{C.BOLD}{C.WHITE}  📐 FIBONACCI TREND ANALYZER  │  {idx}  │  {ts}{C.RESET}")
    print(f"{C.CYAN}{'═'*68}{C.RESET}")

    print(f"  {C.BOLD}Spot Price : {C.WHITE}{spot:>10.2f}{C.RESET}")
    print(f"  {C.BOLD}15m data   : {src_15m_color}{src_15m}{C.RESET}")
    print(f"  {C.BOLD}1hr data   : {src_1h_color}{src_1h}{C.RESET}")
    print(f"  {C.BOLD}Trend Bias : {r['bias_color']}{C.BOLD}{r['bias_label']}{C.RESET}")
    print(f"  {C.BOLD}RSI (15m)  : {C.RESET}{rsi_s}")
    print(f"  {C.BOLD}Pattern    : {C.RESET}{r['pattern']}")

    frozen = r.get("frozen", False)
    if frozen:
        market_s = f"{C.YELLOW}⚠️  MARKET CLOSED / HOLIDAY  (price frozen — Telegram alerts suppressed){C.RESET}"
    elif is_market_open():
        market_s = f"{C.GREEN}MARKET OPEN{C.RESET}"
    else:
        market_s = f"{C.DIM}market closed{C.RESET}"
    print(f"  {C.BOLD}Market     : {C.RESET}{market_s}")
    print()

    # ── 15-min Fibonacci Grid ─────────────────────────────
    fib  = r.get("fib15m")
    pair = r.get("pair15m")
    if fib and pair:
        print(f"{C.YELLOW}─── 15-min Fibonacci Grid  [{pair['description']}] ───{C.RESET}")
        entries = sorted(
            [(lb, px) for lb, px in fib.items()
             if not lb.startswith("_") and isinstance(px, float)],
            key=lambda x: x[1], reverse=True,
        )
        for lb, px in entries:
            print(_level_row(lb, px, spot))
        sup = r.get("sup15m")
        res = r.get("res15m")
        if sup and res:
            print(
                f"\n  Support: {C.GREEN}{sup[0]} @ {sup[1]:.0f}{C.RESET}   "
                f"Resistance: {C.RED}{res[0]} @ {res[1]:.0f}{C.RESET}"
            )
        print(f"  15-min position: {C.WHITE}{r['pos15m']}{C.RESET}  (score: {r['score15m']:+d})")
    else:
        print(f"{C.DIM}  15-min data not yet available (building history…){C.RESET}")

    print()

    # ── 1-hr Fibonacci Grid ───────────────────────────────
    fib1h  = r.get("fib1h")
    pair1h = r.get("pair1h")
    if fib1h and pair1h:
        print(f"{C.YELLOW}─── 1-hr Fibonacci Grid  [{pair1h['description']}] ───{C.RESET}")
        entries = sorted(
            [(lb, px) for lb, px in fib1h.items()
             if not lb.startswith("_") and isinstance(px, float)],
            key=lambda x: x[1], reverse=True,
        )
        for lb, px in entries:
            print(_level_row(lb, px, spot))
        sup = r.get("sup1h")
        res = r.get("res1h")
        if sup and res:
            print(
                f"\n  Support: {C.GREEN}{sup[0]} @ {sup[1]:.0f}{C.RESET}   "
                f"Resistance: {C.RED}{res[0]} @ {res[1]:.0f}{C.RESET}"
            )
        print(f"  1-hr position:   {C.WHITE}{r['pos1h']}{C.RESET}  (score: {r['score1h']:+d})")
    else:
        print(f"{C.DIM}  1-hr data not yet available (building history…){C.RESET}")

    print()

    # ── Confluence Zones ──────────────────────────────────
    conf = r.get("confluence", [])
    if conf:
        print(f"{C.YELLOW}─── 🎯 Confluence Zones (strongest first) ───{C.RESET}")
        for z in conf[:6]:
            dist   = z["price"] - spot
            dist_s = f"{dist:+.0f}"
            stars  = "★" * z["count"]
            color  = C.GREEN if z["price"] > spot else C.RED
            zone_w = round(z["max_price"] - z["min_price"], 0)
            lbls   = ", ".join(z["labels"][:3])
            print(
                f"  {color}{stars:<5}{C.RESET}  {z['price']:>10.2f}  "
                f"({dist_s:>6} pts)  [{lbls}]  ±{zone_w:.0f}pts"
            )
    else:
        print(f"{C.DIM}  No multi-level confluence found yet{C.RESET}")

    print()

    # ── Next Move Prediction ──────────────────────────────
    pred = r.get("prediction")
    if pred:
        print(f"{C.YELLOW}─── 🎯 Predicted Next Move ───{C.RESET}")
        d = pred["direction"]
        d_color = C.GREEN if d == "UP" else C.RED if d == "DOWN" else C.YELLOW
        print(
            f"  Direction : {d_color}{C.BOLD}{d}{C.RESET}\n"
            f"  Target    : {pred['target']:.0f}  ({pred['label']} pts)\n"
            f"  Probability: ~{pred['probability']*100:.0f}%\n"
            f"\n  {C.DIM}Interpretation:{C.RESET}"
        )
        if d == "UP":
            print(f"  {C.GREEN}→ Bias is bullish. Look to BUY CE options in main bot.{C.RESET}")
            print(f"  {C.GREEN}  Enter near support levels. SL below swing low.{C.RESET}")
        elif d == "DOWN":
            print(f"  {C.RED}→ Bias is bearish. Look to BUY PE options in main bot.{C.RESET}")
            print(f"  {C.RED}  Enter near resistance levels. SL above swing high.{C.RESET}")
        else:
            print(f"  {C.YELLOW}→ Neutral / range-bound. Wait for breakout direction.{C.RESET}")

    # ── Chart Setup (copy these to TradingView / charting tool) ──
    print()
    print(f"{C.YELLOW}─── 📊 CHART SETUP  (draw these on your chart) ───{C.RESET}")

    fib15m_d  = r.get("fib15m")
    pair15m_d = r.get("pair15m")
    fib1h_d   = r.get("fib1h")
    pair1h_d  = r.get("pair1h")

    def _chart_block(label: str, pair: dict | None, fib: dict | None) -> None:
        if not pair or not fib:
            print(f"  {C.DIM}{label}: data not yet ready{C.RESET}")
            return
        sl   = fib["SWING_LOW"]
        sh   = fib["SWING_HIGH"]
        rng  = fib["_range"]
        bull = fib["_bullish"]
        arrow = "↑" if bull else "↓"
        direction_word = "BULLISH" if bull else "BEARISH"
        print(f"  {C.BOLD}{C.CYAN}{label} Fibonacci ({direction_word} swing){C.RESET}")
        print(f"    Draw fib FROM  : {C.GREEN if bull else C.RED}{sl:.2f}  (swing low){C.RESET}")
        print(f"    Draw fib TO    : {C.RED if bull else C.GREEN}{sh:.2f}  (swing high){C.RESET}")
        print(f"    Swing range    : {rng:.0f} pts  {arrow}")
        print(f"    Key levels to mark on chart:")
        all_entries = sorted(
            [(lb, px) for lb, px in fib.items()
             if not lb.startswith("_") and lb not in ("SWING_HIGH", "SWING_LOW")
             and isinstance(px, float)],
            key=lambda x: x[1], reverse=True,
        )
        for lb, px in all_entries:
            dist     = px - spot
            near_tag = f"  {C.YELLOW}◄ NEAR{C.RESET}" if abs(dist / spot * 100) < 0.5 else ""
            golden   = f"  {C.YELLOW}★ GOLDEN ZONE{C.RESET}" if lb in ("R50.0%", "R61.8%") else ""
            color    = C.GREEN if px > spot else C.RED
            print(f"      {color}{lb:<12}{C.RESET}  {px:>10.2f}{golden}{near_tag}")

    _chart_block("15-min", pair15m_d, fib15m_d)
    print()
    _chart_block("1-hour ", pair1h_d,  fib1h_d)

    # ── Market Reading ───────────────────────────────────
    print()
    print(f"{C.YELLOW}─── 📖 MARKET READING ───{C.RESET}")
    for line in generate_reading(r):
        print(f"  {line}")

    print()
    print(f"{C.CYAN}{'═'*68}{C.RESET}")
    print(
        f"{C.DIM}  📌 Read-only  │  No orders placed  │  "
        f"Refreshing every {FIBO_CONFIG['REFRESH_SEC']}s{C.RESET}"
    )
    print(f"{C.CYAN}{'═'*68}{C.RESET}")


# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
def setup_logger():
    base   = os.path.dirname(os.path.abspath(__file__))
    log_d  = os.path.join(base, "logs")
    os.makedirs(log_d, exist_ok=True)
    ts     = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path   = os.path.join(log_d, f"Fibo_Analyzer_{ts}.log")

    class Tee:
        def __init__(self, *s):
            self.streams = s
        def write(self, d):
            for s in self.streams:
                try:    s.write(d); s.flush()
                except: pass
        def flush(self):
            for s in self.streams:
                try:    s.flush()
                except: pass

    lf = open(path, "a", buffering=1, encoding="utf-8")
    sys.stdout = Tee(sys.stdout, lf)
    sys.stderr = Tee(sys.stderr, lf)
    print(f"📝 Log: {path}")
    return path


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
def main():
    setup_logger()

    print(f"{C.CYAN}")
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║   FIBONACCI TREND ANALYZER  (read-only)      ║")
    print("  ║   Parallel companion to PROD10FEB ManualBOT  ║")
    print("  ╚══════════════════════════════════════════════╝")
    print(f"{C.RESET}")
    print(f"  Index  : {FIBO_CONFIG['INDEX']}")
    cur_exp, nxt_exp = get_active_expiry(FIBO_CONFIG["INDEX"])
    print(f"  Expiry : {cur_exp}  (next: {nxt_exp})  [auto-detected]")
    print(f"  Refresh: every {FIBO_CONFIG['REFRESH_SEC']}s")
    print(f"  Alerts : {'ON' if FIBO_CONFIG['TELEGRAM_ALERTS'] else 'OFF'}")
    print()

    groww, access_token = init_groww()

    ltp_buffer = deque(maxlen=300)  # ~5 hours at 1 poll/min
    prev_bias  = None
    loop_count = 0

    while True:
        try:
            loop_count += 1
            print(f"\n🔄 Analysis cycle #{loop_count}  [{datetime.now().strftime('%H:%M:%S')}]")

            result = run_analysis(groww, access_token, ltp_buffer)

            if result:
                print_dashboard(result)

                # ── Telegram: level proximity alerts ───────
                for alert in result.get("alerts", []):
                    msg = f"📐 FIBO ALERT — {result['index']}\n{alert}\nBias: {result['bias_label']}"
                    send_telegram(msg)
                    print(f"{C.YELLOW}📨 Telegram: {alert}{C.RESET}")

                # ── Telegram: bias direction change ─────────
                cur_bias = result["bias_score"]
                if prev_bias is not None and cur_bias != prev_bias:
                    change = (
                        f"📊 {result['index']} TREND BIAS CHANGED\n"
                        f"  Was : {BIAS_MAP.get(prev_bias, ('?',''))[0]}\n"
                        f"  Now : {result['bias_label']}\n"
                        f"  Spot: {result['spot']:.0f}\n"
                        f"  RSI : {result['rsi'] or 'N/A'}"
                    )
                    send_telegram(change)
                    print(f"{C.YELLOW}📨 Bias change alert sent{C.RESET}")
                prev_bias = cur_bias
            else:
                print(f"{C.RED}⚠️  Analysis returned no result. Will retry in {FIBO_CONFIG['REFRESH_SEC']}s{C.RESET}")

        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}🛑 Fibonacci Analyzer stopped.{C.RESET}")
            break
        except Exception as e:
            import traceback
            print(f"{C.RED}❌ Error in main loop: {e}{C.RESET}")
            traceback.print_exc()

        time.sleep(FIBO_CONFIG["REFRESH_SEC"])


if __name__ == "__main__":
    main()
