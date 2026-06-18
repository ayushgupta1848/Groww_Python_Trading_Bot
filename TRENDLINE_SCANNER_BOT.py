#!/usr/bin/env python3
"""
TRENDLINE_SCANNER_BOT.py
────────────────────────────────────────────────────────────────────────────────
Standalone trendline-based option scanner for NIFTY/SENSEX/BANKNIFTY.

Monitors option premiums using 5-min candles from Groww charting API.
Detects ascending support trendlines and trades two signal types:

  BOUNCE — premium nears ascending support → confirm tick-up → BUY
           Exit: target = last swing high − buffer | trailing SL

  BREAK  — premium breaks below support →
           opposite side (CE↔PE) gets momentum → BUY immediately
           Exit: trailing SL only (no hard target)

API layer uses public Groww web endpoints — no auth token needed.
Simulation mode by default (no real orders placed).
────────────────────────────────────────────────────────────────────────────────
"""

import json, logging, os, sys, time, threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import List, Optional, Tuple
import requests

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG  — edit these before running
# ═══════════════════════════════════════════════════════════════════════════════
CONFIG: dict = {
    # ── Instrument ─────────────────────────────────────────────────────────────
    "index":              "NIFTY",    # NIFTY | BANKNIFTY | SENSEX
    "exchange":           "NSE",      # NSE for NIFTY/BANKNIFTY, BSE for SENSEX
    "expiry_date":        "2026-06-23",  # YYYY-MM-DD of weekly expiry
    "strike_step":        50,         # NIFTY=50, BANKNIFTY=100, SENSEX=100
    "scan_range":         20,         # fetch ATM ± N strikes (wide net)
    "premium_min":        85.0,       # only trade options with LTP >= this
    "premium_max":        200.0,      # only trade options with LTP <= this

    # ── Candles ────────────────────────────────────────────────────────────────
    "candle_interval":    5,          # candle size in minutes
    "structural_refresh": 30,        # seconds between candle refreshes (5 min)

    # ── Live monitoring ────────────────────────────────────────────────────────
    "ltp_poll_sec":       15,         # poll interval for live prices

    # ── Trendline detection ────────────────────────────────────────────────────
    "pivot_lookback":     3,          # candles each side to confirm swing pivot
    "min_pivots":         2,          # need ≥N ascending lows to form trendline

    # ── Signal thresholds ──────────────────────────────────────────────────────
    "proximity_pts":      6.0,        # BOUNCE: max distance from support to trigger
    "break_pts":          3.0,        # BREAK: min pts below support to count as break

    # ── Confirmation windows ───────────────────────────────────────────────────
    "bounce_confirm_pts": 2.0,        # price must tick up N pts during window
    "bounce_confirm_sec": 25,         # window duration in seconds
    "break_confirm_pts":  1.5,        # opposite side must tick up N pts
    "break_confirm_sec":  15,         # shorter window (break moves are fast)

    # ── BOUNCE exit params ─────────────────────────────────────────────────────
    "target_buffer":      2.0,        # target = last_swing_high − N
    "trendline_sl_buf":   3.0,        # initial SL = trendline_support − N
    "bounce_trail_act":   5.0,        # activate trailing after +N pts profit
    "bounce_trail_by":    4.0,        # trail distance once active

    # ── BREAK exit params ──────────────────────────────────────────────────────
    "break_initial_sl":   5.0,        # fixed initial SL before trailing activates
    "break_trail_act":    4.0,        # activate trailing after +N pts profit
    "break_trail_by":     3.0,        # tighter trail (break plays reverse fast)

    # ── Trade params ───────────────────────────────────────────────────────────
    "lots":               18,
    "sim":                True,       # True = simulation only, False = live orders

    # ── Trendline type toggles (control which structures are built + signals active) ─
    "tl_ascending_enabled":    True,   # ascending support from lows  → BOUNCE + BREAK signals
    "tl_descending_enabled":   False,  # descending resist from highs → BREAKOUT signal
    "tl_horizontal_enabled":   False,  # horizontal zone              → HORIZ_BOUNCE signal

    # ── Optional signal quality filters (all OFF by default — toggle from UI) ─
    "spot_confirm_enabled":    False,  # require NIFTY spot trendline to match option direction
    "volume_confirm_enabled":  False,  # require current candle volume > N× last-5-bar avg
    "volume_confirm_mult":     1.3,    # volume surge multiplier
    "pct_confirm_enabled":     False,  # use % move instead of fixed pts for BOUNCE confirm
    "bounce_confirm_pct":      0.8,    # % of LTP required (used when pct_confirm_enabled=True)

    # ── API ────────────────────────────────────────────────────────────────────
    "device_id":  "8cea1d25-588a-5eff-9699-5e7fd20a6ca9",  # from HAR
    "req_timeout": 8,
}

LOT_SIZES   = {"NIFTY": 65, "BANKNIFTY": 15, "FINNIFTY": 40, "SENSEX": 20, "BANKEX": 15}
MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)
IST_OFFSET   = 19800  # seconds = UTC+5:30

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
os.makedirs("logs/trendline_bot", exist_ok=True)
os.makedirs("logs/trade_history", exist_ok=True)

_log_file = f"logs/trendline_bot/TrendlineBot_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("trendline_bot")
# Silence urllib3 connection-pool noise
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# ═══════════════════════════════════════════════════════════════════════════════
# EXTERNAL CONFIG  — dashboard writes trendline_config.json to override defaults
# ═══════════════════════════════════════════════════════════════════════════════
_TRENDLINE_CFG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trendline_config.json")
_CHART_DATA_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trendline_chart_data.json")

def _load_external_config():
    """Override CONFIG with values from trendline_config.json if present."""
    if not os.path.exists(_TRENDLINE_CFG_FILE):
        return
    try:
        with open(_TRENDLINE_CFG_FILE) as f:
            ext = json.load(f)
        for key in (
            "premium_min", "premium_max", "lots", "expiry_date",
            "tl_ascending_enabled", "tl_descending_enabled", "tl_horizontal_enabled",
            "spot_confirm_enabled",
            "volume_confirm_enabled", "volume_confirm_mult",
            "pct_confirm_enabled", "bounce_confirm_pct",
        ):
            if key in ext:
                CONFIG[key] = ext[key]
    except Exception:
        pass

_load_external_config()

# ═══════════════════════════════════════════════════════════════════════════════
# SIGNALS STATUS FILE  — written to .trendline_signals.json for dashboard
# ═══════════════════════════════════════════════════════════════════════════════
_SIGNALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trendline_signals.json")
_signals_log: list = []   # max 30 entries

def _write_signals_file(active_trade_info=None):
    """Write current signals + active trade to .trendline_signals.json for dashboard."""
    today = datetime.now().strftime("%Y-%m-%d")
    hist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             f"logs/trade_history/trendline_{today}.jsonl")
    trades_today, wins, losses, total_pnl = 0, 0, 0, 0.0
    if os.path.exists(hist_path):
        with open(hist_path) as f:
            for line in f:
                try:
                    t = json.loads(line.strip())
                    trades_today += 1
                    p = t.get("pnl", 0)
                    total_pnl += p
                    if p > 0: wins += 1
                    elif p < 0: losses += 1
                except Exception: pass
    try:
        with open(_SIGNALS_FILE, "w") as f:
            json.dump({
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "active_trade": active_trade_info,
                "signals": _signals_log[-30:],
                "stats": {"trades": trades_today, "wins": wins, "losses": losses, "pnl": round(total_pnl, 2)}
            }, f, indent=2)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════════════
# TRENDLINE ANCHOR LOGGING + CHART DATA EXPORT
# ═══════════════════════════════════════════════════════════════════════════════
def _pivot_ist(pivot, candles: list) -> str:
    """Format pivot as IST time + price — used in anchor log lines."""
    try:
        ts = candles[pivot.idx]["ts"]
        t  = datetime.utcfromtimestamp(ts + IST_OFFSET).strftime("%H:%M")
        return f"{t}@₹{pivot.price:.2f}"
    except Exception:
        return f"idx{pivot.idx}@₹{pivot.price:.2f}"


def _log_tl_anchors(label: str, tl: "TrendlineState", candles: list):
    """Print the two anchor pivot points of a trendline when it is first detected."""
    if not tl.valid or len(tl.pivots) < 2:
        return
    p1, p2 = tl.pivots[-2], tl.pivots[-1]
    log.info(f"     ↳ {label:18s} ({_pivot_ist(p1, candles)}) → ({_pivot_ist(p2, candles)})  "
             f"project=₹{tl.support:.2f}  slope={tl.slope:+.3f}/bar")


def _tl_anchor_dict(tl: "TrendlineState") -> Optional[dict]:
    """Serialise a TrendlineState into a dict for chart data export."""
    if not tl.valid or len(tl.pivots) < 2:
        return None
    p1, p2 = tl.pivots[-2], tl.pivots[-1]
    return {
        "p1":       {"idx": p1.idx, "price": p1.price},
        "p2":       {"idx": p2.idx, "price": p2.price},
        "projected": tl.support,
        "slope":     tl.slope,
    }


def _write_chart_data(watch_list: list):
    """Write candle + trendline anchor data for all structured instruments to JSON.
    Read by the dashboard to render mini charts.
    Skips write if no instruments have live candles (market closed) so the last
    good snapshot from trading hours is preserved for after-hours chart viewing."""
    cfg = CONFIG
    instruments = []
    for inst in watch_list:
        if not inst.candles_today and inst.ltp <= 0:
            continue
        tls = []
        asc_d = _tl_anchor_dict(inst.tl)
        if asc_d:
            tls.append({"type": "ASC_SUPPORT", "color": "#00c853", **asc_d})
        asc_top_d = _tl_anchor_dict(inst.tl_asc_top)
        if asc_top_d:
            tls.append({"type": "ASC_RESIST",  "color": "#69f0ae", **asc_top_d})
        desc_d = _tl_anchor_dict(inst.tl_resist)
        if desc_d:
            tls.append({"type": "DESC_RESIST", "color": "#ff5252", **desc_d})
        desc_low_d = _tl_anchor_dict(inst.tl_desc_low)
        if desc_low_d:
            tls.append({"type": "DESC_SUPPORT","color": "#ff8a80", **desc_low_d})
        if inst.horiz_zone is not None:
            tls.append({"type": "HORIZONTAL",  "color": "#ffd740", "price": inst.horiz_zone})
        instruments.append({
            "symbol":     inst.symbol,
            "opt_type":   inst.opt_type,
            "ltp":        round(inst.ltp, 2),
            "candles":    inst.candles_today,
            "trendlines": tls,
        })

    # NIFTY spot
    with _spot_lock:
        spot_candles = list(_spot_state.candles_today)
        spot_tls     = []
        spot_ltp     = float(_spot_state.ltp_history[-1]) if _spot_state.ltp_history else 0.0
        for attr, tl_type, color in [
            ("tl_support",  "ASC_SUPPORT",  "#00c853"),
            ("tl_asc_top",  "ASC_RESIST",   "#69f0ae"),
            ("tl_resist",   "DESC_RESIST",  "#ff5252"),
            ("tl_desc_low", "DESC_SUPPORT", "#ff8a80"),
        ]:
            d = _tl_anchor_dict(getattr(_spot_state, attr))
            if d:
                spot_tls.append({"type": tl_type, "color": color, **d})
        if _spot_state.horiz_zone is not None:
            spot_tls.append({"type": "HORIZONTAL", "color": "#ffd740",
                             "price": _spot_state.horiz_zone})

    # Only write if we have live candle data — otherwise keep last good snapshot
    has_live_data = (any(inst.get("candles") for inst in instruments)
                     or len(spot_candles) > 0)
    if not has_live_data:
        return

    try:
        with open(_CHART_DATA_FILE, "w") as f:
            json.dump({
                "ts":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "index":       cfg["index"],
                "instruments": instruments,
                "spot": {
                    "symbol":     cfg["index"],
                    "ltp":        spot_ltp,
                    "candles":    spot_candles,
                    "trendlines": spot_tls,
                },
            }, f)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP SESSION  — no auth needed for market-data endpoints
# ═══════════════════════════════════════════════════════════════════════════════
_sess = requests.Session()
_sess.headers.update({
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-app-id":        "growwWeb",
    "x-device-id":     CONFIG["device_id"],
    "x-device-id-v2":  CONFIG["device_id"],
    "x-platform":      "web",
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/142.0.0.0 Safari/537.36",
})
_GROWW = "https://groww.in/v1/api"

# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class Pivot:
    idx:   int    # candle index in today's list
    ts:    int    # unix UTC seconds
    price: float  # low (for support) or high (for resistance)


@dataclass
class TrendlineState:
    valid:           bool  = False
    support:         float = 0.0   # projected trendline level right now
    slope:           float = 0.0   # pts per candle (positive = ascending)
    ascending:       bool  = False
    pivots:          List[Pivot] = field(default_factory=list)
    last_swing_high: float = 0.0
    projected_at:    float = 0.0   # unix time of last projection


@dataclass
class Trade:
    symbol:        str
    opt_type:      str
    play_type:     str    # "BOUNCE" | "BREAK"
    entry_price:   float
    entry_time:    str
    qty:           int
    target:        Optional[float]  # None for BREAK plays
    sl:            float
    trail_activate: float
    trail_by:      float
    peak:          float = 0.0
    trail_active:  bool  = False
    # filled on close
    exit_price:    float = ""
    exit_reason:   str   = ""
    exit_time:     str   = ""
    pnl:           float = 0.0
    closed:        bool  = False


@dataclass
class InstrumentState:
    symbol:     str
    index:      str
    opt_type:   str   # "CE" | "PE"
    strike:     int
    exchange:   str   = "NSE"
    # candle data
    candles_all:   list = field(default_factory=list)
    candles_today: list = field(default_factory=list)
    # trendlines — one per channel rail, all optional
    tl:            TrendlineState = field(default_factory=TrendlineState)  # ascending support   (lower rail ↗)
    tl_asc_top:    TrendlineState = field(default_factory=TrendlineState)  # ascending resistance (upper rail ↗)
    tl_resist:     TrendlineState = field(default_factory=TrendlineState)  # descending resistance (upper rail ↘)
    tl_desc_low:   TrendlineState = field(default_factory=TrendlineState)  # descending support   (lower rail ↘)
    horiz_zone:    Optional[float] = None                                   # horizontal zone mid
    # live price
    ltp:           float = 0.0
    # state flags
    last_refresh:  float = 0.0
    last_break_ts: float = 0.0   # unix time of last break signal (cooldown)
    break_ref_ltp: float = 0.0   # CE/PE LTP at first break detection (baseline for cumulative gain)
    break_ref_ts:  float = 0.0   # when break_ref_ltp was set (reset after 10 min)
    confirming:    bool  = False  # True while a confirmation thread is running
    active_trade:  Optional[Trade] = None
    _lock:         threading.Lock  = field(default_factory=threading.Lock)


@dataclass
class SpotState:
    """Tracks all NIFTY spot trendline rails in parallel with option charts."""
    candles_today: list  = field(default_factory=list)
    tl_support:    TrendlineState = field(default_factory=TrendlineState)   # ascending lower rail  ↗
    tl_asc_top:    TrendlineState = field(default_factory=TrendlineState)   # ascending upper rail  ↗
    tl_resist:     TrendlineState = field(default_factory=TrendlineState)   # descending upper rail ↘
    tl_desc_low:   TrendlineState = field(default_factory=TrendlineState)   # descending lower rail ↘
    horiz_zone:    Optional[float] = None                                   # flat consolidation mid
    ltp_history:   object = field(default_factory=lambda: deque(maxlen=12))
    last_refresh:  float = 0.0

    @property
    def tl(self) -> TrendlineState:
        """Backward compat: return ascending support trendline."""
        return self.tl_support

    def structure(self) -> str:
        """Human-readable current NIFTY structure (all active rails)."""
        parts = []
        if self.horiz_zone:
            parts.append(f"HORIZONTAL zone=₹{self.horiz_zone:.0f}")
        if self.tl_support.valid:
            top = f" / top=₹{self.tl_asc_top.support:.0f}" if self.tl_asc_top.valid else ""
            parts.append(f"ASC support=₹{self.tl_support.support:.0f}{top} slope={self.tl_support.slope:+.2f}")
        if self.tl_resist.valid:
            bot = f" / bot=₹{self.tl_desc_low.support:.0f}" if self.tl_desc_low.valid else ""
            parts.append(f"DESC resist=₹{self.tl_resist.support:.0f}{bot} slope={self.tl_resist.slope:+.2f}")
        return "  │  ".join(parts) if parts else "NO STRUCTURE"


_spot_state = SpotState()
_spot_lock  = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════════
# SYMBOL UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════
def make_symbol(index: str, expiry: date, strike: int, opt_type: str) -> str:
    """Build Groww charting-API symbol. e.g. NIFTY2662324000PE"""
    yy = expiry.year % 100
    m  = expiry.month        # single digit, no leading zero
    dd = f"{expiry.day:02d}"
    return f"{index}{yy}{m}{dd}{int(strike)}{opt_type}"

def parse_expiry(date_str: str) -> date:
    return datetime.strptime(date_str, "%Y-%m-%d").date()

def atm_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)

def lot_size(index: str) -> int:
    return LOT_SIZES.get(index.upper(), 75)

# ═══════════════════════════════════════════════════════════════════════════════
# API LAYER
# ═══════════════════════════════════════════════════════════════════════════════
def _get(url: str, params: dict = None) -> Optional[dict]:
    try:
        r = _sess.get(url, params=params, timeout=CONFIG["req_timeout"])
        r.raise_for_status()
        return r.json()
    except Exception as ex:
        path = url.split("groww.in")[-1][:70]
        log.warning(f"  [API] ✗ {path}  →  {ex}")
        return None


def fetch_candles(symbol: str, exchange: str, interval: int) -> Optional[list]:
    """5-min OHLCV candles for an option. Returns list of dicts."""
    url  = (f"{_GROWW}/stocks_fo_data/v1/charting_service/chart"
            f"/exchange/{exchange}/segment/FNO/{symbol}/daily")
    data = _get(url, {"intervalInMinutes": interval})
    if not data or "candles" not in data:
        return None
    out = []
    for c in data["candles"]:
        # [ts, open, high, low, close, volume(may be null)]
        out.append({
            "ts": int(c[0]),
            "o":  float(c[1]),
            "h":  float(c[2]),
            "l":  float(c[3]),
            "c":  float(c[4]),
            "v":  int(c[5]) if c[5] is not None else 0,
        })
    return out


def fetch_ltp(symbol: str, exchange: str) -> float:
    """Live LTP for one option instrument."""
    url  = (f"{_GROWW}/stocks_fo_data/v1/tr_live_prices"
            f"/exchange/{exchange}/segment/FNO/{symbol}/latest")
    data = _get(url)
    return float(data["ltp"]) if data and "ltp" in data else 0.0


def fetch_spot(index: str, exchange: str) -> float:
    """Live spot price for an index."""
    url  = (f"{_GROWW}/stocks_data/v1/tr_live_indices"
            f"/exchange/{exchange}/segment/CASH/{index}/latest")
    data = _get(url)
    return float(data["value"]) if data and "value" in data else 0.0


def _auto_bearer_token() -> str:
    """Fetch fresh Bearer token via TOTP (reads ai_config.json)."""
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
        token = GrowwAPI.get_access_token(api_key=api_key, totp=pyotp.TOTP(totp_sec).now())
        log.info("✅  Fresh Bearer token fetched for spot chart")
        return token
    except Exception as ex:
        log.debug(f"  [spot] Bearer token fetch failed: {ex}")
        return ""


_spot_bearer: str = ""   # cached token for index candle calls

def fetch_index_candles(index: str, exchange: str, interval: int) -> Optional[list]:
    """5-min OHLCV candles for NIFTY/SENSEX spot index.

    Uses Groww charting_service/v4 endpoint (requires Bearer token).
    URL from HAR: GET /v1/api/charting_service/v4/chart/exchange/NSE/segment/CASH/NIFTY
                  ?startTimeInMillis=...&endTimeInMillis=...&intervalInMinutes=5
    """
    global _spot_bearer

    # Ensure we have a token
    if not _spot_bearer:
        _spot_bearer = _auto_bearer_token()
    if not _spot_bearer:
        log.debug("  [spot] No Bearer token — cannot fetch index candles")
        return None

    now_ms = int(time.time() * 1000)
    # Start from 2 days ago to get enough pivot data regardless of pre-market refresh time
    start_ms = now_ms - 2 * 24 * 60 * 60 * 1000

    url  = (f"https://groww.in/v1/api/charting_service/v4/chart"
            f"/exchange/{exchange}/segment/CASH/{index}")
    hdrs = {
        "authorization": f"Bearer {_spot_bearer}",
        "x-device-type":  "charts",
    }
    try:
        resp = _sess.get(url, params={"startTimeInMillis": start_ms,
                                      "endTimeInMillis":   now_ms,
                                      "intervalInMinutes": interval},
                         headers=hdrs, timeout=CONFIG["req_timeout"])
        if resp.status_code == 401:
            # Token expired — refresh once and retry
            _spot_bearer = _auto_bearer_token()
            if not _spot_bearer:
                return None
            hdrs["authorization"] = f"Bearer {_spot_bearer}"
            resp = _sess.get(url, params={"startTimeInMillis": start_ms,
                                          "endTimeInMillis":   now_ms,
                                          "intervalInMinutes": interval},
                             headers=hdrs, timeout=CONFIG["req_timeout"])
        resp.raise_for_status()
        data = resp.json()

        # v4 response: {"candles": [[ts_ms, o, h, l, c, v], ...]}
        # or nested:   {"data": {"candles": [...]}}
        candles_raw = (data.get("candles")
                       or data.get("data", {}).get("candles")
                       or [])
        if not candles_raw:
            return None

        out = []
        for c in candles_raw:
            ts = int(c[0])
            # v4 timestamps are in milliseconds — convert to seconds
            if ts > 1e12:
                ts = ts // 1000
            out.append({
                "ts": ts,
                "o":  float(c[1]),
                "h":  float(c[2]),
                "l":  float(c[3]),
                "c":  float(c[4]),
                "v":  int(c[5]) if len(c) > 5 and c[5] is not None else 0,
            })
        return out if out else None

    except Exception as ex:
        log.debug(f"  [spot] fetch_index_candles failed: {ex}")
        _spot_bearer = ""   # force re-fetch next time
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# TRENDLINE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def _ist_date(ts_utc: int) -> date:
    return datetime.utcfromtimestamp(ts_utc + IST_OFFSET).date()


def filter_today(all_candles: list) -> list:
    """Keep only candles from the most-recent trading date (IST)."""
    if not all_candles:
        return []
    last_date = _ist_date(all_candles[-1]["ts"])
    return [c for c in all_candles if _ist_date(c["ts"]) == last_date]


def find_swing_lows(candles: list, lb: int) -> List[Pivot]:
    """Pivot lows: candle whose LOW < all LBs candles on each side."""
    pivots, n = [], len(candles)
    for i in range(lb, n - lb):
        lo = candles[i]["l"]
        if (all(candles[i-j]["l"] > lo for j in range(1, lb+1)) and
                all(candles[i+j]["l"] > lo for j in range(1, lb+1))):
            pivots.append(Pivot(idx=i, ts=candles[i]["ts"], price=lo))
    return pivots


def find_swing_highs(candles: list, lb: int) -> List[Pivot]:
    """Pivot highs: candle whose HIGH > all LBs candles on each side."""
    pivots, n = [], len(candles)
    for i in range(lb, n - lb):
        hi = candles[i]["h"]
        if (all(candles[i-j]["h"] < hi for j in range(1, lb+1)) and
                all(candles[i+j]["h"] < hi for j in range(1, lb+1))):
            pivots.append(Pivot(idx=i, ts=candles[i]["ts"], price=hi))
    return pivots


def project_trendline(pivots: List[Pivot], cur_idx: int) -> Optional[float]:
    """Project trendline through last 2 pivots to cur_idx."""
    if len(pivots) < 2:
        return None
    p1, p2 = pivots[-2], pivots[-1]
    d_idx = p2.idx - p1.idx
    if d_idx == 0:
        return None
    slope = (p2.price - p1.price) / d_idx
    return p2.price + slope * (cur_idx - p2.idx)


def compute_trendline(candles_today: list, lb: int, min_p: int) -> TrendlineState:
    """Ascending support trendline from swing lows (used for option premiums)."""
    tl = TrendlineState()
    if len(candles_today) < lb * 2 + 2:
        return tl

    swing_lows  = find_swing_lows(candles_today,  lb)
    swing_highs = find_swing_highs(candles_today, lb)

    tl.last_swing_high = max((p.price for p in swing_highs), default=0.0)

    if len(swing_lows) < min_p:
        return tl

    last = swing_lows[-min_p:]
    ascending = all(last[i].price > last[i-1].price for i in range(1, len(last)))
    tl.ascending = ascending

    if not ascending:
        return tl

    cur_idx   = len(candles_today) - 1
    projected = project_trendline(swing_lows, cur_idx)
    if not projected or projected <= 0:
        return tl

    p1, p2 = swing_lows[-2], swing_lows[-1]
    slope   = (p2.price - p1.price) / max(p2.idx - p1.idx, 1)

    tl.valid        = True
    tl.pivots       = swing_lows
    tl.support      = round(projected, 2)
    tl.slope        = round(slope, 4)
    tl.projected_at = time.time()
    return tl


def compute_ascending_resistance(candles_today: list, lb: int, min_p: int) -> TrendlineState:
    """Ascending resistance from swing HIGHS (channel top / upper rail).
    valid=True means highs are also rising — confirms full ascending channel.
    TrendlineState.support stores the projected channel-top price."""
    tl = TrendlineState()
    if len(candles_today) < lb * 2 + 2:
        return tl

    swing_highs = find_swing_highs(candles_today, lb)
    if len(swing_highs) < min_p:
        return tl

    last = swing_highs[-min_p:]
    ascending = all(last[i].price > last[i-1].price for i in range(1, len(last)))
    if not ascending:
        return tl

    cur_idx   = len(candles_today) - 1
    projected = project_trendline(swing_highs, cur_idx)
    if not projected or projected <= 0:
        return tl

    p1, p2 = swing_highs[-2], swing_highs[-1]
    slope   = (p2.price - p1.price) / max(p2.idx - p1.idx, 1)

    tl.valid        = True
    tl.ascending    = True
    tl.pivots       = swing_highs
    tl.support      = round(projected, 2)   # projected channel TOP (resistance)
    tl.slope        = round(slope, 4)
    tl.projected_at = time.time()
    return tl


def compute_descending_support(candles_today: list, lb: int, min_p: int) -> TrendlineState:
    """Descending support from swing LOWS (channel bottom / lower rail of a downtrend).
    valid=True means lows are also falling — confirms full descending channel.
    TrendlineState.support stores the projected channel-bottom price."""
    tl = TrendlineState()
    if len(candles_today) < lb * 2 + 2:
        return tl

    swing_lows = find_swing_lows(candles_today, lb)
    if len(swing_lows) < min_p:
        return tl

    last = swing_lows[-min_p:]
    descending = all(last[i].price < last[i-1].price for i in range(1, len(last)))
    if not descending:
        return tl

    cur_idx   = len(candles_today) - 1
    projected = project_trendline(swing_lows, cur_idx)
    if not projected or projected <= 0:
        return tl

    p1, p2 = swing_lows[-2], swing_lows[-1]
    slope   = (p2.price - p1.price) / max(p2.idx - p1.idx, 1)

    tl.valid        = True
    tl.ascending    = False
    tl.pivots       = swing_lows
    tl.support      = round(projected, 2)   # projected channel BOTTOM
    tl.slope        = round(slope, 4)       # negative slope
    tl.projected_at = time.time()
    return tl


def compute_descending_trendline(candles_today: list, lb: int, min_p: int) -> TrendlineState:
    """Descending resistance trendline from swing highs.
    valid=True means NIFTY is making lower highs → downtrend.
    TrendlineState.support stores the projected resistance level (despite the name)."""
    tl = TrendlineState()
    if len(candles_today) < lb * 2 + 2:
        return tl

    swing_highs = find_swing_highs(candles_today, lb)
    if len(swing_highs) < min_p:
        return tl

    last = swing_highs[-min_p:]
    descending = all(last[i].price < last[i-1].price for i in range(1, len(last)))
    if not descending:
        return tl

    cur_idx   = len(candles_today) - 1
    projected = project_trendline(swing_highs, cur_idx)
    if not projected or projected <= 0:
        return tl

    p1, p2 = swing_highs[-2], swing_highs[-1]
    slope   = (p2.price - p1.price) / max(p2.idx - p1.idx, 1)

    tl.valid        = True
    tl.ascending    = False          # explicitly False = descending
    tl.pivots       = swing_highs
    tl.support      = round(projected, 2)   # projected resistance
    tl.slope        = round(slope, 4)       # negative slope
    tl.projected_at = time.time()
    return tl


def detect_horizontal_zone(candles_today: list, lb: int,
                            tolerance_pct: float = 0.15) -> Optional[float]:
    """Return the mid-price of a horizontal consolidation zone if one exists.
    Detects when last 2 swing lows AND last 2 swing highs are both within tolerance_pct% of each other.
    Returns None when market is trending."""
    if len(candles_today) < lb * 2 + 4:
        return None
    lows  = find_swing_lows(candles_today,  lb)
    highs = find_swing_highs(candles_today, lb)
    if len(lows) < 2 or len(highs) < 2:
        return None
    last_lows  = [p.price for p in lows[-2:]]
    last_highs = [p.price for p in highs[-2:]]
    mid = sum(last_lows + last_highs) / 4
    if mid <= 0:
        return None
    low_range  = abs(last_lows[1]  - last_lows[0])  / mid * 100
    high_range = abs(last_highs[1] - last_highs[0]) / mid * 100
    if low_range <= tolerance_pct and high_range <= tolerance_pct:
        return round(mid, 2)
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# SPOT STATE & SIGNAL QUALITY FILTERS
# ═══════════════════════════════════════════════════════════════════════════════
def _refresh_spot():
    """Fetch NIFTY spot candles and compute all three trendline structures in parallel with options."""
    cfg = CONFIG
    # Always store latest spot LTP
    ltp = fetch_spot(cfg["index"], cfg["exchange"])
    if ltp > 0:
        with _spot_lock:
            _spot_state.ltp_history.append(ltp)

    candles = fetch_index_candles(cfg["index"], cfg["exchange"], cfg["candle_interval"])
    if not candles:
        return
    today   = filter_today(candles)
    lb      = cfg["pivot_lookback"]
    min_p   = cfg["min_pivots"]

    asc_en  = cfg.get("tl_ascending_enabled",  True)
    desc_en = cfg.get("tl_descending_enabled", False)
    hor_en  = cfg.get("tl_horizontal_enabled", False)

    tl_sup      = compute_trendline(today, lb, min_p)            if asc_en  else TrendlineState()
    tl_asc_top  = compute_ascending_resistance(today, lb, min_p) if asc_en  else TrendlineState()
    tl_res      = compute_descending_trendline(today, lb, min_p) if desc_en else TrendlineState()
    tl_desc_low = compute_descending_support(today, lb, min_p)   if desc_en else TrendlineState()
    horiz       = detect_horizontal_zone(today, lb)              if hor_en  else None

    with _spot_lock:
        _spot_state.candles_today = today
        _spot_state.tl_support    = tl_sup
        _spot_state.tl_asc_top    = tl_asc_top
        _spot_state.tl_resist     = tl_res
        _spot_state.tl_desc_low   = tl_desc_low
        _spot_state.horiz_zone    = horiz
        _spot_state.last_refresh  = time.time()

    struct = _spot_state.structure()
    log.info(f"  📈 NIFTY spot  {struct}  bars={len(today)}")


def _spot_confirms(opt_type: str) -> Tuple[bool, str]:
    """
    Double confirmation using NIFTY spot trendlines (same algorithm as option premiums).

    Detects three structures on NIFTY 5-min chart in parallel:
      - Ascending support  (higher lows)  → NIFTY uptrend  → confirms CE trades
      - Descending resist  (lower highs)  → NIFTY downtrend → confirms PE trades
      - Horizontal zone    (flat range)   → consolidation   → allows both directions

    CE trade: NIFTY must show ascending structure OR be breaking above descending resist.
    PE trade: NIFTY must show descending structure OR be breaking below ascending support.
    Horizontal: always allowed (both CE and PE can bounce in a range).
    No structure: not enough data yet → don't block (let option trendline decide).
    """
    if not CONFIG.get("spot_confirm_enabled"):
        return True, ""

    with _spot_lock:
        tl_sup  = _spot_state.tl_support
        tl_res  = _spot_state.tl_resist
        horiz   = _spot_state.horiz_zone
        today   = list(_spot_state.candles_today)

    if not today:
        return True, "no NIFTY spot data yet — filter skipped"

    cur = today[-1]["c"]

    # Only use structures enabled by tl toggles
    asc_enabled   = CONFIG.get("tl_ascending_enabled",  True)
    desc_enabled  = CONFIG.get("tl_descending_enabled", False)
    horiz_enabled = CONFIG.get("tl_horizontal_enabled", False)

    # ── Horizontal zone: NIFTY is ranging — allow both directions ─────────────
    if horiz_enabled and horiz is not None:
        return True, f"NIFTY horizontal zone ₹{horiz:.0f} — both directions ok"

    # ── CE trade: need uptrend confirmation ───────────────────────────────────
    if opt_type == "CE":
        if asc_enabled and tl_sup.valid:
            vs_sup = cur - tl_sup.support
            ok = vs_sup >= -10.0
            reason = (f"NIFTY ascending support=₹{tl_sup.support:.0f}  "
                      f"spot {'above ✓' if ok else 'below ✗'} by {vs_sup:+.0f}  "
                      f"slope={tl_sup.slope:+.3f}/bar")
            return ok, reason
        if desc_enabled and tl_res.valid:
            vs_res = cur - tl_res.support
            ok = vs_res >= 0
            reason = (f"NIFTY descending resist=₹{tl_res.support:.0f}  "
                      f"spot {'broke above ✓' if ok else 'still below ✗'} by {vs_res:+.0f}")
            return ok, reason
        return True, "NIFTY no enabled structure — not blocking CE"

    # ── PE trade: need downtrend confirmation ─────────────────────────────────
    else:
        if desc_enabled and tl_res.valid:
            vs_res = cur - tl_res.support
            ok = vs_res <= 10.0
            reason = (f"NIFTY descending resist=₹{tl_res.support:.0f}  "
                      f"spot {'below ✓' if ok else 'above ✗'} by {vs_res:+.0f}  "
                      f"slope={tl_res.slope:+.3f}/bar")
            return ok, reason
        if asc_enabled and tl_sup.valid:
            vs_sup = cur - tl_sup.support
            ok = vs_sup < 0
            reason = (f"NIFTY ascending support=₹{tl_sup.support:.0f}  "
                      f"spot {'broke below ✓' if ok else 'still above ✗'} by {vs_sup:+.0f}")
            return ok, reason
        return True, "NIFTY no enabled structure — not blocking PE"


def _volume_confirms(inst: InstrumentState) -> Tuple[bool, str]:
    """Current candle volume > mult × 5-bar average."""
    if not CONFIG.get("volume_confirm_enabled"):
        return True, ""
    today = inst.candles_today
    if not today or len(today) < 6:
        return True, "not enough candles"
    mult    = CONFIG.get("volume_confirm_mult", 1.3)
    last5   = [c["v"] for c in today[-6:-1] if c["v"] > 0]
    cur_vol = today[-1]["v"]
    if not last5 or sum(last5) == 0:
        return True, "zero avg volume"
    avg_vol = sum(last5) / len(last5)
    ok      = cur_vol >= avg_vol * mult
    reason  = f"vol {cur_vol} {'≥' if ok else '<'} {mult:.1f}×avg({avg_vol:.0f})"
    return ok, reason


def _required_confirm_pts(ltp: float) -> float:
    """Return confirmation threshold in pts: fixed or percentage of LTP."""
    cfg = CONFIG
    if cfg.get("pct_confirm_enabled"):
        pct = cfg.get("bounce_confirm_pct", 0.8)
        return max(round(ltp * pct / 100.0, 2), 1.0)  # floor at 1 pt
    return cfg["bounce_confirm_pts"]


# ═══════════════════════════════════════════════════════════════════════════════
# STRUCTURAL REFRESH
# ═══════════════════════════════════════════════════════════════════════════════
def refresh_one(inst: InstrumentState, verbose: bool = True):
    """Fetch candles + recompute all enabled trendline types for one instrument.
    verbose=True  → log every instrument (startup)
    verbose=False → only log instruments that gained/lost valid structure (background)
    """
    cfg     = CONFIG
    candles = fetch_candles(inst.symbol, inst.exchange, cfg["candle_interval"])
    if not candles:
        return

    today  = filter_today(candles)
    lb     = cfg["pivot_lookback"]
    min_p  = cfg["min_pivots"]

    was_asc       = inst.tl.valid
    was_asc_top   = inst.tl_asc_top.valid
    was_desc      = inst.tl_resist.valid
    was_desc_low  = inst.tl_desc_low.valid
    was_horiz     = inst.horiz_zone is not None

    asc_en  = cfg.get("tl_ascending_enabled",  True)
    desc_en = cfg.get("tl_descending_enabled", False)
    hor_en  = cfg.get("tl_horizontal_enabled", False)

    tl         = compute_trendline(today, lb, min_p)              if asc_en  else TrendlineState()
    tl_asc_top = compute_ascending_resistance(today, lb, min_p)   if asc_en  else TrendlineState()
    tl_res     = compute_descending_trendline(today, lb, min_p)   if desc_en else TrendlineState()
    tl_desc_low= compute_descending_support(today, lb, min_p)     if desc_en else TrendlineState()
    horiz      = detect_horizontal_zone(today, lb)                if hor_en  else None

    with inst._lock:
        inst.candles_all   = candles
        inst.candles_today = today
        inst.tl            = tl
        inst.tl_asc_top    = tl_asc_top
        inst.tl_resist     = tl_res
        inst.tl_desc_low   = tl_desc_low
        inst.horiz_zone    = horiz
        inst.last_refresh  = time.time()

    if verbose:
        parts = []
        if tl.valid:
            top_s = f"→top ₹{tl_asc_top.support:.2f}" if tl_asc_top.valid else ""
            parts.append(f"ASC sup=₹{tl.support:.2f} {top_s} slope={tl.slope:+.3f} p={len(tl.pivots)}")
        if tl_res.valid:
            bot_s = f"→bot ₹{tl_desc_low.support:.2f}" if tl_desc_low.valid else ""
            parts.append(f"DESC res=₹{tl_res.support:.2f} {bot_s} slope={tl_res.slope:+.3f}")
        if horiz is not None:
            parts.append(f"HORIZ ₹{horiz:.2f}")
        if parts:
            log.info(f"  📐 {inst.symbol:22s}  {'  │  '.join(parts)}  bars={len(today)}")
            _log_tl_anchors("ASC_SUPPORT", tl,         today)
            _log_tl_anchors("ASC_RESIST",  tl_asc_top, today)
            _log_tl_anchors("DESC_RESIST", tl_res,     today)
            _log_tl_anchors("DESC_SUPPORT",tl_desc_low,today)
        else:
            log.info(f"  ⬜ {inst.symbol:22s}  no structure  (today={len(today)} bars)")
    else:
        if tl.valid and not was_asc:
            ch = f" channel_top=₹{tl_asc_top.support:.2f}" if tl_asc_top.valid else ""
            log.info(f"  📐 NEW ASC: {inst.symbol}  support=₹{tl.support:.2f}  slope={tl.slope:+.3f}/bar{ch}  pivots={len(tl.pivots)}")
            _log_tl_anchors("ASC_SUPPORT", tl,         today)
            _log_tl_anchors("ASC_RESIST",  tl_asc_top, today)
        elif not tl.valid and was_asc:
            log.info(f"  ⬜ ASC LOST: {inst.symbol}")
        if tl_res.valid and not was_desc:
            ch = f" channel_bot=₹{tl_desc_low.support:.2f}" if tl_desc_low.valid else ""
            log.info(f"  📐 NEW DESC: {inst.symbol}  resist=₹{tl_res.support:.2f}  slope={tl_res.slope:+.3f}/bar{ch}")
            _log_tl_anchors("DESC_RESIST",  tl_res,     today)
            _log_tl_anchors("DESC_SUPPORT", tl_desc_low,today)
        elif not tl_res.valid and was_desc:
            log.info(f"  ⬜ DESC LOST: {inst.symbol}")
        if horiz is not None and not was_horiz:
            log.info(f"  📐 NEW HORIZ: {inst.symbol}  zone=₹{horiz:.2f}")
        elif horiz is None and was_horiz:
            log.info(f"  ⬜ HORIZ LOST: {inst.symbol}")


def structural_loop(watch_list: List[InstrumentState]):
    """Background thread: refresh candles every structural_refresh seconds."""
    interval = CONFIG["structural_refresh"]
    while True:
        time.sleep(interval)
        for inst in watch_list:
            if not inst.active_trade:
                refresh_one(inst, verbose=False)
        _refresh_spot()
        cfg      = CONFIG
        valid    = sum(1 for i in watch_list if (
            (cfg.get("tl_ascending_enabled",  True)  and i.tl.valid) or
            (cfg.get("tl_descending_enabled", False) and i.tl_resist.valid) or
            (cfg.get("tl_horizontal_enabled", False) and i.horiz_zone is not None)
        ))
        in_trade = sum(1 for i in watch_list if i.active_trade)
        spot_str = f"  │  NIFTY: {_spot_state.structure()}" if _spot_state.candles_today else ""
        log.info(f"🔄 Structural refresh  │  {valid}/{len(watch_list)} trendlines active  │  "
                 f"{in_trade} open trade(s){spot_str}")
        _write_chart_data(watch_list)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIRMATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def confirm_bounce(inst: InstrumentState, baseline: float) -> Tuple[bool, float]:
    """
    Poll LTP for bounce_confirm_sec.
    Confirmed when price rises >= required pts (fixed or % of LTP) from baseline
    and stays above trendline support.
    """
    cfg      = CONFIG
    need     = _required_confirm_pts(baseline)
    deadline = time.time() + cfg["bounce_confirm_sec"]
    ticks    = [baseline]

    while time.time() < deadline:
        ltp = fetch_ltp(inst.symbol, inst.exchange)
        if ltp > 0:
            ticks.append(ltp)
            # abort if price broke through support
            if ltp < inst.tl.support - cfg["break_pts"]:
                log.info(f"  ❌ Bounce ABORT — broke support ₹{inst.tl.support:.2f}")
                return False, ltp
            gain = ltp - baseline
            if gain >= need:
                log.info(f"  ✅ Bounce confirmed  +{gain:.2f} pts  "
                         f"path={[round(t,1) for t in ticks]}")
                return True, ltp
        time.sleep(3)

    final = ticks[-1]
    log.info(f"  ⏱️  Bounce timeout  final={final:.2f}  "
             f"gain={final-baseline:+.2f} (need {need:.2f})")
    return False, final


def confirm_break_play(inst: InstrumentState, baseline: float,
                       need_pts: float = None) -> Tuple[bool, float]:
    """
    Poll LTP for break_confirm_sec.
    need_pts overrides break_confirm_pts (used when cumulative gain already covers some of it).
    """
    cfg      = CONFIG
    need     = need_pts if need_pts is not None else cfg["break_confirm_pts"]
    deadline = time.time() + cfg["break_confirm_sec"]
    ticks    = [baseline]

    while time.time() < deadline:
        ltp = fetch_ltp(inst.symbol, inst.exchange)
        if ltp > 0:
            ticks.append(ltp)
            gain = ltp - baseline
            if gain >= need:
                log.info(f"  ✅ Break-play confirmed  +{gain:.2f} pts  "
                         f"path={[round(t,1) for t in ticks]}")
                return True, ltp
        time.sleep(2)

    final = ticks[-1]
    log.info(f"  ⏱️  Break-play timeout  final={final:.2f}  "
             f"gain={final-baseline:+.2f} (need {need:.2f})")
    return False, final

# ═══════════════════════════════════════════════════════════════════════════════
# TRADE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
def _write_trade_log(t: Trade, inst: "InstrumentState"):
    today  = datetime.now().strftime("%Y-%m-%d")
    cfg    = CONFIG
    path   = f"logs/trade_history/trendline_{today}.jsonl"
    # Parse option string (e.g. "23400CE") from symbol
    sym    = t.symbol
    opt    = sym[sym.rfind(inst.index) + len(inst.index):].lstrip("0123456789") if inst.index in sym else sym[-6:]
    # Try to extract strike+type suffix
    import re as _re_tl
    _m = _re_tl.search(r'(\d{4,6}(?:CE|PE))$', sym)
    opt = _m.group(1) if _m else sym[-6:]
    with open(path, "a") as f:
        f.write(json.dumps({
            "date":        today,
            "time_entry":  t.entry_time,
            "time_exit":   t.exit_time,
            "bot":         "Trendline",
            "mode":        "sim" if cfg["sim"] else "live",
            "index":       inst.index,
            "symbol":      sym,
            "option":      opt,
            "expiry":      cfg.get("expiry_date", ""),
            "buy_price":   round(t.entry_price, 2),
            "sell_price":  round(t.exit_price,  2),
            "qty":         t.qty,
            "lots":        cfg["lots"],
            "pnl":         round(t.pnl, 2),
            "exit_reason": t.exit_reason,
            "play_type":   t.play_type,
        }) + "\n")


def open_trade(inst: InstrumentState, play_type: str,
               entry_price: float, target: Optional[float],
               initial_sl: float, trail_act: float, trail_by: float) -> Trade:
    cfg  = CONFIG
    qty  = lot_size(inst.index) * cfg["lots"]
    mode = "SIM" if cfg["sim"] else "LIVE"

    t = Trade(
        symbol=inst.symbol, opt_type=inst.opt_type, play_type=play_type,
        entry_price=entry_price, entry_time=datetime.now().strftime("%H:%M:%S"),
        qty=qty, target=target, sl=initial_sl,
        trail_activate=trail_act, trail_by=trail_by, peak=entry_price,
    )
    inst.active_trade = t

    tgt_str = f"₹{target:.2f}" if target else "none (trailing only)"
    log.info(f"  📈 [{mode}] ENTER {inst.opt_type} [{play_type}] @ ₹{entry_price:.2f}  "
             f"qty={qty} ({cfg['lots']} lot{'s' if cfg['lots']>1 else ''})")
    log.info(f"      Target: {tgt_str}  │  SL: ₹{initial_sl:.2f}")
    log.info(f"      Trail: +{trail_act} pts activates, then {trail_by} pts distance")
    return t


def close_trade(inst: InstrumentState, exit_price: float, reason: str):
    t = inst.active_trade
    if not t:
        return
    t.exit_price  = exit_price
    t.exit_reason = reason
    t.exit_time   = datetime.now().strftime("%H:%M:%S")
    t.pnl         = round((exit_price - t.entry_price) * t.qty, 2)
    t.closed      = True
    inst.active_trade = None
    _write_signals_file(None)

    sign = "✅" if t.pnl >= 0 else "❌"
    log.info(f"  {sign} CLOSED {t.opt_type} [{t.play_type}] @ ₹{exit_price:.2f}  {reason}")
    log.info(f"      P&L: ₹{t.pnl:+,.2f}  "
             f"(₹{t.entry_price:.2f}→₹{exit_price:.2f}  × {t.qty} qty)")
    _write_trade_log(t, inst)


def manage_trade(inst: InstrumentState):
    """Update trailing SL and check exit conditions for active trade."""
    t   = inst.active_trade
    ltp = inst.ltp
    if not t or ltp <= 0:
        return

    # Update peak
    if ltp > t.peak:
        t.peak = ltp

    # Activate trailing once profit >= trail_activate
    profit = ltp - t.entry_price
    if not t.trail_active and profit >= t.trail_activate:
        t.trail_active = True
        new_sl = round(ltp - t.trail_by, 2)
        if new_sl > t.sl:
            t.sl = new_sl
        log.info(f"  🔄 TRAIL ON  peak=₹{ltp:.2f}  SL→₹{t.sl:.2f}")

    # Move trail
    if t.trail_active:
        new_sl = round(t.peak - t.trail_by, 2)
        if new_sl > t.sl:
            old_sl = t.sl
            t.sl   = new_sl
            log.info(f"  🔄 TRAIL ↑  peak=₹{t.peak:.2f}  SL ₹{old_sl:.2f}→₹{t.sl:.2f}")

    # Check target
    if t.target and ltp >= t.target:
        close_trade(inst, ltp, f"🎯 TARGET @ ₹{t.target:.2f}")
        return

    # Check SL
    if ltp <= t.sl:
        tag = "🔻 TRAIL SL" if t.trail_active else "🛑 HARD SL"
        close_trade(inst, ltp, f"{tag} @ ₹{t.sl:.2f}")

# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════
def handle_bounce(inst: InstrumentState):
    """BOUNCE signal: confirm and enter on the same instrument."""
    cfg  = CONFIG
    ltp  = inst.ltp
    tl   = inst.tl
    dist = round(ltp - tl.support, 2)
    direction = "NIFTY rising ↑" if inst.opt_type == "CE" else "NIFTY holding ↓"

    # ── Spot confirmation filter ───────────────────────────────────────────────
    spot_ok, spot_reason = _spot_confirms(inst.opt_type)
    if not spot_ok:
        log.info(f"  ⏭️  BOUNCE blocked — spot filter: {spot_reason}")
        inst.confirming = False
        return

    # ── Volume confirmation filter ─────────────────────────────────────────────
    vol_ok, vol_reason = _volume_confirms(inst)
    if not vol_ok:
        log.info(f"  ⏭️  BOUNCE blocked — volume filter: {vol_reason}")
        inst.confirming = False
        return

    need_pts = _required_confirm_pts(ltp)
    log.info(f"")
    log.info(f"  ⚡ ── BOUNCE SIGNAL {'─'*43}")
    log.info(f"     Instrument: {inst.symbol} @ ₹{ltp:.2f}")
    log.info(f"     Support   : ₹{tl.support:.2f}  (dist {dist:.2f} pts above)  slope={tl.slope:+.3f}")
    log.info(f"     Direction : {direction}")
    if spot_reason:
        log.info(f"     Spot check: {spot_reason}")
    log.info(f"     Confirming: need +{need_pts:.2f} pts in {cfg['bounce_confirm_sec']} sec...")

    _signals_log.append({"ts": datetime.now().strftime("%H:%M:%S"), "type": "BOUNCE",
                         "status": "CONFIRMING", "symbol": inst.symbol, "ltp": ltp, "support": tl.support})
    _write_signals_file()
    confirmed, entry = confirm_bounce(inst, ltp)
    if not confirmed:
        log.info(f"  ⏭️  Confirmation failed — no entry")
        _signals_log[-1]["status"] = "FAILED"
        _write_signals_file()
        return

    # Target: use ascending channel top if available (more precise), else last swing high
    if inst.tl_asc_top.valid and inst.tl_asc_top.support > entry:
        target = round(inst.tl_asc_top.support - cfg["target_buffer"], 2)
    elif tl.last_swing_high > entry:
        target = round(tl.last_swing_high - cfg["target_buffer"], 2)
    else:
        target = None
    init_sl   = round(tl.support - cfg["trendline_sl_buf"], 2)

    if entry <= init_sl:
        log.info(f"  ⏭️  Skip — entry ₹{entry:.2f} ≤ SL ₹{init_sl:.2f}")
        return
    if target and target <= entry:
        log.info(f"  ℹ️  Swing high below entry — switching to trailing only")
        target = None

    trail_act_price = round(entry + cfg["bounce_trail_act"], 2)
    log.info(f"")
    log.info(f"  ✅ ── SIGNAL CONFIRMED — ENTERING TRADE {'─'*27}")
    log.info(f"     ACTION  : BUY {inst.opt_type}  {inst.symbol}")
    log.info(f"     Entry   : ₹{entry:.2f}")
    if target:
        log.info(f"     Target  : ₹{target:.2f}  (+{round(target - entry, 2):.1f} pts)")
    else:
        log.info(f"     Target  : None (trailing only)")
    log.info(f"     SL      : ₹{init_sl:.2f}  (−{round(entry - init_sl, 2):.1f} pts)")
    log.info(f"     Trail   : activates at ₹{trail_act_price:.2f} (+{cfg['bounce_trail_act']} pts) → step {cfg['bounce_trail_by']} pts")
    log.info(f"     Mode    : {'📊 SIMULATION' if cfg['sim'] else '🔴 LIVE TRADING'}")
    log.info(f"  {'─'*65}")

    _signals_log[-1].update({"status": "CONFIRMED", "entry": entry, "sl": init_sl,
                              "target": target, "trail_act": cfg["bounce_trail_act"]})
    _write_signals_file({"symbol": inst.symbol, "type": "BOUNCE", "entry": entry, "sl": init_sl})
    open_trade(inst, "BOUNCE", entry, target, init_sl,
               cfg["bounce_trail_act"], cfg["bounce_trail_by"])


def handle_break(broken: InstrumentState, watch_list: List[InstrumentState]):
    """BREAK signal: broken instrument dropped through support → enter opposite side."""
    cfg = CONFIG

    # Cooldown: one break signal per instrument per 2 minutes
    if time.time() - broken.last_break_ts < 120:
        return
    broken.last_break_ts = time.time()

    # ── Spot direction filter for BREAK ───────────────────────────────────────
    # PE broke → we want to buy CE → NIFTY should be rising
    # CE broke → we want to buy PE → NIFTY should be falling
    opp_type_for_spot = "CE" if broken.opt_type == "PE" else "PE"
    spot_ok, spot_reason = _spot_confirms(opp_type_for_spot)
    if not spot_ok:
        log.info(f"  ⏭️  BREAK blocked — spot filter: {spot_reason}")
        return

    ltp_broken = broken.ltp
    support    = broken.tl.support
    drop       = round(support - ltp_broken, 2)
    opp_type   = "CE" if broken.opt_type == "PE" else "PE"
    direction  = "NIFTY bouncing ↑" if broken.opt_type == "PE" else "NIFTY falling ↓"

    # Find best opposite candidate — prefer same strike, then nearest in premium range
    candidates = [i for i in watch_list
                  if i.opt_type == opp_type and not i.active_trade and not i.confirming]
    if not candidates:
        log.info(f"  ⏭️  No available {opp_type} candidates for break signal")
        return

    candidates.sort(key=lambda i: abs(i.strike - broken.strike))
    opp = candidates[0]

    opp_ltp = fetch_ltp(opp.symbol, opp.exchange)
    if opp_ltp <= 0:
        log.info(f"  ⏭️  Could not fetch LTP for {opp.symbol}")
        return
    opp.ltp = opp_ltp

    # Cumulative baseline: use LTP from FIRST break detection, not the current one.
    # Resets after 10 min so a stale break doesn't carry over to a new setup.
    now = time.time()
    if broken.break_ref_ltp == 0.0 or (now - broken.break_ref_ts) > 600:
        broken.break_ref_ltp = opp_ltp
        broken.break_ref_ts  = now
        cumulative_gain      = 0.0
    else:
        cumulative_gain = round(opp_ltp - broken.break_ref_ltp, 2)

    log.info(f"")
    log.info(f"  🚨 ── BREAK SIGNAL {'─'*44}")
    log.info(f"     Broken   : {broken.symbol} @ ₹{ltp_broken:.2f}")
    log.info(f"     Support  : ₹{support:.2f}  (broke by {drop:.2f} pts)")
    log.info(f"     Direction: {direction}")
    if spot_reason:
        log.info(f"     Spot check: {spot_reason}")
    log.info(f"     Candidate: {opp.symbol} @ ₹{opp_ltp:.2f}  "
             f"(+{cumulative_gain:.2f} from first detection @ ₹{broken.break_ref_ltp:.2f})")

    # If CE/PE has already moved enough since first break detection → enter immediately
    if cumulative_gain >= cfg["break_confirm_pts"]:
        log.info(f"     Already moved +{cumulative_gain:.2f} pts ≥ {cfg['break_confirm_pts']} → entering directly")
        _signals_log.append({"ts": datetime.now().strftime("%H:%M:%S"), "type": "BREAK",
                             "status": "CONFIRMING", "broken": broken.symbol, "symbol": opp.symbol,
                             "ltp": opp_ltp, "direction": direction})
        _write_signals_file()
        confirmed, entry = True, opp_ltp
    else:
        remaining = round(cfg["break_confirm_pts"] - cumulative_gain, 2)
        log.info(f"     Confirming: need +{remaining:.2f} more pts in {cfg['break_confirm_sec']} sec...")
        _signals_log.append({"ts": datetime.now().strftime("%H:%M:%S"), "type": "BREAK",
                             "status": "CONFIRMING", "broken": broken.symbol, "symbol": opp.symbol,
                             "ltp": opp_ltp, "direction": direction})
        _write_signals_file()
        confirmed, entry = confirm_break_play(opp, opp_ltp, need_pts=remaining)

    if not confirmed:
        log.info(f"  ⏭️  Confirmation failed — no entry")
        if _signals_log and _signals_log[-1].get("type") == "BREAK":
            _signals_log[-1]["status"] = "FAILED"
            _write_signals_file()
        return

    # Reset baseline after successful entry
    broken.break_ref_ltp = 0.0
    broken.break_ref_ts  = 0.0

    init_sl          = round(entry - cfg["break_initial_sl"], 2)
    trail_act_price  = round(entry + cfg["break_trail_act"], 2)
    log.info(f"")
    log.info(f"  ✅ ── SIGNAL CONFIRMED — ENTERING TRADE {'─'*27}")
    log.info(f"     ACTION  : BUY {opp_type}  {opp.symbol}")
    log.info(f"     Entry   : ₹{entry:.2f}")
    log.info(f"     SL      : ₹{init_sl:.2f}  (−{cfg['break_initial_sl']:.1f} pts)")
    log.info(f"     Trail   : activates at ₹{trail_act_price:.2f} (+{cfg['break_trail_act']} pts) → step {cfg['break_trail_by']} pts")
    log.info(f"     Mode    : {'📊 SIMULATION' if cfg['sim'] else '🔴 LIVE TRADING'}")
    log.info(f"  {'─'*65}")

    if _signals_log and _signals_log[-1].get("type") == "BREAK":
        _signals_log[-1].update({"status": "CONFIRMED", "entry": entry, "sl": init_sl,
                                  "trail_act": cfg["break_trail_act"]})
        _write_signals_file({"symbol": opp.symbol, "type": "BREAK", "entry": entry, "sl": init_sl})
    open_trade(opp, "BREAK", entry, target=None, initial_sl=init_sl,
               trail_act=cfg["break_trail_act"], trail_by=cfg["break_trail_by"])


def handle_breakout(inst: InstrumentState):
    """BREAKOUT signal: option premium breaks ABOVE descending resistance → BUY."""
    cfg  = CONFIG
    ltp  = inst.ltp
    tr   = inst.tl_resist
    dist = round(ltp - tr.support, 2)

    spot_ok, spot_reason = _spot_confirms(inst.opt_type)
    if not spot_ok:
        log.info(f"  ⏭️  BREAKOUT blocked — spot filter: {spot_reason}")
        inst.confirming = False
        return

    vol_ok, vol_reason = _volume_confirms(inst)
    if not vol_ok:
        log.info(f"  ⏭️  BREAKOUT blocked — volume filter: {vol_reason}")
        inst.confirming = False
        return

    need_pts = _required_confirm_pts(ltp)
    ch_bot = f"  channel_bot=₹{inst.tl_desc_low.support:.2f}" if inst.tl_desc_low.valid else ""
    log.info(f"")
    log.info(f"  ⚡ ── BREAKOUT SIGNAL {'─'*41}")
    log.info(f"     Instrument: {inst.symbol} @ ₹{ltp:.2f}")
    log.info(f"     Resistance: ₹{tr.support:.2f}  (dist {dist:+.2f} pts)  slope={tr.slope:+.3f}{ch_bot}")
    if spot_reason:
        log.info(f"     Spot check: {spot_reason}")
    log.info(f"     Confirming: need +{need_pts:.2f} pts in {cfg['bounce_confirm_sec']} sec...")

    _signals_log.append({"ts": datetime.now().strftime("%H:%M:%S"), "type": "BREAKOUT",
                         "status": "CONFIRMING", "symbol": inst.symbol, "ltp": ltp,
                         "resistance": tr.support})
    _write_signals_file()
    confirmed, entry = confirm_bounce(inst, ltp)
    if not confirmed:
        log.info(f"  ⏭️  Confirmation failed — no entry")
        _signals_log[-1]["status"] = "FAILED"
        _write_signals_file()
        return

    # SL: use descending channel bottom if available (natural next support), else below broken resistance
    if inst.tl_desc_low.valid and inst.tl_desc_low.support < tr.support:
        init_sl = round(inst.tl_desc_low.support - cfg["trendline_sl_buf"], 2)
    else:
        init_sl = round(tr.support - cfg["trendline_sl_buf"], 2)
    trail_act_price = round(entry + cfg["bounce_trail_act"], 2)

    if entry <= init_sl:
        log.info(f"  ⏭️  Skip — entry ₹{entry:.2f} ≤ SL ₹{init_sl:.2f}")
        return

    log.info(f"")
    log.info(f"  ✅ ── SIGNAL CONFIRMED — ENTERING TRADE {'─'*27}")
    log.info(f"     ACTION  : BUY {inst.opt_type}  {inst.symbol}")
    log.info(f"     Entry   : ₹{entry:.2f}")
    log.info(f"     Target  : None (trailing only)")
    log.info(f"     SL      : ₹{init_sl:.2f}  (−{round(entry - init_sl, 2):.1f} pts)")
    log.info(f"     Trail   : activates at ₹{trail_act_price:.2f} (+{cfg['bounce_trail_act']} pts) → step {cfg['bounce_trail_by']} pts")
    log.info(f"     Mode    : {'📊 SIMULATION' if cfg['sim'] else '🔴 LIVE TRADING'}")
    log.info(f"  {'─'*65}")

    _signals_log[-1].update({"status": "CONFIRMED", "entry": entry, "sl": init_sl,
                              "target": None, "trail_act": cfg["bounce_trail_act"]})
    _write_signals_file({"symbol": inst.symbol, "type": "BREAKOUT", "entry": entry, "sl": init_sl})
    open_trade(inst, "BREAKOUT", entry, None, init_sl,
               cfg["bounce_trail_act"], cfg["bounce_trail_by"])


def handle_horiz_bounce(inst: InstrumentState):
    """HORIZ_BOUNCE signal: option premium near horizontal zone → BUY on bounce."""
    cfg  = CONFIG
    ltp  = inst.ltp
    zone = inst.horiz_zone
    dist = round(ltp - zone, 2)

    spot_ok, spot_reason = _spot_confirms(inst.opt_type)
    if not spot_ok:
        log.info(f"  ⏭️  HORIZ_BOUNCE blocked — spot filter: {spot_reason}")
        inst.confirming = False
        return

    vol_ok, vol_reason = _volume_confirms(inst)
    if not vol_ok:
        log.info(f"  ⏭️  HORIZ_BOUNCE blocked — volume filter: {vol_reason}")
        inst.confirming = False
        return

    need_pts = _required_confirm_pts(ltp)
    log.info(f"")
    log.info(f"  ⚡ ── HORIZ_BOUNCE SIGNAL {'─'*38}")
    log.info(f"     Instrument: {inst.symbol} @ ₹{ltp:.2f}")
    log.info(f"     Zone mid  : ₹{zone:.2f}  (dist {dist:+.2f} pts)")
    if spot_reason:
        log.info(f"     Spot check: {spot_reason}")
    log.info(f"     Confirming: need +{need_pts:.2f} pts in {cfg['bounce_confirm_sec']} sec...")

    _signals_log.append({"ts": datetime.now().strftime("%H:%M:%S"), "type": "HORIZ_BOUNCE",
                         "status": "CONFIRMING", "symbol": inst.symbol, "ltp": ltp, "zone": zone})
    _write_signals_file()
    confirmed, entry = confirm_bounce(inst, ltp)
    if not confirmed:
        log.info(f"  ⏭️  Confirmation failed — no entry")
        _signals_log[-1]["status"] = "FAILED"
        _write_signals_file()
        return

    init_sl         = round(zone - cfg["trendline_sl_buf"], 2)
    trail_act_price = round(entry + cfg["bounce_trail_act"], 2)

    if entry <= init_sl:
        log.info(f"  ⏭️  Skip — entry ₹{entry:.2f} ≤ SL ₹{init_sl:.2f}")
        return

    log.info(f"")
    log.info(f"  ✅ ── SIGNAL CONFIRMED — ENTERING TRADE {'─'*27}")
    log.info(f"     ACTION  : BUY {inst.opt_type}  {inst.symbol}")
    log.info(f"     Entry   : ₹{entry:.2f}")
    log.info(f"     Target  : None (trailing only — horizontal zone)")
    log.info(f"     SL      : ₹{init_sl:.2f}  (−{round(entry - init_sl, 2):.1f} pts)")
    log.info(f"     Trail   : activates at ₹{trail_act_price:.2f} (+{cfg['bounce_trail_act']} pts) → step {cfg['bounce_trail_by']} pts")
    log.info(f"     Mode    : {'📊 SIMULATION' if cfg['sim'] else '🔴 LIVE TRADING'}")
    log.info(f"  {'─'*65}")

    _signals_log[-1].update({"status": "CONFIRMED", "entry": entry, "sl": init_sl,
                              "target": None, "trail_act": cfg["bounce_trail_act"]})
    _write_signals_file({"symbol": inst.symbol, "type": "HORIZ_BOUNCE", "entry": entry, "sl": init_sl})
    open_trade(inst, "HORIZ_BOUNCE", entry, None, init_sl,
               cfg["bounce_trail_act"], cfg["bounce_trail_by"])


def _signal_worker(inst: InstrumentState, signal: str,
                   watch_list: List[InstrumentState]):
    """Runs in its own thread so monitor loop never blocks."""
    try:
        if signal == "BOUNCE":
            handle_bounce(inst)
        elif signal == "BREAK":
            handle_break(inst, watch_list)
        elif signal == "BREAKOUT":
            handle_breakout(inst)
        elif signal == "HORIZ_BOUNCE":
            handle_horiz_bounce(inst)
    except Exception as ex:
        log.error(f"  [SIGNAL] Exception in {signal} for {inst.symbol}: {ex}", exc_info=True)
    finally:
        inst.confirming = False

# ═══════════════════════════════════════════════════════════════════════════════
# MONITOR LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def is_market_open() -> bool:
    now_ist = datetime.utcfromtimestamp(time.time() + IST_OFFSET)
    t = (now_ist.hour, now_ist.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def _fetch_all_ltps(instruments: List[InstrumentState]):
    """Fetch LTPs for all instruments concurrently."""
    def _fetch(inst):
        ltp = fetch_ltp(inst.symbol, inst.exchange)
        if ltp > 0:
            inst.ltp = ltp
        return inst.symbol, ltp

    with ThreadPoolExecutor(max_workers=min(len(instruments), 12)) as ex:
        list(ex.map(lambda i: _fetch(i), instruments))


def monitor_loop(watch_list: List[InstrumentState]):
    cfg = CONFIG
    log.info("🟢 Monitor loop started")

    while True:
        # ── Wait for market open ───────────────────────────────────────────────
        if not is_market_open():
            log.info("⏸  Outside market hours — sleeping 60 s")
            _write_signals_file(None)   # keep dashboard TODAY stats fresh during off-hours
            time.sleep(60)
            continue

        # ── Fetch all LTPs in parallel ─────────────────────────────────────────
        _fetch_all_ltps(watch_list)

        # ── Per-instrument logic ───────────────────────────────────────────────
        n_valid   = 0
        n_trades  = 0
        n_signals = 0

        # Single-trade gate: skip new signals if any instrument is in-trade or confirming
        any_busy = any(i.active_trade or i.confirming for i in watch_list)

        for inst in watch_list:
            if inst.ltp <= 0:
                continue

            # Manage any open trade
            if inst.active_trade:
                manage_trade(inst)
                n_trades += 1
                continue

            # Skip if confirmation thread already running
            if inst.confirming:
                continue

            # Check which trendline types are valid on this instrument
            asc_ok   = cfg.get("tl_ascending_enabled",  True)  and inst.tl.valid
            desc_ok  = cfg.get("tl_descending_enabled", False) and inst.tl_resist.valid
            horiz_ok = cfg.get("tl_horizontal_enabled", False) and inst.horiz_zone is not None

            if not (asc_ok or desc_ok or horiz_ok):
                continue

            # Skip instruments outside premium range
            if not (cfg["premium_min"] <= inst.ltp <= cfg["premium_max"]):
                continue

            n_valid += 1

            # No new signals while a trade or confirmation is already active
            if any_busy:
                continue

            ltp          = inst.ltp
            signal_fired = False

            # ── BOUNCE / BREAK: ascending support trendline ────────────────────
            if asc_ok and not signal_fired:
                dist = ltp - inst.tl.support
                if 0.0 <= dist <= cfg["proximity_pts"]:
                    inst.confirming = True
                    signal_fired    = True
                    n_signals += 1
                    any_busy = True
                    threading.Thread(
                        target=_signal_worker,
                        args=(inst, "BOUNCE", watch_list),
                        daemon=True, name=f"sig-bounce-{inst.symbol}"
                    ).start()
                elif dist < -cfg["break_pts"]:
                    inst.confirming = True
                    signal_fired    = True
                    n_signals += 1
                    any_busy = True
                    threading.Thread(
                        target=_signal_worker,
                        args=(inst, "BREAK", watch_list),
                        daemon=True, name=f"sig-break-{inst.symbol}"
                    ).start()

            # ── BREAKOUT: price near/above descending resistance ───────────────
            if desc_ok and not signal_fired:
                dist_r = ltp - inst.tl_resist.support
                if -cfg["proximity_pts"] <= dist_r <= cfg["proximity_pts"]:
                    inst.confirming = True
                    signal_fired    = True
                    n_signals += 1
                    any_busy = True
                    threading.Thread(
                        target=_signal_worker,
                        args=(inst, "BREAKOUT", watch_list),
                        daemon=True, name=f"sig-breakout-{inst.symbol}"
                    ).start()

            # ── HORIZ_BOUNCE: price near horizontal zone ───────────────────────
            if horiz_ok and not signal_fired:
                dist_h = ltp - inst.horiz_zone
                if 0.0 <= dist_h <= cfg["proximity_pts"]:
                    inst.confirming = True
                    signal_fired    = True
                    n_signals += 1
                    any_busy = True
                    threading.Thread(
                        target=_signal_worker,
                        args=(inst, "HORIZ_BOUNCE", watch_list),
                        daemon=True, name=f"sig-horiz-{inst.symbol}"
                    ).start()

        # ── Poll summary: only show when something interesting is happening ────
        if n_trades > 0 or n_signals > 0:
            log.info(f"  📡 poll  trendlines={n_valid}/{len(watch_list)}  "
                     f"trades={n_trades}  signals_fired={n_signals}")

        # Write status every ~30s
        if int(time.time()) % 30 < cfg["ltp_poll_sec"]:
            active = next(({"symbol": i.active_trade.symbol, "type": i.active_trade.play_type,
                            "entry": i.active_trade.entry_price, "sl": i.active_trade.sl,
                            "peak": i.active_trade.peak}
                           for i in watch_list if i.active_trade), None)
            _write_signals_file(active)

        time.sleep(cfg["ltp_poll_sec"])

# ═══════════════════════════════════════════════════════════════════════════════
# DAILY P&L SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
def print_daily_summary(watch_list: List[InstrumentState]):
    today = datetime.now().strftime("%Y-%m-%d")
    path  = f"logs/trade_history/trendline_{today}.jsonl"
    if not os.path.exists(path):
        log.info("  [SUMMARY] No trades today")
        return

    trades = []
    with open(path) as f:
        for line in f:
            try:
                trades.append(json.loads(line.strip()))
            except Exception:
                pass

    if not trades:
        log.info("  [SUMMARY] No trades today")
        return

    total_pnl = sum(t.get("pnl", 0) for t in trades)
    wins      = sum(1 for t in trades if t.get("pnl", 0) > 0)
    losses    = sum(1 for t in trades if t.get("pnl", 0) < 0)

    log.info("═" * 60)
    log.info(f"  DAILY SUMMARY — {today}")
    log.info(f"  Trades: {len(trades)}  Wins: {wins}  Losses: {losses}")
    log.info(f"  Total P&L: ₹{total_pnl:+,.2f}")
    log.info("  ─────────────────────────────────────────────────────────")
    for t in trades:
        sign  = "✅" if t.get("pnl", 0) >= 0 else "❌"
        ptype = t.get("play_type", "?")
        log.info(f"  {sign} {t.get('symbol','')[-16:]:16s} [{ptype:6s}]  "
                 f"₹{t.get('entry_price',0):.2f}→₹{t.get('exit_price',0):.2f}  "
                 f"P&L ₹{t.get('pnl',0):+,.2f}  {t.get('exit_reason','')[:25]}")
    log.info("═" * 60)

# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    cfg    = CONFIG
    index  = cfg["index"]
    exc    = cfg["exchange"]
    expiry = parse_expiry(cfg["expiry_date"])
    step   = cfg["strike_step"]

    log.info("═" * 60)
    log.info("  TRENDLINE SCANNER BOT")
    log.info(f"  Index   : {index}  ({exc})")
    log.info(f"  Expiry  : {cfg['expiry_date']}")
    log.info(f"  Mode    : {'📊 SIMULATION' if cfg['sim'] else '🔴 LIVE TRADING'}")
    log.info(f"  Scan ±  : {cfg['scan_range']} strikes  step={step}")
    log.info(f"  Premium : ₹{cfg['premium_min']}–₹{cfg['premium_max']}  (only options in this range traded)")
    tl_types = []
    if cfg.get("tl_ascending_enabled",  True):  tl_types.append("ASC→BOUNCE/BREAK")
    if cfg.get("tl_descending_enabled", False): tl_types.append("DESC→BREAKOUT")
    if cfg.get("tl_horizontal_enabled", False): tl_types.append("HORIZ→HORIZ_BOUNCE")
    log.info(f"  TL types: {' | '.join(tl_types) if tl_types else 'NONE — all disabled!'}")
    log.info("═" * 60)

    # ── Fetch spot ─────────────────────────────────────────────────────────────
    log.info("  Fetching spot price...")
    spot = fetch_spot(index, exc)
    if spot <= 0:
        log.error("❌ Could not fetch spot — check network")
        sys.exit(1)

    atm = atm_strike(spot, step)
    log.info(f"  Spot: ₹{spot:.2f}   ATM: {atm}")

    # ── Build watch list ───────────────────────────────────────────────────────
    watch_list: List[InstrumentState] = []
    r = cfg["scan_range"]
    for offset in range(-r, r + 1):
        strike = atm + offset * step
        for ot in ["CE", "PE"]:
            sym = make_symbol(index, expiry, strike, ot)
            watch_list.append(InstrumentState(
                symbol=sym, index=index, opt_type=ot,
                strike=strike, exchange=exc,
            ))

    ce_count = sum(1 for i in watch_list if i.opt_type == "CE")
    pe_count = sum(1 for i in watch_list if i.opt_type == "PE")
    strikes_str = f"{atm - r * step}–{atm + r * step}"
    log.info(f"\n  Watch list: {len(watch_list)} instruments  "
             f"({ce_count} CE + {pe_count} PE)  strikes {strikes_str}  expiry {expiry}")

    # ── Initial NIFTY spot trendline ───────────────────────────────────────────
    log.info("\n📈 Fetching NIFTY spot trendline...")
    _refresh_spot()
    log.info(f"  NIFTY spot: {_spot_state.structure()}")

    # ── Initial structural refresh ─────────────────────────────────────────────
    log.info("\n📐 Initial structural refresh (scanning all instruments)...")
    for inst in watch_list:
        refresh_one(inst)

    valid_count = sum(1 for i in watch_list if i.tl.valid or i.tl_resist.valid or i.horiz_zone is not None)
    log.info(f"\n  {valid_count}/{len(watch_list)} instruments have valid trendlines")
    _write_chart_data(watch_list)   # seed dashboard chart on startup

    # ── Background structural thread ───────────────────────────────────────────
    threading.Thread(
        target=structural_loop,
        args=(watch_list,),
        daemon=True, name="structural"
    ).start()
    log.info(f"  Structural refresh: every {cfg['structural_refresh']}s")
    log.info(f"  LTP poll interval : every {cfg['ltp_poll_sec']}s")
    log.info("")

    # Write initial stats so dashboard shows today P&L even before market opens
    _write_signals_file(None)

    # ── Monitor loop (blocking) ────────────────────────────────────────────────
    try:
        monitor_loop(watch_list)
    except KeyboardInterrupt:
        log.info("\n⛔ Interrupted by user")
        print_daily_summary(watch_list)
        sys.exit(0)


if __name__ == "__main__":
    main()
