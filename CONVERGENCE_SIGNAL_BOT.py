#!/usr/bin/env python3
from __future__ import annotations
"""
CONVERGENCE_SIGNAL_BOT.py
=========================
Multi-strike convergence signal detector for NIFTY/SENSEX options.

Problem it solves
-----------------
MOMENTUM_AUTO_BOT watches ONE strike for velocity over 20 seconds.
By the time it confirms the signal the move is already partially done.

This bot watches ALL ATM±range CE and PE strikes simultaneously in a
5-second window. When 3+ strikes on the SAME side all show positive
velocity above the threshold — that is a CONVERGENCE SIGNAL. This
pattern precedes large directional moves by 15–30 seconds because
institutional order flow hits multiple strikes simultaneously before
the index price catches up.

Detection Logic
---------------
Phase 1 — Snapshot : fetch LTP for all ATM±range CE/PE in ONE batch call.
Phase 2 — Observe  : repeat every poll_sec, building per-strike velocity table.
Phase 3 — Score    : count how many strikes exceed velocity_pct on each side.
Phase 4 — Accel    : split window in half — check if 2nd-half velocity > 1st-half.
Phase 5 — Signal   : convergence_count >= min_convergence → fire alert.
Phase 6 — Cooldown : wait cooldown_sec before next scan.

Outputs
-------
- Console + log file under logs/convergence_bot/
- Telegram alert (STRONG / MODERATE) when signal fires
- .convergence_signals.json  — read by LIVE_DASHBOARD

Config override
---------------
Write convergence_config_override.json (same pattern as momentum bot)
to change params live without restarting.
"""

import os
import sys
import csv
import json
import time
import threading
from collections import deque
from datetime import datetime, timedelta

import pyotp
import requests

# ============================================================
# 1. LOGGING
# ============================================================
def _ts() -> str:
    n = datetime.now()
    return n.strftime(f"%H:%M:%S.{n.microsecond // 1000:03d}")


def _setup_logger():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir  = os.path.join(base_dir, "logs", "convergence_bot")
    os.makedirs(log_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(log_dir, f"Convergence_Bot_{ts}.log")

    class Tee:
        def __init__(self, *streams): self.streams = streams
        def write(self, d):
            for s in self.streams:
                try: s.write(d)
                except Exception: pass
        def flush(self):
            for s in self.streams:
                try: s.flush()
                except Exception: pass

    lf = open(log_path, "a", buffering=1, encoding="utf-8")
    sys.stdout = Tee(sys.stdout, lf)
    sys.stderr = Tee(sys.stderr, lf)
    print(f"[CONV-BOT] Logging → {log_path}")
    return log_path


LOG_PATH = _setup_logger()

# ============================================================
# 2. CREDENTIALS
# ============================================================
API_KEY     = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjQ2NTczODEsImlhdCI6MTc3NjI1NzM4MSwibmJmIjoxNzc2MjU3MzgxLCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCJjMjAzMmM5MS04ZGYzLTRkZDUtYjc5NS0yMGVlOWRhZDhhZjlcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZXNzaW9uSWRcIjpcIjI0OWQ2OGRlLTNjZTgtNGQ4OS05ODJkLWM0N2NmYmI1YzdlNFwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIyNDA5OjQwYzQ6MTBhMzozN2UzOjE4NGI6N2IyOTpiMzBlOjIwZTUsMTcyLjcwLjIxOC4xMzUsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjQ2NTczODE2ODYsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.3kotfZI_EC0lzszHKlXiRdqEQv-O8ubYFh0pgoAT0KsSfdQ1sHmts5UtlaAq4PB6DEwY4X2jZUCD8uBgc2nwXQ"
TOTP_SECRET = "SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4"
from whatsapp_gateway import send_whatsapp as send_telegram, start_webhook_server

# ============================================================
# 3. CONFIG
# ============================================================
PROJECT_ROOT     = os.path.dirname(os.path.abspath(__file__))
CSV_PATH         = os.path.join(PROJECT_ROOT, "instrument.csv")
OI_SNAPSHOT_PATH = os.path.join(PROJECT_ROOT, "oi_snapshot.json")
SIGNALS_PATH     = os.path.join(PROJECT_ROOT, ".convergence_signals.json")

_INDEX_SPOT_SYMBOL = {
    "NIFTY":     "NSE_NIFTY",
    "BANKNIFTY": "NSE_BANKNIFTY",
    "FINNIFTY":  "NSE_FINNIFTY",
    "SENSEX":    "BSE_SENSEX",
    "BANKEX":    "BSE_BANKEX",
}
_INDEX_EXCHANGE = {
    "NIFTY":     "NSE",
    "BANKNIFTY": "NSE",
    "FINNIFTY":  "NSE",
    "SENSEX":    "BSE",
    "BANKEX":    "BSE",
}
_INDEX_STRIKE_STEP = {
    "NIFTY":     50,
    "BANKNIFTY": 100,
    "FINNIFTY":  50,
    "SENSEX":    100,
    "BANKEX":    100,
}


def _next_expiry(weekday: int = 1) -> str:
    """Next occurrence of weekday (0=Mon…6=Sun). NIFTY=Tuesday(1)."""
    today = datetime.now().date()
    days  = (weekday - today.weekday()) % 7 or 7
    return (today + timedelta(days=days)).strftime("%Y-%m-%d")


CONFIG = {
    "index":        "NIFTY",
    "expiry":       _next_expiry(weekday=1),   # Tuesday for NIFTY
    "strike_step":  50,
    "atm_range":    6,     # scan ATM ± 6 strikes → up to 24 symbols (12CE + 12PE)

    # --- convergence detection ---
    "scan_seconds":    5,    # observation window — much shorter than momentum bot
    "poll_sec":        1,    # LTP poll interval during window
    "velocity_pct":  0.8,    # min % move per strike to count as "active"
    "min_convergence": 3,    # min number of active strikes on same side to fire

    # --- premium filter ---
    "min_premium":   30,     # ignore strikes cheaper than this (₹)
    "max_premium":  500,     # ignore strikes more expensive than this (₹)

    # --- acceleration mode ---
    # Split the scan window in half; if 2nd-half velocity ≥ accel_ratio × 1st-half
    # that strike gets an "accelerating" flag. 2+ accelerating strikes = STRONG signal.
    "acceleration_mode": True,
    "accel_ratio":       1.3,

    # --- OI bias (reads oi_snapshot.json written by calculate_oi_pcr.py) ---
    "use_oi_filter":   True,
    "oi_max_age_sec":  180,   # ignore OI snapshot older than 3 minutes

    # --- timing ---
    "market_open":     "09:15",
    "market_close":    "15:25",
    "cooldown_sec":     30,   # wait after a signal fires before scanning again
    "no_signal_wait":    2,   # wait after a no-signal scan before restarting

    # --- signal throttle (prevent duplicate alerts on sustained moves) ---
    "min_signal_interval_sec": 60,
}

_override_path = os.path.join(PROJECT_ROOT, "convergence_config_override.json")
_OVERRIDE_CAST = {
    "index":            str,
    "expiry":           str,
    "atm_range":        int,
    "scan_seconds":     int,
    "poll_sec":         int,
    "velocity_pct":     float,
    "min_convergence":  int,
    "min_premium":      float,
    "max_premium":      float,
    "acceleration_mode": bool,
    "accel_ratio":      float,
    "use_oi_filter":    bool,
    "oi_max_age_sec":   int,
    "cooldown_sec":     int,
    "min_signal_interval_sec": int,
}


def _reload_override(verbose: bool = True) -> None:
    if not os.path.exists(_override_path):
        return
    try:
        with open(_override_path) as f:
            ov = json.load(f)
        applied = {}
        for k, cast in _OVERRIDE_CAST.items():
            if k in ov:
                try:
                    CONFIG[k] = cast(ov[k])
                    applied[k] = CONFIG[k]
                except Exception:
                    pass
        if applied and verbose:
            print(f"[CONFIG] Override applied: {applied}")
    except Exception as e:
        if verbose:
            print(f"[CONFIG] Override read error: {e}")


_reload_override(verbose=True)

# ============================================================
# 4. GROWW INIT
# ============================================================
try:
    from growwapi import GrowwAPI
except ImportError:
    print("❌ growwapi not found. pip install growwapi or place it in PYTHONPATH.")
    sys.exit(1)

_totp_gen    = pyotp.TOTP(TOTP_SECRET)
_session     = requests.Session()
access_token: str | None = None
groww        = None


from groww_token import get_access_token as get_cached_access_token


def groww_init() -> None:
    global groww, access_token
    access_token = get_cached_access_token(API_KEY, TOTP_SECRET)
    groww        = GrowwAPI(access_token)
    print(f"✅ Groww API initialised  [{datetime.now().strftime('%H:%M:%S')}]")

# ============================================================
# 5. WHATSAPP (see whatsapp_gateway.py)
# ============================================================

# ============================================================
# 6. INSTRUMENTS
# ============================================================
instruments_data: list = []


def _load_instruments(spot: float, atm: float) -> None:
    global instruments_data
    if not os.path.exists(CSV_PATH):
        print(f"⚠️  instrument.csv not found at {CSV_PATH} — LTP fetch will fail")
        return

    rows = []
    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    INDEX  = CONFIG["index"].upper()
    EXPIRY = CONFIG["expiry"]
    step   = _INDEX_STRIKE_STEP.get(INDEX, CONFIG["strike_step"])
    CONFIG["strike_step"] = step

    # Load strikes within 2× the atm_range to have buffer when spot moves
    lo = atm - CONFIG["atm_range"] * step * 2
    hi = atm + CONFIG["atm_range"] * step * 2

    filtered = []
    for r in rows:
        if r.get("underlying_symbol", "").upper() != INDEX:
            continue
        if r.get("expiry_date", "").strip() != EXPIRY:
            continue
        try:
            strike = float(r.get("strike_price") or 0)
        except ValueError:
            continue
        if lo <= strike <= hi:
            filtered.append(r)

    instruments_data = filtered
    print(f"✅ Loaded {len(instruments_data)} instruments  (spot={spot:.1f}, ATM={atm:.0f})")

# ============================================================
# 7. BATCH LTP — fetch all strikes in ONE API call
# ============================================================
def _batch_ltp(symbols: list[str]) -> dict[str, float]:
    """
    Fetch LTP for up to 50 exchange symbols per call.
    Returns {exchange_symbol: ltp_float}.
    This is the key efficiency advantage over the momentum bot which
    calls _get_ltp() individually per strike.
    """
    if not symbols:
        return {}
    results: dict[str, float] = {}
    chunk_size = 50
    for i in range(0, len(symbols), chunk_size):
        chunk  = symbols[i:i + chunk_size]
        params = "&".join(f"exchange_symbols={s}" for s in chunk)
        url    = f"https://api.groww.in/v1/live-data/ltp?segment=FNO&{params}"
        hdrs   = {
            "Accept":        "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0",
        }
        try:
            resp = _session.get(url, headers=hdrs, timeout=8)
            if resp.status_code == 401:
                print("  [AUTH] Token expired — re-initialising…")
                groww_init()
                hdrs["Authorization"] = f"Bearer {access_token}"
                resp = _session.get(url, headers=hdrs, timeout=8)
            if resp.status_code == 429:
                print("  [RATE] 429 — sleeping 3s")
                time.sleep(3)
                continue
            payload = resp.json().get("payload", {})
            for sym, val in payload.items():
                if val is not None:
                    try:
                        results[sym] = float(val)
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            print(f"  [BATCH-LTP] Error: {e}")
    return results


def _get_spot() -> float:
    idx = CONFIG.get("index", "NIFTY").upper()
    sym = _INDEX_SPOT_SYMBOL.get(idx, "NSE_NIFTY")
    seg = "CASH"
    url = f"https://api.groww.in/v1/live-data/ltp?segment={seg}&exchange_symbols={sym}"
    hdrs = {
        "Accept":        "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0",
    }
    try:
        resp = _session.get(url, headers=hdrs, timeout=8)
        val  = resp.json().get("payload", {}).get(sym)
        return float(val) if val else 0.0
    except Exception:
        return 0.0

# ============================================================
# 8. OI SNAPSHOT
# ============================================================
def load_oi_snapshot() -> dict | None:
    if not CONFIG.get("use_oi_filter"):
        return None
    try:
        with open(OI_SNAPSHOT_PATH) as f:
            snap = json.load(f)
        age = time.time() - snap.get("timestamp", 0)
        if age > CONFIG["oi_max_age_sec"]:
            return None
        return snap
    except Exception:
        return None


def oi_bias(snap: dict | None) -> str:
    if snap is None:
        return "NEUTRAL"
    writer    = snap.get("writer_bias", "NEUTRAL")
    sentiment = snap.get("sentiment",   "NEUTRAL")
    if writer == sentiment:
        return writer
    pcr = snap.get("pcr_atm", 1.0)
    if pcr > 1.1:
        return "BULLISH"
    if pcr < 0.9:
        return "BEARISH"
    return writer

# ============================================================
# 9. SIGNAL OUTPUT
# ============================================================
_signal_history: list = []


def _write_signal(sig: dict) -> None:
    """Persist signal to .convergence_signals.json for dashboard to read."""
    _signal_history.append(sig)
    try:
        with open(SIGNALS_PATH, "w") as f:
            json.dump({
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total":   len(_signal_history),
                "signals": _signal_history[-100:],
            }, f, indent=2)
    except Exception as e:
        print(f"  [SIGNAL-WRITE] {e}")

# ============================================================
# 10. BUILD SYMBOL MAP
# ============================================================
def _build_symbol_map(spot: float) -> dict[str, dict]:
    """
    Build {exchange_symbol: {strike, opt_type, ts}} for all
    ATM±atm_range CE/PE strikes.
    """
    idx  = CONFIG["index"].upper()
    step = CONFIG["strike_step"]
    atm  = round(spot / step) * step
    rng  = CONFIG["atm_range"]
    exch = _INDEX_EXCHANGE.get(idx, "NSE")

    sym_map: dict[str, dict] = {}
    for inst in instruments_data:
        try:
            strike   = float(inst.get("strike_price") or 0)
            opt_type = inst.get("instrument_type", "").upper()
        except ValueError:
            continue
        if opt_type not in ("CE", "PE"):
            continue
        if abs(strike - atm) > rng * step:
            continue
        ts = (inst.get("internal_trading_symbol") or inst.get("trading_symbol") or "")
        if not ts:
            continue
        ex_sym = f"{exch}_{ts}"
        sym_map[ex_sym] = {
            "strike":   strike,
            "opt_type": opt_type,
            "ts":       ts,
        }
    return sym_map

# ============================================================
# 11. CONVERGENCE SCANNER — CORE
# ============================================================
def scan_convergence(sym_map: dict, oi_snap: dict | None) -> dict | None:
    """
    Observe all strikes for scan_seconds. Return signal dict on convergence,
    None otherwise.

    Algorithm:
    1. Fetch LTP snapshot for all symbols (baseline).
    2. Poll every poll_sec for scan_seconds more ticks.
    3. Compute velocity = (last_ltp - first_ltp) / first_ltp * 100 per strike.
    4. Filter by premium bounds.
    5. Count how many CE strikes and PE strikes cleared velocity_pct.
    6. If count on either side >= min_convergence → signal.
    7. Acceleration bonus: if 2nd-half velocity ≥ accel_ratio × 1st-half on 2+ strikes.
    """
    cfg          = CONFIG
    scan_secs    = cfg["scan_seconds"]
    poll_sec     = cfg["poll_sec"]
    vel_thresh   = cfg["velocity_pct"]
    min_conv     = cfg["min_convergence"]
    accel_mode   = cfg["acceleration_mode"]
    accel_ratio  = cfg["accel_ratio"]
    min_prem     = cfg["min_premium"]
    max_prem     = cfg["max_premium"]
    bias         = oi_bias(oi_snap)

    all_syms  = list(sym_map.keys())
    n_ticks   = max(scan_secs // max(poll_sec, 1), 2)

    # tick_data[sym] = deque of (t, ltp)
    tick_data: dict[str, deque] = {s: deque(maxlen=n_ticks + 2) for s in all_syms}

    print(f"\n  [{_ts()}] 🔭 Scan started: {len(all_syms)} symbols | "
          f"{scan_secs}s win | vel≥{vel_thresh}% | min_conv={min_conv} | bias={bias}")

    # ── Baseline snapshot ─────────────────────────────────────
    baseline = _batch_ltp(all_syms)
    t0 = time.time()
    for sym, ltp in baseline.items():
        tick_data[sym].append((t0, ltp))

    # ── Tick loop ─────────────────────────────────────────────
    for tick_i in range(n_ticks):
        time.sleep(poll_sec)
        now  = time.time()
        ltps = _batch_ltp(all_syms)
        for sym, ltp in ltps.items():
            tick_data[sym].append((now, ltp))

        # Live counter
        ce_act = pe_act = 0
        for sym, tks in tick_data.items():
            if len(tks) < 2:
                continue
            base = tks[0][1]
            last = tks[-1][1]
            if base <= 0:
                continue
            v = (last - base) / base * 100
            if v >= vel_thresh:
                if sym_map[sym]["opt_type"] == "CE":
                    ce_act += 1
                else:
                    pe_act += 1
        print(f"    [{_ts()}] tick {tick_i + 1}/{n_ticks}  "
              f"CE_active={ce_act}  PE_active={pe_act}  "
              f"(+{time.time() - t0:.1f}s)")

    # ── Final scoring ─────────────────────────────────────────
    ce_hits: list[dict] = []
    pe_hits: list[dict] = []

    for sym, tks in tick_data.items():
        if len(tks) < 2:
            continue
        info      = sym_map[sym]
        first_ltp = tks[0][1]
        last_ltp  = tks[-1][1]

        if first_ltp < min_prem or first_ltp > max_prem:
            continue
        if first_ltp <= 0:
            continue

        vel = (last_ltp - first_ltp) / first_ltp * 100

        # Acceleration: split ticks into two halves
        accel = False
        if accel_mode and len(tks) >= 4:
            tklist = list(tks)
            mid    = len(tklist) // 2
            h1     = tklist[:mid]
            h2     = tklist[mid:]
            v1 = (h1[-1][1] - h1[0][1]) / h1[0][1] * 100 if h1[0][1] > 0 else 0.0
            v2 = (h2[-1][1] - h2[0][1]) / h2[0][1] * 100 if h2[0][1] > 0 else 0.0
            if v1 > 0 and v2 >= v1 * accel_ratio:
                accel = True

        if vel >= vel_thresh:
            entry = {
                "sym":        sym,
                "strike":     info["strike"],
                "opt_type":   info["opt_type"],
                "vel_pct":    round(vel, 3),
                "ltp_start":  round(first_ltp, 2),
                "ltp_end":    round(last_ltp, 2),
                "accelerating": accel,
            }
            (ce_hits if info["opt_type"] == "CE" else pe_hits).append(entry)

    ce_hits.sort(key=lambda x: x["vel_pct"], reverse=True)
    pe_hits.sort(key=lambda x: x["vel_pct"], reverse=True)

    ce_count = len(ce_hits)
    pe_count = len(pe_hits)

    print(f"\n  [{_ts()}] 📊 Results: CE={ce_count}  PE={pe_count}  "
          f"(need {min_conv} on same side)")

    # ── OI bias lowers required threshold by 1 for aligned side ─
    ce_min = max(min_conv - (1 if bias == "BULLISH" else 0), 2)
    pe_min = max(min_conv - (1 if bias == "BEARISH" else 0), 2)

    # Acceleration bonus: 2+ accelerating strikes counts as +1 effective
    ce_accel_cnt = sum(1 for h in ce_hits if h["accelerating"])
    pe_accel_cnt = sum(1 for h in pe_hits if h["accelerating"])
    ce_eff = ce_count + (1 if ce_accel_cnt >= 2 else 0)
    pe_eff = pe_count + (1 if pe_accel_cnt >= 2 else 0)

    # Pick dominant side (or BOTH if equal and both qualify)
    signal_side: str | None = None
    signal_hits: list[dict] = []

    if ce_eff >= ce_min and ce_eff > pe_eff:
        signal_side = "CE"
        signal_hits = ce_hits
    elif pe_eff >= pe_min and pe_eff > ce_eff:
        signal_side = "PE"
        signal_hits = pe_hits
    elif ce_eff >= ce_min and pe_eff >= pe_min:
        signal_side = "BOTH"
        signal_hits = ce_hits + pe_hits
    else:
        print(f"  [{_ts()}] ⬜  No convergence  "
              f"CE={ce_count}(eff {ce_eff}/{ce_min})  "
              f"PE={pe_count}(eff {pe_eff}/{pe_min})")
        return None

    # ── Build signal ─────────────────────────────────────────
    accel_total = sum(1 for h in signal_hits if h["accelerating"])
    avg_vel     = round(sum(h["vel_pct"] for h in signal_hits) / len(signal_hits), 3)
    top_strike  = signal_hits[0]["strike"] if signal_hits else 0
    strength    = ("STRONG"
                   if (len(signal_hits) >= min_conv + 1 or accel_total >= 2)
                   else "MODERATE")
    spot_now    = _get_spot()

    return {
        "time":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ts_ms":       int(time.time() * 1000),
        "side":        signal_side,
        "strength":    strength,
        "conv_count":  len(signal_hits),
        "accel_count": accel_total,
        "avg_vel_pct": avg_vel,
        "top_strike":  top_strike,
        "spot":        spot_now,
        "oi_bias":     bias,
        "vel_thresh":  vel_thresh,
        "scan_secs":   scan_secs,
        "hits":        signal_hits[:8],   # top 8 strikes for context
    }

# ============================================================
# 12. SIGNAL FORMATTER
# ============================================================
def _fmt_signal(sig: dict) -> str:
    side    = sig["side"]
    strn    = sig["strength"]
    count   = sig["conv_count"]
    accel   = sig["accel_count"]
    vel     = sig["avg_vel_pct"]
    strike  = sig["top_strike"]
    spot    = sig["spot"]
    bias    = sig["oi_bias"]
    ts      = sig["time"]

    icon  = "🔴⚡" if side == "CE" else ("🟢⚡" if side == "PE" else "⚡⚡")
    label = f"{icon} [{strn}] CONVERGENCE — {side}"

    strikes_str = "  ".join(
        f"{h['strike']}{h['opt_type']} {h['vel_pct']:+.1f}%{'🚀' if h['accelerating'] else ''}"
        for h in sig.get("hits", [])[:6]
    )

    lines = [
        label,
        f"Strikes converged : {count}  |  Avg velocity: {vel:+.2f}%",
    ]
    if accel >= 2:
        lines.append(f"Accelerating      : {accel} strikes 🚀")
    lines += [
        f"Top strike        : {strike}  |  Spot: {spot:.1f}",
        f"OI bias           : {bias}",
        f"Active strikes    : {strikes_str}",
        f"Time              : {ts}",
    ]
    return "\n".join(lines)

# ============================================================
# 13. SIGNAL THROTTLE
# ============================================================
_last_signal_ts: dict[str, float] = {"CE": 0.0, "PE": 0.0, "BOTH": 0.0}


def _throttled(side: str) -> bool:
    interval = CONFIG.get("min_signal_interval_sec", 60)
    return (time.time() - _last_signal_ts.get(side, 0.0)) < interval


def _mark_signal(side: str) -> None:
    _last_signal_ts[side] = time.time()

# ============================================================
# 14. MARKET HOURS
# ============================================================
def _in_market_hours() -> bool:
    now    = datetime.now()
    open_  = datetime.strptime(CONFIG["market_open"],  "%H:%M").replace(
        year=now.year, month=now.month, day=now.day)
    close_ = datetime.strptime(CONFIG["market_close"], "%H:%M").replace(
        year=now.year, month=now.month, day=now.day)
    return open_ <= now <= close_

# ============================================================
# 15. MAIN LOOP
# ============================================================
def main() -> None:
    print("\n" + "═" * 60)
    print("   CONVERGENCE SIGNAL BOT  v1.0")
    print("   Multi-strike convergence detector")
    print("   Detects 3+ strike simultaneous velocity → early signal")
    print("═" * 60)

    groww_init()

    spot = _get_spot()
    if spot <= 0:
        print("❌ Could not fetch spot price. Check credentials / market hours.")
        sys.exit(1)

    step = _INDEX_STRIKE_STEP.get(CONFIG["index"].upper(), 50)
    atm  = round(spot / step) * step
    _load_instruments(spot, atm)

    if not instruments_data:
        print("❌ No instruments loaded. Check instrument.csv and expiry date.")
        sys.exit(1)

    print(f"\n  Index    : {CONFIG['index']}  |  Expiry : {CONFIG['expiry']}")
    print(f"  Spot     : {spot:.1f}  |  ATM    : {atm:.0f}")
    print(f"  Range    : ATM ± {CONFIG['atm_range']} strikes")
    print(f"  Velocity : ≥{CONFIG['velocity_pct']}%  |  Min conv: {CONFIG['min_convergence']} strikes")
    print(f"  Win      : {CONFIG['scan_seconds']}s  |  Accel: {CONFIG['acceleration_mode']}")
    print(f"  OI bias  : {CONFIG['use_oi_filter']}  |  Cooldown: {CONFIG['cooldown_sec']}s")
    print()

    scan_count   = 0
    signal_count = 0

    while True:
        _reload_override(verbose=False)

        # ── Market hours guard ─────────────────────────────────
        if not _in_market_hours():
            print(f"[{_ts()}] Outside market hours "
                  f"({CONFIG['market_open']}–{CONFIG['market_close']}). Sleeping 60s…")
            time.sleep(60)
            continue

        scan_count += 1
        print(f"\n{'─' * 60}")
        print(f"  [{_ts()}] SCAN #{scan_count}  (signals today: {signal_count})")

        # ── Refresh spot + symbol map every 5 scans ────────────
        if scan_count % 5 == 1:
            spot = _get_spot()
            step = _INDEX_STRIKE_STEP.get(CONFIG["index"].upper(), 50)
            atm  = round(spot / step) * step
            print(f"  Spot: {spot:.1f}  ATM: {atm:.0f}")

        oi_snap  = load_oi_snapshot()
        sym_map  = _build_symbol_map(spot)

        if len(sym_map) < 4:
            print(f"  ⚠️  Symbol map too small ({len(sym_map)}). Reloading instruments…")
            _load_instruments(spot, atm)
            sym_map = _build_symbol_map(spot)

        print(f"  OI bias: {oi_bias(oi_snap)}  |  Watching {len(sym_map)} symbols")

        sig = scan_convergence(sym_map, oi_snap)

        if sig is None:
            time.sleep(CONFIG["no_signal_wait"])
            continue

        # ── Signal fired ───────────────────────────────────────
        side = sig["side"]

        if _throttled(side):
            age = int(time.time() - _last_signal_ts.get(side, 0))
            print(f"  [{_ts()}] ⏸  Throttled — last {side} signal {age}s ago "
                  f"(min={CONFIG['min_signal_interval_sec']}s)")
            time.sleep(CONFIG["no_signal_wait"])
            continue

        signal_count += 1
        _mark_signal(side)
        _write_signal(sig)

        msg = _fmt_signal(sig)
        print(f"\n{'═' * 60}")
        print(msg)
        print(f"{'═' * 60}")
        send_telegram(msg)

        print(f"\n  ✅ Signal #{signal_count} saved → {SIGNALS_PATH}")
        print(f"  💤 Cooldown {CONFIG['cooldown_sec']}s…")
        time.sleep(CONFIG["cooldown_sec"])


if __name__ == "__main__":
    main()
