#!/usr/bin/env python3
# =============================================================================
#  KEY LEVELS (Terminal) — Prev-Day H/L/C/O + Multi-Touch Support/Resistance
#
#  Terminal-only companion to pine_script/key_levels.pine. Prints, for an index:
#    1) Yesterday's day High / Low / Close / Open   (the ORANGE lines)
#    2) Multi-touch S/R levels — prices the market has respected several times
#       (the GREEN lines), ranked by touch count = strength.
#
#  Logic (same as the Pine version):
#    - Detect swing pivots (highs & lows) on intraday candles.
#    - Cluster pivots that fall within a tolerance band into one level.
#    - Count how many pivots landed in the band = "touches".
#    - Keep only levels with >= min-touches, sorted strongest first.
#
#  Usage:
#    python3 KEY_LEVELS_TERMINAL.py                     # NIFTY, 5min, defaults
#    python3 KEY_LEVELS_TERMINAL.py --index SENSEX
#    python3 KEY_LEVELS_TERMINAL.py --interval 15minute --days 10
#    python3 KEY_LEVELS_TERMINAL.py --min-touches 3 --tol-pct 0.12
#    python3 KEY_LEVELS_TERMINAL.py --watch 60          # refresh every 60s
# =============================================================================
from __future__ import annotations
import argparse
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
import pyotp
from growwapi import GrowwAPI
from groww_token import get_access_token as get_cached_access_token


# ─────────────────────────────────────────────────────────────
#  CREDENTIALS  (same as CHART_LEVEL_ANALYZER / main bots)
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


# ─────────────────────────────────────────────────────────────
#  ANSI COLORS
# ─────────────────────────────────────────────────────────────
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    ORANGE  = "\033[38;5;208m"
    GREY    = "\033[90m"


# ─────────────────────────────────────────────────────────────
#  AUTH / DATA
# ─────────────────────────────────────────────────────────────
def init_groww() -> tuple:
    access_token = get_cached_access_token(API_KEY, TOTP_SECRET)
    client = GrowwAPI(access_token)
    return client, access_token


def _index_symbols(groww, index_name: str):
    idx = index_name.upper()
    if idx == "NIFTY":
        return groww.EXCHANGE_NSE, ["NSE-NIFTY 50", "NSE-NIFTY"]
    if idx == "SENSEX":
        return groww.EXCHANGE_BSE, ["BSE-SENSEX", "BSE-S&P BSE SENSEX"]
    if idx == "BANKNIFTY":
        return groww.EXCHANGE_NSE, ["NSE-NIFTY BANK", "NSE-BANKNIFTY"]
    if idx == "FINNIFTY":
        return groww.EXCHANGE_NSE, ["NSE-NIFTY FIN SERVICE"]
    return groww.EXCHANGE_NSE, [f"NSE-{idx}"]


def fetch_candles(groww, index_name: str, interval: str, days_back: int) -> list[dict]:
    """Return OHLC candles for the index over the lookback window."""
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days_back)
    exchange, symbols = _index_symbols(groww, index_name)
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
                     "low": float(c[3]), "close": float(c[4]),
                     "volume": (c[5] if len(c) > 5 else None)}
                    for c in result["candles"]
                ]
        except Exception:
            pass
    return []


def get_spot(index_name: str, access_token: str, fallback: Optional[float]) -> Optional[float]:
    """Live index spot via live-data/ltp (CASH). Falls back to last candle close."""
    idx = index_name.upper()
    exch = "BSE" if idx == "SENSEX" else "NSE"
    try:
        url = (f"https://api.groww.in/v1/live-data/ltp"
               f"?segment=CASH&exchange_symbols={exch}_{idx}")
        resp = _session.get(url, headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0",
        }, timeout=6)
        if resp.status_code == 200:
            payload = resp.json().get("payload", {})
            if payload:
                return float(next(iter(payload.values())))
    except Exception:
        pass
    return fallback


# ─────────────────────────────────────────────────────────────
#  LEVEL LOGIC
# ─────────────────────────────────────────────────────────────
def _ts_to_dt(ts) -> datetime:
    # Groww returns epoch-ms ints for intraday candles but ISO strings
    # ("2026-07-09T00:00:00") for daily candles — handle both.
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return datetime.now()
    try:
        return datetime.fromtimestamp(int(ts) / 1000)
    except Exception:
        return datetime.now()


def prev_day_ohlc(candles_1d: list[dict]) -> Optional[dict]:
    """Most recent completed past trading day's full OHLC candle."""
    today = datetime.now().date()
    past  = [c for c in candles_1d if _ts_to_dt(c["ts"]).date() < today]
    if not past:
        return None
    return sorted(past, key=lambda c: _ts_to_dt(c["ts"]))[-1]


def prev_day_from_intraday(intraday: list[dict]) -> Optional[dict]:
    """Rebuild previous trading day's OHLC by aggregating intraday candles.
    More accurate than the daily feed, whose OPEN field is unreliable
    (Groww reports the prior close as the day's open)."""
    today = datetime.now().date()
    by_date: dict = {}
    for c in intraday:
        d = _ts_to_dt(c["ts"]).date()
        if d >= today:
            continue
        by_date.setdefault(d, []).append(c)
    if not by_date:
        return None
    prev_date = max(by_date)
    bars = sorted(by_date[prev_date], key=lambda c: c["ts"])
    return {
        "ts":    bars[0]["ts"],
        "open":  bars[0]["open"],
        "high":  max(c["high"] for c in bars),
        "low":   min(c["low"] for c in bars),
        "close": bars[-1]["close"],
    }


def filter_spikes(candles: list[dict], mult: float) -> tuple[list[dict], int]:
    """Drop bad-tick candles whose range dwarfs the median bar range.
    Groww's index feed stamps a phantom 09:00 opening candle with a huge
    fake wick (e.g. a 450-770pt range vs a ~17pt median); this removes them.
    Returns (clean_candles, dropped_count)."""
    if len(candles) < 5 or mult <= 0:
        return candles, 0
    ranges = sorted(c["high"] - c["low"] for c in candles)
    med = ranges[len(ranges) // 2]
    if med <= 0:
        return candles, 0
    thr = med * mult
    clean = [c for c in candles if (c["high"] - c["low"]) <= thr]
    return clean, len(candles) - len(clean)


def find_pivots(candles: list[dict], left: int, right: int) -> list[float]:
    """Return prices of confirmed swing highs and lows (a pivot needs `left`
    bars before and `right` bars after that don't exceed it)."""
    pivots: list[float] = []
    n = len(candles)
    for i in range(left, n - right):
        hi = candles[i]["high"]
        lo = candles[i]["low"]
        is_high = all(candles[j]["high"] <= hi for j in range(i - left, i + right + 1) if j != i)
        is_low  = all(candles[j]["low"]  >= lo for j in range(i - left, i + right + 1) if j != i)
        if is_high:
            pivots.append(hi)
        if is_low:
            pivots.append(lo)
    return pivots


def cluster_levels(pivots: list[float], tol: float) -> list[dict]:
    """Cluster nearby pivots into levels; touches = pivots in the band.
    `tol` is the price half-width of a cluster band."""
    levels: list[dict] = []   # {price, touches}
    for p in pivots:
        best_idx, best_dist = -1, float("inf")
        for i, lv in enumerate(levels):
            d = abs(lv["price"] - p)
            if d <= tol and d < best_dist:
                best_dist, best_idx = d, i
        if best_idx >= 0:
            lv = levels[best_idx]
            t = lv["touches"] + 1
            lv["price"]   = (lv["price"] * lv["touches"] + p) / t   # running average
            lv["touches"] = t
        else:
            levels.append({"price": p, "touches": 1})
    return levels


# ─────────────────────────────────────────────────────────────
#  MOVING INDICATORS  (mirror indicator.pine)
# ─────────────────────────────────────────────────────────────
def _ema_series(vals: list[float], length: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (length + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema100(candles: list[dict]) -> Optional[float]:
    """N-Line = ema(close, 100) from indicator.pine (the purple line)."""
    closes = [c["close"] for c in candles]
    if len(closes) < 20:
        return None
    return _ema_series(closes, 100)[-1]


def hull_ma(candles: list[dict]) -> Optional[dict]:
    """EHMA(16) trend band = MHULL (now) / SHULL (2 bars back) from indicator.pine.
    EHMA(src,L) = ema(2*ema(src,L/2) - ema(src,L), round(sqrt(L)))."""
    closes = [c["close"] for c in candles]
    if len(closes) < 20:
        return None
    length = 16
    e_half = _ema_series(closes, length // 2)      # ema(src, 8)
    e_full = _ema_series(closes, length)           # ema(src, 16)
    raw    = [2 * a - b for a, b in zip(e_half, e_full)]
    hull   = _ema_series(raw, round(length ** 0.5))  # ema(raw, 4)
    if len(hull) < 3:
        return None
    mhull, shull = hull[-1], hull[-3]
    return {"mhull": mhull, "shull": shull, "up": mhull > shull}


def vwap_session(intraday: list[dict]) -> Optional[dict]:
    """Session VWAP + 1-sigma bands for the most recent day.
    Groww's index feed has no volume, so this falls back to an unweighted
    typical-price average (labelled) unless real volume is present."""
    if not intraday:
        return None
    last_day = max(_ts_to_dt(c["ts"]).date() for c in intraday)
    bars = [c for c in intraday if _ts_to_dt(c["ts"]).date() == last_day]
    if not bars:
        return None
    tps  = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in bars]
    vols = [c.get("volume") for c in bars]
    have_vol = all(v not in (None, 0) for v in vols) and any(vols)
    if have_vol:
        tot_v = sum(vols)
        vwap  = sum(tp * v for tp, v in zip(tps, vols)) / tot_v
        var   = sum(v * (tp - vwap) ** 2 for tp, v in zip(tps, vols)) / tot_v
    else:
        vwap  = sum(tps) / len(tps)
        var   = sum((tp - vwap) ** 2 for tp in tps) / len(tps)
    sd = var ** 0.5
    return {"vwap": vwap, "upper": vwap + sd, "lower": vwap - sd,
            "weighted": have_vol, "date": last_day}


# ─────────────────────────────────────────────────────────────
#  RENDER
# ─────────────────────────────────────────────────────────────
def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def _dist(spot: Optional[float], price: float) -> str:
    if not spot:
        return ""
    d = price - spot
    pct = d / spot * 100
    arrow = f"{C.GREEN}▲{C.RESET}" if d >= 0 else f"{C.RED}▼{C.RESET}"
    return f"{arrow} {d:+8.2f} ({pct:+.2f}%)"


def render(index_name: str, interval: str, spot: Optional[float],
           pdo: Optional[dict], levels: list[dict], min_touches: int,
           ind: Optional[dict] = None) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{C.BOLD}{C.CYAN}══════════ KEY LEVELS · {index_name.upper()} · {interval} ══════════{C.RESET}")
    print(f"{C.GREY}{now}{C.RESET}   Spot: "
          f"{C.BOLD}{_fmt(spot) if spot else 'n/a'}{C.RESET}\n")

    # --- Moving indicators (VWAP / EMA100 / N-Line / Hull) ---
    if ind:
        print(f"{C.MAGENTA}{C.BOLD}▶ MOVING INDICATORS  (this timeframe){C.RESET}")
        e = ind.get("ema100")
        if e is not None:
            print(f"  {C.MAGENTA}N-Line / EMA100{C.RESET}  {C.BOLD}{_fmt(e):>12}{C.RESET}   {_dist(spot, e)}")
        h = ind.get("hull")
        if h:
            trend = f"{C.GREEN}UP ▲{C.RESET}" if h["up"] else f"{C.RED}DOWN ▼{C.RESET}"
            print(f"  {C.BLUE}Hull MHULL     {C.RESET}  {C.BOLD}{_fmt(h['mhull']):>12}{C.RESET}   {_dist(spot, h['mhull'])}")
            print(f"  {C.BLUE}Hull SHULL     {C.RESET}  {C.BOLD}{_fmt(h['shull']):>12}{C.RESET}   trend: {trend}")
        v = ind.get("vwap")
        if v:
            tag = "" if v["weighted"] else f"  {C.GREY}(no volume in index feed — typical-price avg){C.RESET}"
            print(f"  {C.CYAN}VWAP (session) {C.RESET}  {C.BOLD}{_fmt(v['vwap']):>12}{C.RESET}   {_dist(spot, v['vwap'])}{tag}")
            print(f"  {C.CYAN}VWAP upper 1σ  {C.RESET}  {C.BOLD}{_fmt(v['upper']):>12}{C.RESET}   {_dist(spot, v['upper'])}")
            print(f"  {C.CYAN}VWAP lower 1σ  {C.RESET}  {C.BOLD}{_fmt(v['lower']):>12}{C.RESET}   {_dist(spot, v['lower'])}")
        print()

    # --- Previous day (orange) ---
    print(f"{C.ORANGE}{C.BOLD}▶ PREVIOUS DAY LEVELS{C.RESET}")
    if pdo:
        d = _ts_to_dt(pdo["ts"]).date()
        rows = [
            ("Prev Day HIGH ", pdo["high"]),
            ("Prev Day LOW  ", pdo["low"]),
            ("Prev Day CLOSE", pdo["close"]),
            ("Prev Day OPEN ", pdo["open"]),
        ]
        print(f"  {C.GREY}({d}){C.RESET}")
        for name, val in rows:
            print(f"  {C.ORANGE}{name}{C.RESET}  {C.BOLD}{_fmt(val):>12}{C.RESET}   {_dist(spot, val)}")
    else:
        print(f"  {C.GREY}no daily data{C.RESET}")

    # --- Multi-touch S/R (green) ---
    strong = sorted(
        [lv for lv in levels if lv["touches"] >= min_touches],
        key=lambda lv: lv["price"], reverse=True,
    )
    print(f"\n{C.GREEN}{C.BOLD}▶ MULTI-TOUCH S/R  (>= {min_touches} touches){C.RESET}")
    if not strong:
        print(f"  {C.GREY}no qualifying levels — lower --min-touches or widen --tol-pct{C.RESET}")
        print()
        return

    print(f"  {C.GREY}{'LEVEL':>12}  {'ROLE':<11} {'TOUCHES':<20} DISTANCE{C.RESET}")
    for lv in strong:
        price = lv["price"]
        role = (f"{C.RED}RESISTANCE{C.RESET}" if spot and price > spot
                else f"{C.GREEN}SUPPORT{C.RESET}   " if spot else "—")
        stars = "★" * min(lv["touches"], 10)
        strength = (C.GREEN if lv["touches"] >= 4 else
                    C.YELLOW if lv["touches"] == 3 else C.GREY)
        bar = f"{strength}{stars}{C.RESET} x{lv['touches']}"
        print(f"  {C.BOLD}{_fmt(price):>12}{C.RESET}  {role:<11} {bar:<28} {_dist(spot, price)}")
    print()


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def run_once(groww, access_token, args) -> None:
    # Intraday candles for S/R
    intraday = fetch_candles(groww, args.index, args.interval, args.days)
    # Daily candles for previous-day OHLC (fallback only)
    daily = fetch_candles(groww, args.index, "1day", max(args.days, 7))

    if not intraday:
        print(f"{C.RED}No intraday candles returned for {args.index}. "
              f"Market may be closed or symbol unavailable.{C.RESET}")

    # Strip phantom bad-tick candles before computing levels
    dropped = 0
    if not args.no_spike_filter:
        intraday, dropped = filter_spikes(intraday, args.spike_mult)
    if dropped:
        print(f"{C.GREY}spike filter: dropped {dropped} outlier candle(s) "
              f"(range > {args.spike_mult}x median){C.RESET}")

    last_close = intraday[-1]["close"] if intraday else (daily[-1]["close"] if daily else None)
    spot = get_spot(args.index, access_token, last_close)

    # Prefer intraday-aggregated prev day (accurate OPEN); fall back to daily feed.
    pdo = prev_day_from_intraday(intraday) or prev_day_ohlc(daily)

    # Tolerance band width in price terms
    ref = spot or last_close or 0.0
    tol = (ref * args.tol_pct / 100.0) if args.tol_mode == "pct" else args.tol_pts

    pivots = find_pivots(intraday, args.left, args.right)
    levels = cluster_levels(pivots, tol) if tol > 0 else []

    # Moving indicators (VWAP / EMA100 / N-Line / Hull)
    ind = None
    if not args.no_indicators and intraday:
        ind = {
            "ema100": ema100(intraday),
            "hull":   hull_ma(intraday),
            "vwap":   vwap_session(intraday),
        }

    render(args.index, args.interval, spot, pdo, levels, args.min_touches, ind)


def main() -> None:
    ap = argparse.ArgumentParser(description="Terminal Key Levels: prev-day H/L + multi-touch S/R")
    ap.add_argument("--index", default="NIFTY",
                    help="NIFTY | SENSEX | BANKNIFTY | FINNIFTY (default NIFTY)")
    ap.add_argument("--interval", default="5minute",
                    help="candle interval for S/R: 1minute|5minute|15minute|1hour (default 5minute)")
    ap.add_argument("--days", type=int, default=7,
                    help="lookback days for intraday pivots (default 7)")
    ap.add_argument("--left", type=int, default=10, help="pivot bars to the left (default 10)")
    ap.add_argument("--right", type=int, default=10, help="pivot bars to the right (default 10)")
    ap.add_argument("--min-touches", type=int, default=2,
                    help="minimum touches to report a level (default 2)")
    ap.add_argument("--tol-mode", choices=["pct", "pts"], default="pct",
                    help="cluster tolerance mode (default pct)")
    ap.add_argument("--tol-pct", type=float, default=0.15,
                    help="cluster tolerance as %% of price (default 0.15)")
    ap.add_argument("--tol-pts", type=float, default=20.0,
                    help="cluster tolerance in points (used when --tol-mode pts)")
    ap.add_argument("--spike-mult", type=float, default=8.0,
                    help="drop candles whose range > this x median bar range (default 8.0)")
    ap.add_argument("--no-spike-filter", action="store_true",
                    help="disable bad-tick spike filtering (show raw Groww data)")
    ap.add_argument("--no-indicators", action="store_true",
                    help="hide moving indicators (VWAP / EMA100 / N-Line / Hull)")
    ap.add_argument("--watch", type=int, default=0,
                    help="refresh every N seconds (0 = run once)")
    args = ap.parse_args()

    print(f"{C.CYAN}Authenticating with Groww…{C.RESET}")
    groww, access_token = init_groww()

    while True:
        try:
            run_once(groww, access_token, args)
        except Exception as e:
            print(f"{C.RED}Error: {e}{C.RESET}")
        if args.watch <= 0:
            break
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print(f"\n{C.GREY}stopped.{C.RESET}")
            break


if __name__ == "__main__":
    main()
