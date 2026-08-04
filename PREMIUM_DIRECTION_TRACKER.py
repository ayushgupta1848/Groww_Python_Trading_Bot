#!/usr/bin/env python3
"""
PREMIUM_DIRECTION_TRACKER.py
==============================
Read-only live tracker — watches one CE + one PE strike and prints
whether the premium is going UP, DOWN, or is STABLE on every poll.

Usage:
    python PREMIUM_DIRECTION_TRACKER.py

On startup you will be asked:
    1. Index  → NIFTY or SENSEX
    2. Expiry → Current or Next

The tracker then auto-selects the ATM strike (or nearest strike whose
premiums fall within the configured range in the CONFIG block below)
and streams live direction every REFRESH_SEC seconds.

Sample output:
    [09:32:15]  (23700 CE) STABLE  121.50   |  (23700 PE) DOWN   88.00
    [09:32:18]  (23700 CE) UP      122.25   |  (23700 PE) STABLE  88.00
"""

from __future__ import annotations
import os
import sys
import csv
import time
import math
import wave
import struct
import random
import tempfile
import threading
import subprocess
import requests
import pyotp
from collections import deque
from datetime import datetime, timedelta

try:
    from growwapi import GrowwAPI
except ImportError:
    print("❗ growwapi not found. Install it or add to PYTHONPATH.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
def setup_logger():
    base  = os.path.dirname(os.path.abspath(__file__))
    log_d = os.path.join(base, "logs", "premium_tracker")
    os.makedirs(log_d, exist_ok=True)
    ts    = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path  = os.path.join(log_d, f"Premium_Tracker_{ts}.log")

    import builtins as _builtins, re as _re
    _ANSI_STRIP = _re.compile(r'\033\[[0-9;]*[mKHFABCDEFGJRSTihlnpu]')
    lf = open(path, "a", buffering=1, encoding="utf-8")

    # Do NOT replace sys.stdout — that path causes colorama / library wrappers
    # to intercept stdout and strip ANSI codes, breaking all colors.
    # Instead, hook builtins.print so every print() call writes:
    #   1) full ANSI text straight to sys.__stdout__ (the real TTY fd, always)
    #   2) stripped plain text to the log file
    _real = sys.__stdout__
    _orig_print = _builtins.print

    def _tee_print(*args, sep=' ', end='\n', file=None, flush=False):
        if file is None:
            _orig_print(*args, sep=sep, end=end, file=_real, flush=True)
            text = sep.join(str(a) for a in args) + end
            try:    lf.write(_ANSI_STRIP.sub('', text)); lf.flush()
            except: pass
        else:
            _orig_print(*args, sep=sep, end=end, file=file, flush=flush)

    _builtins.print = _tee_print
    print(f"📝 Log: {path}")
    return path

LOG_FILE_PATH = setup_logger()

# ─────────────────────────────────────────────────────────────
#  CONFIG  ← edit this block to customize behaviour
# ─────────────────────────────────────────────────────────────
CONFIG = {
    # Premium range: only strikes where BOTH CE and PE fall inside
    # [MIN_PREMIUM, MAX_PREMIUM] are considered. Set a wide range to
    # always land on raw ATM.
    "MIN_PREMIUM": 120,       # ₹ — skip strikes cheaper than this
    "MAX_PREMIUM": 380,      # ₹ — skip strikes more expensive than this

    # How many strikes above/below ATM to scan (order: ATM, ATM-1, ATM+1, …)
    "STRIKE_SCAN_RANGE": 8,

    # ₹ change needed between two readings to register as UP or DOWN.
    # Smaller = more sensitive. Larger = fewer direction flips.
    "DIRECTION_THRESHOLD": 0.25,

    # Seconds between LTP polls. Keep ≥ 2 when running alongside main bot to stay under rate limits.
    "REFRESH_SEC": 2,

    "MARKET_OPEN":  "09:15",
    "MARKET_CLOSE": "15:30",

    # ── Fibonacci breakout/breakdown levels ─────────────────────
    # How often to DISPLAY the fib panel (seconds). Uses cached data — can be fast.
    # Set to 5 to see it every 5 seconds, 30 for every 30 seconds, etc.
    "FIB_PRINT_SEC":      10,

    # How often to RECOMPUTE fib levels (fetches candles — keep ≥ 60 to avoid rate limits).
    "FIB_REFRESH_SEC":    30,

    # Hours of 15-min candle history to use for swing detection.
    # 24h = yesterday's full session + today's partial session.
    # 48h risked pulling in 2-day-old swings at stale price levels.
    "FIB_LOOKBACK_HOURS": 24,
    # Bars each side for swing high/low detection (lower = more sensitive).
    "FIB_SWING_WINDOW":   3,

    # ── Fib Mentor voice ────────────────────────────────────────
    # Speak the mentor guidance aloud when the zone changes.
    # Set False to disable voice entirely.
    "FIB_VOICE":          False,
    # macOS say command speech rate (words per minute). 160 = clear & natural.
    "FIB_VOICE_RATE":     160,
}

# ─────────────────────────────────────────────────────────────
#  COLOR CONFIG  ← paste any hex color code you like (#rrggbb)
#
#  Use any web/design tool hex picker, e.g. https://htmlcolorcodes.com
#  Just replace the value with your chosen hex, e.g. "#00ff00"
# ─────────────────────────────────────────────────────────────
COLOR_CONFIG = {
    # ── Live ticker ─────────────────────────────────────────
    "UP":             "#00ff00",   # premium going UP          ← bright green
    "DOWN":           "#ff0000",   # premium going DOWN        ← bright red
    "STABLE":         "#ffff00",   # premium STABLE            ← yellow
    "SPOT":           "#00ffff",   # spot price label          ← cyan
    # ── Zone / Trend ────────────────────────────────────────
    "BULLISH":        "#00ff00",   # bullish zone or trend
    "BEARISH":        "#ff0000",   # bearish zone or trend
    "NEUTRAL":        "#ffff00",   # neutral zone or trend
    # ── Key levels ──────────────────────────────────────────
    "BREAKOUT":       "#00ff00",   # breakout resistance level
    "SUPPORT":        "#ff4444",   # support level
    "TARGET":         "#ffaa00",   # pts annotation (orange)
    # ── Day High / Low distance ──────────────────────────────
    "DAY_H_NEAR":     "#ff0000",   # < 15 pts to Day High  (imminent)   ← red
    "DAY_H_MID":      "#ff8800",   # 15–40 pts to Day High (approaching) ← orange
    "DAY_H_FAR":      "#aaaaaa",   # > 40 pts to Day High  (safe)        ← gray
    "DAY_L_NEAR":     "#00ff00",   # < 15 pts to Day Low   (support near) ← green
    "DAY_L_MID":      "#ffff00",   # 15–40 pts to Day Low  (watch)       ← yellow
    "DAY_L_FAR":      "#aaaaaa",   # > 40 pts to Day Low   (safe)        ← gray
    # ── Score bar ───────────────────────────────────────────
    "SCORE_HIGH":     "#00ff00",   # score 7–10
    "SCORE_MID":      "#ffff00",   # score 5–6
    "SCORE_LOW":      "#ff0000",   # score 0–4
    # ── CE / PE probability bars ────────────────────────────
    "CE_HIGH":        "#00ff00",   # CE% ≥ 60
    "CE_MID":         "#ffff00",   # CE% 45–59
    "CE_LOW":         "#ff4444",   # CE% < 45
    "PE_HIGH":        "#ff0000",   # PE% ≥ 60
    "PE_MID":         "#ffff00",   # PE% 45–59
    "PE_LOW":         "#00cc44",   # PE% < 45  (good — PE is weak)
    # ── Action / Mentor ─────────────────────────────────────
    "ACTION_BULL":    "#00ff00",   # CE action text
    "ACTION_BEAR":    "#ff0000",   # PE action text
    "ACTION_NEUTRAL": "#ffff00",   # neutral action
    "MENTOR_NOTES":   "#dddddd",   # mentor guidance lines     ← light gray
    # ── Flow chart ──────────────────────────────────────────
    "FLOW_BULL":      "#00ff00",   # chart dot/trend > 55% CE
    "FLOW_BEAR":      "#ff0000",   # chart dot/trend < 45% CE
    "FLOW_NEUTRAL":   "#ffff00",   # chart dot/trend 45–55%
    # ── Startup messages ────────────────────────────────────
    "API_OK":         "#00ff00",   # ✅ Groww API initialized
    "INSTRUMENTS_OK": "#00cc44",   # ✅ Loaded N instruments
    "TRACKING_LABEL": "#00ffff",   # 📅 Tracking NIFTY expiry …
    "SPOT_LABEL":     "#ffaa00",   # 📊 Spot: … ATM strike: …
    "FIB_START":      "#888888",   # 🔢 Fibonacci worker started
    "TRACKER_HEADER": "#00ffff",   # ━━━ PREMIUM DIRECTION TRACKER ━━━
    "TRACKING_LINE":  "#ffffff",   # ▶  Tracking (CE) & (PE) — refresh
    "STATUS_DIM":     "#666666",   # Read-only | Threshold | Ctrl-C
}

# ─────────────────────────────────────────────────────────────
#  TEST MODE  ← set True to run without market / API access
#  Generates a realistic random-walk premium stream so you can
#  verify the display and direction logic while market is closed.
# ─────────────────────────────────────────────────────────────
TEST_MODE = False

# Mock settings (only used when TEST_MODE = True)
TEST_INDEX       = "NIFTY"
TEST_STRIKE      = 24700          # simulated ATM strike
TEST_CE_START    = 135.0          # starting CE premium
TEST_PE_START    = 118.0          # starting PE premium
TEST_VOLATILITY  = 0.6            # max ₹ random-walk step per tick


# ─────────────────────────────────────────────────────────────
#  CREDENTIALS
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

_session = requests.Session()

BOT_TOKEN = "8666941668:AAEObDodwWqDwdVJVXy8WvFx_lyreq8p7fI"
CHAT_ID   = "6012308856"

def _send_telegram(msg: str) -> None:
    try:
        _session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=3,
        )
    except Exception:
        pass

# ── Token-bucket rate limiter for Groww Live Data API ──────────────────────
# Budget allocation: main bot uses ~240 req/min, tracker gets ~60 req/min.
# After batching CE+PE into one call the tracker makes ≤ 30 req/min; limiter
# is a safety net capping at 1 req/sec = 60 req/min.
class _RateLimiter:
    def __init__(self, rate: float):
        self._rate   = rate
        self._tokens = rate
        self._last   = time.monotonic()
        self._lock   = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self._rate
        time.sleep(wait)
        with self._lock:
            self._tokens = max(0.0, self._tokens - 1.0)

_live_data_limiter = _RateLimiter(rate=1.0)  # 1 req/sec = 60 req/min from this bot

# ─────────────────────────────────────────────────────────────
#  ANSI COLORS
# ─────────────────────────────────────────────────────────────
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Base colors
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    # Bold+bright combos — maximum visibility
    B_RED    = "\033[1;91m"
    B_GREEN  = "\033[1;92m"
    B_YELLOW = "\033[1;93m"
    B_CYAN   = "\033[1;96m"
    B_WHITE  = "\033[1;97m"
    # 256-color extras
    ORANGE   = "\033[38;5;214m"
    B_ORANGE = "\033[1;38;5;214m"
    LIME     = "\033[38;5;154m"
    B_LIME   = "\033[1;38;5;154m"
    MAGENTA  = "\033[95m"
    B_MAGENTA= "\033[1;95m"
    PINK     = "\033[38;5;213m"


# Maps COLOR_CONFIG name strings → actual ANSI codes
_COLOR_MAP: dict[str, str] = {}
def _build_color_map() -> None:
    _COLOR_MAP.update({
        "BRIGHT_GREEN":   C.B_GREEN,
        "BRIGHT_RED":     C.B_RED,
        "BRIGHT_YELLOW":  C.B_YELLOW,
        "BRIGHT_CYAN":    C.B_CYAN,
        "BRIGHT_WHITE":   C.B_WHITE,
        "BRIGHT_ORANGE":  C.B_ORANGE,
        "BRIGHT_MAGENTA": C.B_MAGENTA,
        "BRIGHT_LIME":    C.B_LIME,
        "GREEN":          C.GREEN,
        "RED":            C.RED,
        "YELLOW":         C.YELLOW,
        "CYAN":           C.CYAN,
        "MAGENTA":        C.MAGENTA,
        "WHITE":          C.WHITE,
        "ORANGE":         C.ORANGE,
        "LIME":           C.LIME,
        "DIM":            C.DIM,
    })
_build_color_map()


def _hex_to_ansi(hex_color: str) -> str:
    """Convert #rrggbb → ANSI 24-bit true-color bold escape code."""
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"\033[1;38;2;{r};{g};{b}m"  # bold + true color
    except Exception:
        return C.WHITE


def _cc(key: str) -> str:
    """Return the ANSI code for a COLOR_CONFIG slot.
    Accepts either a hex string '#rrggbb' or a legacy name like 'BRIGHT_GREEN'.
    """
    val = COLOR_CONFIG.get(key, "#ffffff")
    if val.startswith("#"):
        return _hex_to_ansi(val)
    return _COLOR_MAP.get(val, C.WHITE)


# ─────────────────────────────────────────────────────────────
#  GROWW AUTH
# ─────────────────────────────────────────────────────────────
from groww_token import get_access_token as get_cached_access_token


def init_groww():
    access_token = get_cached_access_token(API_KEY, TOTP_SECRET)
    client = GrowwAPI(access_token)
    return client, access_token


# ─────────────────────────────────────────────────────────────
#  INSTRUMENTS
# ─────────────────────────────────────────────────────────────
_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instrument.csv")


def _download_instruments() -> bool:
    try:
        url = "https://growwapi-assets.groww.in/instruments/instrument.csv"
        print("📥 Downloading latest instrument.csv ...")
        resp = _session.get(url, timeout=30)
        resp.raise_for_status()
        with open(_CSV_PATH, "wb") as f:
            f.write(resp.content)
        print("✅ instrument.csv updated")
        return True
    except Exception as e:
        print(f"⚠️  instrument.csv download failed: {e}")
        return False


def load_instruments() -> list[dict]:
    should_download = not os.path.exists(_CSV_PATH)
    if not should_download:
        age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(_CSV_PATH))
        should_download = age > timedelta(days=1)
    if should_download:
        _download_instruments()
    if not os.path.exists(_CSV_PATH):
        return []
    rows = []
    with open(_CSV_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    print(f"{_cc('INSTRUMENTS_OK')}✅ Loaded {len(rows):,} instruments{C.RESET}")
    return rows


# ─────────────────────────────────────────────────────────────
#  EXPIRY DETECTION
# ─────────────────────────────────────────────────────────────
def get_expiry_dates(instruments: list[dict], index_name: str) -> tuple[str | None, str | None]:
    expiries: set[str] = set()
    for item in instruments:
        if item.get("underlying_symbol", "").upper() == index_name.upper():
            exp = item.get("expiry_date", "").strip()
            if exp:
                expiries.add(exp)
    today = datetime.now().date()
    future = sorted(
        e for e in expiries
        if datetime.strptime(e, "%Y-%m-%d").date() >= today
    )
    current = future[0] if len(future) >= 1 else None
    nxt     = future[1] if len(future) >= 2 else None
    return current, nxt


# ─────────────────────────────────────────────────────────────
#  SPOT PRICE
# ─────────────────────────────────────────────────────────────
def get_spot(index_name: str, expiry: str, access_token: str) -> float | None:
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
    try:
        resp = _session.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            ltp = resp.json().get("payload", {}).get("underlying_ltp")
            if ltp:
                return float(ltp)
    except Exception as e:
        print(f"⚠️  Spot fetch error: {e}")
    return None


# Cached spot so live mode doesn't hammer the option-chain endpoint every 0.5s
_spot_cache: dict = {"price": None, "ts": 0.0}

def get_spot_live(index_name: str, expiry: str, access_token: str,
                  max_age: float = 3.0) -> float | None:
    """Return spot price, re-fetching only when cache is older than max_age seconds."""
    if (time.time() - _spot_cache["ts"]) < max_age and _spot_cache["price"] is not None:
        return _spot_cache["price"]
    price = get_spot(index_name, expiry, access_token)
    if price is not None:
        _spot_cache["price"] = price
        _spot_cache["ts"]    = time.time()
    return _spot_cache["price"]


# ─────────────────────────────────────────────────────────────
#  LTP FOR A SINGLE OPTION
# ─────────────────────────────────────────────────────────────
def get_ltp(instrument: dict, access_token: str) -> float | None:
    trading_symbol = instrument.get("trading_symbol")
    if not trading_symbol:
        return None
    exchange = instrument.get("exchange", "NSE").upper()
    exchange_symbol = f"{exchange}_{trading_symbol}"
    segment = "FNO"
    url = (
        f"https://api.groww.in/v1/live-data/ltp"
        f"?segment={segment}&exchange_symbols={exchange_symbol}"
    )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0",
    }
    _live_data_limiter.acquire()
    try:
        resp = _session.get(url, headers=headers, timeout=5)
        if resp.status_code == 429:
            time.sleep(5)
            return None
        if resp.status_code == 200:
            val = resp.json().get("payload", {}).get(exchange_symbol)
            if val is not None:
                return float(val)
    except Exception:
        pass
    return None


def get_ltp_pair(ce_inst: dict, pe_inst: dict, access_token: str) -> tuple[float | None, float | None]:
    """Fetch CE and PE LTP in a single API call — halves Live Data quota usage."""
    syms: list[str] = []
    for inst in (ce_inst, pe_inst):
        ts = inst.get("trading_symbol")
        ex = inst.get("exchange", "NSE").upper()
        if ts:
            syms.append(f"{ex}_{ts}")
    if len(syms) != 2:
        return None, None
    url = (
        f"https://api.groww.in/v1/live-data/ltp"
        f"?segment=FNO&exchange_symbols={syms[0]}&exchange_symbols={syms[1]}"
    )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0",
    }
    _live_data_limiter.acquire()
    try:
        resp = _session.get(url, headers=headers, timeout=5)
        if resp.status_code == 429:
            time.sleep(5)
            return None, None
        if resp.status_code == 200:
            payload = resp.json().get("payload", {})
            ce_val = payload.get(syms[0])
            pe_val = payload.get(syms[1])
            return (float(ce_val) if ce_val is not None else None,
                    float(pe_val) if pe_val is not None else None)
    except Exception:
        pass
    return None, None


# ─────────────────────────────────────────────────────────────
#  STRIKE SELECTION
# ─────────────────────────────────────────────────────────────
def find_instrument(instruments: list[dict], index_name: str, expiry: str,
                    strike: float, opt_type: str) -> dict | None:
    for item in instruments:
        if (item.get("underlying_symbol", "").upper() == index_name.upper()
                and item.get("expiry_date", "").strip() == expiry
                and item.get("instrument_type", "").upper() == opt_type.upper()
                and abs(float(item.get("strike_price", 0)) - strike) < 0.01):
            return item
    return None


def select_strike(instruments: list[dict], index_name: str, expiry: str,
                  spot: float, access_token: str) -> tuple[float, dict | None, dict | None]:
    """
    Returns (chosen_strike, ce_instrument, pe_instrument).
    Scans ATM ± STRIKE_SCAN_RANGE and picks the first strike where
    both CE and PE premiums land inside [MIN_PREMIUM, MAX_PREMIUM].
    Falls back to raw ATM if nothing qualifies.
    """
    step  = 100 if index_name.upper() == "SENSEX" else 50
    atm   = round(spot / step) * step
    scan  = CONFIG["STRIKE_SCAN_RANGE"]
    lo    = CONFIG["MIN_PREMIUM"]
    hi    = CONFIG["MAX_PREMIUM"]

    # Build search order: ATM, ATM-step, ATM+step, ATM-2*step, ...
    offsets = [0]
    for i in range(1, scan + 1):
        offsets += [-i, i]

    print(f"\n🔍 Scanning strikes around ATM {atm} for premiums ₹{lo}–₹{hi} ...")

    for off in offsets:
        strike = atm + off * step
        ce_inst = find_instrument(instruments, index_name, expiry, strike, "CE")
        pe_inst = find_instrument(instruments, index_name, expiry, strike, "PE")
        if not ce_inst or not pe_inst:
            continue
        ce_ltp = get_ltp(ce_inst, access_token)
        pe_ltp = get_ltp(pe_inst, access_token)
        if ce_ltp is None or pe_ltp is None:
            continue

        ce_ok = lo <= ce_ltp <= hi
        pe_ok = lo <= pe_ltp <= hi

        marker = "✅" if (ce_ok and pe_ok) else "  "
        print(f"  {marker} {int(strike):>6}  CE ₹{ce_ltp:>7.2f}  PE ₹{pe_ltp:>7.2f}")

        if ce_ok and pe_ok:
            return strike, ce_inst, pe_inst

    print(f"⚠️  No strike found within premium range — defaulting to ATM {atm}")
    ce_inst = find_instrument(instruments, index_name, expiry, atm, "CE")
    pe_inst = find_instrument(instruments, index_name, expiry, atm, "PE")
    return atm, ce_inst, pe_inst


# ─────────────────────────────────────────────────────────────
#  MOCK PRICES  (TEST_MODE only)
# ─────────────────────────────────────────────────────────────
class _MockPrice:
    """Stateful random-walk premium generator."""
    def __init__(self, start: float, volatility: float):
        self._price = start
        self._vol   = volatility

    def next(self) -> float:
        step = random.uniform(-self._vol, self._vol)
        self._price = max(0.5, round(self._price + step, 2))
        return self._price


class _MockSpot:
    """Stateful random-walk index spot price generator."""
    def __init__(self, start: float, volatility: float = 3.0):
        self._price = start
        self._vol   = volatility

    def next(self) -> float:
        step = random.uniform(-self._vol, self._vol)
        self._price = max(1.0, round(self._price + step, 1))
        return self._price


# ─────────────────────────────────────────────────────────────
#  FIBONACCI MATH  (pure calculation — no API calls)
# ─────────────────────────────────────────────────────────────
_FIB_RETRACE = [(0.236,"R23.6%"),(0.382,"R38.2%"),(0.500,"R50.0%"),(0.618,"R61.8%"),(0.786,"R78.6%")]
_FIB_EXTEND  = [(1.272,"E127.2%"),(1.618,"E161.8%")]

def _calc_fib(low: float, high: float, bullish: bool = True) -> dict | None:
    if high <= low:
        return None
    rng = high - low
    d = {"SWING_HIGH": high, "SWING_LOW": low, "_range": rng, "_bullish": bullish}
    if bullish:
        for r, lb in _FIB_RETRACE: d[lb] = round(high - rng * r, 2)
        for r, lb in _FIB_EXTEND:  d[lb] = round(low  + rng * r, 2)
    else:
        for r, lb in _FIB_RETRACE: d[lb] = round(low  + rng * r, 2)
        for r, lb in _FIB_EXTEND:  d[lb] = round(high - rng * r, 2)
    return d

def _detect_swings(candles: list[dict], window: int = 3) -> list[dict]:
    n = len(candles)
    if n < window * 2 + 1:
        return []
    raw = []
    for i in range(window, n - window):
        sl = candles[i - window: i + window + 1]
        c  = candles[i]
        if c["high"] == max(b["high"] for b in sl):
            raw.append({"type": "HIGH", "price": c["high"]})
        elif c["low"] == min(b["low"] for b in sl):
            raw.append({"type": "LOW",  "price": c["low"]})
    val: list[dict] = []
    for s in raw:
        if not val:
            val.append(s)
        elif val[-1]["type"] == s["type"]:
            if s["type"] == "HIGH" and s["price"] > val[-1]["price"]: val[-1] = s
            elif s["type"] == "LOW"  and s["price"] < val[-1]["price"]: val[-1] = s
        else:
            val.append(s)
    return val

def _swing_pair(swings: list[dict], spot: float | None = None) -> dict | None:
    if len(swings) < 2:
        return None

    def _make_pair(last: dict, prev: dict) -> dict:
        if last["type"] == "HIGH":
            return {"low": prev["price"], "high": last["price"], "bullish": True,
                    "desc": f"↑ {prev['price']:.0f}→{last['price']:.0f}"}
        return {"low": last["price"], "high": prev["price"], "bullish": False,
                "desc": f"↓ {prev['price']:.0f}→{last['price']:.0f}"}

    if spot is not None:
        # Pass 1: spot strictly inside the swing range (most recent first)
        for i in range(len(swings) - 1, 0, -1):
            last, prev = swings[i], swings[i - 1]
            lo = min(last["price"], prev["price"])
            hi = max(last["price"], prev["price"])
            if lo <= spot <= hi:
                return _make_pair(last, prev)

        # Pass 2: spot within 0.5× range outside — valid breakdown/breakout context
        for i in range(len(swings) - 1, 0, -1):
            last, prev = swings[i], swings[i - 1]
            lo = min(last["price"], prev["price"])
            hi = max(last["price"], prev["price"])
            rng = hi - lo
            if rng > 0 and lo - 0.5 * rng <= spot <= hi + 0.5 * rng:
                return _make_pair(last, prev)

        # Pass 3: no nearby pair found — pick the one whose range is closest to spot
        # (normalized by range size so small nearby swings beat large distant ones)
        best_i, best_dist = 1, float("inf")
        for i in range(len(swings) - 1, 0, -1):
            last, prev = swings[i], swings[i - 1]
            lo = min(last["price"], prev["price"])
            hi = max(last["price"], prev["price"])
            rng = hi - lo
            if rng == 0:
                continue
            dist = max(0.0, lo - spot, spot - hi) / rng
            if dist < best_dist:
                best_dist = dist
                best_i = i
        return _make_pair(swings[best_i], swings[best_i - 1])

    # No spot provided — use most recent pair
    return _make_pair(swings[-1], swings[-2])

def _nearest_levels(spot: float, fib: dict) -> tuple:
    levels = sorted(
        [(lb, p) for lb, p in fib.items()
         if not lb.startswith("_") and lb not in ("SWING_HIGH","SWING_LOW") and isinstance(p, float)],
        key=lambda x: x[1]
    )
    below = [(lb, p) for lb, p in levels if p < spot]
    above = [(lb, p) for lb, p in levels if p > spot]
    sup = below[-1] if below else ("SWING_LOW",  fib["SWING_LOW"])
    res = above[0]  if above else ("SWING_HIGH", fib["SWING_HIGH"])
    return sup[0], sup[1], res[0], res[1]

def _fib_signal(spot: float, fib: dict) -> dict:
    """Return a rich mentor dict describing the Fibonacci position of spot."""
    is_bull = fib["_bullish"]
    sh      = fib["SWING_HIGH"]
    sl      = fib["SWING_LOW"]
    rng     = fib["_range"]
    # Pre-compute all levels (never None after _calc_fib)
    r236 = fib.get("R23.6%", sh - rng * 0.236 if is_bull else sl + rng * 0.236)
    r382 = fib.get("R38.2%", sh - rng * 0.382 if is_bull else sl + rng * 0.382)
    r500 = fib.get("R50.0%", (sh + sl) / 2)
    r618 = fib.get("R61.8%", sh - rng * 0.618 if is_bull else sl + rng * 0.618)
    r786 = fib.get("R78.6%", sh - rng * 0.786 if is_bull else sl + rng * 0.786)
    e1272 = fib.get("E127.2%")
    sup_lb, sup_px, res_lb, res_px = _nearest_levels(spot, fib)

    if is_bull:
        if spot >= sh:
            zone="BREAKOUT — above swing high"; ce=85; pe=15
            trend="⬆ STRONG BULLISH — breakout confirmed"
            action="STRONG CE — ride the breakout"
            tgt=e1272 or round(sh + rng * 0.272, 1)
            stp=sh; stp_lbl=f"SWING HIGH ({sh:.0f})"
            lines=[f"✅ Breakout confirmed above swing high {sh:.0f}.",
                   f"   Momentum is strongly bullish — can extend to E127.2%.",
                   f"   Target: {tgt:.0f}  |  Hold CE; stop if falls back below {sh:.0f}."]
        elif spot >= r236:
            zone=f"SHALLOW PULLBACK — above R23.6% ({r236:.0f})"; ce=70; pe=30
            trend="⬆ BULLISH — minor retracement, uptrend intact"
            action="STAY IN CE — shallow pullback, trend still up"
            tgt=sh; stp=r382; stp_lbl=f"R38.2% ({r382:.0f})"
            lines=[f"✅ Only a shallow pullback — uptrend is strong.",
                   f"   Price holding above R23.6% ({r236:.0f}).",
                   f"   Stay in CE.  Target: swing high {sh:.0f}.",
                   f"   Exit CE if it closes below R38.2% ({r382:.0f})."]
        elif spot >= r382:
            zone=f"NORMAL PULLBACK — R23.6% to R38.2%"; ce=60; pe=40
            trend="⬆ BULLISH — normal retracement zone"
            action="LEAN CE — watch R38.2% for bounce"
            tgt=sh; stp=r500; stp_lbl=f"R50.0% ({r500:.0f})"
            lines=[f"⚠️  Normal retracement. R38.2% ({r382:.0f}) is key support.",
                   f"   CE entry valid on a bounce here.  Target: {sh:.0f}.",
                   f"   Caution if it breaks below R50% ({r500:.0f})."]
        elif spot >= r500:
            zone=f"DEEP PULLBACK — R38.2% to R50.0% (midpoint)"; ce=52; pe=48
            trend="⬆⬇ NEUTRAL — midpoint battle, no clear edge"
            action="WAIT — no edge at midpoint, watch next move"
            tgt=sh; stp=r618; stp_lbl=f"R61.8% ({r618:.0f})"
            lines=[f"⚠️  At midpoint R50% ({r500:.0f}) — bulls vs bears even.",
                   f"   Wait for a clear candle direction before entering.",
                   f"   CE if bounces from here; PE if drops to R61.8% ({r618:.0f})."]
        elif spot >= r618:
            zone=f"GOLDEN ZONE — R50.0% to R61.8% (critical support)"; ce=42; pe=58
            trend="⬆ WEAKENING — deep pullback into golden zone"
            action="WATCH CE — golden zone bounce possible, high risk"
            tgt=r382; stp=r786; stp_lbl=f"R78.6% ({r786:.0f})"
            lines=[f"⚠️  Deep pullback into GOLDEN ZONE (50–61.8%).",
                   f"   R61.8% ({r618:.0f}) is the LAST key support for bulls.",
                   f"   CE only on a confirmed bounce candle from {r618:.0f}.",
                   f"   Exit CE fully if price breaks R78.6% ({r786:.0f})."]
        elif spot >= r786:
            zone=f"DANGER ZONE — R61.8% to R78.6% (trend failing)"; ce=28; pe=72
            trend="⬇ BEARISH — golden zone broken, uptrend failing"
            action="LEAN PE — uptrend likely reversing"
            tgt=sl; stp=r618; stp_lbl=f"R61.8% ({r618:.0f})"
            lines=[f"🚨 Below golden zone! Uptrend is in serious danger.",
                   f"   PE preferred here.  Target: swing low {sl:.0f}.",
                   f"   CE only if price fully reclaims R61.8% ({r618:.0f})."]
        elif spot > sl:
            zone=f"NEAR BREAKDOWN — below R78.6% ({r786:.0f})"; ce=15; pe=85
            trend="⬇ STRONGLY BEARISH — breakdown imminent"
            action="STRONG PE — breakdown very likely"
            tgt=sl; stp=r786; stp_lbl=f"R78.6% ({r786:.0f})"
            lines=[f"🚨 CRITICAL — below R78.6%, near swing low {sl:.0f}.",
                   f"   Very high probability of breakdown. Stay in PE.",
                   f"   Avoid CE. Re-enter CE only above R61.8% ({r618:.0f})."]
        else:
            # Price broke below the swing low — bullish swing is fully negated.
            # Extension targets for the reversed (bearish) move, measured from sh.
            _ext_dns = [
                (round(sh - rng * 1.272, 1), "E127.2%"),
                (round(sh - rng * 1.618, 1), "E161.8%"),
                (round(sh - rng * 2.618, 1), "E261.8%"),
                (round(sh - rng * 4.236, 1), "E423.6%"),
                (round(sh - rng * 6.854, 1), "E685.4%"),
            ]
            # Pick the first extension that price hasn't reached yet (going down)
            _unmet = [(p, lb) for p, lb in _ext_dns if p < spot]
            tgt, tgt_lbl = _unmet[0] if _unmet else _ext_dns[-1]
            zone="BREAKDOWN CONFIRMED — below swing low"; ce=10; pe=90
            trend="⬇ STRONGLY BEARISH — bullish swing fully reversed"
            action="STRONG PE — breakdown confirmed below swing low"
            stp=sl; stp_lbl=f"SWING LOW ({sl:.0f})"
            lines=[f"🚨 BREAKDOWN CONFIRMED below swing low {sl:.0f}!",
                   f"   The bullish swing {sl:.0f}→{sh:.0f} is fully negated.",
                   f"   Next target: {tgt:.0f}  ({tgt_lbl} — {tgt - spot:+.0f} pts).",
                   f"   Hold PE. Trail stop above {sl:.0f} (now becomes resistance).",
                   f"   Avoid CE until spot reclaims R61.8% ({r618:.0f})."]
    else:
        # Bearish swing — levels go from sl upward
        if spot >= sh:
            # Price exceeded the swing high — the entire bearish swing is reversed.
            # Extension targets for the reversed (bullish) move, measured from sl.
            _ext_ups = [
                (round(sl + rng * 1.272, 1), "E127.2%"),
                (round(sl + rng * 1.618, 1), "E161.8%"),
                (round(sl + rng * 2.618, 1), "E261.8%"),
                (round(sl + rng * 4.236, 1), "E423.6%"),
                (round(sl + rng * 6.854, 1), "E685.4%"),
            ]
            # Pick the first extension that price hasn't reached yet
            _unmet = [(p, lb) for p, lb in _ext_ups if p > spot]
            tgt, tgt_lbl = _unmet[0] if _unmet else _ext_ups[-1]
            zone="BREAKOUT CONFIRMED — above swing high"; ce=90; pe=10
            trend="⬆ STRONGLY BULLISH — bearish swing fully reversed"
            action="STRONG CE — breakout confirmed above swing high"
            stp=sh; stp_lbl=f"SWING HIGH ({sh:.0f})"
            lines=[f"✅ BREAKOUT CONFIRMED above swing high {sh:.0f}!",
                   f"   The bearish swing {sh:.0f}→{sl:.0f} is fully negated.",
                   f"   Next target: {tgt:.0f}  ({tgt_lbl} — {tgt - spot:+.0f} pts).",
                   f"   Hold CE. Trail stop below {sh:.0f} (now becomes support).",
                   f"   Avoid PE until spot drops back below R61.8% ({r618:.0f})."]
        elif spot <= sl:
            zone="BREAKDOWN — below swing low"; ce=15; pe=85
            trend="⬇ STRONG BEARISH — breakdown confirmed"
            action="STRONG PE — ride the breakdown"
            tgt=e1272 or round(sl - rng * 0.272, 1)
            stp=sl; stp_lbl=f"SWING LOW ({sl:.0f})"
            lines=[f"🚨 Breakdown confirmed below swing low {sl:.0f}.",
                   f"   Momentum is strongly bearish — can extend to E127.2%.",
                   f"   Target: {tgt:.0f}  |  Hold PE; stop if reclaims {sl:.0f}."]
        elif spot <= r236:
            zone=f"SHALLOW BOUNCE — below R23.6% ({r236:.0f})"; ce=30; pe=70
            trend="⬇ BEARISH — minor bounce, downtrend intact"
            action="STAY IN PE — shallow bounce, trend still down"
            tgt=sl; stp=r382; stp_lbl=f"R38.2% ({r382:.0f})"
            lines=[f"✅ Only a shallow bounce — downtrend is strong.",
                   f"   Price below R23.6% ({r236:.0f}) resistance.",
                   f"   Stay in PE.  Target: swing low {sl:.0f}.",
                   f"   Exit PE if it breaks above R38.2% ({r382:.0f})."]
        elif spot <= r382:
            zone=f"NORMAL BOUNCE — R23.6% to R38.2%"; ce=40; pe=60
            trend="⬇ BEARISH — normal bounce in downtrend"
            action="LEAN PE — watch R38.2% for rejection"
            tgt=sl; stp=r500; stp_lbl=f"R50.0% ({r500:.0f})"
            lines=[f"⚠️  Normal bounce. R38.2% ({r382:.0f}) is key resistance.",
                   f"   PE entry valid on rejection here.  Target: {sl:.0f}.",
                   f"   Caution if it breaks above R50% ({r500:.0f})."]
        elif spot <= r500:
            zone=f"DEEP BOUNCE — R38.2% to R50.0% (midpoint)"; ce=48; pe=52
            trend="⬆⬇ NEUTRAL — midpoint battle, no clear edge"
            action="WAIT — no edge at midpoint, watch next move"
            tgt=sl; stp=r618; stp_lbl=f"R61.8% ({r618:.0f})"
            lines=[f"⚠️  At midpoint R50% ({r500:.0f}) — bears vs bulls even.",
                   f"   Wait for a clear candle direction before entering.",
                   f"   PE if rejected from here; CE if breaks R61.8% ({r618:.0f})."]
        elif spot <= r618:
            zone=f"GOLDEN ZONE — R50.0% to R61.8% (critical resistance)"; ce=55; pe=45
            trend="⬇ WEAKENING — deep bounce into golden zone"
            action="WATCH PE — golden zone rejection possible, high risk"
            tgt=r382; stp=r786; stp_lbl=f"R78.6% ({r786:.0f})"
            lines=[f"⚠️  Deep bounce into GOLDEN ZONE (50–61.8%).",
                   f"   R61.8% ({r618:.0f}) is the LAST key resistance for bears.",
                   f"   PE only on a confirmed rejection candle from {r618:.0f}.",
                   f"   Exit PE fully if price breaks R78.6% ({r786:.0f})."]
        elif spot <= r786:
            zone=f"DANGER ZONE — R61.8% to R78.6% (trend reversing)"; ce=72; pe=28
            trend="⬆ BULLISH — golden zone broken, downtrend reversing"
            action="LEAN CE — bear trend likely reversing"
            tgt=sh; stp=r618; stp_lbl=f"R61.8% ({r618:.0f})"
            lines=[f"🚨 Above golden zone! Downtrend is in serious danger.",
                   f"   CE preferred here.  Target: swing high {sh:.0f}.",
                   f"   PE only if it drops back below R61.8% ({r618:.0f})."]
        else:
            zone=f"NEAR BREAKOUT — above R78.6% ({r786:.0f})"; ce=85; pe=15
            trend="⬆ STRONGLY BULLISH — breakout imminent"
            action="STRONG CE — breakout very likely"
            tgt=sh; stp=r786; stp_lbl=f"R78.6% ({r786:.0f})"
            lines=[f"🚨 CRITICAL — above R78.6%, near swing high {sh:.0f}.",
                   f"   Very high probability of breakout. Stay in CE.",
                   f"   Avoid PE. Re-enter PE only below R61.8% ({r618:.0f})."]

    return {
        "zone":       zone,
        "trend":      trend,
        "ce_prob":    ce,
        "pe_prob":    pe,
        "action":     action,
        "target":     tgt,
        "stop_price": stp,
        "stop_label": stp_lbl,
        "mentor":     lines,
        "sup_label":  sup_lb,
        "sup_price":  sup_px,
        "res_label":  res_lb,
        "res_price":  res_px,
    }


# ─────────────────────────────────────────────────────────────
#  FIBONACCI STATE  (updated by background thread)
# ─────────────────────────────────────────────────────────────
_fib_lock      = threading.Lock()
_last_fib_zone = ""          # tracks last spoken zone to avoid repeating
_prob_history:  deque = deque(maxlen=40)   # ce_prob ratio history (for chart)
_spot_history:  deque = deque(maxlen=40)   # spot sampled each fib refresh (~30s each → 20 min)


def _calc_momentum() -> tuple[str, float, float]:
    """
    Linear-regression slope over the rolling spot history.
    Returns (direction, pts_per_min, minutes_tracked).
      direction: "UP" | "DOWN" | "FLAT"
    Threshold: <3 pts/min treated as FLAT (normal noise).
    """
    spots = list(_spot_history)
    n = len(spots)
    if n < 4:                          # need at least ~2 min of samples
        return "FLAT", 0.0, 0.0

    xs     = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(spots) / n
    numer  = sum((xs[i] - x_mean) * (spots[i] - y_mean) for i in range(n))
    denom  = sum((xs[i] - x_mean) ** 2 for i in range(n))
    if denom == 0:
        return "FLAT", 0.0, 0.0

    slope_per_sample = numer / denom                        # pts per sample
    refresh_sec      = CONFIG.get("FIB_REFRESH_SEC", 30)
    pts_per_min      = slope_per_sample * (60 / refresh_sec)
    minutes          = round(n * refresh_sec / 60, 1)

    if abs(pts_per_min) < 3.0:
        return "FLAT", pts_per_min, minutes
    return ("UP" if pts_per_min > 0 else "DOWN"), pts_per_min, minutes


def _calc_divergence() -> tuple[str, str]:
    """
    Detect CE/PE divergence vs spot movement over recent ticks.
    Returns (signal, description): signal = "BULLISH" | "BEARISH" | "NEUTRAL"
    """
    ticks = [(t[0], t[1], t[2]) for t in _tick_history
             if t[0] is not None and t[1] is not None and t[2] is not None]
    n = len(ticks)
    if n < 6:
        return "NEUTRAL", ""
    third = max(2, n // 3)
    early  = ticks[:third]
    recent = ticks[-third:]
    spot_chg = sum(t[0] for t in recent) / third - sum(t[0] for t in early) / third
    ce_chg   = sum(t[1] for t in recent) / third - sum(t[1] for t in early) / third
    pe_chg   = sum(t[2] for t in recent) / third - sum(t[2] for t in early) / third
    SPOT_T, PREM_T = 3.0, 0.60
    if abs(spot_chg) < SPOT_T:
        return "NEUTRAL", ""
    if spot_chg > SPOT_T:
        if pe_chg > PREM_T:
            return "BEARISH", f"spot ↑{spot_chg:.0f}pts but PE ↑₹{pe_chg:.1f} (institutional hedging)"
        if ce_chg < -PREM_T:
            return "BEARISH", f"spot ↑{spot_chg:.0f}pts but CE ↓₹{abs(ce_chg):.1f} (smart selling)"
    else:
        if ce_chg > PREM_T:
            return "BULLISH", f"spot ↓{abs(spot_chg):.0f}pts but CE ↑₹{ce_chg:.1f} (smart accumulation)"
        if pe_chg < -PREM_T:
            return "BULLISH", f"spot ↓{abs(spot_chg):.0f}pts but PE ↓₹{abs(pe_chg):.1f} (PE sellers absorbed)"
    return "NEUTRAL", ""


def _calc_composite_score() -> tuple[int, str]:
    """
    Combine FIB zone + momentum + premium flow + divergence into CE score 1–10.
    """
    with _fib_lock:
        ce_prob = _fib_state.get("ce_prob", 50)
    fib_score = (ce_prob - 50) / 50 * 3.0
    mom_dir, mom_pts, _ = _calc_momentum()
    if mom_dir == "UP":
        mom_score = min(2.0, abs(mom_pts) / 5.0)
    elif mom_dir == "DOWN":
        mom_score = -min(2.0, abs(mom_pts) / 5.0)
    else:
        mom_score = 0.0
    flow = list(_prob_history)
    if len(flow) >= 5:
        flow_score = (sum(flow[-5:]) / 5 - 50) / 50 * 2.0
    else:
        flow_score = 0.0
    div_sig, _ = _calc_divergence()
    div_score = 1.0 if div_sig == "BULLISH" else (-1.0 if div_sig == "BEARISH" else 0.0)
    total    = fib_score * 0.40 + mom_score * 0.30 + flow_score * 0.20 + div_score * 0.10
    ce_score = int(round(max(1, min(10, 5 + total * 1.5))))
    fib_ok   = "✅" if ce_prob >= 60 else ("❌" if ce_prob <= 40 else "⚪")
    mom_ok   = "✅" if mom_dir == "UP" else ("❌" if mom_dir == "DOWN" else "⚪")
    flow_ok  = "✅" if flow_score > 0.3 else ("❌" if flow_score < -0.3 else "⚪")
    div_ok   = "✅" if div_sig == "BULLISH" else ("❌" if div_sig == "BEARISH" else "⚪")
    return ce_score, f"FIB{fib_ok}  MOM{mom_ok}  FLOW{flow_ok}  DIV{div_ok}"


def _session_context_str() -> str:
    """One-line session context for the panel header."""
    ctx = _session_ctx
    parts = []
    now = datetime.now()
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now < close_t:
        rem = int((close_t - now).total_seconds())
        h, m = divmod(rem // 60, 60)
        parts.append(f"{h}h {m:02d}m to close")
    else:
        parts.append("market closed")
    if ctx.get("is_expiry_day"):
        parts.append("EXPIRY TODAY ⚠️")
    strike = ctx.get("strike")
    spot_now = _fib_state.get("spot")
    if strike and spot_now:
        otm = abs(int(strike) - spot_now)
        side = "OTM" if int(strike) > spot_now else "ITM"
        parts.append(f"Strike {strike} = {otm:.0f}pts {side}")
    hi = ctx.get("session_high")
    lo = ctx.get("session_low")
    if hi and lo:
        parts.append(f"H:{hi:.0f}  L:{lo:.0f}")
    return "  │  ".join(parts)


_fib_force_refresh = threading.Event()   # set to trigger immediate fib recalc

_tick_history: deque = deque(maxlen=20)  # (spot, ce_ltp, pe_ltp) per poll tick

_session_ctx: dict = {                   # set once by main(), read in panel
    "strike":       None,   # int tracked strike
    "expiry":       None,   # str "YYYY-MM-DD"
    "session_high": None,   # float, intraday spot high
    "session_low":  None,   # float, intraday spot low
    "is_expiry_day": False,
}

_telegram_sent: dict = {                 # cooldowns to avoid duplicate alerts
    "zone":      "",        # last alerted zone
    "conflict":  0.0,       # epoch of last conflict alert
    "level":     0.0,       # epoch of last level-cross alert
    "score_low": 0.0,       # epoch of last low-score alert
    "day_hl":    0.0,       # epoch of last day H/L proximity alert
}

_day_hl: dict = {           # today's intraday high/low for the index
    "high":       None,     # float
    "low":        None,     # float
    "updated_at": None,     # datetime
}


_fib_state: dict = {
    "updated_at": None,
    "spot":       None,
    "swing_desc": "",
    # nearest support / resistance
    "sup_label":  "",
    "sup_price":  None,
    "res_label":  "",
    "res_price":  None,
    # mentor fields
    "zone":       "",
    "trend":      "",
    "ce_prob":    0,
    "pe_prob":    0,
    "action":     "Computing…",
    "target":     None,
    "stop_price": None,
    "stop_label": "",
    "mentor":     [],
}


def _speak_fib(action: str, trend: str, mentor_lines: list[str]) -> None:
    """Speak the Fib mentor guidance aloud using macOS say (non-blocking)."""
    import re
    def clean(s: str) -> str:
        s = re.sub(r'[^\x00-\x7F]+', ' ', s)   # strip emoji / non-ASCII
        s = re.sub(r'\s+', ' ', s).strip()
        s = s.replace("CE", "C E").replace("PE", "P E")
        s = s.replace("[mock]", "").replace("[mock data]", "")
        return s

    trend_clean  = clean(trend)
    action_clean = clean(action)
    mentor_clean = clean(mentor_lines[0]) if mentor_lines else ""

    parts = [p for p in [trend_clean, action_clean, mentor_clean] if p]
    text  = ". ".join(parts)
    if text:
        rate = str(CONFIG.get("FIB_VOICE_RATE", 160))
        subprocess.Popen(
            ["say", "-r", rate, text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _fetch_candles_fib(groww_client, index_name: str) -> list[dict]:
    hours    = CONFIG["FIB_LOOKBACK_HOURS"]
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(hours=hours)
    sym_map  = {
        "NIFTY":     (groww_client.EXCHANGE_NSE, ["NSE-NIFTY 50", "NSE-NIFTY"]),
        "SENSEX":    (groww_client.EXCHANGE_BSE, ["BSE-SENSEX", "BSE-S&P BSE SENSEX"]),
        "BANKNIFTY": (groww_client.EXCHANGE_NSE, ["NSE-NIFTY BANK"]),
    }
    exchange, symbols = sym_map.get(index_name.upper(), (groww_client.EXCHANGE_NSE, []))
    for sym in symbols:
        try:
            result = groww_client.get_historical_candles(
                groww_symbol=sym, exchange=exchange, segment="CASH",
                start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval="15minute",
            )
            if result and result.get("candles") and len(result["candles"]) >= 5:
                return [{"high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
                        for c in result["candles"]]
        except Exception:
            pass
    return []


def _refresh_day_hl(groww_client, index_name: str) -> None:
    """Fetch today's intraday high/low from 15-min candles starting at 09:15."""
    now = datetime.now()
    today_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    sym_map = {
        "NIFTY":     (groww_client.EXCHANGE_NSE, ["NSE-NIFTY 50", "NSE-NIFTY"]),
        "SENSEX":    (groww_client.EXCHANGE_BSE, ["BSE-SENSEX", "BSE-S&P BSE SENSEX"]),
        "BANKNIFTY": (groww_client.EXCHANGE_NSE, ["NSE-NIFTY BANK"]),
    }
    exchange, symbols = sym_map.get(index_name.upper(), (groww_client.EXCHANGE_NSE, []))
    for sym in symbols:
        try:
            result = groww_client.get_historical_candles(
                groww_symbol=sym, exchange=exchange, segment="CASH",
                start_time=today_open.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval="15minute",
            )
            if result and result.get("candles") and len(result["candles"]) >= 1:
                highs = [float(c[2]) for c in result["candles"]]
                lows  = [float(c[3]) for c in result["candles"]]
                _day_hl["high"]       = max(highs)
                _day_hl["low"]        = min(lows)
                _day_hl["updated_at"] = now
                return
        except Exception:
            pass


def _refresh_fib(groww_client, index_name: str, access_token: str, expiry: str) -> None:
    spot = get_spot(index_name, expiry, access_token)
    if spot is None:
        print(f"\n{C.DIM}[FIB] ⚠️  Spot fetch failed — check API/expiry{C.RESET}")
        return
    candles = _fetch_candles_fib(groww_client, index_name)
    if not candles:
        print(f"\n{C.DIM}[FIB] ⚠️  No candle data returned (market closed / API limit){C.RESET}")
        return
    swings = _detect_swings(candles, CONFIG["FIB_SWING_WINDOW"])
    pair   = _swing_pair(swings, spot)
    # Early-session fallback: shrink the window until we get ≥2 alternating swings.
    # Happens when only a few candles are available (e.g. first 1-2 hours of the day).
    if not pair:
        for fallback_win in [2, 1]:
            swings = _detect_swings(candles, fallback_win)
            pair   = _swing_pair(swings, spot)
            if pair:
                print(f"\n{C.DIM}[FIB] ℹ️  Using window={fallback_win} (only {len(candles)} candles available){C.RESET}")
                break
    if not pair:
        print(f"\n{C.DIM}[FIB] ⚠️  Only {len(swings)} swing(s) in {len(candles)} candles — data too thin for Fibonacci{C.RESET}")
        return
    fib = _calc_fib(pair["low"], pair["high"], pair["bullish"])
    if not fib:
        return
    sig = _fib_signal(spot, fib)
    _spot_history.append(spot)          # feed momentum tracker
    with _fib_lock:
        _fib_state.update({
            "updated_at": datetime.now(),
            "spot":       spot,
            "swing_desc": pair["desc"],
            **sig,
        })
    _refresh_day_hl(groww_client, index_name)   # piggyback on fib cycle — no extra rate cost
    global _last_fib_zone
    zone_changed = sig["zone"] != _last_fib_zone and _last_fib_zone != ""
    if sig["zone"] != _last_fib_zone:
        if zone_changed:
            msg = (f"📐 PDT ZONE CHANGE — {index_name}\n"
                   f"  Was : {_last_fib_zone}\n"
                   f"  Now : {sig['zone']}\n"
                   f"  Spot: {spot:.0f}\n"
                   f"  Action: {sig['action']}")
            threading.Thread(target=_send_telegram, args=(msg,), daemon=True).start()
        _last_fib_zone = sig["zone"]
        if CONFIG.get("FIB_VOICE"):
            _speak_fib(sig["action"], sig["trend"], sig["mentor"])


def _fib_worker(groww_client, index_name: str, access_token: str, expiry: str) -> None:
    refresh = CONFIG["FIB_REFRESH_SEC"]
    while True:
        try:
            _refresh_fib(groww_client, index_name, access_token, expiry)
        except Exception as e:
            print(f"\n{C.RED}[FIB] Worker exception: {e}{C.RESET}")
        # Wait for scheduled refresh OR an immediate force-refresh signal
        triggered = _fib_force_refresh.wait(timeout=refresh)
        if triggered:
            _fib_force_refresh.clear()
            print(f"{C.DIM}[FIB] ⚡ Force-refresh triggered (key level crossed){C.RESET}")


def _print_prob_chart() -> None:
    hist = list(_prob_history)
    if len(hist) < 2:
        return

    # Trend: recent avg vs earlier avg
    n      = max(1, min(5, len(hist) // 2))
    recent = sum(hist[-n:]) / n
    older  = sum(hist[:n])  / n
    diff   = recent - older
    if diff > 3:
        trend_str = f"↑ BULLISH  CE +{diff:.0f}%"
        t_col = _cc("FLOW_BULL")
    elif diff < -3:
        trend_str = f"↓ BEARISH  CE {diff:.0f}%"
        t_col = _cc("FLOW_BEAR")
    else:
        trend_str = f"→ NEUTRAL  CE {recent:.0f}%"
        t_col = _cc("FLOW_NEUTRAL")

    current = hist[-1]
    c_col   = _cc("FLOW_BULL") if current > 55 else (_cc("FLOW_BEAR") if current < 45 else _cc("FLOW_NEUTRAL"))

    print(f"\n  {C.BOLD}LIVE PREMIUM FLOW CHART{C.RESET}"
          f"  {C.DIM}(CE÷(CE+PE) ratio — independent of Fibonacci){C.RESET}")
    print(f"  {t_col}{C.BOLD}{trend_str}{C.RESET}"
          f"   {C.DIM}current flow:{C.RESET} {c_col}{C.BOLD}CE {current}%  PE {100-current}%{C.RESET}")

    for r in range(11):          # r=0 → 100%, r=10 → 0%
        level = 100 - r * 10
        chars = []
        for val in hist:
            val_row = round((100 - val) / 10)
            if val_row == r:
                dot_col = _cc("FLOW_BULL") if val > 55 else (_cc("FLOW_BEAR") if val < 45 else _cc("FLOW_NEUTRAL"))
                chars.append(f"{dot_col}●{C.RESET}")
            elif r == 5:
                chars.append(f"{C.DIM}─{C.RESET}")
            else:
                chars.append(" ")
        sep       = "┼" if r == 5 else "│"
        note      = f"  {C.DIM}◀ NEUTRAL (50%){C.RESET}" if r == 5 else ""
        print(f"  {C.DIM}{level:>3}%{C.RESET} {sep} {''.join(chars)}{note}")

    tick = "─" * len(hist)
    print(f"       └─{tick}")
    print(f"  {C.DIM}        ← older{' ' * max(0, len(hist) - 14)}newer →{C.RESET}\n")


def _print_fib_panel() -> None:
    with _fib_lock:
        s = dict(_fib_state)
    if not s["updated_at"]:
        return

    ts      = s["updated_at"].strftime("%H:%M:%S")
    W       = 68
    bar_w   = 22
    ce_p    = s.get("ce_prob", 0)
    pe_p    = s.get("pe_prob", 0)
    ce_bars = round(ce_p * bar_w / 100)
    pe_bars = round(pe_p * bar_w / 100)
    ce_bar  = "█" * ce_bars + "░" * (bar_w - ce_bars)
    pe_bar  = "█" * pe_bars + "░" * (bar_w - pe_bars)
    ce_col  = _cc("CE_HIGH")  if ce_p >= 60 else (_cc("CE_MID")  if ce_p >= 45 else _cc("CE_LOW"))
    pe_col  = _cc("PE_HIGH")  if pe_p >= 60 else (_cc("PE_MID")  if pe_p >= 45 else _cc("PE_LOW"))
    act_col = _cc("ACTION_BULL") if ce_p >= 60 else (_cc("ACTION_BEAR") if pe_p >= 60 else _cc("ACTION_NEUTRAL"))

    spot_val = s.get("spot") or 0
    tgt      = s.get("target")
    stp_px   = s.get("stop_price")
    tgt_str  = ""
    if tgt:
        diff    = tgt - spot_val
        sign    = "+" if diff >= 0 else ""
        tgt_str = f"{tgt:.0f}  ({sign}{diff:.0f} pts)"

    rule  = f"{C.CYAN}{'─' * W}{C.RESET}"
    thin  = f"  {C.DIM}{'·' * (W - 2)}{C.RESET}"

    # ── Composite score ───────────────────────────────────────
    ce_score, score_breakdown = _calc_composite_score()
    score_filled = round(ce_score * bar_w / 10)
    score_bar    = "█" * score_filled + "░" * (bar_w - score_filled)
    score_col    = _cc("SCORE_HIGH") if ce_score >= 7 else (_cc("SCORE_MID") if ce_score >= 5 else _cc("SCORE_LOW"))

    # ── Divergence ────────────────────────────────────────────
    div_sig, div_desc = _calc_divergence()

    print(f"\n{rule}")
    print(f"  {C.BOLD}{C.CYAN}FIB MENTOR  [{ts}]{C.RESET}")
    # Session context bar
    ctx_str = _session_context_str()
    if ctx_str:
        print(f"  {C.DIM}{ctx_str}{C.RESET}")
    print(f"  Swing  {s['swing_desc']}  │  Spot {C.BOLD}{spot_val:.1f}{C.RESET}")
    # Day High / Day Low line
    dh = _day_hl.get("high")
    dl = _day_hl.get("low")
    if dh and dl and spot_val:
        above_h = spot_val > dh
        below_l = spot_val < dl
        if above_h:
            # Spot broke above day high — show pts above + upside extension
            pts_above  = spot_val - dh
            ext_target = round(dh + (dh - dl) * 0.618, 0)   # 61.8% of day range projected up
            print(f"  {C.BOLD}Day  {C.GREEN}⬆ H BROKEN  {dh:.0f}  "
                  f"(+{pts_above:.0f} pts above){C.RESET}  "
                  f"{C.DIM}Next up: {ext_target:.0f}  (+{ext_target-spot_val:.0f} pts){C.RESET}")
            print(f"  {C.DIM}     L {dl:.0f}   │   Day range {dh-dl:.0f} pts{C.RESET}")
        elif below_l:
            # Spot broke below day low — show pts below + downside extension
            pts_below  = dl - spot_val
            ext_target = round(dl - (dh - dl) * 0.618, 0)   # 61.8% of day range projected down
            print(f"  {C.BOLD}Day  {C.RED}⬇ L BROKEN  {dl:.0f}  "
                  f"(-{pts_below:.0f} pts below){C.RESET}  "
                  f"{C.DIM}Next down: {ext_target:.0f}  (-{spot_val-ext_target:.0f} pts){C.RESET}")
            print(f"  {C.DIM}     H {dh:.0f}   │   Day range {dh-dl:.0f} pts{C.RESET}")
        else:
            # Spot inside day range — show distance to each level
            to_h   = dh - spot_val
            to_l   = spot_val - dl
            h_col  = _cc("DAY_H_NEAR") if to_h < 15 else (_cc("DAY_H_MID") if to_h < 40 else _cc("DAY_H_FAR"))
            l_col  = _cc("DAY_L_NEAR") if to_l < 15 else (_cc("DAY_L_MID") if to_l < 40 else _cc("DAY_L_FAR"))
            h_warn = " ⚠" if to_h < 15 else (" ↗" if to_h < 40 else "")
            l_warn = " ⚠" if to_l < 15 else ""
            print(f"  {C.BOLD}Day{C.RESET}   "
                  f"{h_col}H {dh:.0f}  (+{to_h:.0f} to break{h_warn}){C.RESET}"
                  f"   │   "
                  f"{l_col}L {dl:.0f}  (-{to_l:.0f} support{l_warn}){C.RESET}")
    print(thin)
    zone_txt = s.get('zone', '')
    zone_col = _cc("BULLISH") if "BULLISH" in zone_txt or "BREAKOUT" in zone_txt else (
               _cc("BEARISH") if "BEARISH" in zone_txt or "BREAKDOWN" in zone_txt else _cc("NEUTRAL"))
    print(f"  {C.BOLD}Zone   {zone_col}{zone_txt}{C.RESET}")
    trend_txt = s.get('trend', '')
    trend_col = _cc("BULLISH") if "BULLISH" in trend_txt or "⬆" in trend_txt else (
                _cc("BEARISH") if "BEARISH" in trend_txt or "⬇" in trend_txt else _cc("NEUTRAL"))
    print(f"  {C.BOLD}Trend  {trend_col}{trend_txt}{C.RESET}")
    # Composite score line
    print(f"  {C.BOLD}Score  {score_col}{score_bar}  CE {ce_score}/10{C.RESET}  "
          f"{C.DIM}[{score_breakdown}]{C.RESET}")
    print(thin)
    print(f"  {ce_col}CE {ce_p:>3}%  {ce_bar}{C.RESET}"
          f"   {pe_col}PE {pe_p:>3}%  {pe_bar}{C.RESET}")
    # Divergence line
    if div_sig != "NEUTRAL":
        div_col = C.GREEN if div_sig == "BULLISH" else C.YELLOW
        div_icon = "🟢" if div_sig == "BULLISH" else "⚠️"
        print(f"  {div_col}{C.BOLD}Divergence  {div_icon} {div_sig}{C.RESET}  "
              f"{C.DIM}{div_desc}{C.RESET}")
    print(thin)
    if s.get("res_price") is not None:
        pts_to_res = s['res_price'] - spot_val if spot_val else 0
        print(f"  {_cc('BREAKOUT')}BREAKOUT  ↗ {s['res_price']:.0f}  "
              f"{_cc('TARGET')}(+{pts_to_res:.0f} pts)  {C.DIM}[{s['res_label']}]{C.RESET}")
    if s.get("sup_price") is not None:
        pts_to_sup = spot_val - s['sup_price'] if spot_val else 0
        print(f"  {_cc('SUPPORT')}SUPPORT   ↙ {s['sup_price']:.0f}  "
              f"{_cc('TARGET')}(-{pts_to_sup:.0f} pts)  {C.DIM}[{s['sup_label']}]{C.RESET}")
    if tgt_str:
        stp_str = f"{stp_px:.0f}" if stp_px else "?"
        print(f"  Target  {tgt_str}   │   Stop  {s.get('stop_label','?')} = {stp_str}")
    print(thin)
    print(f"  {C.BOLD}ACTION  {act_col}{s.get('action','')}{C.RESET}")
    for line in s.get("mentor", []):
        print(f"  {_cc('MENTOR_NOTES')}{line}{C.RESET}")

    # ── Momentum conflict block ───────────────────────────────
    mom_dir, mom_pts, mom_mins = _calc_momentum()
    ce_p_val   = s.get("ce_prob", 50)
    fib_says_ce = ce_p_val >= 60
    fib_says_pe = ce_p_val <= 40
    conflict = (mom_dir == "DOWN" and fib_says_ce) or (mom_dir == "UP" and fib_says_pe)

    if conflict and mom_mins >= 2.0:
        sup_px  = s.get("sup_price")
        res_px  = s.get("res_price")
        sup_lbl = s.get("sup_label", "")
        res_lbl = s.get("res_label", "")
        mom_str = f"{abs(mom_pts):.1f} pts/min"
        mins_str = f"{mom_mins:.0f} min"
        print(thin)
        if mom_dir == "DOWN" and fib_says_ce:
            print(f"  {C.YELLOW}{C.BOLD}⚠️  MOMENTUM CONFLICT{C.RESET}  "
                  f"{C.DIM}Fib → CE  │  Market ↓ {mom_str} (last {mins_str}){C.RESET}")
            print(f"  {C.DIM}Market has been falling while Fib zone suggests bullish.{C.RESET}")
            if sup_px:
                print(f"  {C.DIM}► Spot is near support {sup_lbl} ({sup_px:.0f}) — wait here.{C.RESET}")
                print(f"  {C.RED}► If breaks BELOW {sup_px:.0f} → momentum accelerating down → switch to PE.{C.RESET}")
            if res_px:
                print(f"  {C.GREEN}► CE conviction returns only on close ABOVE {res_px:.0f}.{C.RESET}")
            print(f"  {C.DIM}► Do NOT enter CE while market is still declining.{C.RESET}")
        else:
            print(f"  {C.YELLOW}{C.BOLD}⚠️  MOMENTUM CONFLICT{C.RESET}  "
                  f"{C.DIM}Fib → PE  │  Market ↑ {mom_str} (last {mins_str}){C.RESET}")
            print(f"  {C.DIM}Market has been rising while Fib zone suggests bearish.{C.RESET}")
            if res_px:
                print(f"  {C.DIM}► Spot is near resistance {res_lbl} ({res_px:.0f}) — wait here.{C.RESET}")
                print(f"  {C.GREEN}► If breaks ABOVE {res_px:.0f} → momentum accelerating up → switch to CE.{C.RESET}")
            if sup_px:
                print(f"  {C.RED}► PE conviction returns only on close BELOW {sup_px:.0f}.{C.RESET}")
            print(f"  {C.DIM}► Do NOT enter PE while market is still rising.{C.RESET}")
        # Telegram alert for conflict (max once per 5 min)
        now_ts = time.time()
        if now_ts - _telegram_sent["conflict"] > 300:
            _telegram_sent["conflict"] = now_ts
            conflict_msg = (f"⚠️ PDT MOMENTUM CONFLICT\n"
                            f"  Fib: {'CE' if fib_says_ce else 'PE'}  │  "
                            f"Market: {'↓' if mom_dir=='DOWN' else '↑'} {mom_str} ({mins_str})\n"
                            f"  Spot: {spot_val:.0f}  Score: {ce_score}/10")
            threading.Thread(target=_send_telegram, args=(conflict_msg,), daemon=True).start()

    # Telegram for very low score
    if ce_score <= 3:
        now_ts = time.time()
        if now_ts - _telegram_sent["score_low"] > 300:
            _telegram_sent["score_low"] = now_ts
            threading.Thread(target=_send_telegram,
                args=(f"🔴 PDT LOW SCORE: CE {ce_score}/10 [{score_breakdown}]\nSpot: {spot_val:.0f}",),
                daemon=True).start()

    print(rule)
    _print_prob_chart()
    print(f"{rule}\n")


# ─────────────────────────────────────────────────────────────
#  DIRECTION LOGIC
# ─────────────────────────────────────────────────────────────
def direction(prev: float | None, curr: float, threshold: float) -> str:
    if prev is None:
        return "INIT"
    diff = curr - prev
    if diff > threshold:
        return "UP"
    if diff < -threshold:
        return "DOWN"
    return "STABLE"


def direction_color(d: str) -> str:
    if d == "UP":
        return _cc("UP")
    if d == "DOWN":
        return _cc("DOWN")
    if d == "STABLE":
        return _cc("STABLE")
    return C.DIM


# ─────────────────────────────────────────────────────────────
#  SOUND  — pre-generated WAV tones (zero lag at playback)
#  UP   : double high-pitch beep  1000 Hz  pip-pip  → clearly rising
#  DOWN : single long low beep     180 Hz  bwooom   → clearly falling
#  WAVs are built at import time so afplay starts instantly.
# ─────────────────────────────────────────────────────────────
_SR = 44100

def _build_wav(tones: list[tuple[float, float, float]]) -> str:
    # tones: list of (hz, duration_sec, silence_after_sec)
    buf = bytearray()
    for hz, dur, gap in tones:
        n     = int(_SR * dur)
        fade  = max(1, int(_SR * 0.006))
        for i in range(n):
            v = int(26000 * math.sin(2 * math.pi * hz * i / _SR))
            if i >= n - fade:
                v = int(v * (n - i) / fade)
            buf += struct.pack("<h", v)
        buf += b"\x00\x00" * int(_SR * gap)
    path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SR)
        wf.writeframes(bytes(buf))
    return path

_SOUND_FILES = {
    "UP":   _build_wav([(1000, 0.07, 0.05), (1000, 0.07, 0)]),  # pip-pip  high
    "DOWN": _build_wav([(180,  0.30, 0)]),                        # bwooom   low
}

def play_sound(d: str) -> None:
    path = _SOUND_FILES.get(d)
    if path:
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

def ask_sound_settings() -> tuple[bool, str | None]:
    env_sound = os.environ.get("BOT_SOUND", "").strip().lower()
    if env_sound == "n":
        return False, None
    if env_sound == "y":
        track = os.environ.get("BOT_SOUND_TRACK", "CE").strip().upper()
        if track in ("CE", "PE"):
            print(f"  Sound (env): ON for {track}")
            return True, track
    print(f"\n{C.BOLD}Sound alerts?{C.RESET}  (y/n): ", end="", flush=True)
    ans = input().strip().lower()
    if ans != "y":
        return False, None
    print(f"{C.BOLD}Play sound for CE or PE?{C.RESET}  (ce/pe): ", end="", flush=True)
    while True:
        track = input().strip().lower()
        if track in ("ce", "pe"):
            print(f"  🔔 Sound ON for {track.upper()}")
            return True, track.upper()
        print("  ❗ Enter 'ce' or 'pe': ", end="", flush=True)


# ─────────────────────────────────────────────────────────────
#  USER PROMPTS
# ─────────────────────────────────────────────────────────────
def ask_index() -> str:
    env_index = os.environ.get("BOT_INDEX", "").strip().upper()
    if env_index in ("NIFTY", "SENSEX"):
        print(f"  Index (env): {env_index}")
        return env_index
    print(f"\n{C.BOLD}Select Index:{C.RESET}")
    print("  1. NIFTY")
    print("  2. SENSEX")
    while True:
        choice = input("Enter 1 or 2: ").strip()
        if choice == "1":
            return "NIFTY"
        if choice == "2":
            return "SENSEX"
        print("  ❗ Please enter 1 or 2.")


def ask_expiry(current: str | None, nxt: str | None) -> str:
    env_expiry = os.environ.get("BOT_EXPIRY", "").strip().lower()
    if env_expiry == "current" and current:
        print(f"  Expiry (env): current → {current}")
        return current
    if env_expiry == "next" and nxt:
        print(f"  Expiry (env): next → {nxt}")
        return nxt

    def fmt(d: str) -> str:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")

    print(f"\n{C.BOLD}Select Expiry:{C.RESET}")
    options: list[str] = []
    if current:
        options.append(current)
        print(f"  1. Current  →  {fmt(current)}")
    if nxt:
        options.append(nxt)
        idx = len(options)
        print(f"  {idx}. Next     →  {fmt(nxt)}")

    if not options:
        print("❗ No future expiries found.")
        sys.exit(1)

    while True:
        choice = input(f"Enter 1–{len(options)}: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print(f"  ❗ Please enter a number between 1 and {len(options)}.")


# ─────────────────────────────────────────────────────────────
#  SHARED PRINT LOOP  (used by both live and test modes)
# ─────────────────────────────────────────────────────────────
def run_loop(ce_label: str, pe_label: str, get_ce_ltp, get_pe_ltp,
             sound_enabled: bool = False, sound_track: str | None = None,
             get_spot_fn=None, get_ltp_pair_fn=None):
    threshold = CONFIG["DIRECTION_THRESHOLD"]
    refresh   = CONFIG["REFRESH_SEC"]

    print(f"\n{_cc('TRACKING_LINE')}▶  Tracking  {ce_label}  &  {pe_label}  — refresh every {refresh}s{C.RESET}")
    sound_info = f"🔔 sound on {sound_track}" if sound_enabled else "🔕 sound off"
    print(f"{_cc('STATUS_DIM')}   Threshold ₹{threshold}   |   {sound_info}   |   Ctrl-C to stop{C.RESET}\n")

    prev_ce: float | None = None
    prev_pe: float | None = None
    prev_dir_ce: str = "INIT"
    prev_dir_pe: str = "INIT"
    prev_spot: float | None = None
    last_fib_print: float = 0.0   # epoch seconds of last panel print
    fib_print_sec  = CONFIG["FIB_PRINT_SEC"]
    frozen_ticks   = 0            # consecutive ticks with unchanged CE price
    last_ce_val: float | None = None

    while True:
        try:
            ts = datetime.now().strftime("%H:%M:%S")

            if get_ltp_pair_fn is not None:
                ce_ltp, pe_ltp = get_ltp_pair_fn()
            else:
                ce_ltp = get_ce_ltp()
                pe_ltp = get_pe_ltp()

            if ce_ltp is not None:
                d_ce = direction(prev_ce, ce_ltp, threshold)
                col_ce = direction_color(d_ce)
                ce_arrow = "↑" if d_ce == "UP" else ("↓" if d_ce == "DOWN" else "→")
                ce_str = (
                    f"{col_ce}{ce_label} {ce_arrow} {d_ce:<6} ₹{ce_ltp:>7.2f}{C.RESET}"
                )
                if sound_enabled and sound_track == "CE" and d_ce not in ("INIT", prev_dir_ce):
                    play_sound(d_ce)
                # Frozen LTP detection — warn after 20 unchanged ticks
                if ce_ltp == last_ce_val:
                    frozen_ticks += 1
                    if frozen_ticks == 20:
                        print(f"\n{C.YELLOW}⚠️  LTP unchanged for 20 ticks — "
                              f"market may be closed or API returning stale data.{C.RESET}\n")
                else:
                    frozen_ticks = 0
                last_ce_val = ce_ltp
                prev_ce = ce_ltp
                prev_dir_ce = d_ce
            else:
                ce_str = f"{C.DIM}{ce_label} -- N/A{C.RESET}"

            if pe_ltp is not None:
                d_pe = direction(prev_pe, pe_ltp, threshold)
                col_pe = direction_color(d_pe)
                pe_arrow = "↑" if d_pe == "UP" else ("↓" if d_pe == "DOWN" else "→")
                pe_str = (
                    f"{col_pe}{pe_label} {pe_arrow} {d_pe:<6} ₹{pe_ltp:>7.2f}{C.RESET}"
                )
                if sound_enabled and sound_track == "PE" and d_pe not in ("INIT", prev_dir_pe):
                    play_sound(d_pe)
                prev_pe = pe_ltp
                prev_dir_pe = d_pe
            else:
                pe_str = f"{C.DIM}{pe_label} -- N/A{C.RESET}"

            # Reprint fib panel every FIB_PRINT_SEC seconds (uses cached data — no API call)
            with _fib_lock:
                fib_ready = _fib_state["updated_at"] is not None
            if fib_ready and (time.time() - last_fib_print) >= fib_print_sec:
                # Feed chart from live CE/PE premium ratio (works in both TEST and LIVE mode)
                if prev_ce is not None and prev_pe is not None:
                    total = prev_ce + prev_pe
                    if total > 0:
                        _prob_history.append(round(prev_ce / total * 100))
                _print_fib_panel()
                last_fib_print = time.time()

            spot_val = get_spot_fn() if get_spot_fn else None

            # Track tick history and session H/L
            if spot_val is not None:
                _tick_history.append((spot_val, ce_ltp, pe_ltp))
                hi = _session_ctx.get("session_high")
                lo = _session_ctx.get("session_low")
                _session_ctx["session_high"] = max(hi, spot_val) if hi else spot_val
                _session_ctx["session_low"]  = min(lo, spot_val) if lo else spot_val

                # Force-refresh fib when spot crosses a key support/resistance level
                with _fib_lock:
                    sup_px = _fib_state.get("sup_price")
                    res_px = _fib_state.get("res_price")
                if prev_spot is not None:
                    crossed_lbl = crossed_lvl = None
                    if res_px and ((prev_spot < res_px <= spot_val) or (prev_spot > res_px >= spot_val)):
                        crossed_lbl = "BREAKOUT" if spot_val >= res_px else "PULLBACK"
                        crossed_lvl = f"{res_px:.0f}"
                    elif sup_px and ((prev_spot > sup_px >= spot_val) or (prev_spot < sup_px <= spot_val)):
                        crossed_lbl = "BREAKDOWN" if spot_val <= sup_px else "BOUNCE"
                        crossed_lvl = f"{sup_px:.0f}"
                    if crossed_lbl:
                        now_ts = time.time()
                        if now_ts - _telegram_sent["level"] > 120:
                            _telegram_sent["level"] = now_ts
                            threading.Thread(
                                target=_send_telegram,
                                args=(f"⚡ PDT KEY LEVEL: {crossed_lbl} at {crossed_lvl}\n"
                                      f"Spot: {spot_val:.0f}",),
                                daemon=True,
                            ).start()
                        _fib_force_refresh.set()

                # Day High / Day Low proximity and crossing alerts
                dh = _day_hl.get("high")
                dl = _day_hl.get("low")
                if dh and dl and prev_spot is not None:
                    now_ts = time.time()
                    alert_msg = None
                    day_range = dh - dl
                    ext_up   = round(dh + day_range * 0.618, 0)
                    ext_down = round(dl - day_range * 0.618, 0)
                    # Crossing alerts (highest priority)
                    if prev_spot < dh <= spot_val:
                        alert_msg = (f"🚀 PDT DAY HIGH BROKEN: {dh:.0f}\n"
                                     f"Spot: {spot_val:.0f}  (+{spot_val-dh:.0f} pts above)\n"
                                     f"Next up target: {ext_up:.0f}  (+{ext_up-spot_val:.0f} pts)")
                    elif prev_spot > dl >= spot_val:
                        alert_msg = (f"🔻 PDT DAY LOW BROKEN: {dl:.0f}\n"
                                     f"Spot: {spot_val:.0f}  (-{dl-spot_val:.0f} pts below)\n"
                                     f"Next down target: {ext_down:.0f}  (-{spot_val-ext_down:.0f} pts)")
                    # Proximity alert when within 15 pts
                    else:
                        near_h = (dh - spot_val) <= 15
                        near_l = (spot_val - dl) <= 15
                        if near_h or near_l:
                            parts = []
                            if near_h:
                                parts.append(f"Day High {dh:.0f} — {dh-spot_val:.0f} pts away ⬆️  (if breaks → {ext_up:.0f})")
                            if near_l:
                                parts.append(f"Day Low {dl:.0f} — {spot_val-dl:.0f} pts away ⬇️  (if breaks → {ext_down:.0f})")
                            alert_msg = (f"⚠️ PDT NEAR DAY RANGE\n"
                                         f"Spot: {spot_val:.0f}\n" + "\n".join(parts))
                    if alert_msg and (now_ts - _telegram_sent["day_hl"] > 180):
                        _telegram_sent["day_hl"] = now_ts
                        threading.Thread(
                            target=_send_telegram, args=(alert_msg,), daemon=True
                        ).start()

            prev_spot = spot_val

            spot_str = (f"  {_cc('SPOT')}SPOT {spot_val:.1f}{C.RESET}" if spot_val else "")
            print(f"[{C.DIM}{ts}{C.RESET}]{spot_str}  {ce_str}   |   {pe_str}")
            time.sleep(refresh)

        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}Stopped.{C.RESET}")
            break
        except Exception as e:
            print(f"{C.RED}⚠️  Error: {e}{C.RESET}")
            time.sleep(refresh)


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
def main():
    print(f"\n{_cc('TRACKER_HEADER')}━━━ PREMIUM DIRECTION TRACKER ━━━{C.RESET}")

    # ── TEST MODE ────────────────────────────────────────────
    if TEST_MODE:
        print(f"{C.YELLOW}  [TEST MODE — no API calls, mock data]{C.RESET}\n")
        ce_label = f"({TEST_STRIKE} CE)"
        pe_label = f"({TEST_STRIKE} PE)"
        mock_ce   = _MockPrice(TEST_CE_START, TEST_VOLATILITY)
        mock_pe   = _MockPrice(TEST_PE_START, TEST_VOLATILITY)
        mock_spot = _MockSpot(float(TEST_STRIKE))
        print(f"📊 Mock index: {C.BOLD}{TEST_INDEX}{C.RESET}   "
              f"Strike: {C.BOLD}{TEST_STRIKE}{C.RESET}   "
              f"CE start ₹{TEST_CE_START}   PE start ₹{TEST_PE_START}")
        # Populate mock Fibonacci state so the panel appears on first tick
        _mock_swing_low  = TEST_STRIKE - 300
        _mock_swing_high = TEST_STRIKE + 200
        _mock_r236 = round(_mock_swing_high - (_mock_swing_high - _mock_swing_low) * 0.236, 0)
        _mock_r382 = round(_mock_swing_high - (_mock_swing_high - _mock_swing_low) * 0.382, 0)
        with _fib_lock:
            _fib_state.update({
                "updated_at": datetime.now(),
                "spot":       float(TEST_STRIKE),
                "swing_desc": f"↑ {_mock_swing_low}→{_mock_swing_high} [mock]",
                "res_label":  "R23.6%",
                "res_price":  _mock_r236,
                "sup_label":  "R38.2%",
                "sup_price":  _mock_r382,
                "zone":       f"SHALLOW PULLBACK — above R23.6% ({_mock_r236:.0f}) [mock]",
                "trend":      "⬆ BULLISH — minor retracement, uptrend intact [mock]",
                "ce_prob":    70,
                "pe_prob":    30,
                "action":     "STAY IN CE — shallow pullback, trend still up [mock]",
                "target":     float(_mock_swing_high),
                "stop_price": _mock_r382,
                "stop_label": f"R38.2% ({_mock_r382:.0f})",
                "mentor":     [
                    f"✅ Only a shallow pullback — uptrend is strong. [mock data]",
                    f"   Stay in CE.  Target: swing high {_mock_swing_high}.",
                    f"   Exit CE if it closes below R38.2% ({_mock_r382:.0f}).",
                ],
            })
        if CONFIG.get("FIB_VOICE"):
            _speak_fib(
                "STAY IN C E — shallow pullback, trend still up",
                "Bullish, minor retracement, uptrend intact",
                ["Only a shallow pullback. Stay in C E. Target swing high."],
            )
        snd_on, snd_track = ask_sound_settings()
        run_loop(ce_label, pe_label, mock_ce.next, mock_pe.next, snd_on, snd_track, mock_spot.next)
        return

    # ── LIVE MODE ────────────────────────────────────────────
    print(f"{C.DIM}Read-only | no orders placed{C.RESET}\n")

    try:
        groww_client, access_token = init_groww()
        print(f"{_cc('API_OK')}✅ Groww API initialized{C.RESET}")
    except Exception as e:
        print(f"❌ Auth failed: {e}")
        sys.exit(1)

    instruments = load_instruments()
    if not instruments:
        print("❌ Could not load instruments.")
        sys.exit(1)

    index_name = ask_index()

    current_exp, next_exp = get_expiry_dates(instruments, index_name)
    if not current_exp:
        print(f"❌ No expiry found for {index_name}.")
        sys.exit(1)

    expiry = ask_expiry(current_exp, next_exp)
    exp_display = datetime.strptime(expiry, "%Y-%m-%d").strftime("%d %b %Y")
    print(f"\n{_cc('TRACKING_LABEL')}📅 Tracking  {index_name}  expiry  {exp_display}{C.RESET}")

    print("📡 Fetching spot price ...")
    spot = get_spot(index_name, expiry, access_token)
    if not spot:
        print("❌ Could not fetch spot price.")
        sys.exit(1)
    # Pre-seed cache so the first tick shows spot immediately
    _spot_cache["price"] = spot
    _spot_cache["ts"]    = time.time()
    step = 100 if index_name.upper() == "SENSEX" else 50
    atm  = round(spot / step) * step
    print(f"{_cc('SPOT_LABEL')}📊 Spot: {spot:.2f}   ATM strike: {int(atm)}{C.RESET}")

    chosen_strike, ce_inst, pe_inst = select_strike(
        instruments, index_name, expiry, spot, access_token
    )
    if not ce_inst or not pe_inst:
        print(f"❌ Could not find CE/PE instruments for strike {int(chosen_strike)}.")
        sys.exit(1)

    strike_label = int(chosen_strike)
    ce_label = f"({strike_label} CE)"
    pe_label = f"({strike_label} PE)"

    # Populate session context so the panel header shows strike/expiry/expiry-day info
    _session_ctx["strike"]        = strike_label
    _session_ctx["expiry"]        = expiry
    _session_ctx["is_expiry_day"] = (datetime.now().strftime("%Y-%m-%d") == expiry)

    # Start Fibonacci background thread
    fib_thread = threading.Thread(
        target=_fib_worker,
        args=(groww_client, index_name, access_token, expiry),
        daemon=True,
    )
    fib_thread.start()
    print(f"{_cc('FIB_START')}🔢 Fibonacci worker started (refresh every {CONFIG['FIB_REFRESH_SEC']}s){C.RESET}")

    snd_on, snd_track = ask_sound_settings()
    run_loop(
        ce_label, pe_label,
        get_ce_ltp=None,
        get_pe_ltp=None,
        sound_enabled=snd_on,
        sound_track=snd_track,
        get_spot_fn=lambda: get_spot_live(index_name, expiry, access_token),
        get_ltp_pair_fn=lambda: get_ltp_pair(ce_inst, pe_inst, access_token),
    )


if __name__ == "__main__":
    main()
