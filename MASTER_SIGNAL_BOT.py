"""
MASTER SIGNAL BOT
=================
Single-output trading signal: ENTER CE / ENTER PE / NO TRADE
Combines 1h + 15m + 5m candles with live ATM premium flow.
Confidence threshold: 65%. No-trade: before 9:30 AM, after 3:00 PM.
Refresh: 60 seconds.
"""

import os, sys, re, time, csv, json, requests, pyotp
from datetime import datetime, timedelta

try:
    from growwapi import GrowwAPI
except ImportError:
    print("growwapi not installed — run: pip install growwapi")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════
#  CREDENTIALS
# ══════════════════════════════════════════════════════════════
API_KEY     = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjQ2NTczODEsImlhdCI6MTc3NjI1NzM4MSwibmJmIjoxNzc2MjU3MzgxLCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCJjMjAzMmM5MS04ZGYzLTRkZDUtYjc5NS0yMGVlOWRhZDhhZjlcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZXNzaW9uSWRcIjpcIjI0OWQ2OGRlLTNjZTgtNGQ4OS05ODJkLWM0N2NmYmI1YzdlNFwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIyNDA5OjQwYzQ6MTBhMzozN2UzOjE4NGI6N2IyOTpiMzBlOjIwZTUsMTcyLjcwLjIxOC4xMzUsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjQ2NTczODE2ODYsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.3kotfZI_EC0lzszHKlXiRdqEQv-O8ubYFh0pgoAT0KsSfdQ1sHmts5UtlaAq4PB6DEwY4X2jZUCD8uBgc2nwXQ"
TOTP_SECRET = "SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4"

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
REFRESH_SEC          = 60
CONFIDENCE_THRESHOLD = 65
NO_TRADE_BEFORE_MIN  = 9 * 60 + 30   # 9:30 AM
NO_TRADE_AFTER_MIN   = 15 * 60        # 3:00 PM

INDICES = {
    "NIFTY":  {"qty": 20, "step": 50,  "exchange": "NSE",
               "sym_candidates": ["NSE-NIFTY 50", "NSE-NIFTY"]},
    "SENSEX": {"qty": 50, "step": 100, "exchange": "BSE",
               "sym_candidates": ["BSE-SENSEX", "BSE-S&P BSE SENSEX"]},
}

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CSV_PATH     = os.path.join(PROJECT_ROOT, "instrument.csv")
TUNING_PATH  = os.path.join(PROJECT_ROOT, "BOT_TUNING.json")

W_1H = 0.35; W_15M = 0.40; W_5M = 0.15; W_PREM = 0.10
MAX_TOTAL = 3*W_1H + 3*W_15M + 2*W_5M + 1*W_PREM  # 2.65

# ══════════════════════════════════════════════════════════════
#  TUNING LOADER  (written by SIGNAL_ANALYZER — applied each cycle)
# ══════════════════════════════════════════════════════════════
_tuning_mtime = 0.0
_tuning: dict = {}

def load_tuning() -> dict:
    """Re-read BOT_TUNING.json only when file changes. Returns active tuning dict."""
    global _tuning_mtime, _tuning
    try:
        mtime = os.path.getmtime(TUNING_PATH)
        if mtime != _tuning_mtime:
            with open(TUNING_PATH, "r") as f:
                _tuning = json.load(f)
            _tuning_mtime = mtime
    except (FileNotFoundError, json.JSONDecodeError):
        _tuning = {}
    return _tuning

def tuning_confidence_threshold() -> int:
    return int(load_tuning().get("confidence_threshold", CONFIDENCE_THRESHOLD))

def tuning_excluded_zones() -> list:
    return load_tuning().get("excluded_zones", [])

def tuning_excluded_patterns() -> list:
    return load_tuning().get("excluded_patterns", [])

def tuning_direction_multiplier(direction: str) -> float:
    t = load_tuning()
    if direction == "CE":
        return float(t.get("ce_multiplier", 1.0))
    if direction == "PE":
        return float(t.get("pe_multiplier", 1.0))
    return 1.0

FIB_RATIOS = [0.0, 0.236, 0.382, 0.500, 0.618, 0.786, 1.0]
FIB_NAMES  = ["SL", "23.6%", "38.2%", "50%", "61.8%", "78.6%", "SH"]

# ══════════════════════════════════════════════════════════════
#  ANSI BASE  (identical to PREMIUM_DIRECTION_TRACKER & FIBONACCI_TREND_ANALYZER)
# ══════════════════════════════════════════════════════════════
class C:
    RESET    = "\033[0m"
    BOLD     = "\033[1m"
    DIM      = "\033[2m"
    RED      = "\033[91m"
    GREEN    = "\033[92m"
    YELLOW   = "\033[93m"
    BLUE     = "\033[94m"
    MAGENTA  = "\033[95m"
    CYAN     = "\033[96m"
    WHITE    = "\033[97m"
    B_RED    = "\033[1;91m"
    B_GREEN  = "\033[1;92m"
    B_YELLOW = "\033[1;93m"
    B_CYAN   = "\033[1;96m"
    B_WHITE  = "\033[1;97m"
    ORANGE   = "\033[38;5;214m"
    B_ORANGE = "\033[1;38;5;214m"
    LIME     = "\033[38;5;154m"
    B_LIME   = "\033[1;38;5;154m"

# ══════════════════════════════════════════════════════════════
#  THEMES  (4 choices — set at startup)
# ══════════════════════════════════════════════════════════════
THEMES = {
    "1": {
        "_name":        "Groww Classic",
        "_desc":        "green / red / cyan  —  matches PDT & Fibo",
        "BULLISH":      "#00ff00",
        "BEARISH":      "#ff4444",
        "NEUTRAL":      "#ffff00",
        "BORDER":       "#00ffff",
        "HEADER":       "#ffffff",
        "SPOT_VAL":     "#ffff00",
        "DIM_TEXT":     "#666666",
        "GOLDEN_ZONE":  "#ffcc00",
        "SCORE_HIGH":   "#00ff00",
        "SCORE_MID":    "#ffff00",
        "SCORE_LOW":    "#ff4444",
        "ACTION_CE":    "#00ff00",
        "ACTION_PE":    "#ff4444",
        "ACTION_WAIT":  "#ffff00",
        "FIB_ABOVE":    "#ff4444",
        "FIB_BELOW":    "#00ff00",
        "FIB_SWING":    "#00ffff",
        "SECTION_HDR":  "#ffff00",
    },
    "2": {
        "_name":        "Amber Night",
        "_desc":        "orange / red / gold  —  warm dark theme",
        "BULLISH":      "#ffaa00",
        "BEARISH":      "#ff4444",
        "NEUTRAL":      "#ffee88",
        "BORDER":       "#ffcc44",
        "HEADER":       "#ffeecc",
        "SPOT_VAL":     "#ffdd88",
        "DIM_TEXT":     "#886644",
        "GOLDEN_ZONE":  "#ffcc00",
        "SCORE_HIGH":   "#ffaa00",
        "SCORE_MID":    "#ffdd66",
        "SCORE_LOW":    "#ff4444",
        "ACTION_CE":    "#ffaa00",
        "ACTION_PE":    "#ff4444",
        "ACTION_WAIT":  "#ffdd88",
        "FIB_ABOVE":    "#ff8844",
        "FIB_BELOW":    "#ffaa00",
        "FIB_SWING":    "#ffcc44",
        "SECTION_HDR":  "#ffcc44",
    },
    "3": {
        "_name":        "Ocean Blue",
        "_desc":        "blue / pink / cyan  —  cool dark theme",
        "BULLISH":      "#44aaff",
        "BEARISH":      "#ff4488",
        "NEUTRAL":      "#aaddff",
        "BORDER":       "#00ccff",
        "HEADER":       "#cceeff",
        "SPOT_VAL":     "#00ccff",
        "DIM_TEXT":     "#446688",
        "GOLDEN_ZONE":  "#ffcc44",
        "SCORE_HIGH":   "#44aaff",
        "SCORE_MID":    "#aaddff",
        "SCORE_LOW":    "#ff4488",
        "ACTION_CE":    "#44aaff",
        "ACTION_PE":    "#ff4488",
        "ACTION_WAIT":  "#aaddff",
        "FIB_ABOVE":    "#ff4488",
        "FIB_BELOW":    "#44aaff",
        "FIB_SWING":    "#00ccff",
        "SECTION_HDR":  "#aaddff",
    },
    "4": {
        "_name":        "Minimal",
        "_desc":        "no bright colors  —  easy on the eyes",
        "BULLISH":      "#aaaaaa",
        "BEARISH":      "#888888",
        "NEUTRAL":      "#999999",
        "BORDER":       "#888888",
        "HEADER":       "#cccccc",
        "SPOT_VAL":     "#ffffff",
        "DIM_TEXT":     "#555555",
        "GOLDEN_ZONE":  "#bbbbbb",
        "SCORE_HIGH":   "#aaaaaa",
        "SCORE_MID":    "#888888",
        "SCORE_LOW":    "#666666",
        "ACTION_CE":    "#cccccc",
        "ACTION_PE":    "#888888",
        "ACTION_WAIT":  "#999999",
        "FIB_ABOVE":    "#888888",
        "FIB_BELOW":    "#aaaaaa",
        "FIB_SWING":    "#cccccc",
        "SECTION_HDR":  "#999999",
    },
}

COLOR_CONFIG: dict = {}   # filled at startup by select_theme()

def _hex_to_ansi(hex_color: str) -> str:
    """#rrggbb → bold 24-bit true-color escape  (matches existing bots)."""
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"\033[1;38;2;{r};{g};{b}m"
    except Exception:
        return C.WHITE

def _cc(key: str) -> str:
    return _hex_to_ansi(COLOR_CONFIG.get(key, "#ffffff"))

# ── BIAS MAP  (same labels as FIBONACCI_TREND_ANALYZER) ───────
BIAS_MAP = {
     3: ("⬆⬆ STRONG BULLISH", "BULLISH"),
     2: ("⬆  BULLISH",         "BULLISH"),
     1: ("↗  MILD BULLISH",    "BULLISH"),
     0: ("→  NEUTRAL",          "NEUTRAL"),
    -1: ("↘  MILD BEARISH",    "BEARISH"),
    -2: ("⬇  BEARISH",         "BEARISH"),
    -3: ("⬇⬇ STRONG BEARISH",  "BEARISH"),
}

def _5m_label(s):
    if s >=  2: return ("⬆⬆ STRONG UP",  "BULLISH")
    if s >=  1: return ("↗  LEAN UP",     "BULLISH")
    if s <= -2: return ("⬇⬇ STRONG DOWN", "BEARISH")
    if s <= -1: return ("↘  LEAN DOWN",   "BEARISH")
    return              ("→  MIXED",       "NEUTRAL")

def _prem_label(s):
    if s > 0: return ("↑  CE > PE",  "BULLISH")
    if s < 0: return ("↓  PE > CE",  "BEARISH")
    return            ("→  Neutral",  "NEUTRAL")

# ── display helpers ────────────────────────────────────────────
_ANSI = re.compile(r'\x1b\[[0-9;]*m')

def vlen(s):
    return len(_ANSI.sub('', s))

def rpad(s, w):
    return s + ' ' * max(0, w - vlen(s))

def score_bar(s, max_s, width=10):
    filled = min(width, int(abs(s) / max_s * width))
    bar    = "█" * filled + "░" * (width - filled)
    ratio  = abs(s) / max_s
    key    = "SCORE_HIGH" if ratio >= 0.66 else ("SCORE_MID" if ratio >= 0.33 else "SCORE_LOW")
    return f"{_cc(key)}[{bar}]{C.RESET}"

def hdiv():
    return f"{_cc('BORDER')}{'═' * 66}{C.RESET}"

def sdiv(label=""):
    col = _cc("SECTION_HDR")
    if label:
        dashes = "─" * (62 - len(label))
        return f"  {col}─── {label} {dashes}{C.RESET}"
    return f"  {col}{'─' * 64}{C.RESET}"


# ══════════════════════════════════════════════════════════════
#  GROWW INIT
# ══════════════════════════════════════════════════════════════
from groww_token import get_access_token as get_cached_access_token


def init_groww():
    token = get_cached_access_token(API_KEY, TOTP_SECRET)
    return GrowwAPI(token), token


# ══════════════════════════════════════════════════════════════
#  INSTRUMENTS
# ══════════════════════════════════════════════════════════════
def _download_csv(session):
    try:
        r = session.get("https://growwapi-assets.groww.in/instruments/instrument.csv", timeout=30)
        r.raise_for_status()
        with open(CSV_PATH, "wb") as f:
            f.write(r.content)
    except Exception as e:
        print(f"⚠️  CSV download failed: {e}")

def load_instruments(session):
    needs = not os.path.exists(CSV_PATH)
    if not needs:
        age   = datetime.now() - datetime.fromtimestamp(os.path.getmtime(CSV_PATH))
        needs = age > timedelta(days=1)
    if needs:
        _download_csv(session)
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))

def get_expiry_dates(instruments, index_name):
    expiries = {i.get("expiry_date", "").strip()
                for i in instruments
                if i.get("underlying_symbol", "").upper() == index_name.upper()}
    today  = datetime.now().date()
    future = sorted(e for e in expiries if e and datetime.strptime(e, "%Y-%m-%d").date() >= today)
    return (future[0] if future else None), (future[1] if len(future) > 1 else None)

def find_atm_instruments(instruments, index_name, expiry, spot, step):
    atm = round(spot / step) * step
    ce_inst = pe_inst = None
    for item in instruments:
        if (item.get("underlying_symbol", "").upper() == index_name.upper()
                and item.get("expiry_date", "").strip() == expiry):
            try:
                strike = float(item.get("strike_price", 0))
            except ValueError:
                continue
            if abs(strike - atm) < 0.01:
                otype = item.get("instrument_type", "").upper()
                if otype == "CE":   ce_inst = item
                elif otype == "PE": pe_inst = item
    return ce_inst, pe_inst


# ══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════
def fetch_candles(groww_client, index_name, interval, hours_back):
    cfg = INDICES.get(index_name, INDICES["NIFTY"])
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(hours=hours_back)
    exc = groww_client.EXCHANGE_NSE if cfg["exchange"] == "NSE" else groww_client.EXCHANGE_BSE
    for sym in cfg["sym_candidates"]:
        try:
            r = groww_client.get_historical_candles(
                groww_symbol=sym, exchange=exc, segment="CASH",
                start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=interval,
            )
            if r and r.get("candles") and len(r["candles"]) >= 5:
                return [{"ts": c[0], "open": float(c[1]), "high": float(c[2]),
                         "low": float(c[3]), "close": float(c[4])} for c in r["candles"]]
        except Exception:
            continue
    return []

def get_spot(index_name, expiry, token, session):
    exchange = "BSE" if "SENSEX" in index_name.upper() else "NSE"
    url  = (f"https://api.groww.in/v1/option-chain/exchange/{exchange}"
            f"/underlying/{index_name}?expiry_date={expiry}")
    hdrs = {"Accept": "application/json",
            "Authorization": f"Bearer {token}", "X-API-VERSION": "1.0"}
    try:
        r = session.get(url, headers=hdrs, timeout=8)
        if r.status_code == 200:
            ltp = r.json().get("payload", {}).get("underlying_ltp")
            if ltp:
                return float(ltp)
    except Exception:
        pass
    return None

def get_ltp_pair(ce_inst, pe_inst, token, session):
    syms = []
    for inst in (ce_inst, pe_inst):
        if inst:
            ts = inst.get("trading_symbol")
            ex = inst.get("exchange", "NSE").upper()
            if ts:
                syms.append(f"{ex}_{ts}")
    if len(syms) != 2:
        return None, None
    url  = (f"https://api.groww.in/v1/live-data/ltp"
            f"?segment=FNO&exchange_symbols={syms[0]}&exchange_symbols={syms[1]}")
    hdrs = {"Accept": "application/json",
            "Authorization": f"Bearer {token}", "X-API-VERSION": "1.0"}
    try:
        r = session.get(url, headers=hdrs, timeout=5)
        if r.status_code == 200:
            p    = r.json().get("payload", {})
            ce_v = p.get(syms[0])
            pe_v = p.get(syms[1])
            return (float(ce_v) if ce_v else None), (float(pe_v) if pe_v else None)
    except Exception:
        pass
    return None, None


# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════
def calc_rsi(candles, period=14):
    if len(candles) < period + 1:
        return 50.0
    c      = [x["close"] for x in candles]
    gains  = [max(c[i]-c[i-1], 0.0) for i in range(1, len(c))]
    losses = [max(c[i-1]-c[i], 0.0) for i in range(1, len(c))]
    ag = sum(gains[-period:])  / period
    al = sum(losses[-period:]) / period
    return round(100 - 100 / (1 + ag / al), 1) if al else 100.0

def detect_swing(candles, window):
    if len(candles) < window * 2 + 1:
        return max(c["high"] for c in candles), min(c["low"] for c in candles)
    sh_pts, sl_pts = [], []
    for i in range(window, len(candles) - window):
        nb = [j for j in range(i-window, i+window+1) if j != i]
        if all(candles[i]["high"] > candles[j]["high"] for j in nb):
            sh_pts.append(candles[i]["high"])
        if all(candles[i]["low"]  < candles[j]["low"]  for j in nb):
            sl_pts.append(candles[i]["low"])
    sh = sh_pts[-1] if sh_pts else max(c["high"] for c in candles[-20:])
    sl = sl_pts[-1] if sl_pts else min(c["low"]  for c in candles[-20:])
    return sh, sl

def fib_score(price, sl, sh):
    """Position in swing: -3 (breakdown) … 0 (mid) … +3 (breakout)."""
    if sh <= sl: return 0
    if price >= sh: return  3
    if price <= sl: return -3
    pos = (price - sl) / (sh - sl)
    if pos >= 0.786: return  2
    if pos >= 0.618: return  1
    if pos >= 0.500: return  0
    if pos >= 0.382: return -1   # Golden zone — high-probability reversal
    if pos >= 0.236: return -2
    return -3

def fib_zone_name(price, sl, sh):
    if sh <= sl: return "—"
    if price >= sh: return "BREAKOUT ↑ above swing high"
    if price <= sl: return "BREAKDOWN ↓ below swing low"
    pos = (price - sl) / (sh - sl)
    if pos >= 0.786: return "78.6–100%  shallow retrace"
    if pos >= 0.618: return "61.8–78.6%  normal retrace"
    if pos >= 0.500: return "50–61.8%  mid range"
    if pos >= 0.382: return "38.2–50%  GOLDEN ZONE ★"
    if pos >= 0.236: return "23.6–38.2%  deep retrace"
    return "0–23.6%  near swing low"

def fib_levels(sl, sh):
    rng = sh - sl
    return {n: round(sl + rng * r, 1) for n, r in zip(FIB_NAMES, FIB_RATIOS)}

def candle_pattern(candles):
    if len(candles) < 2:
        return 0, "—"
    c, p = candles[-1], candles[-2]
    body  = abs(c["close"] - c["open"])
    rng   = c["high"] - c["low"]
    if rng < 0.01: return 0, "Doji"
    lo_w = min(c["open"], c["close"]) - c["low"]
    hi_w = c["high"] - max(c["open"], c["close"])
    br   = body / rng
    pb   = abs(p["close"] - p["open"])
    if (c["close"] > c["open"] and p["close"] < p["open"]
            and c["open"] <= p["close"] and c["close"] >= p["open"] and body > pb):
        return 1, "Bullish Engulfing"
    if (c["close"] < c["open"] and p["close"] > p["open"]
            and c["open"] >= p["close"] and c["close"] <= p["open"] and body > pb):
        return -1, "Bearish Engulfing"
    if lo_w > body * 2 and hi_w < body and br > 0.1: return  1, "Hammer"
    if hi_w > body * 2 and lo_w < body and br > 0.1: return -1, "Shooting Star"
    if br < 0.1: return 0, "Doji"
    if c["close"] > c["open"] and br > 0.65: return  1, "Strong Bull"
    if c["close"] < c["open"] and br > 0.65: return -1, "Strong Bear"
    return 0, "Normal"

def score_5m(candles_5m):
    last = candles_5m[-8:] if len(candles_5m) >= 8 else candles_5m
    if len(last) < 4: return 0, "not enough data"
    n     = len(last)
    bulls = sum(1 for c in last if c["close"] > c["open"])
    bears = n - bulls
    if bulls >= int(n * 0.75): return  2, f"{bulls}/{n} bull candles"
    if bulls >= int(n * 0.60): return  1, f"{bulls}/{n} bull candles"
    if bears >= int(n * 0.75): return -2, f"{bears}/{n} bear candles"
    if bears >= int(n * 0.60): return -1, f"{bears}/{n} bear candles"
    return 0, f"mixed  {bulls}B / {bears}A"

def premium_flow(ce_prev, pe_prev, ce_curr, pe_curr):
    if None in (ce_prev, pe_prev, ce_curr, pe_curr):
        return 0, "— (first poll)"
    ce_d = ce_curr - ce_prev
    pe_d = pe_curr - pe_prev
    detail = f"CE {'+' if ce_d>=0 else ''}{ce_d:.1f}  PE {'+' if pe_d>=0 else ''}{pe_d:.1f}"
    if ce_d > 0 and ce_d > pe_d: return  1, detail
    if pe_d > 0 and pe_d > ce_d: return -1, detail
    return 0, detail


# ══════════════════════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════
def compute_signal(s1h, s15m, s5m, sprem):
    if s1h <= -2 and s15m >= 2: return "WAIT", 0.0, 0.0
    if s1h >= 2 and s15m <= -2: return "WAIT", 0.0, 0.0
    raw  = s1h * W_1H + s15m * W_15M + s5m * W_5M + sprem * W_PREM
    conf = round(abs(raw) / MAX_TOTAL * 100, 1)
    tentative = "CE" if raw > 0 else "PE"
    # apply per-direction multiplier from SIGNAL_ANALYZER tuning
    conf = round(conf * tuning_direction_multiplier(tentative), 1)
    threshold = tuning_confidence_threshold()
    if conf < threshold:
        return "WAIT", conf, raw
    return tentative, conf, raw

def entry_levels(spot, lvls, direction):
    vals   = sorted(lvls.values())
    sup    = max((v for v in vals if v < spot), default=round(spot - 100, 1))
    res    = min((v for v in vals if v > spot), default=round(spot + 100, 1))
    stop, target = (sup, res) if direction == "CE" else (res, sup)
    d_stop   = abs(spot - stop)
    d_target = abs(target - spot)
    rr = round(d_target / d_stop, 1) if d_stop > 0 else 0.0
    return stop, target, rr


# ══════════════════════════════════════════════════════════════
#  DISPLAY
# ══════════════════════════════════════════════════════════════
def _fib_level_row(label, price, spot):
    """Single fib-level row with distance annotation — matches FIBO style."""
    dist     = price - spot
    dist_s   = f"{dist:+.0f} pts"
    is_above = price > spot
    color    = _cc("FIB_SWING") if label in ("SL", "SH") else (
               _cc("FIB_ABOVE") if is_above else _cc("FIB_BELOW"))
    golden   = f"  {_cc('GOLDEN_ZONE')}★ GOLDEN ZONE{C.RESET}" if label in ("38.2%", "50%") else ""
    return f"  {color}{label:<9}{C.RESET}  {price:>10,.1f}   ({dist_s:>9}){golden}"

def print_signal(
    index_name, spot, expiry,
    s1h, rsi1h, sh1h, sl1h,
    s15m, rsi15m, sh15m, sl15m, pat_name,
    s5m, note_5m,
    sprem, note_prem,
    ce_ltp, pe_ltp, atm_strike,
    direction, confidence,
    stop, target, rr, qty,
    no_trade_reason,
    zone_name, lvls,
):
    os.system("clear")
    now     = datetime.now()
    now_str = now.strftime("%a %d %b %Y   %H:%M:%S")
    mkt_open = NO_TRADE_BEFORE_MIN <= now.hour * 60 + now.minute < NO_TRADE_AFTER_MIN
    mkt_lbl  = (f"{_cc('BULLISH')}● MARKET OPEN{C.RESET}"
                if mkt_open else f"{_cc('NEUTRAL')}○ MARKET CLOSED{C.RESET}")

    # ── HEADER ────────────────────────────────────────────────
    print()
    print(hdiv())
    print(f"  {_cc('HEADER')}{C.BOLD}MASTER SIGNAL BOT{C.RESET}"
          f"  ·  {_cc('BORDER')}{index_name}{C.RESET}"
          f"  ·  Spot {_cc('SPOT_VAL')}₹{spot:,.2f}{C.RESET}"
          f"  ·  {mkt_lbl}")
    t = load_tuning()
    if t:
        thr = t.get("confidence_threshold", CONFIDENCE_THRESHOLD)
        ce_m = t.get("ce_multiplier", 1.0)
        pe_m = t.get("pe_multiplier", 1.0)
        excl = len(t.get("excluded_zones", [])) + len(t.get("excluded_patterns", []))
        tune_str = (f"  {_cc('GOLDEN_ZONE')}⚙ Analyzer tuning active:"
                    f"  threshold={thr}%"
                    f"  CE×{ce_m:.2f}  PE×{pe_m:.2f}"
                    f"  {excl} exclusions{C.RESET}")
        print(tune_str)
    print(f"  {_cc('DIM_TEXT')}{now_str}   Theme: {COLOR_CONFIG['_name']}{C.RESET}")
    print(hdiv())

    # ── SIGNAL COMPONENTS ────────────────────────────────────
    print(sdiv("SIGNAL COMPONENTS"))
    print()

    def component_row(layer, bias_label, bias_color_key, extra, sval, max_s):
        col   = _cc(bias_color_key)
        label_str = rpad(f"{col}{bias_label}{C.RESET}", 36)
        return (f"  {_cc('DIM_TEXT')}{layer:<13}{C.RESET}"
                f"  {label_str}"
                f"  {score_bar(sval, max_s)}"
                f"  {_cc('DIM_TEXT')}{extra}{C.RESET}")

    # clamp float s15m to nearest integer for BIAS_MAP lookup
    s15m_key = max(-3, min(3, round(s15m)))
    lbl1h,  col1h  = BIAS_MAP.get(s1h,       ("→  NEUTRAL", "NEUTRAL"))
    lbl15m, col15m = BIAS_MAP.get(s15m_key,  ("→  NEUTRAL", "NEUTRAL"))
    lbl5m,  col5m  = _5m_label(s5m)
    lblpr,  colpr  = _prem_label(sprem)

    print(component_row("1H  Trend",   lbl1h,  col1h,  f"RSI {rsi1h}   {sl1h:,.0f}–{sh1h:,.0f}",  s1h,  3))
    print(component_row("15M Setup",   lbl15m, col15m, f"RSI {rsi15m}   [{pat_name}]",              s15m, 3))
    print(component_row("5M  Confirm", lbl5m,  col5m,  note_5m,                                     s5m,  2))
    print(component_row("Premium",     lblpr,  colpr,  note_prem,                                   sprem, 1))

    # ── FIBONACCI LEVELS ────────────────────────────────────
    print()
    print(sdiv(f"15M FIBONACCI  ({sl15m:,.0f} — {sh15m:,.0f})"))
    print()

    # build ordered level list, insert SPOT marker at the right place
    level_rows = [
        ("SH",    lvls["SH"]),
        ("78.6%", lvls["78.6%"]),
        ("61.8%", lvls["61.8%"]),
        ("50%",   lvls["50%"]),
        ("38.2%", lvls["38.2%"]),
        ("23.6%", lvls["23.6%"]),
        ("SL",    lvls["SL"]),
    ]
    spot_printed = False
    for label, price in level_rows:
        if not spot_printed and price <= spot:
            spot_line = (f"  {_cc('SPOT_VAL')}{'─'*3} SPOT ₹{spot:,.2f} {'─'*38}{C.RESET}")
            print(spot_line)
            spot_printed = True
        print(_fib_level_row(label, price, spot))
    if not spot_printed:
        print(f"  {_cc('SPOT_VAL')}{'─'*3} SPOT ₹{spot:,.2f} {'─'*38}{C.RESET}")

    print()
    zone_col = _cc("GOLDEN_ZONE") if "GOLDEN" in zone_name else _cc("DIM_TEXT")
    print(f"  {_cc('DIM_TEXT')}Fib Zone  {C.RESET}{zone_col}{zone_name}{C.RESET}")
    if ce_ltp and pe_ltp:
        print(f"  {_cc('DIM_TEXT')}ATM {atm_strike}   "
              f"CE ₹{ce_ltp:.2f}  ·  PE ₹{pe_ltp:.2f}  ·  Expiry {expiry}{C.RESET}")

    # ── THE SIGNAL ────────────────────────────────────────────
    print()
    print(hdiv())

    conf_filled = min(20, int(confidence / 5))
    conf_bar    = "█" * conf_filled + "░" * (20 - conf_filled)

    if no_trade_reason:
        wait_col = _cc("ACTION_WAIT")
        print(f"\n  {wait_col}⬛  NO TRADE{C.RESET}")
        print(f"  {_cc('DIM_TEXT')}Confidence  [{conf_bar}]  {confidence:.0f}%{C.RESET}")
        print(f"  {_cc('DIM_TEXT')}Reason      {no_trade_reason}{C.RESET}\n")

    elif direction == "CE":
        ce_col = _cc("ACTION_CE")
        rr_col = _cc("SCORE_HIGH") if rr >= 1.5 else _cc("NEUTRAL")
        rr_tag = "✅ Good R:R" if rr >= 1.5 else "⚠️  consider tighter entry"
        print(f"\n  {ce_col}🟢  ENTER CE{C.RESET}")
        print(f"  {ce_col}Confidence  [{conf_bar}]  {confidence:.0f}%{C.RESET}")
        print()
        print(f"  {_cc('DIM_TEXT')}Entry   {C.RESET}₹{spot:,.2f}")
        print(f"  {_cc('FIB_BELOW')}Stop    {C.RESET}₹{stop:,.2f}  "
              f"{_cc('DIM_TEXT')}({abs(spot-stop):.0f} pts){C.RESET}")
        print(f"  {_cc('FIB_ABOVE')}Target  {C.RESET}₹{target:,.2f}  "
              f"{_cc('DIM_TEXT')}({abs(target-spot):.0f} pts){C.RESET}")
        print(f"  {rr_col}R:R  1 : {rr}   {rr_tag}{C.RESET}")
        print(f"  {_cc('DIM_TEXT')}Qty  {qty} lots  ·  Expiry {expiry}{C.RESET}\n")

    else:  # PE
        pe_col = _cc("ACTION_PE")
        rr_col = _cc("SCORE_HIGH") if rr >= 1.5 else _cc("NEUTRAL")
        rr_tag = "✅ Good R:R" if rr >= 1.5 else "⚠️  consider tighter entry"
        print(f"\n  {pe_col}🔴  ENTER PE{C.RESET}")
        print(f"  {pe_col}Confidence  [{conf_bar}]  {confidence:.0f}%{C.RESET}")
        print()
        print(f"  {_cc('DIM_TEXT')}Entry   {C.RESET}₹{spot:,.2f}")
        print(f"  {_cc('FIB_ABOVE')}Stop    {C.RESET}₹{stop:,.2f}  "
              f"{_cc('DIM_TEXT')}({abs(spot-stop):.0f} pts){C.RESET}")
        print(f"  {_cc('FIB_BELOW')}Target  {C.RESET}₹{target:,.2f}  "
              f"{_cc('DIM_TEXT')}({abs(target-spot):.0f} pts){C.RESET}")
        print(f"  {rr_col}R:R  1 : {rr}   {rr_tag}{C.RESET}")
        print(f"  {_cc('DIM_TEXT')}Qty  {qty} lots  ·  Expiry {expiry}{C.RESET}\n")

    print(hdiv())


# ══════════════════════════════════════════════════════════════
#  STARTUP SELECTION
# ══════════════════════════════════════════════════════════════
def select_theme():
    global COLOR_CONFIG
    env_theme = os.environ.get("BOT_THEME", "").strip()
    if env_theme and env_theme in THEMES:
        COLOR_CONFIG = THEMES[env_theme].copy()
        print(f"  Theme (env): {COLOR_CONFIG['_name']}")
        return
    print()
    print(f"  {C.CYAN}{'─'*40}{C.RESET}")
    print(f"  {C.BOLD}{C.WHITE}  Select color theme{C.RESET}")
    print(f"  {C.CYAN}{'─'*40}{C.RESET}")
    for k, t in THEMES.items():
        print(f"  {C.DIM}{k}.{C.RESET}  {C.WHITE}{t['_name']:<16}{C.RESET}  "
              f"{C.DIM}{t['_desc']}{C.RESET}")
    print(f"  {C.CYAN}{'─'*40}{C.RESET}")
    choice = input(f"\n  Enter 1–4  [default 1]: ").strip()
    COLOR_CONFIG = THEMES.get(choice, THEMES["1"]).copy()
    print(f"  {C.DIM}Theme: {COLOR_CONFIG['_name']}{C.RESET}")

def select_index():
    env_index = os.environ.get("BOT_INDEX", "").strip().upper()
    if env_index in ("NIFTY", "SENSEX"):
        print(f"  Index (env): {env_index}")
        return env_index
    print()
    print(f"  {C.CYAN}{'─'*40}{C.RESET}")
    print(f"  {C.BOLD}{C.WHITE}  Select index{C.RESET}")
    print(f"  {C.CYAN}{'─'*40}{C.RESET}")
    print(f"  {C.DIM}1.{C.RESET}  NIFTY   (lot 20)")
    print(f"  {C.DIM}2.{C.RESET}  SENSEX  (lot 50)")
    print(f"  {C.CYAN}{'─'*40}{C.RESET}")
    choice = input(f"\n  Enter 1 or 2  [default 1]: ").strip()
    return "SENSEX" if choice == "2" else "NIFTY"


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
def _fallback_candle(spot):
    return [{"ts": 0, "open": spot, "high": spot+1, "low": spot-1, "close": spot}]

def main():
    os.system("clear")
    print(f"\n  {C.BOLD}{C.CYAN}━━━  MASTER SIGNAL BOT  ━━━{C.RESET}\n")

    select_theme()
    index_name = select_index()
    cfg = INDICES[index_name]

    session = requests.Session()
    groww_client, token = init_groww()

    instruments = load_instruments(session)
    if not instruments:
        print("❌ Could not load instruments"); return

    expiry, _ = get_expiry_dates(instruments, index_name)
    if not expiry:
        print(f"❌ No upcoming expiry for {index_name}"); return

    print(f"\n  {_cc('BULLISH')}✅ {index_name}  ·  Expiry {expiry}  ·  Refresh {REFRESH_SEC}s{C.RESET}")
    print(f"  {_cc('DIM_TEXT')}Loading first signal...{C.RESET}\n")

    # ── Signal log (JSON lines, one per cycle — read by SIGNAL_ANALYZER) ──
    _log_dir = os.path.join(PROJECT_ROOT, "logs", "master_signal")
    os.makedirs(_log_dir, exist_ok=True)
    _log_path = os.path.join(_log_dir,
                             f"Master_Signal_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
    _log_file = open(_log_path, "a", buffering=1)

    prev_ce_ltp = prev_pe_ltp = None

    while True:
        try:
            t0       = time.time()
            now      = datetime.now()
            now_mins = now.hour * 60 + now.minute

            # ── Candles ───────────────────────────────────
            c1h  = fetch_candles(groww_client, index_name, "1hour",    hours_back=48)
            c15m = fetch_candles(groww_client, index_name, "15minute", hours_back=26)
            c5m  = fetch_candles(groww_client, index_name, "5minute",  hours_back=8)

            spot = get_spot(index_name, expiry, token, session)
            if not spot and c15m:
                spot = c15m[-1]["close"]
            if not spot:
                print("⚠️  No spot price — retry in 15s"); time.sleep(15); continue

            atm_strike = round(spot / cfg["step"]) * cfg["step"]
            ce_inst, pe_inst = find_atm_instruments(instruments, index_name, expiry,
                                                    spot, cfg["step"])
            ce_ltp, pe_ltp   = get_ltp_pair(ce_inst, pe_inst, token, session)

            # ── Indicators ───────────────────────────────
            src1h  = c1h  or _fallback_candle(spot)
            src15m = c15m or _fallback_candle(spot)

            sh1h,  sl1h  = detect_swing(src1h,  window=3)
            sh15m, sl15m = detect_swing(src15m, window=2)

            rsi1h  = calc_rsi(src1h)
            rsi15m = calc_rsi(src15m)

            s1h  = fib_score(spot, sl1h,  sh1h)
            s15m = fib_score(spot, sl15m, sh15m)

            # RSI dampening at extremes
            if rsi1h  >= 75 and s1h  > 0: s1h  = max(s1h  - 1, 0)
            if rsi1h  <= 25 and s1h  < 0: s1h  = min(s1h  + 1, 0)
            if rsi15m >= 75 and s15m > 0: s15m = max(s15m - 1, 0)
            if rsi15m <= 25 and s15m < 0: s15m = min(s15m + 1, 0)

            pat_sig, pat_name = candle_pattern(src15m)
            s15m = max(-3.0, min(3.0, s15m + pat_sig * 0.5))

            s5m,   note_5m   = score_5m(c5m)
            sprem, note_prem = premium_flow(prev_ce_ltp, prev_pe_ltp, ce_ltp, pe_ltp)
            prev_ce_ltp, prev_pe_ltp = ce_ltp, pe_ltp

            # ── Signal ───────────────────────────────────
            direction, confidence, raw = compute_signal(s1h, s15m, s5m, sprem)
            zone_name = fib_zone_name(spot, sl15m, sh15m)
            lvls      = fib_levels(sl15m, sh15m)

            # ── Tuning gates (SIGNAL_ANALYZER auto-corrections) ──
            _excl_zones    = tuning_excluded_zones()
            _excl_patterns = tuning_excluded_patterns()
            _tuning_block  = None
            if direction in ("CE", "PE") and _excl_zones:
                for ez in _excl_zones:
                    if ez.lower() in zone_name.lower():
                        direction = "WAIT"
                        _tuning_block = f"Zone '{zone_name}' blocked by analyzer"
                        break
            if direction in ("CE", "PE") and _excl_patterns:
                for ep in _excl_patterns:
                    if ep.lower() in pat_name.lower():
                        direction = "WAIT"
                        _tuning_block = f"Pattern '{pat_name}' blocked by analyzer"
                        break

            # ── No-trade gates ────────────────────────────
            no_trade_reason = _tuning_block
            if now_mins < NO_TRADE_BEFORE_MIN:
                no_trade_reason = "Before 9:30 AM — opening noise, wait"
                direction = "WAIT"
            elif now_mins >= NO_TRADE_AFTER_MIN:
                no_trade_reason = "After 3:00 PM — no new positions"
                direction = "WAIT"
            elif direction == "WAIT" and not _tuning_block:
                if raw == 0.0:
                    no_trade_reason = "1h and 15m in direct conflict — wait for alignment"
                else:
                    no_trade_reason = (f"Confidence {confidence:.0f}% below "
                                       f"{tuning_confidence_threshold()}% — signals not aligned")

            # ── Entry levels ──────────────────────────────
            eff_dir = direction if direction in ("CE", "PE") else "CE"
            stop, target, rr = entry_levels(spot, lvls, eff_dir)

            # ── Log cycle to JSON (for SIGNAL_ANALYZER) ──
            _log_file.write(json.dumps({
                "ts":         now.strftime("%Y-%m-%dT%H:%M:%S"),
                "index":      index_name,
                "spot":       round(spot, 2),
                "direction":  direction,
                "confidence": confidence,
                "s1h":        int(s1h),
                "s15m":       round(float(s15m), 2),
                "s5m":        int(s5m),
                "sprem":      int(sprem),
                "rsi1h":      rsi1h,
                "rsi15m":     rsi15m,
                "pattern":    pat_name,
                "zone":       zone_name,
                "stop":       round(stop, 2),
                "target":     round(target, 2),
                "rr":         rr,
                "sh15m":      round(sh15m, 2),
                "sl15m":      round(sl15m, 2),
            }) + "\n")

            # ── Render ────────────────────────────────────
            print_signal(
                index_name, spot, expiry,
                s1h, rsi1h, sh1h, sl1h,
                s15m, rsi15m, sh15m, sl15m, pat_name,
                s5m, note_5m,
                sprem, note_prem,
                ce_ltp, pe_ltp, atm_strike,
                direction, confidence,
                stop, target, rr, cfg["qty"],
                no_trade_reason,
                zone_name, lvls,
            )

            # ── Countdown ─────────────────────────────────
            wait = max(0, REFRESH_SEC - (time.time() - t0))
            for rem in range(int(wait), 0, -1):
                print(f"\r  {_cc('DIM_TEXT')}Next refresh in {rem:2d}s  ·  "
                      f"{now.strftime('%H:%M:%S')}  ·  {index_name}{C.RESET}",
                      end="", flush=True)
                time.sleep(1)
            print()

        except KeyboardInterrupt:
            print(f"\n  {_cc('DIM_TEXT')}Exiting.{C.RESET}")
            break
        except Exception as exc:
            print(f"\n⚠️  Error: {exc}")
            time.sleep(15)


if __name__ == "__main__":
    main()
