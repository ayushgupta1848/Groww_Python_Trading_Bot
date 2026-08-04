#!/usr/bin/env python3
"""
LIVE_DASHBOARD.py
=================
Single-file live HTML dashboard — reads ALL bot logs, serves at http://localhost:8765
No API calls. No external dependencies. Pure Python stdlib + log files.

Start bots first, then:  python3 LIVE_DASHBOARD.py
Open browser:            http://localhost:8765
"""
from __future__ import annotations
import os, json, time, re as _re, threading, csv, sys
import requests as _req
from datetime import datetime
from typing import Optional
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

BASE        = os.path.dirname(os.path.abspath(__file__))
PROD10_BRIDGE_FILE = os.path.join(BASE, ".prod10_bridge_cmd.json")
PORT        = 8765
REFRESH_SEC = 15
STALE_SECS  = 300

# ─────────────────────────────────────────────────────────────
#  BOT CONTROL CENTER — registry + terminal-based process management
# ─────────────────────────────────────────────────────────────
import subprocess as _bsp
import signal as _signal

# Bounds mirror the layout in START_ALL_BOTS.command (x1, y1, x2, y2)
_BOT_REGISTRY = [
    {"id":"oi_pcr",        "name":"OI PCR Analyzer",
     "script":"calculate_oi_pcr.py",
     "desc":"Live OI + PCR data — feeds OI Intelligence tab",
     "terminal": True,  "bounds": ""},          # terminal so output is visible
    {"id":"premium",       "name":"Premium Direction Tracker",
     "script":"PREMIUM_DIRECTION_TRACKER.py",
     "desc":"CE/PE premium flow + momentum tracker",
     "terminal": True, "bounds": "0, 25, 490, 400"},
    {"id":"fibo",          "name":"Fibonacci Analyzer",
     "script":"FIBONACCI_TREND_ANALYZER.py",
     "desc":"Fibonacci trend levels + 1h/15m direction",
     "terminal": True, "bounds": "490, 25, 980, 400"},
    {"id":"master_signal", "name":"Master Signal Bot",
     "script":"MASTER_SIGNAL_BOT.py",
     "desc":"Multi-TF CE/PE signal generator",
     "terminal": True, "bounds": "980, 25, 1470, 400"},
    {"id":"chart_level",   "name":"Chart Level Analyzer",
     "script":"CHART_LEVEL_ANALYZER.py",
     "desc":"Multi-TF S/R levels with strength scoring",
     "terminal": True, "bounds": "0, 400, 490, 874"},
    {"id":"signal_monitor","name":"Signal Monitor",
     "script":"SIGNAL_MONITOR.py",
     "desc":"Tracks signal accuracy across bots",
     "terminal": True, "bounds": "490, 400, 980, 874"},
    {"id":"trade_bot",     "name":"Trade Bot (PROD10FEB)",
     "script":"PROD10FEB_ManualBOT_groww_option_trading_final_bot.py",
     "desc":"Main trading bot — places real orders on Groww",
     "terminal": True, "bounds": "980, 400, 1470, 874"},
    {"id":"momentum",      "name":"Momentum Auto Bot",
     "script":"MOMENTUM_AUTO_BOT.py",
     "desc":"Premium velocity scanner — auto-trades on momentum",
     "terminal": True, "bounds": ""},           # no fixed position — opens wherever
    {"id":"trendline_scanner", "name":"Trendline Scanner Bot",
     "script":"TRENDLINE_SCANNER_BOT.py",
     "desc":"Ascending trendline bounce/break signals on option premiums",
     "terminal": True, "bounds": ""},
]

_PY_BIN = sys.executable   # same Python that runs the dashboard — guaranteed to have all packages

_bot_procs: dict = {}   # {bot_id: Popen} — only for non-terminal (oi_pcr)
_bot_logs:  dict = {}   # {bot_id: list[str]}
_bot_lock   = threading.Lock()
_BOT_MAX_LOG = 200

_alert_state:    dict = {}   # {log_file_path: byte_offset} — tracks how far we've read each log
_alert_bot_idx:  dict = {}   # {bot_id: int} — last processed index in _bot_logs[bot_id]
_alert_dedup:    dict = {}   # {(source,type,msg_key): last_fired_time} — suppress repeats < 5 min
_oi_snap_last:   dict = {}   # last seen oi_snapshot.json values for change detection
_consensus_last: dict = {"signal": ""}  # last seen consensus signal

def _bot_log_reader(bot_id: str, proc):
    """Captures stdout for background (non-terminal) bots only."""
    try:
        for line in proc.stdout:
            line = line.rstrip()
            with _bot_lock:
                buf = _bot_logs.setdefault(bot_id, [])
                buf.append(line)
                if len(buf) > _BOT_MAX_LOG:
                    del buf[:-_BOT_MAX_LOG]
    except Exception:
        pass

def _bot_start(bot_id: str, config: dict = None) -> dict:
    bot = next((b for b in _BOT_REGISTRY if b["id"] == bot_id), None)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {bot_id}"}
    script = os.path.join(BASE, bot["script"])
    if not os.path.exists(script):
        return {"ok": False, "error": f"Script not found: {bot['script']}"}

    # Write config override file for momentum bot before launch
    if bot_id == "momentum" and isinstance(config, dict):
        _allowed = {"trade_mode", "index", "expiry", "lots",
                    "exit_mode", "min_premium", "max_premium", "atm_range",
                    "validate_orders", "scan_seconds", "poll_seconds",
                    "choppiness_enabled",
                    "consec_sl_brake", "consec_sl_pause_min",
                    "HARD_SL_ATR_BASED", "HARD_SL_ATR_MULTIPLIER",
                    "atr_source",
                    "min_score_filter", "velocity_filter"}
        override = {k: v for k, v in config.items() if k in _allowed}
        if override:
            try:
                ov_path = os.path.join(BASE, "momentum_config_override.json")
                with open(ov_path, "w") as _f:
                    json.dump(override, _f)
            except Exception as _e:
                return {"ok": False, "error": f"Could not write config override: {_e}"}

    if not bot["terminal"]:
        # Background subprocess (oi_pcr) — capture output
        with _bot_lock:
            existing = _bot_procs.get(bot_id)
            if existing and existing.poll() is None:
                return {"ok": False, "error": "Already running"}
        try:
            proc = _bsp.Popen(
                [_PY_BIN, script], stdout=_bsp.PIPE, stderr=_bsp.STDOUT,
                text=True, bufsize=1, cwd=BASE
            )
            with _bot_lock:
                _bot_procs[bot_id] = proc
                _bot_logs[bot_id] = []
            threading.Thread(target=_bot_log_reader, args=(bot_id, proc), daemon=True).start()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Terminal bot — open via osascript like START_ALL_BOTS.command
    r = _bsp.run(["pgrep", "-f", bot["script"]], capture_output=True)
    if r.returncode == 0:
        return {"ok": False, "error": "Already running"}

    name  = bot["name"]
    bnds  = bot["bounds"]
    bounds_line = f"set bounds of w to {{{bnds}}}" if bnds else ""
    osa = f"""tell application "Terminal"
    activate
    do script "cd '{BASE}' && clear && echo '  {name}' && '{_PY_BIN}' '{script}'"
    delay 0.6
    set w to front window
    try
        set current settings of w to settings set "Pro"
    end try
    {bounds_line}
end tell"""
    try:
        _bsp.Popen(["osascript", "-e", osa])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _bot_stop(bot_id: str) -> dict:
    bot = next((b for b in _BOT_REGISTRY if b["id"] == bot_id), None)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {bot_id}"}
    try:
        if not bot["terminal"]:
            with _bot_lock:
                proc = _bot_procs.get(bot_id)
            if proc:
                proc.terminate()
                try: proc.wait(timeout=3)
                except Exception: proc.kill()
                with _bot_lock:
                    _bot_procs[bot_id] = None
        else:
            _bsp.run(["pkill", "-f", bot["script"]])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _bot_status_all() -> dict:
    out = {}
    for b in _BOT_REGISTRY:
        if not b["terminal"]:
            with _bot_lock:
                p = _bot_procs.get(b["id"])
            out[b["id"]] = "running" if (p and p.poll() is None) else "stopped"
        else:
            r = _bsp.run(["pgrep", "-f", b["script"]], capture_output=True)
            out[b["id"]] = "running" if r.returncode == 0 else "stopped"
    return out

_BOT_LOG_DIRS = {
    "trendline_scanner": ("logs/trendline_bot",  "TrendlineBot_"),
    "momentum":          ("logs/momentum_bot",   "Momentum_Bot_"),
    "trade_bot":         ("logs/groww_bot",      "Groww_Bot_"),
}

# ─────────────────────────────────────────────────────────────
#  DECISION ENGINE (trading_decision_engine) PROCESS MANAGER
# ─────────────────────────────────────────────────────────────
_DE_PROC_MARK = "trading_decision_engine.app.run"   # pgrep/pkill marker
_de_expiry_cache: dict = {}                          # {index: (fetched_at, [expiries])}

def _engine_running() -> dict:
    r = _bsp.run(["pgrep", "-f", _DE_PROC_MARK], capture_output=True, text=True)
    pids = [p for p in r.stdout.split() if p.strip()]
    return {"running": bool(pids), "pid": int(pids[0]) if pids else None}

def _engine_expiries(index: str) -> list:
    """Live expiries for an index from instrument.csv (nearest first), cached 10 min."""
    import csv as _csv
    from datetime import date as _date
    now = time.time()
    cached = _de_expiry_cache.get(index)
    if cached and now - cached[0] < 600:
        return cached[1]
    expiries = set()
    try:
        today = _date.today().isoformat()
        with open(os.path.join(BASE, "instrument.csv"), newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                if row.get("underlying_symbol") == index and row.get("expiry_date", "") >= today:
                    expiries.add(row["expiry_date"])
    except Exception:
        pass
    out = sorted(expiries)[:6]
    _de_expiry_cache[index] = (now, out)
    return out

def _engine_start(cfg: dict) -> dict:
    if _engine_running()["running"]:
        return {"ok": False, "error": "Decision engine already running"}
    mode = str(cfg.get("mode", "shadow")).lower()
    if mode not in ("live", "shadow"):
        return {"ok": False, "error": f"Mode must be live or shadow (got {mode!r})"}
    # LIVE places real orders with real money — the UI must send explicit confirmation,
    # mirroring the CLI's type-'yes' guard.
    if mode == "live" and str(cfg.get("confirm_live", "")).strip().upper() != "YES":
        return {"ok": False, "error": "LIVE mode requires typing YES in the confirmation box"}
    try:
        index   = str(cfg.get("index", "NIFTY")).upper()
        expiry  = str(cfg.get("expiry", "")).strip()
        lots    = int(cfg.get("lots", 1))
        pmin    = float(cfg.get("premium_min", 60))
        pmax    = float(cfg.get("premium_max", 250))
        profile = str(cfg.get("profile", "")).strip()
        validate = bool(cfg.get("validate_orders", True))
        if not expiry:
            return {"ok": False, "error": "Expiry is required"}
        if lots < 1 or pmin >= pmax:
            return {"ok": False, "error": "Check lots / premium range"}
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": f"Bad config value: {e}"}

    cmd = [_PY_BIN, "-m", "trading_decision_engine.app.run",
           "--mode", mode, "--index", index, "--expiry", expiry,
           "--lots", str(lots), "--premium-min", str(pmin), "--premium-max", str(pmax),
           "--validate-orders" if validate else "--no-validate-orders",
           "--no-dashboard"]   # headless: this browser tab IS the dashboard
    if profile:
        cmd += ["--profile", profile]
    try:
        proc = _bsp.Popen(cmd, stdout=_bsp.PIPE, stderr=_bsp.STDOUT, text=True, bufsize=1, cwd=BASE)
        with _bot_lock:
            _bot_procs["decision_engine"] = proc
            _bot_logs["decision_engine"] = []
        threading.Thread(target=_bot_log_reader, args=("decision_engine", proc), daemon=True).start()
        return {"ok": True, "pid": proc.pid, "cmd": " ".join(cmd)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _engine_stop() -> dict:
    try:
        with _bot_lock:
            proc = _bot_procs.get("decision_engine")
        if proc and proc.poll() is None:
            # SIGINT first: run.py's handler stops the feed and prints/saves session stats.
            proc.send_signal(_signal.SIGINT)
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.terminate()
                try: proc.wait(timeout=3)
                except Exception: proc.kill()
        else:
            # Started outside the dashboard — best effort, INT for a clean shutdown.
            _bsp.run(["pkill", "-INT", "-f", _DE_PROC_MARK])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _bot_get_logs(bot_id: str, n: int = 60) -> list:
    # Try in-memory pipe capture first (bots launched by dashboard)
    with _bot_lock:
        cached = list(_bot_logs.get(bot_id, [])[-n:])
    if cached:
        return cached
    # Fall back to reading the latest log file on disk (bots launched from terminal)
    if bot_id in _BOT_LOG_DIRS:
        subdir, prefix = _BOT_LOG_DIRS[bot_id]
        path = _latest(subdir, prefix)
        if path:
            try:
                lines = open(path, encoding="utf-8", errors="ignore").readlines()
                return [l.rstrip("\n") for l in lines[-n:]]
            except Exception:
                pass
    return []

# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def _latest(subdir: str, prefix: str, ext=".log") -> Optional[str]:
    d = os.path.join(BASE, subdir)
    if not os.path.isdir(d): return None
    files = sorted([f for f in os.listdir(d) if f.startswith(prefix) and f.endswith(ext)], reverse=True)
    return os.path.join(d, files[0]) if files else None

def _parse_ts(s: str) -> Optional[datetime]:
    from datetime import timedelta
    now = datetime.now()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            # If parsed timestamp is in the future (e.g. log from yesterday with today's date prepended)
            # roll back one day to get the correct staleness
            if dt > now:
                dt -= timedelta(days=1)
            return dt
        except ValueError: pass
    # time-only like "11:34:22" — assume today, roll back if future
    m = _re.match(r'^(\d{2}:\d{2}:\d{2})$', s.strip())
    if m:
        t = datetime.strptime(m.group(1), "%H:%M:%S")
        dt = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        if dt > now:
            dt -= timedelta(days=1)
        return dt
    return None

def _age(ts: str) -> str:
    dt = _parse_ts(ts)
    if not dt: return "?"
    s = max(0, (datetime.now() - dt).total_seconds())
    if s < 60:   return f"{int(s)}s ago"
    if s < 3600: return f"{int(s//60)}m ago"
    return f"{int(s//3600)}h ago"

def _stale(ts: str, secs: int = STALE_SECS) -> bool:
    dt = _parse_ts(ts)
    return True if not dt else (datetime.now() - dt).total_seconds() > secs

def _tag(d: dict, stale_secs: int = STALE_SECS) -> dict:
    ts = d.get("ts","")
    d["_age"]  = _age(ts)
    d["_live"] = not _stale(ts, stale_secs)
    return d

# ─────────────────────────────────────────────────────────────
#  READERS
# ─────────────────────────────────────────────────────────────
def _tail(path: str, max_bytes: int = 200_000) -> str:
    """Read only the last max_bytes of a file (avoids loading huge logs fully)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""

def read_master() -> dict:
    path = _latest("logs/master_signal", "Master_Signal_")
    if not path: return {}
    try:
        for raw in reversed(open(path, encoding="utf-8", errors="ignore").readlines()):
            try:
                d = json.loads(raw.strip())
                if d.get("ts"): return _tag(d, 150)
            except Exception: pass
    except Exception: pass
    return {}

def read_fibo() -> dict:
    path = _latest("logs/fibo_analyzer", "Fibo_Analyzer_")
    if not path: return {}
    content = _tail(path, 120_000)   # last ~120 KB covers several full blocks
    if not content: return {}

    hdrs = list(_re.finditer(
        r'FIBONACCI ANALYZER\s+\|\s+(\w+)\s+\|\s+([\d\-: ]+)\s+\|\s+Spot (\d+)', content))
    if not hdrs: return {}
    last = hdrs[-1]; seg = content[last.start():]

    r: dict = {"ts": last.group(2).strip(), "index": last.group(1).strip(),
               "spot": float(last.group(3)), "fib_levels": [], "confluence": []}
    _tag(r, 200)

    dm = _re.search(r'DAY FIB\s+H\s+([\d.]+)\s+L\s+([\d.]+)\s+\([\d]+ pts\s+(\w+) day\)', seg)
    if dm: r.update({"day_high": float(dm.group(1)), "day_low": float(dm.group(2)), "day_dir": dm.group(3)})

    for m in _re.finditer(r'^\s+([\d.]+)\s+([\w%.]+)\s+([+-][\d.]+) pts', seg, _re.MULTILINE):
        lbl = m.group(2).strip()
        r["fib_levels"].append({"price": float(m.group(1)), "label": lbl, "dist_pts": float(m.group(3))})
        if "SWING_LOW"  in lbl: r["swing_low_15m"]  = float(m.group(1))
        if "SWING_HIGH" in lbl: r["swing_high_15m"] = float(m.group(1))

    for m in _re.finditer(r'(\*+)\s+([\d.]+)\s+([+\-][\d.]+) pts\s+\[([^\]]+)\]', seg):
        r["confluence"].append({"stars": len(m.group(1)), "price": float(m.group(2)),
                                 "dist_pts": float(m.group(3)), "tags": m.group(4)})
    r["confluence"].sort(key=lambda x: -x["stars"])

    h1 = _re.search(r'1-HR\s+→\s+(\w+)\s+→\s+(.+)', seg)
    if h1: r["zone_1h"] = f"{h1.group(1)} — {h1.group(2).strip()}"

    tr = _re.search(r'PE trigger:\s*([^|]+)\s*\|\s*CE trigger:\s*(.+)', seg)
    if tr: r.update({"pe_trigger": tr.group(1).strip(), "ce_trigger": tr.group(2).strip()})

    su = _re.search(r'--- SUMMARY ---\n(.*?)(?=\n\n|\Z)', seg, _re.DOTALL)
    if su: r["summary"] = " │ ".join(l.strip() for l in su.group(1).strip().splitlines() if l.strip())

    ts2 = _re.search(r'TRADE SETUP\s*─+\n(.*?)(?=\n\n|\Z)', seg, _re.DOTALL)
    if ts2: r["trade_setup"] = " │ ".join(l.strip() for l in ts2.group(1).strip().splitlines() if l.strip())

    return r

def read_chart_signal() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    p = os.path.join(BASE, "logs", "chart_level", f"signals_{today}.jsonl")
    if not os.path.exists(p): return {}
    try:
        lines = [l.strip() for l in open(p, encoding="utf-8") if l.strip()]
        return _tag(json.loads(lines[-1]), 90) if lines else {}
    except Exception: return {}

def read_chart_decision() -> dict:
    path = _latest("logs/chart_level", "Chart_Level_")
    if not path: return {}
    content = _tail(path, 300_000)   # last ~300 KB — chart log grows fast, keep enough for regex
    if not content: return {}
    decs = list(_re.finditer(r'TRADE DECISION\s+\│\s+(.+)', content))
    opts = list(_re.finditer(r'OPTION SUGGESTION\s+\│\s+(.+)', content))
    tss  = list(_re.finditer(r'(\d{2}:\d{2}:\d{2})', content))
    ts   = (datetime.now().strftime("%Y-%m-%d ") + tss[-1].group(1)) if tss else ""

    # Parse most recent SPOT line: "SPOT:  23,603.25"
    spot_matches = list(_re.finditer(r'SPOT:\s+([\d,]+\.?\d*)', content))
    spot = 0.0
    if spot_matches:
        try: spot = float(spot_matches[-1].group(1).replace(",", ""))
        except ValueError: pass

    # Parse most recent live option LTP from log line:
    # "💡 BUY NIFTY 23500 PE  │  LTP ₹98  │ ..."
    # The log is ANSI-stripped, so just look for the pattern
    current_ltp = 0.0; current_strike = 0; current_dir = ""
    ltp_by_key: dict = {}
    buy_lines = list(_re.finditer(
        r'BUY\s+\w+\s+(\d+)\s+(CE|PE)\s+.*?LTP\s+[₹Rs]*\s*([\d.]+)', content))
    for bl in buy_lines:
        try:
            ltp_by_key[f"{bl.group(1)}_{bl.group(2)}"] = float(bl.group(3))
        except (ValueError, IndexError):
            pass
    if buy_lines:
        m = buy_lines[-1]
        try:
            current_strike = int(m.group(1))
            current_dir    = m.group(2)
            current_ltp    = float(m.group(3))
        except (ValueError, IndexError):
            pass

    return _tag({"ts": ts, "spot": spot,
                 "decision":       decs[-1].group(1).strip() if decs else "",
                 "option_text":    opts[-1].group(1).strip() if opts else "",
                 "current_ltp":    current_ltp,
                 "current_strike": current_strike,
                 "current_dir":    current_dir,
                 "ltp_by_key":     ltp_by_key}, 90)

def read_premium() -> dict:
    path = _latest("logs/premium_tracker", "Premium_Tracker_")
    if not path: return {}
    try:
        for raw in reversed(open(path, encoding="utf-8", errors="ignore").readlines()):
            m = _re.search(r'\[(\d{2}:\d{2}:\d{2})\]\s+SPOT\s+([\d.]+)\s+(.+)', raw.strip())
            if not m: continue
            ts   = datetime.now().strftime("%Y-%m-%d ") + m.group(1)
            line = m.group(3).strip()
            cem  = _re.search(r'\((\d+)\s+CE\)\s+→\s+(\S+)\s+₹\s*([\d.]+)', line)
            pem  = _re.search(r'\((\d+)\s+PE\)\s+→\s+(\S+)\s+₹\s*([\d.]+)', line)
            return _tag({"ts": ts, "spot": float(m.group(2)), "raw": line,
                          "ce_strike": int(cem.group(1))   if cem else None,
                          "ce_flow":   cem.group(2)         if cem else "",
                          "ce_ltp":    float(cem.group(3))  if cem else 0,
                          "pe_strike": int(pem.group(1))   if pem else None,
                          "pe_flow":   pem.group(2)         if pem else "",
                          "pe_ltp":    float(pem.group(3))  if pem else 0}, 90)
    except Exception: pass
    return {}

def read_trade_bot() -> dict:
    path = _latest("logs/groww_bot", "Groww_Bot_")
    if not path: return {}
    try: lines = open(path, encoding="utf-8", errors="ignore").readlines()
    except Exception: return {}
    r: dict = {"active": False}
    recent = lines[-120:]
    for raw in reversed(recent):
        raw = raw.strip()
        tm = _re.search(r'\[(\d{2}:\d{2}:\d{2})', raw)
        if tm and not r.get("ts"):
            r["ts"] = datetime.now().strftime("%Y-%m-%d ") + tm.group(1)
            _tag(r)
        if "Trade cycle completed" in raw or "Ready for next trade" in raw:
            r["active"] = False; r["status"] = "Idle — ready for next trade"
        if "Monitoring" in raw and "LTP last seen" in raw:
            lm = _re.search(r'₹([\d.]+)', raw)
            if lm: r["last_ltp"] = float(lm.group(1)); r["active"] = True
        if "Trailing started" in raw:
            sm = _re.search(r'Dynamic SL:\s*([\d.]+)', raw)
            if sm: r["trailing_sl"] = float(sm.group(1))
        if "Entry price" in raw:
            em = _re.search(r'₹([\d.]+)', raw)
            if em: r["entry_price"] = float(em.group(1))
        if "Parsing symbol" in raw:
            sym = _re.search(r'Parsing symbol:\s*(\S+)', raw)
            if sym and not r.get("symbol"): r["symbol"] = sym.group(1)
    return r

def read_momentum_bot() -> dict:
    path = _latest("logs/momentum_bot", "Momentum_Bot_")
    if not path: return {}
    try: lines = open(path, encoding="utf-8", errors="ignore").readlines()
    except Exception: return {}
    r: dict = {"active": False}
    recent = lines[-80:]
    for raw in reversed(recent):
        raw = raw.strip()
        tm = _re.search(r'\[(\d{2}:\d{2}:\d{2})', raw)
        if tm and not r.get("ts"):
            r["ts"] = datetime.now().strftime("%Y-%m-%d ") + tm.group(1)
            _tag(r)
        if "Cooldown" in raw or "No momentum signal" in raw or "session complete" in raw.lower():
            r["status"] = "Idle"; r["active"] = False
        if "MOMENTUM ENTRY" in raw:
            r["active"] = True; r["status"] = "In trade"
        if "Trail active" in raw or "Trail |" in raw:
            r["active"] = True; r["status"] = "Trailing"
        if "SIGNAL:" in raw and "vel=" in raw and not r.get("status"):
            r["status"] = "Signal found"
        if "CLOSED" in raw or "SELL placed" in raw:
            r["active"] = False; r["status"] = "Trade closed"
    return r

def read_trendline_bot() -> dict:
    """Read trendline scanner status from .trendline_signals.json"""
    sig_file = os.path.join(BASE, ".trendline_signals.json")
    path = _latest("logs/trendline_bot", "TrendlineBot_")
    r: dict = {}
    if path:
        try:
            lines = open(path, encoding="utf-8", errors="ignore").readlines()
        except Exception:
            lines = []
        recent = lines[-20:]
        for raw in reversed(recent):
            tm = _re.search(r'\[?(\d{2}:\d{2}:\d{2})', raw)
            if tm and not r.get("ts"):
                r["ts"] = datetime.now().strftime("%Y-%m-%d ") + tm.group(1)
                _tag(r)
                break
    if os.path.exists(sig_file):
        try:
            with open(sig_file) as f:
                data = json.load(f)
            r["signals"] = data.get("signals", [])
            r["active_trade"] = data.get("active_trade")
            r["stats"] = data.get("stats", {})
            if data.get("ts") and not r.get("ts"):
                r["ts"] = data["ts"]
                _tag(r)
        except Exception:
            pass
    return r

def read_signal_monitor() -> dict:
    path = _latest("logs/signal_monitor", "Signal_Monitor_")
    if not path: return {}
    content = _tail(path, 60_000)    # last ~60 KB — only need the most recent signals
    if not content: return {}
    combined = list(_re.finditer(r'STRONG CE|STRONG PE|✅\s+\S+ CE|✅\s+\S+ PE', content, _re.IGNORECASE))
    pdt  = list(_re.finditer(r'PDT signal\s+(\w+)',  content))
    fibo = list(_re.finditer(r'FIBO signal\s+(\w+)', content))
    tss  = list(_re.finditer(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}', content))
    ts   = tss[-1].group(0) if tss else ""
    return _tag({"ts": ts,
                 "combined": combined[-1].group(0).strip() if combined else "",
                 "pdt":  pdt[-1].group(1)  if pdt  else "",
                 "fibo": fibo[-1].group(1) if fibo else ""}, 150)

def read_live_chain() -> dict:
    path = os.path.join(BASE, "logs", "chart_level", "live_chain.json")
    try:
        with open(path, encoding="utf-8") as f:
            return _tag(json.load(f), 90)
    except Exception:
        return {}

def _update_oi_history(snap: dict):
    """Append a new OI tick to the rolling history buffer if it has a new timestamp."""
    if not snap or not snap.get("time"):
        return
    with _oi_history_lock:
        if _oi_history and _oi_history[-1].get("time") == snap.get("time"):
            return  # same tick, skip
        entry = {
            "time":          snap.get("time", ""),
            "pcr_all":       snap.get("pcr_all", 0),
            "pcr_atm":       snap.get("pcr_atm", 0),
            "total_oi_ce":   snap.get("total_oi_ce", 0),
            "total_oi_pe":   snap.get("total_oi_pe", 0),
            "total_chg_ce":  snap.get("total_chg_ce", 0),
            "total_chg_pe":  snap.get("total_chg_pe", 0),
            "sentiment":     snap.get("sentiment", ""),
            "writer_bias":   snap.get("writer_bias", "NEUTRAL"),
            "atm":           snap.get("atm", 0),
            "price":         snap.get("price", 0),
            "market_signal": snap.get("market_signal", ""),
            "bull_score_v2": snap.get("bull_score_v2", 0),
            "bear_score_v2": snap.get("bear_score_v2", 0),
            "momentum_score":snap.get("momentum_score", 0),
        }
        _oi_history.append(entry)
        if len(_oi_history) > _OI_HISTORY_MAX:
            _oi_history[:] = _oi_history[-_OI_HISTORY_MAX:]

_VIX_CACHE_FILE = os.path.join(BASE, ".vix_cache.json")

def _update_vix_history(vix: float, ts: str):
    """Append a new VIX tick; skip duplicates. Records session-open on first call."""
    if not vix:
        return
    with _vix_history_lock:
        if _vix_history and _vix_history[-1].get("t") == ts:
            return
        if not _vix_session_open[0]:
            _vix_session_open[0] = round(vix, 2)
        _vix_history.append({"t": ts, "v": round(vix, 2)})
        if len(_vix_history) > _VIX_HISTORY_MAX:
            _vix_history[:] = _vix_history[-_VIX_HISTORY_MAX:]
        # Persist to disk so data survives server restarts
        try:
            with open(_VIX_CACHE_FILE, "w") as _f:
                import json as _json
                _json.dump({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "session_open": _vix_session_open[0],
                    "history": list(_vix_history)
                }, _f)
        except Exception:
            pass

def _load_vix_cache():
    """On startup: load today's VIX history from disk if it exists."""
    try:
        with open(_VIX_CACHE_FILE) as f:
            import json as _json
            data = _json.load(f)
        if data.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return  # stale — different day, discard
        hist = data.get("history", [])
        if not hist:
            return
        with _vix_history_lock:
            _vix_history.clear()
            _vix_history.extend(hist[-_VIX_HISTORY_MAX:])
            if not _vix_session_open[0] and data.get("session_open"):
                _vix_session_open[0] = data["session_open"]
        print(f"[VIX] Loaded {len(hist)} cached ticks from disk (session open: {data.get('session_open')})")
    except (FileNotFoundError, Exception):
        pass

def _vix_fetch_loop():
    """Background thread: poll India VIX from NSE allIndices every 2 min."""
    import requests as _req
    _sess = _req.Session()
    _sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
    })
    # Prime the session cookie
    try:
        _sess.get("https://www.nseindia.com/", timeout=5)
    except Exception:
        pass
    while True:
        try:
            r = _sess.get("https://www.nseindia.com/api/allIndices", timeout=7)
            for item in r.json().get("data", []):
                if item.get("index") == "INDIA VIX":
                    v = item.get("last")
                    if v:
                        _update_vix_history(float(v), datetime.now().strftime("%H:%M"))
                    break
        except Exception:
            # Re-prime on error
            try:
                _sess.get("https://www.nseindia.com/", timeout=5)
            except Exception:
                pass
        time.sleep(120)  # every 2 minutes

def read_oi_snapshot() -> dict:
    """Read oi_snapshot.json written by calculate_oi_pcr.py every 60s."""
    path = os.path.join(BASE, "oi_snapshot.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        age = time.time() - data.get("timestamp", 0)
        data["_age_sec"] = round(age)
        data["_stale"]   = age > 300   # stale if > 5 min
        data["_ts_disp"] = data.get("time", "")
        _update_oi_history(data)
        return data
    except Exception:
        return {}

# ─────────────────────────────────────────────────────────────
#  MARKET INDICES  — Groww API, 1s refresh background thread
# ─────────────────────────────────────────────────────────────
# Instruments (from instruments.csv):
#   NIFTY   → NSE CASH   exchange_sym=NSE_NIFTY
#   BNIFTY  → NSE CASH   exchange_sym=NSE_BANKNIFTY
#   SENSEX  → BSE CASH   exchange_sym=BSE_SENSEX
#   CRUDE   → MCX COMMOD exchange_sym=MCX_<nearest_contract>
_idx_state: dict  = {}          # {nifty, banknifty, sensex}
_idx_lock         = threading.Lock()
_idx_prev: dict   = {}          # {sym: prev_close} — fetched once via Quote API
_idx_ohlc: dict   = {}          # {nifty: {open,high,low,close}} — refreshed every 60s from Quote API

def _idx_entry(ltp: float, prev: float) -> dict:
    chg = round(ltp - prev, 2) if prev else 0.0
    pct = round(chg / prev * 100, 2) if prev else 0.0
    return {"last": round(ltp, 2), "chg": chg, "pct": pct}

def _fetch_idx_quote():
    """Fetch prev_close and OHLC for each index via Groww Quote API. Called at startup and every 60s."""
    global _idx_prev, _idx_ohlc
    items = [
        ("NIFTY",     "NSE", "CASH", "nifty"),
        ("BANKNIFTY", "NSE", "CASH", "banknifty"),
        ("SENSEX",    "BSE", "CASH", "sensex"),
    ]
    for sym, exch, seg, label in items:
        try:
            pl = _groww_get("/v1/live-data/quote",
                            {"exchange": exch, "segment": seg, "trading_symbol": sym})
            if pl:
                last_p = float(pl.get("last_price") or pl.get("ohlc", {}).get("close") or 0)
                day_c  = float(pl.get("day_change") or 0)
                prev   = round(last_p - day_c, 2)
                if prev > 0:
                    _idx_prev[sym] = prev
                ohlc = pl.get("ohlc") or {}
                high = float(ohlc.get("high") or pl.get("high") or pl.get("day_high") or 0)
                low  = float(ohlc.get("low")  or pl.get("low")  or pl.get("day_low")  or 0)
                open_= float(ohlc.get("open") or pl.get("open") or 0)
                if high > 0 and low > 0:
                    _idx_ohlc[label] = {"high": round(high,2), "low": round(low,2), "open": round(open_,2)}
                    print(f"[idx ohlc] {sym} H={high} L={low}")
        except Exception as e:
            print(f"[idx quote] {sym}: {e}")

def _fetch_idx_prev_close():
    """Backward-compat alias."""
    _fetch_idx_quote()

def _idx_refresh_loop():
    """Background thread: fetch index LTPs every 3s using Groww LTP API.
    Also refreshes Quote (OHLC / day range) every 60s — no bots needed for day high/low."""
    global _idx_state
    # Fetch quote (prev_close + OHLC) once before starting loop
    threading.Thread(target=_fetch_idx_quote, daemon=True).start()
    time.sleep(2)   # let quote fetch complete
    _last_ohlc_refresh = 0.0
    while True:
        try:
            now = time.time()
            result = {}
            # ── CASH indices: NIFTY + BANKNIFTY + SENSEX in one call ──
            cash_pl = _groww_get("/v1/live-data/ltp",
                                 {"segment": "CASH",
                                  "exchange_symbols": ["NSE_NIFTY","NSE_BANKNIFTY","BSE_SENSEX"]})
            if cash_pl:
                for sym_key, label in [("NSE_NIFTY","nifty"),("NSE_BANKNIFTY","banknifty"),("BSE_SENSEX","sensex")]:
                    ltp = float(cash_pl.get(sym_key) or 0)
                    if ltp > 0:
                        ts = sym_key.split("_",1)[1]
                        prev = _idx_prev.get(ts, 0)
                        result[label] = _idx_entry(ltp, prev)
            if result:
                with _idx_lock: _idx_state.update(result)
            # Refresh OHLC (day high/low) every 60s via Quote API
            if now - _last_ohlc_refresh >= 60:
                threading.Thread(target=_fetch_idx_quote, daemon=True).start()
                _last_ohlc_refresh = now
        except Exception as e:
            print(f"[idx loop] {e}")
        time.sleep(3)   # 1 LTP call/3s = 20/min

def read_market_indices() -> dict:
    with _idx_lock:
        state = dict(_idx_state)
    state["_ohlc"] = dict(_idx_ohlc)   # include day range data in every snapshot
    return state

# ─────────────────────────────────────────────────────────────
#  PERSONAL TRADING AI — PnL + Market Intelligence
# ─────────────────────────────────────────────────────────────
_ptai_ok   = False
_ptai_mod  = None
_ptai_hist: dict  = {}   # loaded once: daily_pnl, daily_trades, expiry_days, intraday, stats
_ptai_mktdb       = None  # pandas DataFrame (yfinance cache)

try:
    import importlib.util as _ptai_iu
    _ptai_spec = _ptai_iu.spec_from_file_location(
        "PERSONAL_TRADING_AI", os.path.join(BASE, "PERSONAL_TRADING_AI.py"))
    _ptai_mod = _ptai_iu.module_from_spec(_ptai_spec)
    _ptai_spec.loader.exec_module(_ptai_mod)
    _ptai_ok = True
except Exception:
    pass

def _ptai_load_history():
    global _ptai_hist, _ptai_mktdb
    if not _ptai_ok or _ptai_hist: return
    try:
        dpnl, dtrades, edays = _ptai_mod.parse_excel_history()
        intra = _ptai_mod.parse_lakshmi_intraday()
        stats = _ptai_mod.overall_stats(dpnl)
        _ptai_hist = {"daily_pnl": dpnl, "daily_trades": dtrades,
                      "expiry_days": edays, "intraday": intra, "stats": stats}
    except Exception as e:
        print(f"[ptai hist] {e}")
    try:
        _ptai_mktdb = _ptai_mod.build_market_db()
    except Exception as e:
        print(f"[ptai mktdb] {e}")

# Groww API — today's PnL from /v1/positions/user (time-cached, 30s)
_today_pnl_cache: dict  = {}
_today_pnl_cache_ts: float = 0.0
_PNL_CACHE_SECS = 30

def _groww_get(path: str, params: dict = None) -> dict:
    token = _get_ltp_token()
    if not token: return {}
    try:
        r = _ltp_session.get(f"https://api.groww.in{path}",
                             headers={"Accept": "application/json",
                                      "Authorization": f"Bearer {token}",
                                      "X-API-VERSION": "1.0"},
                             params=params, timeout=8)
        if r.status_code == 401:
            global _ltp_token_ts; _ltp_token_ts = 0.0
        if r.status_code == 200:
            return r.json().get("payload", {})
    except Exception:
        pass
    return {}

def read_today_pnl() -> dict:
    global _today_pnl_cache, _today_pnl_cache_ts
    _empty = {"ts": "", "total_pnl": 0, "unrealised": 0, "total_with_open": 0,
              "trades": [], "count": 0, "wins": 0, "losses": 0, "open": 0, "error": ""}
    now = time.time()
    if _today_pnl_cache and (now - _today_pnl_cache_ts) < _PNL_CACHE_SECS:
        return _today_pnl_cache
    if not _get_ltp_token():
        return {**_empty, "error": "no_token"}
    try:
        payload   = _groww_get("/v1/positions/user", {"segment": "FNO"})
        positions = payload.get("positions", [])
        trades    = []
        total_r   = 0.0
        open_syms = {}  # exchange_sym → trade index for LTP fetch

        for pos in positions:
            sym      = str(pos.get("trading_symbol", ""))
            exch     = str(pos.get("exchange", "NSE"))
            r        = float(pos.get("realised_pnl", 0) or 0)
            qty      = int(pos.get("quantity", 0) or 0)
            buy_qty  = int(pos.get("credit_quantity", 0) or 0)
            sell_qty = int(pos.get("debit_quantity",  0) or 0)
            avg      = float(pos.get("net_price", 0) or 0)
            is_open  = (qty != 0)
            total_r += r
            if r != 0 or is_open:
                idx = len(trades)
                trades.append({"sym": sym, "exchange": exch, "realised": round(r, 2),
                               "net_qty": qty, "buy_qty": buy_qty, "sell_qty": sell_qty,
                               "is_open": is_open, "avg_price": round(avg, 2),
                               "ltp": 0.0, "unrealised": 0.0})
                if is_open and sym:
                    open_syms[f"{exch}_{sym}"] = idx

        # Fetch LTP for open positions → compute unrealised P&L
        total_u = 0.0
        if open_syms:
            syms_list = list(open_syms.keys())
            ltp_payload = _groww_get("/v1/live-data/ltp",
                                     {"segment": "FNO",
                                      "exchange_symbols": syms_list})
            for esym, idx in open_syms.items():
                ltp = float(ltp_payload.get(esym, 0) or 0)
                if ltp and trades[idx]["avg_price"]:
                    u = round((ltp - trades[idx]["avg_price"]) * trades[idx]["net_qty"], 2)
                    trades[idx]["ltp"]        = ltp
                    trades[idx]["unrealised"] = u
                    total_u += u

        result = {
            "ts":              datetime.now().isoformat(timespec="seconds"),
            "total_pnl":       round(total_r, 2),
            "unrealised":      round(total_u, 2),
            "total_with_open": round(total_r + total_u, 2),
            "trades":          trades,
            "count":           sum(1 for t in trades if t["realised"] != 0),
            "wins":            sum(1 for t in trades if t["realised"] > 0),
            "losses":          sum(1 for t in trades if t["realised"] < 0),
            "open":            sum(1 for t in trades if t["is_open"]),
            "error":           "",
        }
        _today_pnl_cache = result; _today_pnl_cache_ts = now
        return result
    except Exception as e:
        return {**_empty, "error": str(e)[:80]}

# ── Performance / Proof-of-Concept cache ─────────────────────────────────────
_perf_cache: dict = {}
_perf_cache_ts: float = 0.0
_PERF_CACHE_SECS = 30.0

def _parse_perf_data() -> dict:
    """Parse fib NEAR events and chart_level CE/PE signals, compute outcomes."""
    global _perf_cache, _perf_cache_ts
    now = time.time()
    if _perf_cache and (now - _perf_cache_ts) < _PERF_CACHE_SECS:
        return _perf_cache

    import glob as _glob2, json as _j, re as _re
    from datetime import timedelta as _td

    BASE    = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(BASE, "logs")

    # ── 1. S/R Level Events from fib analyzer logs ───────────────────────────
    sr_events = []
    fib_files = sorted(_glob2.glob(os.path.join(log_dir, "fibo_analyzer", "*.log")))[-2:]
    for fp in fib_files:
        try:
            with open(fp) as f:
                content = f.read()
            cycles = _re.split(r'🔄 Analysis cycle', content)
            parsed = []
            for cyc in cycles:
                sm = _re.search(r'Spot\s+([\d,]+)', cyc)
                if not sm: continue
                spot = float(sm.group(1).replace(',', ''))
                tm = _re.search(r'(\d{2}:\d{2}:\d{2})', cyc)
                ts = tm.group(1) if tm else ""
                near_matches = _re.findall(
                    r'^\s+([\d.]+)\s+([\w.%]+)\s+[+-][\d.]+ pts.*?◄ NEAR',
                    cyc, _re.MULTILINE
                )
                parsed.append((ts, spot, near_matches))
            for i, (ts, spot, near_levels) in enumerate(parsed):
                next_spot = parsed[i+1][1] if i+1 < len(parsed) else None
                for level_s, label in near_levels:
                    level = float(level_s)
                    is_res = level > spot
                    move = round(next_spot - spot, 1) if next_spot is not None else 0.0
                    if next_spot is not None:
                        if is_res and move < -8:    result = "RESPECTED"
                        elif not is_res and move > 8: result = "RESPECTED"
                        elif abs(move) > 5:          result = "BROKE"
                        else:                        result = "WATCHING"
                    else:
                        result = "WATCHING"
                    sr_events.append({
                        "ts":        ts,
                        "level":     level,
                        "label":     label,
                        "type":      "RESIST" if is_res else "SUPPORT",
                        "spot_near": spot,
                        "prox":      round(abs(level - spot), 1),
                        "move":      move,
                        "result":    result,
                    })
        except Exception:
            pass

    # ── 2. Option Signal Events from chart_level signals JSONL ───────────────
    signal_events = []
    cfiles = sorted(_glob2.glob(os.path.join(log_dir, "chart_level", "signals_*.jsonl")))[-3:]
    for fp in cfiles:
        try:
            sigs = []
            with open(fp) as f:
                for line in f:
                    try:
                        s = _j.loads(line.strip())
                        if s.get("direction") in ("CE", "PE"):
                            sigs.append(s)
                    except Exception:
                        pass
            for i, sig in enumerate(sigs):
                spot   = float(sig.get("spot") or 0)
                dir_   = sig["direction"]
                t_pts  = float(sig.get("target_pts") or 0)
                sl_pts = float(sig.get("sl_pts") or 0)
                if not spot: continue
                outcome = "PENDING"
                max_fav = 0.0
                for j in range(i+1, min(i+25, len(sigs))):
                    fut = float(sigs[j].get("spot") or 0)
                    if not fut: continue
                    fav = (fut - spot) if dir_ == "CE" else (spot - fut)
                    max_fav = max(max_fav, fav)
                    if t_pts  and fav >= t_pts:   outcome = "WIN";  break
                    if sl_pts and fav <= -sl_pts: outcome = "LOSS"; break
                ts_ = sig.get("ts") or ""
                signal_events.append({
                    "ts":      ts_[11:16] if len(ts_) > 11 else ts_,
                    "date":    ts_[:10],
                    "dir":     dir_,
                    "spot":    spot,
                    "reason":  (sig.get("reason") or "")[:45],
                    "t_pts":   t_pts,
                    "sl_pts":  sl_pts,
                    "max_fav": round(max_fav, 1),
                    "outcome": outcome,
                })
        except Exception:
            pass

    result = {
        "ts":            datetime.now().strftime("%H:%M:%S"),
        "sr_events":     list(reversed(sr_events[-50:])),
        "signal_events": list(reversed(signal_events[-40:])),
    }
    _perf_cache = result
    _perf_cache_ts = now
    return result


# ── Pivot Points cache ───────────────────────────────────────────────────────
_pivot_cache: dict = {}
_pivot_cache_ts: float = 0.0
_PIVOT_CACHE_SECS = 60.0

def _read_pivots(index: str = "NIFTY") -> dict:
    """Standard pivot points: parse chart_level log first, yfinance fallback."""
    global _pivot_cache, _pivot_cache_ts
    now = time.time()
    if _pivot_cache and (now - _pivot_cache_ts) < _PIVOT_CACHE_SECS:
        return _pivot_cache

    import glob as _gl3, re as _re3

    result: dict = {}

    # ── 1. Parse latest chart_level log for Pivot lines ───────────────────
    log_dir = os.path.join(BASE, "logs", "chart_level")
    for fp in reversed(sorted(_gl3.glob(os.path.join(log_dir, "Chart_Level_*.log")))):
        try:
            with open(fp) as f:
                content = f.read()
            pmap: dict = {}
            for m in _re3.finditer(
                r'([\d,]+\.?\d*)\s+[▲▼][+\-][\d.]+pts.*?Pivot\s+(PP|R[123]|S[123])',
                content
            ):
                pmap[m.group(2)] = round(float(m.group(1).replace(",", "")), 2)
            if len(pmap) >= 5:
                result = pmap
                result["_source"] = "chart-level log"
                break
        except Exception:
            pass

    # ── 2. yfinance fallback ──────────────────────────────────────────────
    if len(result) < 5:
        try:
            import yfinance as _yf
            ticker = "^NSEI" if "NIFTY" in index.upper() else "^BSESN"
            data = _yf.download(ticker, period="7d", interval="1d",
                                progress=False, auto_adjust=True)
            data = data.dropna()
            # Flatten MultiIndex columns (yfinance returns Price/Ticker MultiIndex)
            if hasattr(data.columns, "levels"):
                data.columns = data.columns.get_level_values(0)
            if len(data) >= 2:
                # Use the last complete trading day (skip today if partial)
                h = float(data["High"].iloc[-2])
                l = float(data["Low"].iloc[-2])
                c = float(data["Close"].iloc[-2])
                pp  = (h + l + c) / 3
                rng = h - l
                result = {
                    "PP": round(pp, 2),
                    "R1": round(2*pp - l, 2),
                    "R2": round(pp + rng, 2),
                    "R3": round(h + 2*(pp - l), 2),
                    "R4": round(pp + 3*rng, 2),
                    "S1": round(2*pp - h, 2),
                    "S2": round(pp - rng, 2),
                    "S3": round(l - 2*(h - pp), 2),
                    "S4": round(pp - 3*rng, 2),
                    "_prev_h": round(h, 2),
                    "_prev_l": round(l, 2),
                    "_prev_c": round(c, 2),
                    "_source": "yfinance",
                }
        except Exception as e:
            result["error"] = str(e)[:80]

    result["ts"] = datetime.now().strftime("%H:%M:%S")
    result["index"] = index
    _pivot_cache = result
    _pivot_cache_ts = now
    return result


_margin_cache: dict  = {}
_margin_cache_ts: float = 0.0
_MARGIN_CACHE_SECS = 60

def read_margin() -> dict:
    global _margin_cache, _margin_cache_ts
    now = time.time()
    if _margin_cache and (now - _margin_cache_ts) < _MARGIN_CACHE_SECS:
        return _margin_cache
    payload = _groww_get("/v1/margins/detail/user")
    if not payload:
        return _margin_cache or {}
    fno = payload.get("fno_margin_details", {})
    result = {
        "ts":               datetime.now().isoformat(timespec="seconds"),
        "clear_cash":       float(payload.get("clear_cash",        0) or 0),
        "margin_used":      float(payload.get("net_margin_used",   0) or 0),
        "brokerage":        float(payload.get("brokerage_and_charges", 0) or 0),
        "opt_buy_avail":    float(fno.get("option_buy_balance_available",  0) or 0),
        "opt_sell_avail":   float(fno.get("option_sell_balance_available", 0) or 0),
        "fno_margin_used":  float(fno.get("net_fno_margin_used",   0) or 0),
        "span_used":        float(fno.get("span_margin_used",      0) or 0),
        "exposure_used":    float(fno.get("exposure_margin_used",  0) or 0),
    }
    _margin_cache = result; _margin_cache_ts = now
    return result

_orders_cache: dict  = {}
_orders_cache_ts: float = 0.0
_ORDERS_CACHE_SECS = 30

def read_today_orders() -> dict:
    global _orders_cache, _orders_cache_ts
    now = time.time()
    if _orders_cache and (now - _orders_cache_ts) < _ORDERS_CACHE_SECS:
        return _orders_cache
    payload = _groww_get("/v1/order/list", {"segment": "FNO", "page_size": 50})
    if not payload:
        return _orders_cache or {"orders": [], "ts": ""}
    orders = []
    for o in payload.get("order_list", []):
        orders.append({
            "sym":       str(o.get("trading_symbol", "")),
            "status":    str(o.get("order_status",   "")),
            "type":      str(o.get("transaction_type", "")),
            "qty":       int(o.get("quantity", 0) or 0),
            "filled":    int(o.get("filled_quantity", 0) or 0),
            "avg_fill":  float(o.get("average_fill_price", 0) or 0),
            "price":     float(o.get("price", 0) or 0),
            "order_type":str(o.get("order_type", "")),
            "product":   str(o.get("product", "")),
            "created":   str(o.get("created_at", "")),
        })
    result = {"ts": datetime.now().isoformat(timespec="seconds"), "orders": orders}
    _orders_cache = result; _orders_cache_ts = now
    return result

# ─────────────────────────────────────────────────────────────
#  TRADE BOARD  —  BUY + trailing SL engine
# ─────────────────────────────────────────────────────────────
# ── ATR helpers (ported from PROD10FEB) ──────────────────────
def _ema(data: list, period: int):
    if len(data) < period: return None
    k = 2.0 / (period + 1)
    val = sum(data[:period]) / period
    for x in data[period:]:
        val = x * k + val * (1 - k)
    return val

def _atr(high: list, low: list, close: list, period: int = 14):
    if len(high) < period + 1: return 0.0
    import numpy as np
    h = np.array(high); l = np.array(low); c = np.array(close)
    tr1 = h - l
    tr2 = np.abs(h - np.roll(c, 1))
    tr3 = np.abs(l - np.roll(c, 1))
    tr  = np.amax((tr1, tr2, tr3), axis=0)[1:]  # drop first (roll artifact)
    return _ema(tr.tolist(), period) or 0.0

def fetch_atr(trading_symbol: str, exchange: str, period: int = 14) -> float:
    """
    Fetch 1-min candles and compute ATR.
    Tries progressively wider lookback windows so it works during/after market hours.
    Returns 0 on failure.
    """
    insts = _load_instruments_for_ltp()
    groww_sym = ""
    for i in insts:
        if i.get("trading_symbol","").upper() == trading_symbol.upper():
            groww_sym = i.get("groww_symbol",""); break
    if not groww_sym:
        print(f"[ATR] groww_symbol not found for {trading_symbol}"); return 0.0
    token = _get_ltp_token()
    if not token: return 0.0

    from datetime import timedelta
    # Try widening windows: 90min (live), 300min (post-market), 600min (far post-market)
    lookbacks = [90, 300, 600]
    for lb in lookbacks:
        now   = datetime.now()
        start = (now - timedelta(minutes=lb)).strftime("%Y-%m-%d %H:%M:%S")
        end   = now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            r = _ltp_session.get(
                "https://api.groww.in/v1/historical/candles",
                headers={"Accept":"application/json","Authorization":f"Bearer {token}",
                         "X-API-VERSION":"1.0"},
                params={"exchange":exchange,"segment":"FNO","groww_symbol":groww_sym,
                        "start_time":start,"end_time":end,"candle_interval":"1minute"},
                timeout=8)
            if r.status_code != 200:
                print(f"[ATR] HTTP {r.status_code} (lookback={lb}min)"); continue
            body    = r.json()
            candles = body.get("candles",[]) or body.get("payload",{}).get("candles",[])
            if not candles or len(candles) < period + 2:
                print(f"[ATR] only {len(candles) if candles else 0} candles (lookback={lb}min), trying wider")
                continue
            h  = [c[2] for c in candles]
            l  = [c[3] for c in candles]
            c_ = [c[4] for c in candles]
            val = round(_atr(h, l, c_, period), 2)
            print(f"[ATR] {trading_symbol} ATR={val:.2f} ({len(candles)} candles, {lb}min window)")
            return val
        except Exception as e:
            print(f"[ATR] error (lookback={lb}min): {e}"); continue
    print(f"[ATR] failed all lookbacks for {trading_symbol}")
    return 0.0
LOT_SIZES   = {"NIFTY":75,"BANKNIFTY":35,"SENSEX":20,"FINNIFTY":65,"MIDCPNIFTY":75,"BANKEX":15}
EXCH_MAP    = {"NIFTY":"NSE","BANKNIFTY":"NSE","FINNIFTY":"NSE","MIDCPNIFTY":"NSE",
               "SENSEX":"BSE","BANKEX":"BSE"}

_trade_lock   = threading.Lock()
_trail_ltp    = {"ltp": 0.0, "esym": ""}  # LTP cache (REST polling)
_trail_ltp_lock = threading.Lock()

_trade_state: dict = {
    "status":"IDLE","symbol":"","exchange":"NSE","order_id":"",
    "avg_price":0.0,"qty":0,"entry_ts":"","buy_exec_ms":0,
    "ltp":0.0,"highest":0.0,"hard_sl":0.0,"trail_exit":0.0,
    "trail_active":False,"unrealised":0.0,
    "exit_reason":"","exit_price":0.0,"exit_exec_ms":0,
    "total_ms":0,"pnl":0.0,"log":[],"paper":False,"error":"",
    "atr_val":0.0,"atr_based":False,
}
_trade_history: list = []   # [{entry_ts, exit_ts, symbol, direction, buy, sell, qty, pnl, paper}]

def _tlog(msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    entry = f"{ts}  {msg}"
    with _trade_lock:
        _trade_state["log"].insert(0, entry)
        if len(_trade_state["log"]) > 300:
            _trade_state["log"] = _trade_state["log"][:300]
    print(f"[TRADE] {entry}")

_COMPACT_MONTH = {'1':'Jan','2':'Feb','3':'Mar','4':'Apr','5':'May','6':'Jun',
                  '7':'Jul','8':'Aug','9':'Sep','O':'Oct','N':'Nov','D':'Dec'}

def _parse_fno_sym(sym: str):
    """Return (index, option_str, expiry_str) from a Groww FNO trading symbol.
    Handles two formats:
      Monthly : NIFTY26MAY23500CE  → (NIFTY, 23500CE, 29May2026)
      Weekly  : NIFTY2661623400CE  → (NIFTY, 23400CE, 16Jun2026)
    """
    import re as _r
    s = _r.sub(r'^(NSE_FO_|BSE_FO_|NFO_NSE_|BFO_BSE_)', '', sym.upper())
    IDX = r'(NIFTY|BANKNIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|BANKEX)'
    # Monthly: INDEX + YY + 3-letter-month + STRIKE + TYPE
    m = _r.match(IDX + r'(\d{2})([A-Z]{3})(\d{4,6})(CE|PE)$', s)
    if m:
        idx, yy, mon, strike, opt = m.groups()
        expiry = f"{mon.capitalize()}20{yy}"
        return idx, f"{strike}{opt}", expiry
    # Weekly: INDEX + YY + 1-char-month-code + DD + STRIKE + TYPE
    m = _r.match(IDX + r'(\d{2})([1-9OND])(\d{2})(\d{4,6})(CE|PE)$', s)
    if m:
        idx, yy, mc, dd, strike, opt = m.groups()
        mon_name = _COMPACT_MONTH.get(mc.upper(), mc)
        expiry = f"{dd}{mon_name}20{yy}"
        return idx, f"{strike}{opt}", expiry
    return "—", sym, "—"

def _write_trade_jsonl(symbol: str, buy_price: float, sell_price: float,
                        qty: int, pnl: float, paper: bool,
                        entry_ts: str, exit_ts: str) -> None:
    """Persist a completed PROD10 trade to the daily trade-history JSONL log."""
    try:
        os.makedirs(os.path.join(BASE, "logs", "trade_history"), exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        idx, opt, expiry = _parse_fno_sym(symbol)
        record = {
            "date":        date_str,
            "time_entry":  entry_ts,
            "time_exit":   exit_ts,
            "bot":         "PROD10",
            "mode":        "paper" if paper else "live",
            "index":       idx,
            "symbol":      symbol,
            "option":      opt,
            "expiry":      expiry,
            "buy_price":   round(float(buy_price), 2),
            "sell_price":  round(float(sell_price), 2),
            "qty":         int(qty),
            "lots":        1,
            "pnl":         round(float(pnl), 2),
            "exit_reason": "",
        }
        path = os.path.join(BASE, "logs", "trade_history", f"{date_str}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[TRADE] _write_trade_jsonl error: {e}")

def read_trade_history(date_from: str = "", date_to: str = "") -> list:
    """Read trade history for a date range from JSONL files + bot log [TRADE_RECORD] lines."""
    import re as _re2
    today = datetime.now().strftime("%Y-%m-%d")
    df = date_from or today
    dt = date_to   or today
    result = []
    seen = set()   # deduplicate by (bot, ts, symbol, pnl)

    def _add(rec):
        key = (rec.get("bot",""), rec.get("date",""), rec.get("time_exit",""),
               rec.get("symbol",""), rec.get("pnl",0))
        if key not in seen:
            seen.add(key)
            result.append(rec)

    # ── 1. New-format JSONL files written by this session ──────────────
    hist_dir = os.path.join(BASE, "logs", "trade_history")
    if os.path.isdir(hist_dir):
        for fname in sorted(os.listdir(hist_dir)):
            if not fname.endswith(".jsonl"):
                continue
            date_part = fname[:-6]
            if date_part < df or date_part > dt:
                continue
            try:
                with open(os.path.join(hist_dir, fname), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try: _add(json.loads(line))
                            except Exception: pass
            except Exception:
                pass

    # ── 2. [TRADE_RECORD] lines from groww_bot logs (PROD10) ───────────
    # ── 3. [TRADE_RECORD] lines from momentum_bot logs (Auto) ──────────
    _LOG_DIRS = [
        (os.path.join(BASE, "logs", "groww_bot"),    "Groww_Bot_",    "PROD10"),
        (os.path.join(BASE, "logs", "momentum_bot"), "Momentum_Bot_", "Auto"),
    ]
    for log_dir, prefix, bot_name in _LOG_DIRS:
        if not os.path.isdir(log_dir):
            continue
        for fname in sorted(os.listdir(log_dir)):
            if not fname.startswith(prefix) or not fname.endswith(".log"):
                continue
            # filename date: prefix + YYYY-MM-DD_HH-MM-SS.log
            date_part = fname[len(prefix):len(prefix)+10]
            if date_part < df or date_part > dt:
                continue
            try:
                with open(os.path.join(log_dir, fname), encoding="utf-8", errors="ignore") as f:
                    for raw in f:
                        if "[TRADE_RECORD]" not in raw:
                            continue
                        m = _re2.search(r'\[TRADE_RECORD\]\s*(\{.*\})', raw)
                        if not m:
                            continue
                        try:
                            rec = json.loads(m.group(1))
                        except Exception:
                            continue
                        ts_str   = rec.get("ts", "")       # "2026-06-12T11:38:55"
                        rec_date = ts_str[:10] if ts_str else date_part
                        rec_time = ts_str[11:19] if len(ts_str) >= 19 else ""
                        sym      = rec.get("symbol", "")
                        idx, opt, expiry = _parse_fno_sym(sym)
                        mode     = (rec.get("mode") or "live").lower()
                        _add({
                            "date":        rec_date,
                            "time_entry":  rec_time,
                            "time_exit":   rec_time,
                            "bot":         bot_name,
                            "mode":        mode,
                            "index":       idx,
                            "symbol":      sym,
                            "option":      opt,
                            "expiry":      expiry,
                            "buy_price":   round(float(rec.get("buy_px",  0)), 2),
                            "sell_price":  round(float(rec.get("sell_px", 0)), 2),
                            "qty":         int(rec.get("qty", 0)),
                            "lots":        1,
                            "pnl":         round(float(rec.get("pnl", 0)), 2),
                            "exit_reason": rec.get("exit_reason", ""),
                        })
            except Exception:
                pass

    result.sort(key=lambda r: (r.get("date",""), r.get("time_exit","")), reverse=True)
    return result

def _groww_post(path: str, body: dict) -> dict:
    token = _get_ltp_token()
    if not token:
        print(f"[TRADE] _groww_post {path}: no auth token")
        return {"_err": "no_token"}
    try:
        r = _ltp_session.post(
            f"https://api.groww.in{path}",
            headers={"Accept":"application/json","Authorization":f"Bearer {token}",
                     "X-API-VERSION":"1.0","Content-Type":"application/json"},
            json=body, timeout=10)
        if r.status_code == 401:
            global _ltp_token_ts; _ltp_token_ts = 0.0
            print(f"[TRADE] _groww_post {path}: 401 — token expired, will re-auth next call")
            return {"_err": "auth_401"}
        try:
            resp = r.json()
        except Exception:
            resp = {}
        if r.status_code not in (200, 400, 422):
            print(f"[TRADE] _groww_post {path}: HTTP {r.status_code} — {r.text[:200]}")
            return {"_err": f"http_{r.status_code}", "_body": r.text[:200]}
        print(f"[TRADE] _groww_post {path}: HTTP {r.status_code} → {json.dumps(resp)[:300]}")
        return resp
    except Exception as e:
        print(f"[TRADE] _groww_post {path}: exception {e}")
        return {"_err": str(e)}

def _wait_fill(order_id: str, max_sec: float = 8.0):
    """Poll until order fills. Returns (avg_price, status, remark)."""
    fill_p, fill_st = None, None
    deadline = time.time() + max_sec
    while time.time() < deadline:
        time.sleep(0.05)
        p  = _groww_get(f"/v1/order/status/{order_id}", {"segment":"FNO"})
        st = p.get("order_status","")
        if st in ("COMPLETE","EXECUTED","DELIVERY_AWAITED"):
            fill_p, fill_st = p, st; break
        if st in ("REJECTED","FAILED","CANCELLED"):
            return 0.0, st, p.get("remark","")
    if not fill_st:
        return 0.0, "TIMEOUT", ""

    # ── Get exact executed price via trade_list (up to 3 retries) ────────────
    def _vwap(tl):
        tv = sum(float(t["price"])*int(t["quantity"]) for t in tl)
        tq = sum(int(t["quantity"]) for t in tl)
        return round(tv/tq, 2) if tq else 0.0

    for attempt in range(3):
        if attempt > 0: time.sleep(0.5)
        tp = _groww_get(f"/v1/order/trades/{order_id}", {"segment":"FNO"})
        tl = tp.get("trade_list",[])
        if tl:
            avg = _vwap(tl)
            if avg > 0: return avg, fill_st, ""

    # Final fallback: re-fetch order status for latest average_price
    p2  = _groww_get(f"/v1/order/status/{order_id}", {"segment":"FNO"})
    avg = float(p2.get("average_price") or p2.get("avg_price") or
               (fill_p.get("average_price") if fill_p else 0) or
               (fill_p.get("avg_price") if fill_p else 0) or 0)
    return avg, fill_st, ""

def _do_sell(sym,exch,qty,avg_price,paper,reason,t0_epoch):
    global _trade_state
    with _trade_lock:
        if _trade_state["status"] not in ("ACTIVE",): return
        _trade_state["status"] = "EXITING"
        _trade_state["exit_reason"] = reason
    _tlog(f"EXIT → {reason}")
    t_exit = time.time()

    if paper:
        with _trade_lock: ltp = _trade_state.get("ltp") or avg_price
        time.sleep(0.05)
        exit_price = round(ltp,2); exec_ms = 50
    else:
        ref  = "DS" + datetime.now().strftime("%H%M%S%f")[:-3]
        resp = _groww_post("/v1/order/create",{
            "trading_symbol":sym,"quantity":qty,"validity":"DAY",
            "exchange":exch,"segment":"FNO","product":"MIS",
            "order_type":"MARKET","transaction_type":"SELL",
            "order_reference_id":ref})
        sid = resp.get("payload",{}).get("groww_order_id","")
        st  = resp.get("payload",{}).get("order_status","")
        err_key = resp.get("_err","") or (resp.get("status")=="FAILURE" and (resp.get("message","") or str(resp)))
        if err_key or not sid or st in ("REJECTED","FAILED","CANCELLED"):
            remark = resp.get("payload",{}).get("remark","") or str(err_key)
            with _trade_lock:
                _trade_state["status"]="ACTIVE"
                _trade_state["error"]=f"SELL {st}: {remark}"
            _tlog(f"SELL FAILED {st}: {remark}"); return
        _tlog(f"SELL order placed | ID {sid}")
        exit_price,st,_ = _wait_fill(sid, max_sec=5.0)   # market sell fills in <1s
        exec_ms = int((time.time()-t_exit)*1000)
        if not exit_price:
            with _trade_lock: exit_price = _trade_state.get("ltp") or avg_price

    pnl      = round((exit_price-avg_price)*qty,2)
    total    = int((time.time()-t0_epoch)*1000)
    exit_ts  = datetime.now().strftime("%H:%M:%S")
    with _trade_lock:
        _trade_state.update({"status":"DONE","exit_price":exit_price,
            "exit_exec_ms":int((time.time()-t_exit)*1000),
            "total_ms":total,"pnl":pnl})
        entry_ts = _trade_state.get("entry_ts","")
        sym_h    = _trade_state.get("symbol","")
        qty_h    = _trade_state.get("qty",0)
        paper_h  = _trade_state.get("paper",False)
    direction = "CE" if sym_h.endswith("CE") else "PE" if sym_h.endswith("PE") else "—"
    entry_ts_short = entry_ts[11:19] if len(entry_ts) >= 19 else entry_ts
    _trade_history.append({
        "entry_ts": entry_ts_short,
        "exit_ts":  exit_ts,
        "symbol":   sym_h,
        "direction":direction,
        "buy":      avg_price,
        "sell":     exit_price,
        "qty":      qty_h,
        "pnl":      pnl,
        "paper":    paper_h,
    })
    _write_trade_jsonl(sym_h, avg_price, exit_price, qty_h, pnl, paper_h,
                       entry_ts_short, exit_ts)
    sign = "+" if pnl>=0 else ""
    _tlog(f"DONE | Sell ₹{exit_price} | P&L {sign}₹{pnl:,.2f} | "
          f"Exit exec {exec_ms}ms | Total {total//1000}s {total%1000}ms")

def _start_trail_ltp_feed(esym: str):
    """Background REST polling thread for LTP during active trade."""
    def _poll():
        while True:
            with _trade_lock:
                if _trade_state["status"] not in ("ACTIVE","BUYING","EXITING"):
                    break
            p   = _groww_get("/v1/live-data/ltp",{"segment":"FNO","exchange_symbols":[esym]})
            ltp = float(p.get(esym,0) or 0)
            if ltp > 0:
                with _trail_ltp_lock:
                    _trail_ltp["ltp"]  = ltp
                    _trail_ltp["esym"] = esym
            # no sleep — HTTP round-trip (~250ms) is the natural throttle
    threading.Thread(target=_poll, daemon=True).start()

def _trail_loop(sym,exch,qty,avg_price,hard_sl,trail_start,trail_step,max_sec,paper,t0):
    global _trade_state
    highest = avg_price; last_trail = None
    esym = f"{exch}_{sym}"
    _tlog(f"Trail started | entry ₹{avg_price} | SL ₹{hard_sl:.2f} | trail after +{trail_start}pts")

    # Reset cache and start background LTP feed for this trade
    with _trail_ltp_lock:
        _trail_ltp["ltp"] = 0.0; _trail_ltp["esym"] = esym
    _start_trail_ltp_feed(esym)

    last_heartbeat = time.time()

    while True:
        with _trade_lock:
            if _trade_state["status"] != "ACTIVE": break

        # Read LTP from cache — no HTTP call, no blocking
        with _trail_ltp_lock:
            ltp = _trail_ltp["ltp"] if _trail_ltp["esym"] == esym else 0.0

        # Heartbeat every 30s so log shows trail is alive
        now = time.time()
        if now - last_heartbeat >= 30:
            elapsed = int(now - t0)
            unr = round((ltp - avg_price) * qty, 2) if ltp > 0 else 0.0
            sign = "+" if unr >= 0 else ""
            _tlog(f"💓 Trail alive {elapsed}s | LTP ₹{ltp:.2f} | High ₹{highest:.2f} | Unr {sign}₹{unr:,.2f}")
            last_heartbeat = now

        if ltp > 0:
            if ltp > highest: highest=ltp; _tlog(f"New high ₹{highest:.2f}")
            with _trade_lock:
                _trade_state.update({"ltp":ltp,"highest":highest,
                    "unrealised":round((ltp-avg_price)*qty,2)})
        if time.time()-t0 >= max_sec:
            _do_sell(sym,exch,qty,avg_price,paper,"MAX TIME REACHED",t0); return
        if ltp>0 and ltp<=hard_sl:
            _do_sell(sym,exch,qty,avg_price,paper,f"HARD SL @ ₹{ltp:.2f}",t0); return
        if highest >= avg_price+trail_start:
            te = round(round((highest-trail_step)/0.05)*0.05,2)
            with _trade_lock:
                _trade_state["trail_active"]=True; _trade_state["trail_exit"]=te
            if te != last_trail:
                _tlog(f"Trail | LTP ₹{ltp:.2f} | High ₹{highest:.2f} | Exit ₹{te:.2f}")
                last_trail = te
            if ltp>0 and ltp<=te:
                _do_sell(sym,exch,qty,avg_price,paper,f"TRAIL HIT @ ₹{ltp:.2f}",t0); return
        time.sleep(0.015)   # 65Hz check — reads cache, no API cost

def _buy_and_trail(sym,exch,qty,paper,hard_sl_pts,trail_start,trail_step,max_sec,
                   atr_based=False,atr_multiplier=1.0):
    global _trade_state
    t0   = time.time()
    esym = f"{exch}_{sym}"
    if paper:
        p = _groww_get("/v1/live-data/ltp",{"segment":"FNO","exchange_symbols":[esym]})
        avg = float(p.get(esym,0) or 50.0)
        time.sleep(0.05); bms=50
        _tlog(f"[PAPER] BUY @ ₹{avg:.2f}")
    else:
        # Market order — order_reference_id is required by Groww despite docs saying optional
        ref  = "DB" + datetime.now().strftime("%H%M%S%f")[:-3]  # e.g. DB191442860 (11 chars)
        resp = _groww_post("/v1/order/create",{
            "trading_symbol":sym,"quantity":qty,"validity":"DAY",
            "exchange":exch,"segment":"FNO","product":"MIS",
            "order_type":"MARKET","transaction_type":"BUY",
            "order_reference_id":ref})
        # Handle API-level errors (_err key set by _groww_post on HTTP failure)
        if resp.get("_err"):
            err = resp["_err"]
            body = resp.get("_body","")
            with _trade_lock:
                _trade_state["status"]="IDLE"; _trade_state["error"]=f"API error: {err} {body}"
            _tlog(f"BUY FAILED — API error: {err} {body}"); return
        # Handle Groww error response (status=FAILURE at top level)
        if resp.get("status") == "FAILURE":
            msg = resp.get("message","") or resp.get("error","") or str(resp)
            with _trade_lock:
                _trade_state["status"]="IDLE"; _trade_state["error"]=f"Groww: {msg}"
            _tlog(f"BUY FAILED — Groww error: {msg}"); return
        oid = resp.get("payload",{}).get("groww_order_id","")
        st  = resp.get("payload",{}).get("order_status","")
        rem = resp.get("payload",{}).get("remark","")
        if not oid or st in ("REJECTED","FAILED","CANCELLED"):
            with _trade_lock:
                _trade_state["status"]="IDLE"; _trade_state["error"]=f"BUY {st or 'no_order_id'}: {rem}"
            _tlog(f"BUY FAILED — status={st!r} rem={rem!r} oid={oid!r}"); return
        with _trade_lock: _trade_state["order_id"]=oid
        _tlog(f"BUY placed | ID {oid} | {st}")
        avg,st,rem = _wait_fill(oid)
        bms = int((time.time()-t0)*1000)
        if st in ("REJECTED","FAILED","CANCELLED"):
            with _trade_lock:
                _trade_state["status"]="IDLE"; _trade_state["error"]=f"BUY {st}: {rem}"
            _tlog(f"BUY {st}: {rem}"); return
        if not avg:
            p=_groww_get("/v1/live-data/ltp",{"segment":"FNO","exchange_symbols":[esym]})
            avg=float(p.get(esym,0) or 0)
        _tlog(f"BUY EXECUTED @ ₹{avg:.2f} | exec {bms}ms | {st}")

    # ── ATR: background fetch with 2s timeout (PROD10 pattern) ──────────────────
    # Trail starts IMMEDIATELY with fixed fallback SL — no unprotected gap.
    atr_val  = 0.0
    hard_sl  = round(round((avg - hard_sl_pts) / 0.05) * 0.05, 2)   # fallback always set first
    if atr_based:
        with _trade_lock: _trade_state["atr_based"] = True
        _tlog(f"ATR fetching in background… trail active NOW with fallback SL ₹{hard_sl:.2f}")
        _atr_box = [0.0]
        def _fetch_atr_bg(): _atr_box[0] = fetch_atr(sym, exch)
        _atr_thread = threading.Thread(target=_fetch_atr_bg, daemon=True)
        _atr_thread.start()
        _atr_thread.join(timeout=2.0)   # wait up to 2s (usually done in <1s during market hours)
        atr_val = _atr_box[0]
        if atr_val > 0:
            hard_sl    = round(round((avg - 1.5 * atr_val) / 0.05) * 0.05, 2)
            trail_step = round(atr_val * atr_multiplier, 2)
            with _trade_lock: _trade_state["atr_val"] = atr_val
            _tlog(f"✅ ATR={atr_val:.2f} → Hard SL ₹{hard_sl:.2f} (1.5×ATR) | Trail step ₹{trail_step:.2f} ({atr_multiplier}×ATR)")
        else:
            _tlog(f"⚠️ ATR not ready in 2s → fallback Hard SL ₹{hard_sl:.2f} ({hard_sl_pts}pts fixed)")
    else:
        with _trade_lock: _trade_state["atr_based"] = False

    with _trade_lock:
        _trade_state.update({"status":"ACTIVE","avg_price":avg,"highest":avg,
            "ltp":avg,"hard_sl":hard_sl,"trail_exit":0.0,"trail_active":False,
            "buy_exec_ms":bms,"unrealised":0.0,"entry_ts":datetime.now().isoformat(timespec="seconds")})
    _trail_loop(sym,exch,qty,avg,hard_sl,trail_start,trail_step,max_sec,paper,t0)

def trade_start(sym,exch,qty,paper,hard_sl_pts,trail_start,trail_step,max_sec,
                atr_based=False,atr_multiplier=1.0):
    global _trade_state
    with _trade_lock:
        if _trade_state["status"] not in ("IDLE","DONE"):
            return {"ok":False,"error":"Trade already active"}
        _trade_state.update({"status":"BUYING","symbol":sym,"exchange":exch,
            "order_id":"","avg_price":0.0,"qty":qty,"entry_ts":"","buy_exec_ms":0,
            "ltp":0.0,"highest":0.0,"hard_sl":0.0,"trail_exit":0.0,
            "trail_active":False,"unrealised":0.0,"exit_reason":"","exit_price":0.0,
            "exit_exec_ms":0,"total_ms":0,"pnl":0.0,"paper":paper,"error":"",
            "atr_val":0.0,"atr_based":atr_based})
    atr_tag = f" | ATR-based SL (×{atr_multiplier})" if atr_based else ""
    _tlog(f"{'[PAPER] ' if paper else ''}Starting {sym} qty={qty}{atr_tag}")
    threading.Thread(target=_buy_and_trail,
        args=(sym,exch,qty,paper,hard_sl_pts,trail_start,trail_step,max_sec),
        kwargs={"atr_based":atr_based,"atr_multiplier":atr_multiplier},
        daemon=True).start()
    return {"ok":True}

def trade_force_exit():
    with _trade_lock:
        if _trade_state["status"]!="ACTIVE":
            return {"ok":False,"error":"No active trade"}
        sym=_trade_state["symbol"]; exch=_trade_state["exchange"]
        qty=_trade_state["qty"]; avg=_trade_state["avg_price"]
        paper=_trade_state["paper"]
    try: t0=datetime.fromisoformat(_trade_state["entry_ts"]).timestamp()
    except Exception: t0=time.time()-60
    threading.Thread(target=_do_sell,
        args=(sym,exch,qty,avg,paper,"MANUAL EXIT",t0),daemon=True).start()
    return {"ok":True}

def _lot_size_from_csv(index: str, expiry: str) -> int:
    """Read the actual lot size for this index+expiry from instruments.csv."""
    insts = _load_instruments_for_ltp()
    for i in insts:
        if (i.get("underlying_symbol","").upper() == index.upper()
                and i.get("expiry_date","").strip() == expiry):
            try: return int(float(i.get("lot_size",0)))
            except Exception: pass
    # Fallback to known defaults (update if Groww changes lot sizes)
    return {"NIFTY":75,"BANKNIFTY":35,"SENSEX":20,"FINNIFTY":65,"MIDCPNIFTY":75}.get(index.upper(),75)

_chain_cache: dict = {}   # {(index, expiry): (ts, result)}
_CHAIN_CACHE_TTL  = 5    # seconds — 5s for Quick Trade Mode live premium refresh

def fetch_option_chain(index:str, expiry:str) -> dict:
    key = (index.upper(), expiry)
    now = time.time()
    cached_ts, cached_result = _chain_cache.get(key, (0, None))
    if cached_result is not None and (now - cached_ts) < _CHAIN_CACHE_TTL:
        return cached_result
    exch     = EXCH_MAP.get(index.upper(),"NSE")
    lot_size = _lot_size_from_csv(index, expiry)
    pl   = _groww_get(f"/v1/option-chain/exchange/{exch}/underlying/{index.upper()}",
                      {"expiry_date":expiry})
    if not pl: return {"strikes":[],"spot":0,"lot_size":lot_size,"error":"fetch failed"}
    spot = float(pl.get("underlying_ltp",0) or 0)
    raw  = pl.get("strikes",{})
    strikes = []
    for sp,data in sorted(raw.items(),key=lambda x:float(x[0])):
        ce=data.get("CE",{}); pe=data.get("PE",{})
        cg=ce.get("greeks") or {};  pg=pe.get("greeks") or {}
        ce_ltp  = round(float(ce.get("ltp",0) or 0),2)
        pe_ltp  = round(float(pe.get("ltp",0) or 0),2)
        ce_prev = round(float(ce.get("close") or ce.get("prev_close") or ce.get("previous_close") or 0),2)
        pe_prev = round(float(pe.get("close") or pe.get("prev_close") or pe.get("previous_close") or 0),2)
        strikes.append({
            "strike":   float(sp),
            "ce_sym":   ce.get("trading_symbol",""),
            "pe_sym":   pe.get("trading_symbol",""),
            "ce_ltp":   ce_ltp,
            "pe_ltp":   pe_ltp,
            "ce_prev":  ce_prev,
            "pe_prev":  pe_prev,
            "ce_oi":    int(ce.get("open_interest",0) or 0),
            "pe_oi":    int(pe.get("open_interest",0) or 0),
            "ce_vol":   int(ce.get("volume",0) or 0),
            "pe_vol":   int(pe.get("volume",0) or 0),
            "ce_iv":    round(float(cg.get("iv",0) or 0),1),
            "pe_iv":    round(float(pg.get("iv",0) or 0),1),
        })
    result = {"strikes":strikes,"spot":spot,"lot_size":lot_size,"error":""}
    _chain_cache[key] = (time.time(), result)
    return result

def fetch_expiries(index:str) -> list:
    insts = _load_instruments_for_ltp()
    now   = datetime.now()
    # After 15:30, today's expiry is closed — exclude it
    if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
        from datetime import timedelta
        min_date = (now.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        min_date = now.strftime("%Y-%m-%d")
    return sorted({i["expiry_date"].strip() for i in insts
                   if i.get("underlying_symbol","").upper()==index.upper()
                   and i.get("expiry_date","").strip() >= min_date})[:12]

PTAI_ANALYSIS_REFRESH = 300   # 5 min
PTAI_AI_REFRESH       = 1800  # 30 min

_ptai_analysis_lock = threading.Lock()
_ptai_analysis: dict = {}
_ptai_ai_lock  = threading.Lock()
_ptai_ai: dict = {"text": "", "ts": "", "status": "init"}

def _serialize_ptai(live, mkt_score, mkt_bkdwn, sim_report, behav,
                    perm_score, verdict, perm_bkdwn, stats) -> dict:
    sim = {k: v for k, v in sim_report.items() if k not in ("best_day","worst_day","top5")}
    for key, fld in (("best_day","best"), ("worst_day","worst")):
        row = sim_report.get(key)
        if row is not None:
            try: sim[f"{fld}_pnl"] = float(row["pnl"]); sim[f"{fld}_date"] = str(row["date"])
            except Exception: pass
    sim["top5"] = []
    for rec in sim_report.get("top5", []):
        try:
            sim["top5"].append({"date": str(rec["date"]), "vix": float(rec["vix"]),
                                 "gap": float(rec["gap_pct"]), "dow": str(rec["dow"]),
                                 "pnl": float(rec["pnl"]), "sim": float(rec["sim_score"])})
        except Exception: pass
    bkd = {}
    for k, v in mkt_bkdwn.items():
        try: bkd[k] = {"pts": int(v[0]), "max": int(v[1]), "val": str(v[2]),
                        "meaning": str(v[3]) if len(v) > 3 else ""}
        except Exception: pass
    st = {}
    if stats:
        try:
            st = {"total_days": int(stats["total_days"]), "win_days": int(stats["win_days"]),
                  "loss_days": int(stats["loss_days"]), "win_rate": float(stats["win_rate"]),
                  "total_pnl": float(stats["total_pnl"]), "avg_win": float(stats["avg_win"]),
                  "avg_loss": float(stats["avg_loss"])}
            for k2, fld2 in (("best_day","best"), ("worst_day","worst")):
                row2 = stats.get(k2)
                if row2:
                    try: st[f"{fld2}_date"] = str(row2[0]); st[f"{fld2}_pnl"] = float(row2[1])
                    except Exception: pass
            st["yearly"] = {str(k): float(v) for k, v in stats.get("yearly", {}).items()}
        except Exception: pass
    bh = {"risks":    [{"type": r["type"], "detail": r["detail"], "weight": r["weight"]}
                        for r in behav.get("risks", [])],
           "insights":  [str(i) for i in behav.get("insights", [])],
           "risk_score": int(behav.get("risk_score", 0)),
           "recent_wr":  float(behav.get("recent_wr", 0)),
           "recent_avg": float(behav.get("recent_avg", 0))}
    lv = {}
    for k, v in live.items():
        try: lv[k] = float(v) if isinstance(v, (int, float)) else v
        except Exception: pass
    return {"ts": datetime.now().isoformat(timespec="seconds"),
            "live": lv, "mkt_score": int(mkt_score), "mkt_bkdwn": bkd,
            "sim": sim, "behav": bh, "perm_score": int(perm_score),
            "verdict": str(verdict),
            "perm_bkdwn": {str(k): (float(v) if isinstance(v, (int, float)) else str(v))
                           for k, v in perm_bkdwn.items()},
            "stats": st}

def _run_ptai_analysis():
    global _ptai_analysis
    if not _ptai_ok: return
    _ptai_load_history()
    if not _ptai_hist: return
    try:
        import pandas as _pd
        live     = _ptai_mod.fetch_live_market()
        mkt_s, mkt_b = _ptai_mod.market_condition_score(live)
        mdb      = _ptai_mktdb if _ptai_mktdb is not None else _pd.DataFrame()
        _, sim_r = _ptai_mod.find_similar_days(live, mdb, _ptai_hist["daily_pnl"])
        beh      = _ptai_mod.behavioral_analysis(
            _ptai_hist["daily_pnl"], _ptai_hist["daily_trades"],
            _ptai_hist["expiry_days"], _ptai_hist["intraday"])
        ps, verd, pb = _ptai_mod.trading_permission_score(
            mkt_s, sim_r, beh, _ptai_hist["daily_pnl"])
        result = _serialize_ptai(live, mkt_s, mkt_b, sim_r, beh, ps, verd, pb,
                                  _ptai_hist.get("stats", {}))
        with _ptai_analysis_lock: _ptai_analysis = result
    except Exception as e:
        print(f"[ptai analysis] {e}")

def _build_ptai_ai_prompt(analysis: dict, today_pnl: dict) -> str:
    live = analysis.get("live", {}); stats = analysis.get("stats", {})
    sim  = analysis.get("sim",  {}); behav = analysis.get("behav", {})
    risk_lines    = "\n".join(f"  - {r['type']}: {r['detail']}" for r in behav.get("risks", []))
    insight_lines = "\n".join(f"  - {i}" for i in behav.get("insights", []))
    return f"""You are a Personal Trading Intelligence AI for Ayush Gupta, experienced F&O trader.
Time: {datetime.now().strftime('%H:%M')}  {datetime.now().strftime('%d-%b-%Y')}

TODAY: ₹{today_pnl.get('total_pnl', 0):+,.0f}  ({today_pnl.get('count', 0)} trades today, {today_pnl.get('wins', 0)}W/{today_pnl.get('losses', 0)}L)

LIVE: NIFTY {live.get('nifty', 'N/A')}  VIX {live.get('vix', 'N/A')} ({(live.get('vix_chg_pct') or 0):+.1f}%)  Gap {(live.get('gap_pct') or 0):+.2f}%  PCR {live.get('pcr', 'N/A')}

MARKET SCORE: {analysis.get('mkt_score', 0)}/100  |  PERMISSION SCORE: {analysis.get('perm_score', 0)}/100 — {analysis.get('verdict', '')}

SIMILAR DAYS WIN RATE: {sim.get('win_rate', 0)}%  avg win: ₹{sim.get('avg_win', 0):+,.0f}  avg loss: ₹{sim.get('avg_loss', 0):+,.0f}  ({sim.get('count', 0)} days)

3-YEAR STATS: {stats.get('total_days', 0)} days  {stats.get('win_rate', 0):.1f}% WR  Total P&L: ₹{stats.get('total_pnl', 0):+,.0f}

BEHAVIORAL RISKS:\n{risk_lines or '  None detected'}

INSIGHTS:\n{insight_lines or '  None'}

In under 180 words, give Ayush:
1. RECOMMENDATION (Trade/No Trade/Caution) with key reason
2. Direction bias + one level to watch
3. Position size based on permission score
4. One behavioral warning for today
Be direct and specific."""

def _run_ptai_ai():
    global _ptai_ai
    if not _features.get("ptai_ai"): return
    with _ptai_ai_lock: _ptai_ai = {"text": "", "ts": "", "status": "loading"}
    with _ptai_analysis_lock: analysis = dict(_ptai_analysis)
    with _lock: snap = dict(_snapshot)
    today_pnl = snap.get("pnl_today", {})
    if not analysis:
        with _ptai_ai_lock: _ptai_ai = {"text": "", "ts": "", "status": "no_data"}
        return
    text = _try_claude_cli(_build_ptai_ai_prompt(analysis, today_pnl),
                           timeout=60, feature_key="ptai_ai")
    if not _features.get("ptai_ai"): return
    with _ptai_ai_lock:
        if text:
            _ptai_ai = {"text": text, "ts": datetime.now().isoformat(timespec="seconds"), "status": "ok"}
        else:
            _ptai_ai = {"text": "", "ts": "", "status": "no_cli"}

# ─────────────────────────────────────────────────────────────
#  LIVE OPTION LTP — direct Groww API fetch (background thread)
# ─────────────────────────────────────────────────────────────
# Credentials loaded from ai_config.json (gitignored — never commit secrets)
def _load_groww_creds() -> tuple:
    try:
        cfg = json.loads(open(os.path.join(BASE, "ai_config.json")).read())
        return cfg.get("groww_api_key",""), cfg.get("groww_totp_secret","")
    except Exception:
        return "", ""


_GROWW_API_KEY, _GROWW_TOTP_SECRET = _load_groww_creds()

_ltp_access_token: str = ""
_ltp_token_ts: float = 0.0
_ltp_session = _req.Session()
_ltp_result: dict = {}
_ltp_result_lock = threading.Lock()
_instruments_for_ltp: list = []

def _load_instruments_for_ltp() -> list:
    global _instruments_for_ltp
    if _instruments_for_ltp:
        return _instruments_for_ltp
    try:
        with open(os.path.join(BASE, "instrument.csv"), newline="", encoding="utf-8") as f:
            _instruments_for_ltp = list(csv.DictReader(f))
    except Exception:
        pass
    return _instruments_for_ltp

def _get_ltp_token() -> str:
    global _ltp_access_token, _ltp_token_ts
    if _ltp_access_token and (time.time() - _ltp_token_ts) < 7200:
        return _ltp_access_token
    try:
        sys.path.insert(0, BASE)
        from growwapi import GrowwAPI  # type: ignore
        import pyotp
        from groww_token import get_access_token as _get_cached_token
        token = _get_cached_token(_GROWW_API_KEY, _GROWW_TOTP_SECRET)
        if token:
            _ltp_access_token = token
            _ltp_token_ts = time.time()
    except Exception:
        pass
    return _ltp_access_token

def _fetch_live_option_ltp(strike: int, direction: str, index: str) -> float:
    token = _get_ltp_token()
    if not token: return 0.0
    instruments = _load_instruments_for_ltp()
    if not instruments: return 0.0
    today = datetime.now().date()
    expiries = sorted({
        i["expiry_date"].strip() for i in instruments
        if i.get("underlying_symbol", "").upper() == index.upper()
        and i.get("expiry_date", "").strip()
        and datetime.strptime(i["expiry_date"].strip(), "%Y-%m-%d").date() >= today
    })
    if not expiries: return 0.0
    expiry = expiries[0]
    exchange = "BSE" if index.upper() == "SENSEX" else "NSE"
    for item in instruments:
        if (item.get("underlying_symbol", "").upper() != index.upper()): continue
        if item.get("expiry_date", "").strip() != expiry: continue
        if item.get("instrument_type", "").upper() != direction.upper(): continue
        try:
            if int(float(item.get("strike_price", 0))) != strike: continue
        except (ValueError, TypeError): continue
        ts = item.get("trading_symbol", "")
        if not ts: continue
        sym = f"{exchange}_{ts}"
        try:
            url = f"https://api.groww.in/v1/live-data/ltp?segment=FNO&exchange_symbols={sym}"
            resp = _ltp_session.get(url, headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "X-API-VERSION": "1.0",
            }, timeout=6)
            if resp.status_code == 200:
                val = resp.json().get("payload", {}).get(sym)
                return float(val) if val else 0.0
            if resp.status_code == 401:
                global _ltp_token_ts
                _ltp_token_ts = 0.0  # force re-auth next call
        except Exception:
            pass
        break
    return 0.0

def _ltp_fetcher_loop() -> None:
    global _ltp_result
    while True:
        try:
            with _lock:
                snap_copy = dict(_snapshot)
            csig  = snap_copy.get("bots", {}).get("chart_signal", {})
            strike    = csig.get("strike")
            direction = csig.get("direction")
            index     = snap_copy.get("index", "NIFTY")
            if strike and direction in ("CE", "PE"):
                ltp = _fetch_live_option_ltp(int(strike), direction, index)
                with _ltp_result_lock:
                    _ltp_result = {
                        "strike": int(strike), "direction": direction,
                        "ltp": ltp, "ts": datetime.now().isoformat(timespec="seconds"),
                    }
        except Exception:
            pass
        time.sleep(30)

# ─────────────────────────────────────────────────────────────
#  CONSENSUS
# ─────────────────────────────────────────────────────────────
def build_consensus(master: dict, fibo: dict, csig: dict, sigmon: dict) -> dict:
    bull, bear, sources = 0, 0, []
    if master.get("_live"):
        d, c = master.get("direction",""), float(master.get("confidence",0))
        if d == "CE" and c >= 60:
            w = 3 if c >= 75 else 2; bull += w; sources.append(f"MASTER→CE({c:.0f}%)")
        elif d == "PE" and c >= 60:
            w = 3 if c >= 75 else 2; bear += w; sources.append(f"MASTER→PE({c:.0f}%)")
        else: sources.append("MASTER→WAIT")
        pat = master.get("pattern","").upper()
        if any(k in pat for k in ("HAMMER","BULL","MORNING")): bull += 1
        elif any(k in pat for k in ("SHOOTING","BEAR","EVENING")): bear += 1
        s5 = int(master.get("s5m",0)); sp = int(master.get("sprem",0))
        bull += max(0,s5); bear += max(0,-s5)
        bull += max(0,sp); bear += max(0,-sp)
    if fibo.get("_live"):
        setup = fibo.get("trade_setup","").upper()
        if "CE" in setup and "NO TRADE" not in setup and "CONFLICT" not in setup:
            bull += 2; sources.append("FIBO→CE")
        elif "PE" in setup and "NO TRADE" not in setup and "CONFLICT" not in setup:
            bear += 2; sources.append("FIBO→PE")
        else: sources.append("FIBO→WAIT")
    # Chart signal: only count if FRESH ≤2min — option signals go stale fast
    csig_dt = _parse_ts(csig.get("ts",""))
    csig_fresh = bool(csig_dt) and (datetime.now() - csig_dt).total_seconds() <= 120
    if csig_fresh and csig.get("direction") in ("CE","PE"):
        d = csig["direction"]; w = 3 if csig.get("confidence") == "HIGH" else 2
        if d == "CE": bull += w; sources.append(f"CHART→CE({csig.get('confidence','')})")
        else:         bear += w; sources.append(f"CHART→PE({csig.get('confidence','')})")
    if sigmon.get("_live"):
        sc = sigmon.get("combined","").upper()
        if "STRONG CE" in sc: bull += 2; sources.append("SIGMON→STRONG_CE")
        elif "CE" in sc:      bull += 1; sources.append("SIGMON→CE")
        elif "STRONG PE" in sc: bear += 2; sources.append("SIGMON→STRONG_PE")
        elif "PE" in sc:      bear += 1; sources.append("SIGMON→PE")

    if   bull >= 6 and bull > bear: sig, cls = "STRONG CE ▲▲", "strong-bull"
    elif bull >= 3 and bull > bear: sig, cls = "CE ▲",          "bull"
    elif bear >= 6 and bear > bull: sig, cls = "STRONG PE ▼▼", "strong-bear"
    elif bear >= 3 and bear > bull: sig, cls = "PE ▼",          "bear"
    else:                            sig, cls = "WAIT ─",        "neutral"

    msgs = {"strong-bull": f"Strong bullish — all bots aligned CE (bull:{bull} bear:{bear})",
            "bull":         f"Bullish lean — wait for entry trigger (bull:{bull} bear:{bear})",
            "strong-bear": f"Strong bearish — all bots aligned PE (bear:{bear} bull:{bull})",
            "bear":         f"Bearish lean — wait for entry trigger (bear:{bear} bull:{bull})",
            "neutral":      f"No directional edge — monitor for setup (bull:{bull} bear:{bear})"}
    return {"signal": sig, "cls": cls, "summary": msgs[cls],
            "bull": bull, "bear": bear, "sources": sources}

# ─────────────────────────────────────────────────────────────
#  AI SUMMARY  (Claude CLI → Anthropic API → graceful degradation)
# ─────────────────────────────────────────────────────────────
AI_REFRESH_SECS    = 180  # AI summary: every 3 min
SCALP_REFRESH_SECS = 60   # Scalp plan: every 1 min

# Feature on/off flags (toggled via /api/toggle?f=ai or /api/toggle?f=scalp or /api/toggle?f=oi_ai)
_features = {"ai": False, "scalp": False, "ptai_ai": False, "oi_ai": False, "mb_ai": False, "qs_ai": False}

# ── Personal Trading AI (pre-market check) ────────────────────────────────
_pai_cache: dict = {"output": None, "score": None, "verdict": None,
                    "ts": None, "running": False, "error": False}
_pai_lock = threading.Lock()

def _run_pai_bg():
    global _pai_cache
    try:
        import subprocess as _sp
        result = _sp.run(
            [sys.executable, os.path.join(BASE, "PERSONAL_TRADING_AI.py")],
            capture_output=True, text=True, timeout=180, cwd=BASE
        )
        raw = result.stdout + (result.stderr if result.returncode != 0 else "")
        clean = _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw)
        score_m   = _re.search(r'(?:Permission Score|PERMISSION SCORE)[^\d]*(\d+)', clean)
        verdict_m = _re.search(r'\b(NO_TRADE|CAUTION|NORMAL|HIGH_CONFIDENCE)\b', clean)
        with _pai_lock:
            _pai_cache.update({
                "output":  clean,
                "score":   int(score_m.group(1)) if score_m else None,
                "verdict": verdict_m.group(1) if verdict_m else None,
                "ts":      datetime.now().strftime("%H:%M"),
                "running": False,
                "error":   result.returncode != 0,
            })
    except Exception as e:
        with _pai_lock:
            _pai_cache.update({"output": f"Error running PERSONAL_TRADING_AI.py:\n{e}",
                                "running": False, "error": True})

# Single persistent Claude session — reused for all AI queries so only one session
# appears in the VSCode session switcher. Reset if the session expires or errors.
_ai_session_id: str = ""
_ai_session_lock = threading.Lock()

_ai_lock    = threading.Lock()
_scalp_lock = threading.Lock()
_oi_ai_lock = threading.Lock()
_mb_ai_lock = threading.Lock()
_ai_summary: dict = {"text": "", "ts": "", "status": "init", "error": "", "source": ""}
_mb_ai_cache: dict = {
    "intraday": "", "longterm": "", "key_levels": "", "risks": "", "bottom_line": "",
    "ts": "", "status": "idle", "error": "", "context_lines": 0
}
_scalp_plan: dict = {"text": "", "ts": "", "status": "init", "error": ""}
_oi_ai:      dict = {"text": "", "ts": "", "status": "init", "error": "", "source": ""}

# Quick Summary — AI-generated paragraph (auto-refresh, no toggle)
_qs_lock  = threading.Lock()
_qs_cache: dict = {"text": "", "ts": "", "status": "idle", "error": ""}

OI_AI_REFRESH_SECS = 120  # OI Intelligence AI: every 2 min
MB_AI_REFRESH_SECS = 300  # AI Brain: every 5 min
QS_AI_REFRESH_SECS = 300  # Quick Summary AI: every 5 min

# OI History — rolling buffer of snapshots so the UI can show tick-over-tick trend
_oi_history: list = []
_oi_history_lock  = threading.Lock()
_OI_HISTORY_MAX   = 200  # keep last 200 ticks (~full trading day at 2min refresh)

# VIX History — intraday rolling buffer for VIX monitoring (fetched every 2 min)
_vix_history: list  = []
_vix_history_lock   = threading.Lock()
_VIX_HISTORY_MAX    = 210  # ~7h at 2-min intervals covers full trading session
_vix_session_open: list = [0.0]  # mutable so inner func can write; [0] = opening VIX

def _build_prompt(snap: dict) -> str:
    m  = snap.get("bots",{}).get("master",{})
    fi = snap.get("bots",{}).get("fibo",{})
    cs = snap.get("bots",{}).get("chart_signal",{})
    co = snap.get("consensus",{})
    spot = snap.get("spot", 0)
    idx  = snap.get("index", "NIFTY")

    conf_lines = ""
    for c in (fi.get("confluence") or [])[:4]:
        d = c["dist_pts"]; arr = "▲" if d > 0 else "▼"
        conf_lines += f"  {'★'*c['stars']} {c['price']:,.0f} ({arr}{abs(d):.0f}pts) [{c['tags']}]\n"

    trig = ""
    if fi.get("ce_trigger"): trig += f"  CE entry: {fi['ce_trigger']}\n"
    if fi.get("pe_trigger"): trig += f"  PE entry: {fi['pe_trigger']}\n"

    opt_line = ""
    if cs.get("direction") in ("CE","PE"):
        opt_line = (f"OPTION SIGNAL: BUY {cs['direction']} {cs.get('strike','ATM')} "
                    f"LTP ₹{cs.get('option_ltp',0):.0f} | Conf:{cs.get('confidence')} "
                    f"| R:R {cs.get('rr_ratio',0):.1f}:1 | {cs.get('reason','')}\n")

    dh = fi.get("day_high", 0) or spot
    dl = fi.get("day_low",  0) or spot
    pos_pct = f"{(spot-dl)/(dh-dl)*100:.0f}%" if dh != dl else "N/A"

    return f"""You are a live intraday NIFTY F&O trading assistant.
Time: {datetime.now().strftime('%H:%M')}  {datetime.now().strftime('%d-%b-%Y')}

MARKET SNAPSHOT:
Index: {idx}  Spot: {spot:,.2f}
Day: H {dh:,.0f}  L {dl:,.0f}  ({fi.get('day_dir','').upper()})  Position: {pos_pct} into range
1H Zone: {fi.get('zone_1h','—')}

CONSENSUS: {co.get('signal','—')} (bull:{co.get('bull',0)} bear:{co.get('bear',0)})
Sources: {', '.join(co.get('sources',[]))}

MASTER SIGNAL:
Direction: {m.get('direction','—')}  Confidence: {m.get('confidence',0):.1f}%
Zone: {m.get('zone','—')}  Pattern: {m.get('pattern','—')}
Scores: 1H={m.get('s1h',0)}  15M={m.get('s15m',0)}  5M={m.get('s5m',0)}  Prem={m.get('sprem',0)}
Stop: {m.get('stop',0):,.1f}  Target: {m.get('target',0):,.1f}  R:R: {m.get('rr',0):.1f}
15M Floor: {m.get('sl15m',0):,.1f}  15M Ceiling: {m.get('sh15m',0):,.1f}

FIBONACCI CONFLUENCE ZONES:
{conf_lines}
ENTRY TRIGGERS:
{trig}
{opt_line}
Respond in EXACTLY this format (keep it tight, max 160 words):

📍 SITUATION: [one line — what is price doing right now and why]

🎯 LEVELS TO WATCH:
• [price] — [level name]: [what to do if price reaches here, CE or PE]
• [price] — [level name]: [what to do if price reaches here]
• [price] — [level name]: [what to do if price reaches here]

⚡ ACTION NOW: [WAIT / BUY CE / BUY PE] — [one specific reason]

⚠️ KEY RISK: [one risk that could invalidate this view]

Use specific prices. No generic advice."""

_running_procs: dict = {"ai": None, "scalp": None, "ptai_ai": None, "oi_ai": None}
_proc_lock = threading.Lock()

def _try_claude_cli(prompt: str, timeout: int = 45, feature_key: str = "") -> str:
    """Run claude CLI, reusing a single persistent session for all AI queries.
    The session is visible in the VSCode session switcher so you can inspect or
    continue the conversation. If feature_key is given, cancels immediately if
    that feature is toggled off while running."""
    global _ai_session_id
    import subprocess, shutil, json as _json
    claude_bin = shutil.which("claude")
    if not claude_bin: return ""
    try:
        with _ai_session_lock:
            sid = _ai_session_id

        cmd = [claude_bin, "-p", prompt, "--output-format", "json"]
        if sid:
            cmd += ["--resume", sid]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if feature_key:
            with _proc_lock: _running_procs[feature_key] = proc

        deadline = time.time() + timeout
        while time.time() < deadline:
            if feature_key and not _features.get(feature_key):
                proc.terminate()
                try: proc.wait(timeout=2)
                except Exception: proc.kill()
                if feature_key:
                    with _proc_lock: _running_procs[feature_key] = None
                return ""
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        else:
            proc.terminate()
            try: proc.wait(timeout=2)
            except Exception: proc.kill()
            if feature_key:
                with _proc_lock: _running_procs[feature_key] = None
            return ""

        stdout, _ = proc.communicate()
        if feature_key:
            with _proc_lock: _running_procs[feature_key] = None

        if proc.returncode == 0 and stdout.strip():
            try:
                data = _json.loads(stdout.strip())
                # Persist the session ID so every subsequent call reuses this session
                new_sid = data.get("session_id", "")
                if new_sid:
                    with _ai_session_lock:
                        _ai_session_id = new_sid
                result = data.get("result", "") or data.get("content", [{}])[0].get("text", "") \
                         if isinstance(data.get("content"), list) else data.get("result", "")
                return result.strip() if result else ""
            except (_json.JSONDecodeError, Exception):
                # Fallback: treat as plain text
                return stdout.strip()
        return ""
    except Exception:
        if feature_key:
            with _proc_lock: _running_procs[feature_key] = None
        return ""

def _mins_to_close() -> int:
    now   = datetime.now()
    close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    secs  = (close - now).total_seconds()
    return max(0, int(secs // 60))

def _build_scalp_prompt(snap: dict) -> str:
    m    = snap.get("bots",{}).get("master",{})
    fi   = snap.get("bots",{}).get("fibo",{})
    co   = snap.get("consensus",{})
    spot = snap.get("spot", 0)
    idx  = snap.get("index","NIFTY")
    mtc  = _mins_to_close()

    # nearest level above and below
    levels_above = sorted(
        [l for l in fi.get("fib_levels",[]) if l["price"] > spot],
        key=lambda x: x["price"])
    levels_below = sorted(
        [l for l in fi.get("fib_levels",[]) if l["price"] < spot],
        key=lambda x: -x["price"])
    res = f"{levels_above[0]['price']:,.0f} (+{levels_above[0]['price']-spot:.0f}pts)" if levels_above else "—"
    sup = f"{levels_below[0]['price']:,.0f} (-{spot-levels_below[0]['price']:.0f}pts)" if levels_below else "—"

    return f"""NIFTY scalping assistant. Respond with ONE LINE only — no explanation, no extra text.

Time: {datetime.now().strftime('%H:%M')} | {mtc} min to market close | {idx} spot: {spot:,.2f}
Resistance: {res} | Support: {sup}
Trend: {co.get('signal','—')} (bull:{co.get('bull',0)} bear:{co.get('bear',0)})
15M score: {m.get('s15m',0)} | 5M: {m.get('s5m',0)} | Prem: {m.get('sprem',0)} | Pattern: {m.get('pattern','—')}
Zone: {m.get('zone','—')}

Reply in EXACTLY this format (one line, max 18 words total):
[BUY CE / BUY PE / WAIT] — entry: [price or "break X"], target: [price], SL: [price], [reason ≤6 words]

Example: BUY PE — entry: break 23,487, target: 23,430, SL: 23,520, 15M floor breakdown confirmed"""

def generate_scalp_plan(snap: dict) -> None:
    global _scalp_plan
    if not _features.get("scalp"): return   # double-check before starting
    if not snap or not snap.get("spot"):
        with _scalp_lock:
            _scalp_plan = {"text":"","ts":"","status":"no_data","error":"No data"}
        return
    text = _try_claude_cli(_build_scalp_prompt(snap), timeout=25, feature_key="scalp")
    if not _features.get("scalp"): return   # was disabled while running
    if text:
        # Keep only first line in case Claude added extra
        line = next((l.strip() for l in text.splitlines() if l.strip()), text)
        with _scalp_lock:
            _scalp_plan = {"text": line, "ts": datetime.now().isoformat(timespec="seconds"),
                           "status": "ok", "error": ""}
        return
    with _scalp_lock:
        _scalp_plan = {"text":"","ts":"","status":"no_subscription","error":"no_cli"}

def generate_ai_summary(snap: dict) -> None:
    global _ai_summary
    if not _features.get("ai"): return   # double-check before starting
    if not snap or not snap.get("spot"):
        with _ai_lock:
            _ai_summary = {"text":"","ts":"","status":"no_data",
                           "error":"No live bot data yet","source":""}
        return

    text = _try_claude_cli(_build_prompt(snap), feature_key="ai")
    if not _features.get("ai"): return   # was disabled while running
    if text:
        with _ai_lock:
            _ai_summary = {"text": text, "source": "Claude Code CLI",
                           "ts": datetime.now().isoformat(timespec="seconds"),
                           "status": "ok", "error": ""}
        return

    # Claude CLI not found or failed
    with _ai_lock:
        _ai_summary = {"text": "", "ts": "", "status": "no_subscription",
                       "error": "no_cli", "source": ""}

# ─────────────────────────────────────────────────────────────
#  OI INTELLIGENCE AI  (OI data + signal data → Claude summary)
# ─────────────────────────────────────────────────────────────
def _build_oi_prompt(snap: dict) -> str:
    m    = snap.get("bots", {}).get("master", {})
    fi   = snap.get("bots", {}).get("fibo", {})
    co   = snap.get("consensus", {})
    spot = snap.get("spot", 0)
    idx  = snap.get("index", "NIFTY")
    oi   = snap.get("oi_snapshot", {})

    if not oi or oi.get("_stale"):
        return ""   # no usable OI data

    price       = oi.get("price", spot)
    atm         = oi.get("atm", 0)
    pcr_all     = oi.get("pcr_all", 0)
    pcr_atm     = oi.get("pcr_atm", 0)
    sentiment   = oi.get("sentiment", "NEUTRAL")
    writer_bias = oi.get("writer_bias", "NEUTRAL")
    bull_score   = oi.get("bullish_score", 0)
    bear_score   = oi.get("bearish_score", 0)
    ce_writing   = oi.get("ce_writing_strikes", [])
    pe_writing   = oi.get("pe_writing_strikes", [])
    mkt_signal   = oi.get("market_signal", "")
    bull_v2      = oi.get("bull_score_v2", 0)
    bear_v2      = oi.get("bear_score_v2", 0)
    momentum     = oi.get("momentum_score", 0)
    sig_list     = oi.get("signal_list", [])
    max_pain     = oi.get("max_pain", 0)
    vol_pcr      = oi.get("vol_pcr", 0)
    iv_skew      = oi.get("iv_skew", 0)
    sm_ce        = oi.get("smart_money_ce", [])
    sm_pe        = oi.get("smart_money_pe", [])
    resistance  = oi.get("resistance", [])
    support_lvl = oi.get("support", [])
    total_ce    = oi.get("total_oi_ce", 0)
    total_pe    = oi.get("total_oi_pe", 0)
    chg_ce      = oi.get("total_chg_ce", 0)
    chg_pe      = oi.get("total_chg_pe", 0)

    atm_oi  = oi.get("atm_strikes_oi", {})
    atm_lines = []
    for strike_s, sd in sorted(atm_oi.items(), key=lambda x: int(x[0])):
        ce_oi = sd.get("ce_oi", 0); pe_oi = sd.get("pe_oi", 0)
        diff  = pe_oi - ce_oi
        marker = " ← ATM" if int(strike_s) == atm else ""
        atm_lines.append(
            f"  {strike_s:>6}: CE={ce_oi//1e6:.2f}M  PE={pe_oi//1e6:.2f}M  "
            f"diff={diff//1e6:+.2f}M{marker}")

    sm_ce_txt = '  '.join(f"{x['strike']}(+{x['oi_change']/1e3:.0f}K)" for x in sm_ce[:3]) or 'none'
    sm_pe_txt = '  '.join(f"{x['strike']}(+{x['oi_change']/1e3:.0f}K)" for x in sm_pe[:3]) or 'none'
    sig_txt   = '\n'.join(f"  [{s['dir'].upper():4}+{s['pts']:2}] {s['label']}" for s in sig_list if s['pts'] > 0) or '  (no signal data)'

    return f"""You are an expert NIFTY F&O market analyst with deep OI expertise.
Time: {datetime.now().strftime('%H:%M')}  |  {idx} Spot: {price:,.2f}  |  ATM: {atm}
OI data age: {oi.get('_age_sec', 0)}s

═══ 10-FACTOR MARKET SIGNAL ═══
Signal: {mkt_signal or 'PENDING'}  |  Bull Score: {bull_v2}/100  Bear Score: {bear_v2}/100  Momentum: {momentum}/100
Active factors:
{sig_txt}

═══ OPEN INTEREST (NSE Option Chain) ═══
PCR all strikes: {pcr_all:.2f}   PCR ATM±3: {pcr_atm:.2f}
Total CE OI: {total_ce/1e7:.2f}Cr  (session change: {chg_ce/1e7:+.2f}Cr)
Total PE OI: {total_pe/1e7:.2f}Cr  (session change: {chg_pe/1e7:+.2f}Cr)
OI Sentiment: {sentiment}  |  Max Pain: {max_pain}  |  Vol PCR: {vol_pcr:.2f}  |  IV Skew: {iv_skew:+.1f}%

Smart Money today — CE additions (resistance): {sm_ce_txt}
Smart Money today — PE additions (support):    {sm_pe_txt}

Writer Activity (tick-over-tick):
  Writer Bias: {writer_bias}  [Bull: {bull_score:.1f}M  Bear: {bear_score:.1f}M]
  CALL writers ADDING: {', '.join(str(s) for s in ce_writing[:4]) or 'none'} → resistance forming
  PUT  writers ADDING: {', '.join(str(s) for s in pe_writing[:4]) or 'none'} → support forming

ATM ±3 Strike OI breakdown:
{chr(10).join(atm_lines)}

Key Resistance (highest CE OI): {', '.join(str(r) for r in resistance[:3])}
Key Support    (highest PE OI): {', '.join(str(s) for s in support_lvl[:3])}

═══ TECHNICAL SIGNALS ═══
Master Signal: {m.get('direction','WAIT')}  Confidence: {float(m.get('confidence',0)):.0f}%
Scores — 1H: {m.get('s1h',0)}  15M: {m.get('s15m',0)}  5M: {m.get('s5m',0)}  Premium: {m.get('sprem',0)}
Pattern: {m.get('pattern','—')}  |  Zone: {m.get('zone','—')}
Fibo 1H zone: {fi.get('zone_1h','—')}  |  Setup: {fi.get('trade_setup','—')}
Consensus: {co.get('signal','—')} (bull:{co.get('bull',0)} bear:{co.get('bear',0)})
Sources: {', '.join(co.get('sources',[]))}

═══ RESPOND EXACTLY IN THIS FORMAT ═══

🧠 OI INTELLIGENCE

📊 OI READS:
• [PCR/total OI bias — one line with specific numbers]
• [Who is writing where and what that means — specific strikes]
• [Key OI wall: strongest resistance/support from OI — specific price]

⚡ TECHNICALS CONFIRM:
• [Master signal + scores — agree or disagree with OI?]
• [Fibo zone — does it align?]

🎯 COMBINED MARKET VIEW:
Short-term (next 15-30 min): [BULLISH/BEARISH/SIDEWAYS] — [price level + specific reason]
Medium-term (next 1-2 hrs):  [BULLISH/BEARISH/SIDEWAYS] — [price level + specific reason]

🚦 TRADE BIAS: [BUY CE at XXXX / BUY PE at XXXX / WAIT] — [one-line reason with strike]

⚠️ INVALIDATION: [exact price level that kills this view]

Max 200 words. Use specific price levels. No generic advice."""


def generate_oi_summary(snap: dict) -> None:
    global _oi_ai
    if not _features.get("oi_ai"): return
    oi = snap.get("oi_snapshot", {})
    if not oi or oi.get("_stale") or not snap.get("spot"):
        with _oi_ai_lock:
            _oi_ai = {"text": "", "ts": "", "status": "no_data",
                      "error": "OI snapshot missing or stale — run calculate_oi_pcr.py", "source": ""}
        return

    prompt = _build_oi_prompt(snap)
    if not prompt:
        with _oi_ai_lock:
            _oi_ai = {"text": "", "ts": "", "status": "no_data",
                      "error": "Could not build OI prompt", "source": ""}
        return

    text = _try_claude_cli(prompt, timeout=50, feature_key="oi_ai")
    if not _features.get("oi_ai"): return
    if text:
        with _oi_ai_lock:
            _oi_ai = {"text": text, "source": "Claude Code CLI",
                      "ts": datetime.now().isoformat(timespec="seconds"),
                      "status": "ok", "error": ""}
        return
    with _oi_ai_lock:
        _oi_ai = {"text": "", "ts": "", "status": "no_subscription",
                  "error": "no_cli", "source": ""}

# ─────────────────────────────────────────────────────────────
#  AI BRAIN  (OpenAI gpt-4o → comprehensive dual summary)
# ─────────────────────────────────────────────────────────────
def _read_conv_signals() -> list:
    path = os.path.join(BASE, ".convergence_signals.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("signals", [])[-5:]
    except Exception:
        return []

def _read_auto_mode_status() -> dict:
    path = os.path.join(BASE, ".auto_mode_status.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _build_mb_ai_prompt(snap: dict) -> tuple:
    """Build comprehensive prompt from ALL bot state. Returns (prompt, context_lines)."""
    now_str = datetime.now().strftime("%H:%M  %d-%b-%Y")
    spot  = snap.get("spot", 0)
    idx   = snap.get("index", "NIFTY")
    bots  = snap.get("bots", {})
    m     = bots.get("master", {})
    fi    = bots.get("fibo", {})
    cdec  = bots.get("chart_decision", {})
    prem  = bots.get("premium", {})
    mb    = bots.get("momentum_bot", {})
    tb    = bots.get("trendline_bot", {})
    oi    = snap.get("oi_snapshot", {})
    cons  = snap.get("consensus", {})
    vix_h = snap.get("vix_history", [])
    mkt   = snap.get("mkt_idx", {})
    conv  = _read_conv_signals()
    auto  = _read_auto_mode_status()

    lines = []
    lines.append(f"Time: {now_str}")
    lines.append(f"Index: {idx}  Spot: ₹{spot:,.2f}")

    # ── VIX ──
    vix_val = 0.0; vix_chg = 0.0
    if vix_h:
        vix_val = vix_h[-1].get("vix", 0) if isinstance(vix_h[-1], dict) else 0
        if len(vix_h) >= 2:
            v0 = vix_h[0].get("vix", 0) if isinstance(vix_h[0], dict) else 0
            vix_chg = round(vix_val - v0, 2) if v0 else 0
    if not vix_val and mkt:
        vix_val = mkt.get("vix", {}).get("last", 0) or 0
    if vix_val:
        lines.append(f"\nINDIA VIX: {vix_val:.2f}  (session change: {vix_chg:+.2f})")
        zone = "HIGH FEAR" if vix_val > 15 else "MODERATE" if vix_val > 13 else "LOW FEAR"
        lines.append(f"  VIX Zone: {zone} — {'premium expensive, tight stops' if vix_val > 15 else 'normal conditions'}")

    # ── OI & PCR ──
    if oi and not oi.get("_stale"):
        pcr_all  = oi.get("pcr_all", 0)
        pcr_atm  = oi.get("pcr_atm", 0)
        atm      = oi.get("atm", 0)
        sent     = oi.get("sentiment", "")
        wbias    = oi.get("writer_bias", "")
        max_pain = oi.get("max_pain", 0)
        vol_pcr  = oi.get("vol_pcr", 0)
        bull_v2  = oi.get("bull_score_v2", 0)
        bear_v2  = oi.get("bear_score_v2", 0)
        mkt_sig  = oi.get("market_signal", "")
        tot_ce   = oi.get("total_oi_ce", 0)
        tot_pe   = oi.get("total_oi_pe", 0)
        chg_ce   = oi.get("total_chg_ce", 0)
        chg_pe   = oi.get("total_chg_pe", 0)
        resist   = oi.get("resistance", [])
        support  = oi.get("support", [])
        sm_ce    = oi.get("smart_money_ce", [])
        sm_pe    = oi.get("smart_money_pe", [])
        ce_writ  = oi.get("ce_writing_strikes", [])
        pe_writ  = oi.get("pe_writing_strikes", [])
        lines.append(f"\nOI & PCR DATA (age: {oi.get('_age_sec',0)}s):")
        lines.append(f"  PCR all: {pcr_all:.2f}  PCR ATM±3: {pcr_atm:.2f}  Vol-PCR: {vol_pcr:.2f}")
        lines.append(f"  ATM: {atm}  Max Pain: {max_pain}  (spot vs maxpain: {spot-max_pain:+.0f}pts)")
        lines.append(f"  OI Sentiment: {sent}  Writer Bias: {wbias}")
        lines.append(f"  10-Factor Signal: {mkt_sig}  Bull:{bull_v2}/100  Bear:{bear_v2}/100")
        lines.append(f"  Total CE OI: {tot_ce/1e7:.2f}Cr ({chg_ce/1e7:+.2f}Cr today)")
        lines.append(f"  Total PE OI: {tot_pe/1e7:.2f}Cr ({chg_pe/1e7:+.2f}Cr today)")
        if resist: lines.append(f"  Key Resistance (CE wall): {', '.join(str(r) for r in resist[:4])}")
        if support: lines.append(f"  Key Support (PE wall):    {', '.join(str(s) for s in support[:4])}")
        if ce_writ: lines.append(f"  Call writers adding:      {', '.join(str(s) for s in ce_writ[:4])} → resistance")
        if pe_writ: lines.append(f"  Put writers adding:       {', '.join(str(s) for s in pe_writ[:4])} → support")
        if sm_ce:   lines.append(f"  Smart money CE: " + "  ".join(f"{x['strike']}(+{x['oi_change']/1e3:.0f}K)" for x in sm_ce[:3]))
        if sm_pe:   lines.append(f"  Smart money PE: " + "  ".join(f"{x['strike']}(+{x['oi_change']/1e3:.0f}K)" for x in sm_pe[:3]))

    # ── Fibonacci ──
    if fi:
        lines.append(f"\nFIBONACCI ANALYSIS:")
        lines.append(f"  1H Zone: {fi.get('zone_1h','—')}  15M Zone: {fi.get('zone_15m','—')}")
        dh = fi.get("day_high", 0) or spot; dl = fi.get("day_low", 0) or spot
        pos_pct = f"{(spot-dl)/(dh-dl)*100:.0f}%" if dh != dl else "N/A"
        lines.append(f"  Day H: {dh:,.0f}  L: {dl:,.0f}  (price at {pos_pct} of range)")
        lines.append(f"  Day Direction: {fi.get('day_dir','—').upper()}")
        if fi.get("ce_trigger"): lines.append(f"  CE Entry trigger: {fi['ce_trigger']}")
        if fi.get("pe_trigger"): lines.append(f"  PE Entry trigger: {fi['pe_trigger']}")
        conf = (fi.get("confluence") or [])[:5]
        if conf:
            lines.append("  Confluence zones (nearest first):")
            for c in conf:
                d = c.get("dist_pts", 0)
                lines.append(f"    {'★'*c.get('stars',1)} ₹{c.get('price',0):,.0f} ({'+' if d>0 else ''}{d:.0f}pts) [{c.get('tags','')}]")

    # ── Master Signal ──
    if m:
        lines.append(f"\nMASTER SIGNAL:")
        lines.append(f"  Direction: {m.get('direction','WAIT')}  Confidence: {float(m.get('confidence',0)):.0f}%")
        lines.append(f"  Scores — 1H:{m.get('s1h',0)} 15M:{m.get('s15m',0)} 5M:{m.get('s5m',0)} Prem:{m.get('sprem',0)}")
        lines.append(f"  Pattern: {m.get('pattern','—')}  Zone: {m.get('zone','—')}")
        if m.get("stop"):    lines.append(f"  Stop: ₹{m['stop']:,.1f}  Target: ₹{m.get('target',0):,.1f}  R:R: {m.get('rr',0):.1f}")
        if m.get("sl15m"):   lines.append(f"  15M Floor: ₹{m['sl15m']:,.1f}  Ceiling: ₹{m.get('sh15m',0):,.1f}")

    # ── Consensus ──
    if cons:
        lines.append(f"\nCONSENSUS: {cons.get('signal','—')}  (bull:{cons.get('bull',0)} bear:{cons.get('bear',0)})")
        if cons.get("sources"): lines.append(f"  Sources: {', '.join(cons['sources'])}")

    # ── Chart S/R Levels ──
    if cdec:
        lines.append(f"\nCHART ANALYSIS:")
        if cdec.get("decision"):    lines.append(f"  Trade Decision: {cdec['decision']}")
        if cdec.get("option_text"): lines.append(f"  Option Suggestion: {cdec['option_text']}")

    # ── Premium Flow ──
    if prem:
        lines.append(f"\nPREMIUM FLOW:")
        if prem.get("direction"):  lines.append(f"  Direction: {prem.get('direction','—')}  Strength: {prem.get('strength','—')}")
        if prem.get("ce_trend"):   lines.append(f"  CE Trend: {prem['ce_trend']}  PE Trend: {prem.get('pe_trend','—')}")
        if prem.get("momentum"):   lines.append(f"  Momentum: {prem['momentum']}")

    # ── Momentum Bot ──
    if mb:
        lines.append(f"\nMOMENTUM BOT:")
        lines.append(f"  Trades today: {mb.get('trades_today',0)}  Wins: {mb.get('wins',0)}  Losses: {mb.get('losses',0)}")
        if mb.get("last_signal"):  lines.append(f"  Last signal: {mb['last_signal']}")
        if mb.get("last_trade"):   lines.append(f"  Last trade: {mb['last_trade']}")

    # ── Convergence Signals ──
    if conv:
        lines.append(f"\nCONVERGENCE SIGNALS (last {len(conv)}):")
        for cs in conv[-3:]:
            lines.append(f"  [{cs.get('time','?')}] {cs.get('side','?')} strength:{cs.get('strength','?')} "
                         f"strikes:{cs.get('conv_count',0)} vel:{cs.get('avg_vel_pct',0):.2f}%")

    # ── Trendline Signals ──
    ts_sigs = (tb.get("signals") or [])[-3:] if tb else []
    if ts_sigs:
        lines.append(f"\nTRENDLINE SIGNALS (last {len(ts_sigs)}):")
        for s in ts_sigs:
            lines.append(f"  [{s.get('time','?')}] {s.get('type','?')} {s.get('symbol','?')} @ ₹{s.get('ltp',0):.0f}")

    # ── Auto Mode ──
    if auto:
        lines.append(f"\nAUTO MODE: {auto.get('state','—')}  enabled:{auto.get('enabled',False)}")

    context = "\n".join(lines)
    n_lines = len(lines)

    prompt = f"""You are an expert NIFTY F&O trading analyst. Based on ALL available live data below, generate a comprehensive market summary.

{context}

Respond in EXACTLY this format (be specific with numbers, avoid generic advice):

📊 INTRADAY VIEW (Next 1-2 hours):
[2-3 sentences: current market state, momentum direction, immediate bias, what is likely to happen in next 1-2 hours based on all signals]

📈 LONG-TERM VIEW (Positional/Swing — 2-5 days):
[2-3 sentences: overall trend direction, key levels that define the trend, whether to hold positions overnight, swing trade bias]

⚡ KEY LEVELS TO WATCH:
• ₹[price] — [level name]: [what to do if price reaches here — CE or PE trade]
• ₹[price] — [level name]: [what to do if price reaches here]
• ₹[price] — [level name]: [what to do if price reaches here]

⚠️ RISKS:
• [specific risk 1 — data-backed]
• [specific risk 2 — data-backed]

💡 BOTTOM LINE: [ONE clear action sentence: STAY CASH / BUY CE at ₹X / BUY PE at ₹X — with one specific reason]

Use specific price levels from the data. Max 250 words total."""

    return prompt, n_lines

def _build_qs_prompt(snap: dict) -> str:
    """Build a focused, short prompt for the Quick Summary AI call."""
    spot  = snap.get("spot", 0) or 0
    idx   = snap.get("index", "NIFTY")
    oi    = snap.get("oi_snapshot", {})
    cons  = snap.get("consensus", {})
    bots  = snap.get("bots", {})
    m     = bots.get("master", {})
    prem  = bots.get("premium", {})
    vix_h = snap.get("vix_history", [])
    mkt   = snap.get("mkt_idx", {})

    # OI levels
    res_str  = oi.get("resistance_strength", [])
    sup_str  = oi.get("support_strength", [])
    res_list = oi.get("resistance", [])
    sup_list = oi.get("support", [])

    ce_strike = res_str[0]["strike"] if res_str else (res_list[0] if res_list else "N/A")
    ce_oi_cr  = res_str[0]["ce_oi"] / 1e7 if res_str else 0.0
    pe_strike = sup_str[0]["strike"] if sup_str else (sup_list[0] if sup_list else "N/A")
    pe_oi_cr  = sup_str[0]["pe_oi"] / 1e7 if sup_str else 0.0
    ce_next   = res_str[1]["strike"] if len(res_str) > 1 else (res_list[1] if len(res_list) > 1 else "N/A")
    pe_next   = sup_str[1]["strike"] if len(sup_str) > 1 else (sup_list[1] if len(sup_list) > 1 else "N/A")

    # VIX
    vix_val = 0.0
    vix_chg = 0.0
    if vix_h:
        last = vix_h[-1]
        vix_val = last.get("vix", 0.0) if isinstance(last, dict) else 0.0
        if len(vix_h) >= 2:
            first = vix_h[0]
            v0 = first.get("vix", 0.0) if isinstance(first, dict) else 0.0
            vix_chg = round(vix_val - v0, 2) if v0 else 0.0
    if not vix_val and mkt:
        vix_val = float(mkt.get("vix", {}).get("last", 0) or 0)

    # ATM straddle
    atm_straddle = oi.get("atm_straddle", 0)
    atm_ce_iv    = oi.get("atm_ce_iv", 0)
    atm_pe_iv    = oi.get("atm_pe_iv", 0)

    # Sentiment + signal
    sentiment  = oi.get("sentiment", "")
    mkt_signal = oi.get("market_signal", "")
    master_dir = m.get("direction", "") or cons.get("signal", "")
    master_conf = m.get("confidence", 0)
    max_pain   = oi.get("max_pain", 0)
    pcr_all    = oi.get("pcr_all", 0)
    pcr_atm    = oi.get("pcr_atm", 0)

    # Premium flow
    prem_dir = (prem.get("direction","") if prem else "") or ""
    prem_mom = (prem.get("momentum","") if prem else "") or ""

    now_str = datetime.now().strftime("%H:%M %d-%b-%Y")

    return f"""You are an expert NIFTY options trader. Write a concise market snapshot in EXACTLY 3 short paragraphs. Use plain English, specific price levels from the data, and no generic advice.

DATA ({now_str}):
Index: {idx}  Spot: {int(spot):,}
Put OI (Call favours / Support wall): {pe_strike}  OI: ₹{pe_oi_cr:.1f}Cr  Next support: {pe_next}
Call OI (Put favours / Resistance wall): {ce_strike}  OI: ₹{ce_oi_cr:.1f}Cr  Next resistance: {ce_next}
Max Pain: {max_pain}  PCR all: {pcr_all:.2f}  PCR ATM: {pcr_atm:.2f}
OI Sentiment: {sentiment}  Market Signal: {mkt_signal}
Master Signal: {master_dir}  Confidence: {master_conf}%
India VIX: {vix_val:.2f}  Session change: {vix_chg:+.2f}
ATM Straddle: ₹{atm_straddle}  CE IV: {atm_ce_iv:.1f}%  PE IV: {atm_pe_iv:.1f}%
Premium flow: {prem_dir} {prem_mom}

Write EXACTLY this structure (3 paragraphs, no headers, no bullets):

Paragraph 1 — OI PICTURE: Start with "Market is [Bullish/Bearish/Sideways]." Then mention Put OI (Call favours) at {pe_strike} with its ₹Cr value acting as support, and Call OI (Put favours) at {ce_strike} with its ₹Cr value acting as resistance. Current spot. Then: if it breaks below {pe_strike} next move to {pe_next}, if it breaks above {ce_strike} next move to {ce_next}.

Paragraph 2 — VIX & PREMIUMS: Start with "VIX at {vix_val:.2f}..." Explain what VIX level means (calm/elevated/fearful), session change direction, whether premiums are cheap or expensive, and what the straddle price tells us about expected range.

Paragraph 3 — ACTION: Start with "Action:" Give ONE clear recommendation — STAY CASH / BUY CE / BUY PE — with the exact price trigger and target. If sideways, say which side to favour first and why. Keep it to 2 sentences max.

Max 120 words total. Be specific, crisp, trader-language."""


def generate_qs_ai(snap: dict) -> None:
    """Call Claude CLI to generate the Quick Summary paragraph. Runs in background thread."""
    global _qs_cache
    try:
        # Use OI snapshot price as fallback when bots are stale
        spot = snap.get("spot") or snap.get("oi_snapshot", {}).get("price", 0)
        if not snap or not spot:
            with _qs_lock:
                _qs_cache.update({"status": "no_data", "text": "",
                                  "error": "No spot data — start calculate_oi_pcr.py first",
                                  "ts": datetime.now().isoformat(timespec="seconds")})
            return

        # Mark running (guard against double-start)
        with _qs_lock:
            if _qs_cache.get("status") == "running":
                return
            _qs_cache["status"] = "running"

        # Inject OI price into snap if spot was 0
        if not snap.get("spot"):
            snap = dict(snap)
            snap["spot"] = spot

        prompt = _build_qs_prompt(snap)
        raw = _try_claude_cli(prompt, timeout=60, feature_key="")

        if not raw:
            with _qs_lock:
                _qs_cache.update({"status": "no_cli", "text": "",
                                  "error": "Claude CLI not found — run: npm install -g @anthropic-ai/claude-code",
                                  "ts": datetime.now().isoformat(timespec="seconds")})
            return

        with _qs_lock:
            _qs_cache.update({"text": raw.strip(), "status": "ok", "error": "",
                              "ts": datetime.now().isoformat(timespec="seconds")})

    except Exception as exc:
        print(f"[qs_ai] ERROR: {exc}")
        with _qs_lock:
            _qs_cache.update({"status": "error", "text": "",
                              "error": str(exc),
                              "ts": datetime.now().isoformat(timespec="seconds")})


def generate_mb_ai(snap: dict) -> None:
    """Call Claude CLI to generate comprehensive dual (intraday + long-term) summary."""
    global _mb_ai_cache
    if not _features.get("mb_ai"): return

    with _mb_ai_lock:
        _mb_ai_cache["status"] = "running"

    if not snap or not snap.get("spot"):
        with _mb_ai_lock:
            _mb_ai_cache.update({"status": "no_data", "error": "No live bot data yet — start the bots first",
                                  "ts": datetime.now().isoformat(timespec="seconds")})
        return

    prompt, n_lines = _build_mb_ai_prompt(snap)
    raw = _try_claude_cli(prompt, timeout=60, feature_key="mb_ai")

    if not _features.get("mb_ai"): return  # was disabled while running

    if not raw:
        with _mb_ai_lock:
            _mb_ai_cache.update({"status": "no_cli",
                                  "error": "Claude CLI not found — install with: npm install -g @anthropic-ai/claude-code",
                                  "ts": datetime.now().isoformat(timespec="seconds")})
        return

    def _extract(label: str, next_labels: list) -> str:
        pattern = _re.escape(label) + r'\s*(.*?)(?=' + '|'.join(_re.escape(l) for l in next_labels) + r'|$)'
        m = _re.search(pattern, raw, _re.DOTALL)
        return m.group(1).strip() if m else ""

    ALL = ["📊 INTRADAY VIEW", "📈 LONG-TERM VIEW", "⚡ KEY LEVELS", "⚠️ RISKS", "💡 BOTTOM LINE"]
    intraday   = _extract("📊 INTRADAY VIEW (Next 1-2 hours):", ALL[1:])
    longterm   = _extract("📈 LONG-TERM VIEW (Positional/Swing — 2-5 days):", ALL[2:])
    key_levels = _extract("⚡ KEY LEVELS TO WATCH:", ALL[3:])
    risks      = _extract("⚠️ RISKS:", ALL[4:])
    bottom     = _extract("💡 BOTTOM LINE:", [])

    # Fallback: model deviated from format — store full response as intraday
    if not intraday and not longterm:
        intraday = raw

    with _mb_ai_lock:
        _mb_ai_cache.update({
            "intraday":    intraday,
            "longterm":    longterm,
            "key_levels":  key_levels,
            "risks":       risks,
            "bottom_line": bottom,
            "ts":          datetime.now().isoformat(timespec="seconds"),
            "status":      "ok",
            "error":       "",
            "context_lines": n_lines,
        })

# ─────────────────────────────────────────────────────────────
#  DECISION ENGINE (trading_decision_engine) COLLECTOR
# ─────────────────────────────────────────────────────────────
_DE_DIR  = os.path.join(BASE, "trading_decision_engine")
_DE_LOGS = os.path.join(_DE_DIR, "logs")
# Incremental tail-follow state: the events JSONL grows all session (every cycle logs a
# full diagnostics object), so we keep a byte offset + running counters and read only
# what's new on each refresh instead of re-parsing the whole file every 15s.
_de_state: dict = {"file": None, "offset": 0, "cycles": 0, "actions": {}, "gates": {},
                   "eng": {}, "latest": None, "trades": [], "mode": None, "partial": False}

def read_decision_engine() -> dict:
    import glob as _g
    out: dict = {"available": False}

    # Strategy config + profiles (small files, re-read each refresh so live edits show)
    try:
        with open(os.path.join(_DE_DIR, "config", "strategy.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        prof_dir = os.path.join(_DE_DIR, "config", "profiles")
        out["profiles"] = sorted(p[:-5] for p in os.listdir(prof_dir) if p.endswith(".json")) if os.path.isdir(prof_dir) else []
        out["config"] = {k: cfg.get(k) for k in (
            "active_profile", "trend_threshold", "decision_score_threshold", "min_resistance_distance",
            "momentum_threshold", "premium_velocity_scale", "signal_stability_min_seconds",
            "signal_stability_max_seconds", "max_trades_per_day", "cooldown_seconds",
            "daily_loss_limit", "daily_profit_lock")}
    except Exception:
        out["config"], out["profiles"] = {}, []

    files = sorted(_g.glob(os.path.join(_DE_LOGS, "events_*.jsonl")))
    if not files:
        out.update(_engine_running())
        return out
    path = files[-1]
    st = _de_state
    if st["file"] != path:  # new session/day file -> reset counters
        st.update({"file": path, "offset": 0, "cycles": 0, "actions": {}, "gates": {},
                   "eng": {}, "latest": None, "trades": [], "mode": None, "partial": False})
    try:
        size = os.path.getsize(path)
        if size < st["offset"]:
            st["offset"] = 0  # file replaced/truncated
        if size > st["offset"]:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if st["offset"] == 0 and size > 30_000_000:
                    # Dashboard started mid-session against a huge file: only the last
                    # ~10MB is aggregated (stats flagged partial) — never block refresh.
                    fh.seek(size - 10_000_000); fh.readline(); st["partial"] = True
                else:
                    fh.seek(st["offset"])
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    typ = ev.get("event")
                    if typ in ("decision", "rejected"):
                        st["cycles"] += 1
                        st["mode"] = ev.get("mode") or st["mode"]
                        act = ev.get("action", "?")
                        st["actions"][act] = st["actions"].get(act, 0) + 1
                        diag = ev.get("diagnostics")
                        if diag:
                            st["latest"] = diag
                            for gate in diag.get("stage1", {}).get("failed_checks", []):
                                st["gates"][gate] = st["gates"].get(gate, 0) + 1
                            for name, info in diag.get("engines", {}).items():
                                agg = st["eng"].setdefault(name, [0, 0, 0.0])  # [passes, samples, score_sum]
                                agg[1] += 1
                                agg[2] += info.get("score", 0.0)
                                agg[0] += 1 if info.get("passed") else 0
                    elif typ in ("trade_opened", "trade_closed"):
                        st["trades"].append(ev)
                        del st["trades"][:-30]
                st["offset"] = fh.tell()
    except Exception:
        pass

    total_rej = sum(st["gates"].values())
    out.update(_engine_running())
    out.update({
        "available": st["latest"] is not None or st["cycles"] > 0 or bool(st["trades"]),
        "file": os.path.basename(path),
        "mode": st["mode"],
        "partial": st["partial"],
        "age_seconds": round(max(0.0, time.time() - os.path.getmtime(path)), 1),
        "latest": st["latest"],
        "stats": {
            "cycles": st["cycles"],
            "actions": dict(st["actions"]),
            "rejections": [
                {"gate": g, "count": c, "pct": round(c / total_rej * 100.0, 1)}
                for g, c in sorted(st["gates"].items(), key=lambda kv: -kv[1])
            ] if total_rej else [],
            "engines": {
                name: {"pass_pct": round(p / max(1, n) * 100.0, 1), "avg_score": round(s / max(1, n), 1), "samples": n}
                for name, (p, n, s) in sorted(st["eng"].items())
            },
        },
        "trades": st["trades"][::-1][:10],
    })
    return out

# ─────────────────────────────────────────────────────────────
#  DATA REFRESH LOOP
# ─────────────────────────────────────────────────────────────
_lock     = threading.Lock()
_snapshot: dict = {}

def _refresh() -> None:
    global _snapshot
    master = read_master();  fibo   = read_fibo()
    csig   = read_chart_signal(); cdec = read_chart_decision()
    prem   = read_premium(); trade  = read_trade_bot()
    momentum_bot = read_momentum_bot()
    trendline_bot = read_trendline_bot()
    sigmon = read_signal_monitor(); live_chain = read_live_chain()
    today_pnl = read_today_pnl()
    margin    = read_margin()
    mkt_idx   = read_market_indices()
    orders    = read_today_orders()
    with _ltp_result_lock: ltp_result = dict(_ltp_result)
    cons   = build_consensus(master, fibo, csig, sigmon)
    # Pick spot — always prefer the live Groww LTP (3s index poller), bot logs only as fallback
    def _best_spot():
        idx = (master.get("index") or fibo.get("index") or "NIFTY").lower()
        try:
            live = float((mkt_idx.get(idx) or {}).get("last") or 0)
        except (TypeError, ValueError):
            live = 0.0
        if live > 0: return live
        candidates = [
            (cdec,   cdec.get("spot",   0)),
            (csig,   csig.get("spot",   0)),
            (master, master.get("spot", 0)),
            (fibo,   fibo.get("spot",   0)),
        ]
        # prefer live sources first
        for src, val in candidates:
            if val and src.get("_live"): return val
        # fallback to any non-zero value
        for _, val in candidates:
            if val: return val
        return 0

    oi_snap = read_oi_snapshot()

    snap = {
        "ts":    datetime.now().isoformat(timespec="seconds"),
        "index": master.get("index") or fibo.get("index") or "NIFTY",
        "spot":  _best_spot(),
        "bots":  {"master": master, "fibo": fibo, "chart_signal": csig,
                  "chart_decision": cdec, "premium": prem,
                  "trade": trade, "momentum_bot": momentum_bot,
                  "trendline_bot": trendline_bot,
                  "signal_monitor": sigmon},
        "live_chain": live_chain,
        "live_option_ltp": ltp_result,
        "consensus": cons,
        "ai_summary":  dict(_ai_summary),
        "scalp_plan":  dict(_scalp_plan),
        "oi_ai":       dict(_oi_ai),
        "features":    dict(_features),
        "mins_to_close": _mins_to_close(),
        "pnl_today":    today_pnl,
        "margin":       margin,
        "orders":       orders,
        "mkt_idx":      mkt_idx,
        "pnl_analysis": dict(_ptai_analysis),
        "pnl_ai":       dict(_ptai_ai),
        "ptai_ok":      _ptai_ok,
        "oi_snapshot":  oi_snap,
        "oi_history":   list(_oi_history),
        "vix_history":  list(_vix_history),
        "vix_session_open": _vix_session_open[0],
        "mb_ai":           dict(_mb_ai_cache),
        "decision_engine": read_decision_engine(),
    }
    with _lock: _snapshot = snap

def _loop():
    _now = time.time()
    # qs_ai: start after 10s so the snapshot has time to populate
    _last: dict = {"ai": 0.0, "scalp": 0.0, "ptai": 0.0, "ptai_ai": 0.0, "oi_ai": 0.0, "mb_ai": 0.0,
                   "qs_ai": _now - QS_AI_REFRESH_SECS + 10}
    while True:
        try:
            _refresh()
            now = time.time()
            with _lock: snap_copy = dict(_snapshot)

            if _features.get("ai") and (now - _last["ai"]) >= AI_REFRESH_SECS:
                _last["ai"] = now
                threading.Thread(target=generate_ai_summary,
                                 args=(snap_copy,), daemon=True).start()

            if _features.get("scalp") and (now - _last["scalp"]) >= SCALP_REFRESH_SECS:
                _last["scalp"] = now
                threading.Thread(target=generate_scalp_plan,
                                 args=(snap_copy,), daemon=True).start()

            if (now - _last["ptai"]) >= PTAI_ANALYSIS_REFRESH:
                _last["ptai"] = now
                threading.Thread(target=_run_ptai_analysis, daemon=True).start()

            if _features.get("ptai_ai") and (now - _last["ptai_ai"]) >= PTAI_AI_REFRESH:
                _last["ptai_ai"] = now
                threading.Thread(target=_run_ptai_ai, daemon=True).start()

            if _features.get("oi_ai") and (now - _last["oi_ai"]) >= OI_AI_REFRESH_SECS:
                _last["oi_ai"] = now
                threading.Thread(target=generate_oi_summary,
                                 args=(snap_copy,), daemon=True).start()

            if _features.get("mb_ai") and (now - _last["mb_ai"]) >= MB_AI_REFRESH_SECS:
                _last["mb_ai"] = now
                threading.Thread(target=generate_mb_ai,
                                 args=(snap_copy,), daemon=True).start()

            # Quick Summary: auto-refreshes every 5 min when feature is ON
            if _features.get("qs_ai") and (now - _last["qs_ai"]) >= QS_AI_REFRESH_SECS:
                with _qs_lock:
                    qs_running = _qs_cache.get("status") == "running"
                if not qs_running:
                    _last["qs_ai"] = now
                    threading.Thread(target=generate_qs_ai,
                                     args=(snap_copy,), daemon=True).start()

        except Exception as e:
            print(f"[refresh] {e}")
        time.sleep(REFRESH_SEC)

# ─────────────────────────────────────────────────────────────
#  HTML
# ─────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NIFTY Live Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ── Theme variables (all editable via color picker) ── */
:root{
  /* Backgrounds */
  --bg:      #070b14;
  --bg2:     #0c1220;
  --bg3:     #131c30;
  --hdr-bg:  #080f1e;
  /* Signals */
  --bull:    #00e5a0;  --bull-rgb: 0,229,160;
  --bull2:   #001a10;
  --bear:    #ff4d6d;  --bear-rgb: 255,77,109;
  --bear2:   #1a0010;
  --warn:    #ffc107;  --warn-rgb: 255,193,7;
  --info:    #38bdf8;  --info-rgb: 56,189,248;
  /* Text */
  --txt:     #e2e8f0;
  --dim:     #5a7298;
  /* UI */
  --bdr:     #1c2d48;
  --accent:  #a855f7;
  /* Trade Board buy buttons (main + dark shade for gradient) */
  --buy-ce:      #00e5a0;
  --buy-ce-dark: #00875e;
  --buy-pe:      #ff4d6d;
  --buy-pe-dark: #be1a3c;
  /* Glow intensity (0.0 – 1.0) — controlled by glow slider */
  --glow-a:  0.12;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--txt);font-family:'Inter','Courier New',sans-serif;font-size:13px;min-height:100vh;}
::-webkit-scrollbar{width:4px;} ::-webkit-scrollbar-track{background:var(--bg);} ::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:2px;}

/* ── Header ── */
.hdr{
  background:linear-gradient(135deg,var(--hdr-bg) 0%,#0b1428 60%,var(--hdr-bg) 100%);
  border-bottom:1px solid var(--bdr);
  padding:12px 20px;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;
  position:relative;
}
.hdr::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent 0%,var(--info) 40%,var(--accent) 60%,transparent 100%);}
.hdr-left{display:flex;align-items:baseline;gap:16px;}
.hdr-title{color:var(--info);font-size:10px;font-weight:700;letter-spacing:3px;text-transform:uppercase;opacity:.8;}
/* Index cards in header */
.idx-cards{display:flex;gap:0;align-items:stretch;}
.idx-card{display:flex;flex-direction:column;justify-content:center;cursor:pointer;
          padding:6px 18px;border-left:1px solid rgba(255,255,255,.08);min-width:130px;
          border-bottom:2px solid transparent;transition:border-color .2s,background .2s;}
.idx-card:first-child{border-left:none;padding-left:0;}
.idx-card:hover{background:rgba(255,255,255,.03);}
.idx-card.primary{border-bottom-color:var(--info);}
.idx-card-header{display:flex;align-items:center;margin-bottom:3px;}
.idx-card-name{font-size:9px;color:var(--dim);letter-spacing:.8px;font-weight:700;text-transform:uppercase;}
.idx-card.primary .idx-card-name{color:var(--info);}
.idx-card-price{font-family:'JetBrains Mono',monospace;font-weight:700;color:#fff;
                font-size:24px;line-height:1.1;letter-spacing:-0.5px;}
.idx-card-chg{font-size:10px;font-weight:600;font-family:'JetBrains Mono',monospace;margin-top:2px;}
.hdr-spot{font-family:'JetBrains Mono',monospace;font-size:30px;font-weight:700;color:#fff;letter-spacing:-1px;
  text-shadow:0 0 24px rgba(56,189,248,.25);}
.hdr-r{display:flex;gap:10px;align-items:center;font-size:11px;color:var(--dim);}
#countdown{color:var(--warn);font-weight:700;font-size:13px;font-family:'JetBrains Mono',monospace;}

/* ── Bot status bar ── */
.bbar{background:#060a12;border-bottom:1px solid var(--bdr);padding:5px 20px;display:flex;gap:6px;flex-wrap:nowrap;align-items:center;overflow-x:auto;scrollbar-width:none;}
.bbar::-webkit-scrollbar{display:none;}
.badge{display:flex;align-items:center;gap:4px;padding:2px 8px;border-radius:20px;border:1px solid var(--bdr);
       background:var(--bg3);font-size:10px;font-weight:500;letter-spacing:.3px;transition:all .3s;white-space:nowrap;}
.badge.live  {border-color:var(--bull);color:var(--bull);box-shadow:0 0 8px rgba(0,229,160,.12);}
.badge.stale {border-color:var(--warn);color:var(--warn);}
.badge.off   {border-color:var(--bear);color:var(--bear);}
.dot{width:6px;height:6px;border-radius:50%;}
.dlive {background:var(--bull);box-shadow:0 0 6px var(--bull);animation:pulse 2s infinite;}
.dstale{background:var(--warn);}
.doff  {background:var(--bear);}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* ── Layout ── */
.main{padding:12px 18px;display:grid;gap:10px;}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;}
@media(max-width:960px){.g2,.g3{grid-template-columns:1fr;}}

/* ── Consensus ── */
.cons{
  border-radius:12px;border:1px solid;padding:16px 20px;
  display:grid;grid-template-columns:210px 1fr auto;gap:14px;align-items:center;
  position:relative;overflow:hidden;
}
.cons::before{content:'';position:absolute;inset:0;
  background:repeating-linear-gradient(45deg,rgba(255,255,255,.01),rgba(255,255,255,.01) 1px,transparent 1px,transparent 14px);}
.cons.strong-bull{border-color:var(--bull);background:var(--bull2);box-shadow:0 0 40px rgba(var(--bull-rgb),var(--glow-a));}
.cons.bull       {border-color:var(--bull);background:var(--bull2);box-shadow:0 0 20px rgba(var(--bull-rgb),calc(var(--glow-a)*.6));}
.cons.strong-bear{border-color:var(--bear);background:var(--bear2);box-shadow:0 0 40px rgba(var(--bear-rgb),var(--glow-a));}
.cons.bear       {border-color:var(--bear);background:var(--bear2);box-shadow:0 0 20px rgba(var(--bear-rgb),calc(var(--glow-a)*.6));}
.cons.neutral    {border-color:var(--warn);background:#0f0a00;box-shadow:0 0 20px rgba(var(--warn-rgb),calc(var(--glow-a)*.5));}
.csig{font-size:30px;font-weight:800;letter-spacing:2px;font-family:'Inter',sans-serif;}
.strong-bull .csig,.bull .csig{color:var(--bull);text-shadow:0 0 20px rgba(0,229,160,.5);}
.strong-bear .csig,.bear .csig{color:var(--bear);text-shadow:0 0 20px rgba(255,77,109,.5);}
.neutral .csig{color:var(--warn);}
.csmry{font-size:12px;margin-bottom:4px;opacity:.9;}
.csrc{font-size:10px;color:var(--dim);letter-spacing:.3px;}
.cscores{font-size:12px;text-align:right;}

/* ── Cards ── */
.card{background:var(--bg2);border:1px solid var(--bdr);border-radius:10px;padding:14px;transition:border-color .3s;}
.card:hover{border-color:#2a4060;}
.card.ce-bdr{border:2px solid var(--bull);box-shadow:0 0 18px rgba(0,229,160,.08);}
.card.pe-bdr{border:2px solid var(--bear);box-shadow:0 0 18px rgba(255,77,109,.08);}
.ctitle{font-size:10px;letter-spacing:1.5px;color:var(--dim);text-transform:uppercase;
        border-bottom:1px solid var(--bdr);padding-bottom:8px;margin-bottom:12px;
        display:flex;justify-content:space-between;font-weight:600;align-items:center;}
.age{font-size:10px;color:var(--dim);}
.chart-tip{font-size:10px;color:var(--dim);background:rgba(56,189,248,.06);border:1px solid rgba(56,189,248,.15);
           border-radius:5px;padding:5px 9px;margin-bottom:10px;line-height:1.5;}
.ctip-wrap{font-size:10.5px;background:rgba(56,189,248,.05);border:1px solid rgba(56,189,248,.13);
           border-radius:6px;padding:8px 10px;margin-bottom:10px;line-height:1.7;font-family:'JetBrains Mono','Courier New',monospace;}
.ctip-title{font-size:10px;font-weight:700;letter-spacing:1.2px;color:var(--warn);text-transform:uppercase;margin-bottom:4px;}
.ctip-sub{color:var(--dim);font-size:9.5px;margin-bottom:6px;}
.ctip-block{border-left:2px solid rgba(56,189,248,.3);padding-left:8px;margin-bottom:7px;}
.ctip-num{color:var(--info);font-weight:700;}

/* ── Rows ── */
.row{display:flex;justify-content:space-between;margin-bottom:6px;gap:8px;align-items:center;}
.lbl{color:var(--dim);font-size:11px;}
.v{font-weight:600;font-family:'JetBrains Mono',monospace;}
.vbull{color:var(--bull);font-weight:700;} .vbear{color:var(--bear);font-weight:700;}
.vwarn{color:var(--warn);font-weight:700;} .vinfo{color:var(--info);} .vdim{color:var(--dim);}

/* ── Score chips ── */
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0;}
.chip{padding:3px 10px;border-radius:20px;font-size:10px;font-weight:600;border:1px solid;letter-spacing:.5px;}
.cup{color:var(--bull);border-color:var(--bull);background:rgba(0,229,160,.08);}
.cdn{color:var(--bear);border-color:var(--bear);background:rgba(255,77,109,.08);}
.cfl{color:var(--dim);border-color:var(--bdr);}

/* ── Levels table ── */
.ltbl{width:100%;border-collapse:collapse;}
.ltbl th{color:var(--dim);font-size:10px;padding:4px 6px;border-bottom:1px solid var(--bdr);
         text-align:right;font-weight:600;letter-spacing:.5px;}
.ltbl th:first-child{text-align:left;}
.ltbl td{padding:5px 6px;border-bottom:1px solid #0a1020;font-size:12px;text-align:right;
         font-family:'JetBrains Mono',monospace;transition:background .2s;}
.ltbl td:first-child{text-align:left;font-family:'Inter',sans-serif;font-size:11px;}
.ltbl tr:hover td{background:#0d1828;}
.ltbl .ar{border-left:2px solid rgba(255,77,109,.35);}
.ltbl .ar td:first-child{color:var(--bear);}
.ltbl .br{border-left:2px solid rgba(0,229,160,.35);}
.ltbl .br td:first-child{color:var(--bull);}
.ltbl .srow td{background:linear-gradient(90deg,#050f1a,#081528,#050f1a);
               color:var(--info);text-align:center;font-weight:700;letter-spacing:1px;padding:7px;border:none;}
.s3{color:#ffd700;text-shadow:0 0 6px rgba(255,215,0,.4);}
.s2{color:#94a3b8;}
/* Only blink when price is within 6pts of a level (truly AT it) */
.atl{color:var(--bear);font-weight:700;animation:pulse 1.5s infinite;}

/* ── Star tooltip ── */
.star-cell{position:relative;cursor:help;}
.star-cell .stip{
  display:none;position:absolute;bottom:calc(100% + 6px);right:0;
  background:#0c1a30;border:1px solid var(--bdr);border-radius:8px;
  padding:10px 13px;min-width:220px;z-index:200;
  font-size:11px;line-height:1.7;color:var(--txt);white-space:normal;
  box-shadow:0 8px 24px rgba(0,0,0,.6);font-family:'Inter',sans-serif;
}
.star-cell:hover .stip{display:block;}
.stip-title{font-size:10px;letter-spacing:1px;text-transform:uppercase;
            color:var(--info);font-weight:700;margin-bottom:6px;}
.stip-row{display:flex;gap:8px;align-items:baseline;margin-bottom:3px;}
.stip-star{color:#ffd700;min-width:55px;font-size:12px;}
.stip-def{color:var(--dim);}

/* ── Tabs ── */
.tabbar{display:flex;flex-wrap:wrap;gap:0;background:#050910;border-bottom:1px solid var(--bdr);padding:0 18px;}
.tab-btn{
  padding:9px 18px;font-size:11px;font-weight:600;letter-spacing:.5px;
  border:none;background:none;cursor:pointer;color:var(--dim);
  border-bottom:2px solid transparent;margin-bottom:-1px;
  font-family:'Inter',sans-serif;transition:all .2s;
}
.tab-btn:hover{color:var(--txt);}
.tab-btn.active{color:var(--info);border-bottom-color:var(--info);}
.tab-pane{display:none!important;} .tab-pane.active{display:block!important;}
#tab-trade.active{display:flex!important;}

/* ── Guide tab ── */
.guide{padding:20px 22px;max-width:1200px;}
.guide-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;}
@media(max-width:800px){.guide-grid{grid-template-columns:1fr;}}
.gcard{background:var(--bg2);border:1px solid var(--bdr);border-radius:10px;padding:16px;}
.gcard-title{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700;
             color:var(--info);margin-bottom:12px;font-family:'Inter',sans-serif;
             display:flex;align-items:center;gap:8px;}
.grow{display:flex;gap:10px;margin-bottom:8px;align-items:baseline;}
.gtag{font-size:11px;font-weight:700;min-width:80px;flex-shrink:0;font-family:'JetBrains Mono',monospace;}
.gtag.bull{color:var(--bull);} .gtag.bear{color:var(--bear);}
.gtag.warn{color:var(--warn);} .gtag.info{color:var(--info);}
.gtag.dim {color:var(--dim);}  .gtag.acc {color:var(--accent);}
.gdesc{font-size:12px;color:var(--dim);line-height:1.6;}
.gdesc b{color:var(--txt);}
.gdivider{border:none;border-top:1px solid var(--bdr);margin:10px 0;}
.gstar-row{display:flex;gap:10px;margin-bottom:7px;align-items:center;}
.gstar-val{color:#ffd700;min-width:70px;font-size:14px;}
.gstar-meaning{font-size:12px;color:var(--dim);} .gstar-meaning b{color:var(--txt);}
.gchip{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;
       font-weight:600;border:1px solid;margin:2px;font-family:'Inter',sans-serif;}
.gchip.ce{color:var(--bull);border-color:var(--bull);}
.gchip.pe{color:var(--bear);border-color:var(--bear);}
.gchip.w {color:var(--warn);border-color:var(--warn);}

/* ── Glow slider in picker ── */
.pk-slider-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px;gap:10px;}
.pk-slider-row label{font-size:11px;color:var(--dim);flex:1;font-family:'Inter',sans-serif;}
.pk-slider-wrap{display:flex;align-items:center;gap:8px;}
.pk-slider-val{font-size:10px;color:var(--dim);width:28px;text-align:right;font-family:'JetBrains Mono',monospace;}
input[type=range]{
  width:90px;height:4px;border-radius:2px;background:var(--bdr);
  outline:none;cursor:pointer;-webkit-appearance:none;
}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;width:12px;height:12px;border-radius:50%;
  background:var(--info);cursor:pointer;
}

/* ── Option card ── */
.odir{font-size:22px;font-weight:800;margin-bottom:10px;font-family:'Inter',sans-serif;letter-spacing:1px;}
.odir.ce{color:var(--bull);text-shadow:0 0 15px rgba(0,229,160,.4);}
.odir.pe{color:var(--bear);text-shadow:0 0 15px rgba(255,77,109,.4);}

/* ── Active trade ── */
.atrade{background:#0f0900;border:1px solid var(--warn);border-radius:6px;padding:10px;margin-top:8px;
        box-shadow:0 0 14px rgba(255,193,7,.06);}

/* ── Premium flow ── */
.fup{color:var(--bull);} .fdn{color:var(--bear);} .fst{color:var(--dim);}

/* ── Footer ── */
.footer{text-align:center;color:var(--dim);font-size:10px;padding:12px;
        border-top:1px solid var(--bdr);margin-top:6px;letter-spacing:.5px;}
.offline-warn{color:var(--bear);font-size:12px;}

/* ── Scalp Plan ── */
#scalp-box{
  border-radius:12px;padding:14px 20px;
  display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;
  border:1px solid rgba(56,189,248,.3);
  background:linear-gradient(135deg,#030c16 0%,#050f1e 100%);
  box-shadow:0 0 30px rgba(56,189,248,.07),inset 0 1px 0 rgba(255,255,255,.03);
  position:relative;overflow:hidden;
}
#scalp-box::after{content:'⚡';position:absolute;right:16px;top:50%;transform:translateY(-50%);
  font-size:72px;opacity:.04;pointer-events:none;}
.scalp-label{font-size:10px;color:var(--info);letter-spacing:2px;text-transform:uppercase;
             white-space:nowrap;display:flex;align-items:center;gap:8px;font-weight:700;}
.scalp-text{font-size:15px;font-weight:700;flex:1;letter-spacing:.3px;font-family:'JetBrains Mono',monospace;}
.scalp-ce  {color:var(--bull);text-shadow:0 0 12px rgba(0,229,160,.4);}
.scalp-pe  {color:var(--bear);text-shadow:0 0 12px rgba(255,77,109,.4);}
.scalp-wait{color:var(--warn);}
.scalp-dim {color:var(--dim);font-size:12px;}
.scalp-ts  {font-size:10px;color:#2a3f5f;white-space:nowrap;}
.pulse{animation:pulse 2s infinite;}

/* ── Toggles ── */
.toggle-btn{font-size:10px;padding:2px 9px;border-radius:20px;cursor:pointer;border:1px solid;
            background:none;letter-spacing:.8px;font-weight:600;transition:all .2s;}
.toggle-on {border-color:var(--bull);color:var(--bull);}
.toggle-off{border-color:var(--dim);color:var(--dim);}

/* ── Header extras ── */
.mtc{font-size:11px;padding:3px 10px;border-radius:20px;border:1px solid;font-weight:700;font-family:'JetBrains Mono',monospace;}
.mtc-ok   {border-color:var(--bull);color:var(--bull);}
.mtc-warn {border-color:var(--warn);color:var(--warn);}
.mtc-close{border-color:var(--bear);color:var(--bear);animation:pulse 1s infinite;}

/* ── AI Summary ── */
#ai-card{border:1px solid rgba(168,85,247,.4);background:linear-gradient(135deg,#07041a 0%,#0a0620 100%);
         box-shadow:0 0 28px rgba(168,85,247,.06);}
.ai-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;}
.ai-source{font-size:10px;color:#c084fc;padding:2px 9px;border:1px solid #7c3aed;border-radius:20px;font-weight:600;}
.ai-spinner{color:#a855f7;animation:pulse 1.5s infinite;}
.ai-text{font-size:12px;line-height:1.8;white-space:pre-wrap;color:#cbd5e1;font-family:'Inter',sans-serif;}
.ai-text .sit{color:var(--info);font-weight:700;}
.ai-text .act-ce{color:var(--bull);font-weight:700;}
.ai-text .act-pe{color:var(--bear);font-weight:700;}
.ai-text .act-wait{color:var(--warn);font-weight:700;}
.ai-text .risk{color:var(--warn);}
.ai-no-sub{text-align:center;padding:20px 10px;}
.ai-no-sub .title{color:#c084fc;font-size:15px;font-weight:700;margin-bottom:10px;font-family:'Inter',sans-serif;}
.ai-no-sub .msg{color:var(--dim);font-size:12px;margin-bottom:16px;line-height:1.7;}
.ai-no-sub .plans{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;}
.plan-card{border:1px solid #4c1d95;border-radius:8px;padding:12px 16px;text-align:center;
           background:#0b0818;min-width:160px;transition:border-color .2s;}
.plan-card:hover{border-color:#7c3aed;}
.plan-card .plan-name {color:#c084fc;font-weight:700;margin-bottom:5px;font-family:'Inter',sans-serif;}
.plan-card .plan-price{color:var(--bull);font-size:12px;margin-bottom:5px;font-family:'JetBrains Mono',monospace;}
.plan-card .plan-note {color:var(--dim);font-size:10px;line-height:1.5;}
.ai-error{color:var(--warn);font-size:12px;padding:10px 14px;border:1px solid #78350f;border-radius:6px;background:#0c0800;}

/* ── AI Brain Tab ── */
#tab-aibrain{padding:16px 18px;overflow-y:auto;max-height:calc(100vh - 118px);}
.mb-ai-wrap{max-width:960px;margin:0 auto;}
.mb-ai-toggle-bar{display:flex;align-items:center;gap:14px;margin-bottom:18px;
  background:linear-gradient(135deg,#0c0820,#110a1e);border:1px solid #4c1d95;
  border-radius:12px;padding:16px 20px;}
.mb-ai-toggle-bar .mb-ai-title{font-size:18px;font-weight:800;color:#c084fc;flex:1;font-family:'Inter',sans-serif;}
.mb-ai-toggle-bar .mb-ai-subtitle{font-size:11px;color:var(--dim);margin-top:3px;}
.mb-ai-on{background:linear-gradient(135deg,#7c3aed,#4c1d95)!important;color:#fff!important;
  border-color:#7c3aed!important;box-shadow:0 0 18px rgba(124,58,237,.4)!important;}
.mb-ai-meta{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;}
.mb-ai-ts{font-size:11px;color:var(--dim);}
.mb-ai-ctx{font-size:10px;color:#7c3aed;padding:2px 8px;border:1px solid #4c1d95;border-radius:10px;}
.mb-ai-card{background:var(--bg2);border:1px solid var(--bdr);border-radius:12px;
  padding:18px;margin-bottom:14px;transition:border-color .3s;}
.mb-ai-card:hover{border-color:#2a4060;}
.mb-ai-card.intraday-card{border-left:3px solid var(--info);}
.mb-ai-card.longterm-card{border-left:3px solid var(--bull);}
.mb-ai-card.levels-card{border-left:3px solid var(--accent);}
.mb-ai-card.risks-card{border-left:3px solid var(--warn);}
.mb-ai-card.bottom-card{border-left:3px solid #f97316;background:linear-gradient(135deg,#0c1220,#0c0a06);}
.mb-ai-card-title{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
  margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--bdr);}
.intraday-card .mb-ai-card-title{color:var(--info);}
.longterm-card .mb-ai-card-title{color:var(--bull);}
.levels-card .mb-ai-card-title{color:var(--accent);}
.risks-card .mb-ai-card-title{color:var(--warn);}
.bottom-card .mb-ai-card-title{color:#f97316;}
.mb-ai-body{font-size:13px;line-height:1.8;color:#cbd5e1;white-space:pre-wrap;font-family:'Inter',sans-serif;}
.bottom-card .mb-ai-body{font-size:14px;font-weight:700;color:#fff;line-height:1.6;}
.mb-ai-idle{text-align:center;padding:40px 20px;color:var(--dim);}
.mb-ai-idle .idle-icon{font-size:40px;margin-bottom:14px;}
.mb-ai-idle .idle-msg{font-size:14px;margin-bottom:6px;color:var(--txt);}
.mb-ai-idle .idle-sub{font-size:12px;color:var(--dim);line-height:1.6;}
.mb-ai-loading{display:flex;align-items:center;gap:12px;padding:30px 20px;
  background:var(--bg2);border:1px solid var(--bdr);border-radius:12px;margin-bottom:14px;}
.mb-ai-spinner{width:22px;height:22px;border:3px solid #4c1d95;border-top-color:#c084fc;
  border-radius:50%;animation:mb-spin 0.8s linear infinite;}
@keyframes mb-spin{to{transform:rotate(360deg)}}
.mb-ai-loading-text{color:#c084fc;font-size:13px;font-weight:600;}
.mb-ai-refresh-btn{background:rgba(124,58,237,.2);border:1px solid #7c3aed;color:#c084fc;
  border-radius:8px;padding:6px 16px;cursor:pointer;font-size:12px;font-weight:600;
  transition:all .2s;}
.mb-ai-refresh-btn:hover{background:rgba(124,58,237,.4);}
.mb-ai-refresh-btn:disabled{opacity:.4;cursor:not-allowed;}
.mb-ai-2col{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
@media(max-width:700px){.mb-ai-2col{grid-template-columns:1fr;}}
#qs-text{transition:opacity .3s;}
#qs-text b{color:#38bdf8;}

/* ── Color Picker Button ── */
#picker-btn{
  display:flex;align-items:center;gap:7px;background:none;
  border:1px solid var(--bdr);border-radius:20px;color:var(--dim);
  cursor:pointer;padding:4px 12px;transition:all .25s;
  font-size:11px;font-family:'Inter',sans-serif;font-weight:600;letter-spacing:.3px;
}
#picker-btn:hover{border-color:var(--info);color:var(--txt);box-shadow:0 0 10px rgba(56,189,248,.15);}
/* Conic swatch that auto-reflects current theme colors */
.swatch{
  display:inline-block;width:16px;height:16px;border-radius:50%;flex-shrink:0;
  background:conic-gradient(var(--bull) 0deg 90deg,var(--info) 90deg 180deg,var(--bear) 180deg 270deg,var(--warn) 270deg 360deg);
  border:1.5px solid rgba(255,255,255,.12);
  box-shadow:0 0 8px rgba(56,189,248,.25);
}

/* ── Color Picker Panel ── */
#picker-panel{
  position:fixed;top:60px;right:14px;
  background:linear-gradient(160deg,#0c1220 0%,#0a0f1c 100%);
  border:1px solid var(--bdr);border-radius:14px;padding:18px;
  z-index:9999;min-width:280px;max-height:82vh;overflow-y:auto;
  box-shadow:0 16px 48px rgba(0,0,0,.75),0 0 0 1px rgba(56,189,248,.06);
  display:none;
}
#picker-panel.open{display:block;}
#picker-panel::-webkit-scrollbar{width:3px;}
#picker-panel::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:2px;}

.pk-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
.pk-title{font-size:13px;color:var(--txt);font-weight:700;font-family:'Inter',sans-serif;
          display:flex;align-items:center;gap:8px;}
.pk-title .swatch{width:20px;height:20px;}
.pk-subtitle{font-size:9px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;margin-top:2px;}
.pk-section{
  font-size:9px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700;
  font-family:'Inter',sans-serif;padding:6px 0 4px;margin-top:10px;margin-bottom:4px;
  border-bottom:1px solid var(--bdr);display:flex;align-items:center;gap:6px;
}
.pk-section.bg  {color:#64748b;}
.pk-section.sig {color:var(--bull);}
.pk-section.txt {color:var(--info);}
.pk-section.ui  {color:var(--accent);}

.pk-row{display:flex;align-items:center;justify-content:space-between;padding:5px 0;gap:10px;}
.pk-row label{font-size:11px;color:var(--dim);flex:1;font-family:'Inter',sans-serif;cursor:pointer;}
.pk-swatch-wrap{position:relative;display:flex;align-items:center;gap:6px;}
.pk-hex{font-size:10px;color:var(--dim);font-family:'JetBrains Mono',monospace;width:52px;}
.pk-row input[type=color]{
  width:32px;height:22px;border:1px solid var(--bdr);border-radius:6px;
  background:none;cursor:pointer;padding:1px;transition:border-color .2s;
}
.pk-row input[type=color]:hover{border-color:var(--info);}
.pk-row input[type=color]::-webkit-color-swatch-wrapper{padding:0;}
.pk-row input[type=color]::-webkit-color-swatch{border:none;border-radius:4px;}

.pk-actions{display:flex;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid var(--bdr);}
.pk-reset{
  flex:1;padding:7px;border-radius:8px;cursor:pointer;font-size:11px;font-weight:600;
  border:1px solid var(--warn);color:var(--warn);background:none;
  font-family:'Inter',sans-serif;transition:all .2s;
}
.pk-reset:hover{background:rgba(255,193,7,.08);}
.pk-close{
  flex:1;padding:7px;border-radius:8px;cursor:pointer;font-size:11px;font-weight:600;
  border:1px solid var(--bdr);color:var(--dim);background:none;
  font-family:'Inter',sans-serif;transition:all .2s;
}
.pk-close:hover{border-color:var(--info);color:var(--txt);}
.pk-reset-one{
  background:none;border:none;cursor:pointer;color:var(--dim);
  font-size:13px;padding:2px 3px;border-radius:4px;line-height:1;
  transition:color .15s;flex-shrink:0;
}
.pk-reset-one:hover{color:var(--warn);}

/* ── PnL Tab ── */
#tab-pnl{padding:14px 18px;}
.th-filter-btn{font-size:9.5px;padding:2px 9px;border-radius:10px;border:1px solid var(--bdr);background:var(--bg3);color:var(--dim);cursor:pointer;transition:all .2s;}
.th-filter-btn.active{border-color:var(--info);color:var(--info);background:rgba(99,179,237,.1);}
#th-table tbody tr:hover{background:rgba(255,255,255,.03);}
#th-table tbody tr{border-bottom:1px solid rgba(255,255,255,.04);}
#th-table td{padding:6px 8px;vertical-align:middle;}
.pnl-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;}
.pnl-grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px;}
@media(max-width:960px){.pnl-grid,.pnl-grid3{grid-template-columns:1fr;}}
.pnl-big{font-size:42px;font-weight:800;letter-spacing:-1px;font-family:'JetBrains Mono',monospace;line-height:1;}
.pnl-pos{color:var(--bull);text-shadow:0 0 24px rgba(var(--bull-rgb),.4);}
.pnl-neg{color:var(--bear);text-shadow:0 0 24px rgba(var(--bear-rgb),.4);}
.pnl-zero{color:var(--warn);}
.pnl-bar-wrap{margin:10px 0 4px;background:var(--bg3);border-radius:4px;height:8px;overflow:hidden;}
.pnl-bar-fill{height:100%;border-radius:4px;transition:width .6s ease;}
.pnl-bar-pos{background:linear-gradient(90deg,var(--bull),rgba(0,229,160,.5));}
.pnl-bar-neg{background:linear-gradient(90deg,var(--bear),rgba(255,77,109,.5));}
.pnl-target-row{display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap;}
.pnl-target-row label{font-size:11px;color:var(--dim);}
.pnl-target-inp{
  background:var(--bg3);border:1px solid var(--bdr);border-radius:6px;
  color:var(--txt);font-size:12px;font-family:'JetBrains Mono',monospace;
  padding:3px 8px;width:90px;outline:none;
}
.pnl-target-inp:focus{border-color:var(--info);}
.pnl-alarm-btn{
  background:none;border:1px solid var(--bdr);border-radius:6px;cursor:pointer;
  color:var(--dim);font-size:13px;padding:3px 8px;transition:all .2s;
}
.pnl-alarm-btn.active{border-color:var(--warn);color:var(--warn);}
.pnl-alarm-btn:hover{border-color:var(--info);color:var(--txt);}
.target-hit{
  border:2px solid var(--bull)!important;
  box-shadow:0 0 30px rgba(var(--bull-rgb),.25)!important;
  animation:targetpulse 1.5s infinite;
}
@keyframes targetpulse{0%,100%{box-shadow:0 0 20px rgba(var(--bull-rgb),.2)}50%{box-shadow:0 0 40px rgba(var(--bull-rgb),.5)}}
.verdict-chip{
  display:inline-block;padding:4px 14px;border-radius:20px;font-size:11px;
  font-weight:700;border:1px solid;letter-spacing:.5px;font-family:'Inter',sans-serif;
}
.v-no-trade{color:var(--bear);border-color:var(--bear);background:rgba(255,77,109,.08);}
.v-caution  {color:var(--warn);border-color:var(--warn);background:rgba(255,193,7,.08);}
.v-normal   {color:var(--bull);border-color:var(--bull);background:rgba(0,229,160,.08);}
.v-high     {color:#a3e635;border-color:#a3e635;background:rgba(163,230,53,.08);}
.score-bar-wrap{margin:6px 0;background:var(--bg3);border-radius:4px;height:6px;overflow:hidden;position:relative;}
.score-bar{height:100%;border-radius:4px;transition:width .6s;}
/* tooltip on elements that have data-tip */
.has-tip{position:relative;cursor:help;z-index:0;}  /* z-index:0 needed for sibling comparison */
.has-tip:hover{z-index:100;}  /* lift above all siblings (z:0) so tip-box covers them */
.has-tip .tip-box{
  display:none;position:absolute;left:0;top:calc(100% + 6px);
  background:#0c1a30;border:1px solid var(--bdr);border-radius:8px;
  padding:10px 13px;min-width:240px;max-width:360px;z-index:500;
  font-size:11px;line-height:1.7;color:var(--txt);white-space:normal;
  box-shadow:0 8px 24px rgba(0,0,0,.7);font-family:'Inter',sans-serif;font-weight:400;
  pointer-events:none;
}
.has-tip:hover .tip-box{display:block;}
.tip-title{font-size:10px;letter-spacing:1px;text-transform:uppercase;
           font-weight:700;margin-bottom:6px;}
.tip-row{display:flex;gap:8px;margin-bottom:3px;align-items:baseline;}
.tip-range{min-width:60px;color:var(--info);font-family:'JetBrains Mono',monospace;font-size:10px;}
.tip-meaning{color:var(--dim);font-size:11px;}
.score-green{background:var(--bull);}
.score-yellow{background:var(--warn);}
.score-red{background:var(--bear);}
/* ── VIX Analysis card ── */
.vix-stat-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:6px;}
@media(max-width:900px){.vix-stat-row{grid-template-columns:repeat(3,1fr);}}
@media(max-width:600px){.vix-stat-row{grid-template-columns:repeat(2,1fr);}}
.vix-stat-box{background:var(--bg3);border:1px solid var(--bdr);border-radius:8px;padding:8px 10px;text-align:center;}
.vix-stat-label{font-size:9px;letter-spacing:.6px;text-transform:uppercase;color:var(--dim);margin-bottom:4px;}
.vix-stat-num{font-size:18px;font-weight:700;font-family:'JetBrains Mono',monospace;line-height:1.2;}
.vix-calm{color:#4ade80;}.vix-moderate{color:var(--warn);}.vix-elevated{color:#fb923c;}.vix-danger{color:var(--bear);}
.vix-alarm-on{border-color:var(--warn)!important;color:var(--warn)!important;}
.risk-item{padding:7px 10px;background:rgba(255,193,7,.05);border:1px solid rgba(255,193,7,.2);
           border-radius:6px;margin-bottom:6px;font-size:11px;line-height:1.6;}
.risk-type{font-weight:700;color:var(--warn);font-size:10px;letter-spacing:.5px;}
.insight-item{color:var(--dim);font-size:11px;padding:3px 0;border-bottom:1px solid var(--bdr);}
.sim-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #0a1020;font-size:11px;}
.pnl-trade-row{display:flex;gap:6px;align-items:center;padding:4px 0;
               border-bottom:1px solid #0a1020;font-size:11px;}
.pnl-trade-row .t-time{color:var(--dim);width:38px;flex-shrink:0;}
.pnl-trade-row .t-sym{color:var(--info);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.pnl-trade-row .t-pnl{font-family:'JetBrains Mono',monospace;font-weight:700;width:80px;text-align:right;}
.ptai-no-data{text-align:center;padding:30px 10px;color:var(--dim);font-size:12px;}
.ord-row{display:grid;grid-template-columns:1fr 60px 55px 55px 70px 60px;gap:4px 6px;
         padding:5px 0;border-bottom:1px solid #0a1020;font-size:11px;align-items:center;}
.ord-status{font-size:9px;font-weight:700;padding:2px 6px;border-radius:10px;text-align:center;letter-spacing:.3px;}
.os-complete{background:rgba(0,229,160,.1);color:var(--bull);border:1px solid var(--bull);}
.os-rejected{background:rgba(255,77,109,.1);color:var(--bear);border:1px solid var(--bear);}
.os-pending {background:rgba(255,193,7,.1); color:var(--warn);border:1px solid var(--warn);}
.os-cancelled{background:rgba(90,114,152,.1);color:var(--dim);border:1px solid var(--dim);}

/* ── Trade Board ── */
#tab-trade{flex-direction:column;height:calc(100vh - 118px);overflow:hidden;}
.tb-main{display:flex;flex-direction:row;flex:1;overflow:hidden;min-height:0;}
.tb-chain-side{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:120px;}
.tb-right-panel{display:flex;flex-direction:column;border-left:1px solid var(--bdr);overflow:hidden;min-width:240px;max-width:1100px;width:360px;}
/* Drag handle between chain and right panel */
.tb-drag-handle{
  width:5px;background:var(--bdr);cursor:col-resize;flex-shrink:0;
  transition:background .15s;position:relative;z-index:10;
}
.tb-drag-handle:hover{background:var(--info);}
.tb-drag-handle::after{content:'⋮';position:absolute;top:50%;left:50%;
  transform:translate(-50%,-50%);color:var(--dim);font-size:14px;pointer-events:none;}
/* Config bar — horizontal compact strip */
.tb-cbar{display:flex;flex-direction:column;gap:0;background:var(--bg2);
         border-bottom:1px solid var(--bdr);}
.tb-cbar-row{display:flex;align-items:flex-end;gap:10px;padding:6px 14px;flex-wrap:nowrap;
             overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--bdr) transparent;}
.tb-cbar-row::-webkit-scrollbar{height:3px;}
.tb-cbar-row::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:2px;}
.mb-vel-cons-badge{display:flex;flex-direction:column;gap:2px;min-width:54px;}
.mb-vel-cons-val{font-size:12px;font-weight:700;font-family:'JetBrains Mono',monospace;color:#4ade80;}
.mb-vel-cons-val.vix-set{color:#60b8f0;}
.tb-cbar-row+.tb-cbar-row{border-top:1px solid var(--bdr);}
.tb-cfg-grp{display:flex;flex-direction:column;gap:3px;}
.tb-lbl-sm{font-size:9px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase;}
.tb-inp-sm{background:var(--bg3);border:1px solid var(--bdr);border-radius:5px;color:var(--txt);
           font-size:11px;font-family:'JetBrains Mono',monospace;padding:3px 7px;outline:none;
           width:80px;}
.tb-inp-sm:focus{border-color:var(--info);}
select.tb-inp-sm{width:96px;}
.mb-launch-btn{padding:4px 12px;font-size:10px;font-weight:700;border-radius:6px;cursor:pointer;
               background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;border:none;white-space:nowrap;}
.mb-launch-btn:disabled{opacity:.5;cursor:not-allowed;}
.mb-launch-btn.running{background:linear-gradient(135deg,#15803d,#22c55e);}
.mb-prem-grp{display:flex;align-items:center;gap:3px;}
.mb-prem-grp input{width:58px;}
/* Capital calculator */
.mb-cap-btn{font-size:10px;font-weight:700;padding:3px 10px;border-radius:4px;cursor:pointer;
            border:1px solid #f59e0b;background:rgba(245,158,11,.12);color:#f59e0b;white-space:nowrap;}
.mb-cap-btn:disabled{opacity:.4;cursor:not-allowed;}
.mb-cap-display{font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--txt);white-space:nowrap;}
.mb-cap-max{font-size:10px;font-weight:700;color:#4ade80;white-space:nowrap;}
/* Auto Bot mode toggle buttons */
.mb-mode-btn{font-size:10px;font-weight:700;padding:3px 8px;border-radius:4px;cursor:pointer;
             border:1px solid var(--bdr);background:transparent;color:var(--dim);white-space:nowrap;}
.mb-mode-btn.mb-on-paper{border-color:#4caf50;color:#4caf50;background:rgba(76,175,80,.15);}
.mb-mode-btn.mb-on-mock{border-color:#ff9800;color:#ff9800;background:rgba(255,152,0,.15);}
.mb-mode-btn.mb-on-live{border-color:#f44336;color:#f44336;background:rgba(244,67,54,.15);}
/* Log tab switcher */
.tb-log-tab{background:none;border:none;border-bottom:2px solid transparent;color:var(--dim);
            cursor:pointer;font-size:9px;font-weight:700;padding:3px 10px;letter-spacing:.5px;transition:color .15s;}
.tb-log-tab.active{color:var(--txt);border-bottom-color:var(--acc);}
/* ── Notification bell ── */
.notif-bell-wrap{position:relative;display:flex;align-items:center;}
.notif-bell-btn{background:none;border:none;cursor:pointer;font-size:18px;padding:2px 6px;
                line-height:1;color:var(--dim);transition:color .2s;}
.notif-bell-btn:hover{color:var(--txt);}
.notif-badge{position:absolute;top:-4px;right:-2px;background:#ef4444;color:#fff;
             font-size:9px;font-weight:700;min-width:16px;height:16px;border-radius:8px;
             display:none;align-items:center;justify-content:center;padding:0 3px;
             font-family:'Inter',sans-serif;pointer-events:none;}
.notif-badge.show{display:flex;}
/* Notification panel */
.notif-panel{position:fixed;top:52px;right:12px;width:420px;max-height:520px;
             background:var(--bg2);border:1px solid var(--bdr);border-radius:10px;
             box-shadow:0 8px 32px rgba(0,0,0,.6);z-index:9999;
             display:none;flex-direction:column;overflow:hidden;}
.notif-panel.open{display:flex;}
.notif-panel-hdr{display:flex;align-items:center;justify-content:space-between;
                 padding:10px 14px;border-bottom:1px solid var(--bdr);flex-shrink:0;}
.notif-panel-title{font-size:11px;font-weight:700;letter-spacing:1px;color:var(--txt);}
.notif-panel-actions{display:flex;gap:8px;align-items:center;}
.notif-mute-btn{background:none;border:1px solid var(--bdr);border-radius:4px;
                color:var(--dim);font-size:10px;padding:2px 8px;cursor:pointer;}
.notif-mute-btn.muted{border-color:#ef4444;color:#ef4444;}
.notif-clear-btn{background:none;border:none;color:var(--dim);font-size:11px;cursor:pointer;}
.notif-clear-btn:hover{color:var(--txt);}
.notif-list{overflow-y:auto;flex:1;padding:6px 0;}
.notif-empty{text-align:center;color:var(--dim);font-size:11px;padding:30px;}
.notif-item{display:flex;gap:10px;padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.04);
            animation:notif-slide .25s ease;}
@keyframes notif-slide{from{opacity:0;transform:translateX(12px)}to{opacity:1;transform:none}}
.notif-icon{font-size:16px;flex-shrink:0;margin-top:1px;}
.notif-body{flex:1;min-width:0;}
.notif-source{font-size:9px;font-weight:700;letter-spacing:1px;margin-bottom:2px;}
.notif-msg{font-size:10px;color:var(--txt);font-family:'JetBrains Mono',monospace;
           word-break:break-all;line-height:1.4;}
.notif-time{font-size:9px;color:var(--dim);margin-top:2px;}
/* Alert type colors */
.nt-buy .notif-source{color:#4ade80;}
.nt-sell .notif-source{color:#60a5fa;}
.nt-sl .notif-source{color:#f87171;}
.nt-target .notif-source,.nt-profit .notif-source{color:#4ade80;}
.nt-loss .notif-source{color:#fb923c;}
.nt-error .notif-source{color:#ef4444;}
.nt-signal_buy .notif-source{color:#a78bfa;}
.nt-signal_sell .notif-source{color:#f472b6;}
/* Action bar */
.tb-abar{display:flex;align-items:center;gap:12px;padding:6px 14px;
         background:var(--bg3);border-bottom:1px solid var(--bdr);flex-wrap:wrap;}
/* Option chain section */
.tb-chain-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0;}
.tb-chain-hdr{display:flex;align-items:center;justify-content:space-between;
              padding:5px 14px;background:#060a12;border-bottom:1px solid var(--bdr);flex-shrink:0;}
/* Minimal layout: CE button | STRIKE | PE button only */
.tb-chain-minimal{display:grid;grid-template-columns:1fr 100px 1fr;
                  gap:0;align-items:center;}
.tb-chain-sub-hdr{padding:4px 14px 4px;background:#04080f;border-bottom:1px solid var(--bdr);flex-shrink:0;}
.tb-chain-body{flex:1;overflow-y:auto;min-height:0;}
/* Chain row */
.tb-row{padding:4px 14px;border-bottom:1px solid #060e1a;transition:background .15s;}
.tb-row:hover{background:rgba(56,189,248,.05);}
.tb-row.atm-row{background:rgba(56,189,248,.07);border-top:1px solid rgba(56,189,248,.3);
                border-bottom:1px solid rgba(56,189,248,.3);}
.tb-row.itm-ce{background:rgba(0,229,160,.03);}
.tb-row.itm-pe{background:rgba(255,77,109,.03);}
/* Chain cells */
.tc{font-size:11px;font-family:'JetBrains Mono',monospace;text-align:right;padding:4px 4px;}
.tc-lbl{font-size:9px;color:var(--dim);text-align:right;padding:0 4px;}
.tc-strike{text-align:center;font-weight:700;font-size:12px;}
.tc-ce-ltp{color:var(--bull);font-weight:700;}
.tc-pe-ltp{color:var(--bear);font-weight:700;}
.tc-chg{font-size:9px;font-family:'JetBrains Mono',monospace;line-height:1.2;opacity:.85;}
.tc-chg.up{color:var(--bull)}.tc-chg.dn{color:var(--bear)}.tc-chg.flat{color:var(--dim)}
.tc-oi{color:var(--dim);}
.tc-iv{color:var(--warn);}
.tc-vol{color:#64748b;}
/* LTP flash animation */
@keyframes ltp-up{0%{background:rgba(0,229,160,.35)}100%{background:transparent}}
@keyframes ltp-dn{0%{background:rgba(255,77,109,.35)}100%{background:transparent}}
.ltp-up{animation:ltp-up .7s ease-out}
.ltp-dn{animation:ltp-dn .7s ease-out}
/* OI Intelligence: attention blink animations */
@keyframes oi-blink-bull{0%,100%{box-shadow:0 0 0 rgba(0,229,160,.1)}50%{box-shadow:0 0 24px rgba(0,229,160,.7),0 0 6px rgba(0,229,160,.4) inset}}
@keyframes oi-blink-bear{0%,100%{box-shadow:0 0 0 rgba(255,77,109,.1)}50%{box-shadow:0 0 24px rgba(255,77,109,.7),0 0 6px rgba(255,77,109,.4) inset}}
@keyframes oi-blink-warn{0%,100%{box-shadow:0 0 0 rgba(255,193,7,.1)}50%{box-shadow:0 0 24px rgba(255,193,7,.7),0 0 6px rgba(255,193,7,.4) inset}}
.oi-blink-bull{animation:oi-blink-bull 1.1s ease-in-out infinite;border-color:rgba(0,229,160,.4)!important;}
.oi-blink-bear{animation:oi-blink-bear 1.1s ease-in-out infinite;border-color:rgba(255,77,109,.4)!important;}
.oi-blink-warn{animation:oi-blink-warn 1.4s ease-in-out infinite;border-color:rgba(255,193,7,.4)!important;}
[data-oi-tip]{cursor:help;}
@media(max-width:900px){.tb-main{grid-template-columns:1fr;}.tb-chain-cols{grid-template-columns:70px 55px 46px 70px 44px 85px 44px 70px 46px 55px 70px;}}
/* Config */
.tb-inp{background:var(--bg3);border:1px solid var(--bdr);border-radius:6px;color:var(--txt);
        font-size:12px;font-family:'JetBrains Mono',monospace;padding:4px 8px;width:100%;outline:none;}
.tb-inp:focus{border-color:var(--info);}
.tb-label{font-size:10px;color:var(--dim);letter-spacing:.5px;margin-bottom:3px;display:block;}
.tb-field{margin-bottom:8px;}
.tb-row2{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
/* Option Chain */
.chain-row{display:grid;grid-template-columns:1fr 70px 70px;gap:4px;padding:5px 4px;
           border-bottom:1px solid #0a1020;font-size:11px;align-items:center;cursor:pointer;}
.chain-row:hover{background:rgba(56,189,248,.05);}
.chain-row.atm{background:rgba(56,189,248,.08);border-bottom:1px solid var(--info);}
.chain-atm-label{font-size:9px;color:var(--info);font-weight:700;letter-spacing:.5px;}
.chain-btn{font-size:11px;font-weight:700;padding:5px 12px;border-radius:5px;border:1px solid;
           cursor:pointer;background:none;transition:all .1s;text-align:center;width:100%;max-width:110px;}
.chain-btn.ce{color:var(--bull);border-color:var(--bull);}
.chain-btn.ce:hover:not([disabled]){background:rgba(0,229,160,.18);transform:scale(1.03);}
.chain-btn.pe{color:var(--bear);border-color:var(--bear);}
.chain-btn.pe:hover:not([disabled]){background:rgba(255,77,109,.18);transform:scale(1.03);}
.chain-btn[disabled],.chain-btn.qt-over-budget{opacity:.22!important;cursor:not-allowed!important;filter:grayscale(.6);}
/* Trade history table */
#th-table tbody tr{border-bottom:1px solid #060e1a;transition:background .1s;}
#th-table tbody tr:hover{background:rgba(56,189,248,.04);}
#th-table tbody tr.th-paper td{opacity:.6;}
/* Trade status panels */
.tb-status-idle{text-align:center;padding:20px 10px;color:var(--dim);}
.tb-buy-form{background:var(--bg3);border:1px solid var(--bdr);border-radius:8px;padding:14px;}
.tb-selected-sym{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;
                 padding:8px 12px;border-radius:6px;margin-bottom:10px;text-align:center;}
.tb-selected-ce{color:var(--bull);background:rgba(0,229,160,.07);border:1px solid var(--bull);}
.tb-selected-pe{color:var(--bear);background:rgba(255,77,109,.07);border:1px solid var(--bear);}
.buy-btn{width:100%;padding:10px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;
         border:none;letter-spacing:.5px;transition:all .2s;font-family:'Inter',sans-serif;}
.buy-btn.ce{background:linear-gradient(135deg,var(--buy-ce-dark),var(--buy-ce));color:#000;}
.buy-btn.pe{background:linear-gradient(135deg,var(--buy-pe-dark),var(--buy-pe));color:#fff;}
.buy-btn:disabled{opacity:.55;cursor:not-allowed;filter:saturate(.6);}
.buy-btn:not(:disabled):hover{filter:brightness(1.1);transform:translateY(-1px);box-shadow:0 4px 16px rgba(0,0,0,.3);}
/* Active trade */
.active-trade-card{border:2px solid var(--warn);border-radius:10px;padding:16px;
                   background:rgba(255,193,7,.04);box-shadow:0 0 20px rgba(255,193,7,.08);}
.trade-big-num{font-size:36px;font-weight:800;font-family:'JetBrains Mono',monospace;letter-spacing:-1px;}
.exit-btn{width:100%;padding:10px;margin-top:12px;border-radius:8px;font-size:13px;
          font-weight:700;cursor:pointer;border:2px solid var(--bear);color:var(--bear);
          background:rgba(255,77,109,.08);transition:all .2s;font-family:'Inter',sans-serif;}
.exit-btn:hover{background:rgba(255,77,109,.2);}
.exit-btn:disabled{opacity:.4;cursor:not-allowed;}
/* Done card */
.done-card{border:2px solid;border-radius:10px;padding:16px;}
.done-profit{border-color:var(--bull);background:rgba(0,229,160,.04);}
.done-loss  {border-color:var(--bear);background:rgba(255,77,109,.04);}
/* Log */
.tlog-entry{font-size:11px;padding:3px 0;border-bottom:1px solid #0a1020;
            font-family:'JetBrains Mono',monospace;color:var(--dim);line-height:1.5;}
.tlog-entry .tlog-ts{color:var(--bdr);margin-right:8px;}
/* Paper badge */
.paper-badge{background:rgba(255,193,7,.15);color:var(--warn);border:1px solid var(--warn);
             border-radius:4px;padding:1px 7px;font-size:10px;font-weight:700;letter-spacing:.5px;}
/* Timing row */
.timing-row{display:flex;gap:16px;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px solid var(--bdr);}
.timing-item{text-align:center;}
.timing-val{font-size:18px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--info);}
.timing-lbl{font-size:9px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase;}
/* P&L range bar */
.pnl-range-wrap{margin:10px 0 4px;position:relative;}
.pnl-range-track{height:6px;background:var(--bg3);border-radius:3px;position:relative;overflow:visible;}
.pnl-range-fill-neg{position:absolute;height:100%;background:rgba(255,77,109,.4);border-radius:3px 0 0 3px;left:0;}
.pnl-range-fill-pos{position:absolute;height:100%;background:rgba(0,229,160,.4);border-radius:0 3px 3px 0;}
.pnl-range-entry{position:absolute;top:-4px;width:2px;height:14px;background:var(--txt);border-radius:1px;}
.pnl-range-ltp{position:absolute;top:-5px;width:4px;height:16px;border-radius:2px;transition:left .4s ease;}
.pnl-range-sl  {position:absolute;top:-3px;width:2px;height:12px;background:var(--bear);border-radius:1px;}
.pnl-range-high{position:absolute;top:-3px;width:2px;height:12px;background:var(--bull);border-radius:1px;}
.pnl-range-labels{display:flex;justify-content:space-between;margin-top:4px;font-size:9px;color:var(--dim);font-family:'JetBrains Mono',monospace;}
.pnl-trend-up  {color:var(--bull);font-size:16px;}
.pnl-trend-down{color:var(--bear);font-size:16px;}
.pnl-per-unit  {font-size:11px;color:var(--dim);margin-top:2px;}
/* ── Pivot Points card ── */
.pv-stack{display:flex;flex-direction:column;gap:2px;}
.pv-row{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-radius:6px;gap:8px;}
.pv-row-res{border-left:3px solid var(--bear);background:rgba(255,77,109,.05);}
.pv-row-pp {border-left:3px solid var(--warn);background:rgba(255,193,7,.05);}
.pv-row-sup{border-left:3px solid var(--bull);background:rgba(0,229,160,.05);}
.pv-lbl{font-weight:700;min-width:42px;font-size:12px;}
.pv-val{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600;flex:1;text-align:right;}
.pv-dist{font-size:10px;color:var(--dim);min-width:110px;text-align:right;font-family:'JetBrains Mono',monospace;}
.pv-price-bar{display:flex;align-items:center;gap:8px;padding:3px 8px;margin:2px 0;}
.pv-price-line{flex:1;height:2px;background:var(--info);}
.pv-price-chip{background:var(--info);color:#000;border-radius:12px;padding:2px 12px;
               font-weight:700;font-size:11px;white-space:nowrap;font-family:'JetBrains Mono',monospace;}
/* ── Performance tab ── */
#tab-perf{padding:14px 18px;overflow-y:auto;max-height:calc(100vh - 118px);}
.perf-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px;}
@media(max-width:1100px){.perf-grid{grid-template-columns:1fr 1fr;}}
@media(max-width:640px){.perf-grid{grid-template-columns:1fr;}}
.perf-kpi{background:var(--bg2);border:1px solid var(--bdr);border-radius:10px;padding:14px 16px;}
.perf-kpi-label{font-size:10px;color:var(--dim);letter-spacing:.8px;text-transform:uppercase;margin-bottom:6px;}
.perf-kpi-val{font-size:32px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1.1;}
.perf-kpi-sub{font-size:11px;color:var(--dim);margin-top:4px;}
.perf-section{background:var(--bg2);border:1px solid var(--bdr);border-radius:10px;padding:14px 16px;margin-bottom:12px;}
.perf-section-title{font-size:11px;font-weight:700;letter-spacing:.8px;color:var(--info);text-transform:uppercase;margin-bottom:10px;}
.perf-table{width:100%;border-collapse:collapse;font-size:11px;}
.perf-table th{color:var(--dim);font-weight:600;padding:4px 8px;text-align:left;border-bottom:1px solid var(--bdr);font-size:10px;letter-spacing:.5px;}
.perf-table td{padding:4px 8px;border-bottom:1px solid rgba(255,255,255,.03);vertical-align:middle;}
.perf-table tr:hover td{background:rgba(56,189,248,.04);}
.perf-win {color:var(--bull);font-weight:700;}
.perf-loss{color:var(--bear);font-weight:700;}
.perf-pend{color:var(--warn);}
.perf-badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:10px;font-weight:700;letter-spacing:.4px;}
.perf-badge-ce{background:rgba(0,229,160,.12);color:var(--bull);}
.perf-badge-pe{background:rgba(255,77,109,.12);color:var(--bear);}
.perf-bar-wrap{height:7px;background:var(--bg3);border-radius:4px;overflow:hidden;min-width:60px;}
.perf-bar-fill{height:100%;border-radius:4px;transition:width .6s ease;}
.conf-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;}
.conf-card{background:var(--bg3);border-radius:8px;padding:10px 12px;text-align:center;}
.conf-card-label{font-size:10px;color:var(--dim);margin-bottom:4px;}
.conf-card-wr{font-size:22px;font-weight:800;font-family:'JetBrains Mono',monospace;}
.conf-card-sub{font-size:10px;color:var(--dim);}
/* Bot Control Center */
.bot-card{background:var(--bg2);border:1px solid var(--bdr);border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:10px;transition:border-color .2s;}
.bot-card.running{border-color:var(--bull);}
.bot-card-top{display:flex;align-items:center;gap:8px;}
.bot-badge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;letter-spacing:.5px;}
.bot-badge.running{background:#1a3d1a;color:#4caf50;}
.bot-badge.stopped{background:#2a1a1a;color:#e57373;}
.bot-name{font-size:14px;font-weight:700;color:var(--txt);}
.bot-desc{font-size:11px;color:var(--dim);margin-top:2px;}
.bot-cfg{display:flex;flex-direction:column;gap:7px;background:var(--bg3);border-radius:7px;padding:10px 12px;}
.bot-cfg label{font-size:11px;color:var(--dim);margin-bottom:2px;display:block;}
.bot-cfg select,.bot-cfg input[type=radio]{accent-color:var(--info);}
.bot-cfg select{background:var(--bg2);color:var(--txt);border:1px solid var(--bdr);border-radius:5px;padding:4px 8px;font-size:12px;width:100%;}
.bot-cfg .radio-row{display:flex;gap:14px;flex-wrap:wrap;}
.bot-cfg .radio-row label{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--txt);cursor:pointer;}
.bot-actions{display:flex;gap:8px;}
.bot-start-btn{flex:1;background:var(--bull);color:#fff;border:none;border-radius:6px;padding:7px 0;cursor:pointer;font-size:13px;font-weight:600;}
.bot-stop-btn{flex:1;background:var(--bear);color:#fff;border:none;border-radius:6px;padding:7px 0;cursor:pointer;font-size:13px;font-weight:600;}
.bot-start-btn:disabled,.bot-stop-btn:disabled{opacity:.4;cursor:not-allowed;}
.bot-mode-sel{display:flex;gap:4px;margin-bottom:2px;}
.bot-mode-sel label{flex:1;text-align:center;padding:4px 0;border-radius:5px;border:1px solid var(--bdr);font-size:11px;font-weight:600;cursor:pointer;color:var(--dim);transition:background .15s,color .15s;}
.bot-mode-sel input[type=radio]{display:none;}
.bot-mode-sel input[type=radio]:checked+span{font-weight:700;}
.bot-mode-sel label:has(input[type=radio]:checked){border-color:var(--acc);color:var(--acc);background:rgba(98,179,255,.12);}
.mode-paper:has(input:checked){border-color:#4caf50;color:#4caf50;background:rgba(76,175,80,.12);}
.mode-mock:has(input:checked){border-color:#ff9800;color:#ff9800;background:rgba(255,152,0,.12);}
.mode-live:has(input:checked){border-color:#f44336;color:#f44336;background:rgba(244,67,54,.12);}
.bot-log{background:#060a12;border:1px solid var(--bdr);border-radius:6px;padding:8px 10px;font-size:10px;font-family:"JetBrains Mono",monospace;color:#9ab;max-height:90px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;}
/* ── OI Intraday Chart modal ── */
#oi-chart-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:1100;align-items:center;justify-content:center;backdrop-filter:blur(6px)}
#oi-chart-inner{background:#080f1e;border:1px solid #1e3058;border-radius:12px;width:93vw;height:89vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 30px 90px #000c}
#oi-chart-hdr{display:flex;align-items:center;justify-content:space-between;padding:9px 14px;border-bottom:1px solid #1a2d4e;flex-shrink:0;gap:8px;flex-wrap:wrap}
#oi-chart-wrap{flex:1;position:relative;overflow:hidden}
#oi-chart-canvas{display:block;width:100%;height:100%}
#oi-chart-tt{display:none;position:absolute;background:#0c1525;border:1px solid #1e3058;border-radius:7px;padding:8px 11px;pointer-events:none;z-index:10;font-family:"JetBrains Mono",monospace;min-width:195px;box-shadow:0 8px 30px #0009}
.oi-tog-btn{font-size:9px;padding:3px 9px;border-radius:4px;cursor:pointer;font-family:"JetBrains Mono",monospace;transition:opacity .15s}
.oi-chart-open-btn{font-size:11px;padding:5px 12px;background:#0d1e3a;border:1px solid #38bdf8;border-radius:6px;color:#38bdf8;cursor:pointer;white-space:nowrap;transition:background .15s}
.oi-chart-open-btn:hover{background:#1a3550}
</style>
</head>
<body>

<!-- Color Picker Panel -->
<div id="picker-panel">
  <div class="pk-header">
    <div>
      <div class="pk-title"><span class="swatch"></span> Theme Editor</div>
      <div class="pk-subtitle">All colors · saves automatically</div>
    </div>
  </div>

  <div class="pk-section bg">⬛ Backgrounds</div>
  <div class="pk-row"><label>Main Background</label>    <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-bg">#070b14</span><input type="color" value="#070b14" id="pk-bg"     oninput="setVar('--bg',this.value);updHex('bg',this.value)"><button class="pk-reset-one" onclick="resetOne('--bg','bg')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Card Background</label>    <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-bg2">#0c1220</span><input type="color" value="#0c1220" id="pk-bg2"   oninput="setVar('--bg2',this.value);updHex('bg2',this.value)"><button class="pk-reset-one" onclick="resetOne('--bg2','bg2')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Inner Background</label>   <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-bg3">#131c30</span><input type="color" value="#131c30" id="pk-bg3"   oninput="setVar('--bg3',this.value);updHex('bg3',this.value)"><button class="pk-reset-one" onclick="resetOne('--bg3','bg3')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Header Background</label>  <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-hdrbg">#080f1e</span><input type="color" value="#080f1e" id="pk-hdrbg" oninput="setVar('--hdr-bg',this.value);updHex('hdrbg',this.value)"><button class="pk-reset-one" onclick="resetOne('--hdr-bg','hdrbg')" title="Reset to default">↺</button></div></div>

  <div class="pk-section sig">📈 Signal Colors</div>
  <div class="pk-row"><label>Bullish ▲ (text)</label>   <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-bull">#00e5a0</span><input type="color" value="#00e5a0" id="pk-bull"   oninput="setVar('--bull',this.value);updHex('bull',this.value)"><button class="pk-reset-one" onclick="resetOne('--bull','bull')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Bullish ▲ (bg)</label>     <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-bull2">#001a10</span><input type="color" value="#001a10" id="pk-bull2"  oninput="setVar('--bull2',this.value);updHex('bull2',this.value)"><button class="pk-reset-one" onclick="resetOne('--bull2','bull2')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Bearish ▼ (text)</label>   <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-bear">#ff4d6d</span><input type="color" value="#ff4d6d" id="pk-bear"   oninput="setVar('--bear',this.value);updHex('bear',this.value)"><button class="pk-reset-one" onclick="resetOne('--bear','bear')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Bearish ▼ (bg)</label>     <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-bear2">#1a0010</span><input type="color" value="#1a0010" id="pk-bear2"  oninput="setVar('--bear2',this.value);updHex('bear2',this.value)"><button class="pk-reset-one" onclick="resetOne('--bear2','bear2')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Warning / Caution</label>  <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-warn">#ffc107</span><input type="color" value="#ffc107" id="pk-warn"   oninput="setVar('--warn',this.value);updHex('warn',this.value)"><button class="pk-reset-one" onclick="resetOne('--warn','warn')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Info / Blue</label>        <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-info">#38bdf8</span><input type="color" value="#38bdf8" id="pk-info"   oninput="setVar('--info',this.value);updHex('info',this.value)"><button class="pk-reset-one" onclick="resetOne('--info','info')" title="Reset to default">↺</button></div></div>

  <div class="pk-section txt">🔤 Text</div>
  <div class="pk-row"><label>Main Text</label>          <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-txt">#e2e8f0</span><input type="color" value="#e2e8f0" id="pk-txt"    oninput="setVar('--txt',this.value);updHex('txt',this.value)"><button class="pk-reset-one" onclick="resetOne('--txt','txt')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>Dim / Secondary Text</label><div class="pk-swatch-wrap"><span class="pk-hex" id="hx-dim">#5a7298</span><input type="color" value="#5a7298" id="pk-dim"    oninput="setVar('--dim',this.value);updHex('dim',this.value)"><button class="pk-reset-one" onclick="resetOne('--dim','dim')" title="Reset to default">↺</button></div></div>

  <div class="pk-section ui">🔲 UI Elements</div>
  <div class="pk-row"><label>Border Color</label>       <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-bdr">#1c2d48</span><input type="color" value="#1c2d48" id="pk-bdr"    oninput="setVar('--bdr',this.value);updHex('bdr',this.value)"><button class="pk-reset-one" onclick="resetOne('--bdr','bdr')" title="Reset to default">↺</button></div></div>
  <div class="pk-row"><label>AI / Accent Purple</label> <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-accent">#a855f7</span><input type="color" value="#a855f7" id="pk-accent" oninput="setVar('--accent',this.value);updHex('accent',this.value)"><button class="pk-reset-one" onclick="resetOne('--accent','accent')" title="Reset to default">↺</button></div></div>

  <div class="pk-section" style="color:#00e5a0">🚀 Trade Board Buttons</div>
  <!-- Live preview of buy buttons -->
  <div style="display:flex;gap:6px;margin-bottom:8px">
    <div style="flex:1;padding:5px;border-radius:6px;text-align:center;font-size:10px;font-weight:700;color:#000;
                background:linear-gradient(135deg,var(--buy-ce-dark),var(--buy-ce))">BUY CE ▲</div>
    <div style="flex:1;padding:5px;border-radius:6px;text-align:center;font-size:10px;font-weight:700;color:#fff;
                background:linear-gradient(135deg,var(--buy-pe-dark),var(--buy-pe))">BUY PE ▼</div>
  </div>
  <div style="font-size:9px;color:var(--dim);margin-bottom:6px">Gradient: dark shade (left) → main color (right)</div>
  <div class="pk-row"><label>BUY CE — main (right)</label>  <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-buyce">#00e5a0</span><input type="color" value="#00e5a0" id="pk-buyce"     oninput="setVar('--buy-ce',this.value);updHex('buyce',this.value)"><button class="pk-reset-one" onclick="resetOne('--buy-ce','buyce')" title="Reset">↺</button></div></div>
  <div class="pk-row"><label>BUY CE — shade (left)</label>  <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-buyced">#00875e</span><input type="color" value="#00875e" id="pk-buyced"   oninput="setVar('--buy-ce-dark',this.value);updHex('buyced',this.value)"><button class="pk-reset-one" onclick="resetOne('--buy-ce-dark','buyced')" title="Reset">↺</button></div></div>
  <div class="pk-row"><label>BUY PE — main (right)</label>  <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-buype">#ff4d6d</span><input type="color" value="#ff4d6d" id="pk-buype"     oninput="setVar('--buy-pe',this.value);updHex('buype',this.value)"><button class="pk-reset-one" onclick="resetOne('--buy-pe','buype')" title="Reset">↺</button></div></div>
  <div class="pk-row"><label>BUY PE — shade (left)</label>  <div class="pk-swatch-wrap"><span class="pk-hex" id="hx-buyped">#be1a3c</span><input type="color" value="#be1a3c" id="pk-buyped"   oninput="setVar('--buy-pe-dark',this.value);updHex('buyped',this.value)"><button class="pk-reset-one" onclick="resetOne('--buy-pe-dark','buyped')" title="Reset">↺</button></div></div>
  <div class="pk-slider-row">
    <label>Glow / Shadow Intensity</label>
    <div class="pk-slider-wrap">
      <input type="range" min="0" max="100" value="12" id="pk-glow"
             oninput="setGlow(this.value);$('pk-glow-val').textContent=this.value+'%'">
      <span class="pk-slider-val" id="pk-glow-val">12%</span>
    </div>
  </div>

  <div class="pk-actions">
    <button class="pk-reset" onclick="resetColors()">↺ Reset Default</button>
    <button class="pk-close" onclick="togglePicker()">✕ Close</button>
  </div>
</div>

<div class="hdr">
  <div class="hdr-left" style="flex-direction:column;gap:4px;align-items:flex-start">
    <div class="hdr-title" id="htitle">📡 LIVE DASHBOARD</div>
    <div class="idx-cards" id="mkt-ticker"></div>
  </div>
  <div class="hdr-r">
    <div id="htime" style="font-family:'JetBrains Mono',monospace">—</div>
    <span id="mtc-badge" class="mtc mtc-ok">—m left</span>
    <div>Refresh <span id="countdown">15</span>s</div>
    <button id="picker-btn" onclick="togglePicker()" title="Customize Theme Colors">
      <span class="swatch"></span> Theme
    </button>
    <!-- Notification bell -->
    <div class="notif-bell-wrap">
      <button class="notif-bell-btn" onclick="notifTogglePanel()" title="Alerts">🔔</button>
      <span class="notif-badge" id="notif-badge">0</span>
    </div>
  </div>
</div>

<!-- Notification panel -->
<div class="notif-panel" id="notif-panel">
  <div class="notif-panel-hdr">
    <span class="notif-panel-title">🔔 ALERTS</span>
    <div class="notif-panel-actions">
      <button class="notif-mute-btn" id="notif-mute-btn" onclick="notifToggleMute()" title="Mute/unmute sounds">🔊 Sound ON</button>
      <button class="notif-clear-btn" onclick="notifClear()">✕ Clear all</button>
    </div>
  </div>
  <div class="notif-list" id="notif-list">
    <div class="notif-empty">No alerts yet — bots will notify you here</div>
  </div>
</div>

<div class="bbar" id="bbar"><span style="color:var(--dim);font-size:11px">BOT STATUS:</span></div>

<!-- Tab bar -->
<div class="tabbar">
  <button class="tab-btn active" onclick="switchTab('dashboard',this)">📡 Live Dashboard</button>
  <button class="tab-btn" onclick="switchTab('oi',this);initOITab()">🔬 OI Intelligence</button>
  <button class="tab-btn" onclick="switchTab('trade',this);initTradeTab()">🚀 Trade Board</button>
  <button class="tab-btn" onclick="switchTab('pnl',this);loadTradeHistory()">💹 PnL Status</button>
  <button class="tab-btn" onclick="switchTab('perf',this);initPerfTab()">📈 Performance</button>
  <button class="tab-btn" onclick="switchTab('bots',this);initBotsTab()">🤖 Bot Control</button>
  <button class="tab-btn" onclick="switchTab('scanner',this);initScannerTab()">🔭 Scanner</button>
  <button class="tab-btn" id="aibrain-tab-btn" onclick="switchTab('aibrain',this);initAiBrainTab()" style="color:#c084fc">🧠 AI Brain</button>
  <button class="tab-btn" onclick="switchTab('vix',this);initVixTab()">🌡 VIX</button>
  <button class="tab-btn" onclick="switchTab('engine',this)" style="color:#4ade80">⚡ Decision Engine</button>
  <button class="tab-btn" onclick="switchTab('control',this);initControlTab()" style="color:#f85149">🛡 Control</button>
  <button class="tab-btn" onclick="switchTab('guide',this)">🗺️ Guide</button>
</div>

<!-- Dashboard tab -->
<div id="tab-dashboard" class="tab-pane active">
<div class="main">

  <div class="cons neutral" id="cbox">
    <div class="csig" id="csig">LOADING…</div>
    <div><div class="csmry" id="csmry">Fetching data…</div><div class="csrc" id="csrc"></div></div>
    <div class="cscores">
      <div class="vbull">▲ Bull: <b id="cbull">—</b></div>
      <div class="vbear">▼ Bear: <b id="cbear">—</b></div>
    </div>
  </div>

  <!-- Quick Scalping Action Plan -->
  <div id="scalp-box">
    <div class="scalp-label">
      ⚡ SCALP PLAN
      <span id="scalp-ts" class="scalp-ts"></span>
      <button id="scalp-toggle" class="toggle-btn toggle-off" onclick="toggle('scalp')">OFF</button>
    </div>
    <div id="scalp-text" class="scalp-text scalp-wait">
      <span class="scalp-dim">Generating first scalp plan…</span>
    </div>
  </div>

  <div class="g2">
    <div class="card">
      <div class="ctitle">📐 Key Levels <span class="age" id="lvl-age"></span></div>
      <div id="lvl-chart-tip"></div>
      <table class="ltbl">
        <thead><tr><th>Level</th><th>Price</th><th>Distance</th><th>★</th></tr></thead>
        <tbody id="lvlbody"><tr><td colspan="4" style="color:var(--dim);text-align:center">Loading…</td></tr></tbody>
      </table>
      <div id="swing-danger" style="display:none"></div>
    </div>
    <!-- right column: Master Signal stacked above Pivot Points -->
    <div style="display:flex;flex-direction:column;gap:10px">
      <div class="card" id="master-card">
        <div class="ctitle">🎯 Master Signal <span class="age" id="master-age"></span></div>
        <div id="master-body"><div class="offline-warn">⚠ Not running — start MASTER_SIGNAL_BOT.py</div></div>
      </div>
      <div class="card" id="pivot-card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <div class="ctitle" style="border:none;margin:0;padding:0">
            📊 Pivot Points
            <span style="font-size:9px;color:var(--dim);margin-left:6px" id="pivot-src"></span>
            <span class="age" id="pivot-age" style="margin-left:4px"></span>
          </div>
          <div style="font-size:9px;color:var(--dim)">prev-day OHLC · resets daily</div>
        </div>
        <div id="pivot-body"><div style="color:var(--dim);font-size:11px;padding:8px;text-align:center">Loading…</div></div>
      </div>
    </div>
  </div>

  <div class="g2">
    <div class="card">
      <div class="ctitle">📈 Fibonacci Analyzer <span class="age" id="fibo-age"></span></div>
      <div id="fibo-chart-tip"></div>
      <div id="fibo-body"><div class="offline-warn">⚠ Not running — start FIBONACCI_TREND_ANALYZER.py</div></div>
    </div>
    <div class="card" id="opt-card">
      <div class="ctitle">💡 Option Suggestion <span class="age" id="opt-age"></span></div>
      <div id="opt-body"><div style="color:var(--dim)">Waiting for signal…</div></div>
    </div>
  </div>

  <div class="g3">
    <div class="card">
      <div class="ctitle">💸 Premium Flow <span class="age" id="prem-age"></span></div>
      <div id="prem-body"><div class="offline-warn">⚠ Not running — start PREMIUM_DIRECTION_TRACKER.py</div></div>
    </div>
    <div class="card">
      <div class="ctitle">🤖 Trade Bot <span class="age" id="trade-age"></span></div>
      <div id="trade-body"><div class="offline-warn">⚠ Not running — start PROD10FEB_ManualBOT.py</div></div>
    </div>
    <div class="card">
      <div class="ctitle">🔍 Signal Monitor <span class="age" id="sigmon-age"></span></div>
      <div id="sigmon-body"><div style="color:var(--dim)">No recent data</div></div>
    </div>
  </div>

    <!-- AI Live Summary -->
    <div class="card" id="ai-card">
    <div class="ai-header">
      <div class="ctitle" style="border:none;margin:0;padding:0">🤖 AI Live Summary
        <span class="age" id="ai-age" style="margin-left:8px"></span>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span id="ai-source-badge" class="ai-source" style="display:none"></span>
        <button id="ai-toggle" class="toggle-btn toggle-off" onclick="toggle('ai')">OFF</button>
      </div>
    </div>
    <div id="ai-body">
      <div style="color:var(--dim);font-size:12px">
        <span class="ai-spinner">◌</span> Generating first summary (up to 45s)…
      </div>
    </div>
  </div>

  </div><!-- end .main -->
</div><!-- end #tab-dashboard -->

<!-- PnL Status tab -->
<div id="tab-pnl" class="tab-pane">
<div id="tab-pnl-inner">

  <!-- Row 1: Today PnL + Trading Permission -->
  <div class="pnl-grid">

    <!-- Today PnL card -->
    <div class="card" id="pnl-card">
      <div class="ctitle">
        <span>TODAY'S P&amp;L</span>
        <span id="pnl-ts" class="age"></span>
      </div>
      <div id="pnl-big" class="pnl-big pnl-zero">₹0</div>
      <div style="margin-top:6px;font-size:11px;color:var(--dim)">
        <span id="pnl-count">0 trades</span> &nbsp;·&nbsp;
        <span id="pnl-wins" style="color:var(--bull)">0W</span>&nbsp;
        <span id="pnl-losses" style="color:var(--bear)">0L</span>&nbsp;·&nbsp;
        <span id="pnl-open" style="color:var(--warn)">0 open</span>
      </div>
      <div style="margin-top:4px;font-size:11px">
        <span style="color:var(--dim)">Unrealised: </span><span id="pnl-unrealised" style="font-family:'JetBrains Mono',monospace;font-weight:600">₹0</span>
        &nbsp;·&nbsp;<span style="color:var(--dim)">Total incl. open: </span><span id="pnl-total-open" style="font-family:'JetBrains Mono',monospace;font-weight:600">₹0</span>
      </div>
      <div class="has-tip">
        <div class="pnl-bar-wrap"><div id="pnl-bar" class="pnl-bar-fill pnl-bar-pos" style="width:0%"></div></div>
        <div class="tip-box">
          <div class="tip-title" style="color:var(--bull)">Daily P&amp;L vs Target</div>
          <div style="color:var(--txt);font-size:11px;line-height:1.7">Bar fills as your <b>realised P&amp;L</b> approaches the daily target.<br>Set your target using the input below.<br><span style="color:var(--bull)">Green</span> = profit progress &nbsp;|&nbsp; <span style="color:var(--bear)">Red</span> = loss depth.<br>Alarm fires (🔔) when target is hit.</div>
        </div>
      </div>
      <div style="font-size:10px;color:var(--dim);margin-bottom:8px">
        <span id="pnl-pct">0%</span> of daily target
      </div>
      <div class="pnl-target-row">
        <label>Daily Target ₹</label>
        <input type="number" class="pnl-target-inp" id="pnl-target-inp" value="10000" min="1000" step="1000"
               oninput="savePnlTarget(this.value)">
        <button class="pnl-alarm-btn" id="pnl-alarm-btn" onclick="toggleAlarm()" title="Toggle alarm">🔔</button>
        <span id="pnl-alarm-status" style="font-size:10px;color:var(--dim)">Alarm ON</span>
      </div>
    </div>

    <!-- Trading Permission card -->
    <div class="card" id="perm-card">
      <div class="ctitle"><span>TRADING PERMISSION</span><span id="perm-ts" class="age"></span></div>
      <div style="margin-bottom:10px">
        <span id="perm-verdict" class="verdict-chip v-caution">—</span>
      </div>
      <div class="row"><span class="lbl">Permission Score</span><span id="perm-score" class="v">—/100</span></div>
      <div class="has-tip" id="perm-bar-wrap">
        <div class="score-bar-wrap"><div id="perm-bar" class="score-bar score-green" style="width:0%"></div></div>
        <div class="tip-box">
          <div class="tip-title" style="color:#a855f7">Permission Score — Should YOU trade today?</div>
          <div class="tip-row"><span class="tip-range">81–100</span><span class="tip-meaning"><b style="color:#a3e635">HIGH CONFIDENCE</b> — All signals aligned. Trade up to full size.</span></div>
          <div class="tip-row"><span class="tip-range">61–80</span><span class="tip-meaning"><b style="color:var(--bull)">NORMAL</b> — Good day to trade. 50–75% position size.</span></div>
          <div class="tip-row"><span class="tip-range">41–60</span><span class="tip-meaning"><b style="color:var(--warn)">CAUTION</b> — Mixed signals. 25–40% size, tight stops.</span></div>
          <div class="tip-row"><span class="tip-range">0–40</span><span class="tip-meaning"><b style="color:var(--bear)">NO TRADE</b> — Unfavorable conditions. Stay out.</span></div>
          <div style="margin-top:6px;font-size:10px;color:var(--dim)">Weighted: Market 35% + Your similar-day win rate 40% + Recent form 20% − Behavioral risks</div>
        </div>
      </div>
      <div class="row" style="margin-top:8px"><span class="lbl">Market Score</span><span id="mkt-score" class="v">—/100</span></div>
      <div class="has-tip" id="mkt-bar-wrap">
        <div class="score-bar-wrap"><div id="mkt-bar" class="score-bar score-yellow" style="width:0%"></div></div>
        <div class="tip-box">
          <div class="tip-title" style="color:var(--info)">Market Condition Score — Is the market tradeable?</div>
          <div class="tip-row"><span class="tip-range">81–100</span><span class="tip-meaning"><b style="color:var(--bull)">IDEAL</b> — Low VIX, stable, flat open, healthy PCR.</span></div>
          <div class="tip-row"><span class="tip-range">61–80</span><span class="tip-meaning"><b style="color:var(--bull)">CALM / STABLE</b> — Good conditions, clean trends expected.</span></div>
          <div class="tip-row"><span class="tip-range">41–60</span><span class="tip-meaning"><b style="color:var(--warn)">MIXED</b> — Some risk factors present. Trade smaller.</span></div>
          <div class="tip-row"><span class="tip-range">0–40</span><span class="tip-meaning"><b style="color:var(--bear)">VOLATILE</b> — High VIX / big gap / extreme PCR. High risk.</span></div>
          <div style="margin-top:6px;font-size:10px;color:var(--dim)">Factors: VIX level, VIX daily change, NIFTY gap, PCR, Day of week</div>
        </div>
      </div>
      <div class="row" style="margin-top:8px"><span class="lbl">Recent Win Rate</span><span id="perm-recwr" class="v">—%</span></div>
      <div class="row"><span class="lbl">Direction Bias</span><span id="perm-bias" class="v">—</span></div>
      <div class="row"><span class="lbl">Position Size</span><span id="perm-size" class="v vdim">—</span></div>
      <div style="margin-top:10px;font-size:10px;color:var(--dim);line-height:1.6" id="perm-bkdwn"></div>
    </div>
  </div>

  <!-- Row 2: Live Market + 3-Year Stats -->
  <div class="pnl-grid">

    <!-- Live Market card -->
    <div class="card">
      <div class="ctitle"><span>LIVE MARKET</span><span id="mkt-ts" class="age"></span></div>
      <div class="row"><span class="lbl">NIFTY</span><span id="mkt-nifty" class="v">—</span></div>
      <div class="row"><span class="lbl">India VIX</span><span id="mkt-vix" class="v">—</span></div>
      <div class="row"><span class="lbl">Gap</span><span id="mkt-gap" class="v">—</span></div>
      <div class="row"><span class="lbl">PCR</span><span id="mkt-pcr" class="v">—</span></div>
      <div class="row"><span class="lbl">Market</span><span id="mkt-open" class="v">—</span></div>
      <div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:10px;color:var(--dim)">Market Condition Score</span>
        <span id="mkt-score2" style="font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace">—</span>
      </div>
      <div class="has-tip">
        <div class="score-bar-wrap"><div id="mkt-bar2" class="score-bar score-green" style="width:0%"></div></div>
        <div class="tip-box">
          <div class="tip-title" style="color:var(--info)">Market Condition Score</div>
          <div class="tip-row"><span class="tip-range">81–100</span><span class="tip-meaning">Ideal — low fear, stable open, balanced OI</span></div>
          <div class="tip-row"><span class="tip-range">61–80</span><span class="tip-meaning">Calm — good for technical setups</span></div>
          <div class="tip-row"><span class="tip-range">41–60</span><span class="tip-meaning">Mixed — some risk factors, reduce size</span></div>
          <div class="tip-row"><span class="tip-range">0–40</span><span class="tip-meaning">Volatile — high VIX/gap, whipsaw risk</span></div>
          <div style="margin-top:6px;font-size:10px;color:var(--dim)">Hover individual score lines below for detailed meaning</div>
        </div>
      </div>
      <div id="mkt-bkdwn" style="margin-top:8px;font-size:10px;color:var(--dim);line-height:1.8"></div>
    </div>

    <!-- 3-Year Stats card -->
    <div class="card">
      <div class="ctitle"><span>3-YEAR PERSONAL STATS</span></div>
      <div class="row"><span class="lbl">Total Days</span><span id="st-days" class="v">—</span></div>
      <div class="row"><span class="lbl">Win Rate</span><span id="st-wr" class="v">—</span></div>
      <div class="row"><span class="lbl">Avg Win Day</span><span id="st-avgwin" class="v vbull">—</span></div>
      <div class="row"><span class="lbl">Avg Loss Day</span><span id="st-avgloss" class="v vbear">—</span></div>
      <div class="row"><span class="lbl">Total P&amp;L</span><span id="st-total" class="v">—</span></div>
      <div class="row"><span class="lbl">Best Day</span><span id="st-best" class="v vbull">—</span></div>
      <div class="row"><span class="lbl">Worst Day</span><span id="st-worst" class="v vbear">—</span></div>
      <div style="margin-top:10px;font-size:10px;color:var(--dim)">Yearly P&amp;L</div>
      <div id="st-yearly" style="margin-top:4px;font-size:11px;line-height:1.9;font-family:'JetBrains Mono',monospace"></div>
    </div>
  </div>

  <!-- Row 2b: VIX Analysis (full-width) -->
  <div class="card vix-card" id="vix-analysis-card" style="margin-bottom:10px">
    <div class="ctitle">
      <span>VIX ANALYSIS <span style="font-size:9px;color:var(--dim)">— Intraday Fear Gauge (2-min ticks)</span></span>
      <span id="vix-hist-ts" class="age"></span>
    </div>

    <!-- Top stat row -->
    <div class="vix-stat-row">

      <div class="has-tip vix-stat-box">
        <div class="vix-stat-label">Current VIX</div>
        <div id="vix-curr-val" class="vix-stat-num">—</div>
        <div class="tip-box">
          <div class="tip-title" style="color:#38bdf8">India VIX — What it means</div>
          <div class="tip-row"><span class="tip-range">&lt; 12</span><span class="tip-meaning"><b style="color:#4ade80">Ultra Calm</b> — premiums are cheap. Buy options, avoid selling.</span></div>
          <div class="tip-row"><span class="tip-range">12–15</span><span class="tip-meaning"><b style="color:#a3e635">Calm</b> — normal trading conditions. Standard position sizing.</span></div>
          <div class="tip-row"><span class="tip-range">15–18</span><span class="tip-meaning"><b style="color:var(--warn)">Moderate</b> — some fear. Reduce size slightly, widen stops.</span></div>
          <div class="tip-row"><span class="tip-range">18–22</span><span class="tip-meaning"><b style="color:#fb923c">Elevated</b> — market nervous. Half size, use defined-risk trades.</span></div>
          <div class="tip-row"><span class="tip-range">&gt; 22</span><span class="tip-meaning"><b style="color:var(--bear)">Danger</b> — high fear. Avoid directional bets; only hedge.</span></div>
        </div>
      </div>

      <div class="has-tip vix-stat-box">
        <div class="vix-stat-label">Day Change</div>
        <div id="vix-day-chg" class="vix-stat-num">—</div>
        <div class="tip-box">
          <div class="tip-title" style="color:#38bdf8">VIX vs Yesterday's Close</div>
          <div class="tip-row"><span class="tip-range">&gt; +5%</span><span class="tip-meaning">Fear injected overnight — gap & whipsaw risk. Wait 9:30.</span></div>
          <div class="tip-row"><span class="tip-range">+2 to +5%</span><span class="tip-meaning">Mild fear rise. Direction confirmation needed post-open.</span></div>
          <div class="tip-row"><span class="tip-range">±2%</span><span class="tip-meaning">Stable. Market environment unchanged from yesterday.</span></div>
          <div class="tip-row"><span class="tip-range">&lt; −2%</span><span class="tip-meaning">Fear fading. Premium decay may accelerate — good for sellers.</span></div>
        </div>
      </div>

      <div class="has-tip vix-stat-box">
        <div class="vix-stat-label">Session High / Low</div>
        <div id="vix-hilow" class="vix-stat-num" style="font-size:13px">— / —</div>
        <div class="tip-box">
          <div class="tip-title" style="color:#38bdf8">Intraday VIX Range</div>
          <div style="font-size:11px;color:var(--txt);line-height:1.7">A wide intraday range (Hi − Lo &gt; 5%) signals an unstable session — high whipsaw risk.<br>A tight range means consistent market sentiment — trend trades are safer.</div>
        </div>
      </div>

      <div class="has-tip vix-stat-box">
        <div class="vix-stat-label">10-min Δ</div>
        <div id="vix-10m-chg" class="vix-stat-num">—</div>
        <div class="tip-box">
          <div class="tip-title" style="color:#38bdf8">VIX Speed (last ~10 min)</div>
          <div class="tip-row"><span class="tip-range">&gt; +3%</span><span class="tip-meaning"><b style="color:var(--bear)">Fast spike</b> — panic entering market RIGHT NOW. Stop trading.</span></div>
          <div class="tip-row"><span class="tip-range">+1 to +3%</span><span class="tip-meaning">Moderate rise. Watch for reversal; avoid new entries.</span></div>
          <div class="tip-row"><span class="tip-range">±1%</span><span class="tip-meaning">Stable. No immediate fear signal.</span></div>
          <div class="tip-row"><span class="tip-range">&lt; −1%</span><span class="tip-meaning">VIX easing. Premiums compressing — market calming down.</span></div>
        </div>
      </div>

      <div class="has-tip vix-stat-box">
        <div class="vix-stat-label">Regime</div>
        <div id="vix-regime" class="vix-stat-num" style="font-size:12px">—</div>
        <div class="tip-box">
          <div class="tip-title" style="color:#38bdf8">VIX Regime — Trade Guidance</div>
          <div class="tip-row"><span style="color:#4ade80;min-width:80px;display:inline-block">CALM</span><span class="tip-meaning">VIX &lt; 15 — full size, normal stops, buy options.</span></div>
          <div class="tip-row"><span style="color:var(--warn);min-width:80px;display:inline-block">MODERATE</span><span class="tip-meaning">VIX 15–18 — 75% size, slightly wider stops.</span></div>
          <div class="tip-row"><span style="color:#fb923c;min-width:80px;display:inline-block">ELEVATED</span><span class="tip-meaning">VIX 18–22 — 50% size, defined-risk only.</span></div>
          <div class="tip-row"><span style="color:var(--bear);min-width:80px;display:inline-block">DANGER</span><span class="tip-meaning">VIX &gt; 22 — sit out or hedge only.</span></div>
        </div>
      </div>

    </div><!-- /vix-stat-row -->

    <!-- Sparkline -->
    <div style="position:relative;margin-top:8px">
      <canvas id="vix-sparkline" height="72" style="width:100%;height:72px;display:block;border-radius:6px;background:rgba(14,20,38,.6)"></canvas>
      <div id="vix-spark-tt" style="display:none;position:absolute;top:4px;left:50%;transform:translateX(-50%);font-size:10px;font-family:'JetBrains Mono',monospace;color:var(--txt);background:var(--bg3);border:1px solid var(--bdr);border-radius:4px;padding:2px 8px;pointer-events:none;white-space:nowrap"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--dim);margin-top:3px;font-family:'JetBrains Mono',monospace">
      <span id="vix-spark-t0">—</span>
      <span style="color:var(--dim)">← session timeline →</span>
      <span id="vix-spark-tn">now</span>
    </div>

    <!-- Alarm toggle -->
    <div style="margin-top:8px;display:flex;align-items:center;gap:10px;font-size:10px;color:var(--dim)">
      <button id="vix-alarm-btn" onclick="vixToggleAlarm()"
        style="font-size:10px;padding:3px 10px;border-radius:10px;border:1px solid var(--bdr);background:var(--bg3);color:var(--dim);cursor:pointer">
        🔔 Alarm ON
      </button>
      <span id="vix-alarm-label">Alert fires when VIX spikes &gt;3% in 10 min or crosses 15 / 18 / 20 / 25</span>
    </div>
  </div>

  <!-- Row 3: Behavioral Risks + Similar Days -->
  <div class="pnl-grid">

    <!-- Behavioral Risks card -->
    <div class="card">
      <div class="ctitle"><span>BEHAVIORAL ANALYSIS</span></div>
      <div id="behav-risks"></div>
      <div id="behav-insights" style="margin-top:8px"></div>
    </div>

    <!-- Similar Days card -->
    <div class="card">
      <div class="ctitle"><span>SIMILAR HISTORICAL DAYS</span></div>
      <div class="row"><span class="lbl">Sample Size</span><span id="sim-count" class="v">—</span></div>
      <div class="row"><span class="lbl">Win Rate</span><span id="sim-wr" class="v">—</span></div>
      <div class="row"><span class="lbl">Avg Win</span><span id="sim-avgwin" class="v vbull">—</span></div>
      <div class="row"><span class="lbl">Avg Loss</span><span id="sim-avgloss" class="v vbear">—</span></div>
      <div class="row"><span class="lbl">Best Similar</span><span id="sim-best" class="v vbull">—</span></div>
      <div class="row"><span class="lbl">Worst Similar</span><span id="sim-worst" class="v vbear">—</span></div>
      <div style="margin-top:10px;font-size:10px;color:var(--dim)">Top 5 Most Similar Days</div>
      <div id="sim-top5" style="margin-top:4px"></div>
    </div>
  </div>

  <!-- Row 4: Margin + Orders -->
  <div class="pnl-grid" style="margin-bottom:10px">

    <!-- Capital / Margin card -->
    <div class="card">
      <div class="ctitle"><span>CAPITAL &amp; MARGIN  <span style="font-size:9px;color:var(--dim)">(Groww API)</span></span><span id="margin-ts" class="age"></span></div>
      <div class="row"><span class="lbl">Available Cash</span><span id="m-cash" class="v vbull">—</span></div>
      <div class="row"><span class="lbl">F&amp;O Buy Balance</span><span id="m-opt-buy" class="v vbull">—</span></div>
      <div class="row"><span class="lbl">F&amp;O Sell Balance</span><span id="m-opt-sell" class="v">—</span></div>
      <div style="margin:8px 0 4px;border-top:1px solid var(--bdr);padding-top:8px">
        <div class="row"><span class="lbl">Margin Used</span><span id="m-used" class="v vwarn">—</span></div>
        <div class="row"><span class="lbl">SPAN Used</span><span id="m-span" class="v vdim">—</span></div>
        <div class="row"><span class="lbl">Exposure Used</span><span id="m-exp" class="v vdim">—</span></div>
        <div class="row"><span class="lbl">Brokerage</span><span id="m-brok" class="v vbear">—</span></div>
      </div>
    </div>

    <!-- Today's Orders card -->
    <div class="card">
      <div class="ctitle"><span>TODAY'S ORDERS  <span style="font-size:9px;color:var(--dim)">(Groww API)</span></span><span id="orders-ts" class="age"></span></div>
      <div id="orders-list"><div class="ptai-no-data">Fetching orders…</div></div>
    </div>
  </div>

  <!-- Row 5: Today's Positions -->
  <div class="card" style="margin-bottom:10px">
    <div class="ctitle"><span>TODAY'S POSITIONS  <span style="font-size:9px;color:var(--dim)">(Groww API · unrealised via LTP)</span></span><span id="trades-count" class="age"></span></div>
    <div id="trades-list"><div class="ptai-no-data">Fetching positions from Groww…</div></div>
  </div>

  <!-- Row 6: AI Narrative -->
  <div class="card" id="ptai-ai-card" style="border:1px solid rgba(168,85,247,.4);background:linear-gradient(135deg,#07041a 0%,#0a0620 100%);box-shadow:0 0 28px rgba(168,85,247,.06);">
    <div class="ctitle">
      <span style="color:#c084fc">AI ADVISORY  <span style="font-size:9px;color:var(--dim)">(PERSONAL_TRADING_AI)</span></span>
      <button id="ptai_ai-toggle" class="toggle-btn toggle-off" onclick="toggle('ptai_ai')">OFF</button>
    </div>
    <div id="ptai-ai-body">
      <div class="ptai-no-data" style="color:var(--dim)">Enable AI Assistance to generate advisory.<br><span style="font-size:10px">Uses Claude CLI · refreshes every 30 min</span></div>
    </div>
  </div>

  <!-- Trade History section -->
  <div class="card" style="margin-top:16px" id="trade-hist-card">
    <div class="ctitle" style="margin-bottom:10px">
      <span>TRADE HISTORY <span style="font-size:9px;color:var(--dim);font-weight:400">— all bots, all modes</span></span>
      <button onclick="loadTradeHistory()" style="font-size:10px;padding:3px 10px;border-radius:12px;border:1px solid var(--bdr);background:var(--bg3);color:var(--dim);cursor:pointer">⟳ Refresh</button>
    </div>

    <!-- date filters -->
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px;font-size:11px">
      <label style="color:var(--dim)">From</label>
      <input type="date" id="th-from" style="background:var(--bg3);color:var(--txt);border:1px solid var(--bdr);border-radius:5px;padding:3px 7px;font-size:11px">
      <label style="color:var(--dim)">To</label>
      <input type="date" id="th-to"   style="background:var(--bg3);color:var(--txt);border:1px solid var(--bdr);border-radius:5px;padding:3px 7px;font-size:11px">
      <button onclick="loadTradeHistory()" style="font-size:10px;padding:3px 12px;border-radius:12px;border:1px solid var(--info);background:rgba(99,179,237,.08);color:var(--info);cursor:pointer">Apply</button>
      <button onclick="resetThDates()" style="font-size:10px;padding:3px 10px;border-radius:12px;border:1px solid var(--bdr);background:var(--bg3);color:var(--dim);cursor:pointer">Today</button>
      <!-- bot filter chips -->
      <div style="margin-left:auto;display:flex;gap:6px;align-items:center">
        <span style="color:var(--dim)">Bot:</span>
        <button class="th-filter-btn active" data-bot="ALL"        onclick="setThBot(this)">All</button>
        <button class="th-filter-btn"        data-bot="Auto"       onclick="setThBot(this)">Auto</button>
        <button class="th-filter-btn"        data-bot="PROD10"     onclick="setThBot(this)">PROD10</button>
        <button class="th-filter-btn"        data-bot="Trendline"  onclick="setThBot(this)">Trendline</button>
      </div>
      <div style="display:flex;gap:6px;align-items:center">
        <span style="color:var(--dim)">Mode:</span>
        <button class="th-filter-btn active" data-mode="ALL"    onclick="setThMode(this)">All</button>
        <button class="th-filter-btn"        data-mode="live"   onclick="setThMode(this)">Live</button>
        <button class="th-filter-btn"        data-mode="mock"   onclick="setThMode(this)">Mock</button>
        <button class="th-filter-btn"        data-mode="paper"  onclick="setThMode(this)">Paper</button>
      </div>
    </div>

    <!-- summary bar -->
    <div id="th-summary" style="display:none;margin-bottom:10px;padding:8px 12px;border-radius:6px;background:var(--bg3);border:1px solid var(--bdr);font-size:11px;display:flex;gap:20px;flex-wrap:wrap"></div>

    <!-- table -->
    <div style="overflow-x:auto">
      <table id="th-table" style="width:100%;border-collapse:collapse;font-size:11px;font-family:'JetBrains Mono',monospace">
        <thead>
          <tr style="color:var(--dim);font-size:9.5px;letter-spacing:.5px;border-bottom:1px solid var(--bdr)">
            <th style="padding:5px 8px;text-align:left">BOT</th>
            <th style="padding:5px 8px;text-align:left">MODE</th>
            <th style="padding:5px 8px;text-align:left">INDEX</th>
            <th style="padding:5px 8px;text-align:left">OPTION</th>
            <th style="padding:5px 8px;text-align:left">EXPIRY</th>
            <th style="padding:5px 8px;text-align:right">BUY ₹</th>
            <th style="padding:5px 8px;text-align:right">SELL ₹</th>
            <th style="padding:5px 8px;text-align:right">QTY</th>
            <th style="padding:5px 8px;text-align:right">P&amp;L ₹</th>
            <th style="padding:5px 8px;text-align:left">ENTRY</th>
            <th style="padding:5px 8px;text-align:left">EXIT</th>
            <th style="padding:5px 8px;text-align:left">EXIT REASON</th>
            <th style="padding:5px 8px;text-align:left" title="OI Filter effectiveness: would enabling the OI hard-gate have helped or hurt this trade?">OI</th>
          </tr>
        </thead>
        <tbody id="th-tbody">
          <tr><td colspan="13" style="padding:20px;text-align:center;color:var(--dim)">Loading…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>
</div><!-- end #tab-pnl -->

<!-- Trade Board tab -->
<div id="tab-trade" class="tab-pane">

  <!-- ── Config bar ── -->
  <div class="tb-cbar">

    <!-- Row 1: PROD10 controls -->
    <div class="tb-cbar-row">
      <div style="font-size:9px;font-weight:700;color:var(--info);letter-spacing:1px;align-self:center;white-space:nowrap;min-width:48px">PROD10</div>

      <div class="tb-cfg-grp" title="Index to trade — NIFTY (NSE) or SENSEX / BANKNIFTY / FINNIFTY (BSE)">
        <span class="tb-lbl-sm">INDEX</span>
        <select id="tb-index" class="tb-inp-sm" onchange="tbLoadExpiries()">
          <option>NIFTY</option><option>BANKNIFTY</option><option>SENSEX</option><option>FINNIFTY</option>
        </select>
      </div>

      <div class="tb-cfg-grp" title="Weekly / monthly expiry date for the options contract">
        <span class="tb-lbl-sm">EXPIRY</span>
        <select id="tb-expiry" class="tb-inp-sm" onchange="tbLoadChain()"></select>
      </div>

      <div class="tb-cfg-grp" title="Number of lots to trade. 1 lot = 65 qty for NIFTY, 10 for SENSEX, 15 for BANKNIFTY. Lock icon freezes the value so accidental clicks don't change it.">
        <span class="tb-lbl-sm">LOTS
          <button id="tb-lots-lock" onclick="tbToggleLots()" title="Lock/unlock lots — prevents accidental changes"
            style="background:none;border:none;cursor:pointer;font-size:11px;padding:0 2px;vertical-align:middle;opacity:.45">🔓</button>
        </span>
        <input type="number" id="tb-lots" class="tb-inp-sm" value="1" min="1" max="50" oninput="tbUpdateLotInfo()">
      </div>

      <div class="tb-cfg-grp" title="Manual: continuous trailing stop from entry (like full manual mode). Quick: places a limit sell at entry + target instantly, then switches to trail if price blows past the target.">
        <span class="tb-lbl-sm">MODE</span>
        <select id="tb-p10-mode" class="tb-inp-sm" onchange="tbOnModeChange()">
          <option value="manual">Manual (full trail)</option>
          <option value="quick">Quick (target)</option>
        </select>
      </div>

      <!-- Quick mode controls — hidden until mode=quick -->
      <div class="tb-cfg-grp" id="tb-quick-pts-grp" style="display:none"
           title="Profit target for Quick mode. Bot places a limit SELL at entry + target immediately after buy. If price blows past, switches to trailing stop.&#10;PTS: target is a premium move in points.&#10;₹: enter a profit amount — converted to points using lots × lot size, then snapped to the nearest 5 paise on the actual fill price.">
        <span class="tb-lbl-sm">QK TGT</span>
        <div style="display:flex;align-items:center;gap:3px">
          <button id="tb-quick-mode-btn" onclick="tbToggleQuickTargetMode()"
                  title="Toggle target input: PTS = points of premium move · ₹ = profit amount (converted to points via lots × lot size)."
                  style="font-size:9px;padding:3px 6px;border-radius:4px;border:1px solid var(--accent);background:rgba(0,200,130,.12);color:var(--accent);cursor:pointer;font-weight:700;white-space:nowrap">PTS</button>
          <input type="number" id="tb-quick-pts" class="tb-inp-sm" value="1.5" min="0.5" max="20" step="0.5" style="width:56px" oninput="tbSaveCfg();tbUpdateQuickTargetHint()">
          <button onclick="tbUpdateQuickTarget()" title="Push updated target to the bot mid-trade — cancels current limit sell and places a new one at the new target. Only works while limit sell is still open (before trail phase)."
                  style="font-size:10px;padding:3px 7px;border-radius:4px;border:1px solid var(--accent);background:rgba(0,200,130,.12);color:var(--accent);cursor:pointer;font-weight:600">SET</button>
        </div>
        <span id="tb-quick-tgt-hint" style="font-size:8px;color:var(--muted);white-space:nowrap;align-self:center"></span>
      </div>

      <div class="tb-cfg-grp" id="tb-partial-grp" style="display:none"
           title="Partial profit booking. When ON, sells N% of your position when price reverses from a bounce above 60% of target — catches 'touching 2 then dropping back' scenarios. Remaining qty continues with trail/full target. After partial exit, SL is raised to the partial trigger level (breakeven floor).">
        <span class="tb-lbl-sm">PARTIAL</span>
        <div style="display:flex;align-items:center;gap:3px">
          <button id="tb-partial-btn" class="toggle-btn toggle-off" style="font-size:10px;padding:3px 8px"
                  onclick="tbTogglePartial()" title="Enable/disable partial profit exit">OFF</button>
          <input type="number" id="tb-partial-pct" class="tb-inp-sm" value="50" min="10" max="90" step="5"
                 title="Percentage of position to sell on partial exit (10–90%). E.g. 50 = sell half, keep half running."
                 style="width:40px" oninput="tbSaveCfg()">
          <span style="font-size:9px;color:var(--muted)">%</span>
          <button onclick="tbUpdatePartial()" title="Push updated partial settings to the running bot mid-trade."
                  style="font-size:10px;padding:3px 7px;border-radius:4px;border:1px solid var(--accent);background:rgba(0,200,130,.12);color:var(--accent);cursor:pointer;font-weight:600">SET</button>
        </div>
      </div>

      <div class="tb-cfg-grp" title="Paper trading — executes real Groww orders but no actual capital is at risk. Useful for testing strategy logic with real market prices.">
        <span class="tb-lbl-sm">PAPER</span>
        <button id="tb-paper-btn" class="toggle-btn toggle-off" style="font-size:10px;padding:3px 9px" onclick="tbTogglePaper()">OFF</button>
      </div>

      <div class="tb-cfg-grp" title="ATR-based stop loss. ON: dynamic SL calculated from recent volatility (ATR × multiplier) — adapts to market conditions. OFF: uses fixed HARD_SL_POINTS from CONFIG.">
        <span class="tb-lbl-sm">ATR-SL</span>
        <button id="tb-atr-btn" class="toggle-btn toggle-off" style="font-size:10px;padding:3px 9px" onclick="tbToggleAtr()">OFF</button>
      </div>

      <div class="tb-cfg-grp" title="ATR source — only active when ATR-SL is ON.&#10;HIST ATR: 14-period EMA ATR from 60 min of 1-min candles. Accurate, no floor. Slower (~3s fetch).&#10;TICK RNG: live high-low range from ~8-second LTP scan × multiplier. Fast, minimum 3-pt floor.">
        <span class="tb-lbl-sm">ATR SRC</span>
        <button id="tb-atr-src-btn" class="toggle-btn" style="font-size:10px;padding:3px 9px;border-color:#374151;background:rgba(55,65,81,.15);color:#4b5563;cursor:not-allowed;opacity:0.45"
                onclick="tbToggleAtrSource()" title="Turn ATR-SL ON first to enable ATR source selection.">HIST ATR</button>
      </div>

      <div class="tb-cfg-grp" title="Mock run — places real Groww orders but uses simulated (fake) LTP ticks to trigger the trailing SL logic. Safe for testing the full order flow when the market is closed. Disable for live trading.">
        <span class="tb-lbl-sm" style="color:var(--warn)">MOCK</span>
        <button id="tb-mock-btn" class="toggle-btn toggle-off" style="font-size:10px;padding:3px 9px;border-color:var(--warn)" onclick="tbToggleMock()">OFF</button>
      </div>

      <div class="tb-cfg-grp" title="Validate orders — waits for BUY order to reach EXECUTED status before placing the SELL order. Prevents selling before the buy is confirmed. Recommended ON for live trading. Disable only for paper/test runs where speed matters.">
        <span class="tb-lbl-sm" style="color:#4ade80">VALIDATE</span>
        <button id="tb-validate-btn" class="toggle-btn toggle-off" style="font-size:10px;padding:3px 9px;border-color:#4ade80" onclick="tbToggleValidate()">OFF</button>
      </div>

      <div style="width:1px;background:var(--bdr);align-self:stretch;margin:0 2px"></div>
      <div class="tb-cfg-grp" title="Quick Trade Mode: auto-calculates max affordable premium from capital, disables over-budget strikes, and places orders instantly on BUY CE/PE click — no confirmation step.">
        <button id="tb-quick-trade-btn" class="toggle-btn toggle-off"
                onclick="tbToggleQuickTrade()"
                style="font-size:10px;padding:3px 9px;border-color:#f59e0b;white-space:nowrap">⚡ Quick Trade</button>
      </div>
      <div class="tb-cfg-grp" style="justify-content:center">
        <button onclick="tbStartProd10()" id="tb-start-p10-btn"
                title="Launch PROD10 bot in a new Terminal window"
                style="padding:4px 14px;font-size:10px;font-weight:700;border-radius:6px;cursor:pointer;
                       background:linear-gradient(135deg,#1d4ed8,#3b82f6);color:#fff;border:none;
                       white-space:nowrap">▶ Start PROD10</button>
      </div>
    </div>

    <!-- Quick Trade config row — hidden until Quick Trade Mode is ON -->
    <div id="tb-quick-trade-row" class="tb-cbar-row" style="display:none;background:rgba(245,158,11,.05);border-top:1px solid rgba(245,158,11,.2)">
      <div style="font-size:9px;font-weight:700;color:#f59e0b;letter-spacing:1px;align-self:center;white-space:nowrap;min-width:48px">⚡ QUICK</div>
      <div class="tb-cfg-grp" title="Fetch F&amp;O Buy Balance from Groww API, or enter total available capital manually">
        <span class="tb-lbl-sm" style="color:#f59e0b">CAPITAL SRC</span>
        <select id="tb-cap-source" class="tb-inp-sm" onchange="tbOnCapSourceChange()">
          <option value="api">Fetch from API</option>
          <option value="manual">Pass Manually</option>
        </select>
      </div>
      <div class="tb-cfg-grp" id="tb-cap-fetch-grp">
        <button onclick="tbFetchCapital()" id="tb-cap-fetch-btn"
                style="font-size:10px;padding:3px 9px;border-radius:4px;border:1px solid #f59e0b;background:rgba(245,158,11,.15);color:#f59e0b;cursor:pointer;font-weight:600">FETCH CAPITAL</button>
      </div>
      <div class="tb-cfg-grp" id="tb-cap-manual-grp" style="display:none" title="Total capital available to trade (e.g. 260000)">
        <span class="tb-lbl-sm" style="color:#f59e0b">CAPITAL ₹</span>
        <input type="number" id="tb-cap-manual" class="tb-inp-sm" placeholder="260000" min="1000" step="1000" style="width:80px" oninput="tbOnManualCapital()">
      </div>
      <div class="tb-cfg-grp" id="tb-cap-display-grp" style="display:none">
        <span class="tb-lbl-sm" style="color:#f59e0b">CAPITAL</span>
        <span id="tb-cap-display" style="font-size:11px;color:#f59e0b;font-weight:700;font-family:'JetBrains Mono',monospace">—</span>
      </div>
      <div class="tb-cfg-grp" id="tb-max-prem-grp" style="display:none" title="Max premium per share you can afford based on capital ÷ (lots × lot_size)">
        <span class="tb-lbl-sm" style="color:#10b981">MAX PREM</span>
        <span id="tb-max-prem-display" style="font-size:11px;color:#10b981;font-weight:700;font-family:'JetBrains Mono',monospace">—</span>
      </div>
      <div class="tb-cfg-grp" id="tb-max-prem-calc-grp" style="display:none">
        <span id="tb-max-prem-calc" style="font-size:9px;color:var(--dim)"></span>
      </div>
    </div>

    <!-- Row 2: Momentum Auto Bot controls -->
    <div class="tb-cbar-row">
      <div style="font-size:9px;font-weight:700;color:#a855f7;letter-spacing:1px;align-self:center;white-space:nowrap;min-width:48px">⚡ AUTO</div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm">INDEX</span>
        <select id="mb-index" class="tb-inp-sm" onchange="mbLoadExpiries()">
          <option>NIFTY</option><option>BANKNIFTY</option><option>SENSEX</option><option>FINNIFTY</option>
        </select>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm">EXPIRY</span>
        <select id="mb-expiry" class="tb-inp-sm"></select>
      </div>
      <div class="tb-cfg-grp">
        <span class="tb-lbl-sm">LOTS
          <button id="mb-lots-lock" onclick="mbToggleLots()" title="Lock lots value"
            style="background:none;border:none;cursor:pointer;font-size:11px;padding:0 2px;vertical-align:middle;opacity:.45">🔓</button>
        </span>
        <input type="number" id="mb-lots" class="tb-inp-sm" value="1" min="1" max="50">
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm">MODE</span>
        <select id="mb-exit-mode" class="tb-inp-sm">
          <option value="manual">Manual (trail)</option>
          <option value="quick">Quick (target)</option>
        </select>
      </div>
      <!-- TRADE MODE toggles — aligned under PROD10's PAPER/ATR-SL/MOCK section -->
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" style="color:#a855f7">TRADE MODE</span>
        <div style="display:flex;gap:3px">
          <button id="mb-mode-paper" class="mb-mode-btn mb-on-paper" onclick="mbSetMode('paper')">PAPER</button>
          <button id="mb-mode-mock"  class="mb-mode-btn"             onclick="mbSetMode('mock')">MOCK</button>
          <button id="mb-mode-live"  class="mb-mode-btn"             onclick="mbSetMode('live')">LIVE</button>
        </div>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" style="color:#4ade80">VALIDATE</span>
        <button id="mb-validate-btn" class="toggle-btn toggle-on" style="font-size:10px;padding:3px 9px;border-color:#4ade80;background:rgba(74,222,128,.15);color:#4ade80" onclick="mbToggleValidate()" title="Wait for BUY order EXECUTED status before placing SELL — keep ON for live trading">ON</button>
      </div>
      <div style="width:1px;background:var(--bdr);align-self:stretch;margin:0 2px"></div>
      <!-- PREMIUM + STRIKES after separator -->
      <div class="tb-cfg-grp"><span class="tb-lbl-sm">PREMIUM ₹</span>
        <div class="mb-prem-grp">
          <input type="number" id="mb-prem-min" class="tb-inp-sm" value="50"  min="1"   max="5000" title="Min premium">
          <span style="font-size:10px;color:var(--dim)">–</span>
          <input type="number" id="mb-prem-max" class="tb-inp-sm" value="200" min="10"  max="5000" title="Max premium">
        </div>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm">STRIKES ±</span>
        <input type="number" id="mb-strikes" class="tb-inp-sm" value="3" min="1" max="10" title="ATM ± N strikes to scan" style="width:44px">
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" title="Scan window in seconds (lower = enter earlier in the move)">SCAN SEC</span>
        <input type="number" id="mb-scan-sec" class="tb-inp-sm" value="10" min="5" max="30" title="Observation window (seconds) — 10s enters earlier, 20s waits for full confirmation" style="width:44px">
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" title="Trail SL poll interval (lower = less exit slippage)">POLL SEC</span>
        <input type="number" id="mb-poll-sec" class="tb-inp-sm" value="1" min="1" max="5" title="Trail SL poll interval (seconds) — 1s = tightest exit, 3s = more slippage on SL hits" style="width:44px">
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" title="Choppiness detector — detects sideways market, pauses entries when HIGH">CHOP</span>
        <button id="mb-chop-btn" class="toggle-btn toggle-on" style="font-size:10px;padding:3px 9px;border-color:#4ade80;background:rgba(74,222,128,.15);color:#4ade80" onclick="mbToggleChop()" title="Choppiness tracker ON — bot detects sideways market and pauses new entries automatically. Toggle OFF to disable.">ON</button>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" title="Stop after 2 consecutive Hard SLs — circuit breaker pauses entries for 30 min">CONS SL</span>
        <button id="mb-cons-sl-btn" class="toggle-btn toggle-on" style="font-size:10px;padding:3px 9px;border-color:#4ade80;background:rgba(74,222,128,.15);color:#4ade80" onclick="mbToggleConsSL()" title="Circuit breaker: pause entries for 30 min after N consecutive Hard SLs. Recommended ON.">ON</button>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" title="ATR-based Hard SL — dynamic SL based on ATR × multiplier">ATR SL</span>
        <button id="mb-atr-sl-btn" class="toggle-btn toggle-off" onclick="mbToggleAtrSL()" title="Dynamic Hard SL based on ATR × multiplier. OFF = fixed 8-pt Hard SL.">OFF</button>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" title="ATR source (only active when ATR SL is ON) — HIST ATR: 14-period EMA ATR from 60 min of 1-min historical candles, accurate real volatility, no 3-pt floor (PROD10 style). TICK RNG: live high-low range from 15–25 sec tick scan window × multiplier, fast but shallow, minimum 3-pt floor.">ATR SRC</span>
        <button id="mb-atr-src-btn" class="toggle-btn" style="font-size:10px;padding:3px 9px;border-color:#374151;background:rgba(55,65,81,.15);color:#4b5563;cursor:not-allowed;opacity:0.45" onclick="mbToggleAtrSource()" title="Disabled — turn ATR SL ON first. HIST ATR: 14-period EMA ATR from 1-min candles (accurate, no floor). TICK RNG: scan-window tick range × multiplier (fast, floor 3 pts).">HIST ATR</button>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" title="Min score filter — ON requires winning side score ≥0.275; OFF picks highest positive side regardless">MIN SCORE</span>
        <button id="mb-min-score-btn" class="toggle-btn toggle-on" style="font-size:10px;padding:3px 9px;border-color:#4ade80;background:rgba(74,222,128,.15);color:#4ade80" onclick="mbToggleMinScore()" title="Score filter ON — winning side needs net score ≥ 0.275 (velocity × consistency). OFF = pick highest positive score side without a floor.">ON</button>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" title="Velocity filter — ON requires best strike to clear vel% threshold; OFF picks highest-score rising strike regardless">VEL FILTER</span>
        <button id="mb-vel-filter-btn" class="toggle-btn toggle-on" style="font-size:10px;padding:3px 9px;border-color:#4ade80;background:rgba(74,222,128,.15);color:#4ade80" onclick="mbToggleVelFilter()" title="Velocity filter ON — best strike must clear velocity threshold (default 0.5%). OFF = pick highest-score rising strike on winning side regardless of velocity.">ON</button>
      </div>
      <div class="tb-cfg-grp" title="Velocity threshold — min % premium move over scan window to trigger a signal. Set manually or auto-set by VIX AUTO CONFIG.">
        <span class="tb-lbl-sm">VEL %</span>
        <div class="mb-vel-cons-badge">
          <span id="mb-vel-display" class="mb-vel-cons-val" title="Current velocity_pct threshold">0.5%</span>
        </div>
      </div>
      <div class="tb-cfg-grp" title="Consistency threshold — min % of ticks moving in signal direction. Set manually or auto-set by VIX AUTO CONFIG.">
        <span class="tb-lbl-sm">CONS %</span>
        <div class="mb-vel-cons-badge">
          <span id="mb-cons-display" class="mb-vel-cons-val" title="Current consistency_pct threshold">55%</span>
        </div>
      </div>
      <div class="tb-cfg-grp"><span class="tb-lbl-sm" style="color:#60b8f0" title="VIX Auto Config — reads India VIX, determines market regime, and sets velocity%+consistency% thresholds automatically. Refresh after every 3–4 trades to stay aligned with market conditions.">VIX AUTO</span>
        <div style="display:flex;gap:3px;align-items:center">
          <button id="mb-vix-auto-btn" class="toggle-btn toggle-off" onclick="mbVixAutoToggle()" title="VIX Auto Config — auto-sets vel% and cons% from India VIX level+trend. Enable before launching the bot.">OFF</button>
          <button id="mb-vix-refresh-btn" onclick="mbVixRefreshConfig()" title="Refresh VIX and recompute config — use after every 3–4 trades" style="display:none;font-size:11px;padding:2px 8px;border-radius:12px;cursor:pointer;border:1px solid #60b8f0;background:rgba(96,184,240,.15);color:#60b8f0;font-weight:700">↻</button>
        </div>
      </div>
      <div class="tb-cfg-grp" style="justify-content:center">
        <button onclick="mbStartAutoBot()" id="mb-start-btn" class="mb-launch-btn">🚀 Auto Bot</button>
      </div>
      <div style="width:1px;background:var(--bdr);align-self:stretch;margin:0 4px"></div>
      <!-- Capital calculator -->
      <div class="tb-cfg-grp">
        <span class="tb-lbl-sm" style="color:#f59e0b">CAPITAL CALC</span>
        <div style="display:flex;align-items:center;gap:6px">
          <input type="number" id="mb-cap-lots" value="1" min="1" max="50" style="width:42px;font-size:10px;padding:2px 4px" title="Lots for capital calc">
          <span style="font-size:9px;color:var(--dim)">L</span>
          <input type="number" id="mb-cap-prem" value="200" min="1" max="5000" style="width:52px;font-size:10px;padding:2px 4px" title="Target premium ₹">
          <span style="font-size:9px;color:var(--dim)">₹</span>
          <button id="mb-cap-btn" class="mb-cap-btn" onclick="mbFetchCapital()">💰 Show</button>
          <div style="display:flex;flex-direction:column;gap:2px;min-width:180px">
            <div id="mb-cap-row1" style="display:none;font-size:10px;font-family:'JetBrains Mono',monospace">
              <span style="color:var(--dim)">Buy Power: </span>
              <span id="mb-cap-val" style="color:var(--txt);font-weight:700"></span>
              <span style="color:var(--dim);margin-left:4px">Cash: </span>
              <span id="mb-cap-cash" style="color:var(--dim)"></span>
            </div>
            <div id="mb-cap-row2" style="display:none;font-size:10px;font-family:'JetBrains Mono',monospace">
              <span id="mb-cap-maxprem" style="font-weight:700"></span>
              <span id="mb-cap-need"    style="margin-left:6px;font-weight:700"></span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- VIX Auto Config status panel — shown when VIX AUTO toggle is ON -->
    <div id="mb-vix-status-panel" style="display:none;background:rgba(96,184,240,.07);border:1px solid rgba(96,184,240,.28);border-radius:6px;margin-top:6px;font-size:10px;font-family:'JetBrains Mono',monospace;overflow:hidden">
      <!-- Clickable header row -->
      <div onclick="mbVixTogglePanel()" style="display:flex;align-items:center;gap:8px;padding:5px 12px;cursor:pointer;user-select:none;border-bottom:1px solid rgba(96,184,240,.15)" id="mb-vix-panel-header">
        <span style="color:#60b8f0;font-weight:700;letter-spacing:.5px">⚡ VIX AUTO CONFIG</span>
        <span id="mb-vix-status-text" style="color:var(--dim);font-size:9px;flex:1">fetching…</span>
        <span id="mb-vix-chevron" style="color:#60b8f0;font-size:11px;transition:transform .2s">▲</span>
      </div>
      <!-- Collapsible body -->
      <div id="mb-vix-panel-body" style="padding:10px 12px;line-height:1.5"></div>
    </div>

  </div>

  <!-- ── Action bar ── -->
  <div class="tb-abar">
    <div id="tb-selected-display" style="font-size:12px;color:var(--dim)">← Click CE / PE on any strike to select</div>
    <div id="chain-lotinfo" style="font-size:10px;color:var(--dim)"></div>
    <div id="mb-lotinfo" style="font-size:10px;color:var(--dim);margin-left:12px;padding-left:12px;border-left:1px solid var(--bdr)"></div>
    <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
      <input type="hidden" id="tb-sym-inp">
      <input type="hidden" id="tb-exch-inp" value="NSE">
      <button id="tb-p10-btn" onclick="tbSendToProd10()" disabled
              style="padding:7px 28px;font-size:13px;font-weight:700;border-radius:8px;cursor:pointer;
                     background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;border:none;
                     opacity:.55;transition:opacity .2s,transform .15s">SELECT A STRIKE</button>
    </div>
  </div>

  <!-- ── Main: chain (left) + drag handle + status+log (right) ── -->
  <div class="tb-main" id="tb-main">
  <div class="tb-chain-side">
  <div class="tb-chain-wrap">
    <div class="tb-chain-hdr">
      <div style="display:flex;align-items:center;gap:12px">
        <span style="font-size:10px;font-weight:700;letter-spacing:1px;color:var(--dim)">OPTION CHAIN</span>
        <span id="chain-spot" style="color:var(--info);font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700"></span>
        <span id="chain-refresh-badge" style="font-size:9px;padding:2px 8px;border-radius:10px;display:none"></span>
        <!-- Quick Trade max premium badge — shown when QTM is ON -->
        <span id="chain-qt-badge" style="display:none;font-size:9px;padding:2px 8px;border-radius:10px;background:rgba(245,158,11,.15);border:1px solid #f59e0b;color:#f59e0b;font-weight:700;font-family:'JetBrains Mono',monospace"></span>
      </div>
      <div style="display:flex;align-items:center;gap:10px;font-size:10px;color:var(--dim)">
        <button onclick="tbLoadChain()" style="background:none;border:1px solid var(--bdr);color:var(--dim);border-radius:4px;padding:2px 8px;cursor:pointer;font-size:9px">↺ Refresh</button>
      </div>
    </div>
    <!-- Column headers — rendered by tbRenderChainHeaders() -->
    <div class="tb-chain-sub-hdr" id="chain-col-hdr"></div>
    <div class="tb-chain-body" id="chain-list">
      <div style="text-align:center;color:var(--dim);padding:30px;font-size:12px">Select index &amp; expiry above to load chain</div>
    </div>
  </div><!-- end chain-wrap -->
  </div><!-- end chain-side -->

  <!-- Drag handle -->
  <div class="tb-drag-handle" id="tb-drag-handle" title="Drag to resize"></div>

  <!-- Right panel: trade status on top, log on bottom -->
  <div class="tb-right-panel">

    <!-- AUTO MODE v2 Status Panel -->
    <div id="tb-auto-status-panel" style="flex-shrink:0;border-bottom:1px solid var(--bdr);display:none">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:5px 10px;
                  background:linear-gradient(90deg,rgba(124,58,237,.15),transparent)">
        <span style="font-size:9px;letter-spacing:1px;color:#a855f7;font-weight:700">🤖 AUTO MODE v2</span>
        <span id="tb-auto-mode-badge" style="font-size:9px;padding:1px 7px;border-radius:8px;
              background:rgba(124,58,237,.2);color:#a855f7;font-weight:700">STARTING</span>
      </div>
      <div id="tb-auto-status-body" style="padding:6px 10px 8px;font-size:10px;font-family:'JetBrains Mono',monospace;line-height:1.6">
      </div>
    </div>

    <!-- Trade Status / P&L (top of right panel) -->
    <div id="tb-trade-status" style="padding:10px;flex-shrink:0;overflow-y:auto;max-height:55%">
      <div style="color:var(--dim);font-size:11px;padding:10px 4px;line-height:1.7">
        <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:8px">PROD10 STATUS</div>
        Select a strike and click BUY to send to PROD10.<br><br>
        <span style="color:var(--bdr);font-size:10px">
          Trailing SL is managed by PROD10 bot.<br>
          Logs stream below in real time.
        </span>
      </div>
    </div>

    <!-- Session Log -->
    <div style="flex:1;display:flex;flex-direction:column;border-top:1px solid var(--bdr);overflow:hidden;min-height:0">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:0 10px;
                  background:var(--bg2);border-bottom:1px solid var(--bdr);flex-shrink:0">
        <div style="display:flex;gap:0">
          <button id="tb-log-tab-p10"  class="tb-log-tab active" onclick="tbSwitchLogTab('p10')">PROD10</button>
          <button id="tb-log-tab-auto" class="tb-log-tab"        onclick="tbSwitchLogTab('auto')">⚡ AUTO BOT</button>
        </div>
        <button onclick="tbClearLog()" style="background:none;border:1px solid var(--bdr);color:var(--dim);
                border-radius:4px;padding:1px 7px;cursor:pointer;font-size:9px">Clear</button>
      </div>
      <div id="tb-log"      style="overflow-y:auto;flex:1;padding:4px 8px;font-size:10px;display:block"></div>
      <!-- OI Filter effectiveness summary bar (shown only in Auto tab) -->
      <div id="tb-oi-summary" style="display:none;flex-shrink:0;padding:4px 10px;background:rgba(255,255,255,.03);border-bottom:1px solid var(--bdr);font-size:9.5px;font-family:'JetBrains Mono',monospace;display:none">
        <span style="color:var(--dim);letter-spacing:.5px;margin-right:8px">OI FILTER (today):</span>
        <span id="tb-oi-aw"  title="OI Aligned + Trade Won"   style="color:#4ade80;margin-right:10px">✅ <b id="tb-oi-aw-n">0</b> Aligned Win</span>
        <span id="tb-oi-al"  title="OI Aligned but Trade Lost" style="color:#f59e0b;margin-right:10px">⚠️ <b id="tb-oi-al-n">0</b> Aligned Loss</span>
        <span id="tb-oi-ow"  title="OI Opposed but Won (filter would block)" style="color:#f59e0b;margin-right:10px">🚫 <b id="tb-oi-ow-n">0</b> Opposed Win</span>
        <span id="tb-oi-ol"  title="OI Opposed + Lost (filter would save)"  style="color:#60a5fa;margin-right:10px">🛡️ <b id="tb-oi-ol-n">0</b> Opposed Loss</span>
        <span id="tb-oi-nt"  title="OI Neutral / Stale" style="color:#6b7280">➖ <b id="tb-oi-nt-n">0</b> Neutral</span>
        <span id="tb-oi-verdict" style="margin-left:16px;font-weight:600"></span>
      </div>
      <div id="tb-auto-log" style="overflow-y:auto;flex:1;padding:4px 8px;font-size:10px;display:none"></div>
    </div>

    <!-- Trade History -->
    <div style="flex-shrink:0;border-top:1px solid var(--bdr);display:flex;flex-direction:column;max-height:220px;min-height:90px;">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 10px;
                  background:var(--bg2);border-bottom:1px solid var(--bdr);flex-shrink:0">
        <span style="font-size:10px;letter-spacing:1px;color:var(--dim);font-weight:600">TRADE HISTORY <span id="th-count" style="color:var(--info)"></span></span>
        <span id="th-session-pnl" style="font-size:10px;font-weight:700;font-family:'JetBrains Mono',monospace"></span>
      </div>
      <div style="overflow-y:auto;flex:1;">
        <table id="th-table" style="width:100%;border-collapse:collapse;font-size:10px;font-family:'JetBrains Mono',monospace;">
          <thead>
            <tr style="color:var(--dim);font-size:9px;letter-spacing:.4px;position:sticky;top:0;background:var(--bg2);">
              <th style="text-align:left;padding:3px 6px;font-weight:600">TIME</th>
              <th style="text-align:left;padding:3px 4px;font-weight:600">OPTION</th>
              <th style="text-align:right;padding:3px 4px;font-weight:600">BUY</th>
              <th style="text-align:right;padding:3px 4px;font-weight:600">SELL</th>
              <th style="text-align:right;padding:3px 4px;font-weight:600">QTY</th>
              <th style="text-align:right;padding:3px 6px;font-weight:600">P&amp;L</th>
            </tr>
          </thead>
          <tbody id="th-body">
            <tr><td colspan="6" style="text-align:center;color:var(--dim);padding:14px;font-size:10px">No trades yet this session</td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </div><!-- end right-panel -->
  </div><!-- end tb-main -->

</div><!-- end #tab-trade -->

<!-- Performance / Proof-of-Concept tab -->
<div id="tab-perf" class="tab-pane">
<div style="max-width:1400px;margin:0 auto">

  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
    <div>
      <div style="font-size:14px;font-weight:700;color:var(--txt)">📈 Performance &amp; Proof of Concept</div>
      <div style="font-size:11px;color:var(--dim);margin-top:2px">Verify that bot predictions played out in real market</div>
    </div>
    <div style="font-size:10px;color:var(--dim)" id="perf-last-ts">—</div>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">

    <div class="card">
      <div class="ctitle">🎯 S/R Level Respect Log</div>
      <div style="font-size:10px;color:var(--dim);margin-bottom:8px">Fib levels flagged as NEAR → did market reverse or break through?</div>
      <div style="overflow-x:auto">
        <table class="perf-table">
          <thead><tr>
            <th>Time</th><th>Level</th><th>Label</th><th>Type</th><th>Near at</th><th>±pts</th><th>Result</th>
          </tr></thead>
          <tbody id="perf-sr-tbody">
            <tr><td colspan="7" style="color:var(--dim);padding:14px;text-align:center">Loading…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="ctitle">📊 Option Signal Outcomes</div>
      <div style="font-size:10px;color:var(--dim);margin-bottom:8px">CE/PE signals → how far did spot move in signal direction?</div>
      <div style="overflow-x:auto">
        <table class="perf-table">
          <thead><tr>
            <th>Time</th><th>Dir</th><th>Spot</th><th>Reason</th><th>Tgt</th><th>SL</th><th>Max Move</th><th>Result</th>
          </tr></thead>
          <tbody id="perf-sig-tbody">
            <tr><td colspan="8" style="color:var(--dim);padding:14px;text-align:center">Loading…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </div>

</div>
</div><!-- end #tab-perf -->

<!-- OI Intelligence tab -->
<div id="tab-oi" class="tab-pane">
<div style="max-width:1400px;margin:0 auto;padding:14px 18px">

  <!-- Header row -->
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px">
    <div>
      <div style="font-size:15px;font-weight:700;color:var(--txt)">🔬 OI Intelligence</div>
      <div style="font-size:11px;color:var(--dim);margin-top:2px">Live option chain writer activity + AI market view — powered by <b>calculate_oi_pcr.py</b></div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <div style="font-size:10px;color:var(--dim)" id="oi-tab-age">—</div>
      <button type="button" onclick="oiChartOpen()" class="oi-chart-open-btn"
        title="OI Intraday Chart — CE OI, PE OI, PCR, Spot price over time">
        📊 OI Chart
      </button>
      <button type="button" id="oi-ai-toggle-btn" onclick="toggleOIAI()"
        style="font-size:11px;padding:5px 12px;background:var(--bg3);border:1px solid var(--bdr);border-radius:6px;color:var(--dim);cursor:pointer;user-select:none">
        🤖 OI AI: OFF
      </button>
    </div>
  </div>

  <!-- OI Pulse row -->
  <div style="display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:8px;margin-bottom:14px" id="oi-pulse-cards">
    <div class="card" style="text-align:center" id="oi-pcr-all-card" data-oi-tip="pcr_all">
      <div style="font-size:10px;color:var(--dim);margin-bottom:4px">PCR ALL STRIKES</div>
      <div style="font-size:22px;font-weight:700;font-family:'JetBrains Mono',monospace" id="oi-pcr-all">—</div>
    </div>
    <div class="card" style="text-align:center" id="oi-pcr-atm-card" data-oi-tip="pcr_atm">
      <div style="font-size:10px;color:var(--dim);margin-bottom:4px">PCR ATM ±3</div>
      <div style="font-size:22px;font-weight:700;font-family:'JetBrains Mono',monospace" id="oi-pcr-atm">—</div>
    </div>
    <div class="card" style="text-align:center" data-oi-tip="oi_sentiment">
      <div style="font-size:10px;color:var(--dim);margin-bottom:4px">OI SENTIMENT</div>
      <div style="font-size:16px;font-weight:700" id="oi-sentiment">—</div>
    </div>
    <div class="card" style="text-align:center" data-oi-tip="writer_bias">
      <div style="font-size:10px;color:var(--dim);margin-bottom:4px">WRITER BIAS (tick)</div>
      <div style="font-size:16px;font-weight:700" id="oi-writer-bias">—</div>
    </div>
    <div class="card" style="text-align:center" data-oi-tip="total_ce_oi">
      <div style="font-size:10px;color:var(--dim);margin-bottom:4px">TOTAL CE OI</div>
      <div style="font-size:14px;font-weight:600;font-family:'JetBrains Mono',monospace" id="oi-total-ce">—</div>
      <div style="font-size:10px;color:var(--dim)" id="oi-chg-ce">—</div>
    </div>
    <div class="card" style="text-align:center" data-oi-tip="total_pe_oi">
      <div style="font-size:10px;color:var(--dim);margin-bottom:4px">TOTAL PE OI</div>
      <div style="font-size:14px;font-weight:600;font-family:'JetBrains Mono',monospace" id="oi-total-pe">—</div>
      <div style="font-size:10px;color:var(--dim)" id="oi-chg-pe">—</div>
    </div>
    <div class="card" style="text-align:center" id="oi-max-pain-card" data-oi-tip="max_pain">
      <div style="font-size:10px;color:var(--dim);margin-bottom:4px">MAX PAIN</div>
      <div style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace" id="oi-max-pain">—</div>
      <div style="font-size:10px;color:var(--dim)" id="oi-max-pain-dist">spot vs max pain</div>
    </div>
    <div class="card" style="text-align:center" data-oi-tip="vol_pcr">
      <div style="font-size:10px;color:var(--dim);margin-bottom:4px">VOL PCR <span style="font-size:8px">(intraday)</span></div>
      <div style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace" id="oi-vol-pcr">—</div>
      <div style="font-size:10px;color:var(--dim)" id="oi-atm-iv">ATM IV: CE —  PE —</div>
    </div>
    <div class="card" style="text-align:center" id="oi-res-card" data-oi-tip="resistance_wall">
      <div style="font-size:10px;color:var(--bear);margin-bottom:4px;font-weight:600">RESISTANCE WALL 🔴</div>
      <div style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--bear)" id="oi-res-strike">—</div>
      <div style="font-size:10px;color:var(--dim)" id="oi-res-oi">CE OI —</div>
      <div id="oi-res-breakout" style="display:none;margin-top:3px;font-size:8.5px;font-weight:700;padding:2px 5px;border-radius:3px;letter-spacing:.3px"></div>
      <div style="font-size:9px;color:var(--bear);margin-top:2px">sell below this</div>
    </div>
    <div class="card" style="text-align:center" id="oi-sup-card" data-oi-tip="support_floor">
      <div style="font-size:10px;color:var(--bull);margin-bottom:4px;font-weight:600">SUPPORT FLOOR 🟢</div>
      <div style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--bull)" id="oi-sup-strike">—</div>
      <div style="font-size:10px;color:var(--dim)" id="oi-sup-oi">PE OI —</div>
      <div id="oi-sup-breakdown" style="display:none;margin-top:3px;font-size:8.5px;font-weight:700;padding:2px 5px;border-radius:3px;letter-spacing:.3px"></div>
      <div style="font-size:9px;color:var(--bull);margin-top:2px">buy above this</div>
    </div>
  </div>

  <!-- OI Range Band -->
  <div class="card" style="margin-bottom:14px;padding:12px 16px" id="oi-range-card" data-oi-tip="oi_range_band">
    <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:8px">OI RANGE BAND — Strongest Support ↔ Resistance (from total OI)</div>
    <div style="display:flex;align-items:center;gap:8px;font-size:11px;font-family:'JetBrains Mono',monospace">
      <div style="text-align:center;min-width:70px">
        <div style="font-size:9px;color:var(--bull)">SUPPORT</div>
        <div id="oi-range-sup" style="font-weight:700;color:var(--bull)">—</div>
        <div id="oi-range-sup-oi" style="font-size:9px;color:var(--dim)">PE OI —</div>
      </div>
      <div style="flex:1;position:relative;height:22px">
        <div style="height:4px;background:linear-gradient(90deg,var(--bull),var(--bg3),var(--bear));border-radius:2px;margin-top:9px"></div>
        <div id="oi-range-spot-marker" style="position:absolute;top:0;transform:translateX(-50%);font-size:10px;color:var(--info);font-weight:700;white-space:nowrap">▼ —</div>
      </div>
      <div style="text-align:center;min-width:70px">
        <div style="font-size:9px;color:var(--bear)">RESISTANCE</div>
        <div id="oi-range-res" style="font-weight:700;color:var(--bear)">—</div>
        <div id="oi-range-res-oi" style="font-size:9px;color:var(--dim)">CE OI —</div>
      </div>
    </div>
    <div id="oi-range-verdict" style="margin-top:8px;font-size:11px;color:var(--dim);text-align:center">—</div>
    <!-- Top 3 each -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px;font-size:10px;font-family:'JetBrains Mono',monospace">
      <div>
        <div style="font-size:9px;color:var(--bear);margin-bottom:4px">TOP CE OI STRIKES (Resistance)</div>
        <div id="oi-res-list" style="color:var(--dim)">—</div>
      </div>
      <div>
        <div style="font-size:9px;color:var(--bull);margin-bottom:4px">TOP PE OI STRIKES (Support)</div>
        <div id="oi-sup-list" style="color:var(--dim)">—</div>
      </div>
    </div>
  </div>

  <!-- Market Direction Signal Banner -->
  <div class="card" style="margin-bottom:14px;padding:16px 20px" id="oi-signal-banner" data-oi-tip="market_signal">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
      <div>
        <div style="font-size:9px;color:var(--dim);letter-spacing:1px;margin-bottom:4px">MARKET DIRECTION SIGNAL <span style="opacity:.5">(10-factor score)</span></div>
        <div id="oi-market-signal" style="font-size:26px;font-weight:800;letter-spacing:1px;color:var(--dim)">— AWAITING DATA</div>
      </div>
      <div style="display:flex;gap:20px;align-items:center">
        <div style="text-align:center" data-oi-tip="bull_score">
          <div style="font-size:9px;color:var(--dim);margin-bottom:2px">BULL SCORE</div>
          <div id="oi-bull-score-v2" style="font-size:28px;font-weight:800;color:var(--bull);font-family:'JetBrains Mono',monospace">—</div>
          <div style="font-size:8px;color:var(--dim)">/100</div>
        </div>
        <div style="width:1px;height:48px;background:var(--bdr)"></div>
        <div style="text-align:center" data-oi-tip="momentum_score">
          <div style="font-size:9px;color:var(--dim);margin-bottom:2px">MOMENTUM</div>
          <div id="oi-momentum-score" style="font-size:28px;font-weight:800;color:var(--warn);font-family:'JetBrains Mono',monospace">—</div>
          <div style="font-size:8px;color:var(--dim)">/100</div>
        </div>
        <div style="width:1px;height:48px;background:var(--bdr)"></div>
        <div style="text-align:center" data-oi-tip="bear_score">
          <div style="font-size:9px;color:var(--dim);margin-bottom:2px">BEAR SCORE</div>
          <div id="oi-bear-score-v2" style="font-size:28px;font-weight:800;color:var(--bear);font-family:'JetBrains Mono',monospace">—</div>
          <div style="font-size:8px;color:var(--dim)">/100</div>
        </div>
      </div>
    </div>
    <div style="margin-top:12px;height:7px;background:var(--bg3);border-radius:4px;overflow:hidden;position:relative">
      <div id="oi-signal-bull-bar" style="height:100%;background:var(--bull);position:absolute;left:0;width:0%;transition:width .5s ease;border-radius:4px 0 0 4px"></div>
      <div id="oi-signal-bear-bar" style="height:100%;background:var(--bear);position:absolute;right:0;width:0%;transition:width .5s ease;border-radius:0 4px 4px 0"></div>
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:3px;font-size:8px;color:var(--dim)">
      <span>🟢 Bullish pressure</span><span>🔴 Bearish pressure</span>
    </div>
  </div>

  <!-- Signal Components -->
  <div class="card" style="margin-bottom:14px" data-oi-tip="signal_breakdown">
    <div class="ctitle" style="margin-bottom:10px">📊 Signal Breakdown <span style="font-size:10px;color:var(--dim);font-weight:400">— 10 independent factors (PCR · OI · Writers · Smart Money · IV · Volume · Buildup)</span></div>
    <div id="oi-signal-components" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:5px">
      <div style="color:var(--dim);font-size:11px;padding:8px">Awaiting OI data…</div>
    </div>
  </div>

  <!-- Smart Money Flow: CE vs PE OI additions -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">
    <div class="card" id="oi-sm-ce-card" data-oi-tip="smart_money_ce">
      <div class="ctitle" style="margin-bottom:8px">🔴 Smart Money CE Additions <span style="font-size:9px;color:var(--bear);font-weight:400">→ resistance building (BUY PE)</span></div>
      <table style="width:100%;font-size:11px;font-family:'JetBrains Mono',monospace;border-collapse:collapse">
        <thead><tr style="font-size:9px;color:var(--dim);border-bottom:1px solid var(--bdr)">
          <th style="text-align:right;padding:3px 6px">STRIKE</th>
          <th style="text-align:right;padding:3px 6px" data-oi-tip="sm_session_chg">SESSION OI CHG</th>
          <th style="text-align:right;padding:3px 6px">LTP ₹</th>
          <th style="text-align:right;padding:3px 6px">VOLUME</th>
        </tr></thead>
        <tbody id="sm-ce-tbody">
          <tr><td colspan="4" style="color:var(--dim);text-align:center;padding:10px">No v3 data yet</td></tr>
        </tbody>
      </table>
    </div>
    <div class="card" id="oi-sm-pe-card" data-oi-tip="smart_money_pe">
      <div class="ctitle" style="margin-bottom:8px">🟢 Smart Money PE Additions <span style="font-size:9px;color:var(--bull);font-weight:400">→ support building (BUY CE)</span></div>
      <table style="width:100%;font-size:11px;font-family:'JetBrains Mono',monospace;border-collapse:collapse">
        <thead><tr style="font-size:9px;color:var(--dim);border-bottom:1px solid var(--bdr)">
          <th style="text-align:right;padding:3px 6px">STRIKE</th>
          <th style="text-align:right;padding:3px 6px">SESSION OI CHG</th>
          <th style="text-align:right;padding:3px 6px">LTP ₹</th>
          <th style="text-align:right;padding:3px 6px">VOLUME</th>
        </tr></thead>
        <tbody id="sm-pe-tbody">
          <tr><td colspan="4" style="color:var(--dim);text-align:center;padding:10px">No v3 data yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Call Writing + Put Writing detection -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">
    <div class="card" id="oi-call-writing-card" data-oi-tip="call_writing">
      <div class="ctitle" style="margin-bottom:8px">📞 Call Writing Detection <span style="font-size:9px;color:var(--bear);font-weight:400">(CE OI↑ + LTP↓ = resistance)</span></div>
      <div id="oi-call-writing-rows" style="font-size:11px;font-family:'JetBrains Mono',monospace">
        <div style="color:var(--dim);text-align:center;padding:10px">Awaiting 2nd tick for LTP comparison</div>
      </div>
    </div>
    <div class="card" id="oi-put-writing-card" data-oi-tip="put_writing">
      <div class="ctitle" style="margin-bottom:8px">📍 Put Writing Detection <span style="font-size:9px;color:var(--bull);font-weight:400">(PE OI↑ + LTP↓ = support)</span></div>
      <div id="oi-put-writing-rows" style="font-size:11px;font-family:'JetBrains Mono',monospace">
        <div style="color:var(--dim);text-align:center;padding:10px">Awaiting 2nd tick for LTP comparison</div>
      </div>
    </div>
  </div>

  <!-- ATM Momentum Signal ("BUY NOW") -->
  <div class="card" id="oi-atm-momentum-card" style="margin-bottom:14px;padding:16px 20px;border-left:4px solid var(--dim)" data-oi-tip="atm_momentum">
    <div class="ctitle" style="margin-bottom:8px">⚡ ATM Momentum Signal
      <span style="font-size:9px;color:var(--dim);font-weight:400">(ATM CE/PE: LTP change + OI change = real buyers?)</span>
    </div>
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <div id="oi-momentum-action" style="font-size:18px;font-weight:700;letter-spacing:.5px">⏳ WAIT</div>
      <div style="flex:1;min-width:160px">
        <div id="oi-momentum-reason" style="font-size:11px;color:var(--dim);margin-bottom:6px">Awaiting 2nd tick…</div>
        <div style="display:flex;gap:20px;font-size:11px;font-family:'JetBrains Mono',monospace">
          <span>CE score: <span id="oi-ce-momentum-score" style="color:var(--bull)">—</span></span>
          <span>PE score: <span id="oi-pe-momentum-score" style="color:var(--bear)">—</span></span>
          <span>ATM: <span id="oi-momentum-atm">—</span></span>
        </div>
      </div>
      <div id="oi-momentum-targets" style="font-size:11px;font-family:'JetBrains Mono',monospace;text-align:right;display:none">
        <div>Target: <span id="oi-momentum-target" style="color:var(--bull);font-weight:600">—</span></div>
        <div>Stop: <span id="oi-momentum-stop" style="color:var(--bear);font-weight:600">—</span></div>
      </div>
    </div>
  </div>

  <!-- Per-Strike Buildup table (ATM ±3) -->
  <div class="card" style="margin-bottom:14px" data-oi-tip="strike_buildup">
    <div class="ctitle" style="margin-bottom:10px">🏗️ Per-Strike Buildup <span style="font-size:9px;color:var(--dim);font-weight:400">(ATM ±3 — tick-over-tick LTP + OI direction)</span></div>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:11px;font-family:'JetBrains Mono',monospace">
        <thead>
          <tr style="color:var(--dim);font-size:9px;letter-spacing:.5px;border-bottom:1px solid var(--bdr)">
            <th style="text-align:right;padding:4px 8px">STRIKE</th>
            <th style="text-align:center;padding:4px 8px">CE BUILDUP</th>
            <th style="text-align:right;padding:4px 8px">CE OI Δ</th>
            <th style="text-align:right;padding:4px 8px">CE LTP</th>
            <th style="text-align:center;padding:4px 8px">PE BUILDUP</th>
            <th style="text-align:right;padding:4px 8px">PE OI Δ</th>
            <th style="text-align:right;padding:4px 8px">PE LTP</th>
          </tr>
        </thead>
        <tbody id="oi-buildup-tbody">
          <tr><td colspan="7" style="color:var(--dim);text-align:center;padding:12px">Awaiting 2nd tick for LTP comparison…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- IV Change + PCR Change side by side -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">
    <div class="card" id="oi-iv-card" data-oi-tip="iv_changes">
      <div class="ctitle" style="margin-bottom:8px">📈 IV Change Spikes <span style="font-size:9px;color:var(--dim);font-weight:400">(Δ &gt;1.5% in one tick = event)</span></div>
      <div id="oi-iv-changes" style="font-size:11px;font-family:'JetBrains Mono',monospace">
        <div style="color:var(--dim);text-align:center;padding:10px">No spikes detected yet</div>
      </div>
    </div>
    <div class="card" id="oi-pcr-change-card" data-oi-tip="pcr_change">
      <div class="ctitle" style="margin-bottom:8px">🔄 PCR Change <span style="font-size:9px;color:var(--dim);font-weight:400">(tick-over-tick delta)</span></div>
      <div id="oi-pcr-change" style="font-size:13px;font-family:'JetBrains Mono',monospace;min-height:48px;display:flex;align-items:center;justify-content:center;color:var(--dim)">
        Awaiting 2nd tick…
      </div>
    </div>
  </div>

  <!-- Main grid: Writer Activity left, ATM Strike table right -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">

    <!-- Writer Activity card -->
    <div class="card" data-oi-tip="writer_activity">
      <div class="ctitle" style="margin-bottom:10px">✍️ Writer Activity <span style="font-size:10px;color:var(--dim);font-weight:400">(tick-over-tick OI Δ)</span></div>

      <!-- Score bar -->
      <div style="margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">
          <span style="font-size:10px;color:var(--dim)">🟢 CE favours</span>
          <span style="font-size:9px;color:var(--bull);font-style:italic">PE writers adding → support → BUY CE</span>
          <span id="oi-bull-score" style="font-size:10px;color:var(--bull)">—</span>
        </div>
        <div style="height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;margin-bottom:6px">
          <div id="oi-bull-bar" style="height:100%;background:var(--bull);border-radius:3px;width:50%;transition:width .4s"></div>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">
          <span style="font-size:10px;color:var(--dim)">🔴 PE favours</span>
          <span style="font-size:9px;color:var(--bear);font-style:italic">CE writers adding → resistance → BUY PE</span>
          <span id="oi-bear-score" style="font-size:10px;color:var(--bear)">—</span>
        </div>
        <div style="height:6px;background:var(--bg3);border-radius:3px;overflow:hidden">
          <div id="oi-bear-bar" style="height:100%;background:var(--bear);border-radius:3px;width:50%;transition:width .4s"></div>
        </div>
      </div>

      <div id="oi-writer-rows" style="font-size:11px">
        <div style="color:var(--dim);text-align:center;padding:12px">Loading writer activity…</div>
      </div>

      <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--bdr)">
        <div style="font-size:10px;color:var(--dim);margin-bottom:6px;letter-spacing:.5px">KEY LEVELS FROM OI</div>
        <div style="display:flex;gap:10px;font-size:11px;flex-wrap:wrap">
          <div><span style="color:var(--bear)">■</span> Resistance: <span id="oi-resistance" style="color:var(--txt);font-family:'JetBrains Mono',monospace">—</span></div>
          <div><span style="color:var(--bull)">■</span> Support: <span id="oi-support" style="color:var(--txt);font-family:'JetBrains Mono',monospace">—</span></div>
        </div>
      </div>
    </div>

    <!-- ATM Strike OI table -->
    <div class="card" data-oi-tip="atm_oi_table">
      <div class="ctitle" style="margin-bottom:10px">📊 ATM ±3 Strike OI Breakdown <span style="font-size:10px;color:var(--dim);font-weight:400">Spot: <span id="oi-spot">—</span>  ATM: <span id="oi-atm">—</span></span></div>
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:11px;font-family:'JetBrains Mono',monospace">
          <thead>
            <tr style="color:var(--dim);font-size:9px;letter-spacing:.5px;border-bottom:1px solid var(--bdr)">
              <th style="text-align:right;padding:4px 6px">STRIKE</th>
              <th style="text-align:right;padding:4px 6px">CE OI</th>
              <th style="text-align:right;padding:4px 6px">PE OI</th>
              <th style="text-align:right;padding:4px 6px">PE-CE DIFF</th>
              <th style="text-align:center;padding:4px 6px">BIAS</th>
              <th style="text-align:right;padding:4px 6px">CE IV%</th>
              <th style="text-align:right;padding:4px 6px">PE IV%</th>
              <th style="text-align:right;padding:4px 6px">CE LTP</th>
              <th style="text-align:right;padding:4px 6px">PE LTP</th>
            </tr>
          </thead>
          <tbody id="oi-atm-tbody">
            <tr><td colspan="5" style="color:var(--dim);text-align:center;padding:14px">Loading…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </div><!-- end main grid -->

  <!-- OI History card (full width) -->
  <div class="card" style="margin-bottom:12px" data-oi-tip="oi_history">
    <div class="ctitle" style="margin-bottom:10px">📈 OI Tick History <span style="font-size:10px;color:var(--dim);font-weight:400">— compare PCR &amp; OI build-up across ticks (newest first)</span></div>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:11px;font-family:'JetBrains Mono',monospace" id="oi-history-table">
        <thead>
          <tr style="color:var(--dim);font-size:9px;letter-spacing:.5px;border-bottom:1px solid var(--bdr)">
            <th style="text-align:left;padding:4px 8px">TIME</th>
            <th style="text-align:right;padding:4px 6px">SPOT</th>
            <th style="text-align:right;padding:4px 6px">PCR ALL</th>
            <th style="text-align:right;padding:4px 6px">PCR ATM</th>
            <th style="text-align:right;padding:4px 6px">CE OI<br><span style="color:var(--bear);font-weight:400">↑ = PE favours</span></th>
            <th style="text-align:right;padding:4px 6px">CE Δ</th>
            <th style="text-align:right;padding:4px 6px">PE OI<br><span style="color:var(--bull);font-weight:400">↑ = CE favours</span></th>
            <th style="text-align:right;padding:4px 6px">PE Δ</th>
            <th style="text-align:center;padding:4px 6px">SENTIMENT</th>
            <th style="text-align:center;padding:4px 6px">WRITER BIAS</th>
            <th style="text-align:center;padding:4px 6px">SIGNAL<br><span style="font-weight:400;color:var(--dim)">10-factor</span></th>
            <th style="text-align:right;padding:4px 6px">🟢/🔴</th>
          </tr>
        </thead>
        <tbody id="oi-history-tbody">
          <tr><td colspan="12" style="color:var(--dim);text-align:center;padding:14px">Collecting OI history… (updates every ~60s as calculate_oi_pcr.py writes new ticks)</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- AI Summary card (full width) -->
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:6px">
      <div class="ctitle">🧠 OI Intelligence AI Summary</div>
      <div style="display:flex;gap:8px;align-items:center">
        <div style="font-size:10px;color:var(--dim)" id="oi-ai-ts">—</div>
        <div style="font-size:10px;padding:3px 8px;border-radius:4px;background:var(--bg3)" id="oi-ai-status">—</div>
      </div>
    </div>
    <div id="oi-ai-text"
      style="font-size:12px;line-height:1.7;color:var(--dim);white-space:pre-wrap;font-family:'JetBrains Mono',monospace;min-height:80px">
      Enable OI AI (toggle above) to get Claude's combined OI + signal market view every 2 minutes.<br>
      Requires: calculate_oi_pcr.py running in background to feed oi_snapshot.json.
    </div>
  </div>

</div>
</div><!-- end #tab-oi -->

<!-- Scanner tab -->
<div id="tab-scanner" class="tab-pane" style="padding:16px 18px;overflow-y:auto;max-height:calc(100vh - 118px)">

  <!-- Header row -->
  <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px">
    <div style="font-size:18px;font-weight:700;color:var(--info)">🔭 Trendline Scanner Bot</div>
    <div id="sc-status-badge" style="font-size:11px;padding:3px 8px;border-radius:4px;background:var(--bdr);color:var(--dim)">OFFLINE</div>
    <div style="margin-left:auto;display:flex;gap:8px">
      <button onclick="scStartBot()" style="background:var(--bull);color:#fff;border:none;border-radius:5px;padding:5px 14px;cursor:pointer;font-size:12px;font-weight:600">&#9654; Start</button>
      <button onclick="scStopBot()"  style="background:var(--bear);color:#fff;border:none;border-radius:5px;padding:5px 14px;cursor:pointer;font-size:12px;font-weight:600">&#9632; Stop</button>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:320px 1fr;gap:14px">

    <!-- LEFT: Config Panel -->
    <div style="background:var(--card);border:1px solid var(--bdr);border-radius:8px;padding:14px">
      <div style="font-size:12px;font-weight:700;color:var(--info);margin-bottom:12px;letter-spacing:.8px">&#9881; CONFIG</div>

      <div style="display:flex;flex-direction:column;gap:10px">
        <div>
          <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">EXPIRY DATE</label>
          <select id="sc-expiry"
            style="width:100%;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
            <option value="">Loading expiries...</option>
          </select>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
          <div>
            <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">PREMIUM MIN &#8377;</label>
            <input type="number" id="sc-prem-min" value="85" step="5"
              style="width:100%;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
          </div>
          <div>
            <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">PREMIUM MAX &#8377;</label>
            <input type="number" id="sc-prem-max" value="200" step="5"
              style="width:100%;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
          </div>
        </div>
        <div>
          <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">LOTS (18 &times; 65 = 1170 qty)</label>
          <input type="number" id="sc-lots" value="18" min="1" max="100"
            style="width:100%;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
        </div>
        <!-- Save + IDEAL row -->
        <div style="display:grid;grid-template-columns:1fr auto;gap:6px">
          <button onclick="scSaveConfig()"
            onmouseenter="this.style.background='#1a0a3a';this.style.boxShadow='0 0 8px #7c4dff88'"
            onmouseleave="this.style.background='#0d0d1a';this.style.boxShadow='none'"
            style="background:#0d0d1a;color:#b39ddb;border:2px solid #7c4dff;border-radius:999px;padding:7px 14px;cursor:pointer;font-size:12px;font-weight:700;letter-spacing:.5px">
            SET
          </button>
          <button onclick="scApplyIdeal()"
            title="Apply backtest-proven best config: Asc+Desc ON, Horiz OFF, all filters OFF"
            style="background:linear-gradient(135deg,#f5a623,#e8830a);color:#000;border:none;border-radius:5px;padding:7px 10px;cursor:pointer;font-size:12px;font-weight:700;white-space:nowrap">
            &#11088; IDEAL
          </button>
        </div>
      </div>

      <!-- Trendline Types -->
      <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--bdr)">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <div style="font-size:11px;font-weight:700;color:var(--dim);letter-spacing:.8px">&#128208; TRENDLINE TYPES</div>
          <span id="sc-ideal-badge" style="display:none;font-size:9px;background:#2a1a00;color:#f5a623;border:1px solid #f5a623;border-radius:3px;padding:1px 5px;letter-spacing:.5px">&#11088; IDEAL</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:7px">

          <!-- Ascending TL -->
          <div style="display:flex;align-items:center;gap:8px">
            <button id="sc-tl-asc" data-checked="true" onclick="scToggle('sc-tl-asc')"
              style="padding:3px 0;border-radius:999px;border:1.5px solid #00c853;background:#0d2e1a;color:#00c853;font-size:11px;font-weight:700;cursor:pointer;min-width:54px;text-align:center">ON</button>
            <span style="font-size:11px;color:var(--txt);flex:1">Ascending TL</span>
            <span style="font-size:10px;color:var(--dim);white-space:nowrap">BOUNCE / BREAK</span>
          </div>

          <!-- Descending TL -->
          <div style="display:flex;align-items:center;gap:8px">
            <button id="sc-tl-desc" data-checked="false" onclick="scToggle('sc-tl-desc')"
              style="padding:3px 0;border-radius:999px;border:1.5px solid #333;background:#111;color:#555;font-size:11px;font-weight:700;cursor:pointer;min-width:54px;text-align:center">OFF</button>
            <span style="font-size:11px;color:var(--txt);flex:1">Descending TL</span>
            <span style="font-size:10px;color:var(--dim);white-space:nowrap">BREAKOUT</span>
          </div>

          <!-- Horizontal TL -->
          <div style="display:flex;align-items:center;gap:8px">
            <button id="sc-tl-horiz" data-checked="false" onclick="scToggle('sc-tl-horiz')"
              style="padding:3px 0;border-radius:999px;border:1.5px solid #333;background:#111;color:#555;font-size:11px;font-weight:700;cursor:pointer;min-width:54px;text-align:center">OFF</button>
            <span style="font-size:11px;color:var(--txt);flex:1">Horizontal TL</span>
            <span style="font-size:10px;color:var(--dim);white-space:nowrap">HORIZ_BOUNCE</span>
          </div>

        </div>
      </div>

      <!-- Signal Quality Filters -->
      <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--bdr)">
        <div style="font-size:11px;font-weight:700;color:var(--dim);margin-bottom:8px;letter-spacing:.8px">&#9889; SIGNAL FILTERS</div>
        <div style="display:flex;flex-direction:column;gap:7px">

          <!-- Spot Confirm -->
          <div style="display:flex;align-items:center;gap:8px">
            <button id="sc-spot-confirm" data-checked="false" onclick="scToggle('sc-spot-confirm')"
              style="padding:3px 0;border-radius:999px;border:1.5px solid #333;background:#111;color:#555;font-size:11px;font-weight:700;cursor:pointer;min-width:54px;text-align:center">OFF</button>
            <span style="font-size:11px;color:var(--txt);flex:1">Spot Confirm</span>
            <span style="font-size:10px;color:var(--dim);white-space:nowrap">NIFTY trendline</span>
          </div>

          <!-- Volume Surge -->
          <div style="display:flex;align-items:center;gap:8px">
            <button id="sc-vol-confirm" data-checked="false" onclick="scToggle('sc-vol-confirm')"
              style="padding:3px 0;border-radius:999px;border:1.5px solid #333;background:#111;color:#555;font-size:11px;font-weight:700;cursor:pointer;min-width:54px;text-align:center">OFF</button>
            <span style="font-size:11px;color:var(--txt);flex:1">Volume Surge</span>
            <input type="number" id="sc-vol-mult" value="1.3" step="0.1" min="1.0" max="5.0"
              style="width:48px;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:3px 5px;font-size:11px"
              onchange="scSaveFilters()" title="Volume must be N× 5-bar average">
            <span style="font-size:10px;color:var(--dim);white-space:nowrap">×avg</span>
          </div>

          <!-- % Confirm -->
          <div style="display:flex;align-items:center;gap:8px">
            <button id="sc-pct-confirm" data-checked="false" onclick="scToggle('sc-pct-confirm')"
              style="padding:3px 0;border-radius:999px;border:1.5px solid #333;background:#111;color:#555;font-size:11px;font-weight:700;cursor:pointer;min-width:54px;text-align:center">OFF</button>
            <span style="font-size:11px;color:var(--txt);flex:1">% Confirm</span>
            <input type="number" id="sc-pct-val" value="0.8" step="0.1" min="0.3" max="5.0"
              style="width:48px;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:3px 5px;font-size:11px"
              onchange="scSaveFilters()" title="% of premium LTP needed to confirm bounce">
            <span style="font-size:10px;color:var(--dim);white-space:nowrap">%</span>
          </div>

        </div>
      </div>

      <!-- Today stats -->
      <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--bdr)">
        <div style="font-size:11px;font-weight:700;color:var(--dim);margin-bottom:8px;letter-spacing:.8px">TODAY</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;text-align:center">
          <div style="background:var(--bg);border-radius:4px;padding:6px">
            <div style="font-size:16px;font-weight:700" id="sc-stat-trades">&#8212;</div>
            <div style="font-size:10px;color:var(--dim)">Trades</div>
          </div>
          <div style="background:var(--bg);border-radius:4px;padding:6px">
            <div style="font-size:16px;font-weight:700;color:var(--bull)" id="sc-stat-wins">&#8212;</div>
            <div style="font-size:10px;color:var(--dim)">Wins</div>
          </div>
          <div style="background:var(--bg);border-radius:4px;padding:6px">
            <div style="font-size:16px;font-weight:700;color:var(--bear)" id="sc-stat-losses">&#8212;</div>
            <div style="font-size:10px;color:var(--dim)">Losses</div>
          </div>
        </div>
        <div style="text-align:center;margin-top:8px">
          <div style="font-size:20px;font-weight:700" id="sc-stat-pnl">&#8377;&#8212;</div>
          <div style="font-size:10px;color:var(--dim)">Today P&amp;L</div>
        </div>
      </div>

      <!-- Active trade -->
      <div id="sc-active-trade" style="display:none;margin-top:12px;padding:10px;background:rgba(0,200,100,.07);border:1px solid var(--bull);border-radius:6px">
        <div style="font-size:11px;font-weight:700;color:var(--bull);margin-bottom:6px">&#128994; ACTIVE TRADE</div>
        <div id="sc-active-content" style="font-size:11px;line-height:1.8;color:var(--txt)"></div>
      </div>
    </div>

    <!-- RIGHT: Signals + Logs -->
    <div style="display:flex;flex-direction:column;gap:14px">

      <!-- Signals panel -->
      <div style="background:var(--card);border:1px solid var(--bdr);border-radius:8px;padding:14px;flex:0 0 auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <div style="font-size:12px;font-weight:700;color:var(--info);letter-spacing:.8px">&#9889; LIVE SIGNALS</div>
          <span style="font-size:10px;color:var(--dim)" id="sc-signals-ts">&#8212;</span>
        </div>
        <div id="sc-signals-list" style="display:flex;flex-direction:column;gap:6px;max-height:280px;overflow-y:auto">
          <div style="color:var(--dim);font-size:12px;text-align:center;padding:20px">No signals yet today</div>
        </div>
      </div>

      <!-- Log viewer -->
      <div style="background:var(--card);border:1px solid var(--bdr);border-radius:8px;padding:14px;flex:1">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <div style="font-size:12px;font-weight:700;color:var(--info);letter-spacing:.8px">&#128203; BOT LOGS</div>
          <div style="display:flex;gap:6px">
            <button onclick="scRefreshLogs()" style="background:none;border:1px solid var(--bdr);color:var(--dim);border-radius:4px;padding:3px 8px;cursor:pointer;font-size:11px">&#8635; Refresh</button>
          </div>
        </div>
        <pre id="sc-log-viewer" style="margin:0;font-size:11px;line-height:1.5;color:#b0c4de;max-height:340px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;background:var(--bg);border-radius:4px;padding:10px"></pre>
      </div>

    </div>
  </div>

  <!-- ── TRENDLINE CHARTS ──────────────────────────────────────────── -->
  <div style="margin-top:16px;background:var(--card);border:1px solid var(--bdr);border-radius:8px;padding:14px;position:relative">
    <!-- ── STATUS PANEL ──────────────────────────────────────────── -->
    <div id="sc-status-panel" style="background:#0a0e1a;border:1px solid #1e2a3a;border-radius:6px;padding:10px 12px;margin-bottom:12px;font-size:11px">
      <!-- Row 1: bot dot + spot + bars + tl count -->
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px">
        <span id="sc-st-dot" style="display:inline-flex;align-items:center;gap:5px;font-weight:700">
          <span style="width:8px;height:8px;border-radius:50%;background:#555;display:inline-block" id="sc-st-dot-circle"></span>
          <span id="sc-st-dot-label" style="color:#666">OFFLINE</span>
        </span>
        <span style="color:#888">│</span>
        <span style="color:var(--dim)">NIFTY <span id="sc-st-spot" style="color:#e0e0e0;font-weight:700">—</span></span>
        <span style="color:#888">│</span>
        <span style="color:var(--dim)">Bars: <span id="sc-st-bars" style="color:#ccc">—</span></span>
        <span style="color:#888">│</span>
        <span style="color:var(--dim)">TL: <span id="sc-st-tl" style="color:#00c853;font-weight:700">—</span> active</span>
        <span style="color:#888">│</span>
        <span style="color:var(--dim)">In range: <span id="sc-st-inrange" style="color:#40c4ff;font-weight:700">—</span></span>
      </div>
      <!-- Row 2: open trade or no trade -->
      <div id="sc-st-trade-row" style="margin-bottom:6px;display:none">
        <span style="color:#ff5252;font-weight:700">● OPEN TRADE</span>
        <span id="sc-st-trade-info" style="color:#ccc;margin-left:8px"></span>
      </div>
      <!-- Row 3: watching list -->
      <div>
        <span style="color:var(--dim);letter-spacing:.5px;font-size:10px">WATCHING (nearest to signal)</span>
        <div id="sc-st-watching" style="margin-top:4px;display:flex;flex-wrap:wrap;gap:5px">
          <span style="color:#444;font-size:10px">—</span>
        </div>
      </div>
    </div>

    <!-- Header row -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:10px">
        <div style="font-size:12px;font-weight:700;color:var(--info);letter-spacing:.8px">&#128202; TRENDLINE CHARTS</div>
        <span id="sc-chart-demo-badge" style="display:none;font-size:9px;background:#2a1f00;color:#ffd740;border:1px solid #ffd740;border-radius:3px;padding:1px 5px;letter-spacing:.5px">DEMO</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:10px;color:var(--dim)" id="sc-chart-ts"></span>
        <button onclick="scShowDemoGuide()"
          style="background:none;border:1px solid var(--bdr);color:var(--dim);border-radius:4px;padding:2px 8px;cursor:pointer;font-size:11px"
          title="Show example charts with all trendline types">&#128208; Guide</button>
      </div>
    </div>

    <!-- Chart grid -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <!-- NIFTY Spot chart -->
      <div>
        <div style="font-size:10px;color:var(--dim);margin-bottom:4px;letter-spacing:.5px">NIFTY SPOT</div>
        <div style="background:var(--bg);border-radius:4px;overflow:hidden">
          <canvas id="sc-chart-spot" height="220" style="width:100%;display:block"></canvas>
        </div>
      </div>
      <!-- Option instrument chart -->
      <div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="font-size:10px;color:var(--dim);letter-spacing:.5px" id="sc-chart-opt-label">OPTION</span>
          <select id="sc-chart-select" onchange="scRenderSelectedChart()"
            style="flex:1;background:#1a1f2e;border:1px solid #334;color:#ccc;border-radius:4px;padding:3px 6px;font-size:10px;cursor:pointer;max-width:260px;accent-color:#00c853;outline:none">
            <option value="" style="background:#1a1f2e;color:#888">&#9660; pick instrument</option>
          </select>
        </div>
        <div style="background:var(--bg);border-radius:4px;overflow:hidden">
          <canvas id="sc-chart-option" height="220" style="width:100%;display:block"></canvas>
        </div>
      </div>
    </div>

    <!-- Legend row -->
    <div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;font-size:10px;color:var(--dim)">
      <span><span style="color:#00c853">&#9644;</span> Asc Support (BOUNCE)</span>
      <span><span style="color:#69f0ae">&#9644;</span> Asc Resist → target</span>
      <span><span style="color:#ff5252">&#9644;</span> Desc Resist (BREAKOUT)</span>
      <span><span style="color:#ff8a80">&#9644;</span> Desc Support → SL</span>
      <span><span style="color:#ffd740">&#9644;&#9644;</span> Horizontal (HORIZ_BOUNCE)</span>
      <span><span style="color:#888">&#8942;&#8942;</span> LTP</span>
    </div>
  </div>

  <!-- ── DEMO GUIDE MODAL ──────────────────────────────────────────── -->
  <div id="sc-demo-modal" onclick="if(event.target===this)this.style.display='none'"
    style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9999;align-items:center;justify-content:center">
    <div style="background:#1a1f2e;border:1px solid var(--bdr);border-radius:10px;padding:20px;max-width:960px;width:95%;max-height:90vh;overflow-y:auto">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <div style="font-size:14px;font-weight:700;color:var(--info)">&#128208; Trendline Types — Visual Guide</div>
        <button onclick="document.getElementById('sc-demo-modal').style.display='none'"
          style="background:none;border:none;color:var(--dim);font-size:18px;cursor:pointer">&#10005;</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
        <div>
          <div style="font-size:11px;font-weight:700;color:#00c853;margin-bottom:6px">&#9650; ASCENDING CHANNEL</div>
          <div style="font-size:10px;color:var(--dim);margin-bottom:6px">Higher lows (green) + higher highs (light green). Signal: BOUNCE off lower rail. Target: upper rail.</div>
          <canvas id="sc-demo-asc" height="180" style="width:100%;display:block;background:#0d1117;border-radius:4px"></canvas>
        </div>
        <div>
          <div style="font-size:11px;font-weight:700;color:#ff5252;margin-bottom:6px">&#9660; DESCENDING CHANNEL</div>
          <div style="font-size:10px;color:var(--dim);margin-bottom:6px">Lower highs (red) + lower lows (pink). Signal: BREAKOUT when price crosses above upper rail.</div>
          <canvas id="sc-demo-desc" height="180" style="width:100%;display:block;background:#0d1117;border-radius:4px"></canvas>
        </div>
        <div>
          <div style="font-size:11px;font-weight:700;color:#ffd740;margin-bottom:6px">&#9644; HORIZONTAL ZONE</div>
          <div style="font-size:10px;color:var(--dim);margin-bottom:6px">Flat highs + flat lows. Both lows AND highs within 0.15% of each other. Signal: HORIZ_BOUNCE near mid-zone.</div>
          <canvas id="sc-demo-horiz" height="180" style="width:100%;display:block;background:#0d1117;border-radius:4px"></canvas>
        </div>
      </div>
      <div style="margin-top:12px;font-size:10px;color:#555;text-align:center">
        Pivot dots mark the anchor points. Lines extend to the current candle. Labels show projected price.
        SL for BOUNCE = just below lower rail. Target for BOUNCE = upper rail − buffer.
      </div>
    </div>
  </div>

  <!-- ── BACKTEST / HISTORY ────────────────────────────────────────── -->
  <div style="margin-top:16px;background:var(--card);border:1px solid var(--bdr);border-radius:8px;padding:14px">
    <div style="font-size:13px;font-weight:700;color:var(--info);margin-bottom:12px;letter-spacing:.8px">&#128202; BACKTEST / TRADE HISTORY</div>

    <!-- Trade History row -->
    <div style="font-size:10px;color:var(--dim);margin-bottom:6px;letter-spacing:.5px">TRADE HISTORY  (scanner bot sim/live trades)</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:14px">
      <div>
        <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">FROM</label>
        <input type="date" id="sc-bt-from"
          style="background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:5px 8px;font-size:12px">
      </div>
      <div>
        <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">TO</label>
        <input type="date" id="sc-bt-to"
          style="background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:5px 8px;font-size:12px">
      </div>
      <div>
        <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">MODE</label>
        <select id="sc-bt-mode"
          style="background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:5px 8px;font-size:12px">
          <option value="ALL">All</option>
          <option value="sim">Sim</option>
          <option value="live">Live</option>
        </select>
      </div>
      <button onclick="scRunBacktest()"
        style="background:var(--info);color:#000;border:none;border-radius:5px;padding:7px 14px;cursor:pointer;font-size:12px;font-weight:700">
        &#9654; Load History
      </button>
    </div>

    <!-- Fresh Backtest row -->
    <div style="font-size:10px;color:var(--dim);margin-bottom:6px;letter-spacing:.5px">FRESH BACKTEST  (runs historical simulation via Groww API — takes 1–3 min)</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:14px">
      <div>
        <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">EXPIRY</label>
        <span id="sc-bt-expiry-wrap" style="font-size:12px;color:var(--txt);background:var(--bg);border:1px solid var(--bdr);border-radius:4px;padding:5px 8px;display:inline-block;min-width:90px" id="sc-bt-expiry-disp">—</span>
      </div>
      <div>
        <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:3px">DAYS BACK</label>
        <input type="number" id="sc-bt-days" value="31" min="1" max="90"
          style="width:70px;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);border-radius:4px;padding:5px 8px;font-size:12px">
      </div>
      <button onclick="scRunFreshBacktest()"
        style="background:#8b5cf6;color:#fff;border:none;border-radius:5px;padding:7px 14px;cursor:pointer;font-size:12px;font-weight:700">
        &#128260; Run Backtest
      </button>
      <span id="sc-bt-status" style="font-size:11px;color:var(--dim);align-self:center"></span>
    </div>

    <span id="sc-bt-summary" style="font-size:12px;color:var(--dim);display:block;margin-bottom:8px"></span>

    <!-- Results table -->
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
          <tr style="color:var(--dim);border-bottom:1px solid var(--bdr)">
            <th style="text-align:left;padding:5px 8px">Date</th>
            <th style="text-align:left;padding:5px 8px">Symbol</th>
            <th style="text-align:left;padding:5px 8px">Type</th>
            <th style="text-align:right;padding:5px 8px">Buy</th>
            <th style="text-align:right;padding:5px 8px">Sell</th>
            <th style="text-align:right;padding:5px 8px">Qty</th>
            <th style="text-align:right;padding:5px 8px">P&amp;L</th>
            <th style="text-align:left;padding:5px 8px">Entry</th>
            <th style="text-align:left;padding:5px 8px">Exit</th>
            <th style="text-align:left;padding:5px 8px">Reason</th>
          </tr>
        </thead>
        <tbody id="sc-bt-tbody">
          <tr><td colspan="10" style="padding:20px;text-align:center;color:var(--dim)">Select a date range and click Run</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>

<!-- Guide tab -->
<!-- Trade Control tab — embeds the standalone TRADE_CONTROL_PANEL.py (port 8790) -->
<div id="tab-control" class="tab-pane">
  <iframe id="controlFrame" src="" style="width:100%;height:calc(100vh - 128px);border:none;background:#0d1117"></iframe>
  <div id="controlHint" style="display:none;padding:36px;text-align:center;color:#8b949e;font-size:14px">
    🛡 Control panel not reachable.<br><br>
    Start it with: <code style="color:#58a6ff">python3 TRADE_CONTROL_PANEL.py</code><br>
    then reopen this tab (it is auto-started with the dashboard normally).
  </div>
</div>

<div id="tab-guide" class="tab-pane">
<div class="guide" style="max-width:1400px">

<!-- ── Section 0: Tab Overview ── -->
<div class="gcard" style="margin-bottom:16px;background:linear-gradient(135deg,var(--bg2),var(--bg3))">
  <div class="gcard-title" style="font-size:14px">🗂️ Tab Navigation Overview</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;font-size:11px">
    <div class="grow"><span class="gtag info" style="font-size:9px">📡 Live Dashboard</span><span class="gdesc">Consensus signal, key levels, Fibonacci, Master Signal, Premium, AI summary, scalp plan</span></div>
    <div class="grow"><span class="gtag info" style="font-size:9px">🔬 OI Intelligence</span><span class="gdesc">15 live OI signals — PCR, max pain, IV, buildup, writer bias, smart money, ATM momentum</span></div>
    <div class="grow"><span class="gtag info" style="font-size:9px">🚀 Trade Board</span><span class="gdesc">Manual one-click option buying via PROD10 bot — option chain, trail SL, paper mode</span></div>
    <div class="grow"><span class="gtag info" style="font-size:9px">💹 PnL Status</span><span class="gdesc">Live P&L, margin, orders, personal AI trade advisor, 3-year stats, behavioral risks</span></div>
    <div class="grow"><span class="gtag info" style="font-size:9px">📈 Performance</span><span class="gdesc">Historical signal accuracy, win rates, system proof-of-concept stats</span></div>
    <div class="grow"><span class="gtag info" style="font-size:9px">🤖 Bot Control</span><span class="gdesc">Start/stop all bots, live status, per-bot log view — one-click management</span></div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">🗺️ Guide (this tab)</span><span class="gdesc">Complete reference for every feature, signal, bot, and workflow in this system</span></div>
  </div>
</div>

<!-- ── Section 1: Data Source Map ── -->
<div class="gcard-title" style="font-size:14px;margin-bottom:16px">📡 Data Source Map — What Comes From Where</div>
<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px">

  <div class="gcard">
    <div class="gcard-title">📊 Live Dashboard Tab</div>
    <table style="width:100%;font-size:11px;border-collapse:collapse">
      <tr style="color:var(--dim);font-size:9px;letter-spacing:.5px"><th style="text-align:left;padding:3px 0">CARD</th><th style="text-align:left">SOURCE BOT</th></tr>
      <tr><td style="padding:3px 0;color:var(--txt)">Consensus Signal</td><td style="color:var(--info)">Master+Fibo+Chart+Sigmon</td></tr>
      <tr><td style="color:var(--txt)">Master Signal</td><td style="color:var(--info)">MASTER_SIGNAL_BOT.py</td></tr>
      <tr><td style="color:var(--txt)">Fibonacci</td><td style="color:var(--info)">FIBONACCI_TREND_ANALYZER.py</td></tr>
      <tr><td style="color:var(--txt)">Chart Level</td><td style="color:var(--info)">CHART_LEVEL_ANALYZER.py</td></tr>
      <tr><td style="color:var(--txt)">Premium Tracker</td><td style="color:var(--info)">PREMIUM_DIRECTION_TRACKER.py</td></tr>
      <tr><td style="color:var(--txt)">Signal Monitor</td><td style="color:var(--info)">SIGNAL_MONITOR.py</td></tr>
      <tr><td style="color:var(--txt)">PROD10 Bot status</td><td style="color:var(--info)">logs/groww_bot/</td></tr>
      <tr><td style="color:var(--txt)">Momentum Bot status</td><td style="color:var(--info)">logs/momentum_bot/</td></tr>
      <tr><td style="color:var(--txt)">Live Option LTP</td><td style="color:var(--bull)">Groww /v1/live-data/ltp</td></tr>
      <tr><td style="color:var(--txt)">⚡ Scalp Plan</td><td style="color:var(--accent)">Claude CLI (every 60s)</td></tr>
      <tr><td style="color:var(--txt)">🤖 AI Summary</td><td style="color:var(--accent)">Claude CLI (every 3min)</td></tr>
    </table>
  </div>

  <div class="gcard">
    <div class="gcard-title">🔬 OI Intelligence Tab</div>
    <table style="width:100%;font-size:11px;border-collapse:collapse">
      <tr style="color:var(--dim);font-size:9px;letter-spacing:.5px"><th style="text-align:left;padding:3px 0">SIGNAL</th><th style="text-align:left">SOURCE</th></tr>
      <tr><td style="padding:3px 0;color:var(--txt)">PCR All / ATM</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">Max Pain</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">IV Range Band</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">Market Signal</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">Writer Bias</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">Smart Money</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">CE/PE Writing</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">Per-Strike Buildup</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">IV Change Spikes</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">PCR Change Signal</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">ATM Momentum BUY NOW</td><td style="color:var(--bull)">calculate_oi_pcr.py</td></tr>
      <tr><td style="color:var(--txt)">🧠 OI AI Summary</td><td style="color:var(--accent)">Claude CLI (every 2min)</td></tr>
    </table>
  </div>

  <div class="gcard">
    <div class="gcard-title">🚀 Trade Board + 💹 PnL</div>
    <table style="width:100%;font-size:11px;border-collapse:collapse">
      <tr style="color:var(--dim);font-size:9px;letter-spacing:.5px"><th style="text-align:left;padding:3px 0">FEATURE</th><th style="text-align:left">SOURCE</th></tr>
      <tr><td style="padding:3px 0;color:var(--txt)">Option Chain</td><td style="color:var(--bull)">Groww /v1/option-chain/</td></tr>
      <tr><td style="color:var(--txt)">BUY / SELL Orders</td><td style="color:var(--bull)">Groww /v1/order/create</td></tr>
      <tr><td style="color:var(--txt)">ATR Calculation</td><td style="color:var(--bull)">Groww /v1/historical/candles</td></tr>
      <tr><td style="color:var(--txt)">Fill Confirmation</td><td style="color:var(--bull)">Groww /v1/order/trades/{id}</td></tr>
      <tr><td style="color:var(--txt);border-top:1px solid var(--bdr);padding-top:5px">Today's P&L</td><td style="color:var(--bull)">Groww /v1/positions/user</td></tr>
      <tr><td style="color:var(--txt)">Capital &amp; Margin</td><td style="color:var(--bull)">Groww /v1/margins/detail/user</td></tr>
      <tr><td style="color:var(--txt)">VIX / PCR</td><td style="color:var(--info)">NSE API + PERSONAL_TRADING_AI</td></tr>
      <tr><td style="color:var(--txt)">Market Score</td><td style="color:var(--info)">PERSONAL_TRADING_AI.py</td></tr>
      <tr><td style="color:var(--txt)">3-Year Stats</td><td style="color:var(--warn)">ayush_previous_data/*.xlsx</td></tr>
      <tr><td style="color:var(--txt)">AI Advisory</td><td style="color:var(--accent)">Claude CLI (from PTAI)</td></tr>
    </table>
  </div>
</div>

<!-- ── Section 2: Bot Status Bar ── -->
<div class="gcard" style="margin-bottom:16px">
  <div class="gcard-title">🟢 Bot Status Bar — Top of Every Page</div>
  <div class="gdesc" style="margin-bottom:10px">The colored badges at the top update every 15s. Each badge shows whether a bot is <b style="color:var(--bull)">LIVE</b> (writing recent logs), <b style="color:var(--warn)">STALE</b> (logs stopped updating), or <b style="color:var(--dim)">OFFLINE</b> (no log file found).</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:11px">
    <div>
      <div class="grow"><span class="gtag info" style="min-width:120px;font-size:9px">OI PCR</span><span class="gdesc">calculate_oi_pcr.py — feeds OI Intelligence tab. Stale &gt;5min = OI data old.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px;font-size:9px">MASTER SIGNAL</span><span class="gdesc">MASTER_SIGNAL_BOT.py — feeds Consensus + Key Levels. Stale = no direction signal.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px;font-size:9px">FIBONACCI</span><span class="gdesc">FIBONACCI_TREND_ANALYZER.py — feeds fib levels + 1H/15M trend cards.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px;font-size:9px">CHART LEVEL</span><span class="gdesc">CHART_LEVEL_ANALYZER.py — feeds S/R levels, option suggestion, trade decision.</span></div>
    </div>
    <div>
      <div class="grow"><span class="gtag info" style="min-width:120px;font-size:9px">PREMIUM TRACKER</span><span class="gdesc">PREMIUM_DIRECTION_TRACKER.py — CE/PE premium flow direction.</span></div>
      <div class="grow"><span class="gtag bull" style="min-width:120px;font-size:9px">PROD10 BOT</span><span class="gdesc">PROD10FEB_ManualBOT — manual trading bot. Shows LIVE when running, STALE when idle.</span></div>
      <div class="grow"><span class="gtag bull" style="min-width:120px;font-size:9px">MOMENTUM BOT</span><span class="gdesc">MOMENTUM_AUTO_BOT.py — auto premium scanner. Separate badge from PROD10 BOT.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px;font-size:9px">SIGNAL MONITOR</span><span class="gdesc">SIGNAL_MONITOR.py — tracks signal accuracy, combined signal verdict.</span></div>
    </div>
  </div>
  <div class="gdesc" style="margin-top:8px;color:var(--warn)">⚠️ PROD10 BOT and MOMENTUM BOT have <b>separate status badges</b>. Running PROD10 does not affect MOMENTUM status and vice versa.</div>
</div>

<!-- ── Section 3: Bot Coverage ── -->
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
  <div class="gcard">
    <div class="gcard-title">✅ All Bots Covered</div>
    <div class="grow"><span class="gtag info" style="min-width:155px;font-size:9px">MASTER_SIGNAL_BOT</span><span class="gdesc">Direction, confidence, zone, pattern, scores, SL/target</span></div>
    <div class="grow"><span class="gtag info" style="min-width:155px;font-size:9px">FIBONACCI_ANALYZER</span><span class="gdesc">Fib levels, confluence zones, trade setup, entry triggers</span></div>
    <div class="grow"><span class="gtag info" style="min-width:155px;font-size:9px">CHART_LEVEL_ANALYZER</span><span class="gdesc">Trade decision, option suggestion, S/R levels</span></div>
    <div class="grow"><span class="gtag info" style="min-width:155px;font-size:9px">PREMIUM_TRACKER</span><span class="gdesc">CE/PE flow, LTP, direction</span></div>
    <div class="grow"><span class="gtag info" style="min-width:155px;font-size:9px">SIGNAL_MONITOR</span><span class="gdesc">Combined signal, PDT+FIBO signals, accuracy tracking</span></div>
    <div class="grow"><span class="gtag info" style="min-width:155px;font-size:9px">PERSONAL_TRADING_AI</span><span class="gdesc">Full PnL tab — market score, permission, behavioral analysis</span></div>
    <div class="grow"><span class="gtag info" style="min-width:155px;font-size:9px">OI PCR (calculate_oi)</span><span class="gdesc">Full OI Intelligence tab — 15 live signals + AI summary</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:155px;font-size:9px">PROD10FEB BOT</span><span class="gdesc">Status card + Trade Board — active/idle, trailing SL, live LTP</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:155px;font-size:9px">MOMENTUM_AUTO_BOT</span><span class="gdesc">Status badge — running/idle, in-trade, trailing status</span></div>
  </div>
  <div class="gcard">
    <div class="gcard-title">⚙️ Features Added Since v1.0</div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">OI Intelligence tab</span><span class="gdesc">15 live OI signals with hover tooltips and blink alerts</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Per-strike buildup</span><span class="gdesc">LONG/SHORT BUILDUP, COVERING, UNWINDING at ATM ±3 strikes</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">IV spike detection</span><span class="gdesc">Tick-over-tick IV change alerts — threshold 1.5%/tick</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">ATM Momentum BUY NOW</span><span class="gdesc">LTP+OI direction score 0–100, ≥60 = BUY NOW signal</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">OI blink alerts</span><span class="gdesc">Cards blink green/red/yellow when attention needed — tooltip explains why</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Momentum Auto Bot</span><span class="gdesc">Fully autonomous: discover → 20s observe → decide → trail SL → Telegram</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Prod bot trailing port</span><span class="gdesc">ATR-based trail, TRAIL_START_PROFIT, TRAIL_STEP, hard SL all from PROD10</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Separate bot badges</span><span class="gdesc">PROD10 BOT and MOMENTUM BOT now have independent status in bot bar</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Bot Control Center</span><span class="gdesc">Start/stop all 8 bots from browser — no terminal needed</span></div>
  </div>
</div>

<!-- ── Section 4: OI Intelligence Guide ── -->
<div class="gcard" style="margin-bottom:16px">
  <div class="gcard-title">🔬 OI Intelligence Tab — 15 Signals Explained</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;font-size:11px">
    <div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">CORE MARKET SIGNALS</div>
      <div class="grow"><span class="gtag info" style="font-size:9px">PCR All</span><span class="gdesc">Put-Call Ratio for all strikes. &gt;1.2 = bullish (more puts = hedgers protecting longs). &lt;0.8 = bearish. Blinks when extreme (&gt;1.5 or &lt;0.6).</span></div>
      <div class="grow"><span class="gtag info" style="font-size:9px">PCR ATM</span><span class="gdesc">PCR only at ATM strike — purer sentiment since ATM options are most traded.</span></div>
      <div class="grow"><span class="gtag info" style="font-size:9px">Max Pain</span><span class="gdesc">Price at which option writers (market makers) lose least. Market often gravities toward max pain near expiry. Blinks when spot is within 30 pts.</span></div>
      <div class="grow"><span class="gtag info" style="font-size:9px">IV Range Band</span><span class="gdesc">Expected daily price range from ATM IV. "Above band" = market already moved beyond expectation. "In band" = normal movement.</span></div>
      <div class="grow"><span class="gtag info" style="font-size:9px">Market Signal</span><span class="gdesc">Combined OI + PCR verdict: STRONG BULLISH / BULLISH / NEUTRAL / BEARISH / STRONG BEARISH. Blinks on STRONG signals.</span></div>
      <div class="grow"><span class="gtag info" style="font-size:9px">PCR Change</span><span class="gdesc">Tick-over-tick PCR delta. Rapidly rising PCR = puts being bought = hedging = potential bullish reversal. Blinks when |Δ| &gt; 0.10.</span></div>
      <div style="color:var(--warn);font-weight:700;margin-bottom:8px;margin-top:12px;font-size:10px;letter-spacing:.5px">LEVEL BREAK SIGNALS (NEW)</div>
      <div class="grow"><span class="gtag warn" style="font-size:9px">🔥 BREAKOUT</span><span class="gdesc">Appears on <b>Resistance Wall card</b> when spot crosses above the resistance strike AND CE volume at that strike is ≥1.5× the per-strike average. Real buyers breaking the wall. Action: BUY CE. Card blinks orange.</span></div>
      <div class="grow"><span class="gtag warn" style="font-size:9px">🔻 BREAKDOWN</span><span class="gdesc">Appears on <b>Support Floor card</b> when spot falls below the support strike AND PE volume at that strike is ≥1.5× average. Real sellers collapsing the floor. Action: BUY PE. Card blinks orange.</span></div>
      <div class="grow"><span class="gtag warn" style="font-size:9px">⚡ Tentative</span><span class="gdesc">Spot has crossed the level but volume is NOT elevated. Likely a fake-out or low-conviction move. Wait for volume confirmation before trading the break. Shows vol ratio for reference.</span></div>
    </div>
    <div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">WRITER / SMART MONEY SIGNALS</div>
      <div class="grow"><span class="gtag bull" style="font-size:9px">Writer Bias</span><span class="gdesc">Who are the big option writers leaning toward? Writers make money when price stays in their range. BULLISH bias = they sold PEs (floor support).</span></div>
      <div class="grow"><span class="gtag bull" style="font-size:9px">Smart Money</span><span class="gdesc">Top OI concentration strikes — where big players parked their positions. Blinks when any single strike OI &gt; 10 Lakh.</span></div>
      <div class="grow"><span class="gtag bull" style="font-size:9px">CE Writing</span><span class="gdesc">Top CE strike being written (sold). CE writers profit if price stays below that strike — acts as resistance. Blinks on CONFIRMED writing.</span></div>
      <div class="grow"><span class="gtag bull" style="font-size:9px">PE Writing</span><span class="gdesc">Top PE strike being written. PE writers profit if price stays above that strike — acts as support. Blinks on CONFIRMED writing.</span></div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;margin-top:12px;font-size:10px;letter-spacing:.5px">SHORT-TERM MOMENTUM SIGNALS</div>
      <div class="grow"><span class="gtag warn" style="font-size:9px">Per-Strike Buildup</span><span class="gdesc">For each ATM ±3 strike: LONG BUILDUP (price↑+OI↑), SHORT BUILDUP (price↓+OI↑), SHORT COVERING (price↑+OI↓), LONG UNWINDING (price↓+OI↓).</span></div>
      <div class="grow"><span class="gtag warn" style="font-size:9px">IV Change Spikes</span><span class="gdesc">Sudden IV jump &gt;1.5% in one tick — indicates big player entering or news event. Blinks on spike at ATM strike.</span></div>
      <div class="grow"><span class="gtag warn" style="font-size:9px">ATM Momentum</span><span class="gdesc">Score 0–100 from LTP + OI change direction at ATM strike. Score ≥60 = BUY NOW with +6% target and −3% stop. Blinks green on BUY NOW.</span></div>
    </div>
  </div>
  <div class="gdesc" style="margin-top:10px;border-top:1px solid var(--bdr);padding-top:8px"><b style="color:var(--info)">Hover over any OI element</b> to see a tooltip explaining its meaning and why it may be blinking. Blinking cards need attention — the tooltip always explains the specific reason.</div>
</div>

<!-- ── Section 5: Momentum Auto Bot ── -->
<div class="gcard" style="margin-bottom:16px">
  <div class="gcard-title">🤖 Momentum Auto Bot — Complete Flow (MOMENTUM_AUTO_BOT.py)</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;font-size:11px">
    <div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">PHASE 1 — DISCOVER (instant, parallel)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">1.</b> Fetch LTP for all ATM ± atm_range CE and PE strikes simultaneously</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">2.</b> Keep only strikes with premium in [min_premium, max_premium] (₹50–₹200 default)</div>
      <div class="gdesc" style="margin-bottom:12px"><b style="color:var(--txt)">3.</b> Print: "Scanning for Premiums under range 50-200 found N premiums (CE: x, PE: y)"</div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">PHASE 2 — OBSERVE (scan_seconds = 20s)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">4.</b> Poll all discovered strikes every 1 second for 20 seconds</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">5.</b> Print table each second: "1st second / CE = 98.00, 129.25 / PE = 54.30, 87.35"</div>
      <div class="gdesc" style="margin-bottom:12px"><b style="color:var(--txt)">6.</b> Builds full tick history for each strike</div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">PHASE 3 — DECIDE (after 20s)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">7.</b> For each strike: velocity = (last−first)/first × 100, consistency = % of ticks in same direction</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">8.</b> CE net score vs PE net score — higher wins</div>
      <div class="gdesc"><b style="color:var(--txt)">9.</b> Best strike on winning side: highest velocity × consistency score</div>
    </div>
    <div>
      <div style="color:var(--warn);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">PHASE 4 — TRADE ENTRY</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">10.</b> Place BUY market order via Groww API</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">11.</b> Wait for EXECUTED status; fetch actual fill price</div>
      <div class="gdesc" style="margin-bottom:12px"><b style="color:var(--txt)">12.</b> Fetch ATR (non-blocking, 3s timeout) to resolve trail step</div>
      <div style="color:var(--warn);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">PHASE 5 — TRAIL SL LOOP (prod-bot logic)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">13.</b> Hard SL = entry − HARD_SL_POINTS (default 8 pts)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">14.</b> Trail activates when LTP &gt; entry + TRAIL_START_PROFIT (1 pt)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">15.</b> Trail exit = peak − TRAIL_STEP (0.75 pts fixed, or ATR×mult)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">16.</b> Exponential backoff on LTP fetch errors, 30s heartbeat</div>
      <div class="gdesc" style="margin-bottom:12px"><b style="color:var(--txt)">17.</b> Force exit after max_hold_min (30 min)</div>
      <div style="color:var(--bear);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">AFTER TRADE</div>
      <div class="gdesc" style="margin-bottom:5px">Wait <b>2 minutes</b> (cooldown_sec=120) before next scan</div>
      <div class="gdesc" style="margin-bottom:5px">If <b>no signal</b> found: wait <b>1 minute</b> (no_signal_wait_sec=60)</div>
      <div class="gdesc">Logs to Lakshmi.xlsx + sends Telegram on every entry/exit</div>
    </div>
  </div>
  <div style="margin-top:12px;padding:10px;background:var(--bg3);border-radius:6px;font-size:11px">
    <div style="color:var(--info);font-weight:700;margin-bottom:6px;font-size:10px;letter-spacing:.5px">KEY CONFIG (edit CONFIG dict in MOMENTUM_AUTO_BOT.py)</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px">
      <div class="gdesc"><b>scan_seconds=20</b> — observation window</div>
      <div class="gdesc"><b>min/max_premium=50/200</b> — strike filter</div>
      <div class="gdesc"><b>velocity_pct=0.5%</b> — min momentum</div>
      <div class="gdesc"><b>HARD_SL_POINTS=8</b> — hard stop loss pts</div>
      <div class="gdesc"><b>TRAIL_START_PROFIT=1</b> — trail start pts</div>
      <div class="gdesc"><b>TRAIL_STEP=0.75</b> — trail gap pts</div>
      <div class="gdesc"><b>TRAIL_SL_ATR_BASED=False</b> — fixed by default</div>
      <div class="gdesc"><b>max_hold_min=30</b> — force exit time</div>
      <div class="gdesc"><b>max_trades_day=5</b> — daily trade limit</div>
    </div>
  </div>
</div>

<!-- ── Section 6: Trade Board Flow ── -->
<div class="gcard" style="margin-bottom:16px">
  <div class="gcard-title">⚡ Trade Board — Complete Flow (PROD10FEB bot)</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;font-size:11px">
    <div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">SETUP</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">1.</b> Select Index + Expiry (from instrument.csv)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">2.</b> Set Lots · Hard SL · Trail Start · Trail Step · Max Time</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">3.</b> Toggle ATR-based SL (optional) + ATR multiplier</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">4.</b> Toggle Paper mode for safe testing</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">5.</b> Click CE/PE on any chain strike → fills symbol</div>
      <div class="gdesc" style="margin-bottom:12px"><b style="color:var(--txt)">6.</b> Click BUY button → sends to PROD10 bot</div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">BUY EXECUTION</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">7.</b> POST /v1/order/create (market order)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">8.</b> Poll order status every 0.2s until COMPLETE</div>
      <div class="gdesc"><b style="color:var(--bull)">→ BUY EXECUTED logged with millisecond timing</b></div>
    </div>
    <div>
      <div style="color:var(--warn);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">TRAILING MONITOR (0.2s loop)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">9.</b> Hard SL = entry − 1.5×ATR (or fixed pts if ATR unavailable)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">10.</b> Trail activates when LTP &gt; entry + trail_start</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">11.</b> Trail exit = peak − trail_step (rounded ₹0.05)</div>
      <div class="gdesc" style="margin-bottom:12px"><b style="color:var(--txt)">12.</b> Force exit at max_time (configurable)</div>
      <div style="color:var(--bear);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">EXIT &amp; RESULT</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">13.</b> POST /v1/order/create (SELL market)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">14.</b> Fetch actual sell price from trades API</div>
      <div class="gdesc"><b style="color:var(--bull)">→ P&L · Buy exec ms · Sell exec ms · Total time shown</b></div>
    </div>
  </div>
</div>

<!-- ── Section 7: Known Issues ── -->
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
  <div class="gcard">
    <div class="gcard-title">⚠️ Known Limitations</div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">ATR when closed</span><span class="gdesc">ATR fetch fails after market hours → falls back to fixed SL. Works fine during market hours.</span></div>
    <div class="grow"><span class="gtag bear" style="font-size:9px">Server restart</span><span class="gdesc">If Python crashes during active trade, trailing SL dies. Exit manually from Groww app.</span></div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">OI snapshot stale warning</span><span class="gdesc">Normal on weekends / after market close — calculate_oi_pcr.py only runs during market hours. Warning clears Monday morning.</span></div>
    <div class="grow"><span class="gtag dim" style="font-size:9px">Token expiry</span><span class="gdesc">Groww JWT token lasts ~30 days. TOTP_SECRET must be real base32 — placeholder breaks login.</span></div>
    <div class="grow"><span class="gtag dim" style="font-size:9px">NSE API</span><span class="gdesc">VIX/PCR only works during market hours — shows N/A when closed.</span></div>
  </div>
  <div class="gcard">
    <div class="gcard-title">🔴 Not Yet Implemented</div>
    <div class="grow"><span class="gtag bear" style="font-size:9px">Auto square-off 3:20 PM</span><span class="gdesc">No auto-exit at market close. Exit positions manually before 3:20 PM.</span></div>
    <div class="grow"><span class="gtag bear" style="font-size:9px">Trade state recovery</span><span class="gdesc">Trade state lost if server restarts during active trade. Monitor from Groww app.</span></div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">Momentum Bot dashboard card</span><span class="gdesc">Momentum Bot appears in bot status bar and Bot Control, but no dedicated live trade card yet.</span></div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">yfinance</span><span class="gdesc">Yahoo Finance for NIFTY history — occasionally breaks (third-party dependency).</span></div>
  </div>
</div>

<!-- ── Section 8: Trading Signals Beginner Guide ── -->
<div class="gcard-title" style="font-size:14px;margin-bottom:16px">📖 Trading Signals Reference</div>
<div class="guide-grid">
  <div class="guide-grid">

    <div class="gcard">
      <div class="gcard-title">🚦 What Do CE and PE Mean?</div>
      <div class="grow"><span class="gtag bull">BUY CE ▲</span><span class="gdesc"><b>Call Option = Bullish.</b> You expect NIFTY to go UP. Profit if it rises above your strike.</span></div>
      <div class="grow"><span class="gtag bear">BUY PE ▼</span><span class="gdesc"><b>Put Option = Bearish.</b> You expect NIFTY to go DOWN. Profit if it falls below your strike.</span></div>
      <div class="grow"><span class="gtag warn">WAIT ─</span><span class="gdesc"><b>No clear direction.</b> Bots disagree or price is at a major level. Stay out.</span></div>
      <hr class="gdivider">
      <div class="grow"><span class="gtag info">BREAK</span><span class="gdesc">Entry type — wait for option price to break above trigger level before buying.</span></div>
      <div class="grow"><span class="gtag bull">NOW</span><span class="gdesc">Entry type — buy at current price immediately; setup is confirmed.</span></div>
    </div>

    <div class="gcard">
      <div class="gcard-title">📊 Consensus Box</div>
      <div class="grow"><span class="gtag bull">STRONG CE</span><span class="gdesc">6+ bull votes — all bots agree. Strongest buy signal.</span></div>
      <div class="grow"><span class="gtag bull">CE ▲</span><span class="gdesc">3–5 bull votes. Mild bullish lean — wait for entry trigger.</span></div>
      <div class="grow"><span class="gtag warn">WAIT ─</span><span class="gdesc">Balanced or unclear — stay out until signal forms.</span></div>
      <div class="grow"><span class="gtag bear">PE ▼</span><span class="gdesc">3–5 bear votes. Mild bearish lean — wait for breakdown.</span></div>
      <div class="grow"><span class="gtag bear">STRONG PE</span><span class="gdesc">6+ bear votes — all bots agree. Strongest sell signal.</span></div>
      <hr class="gdivider">
      <div class="gdesc" style="font-size:11px">Votes: MASTER SIGNAL (3), FIBO (2), CHART (3), SIGNAL MONITOR (2). Added to Bull or Bear total.</div>
    </div>

    <div class="gcard">
      <div class="gcard-title">📐 Key Levels + ★ Stars</div>
      <div class="gdesc" style="margin-bottom:10px">Price zones where market likely reacts. Stars = how many sources agree at that level.</div>
      <div class="gstar-row"><span class="gstar-val">★☆☆☆☆</span><span class="gstar-meaning"><b>Weak</b> — single source. Often ignored.</span></div>
      <div class="gstar-row"><span class="gstar-val">★★★☆☆</span><span class="gstar-meaning"><b>Good</b> — 3 sources. Likely to cause bounce or break.</span></div>
      <div class="gstar-row"><span class="gstar-val">★★★★★</span><span class="gstar-meaning"><b>Very Strong</b> — 5+ sources. Major S/R level. Always reacts here.</span></div>
      <hr class="gdivider">
      <div class="grow"><span class="gtag bear" style="min-width:80px">Red rows</span><span class="gdesc">Resistance above spot — may stop price from rising.</span></div>
      <div class="grow"><span class="gtag bull" style="min-width:80px">Green rows</span><span class="gdesc">Support below spot — may stop price from falling.</span></div>
      <div class="grow"><span class="gtag warn" style="min-width:80px">BLINKING</span><span class="gdesc">Spot within 6 pts of this level right now — be careful.</span></div>
    </div>

    <div class="gcard">
      <div class="gcard-title">🤖 What Each Bot Does</div>
      <div class="grow"><span class="gtag info" style="min-width:120px">MASTER SIGNAL</span><span class="gdesc">Core direction bot — 1H+15M+5M Fibonacci + premium flow → CE/PE/WAIT with confidence.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">FIBONACCI</span><span class="gdesc">Level detector — day + 15M fib zones, confluence, entry/target/SL.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">CHART LEVEL</span><span class="gdesc">S/R analyzer — swing highs/lows, VWAP, pivot points. Triggers sound on CE/PE signal.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">PREMIUM TRACKER</span><span class="gdesc">Options flow — real-time CE/PE premium direction. UP = buyers entering.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">SIGNAL MONITOR</span><span class="gdesc">Signal combiner — merges MASTER + FIBONACCI → single STRONG CE/PE verdict.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">OI PCR ANALYZER</span><span class="gdesc">Open interest analysis — 15 signals, writer bias, smart money, buildup detection.</span></div>
      <div class="grow"><span class="gtag bull" style="min-width:120px">PROD10 BOT</span><span class="gdesc">Manual trade executor — places orders via Trade Board, ATR trailing, PROD10FEB logic.</span></div>
      <div class="grow"><span class="gtag bull" style="min-width:120px">MOMENTUM BOT</span><span class="gdesc">Autonomous scanner — discover → observe 20s → decide → buy → trail SL → repeat.</span></div>
    </div>

  </div>
  </div>

  <div class="gcard" style="margin-bottom:14px">
    <div class="gcard-title">🎨 Color Coding Reference</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px">
      <div><div style="color:var(--bull);font-weight:700;margin-bottom:4px">■ Green / Teal</div><div class="gdesc">Bullish · Up · Profit · CE · Support · Live bot · Good R:R</div></div>
      <div><div style="color:var(--bear);font-weight:700;margin-bottom:4px">■ Red / Pink</div><div class="gdesc">Bearish · Down · Loss · PE · Resistance · Hard SL hit</div></div>
      <div><div style="color:var(--warn);font-weight:700;margin-bottom:4px">■ Yellow / Amber</div><div class="gdesc">Caution · WAIT · Stale data · Moderate signal · Attention needed</div></div>
      <div><div style="color:var(--info);font-weight:700;margin-bottom:4px">■ Blue / Cyan</div><div class="gdesc">Info · SPOT marker · 1H data · Dashboard headers · Neutral OI</div></div>
      <div><div style="color:var(--accent);font-weight:700;margin-bottom:4px">■ Purple</div><div class="gdesc">AI Summary · Claude-generated analysis · AI advisory</div></div>
      <div><div style="color:var(--dim);font-weight:700;margin-bottom:4px">■ Gray / Dim</div><div class="gdesc">Secondary data · Stale signal · Labels · Offline bots</div></div>
    </div>
  </div>

  <div class="gcard">
    <div class="gcard-title">⚡ Quick Trading Workflow</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div>
        <div class="gdesc" style="margin-bottom:8px"><b style="color:var(--info)">Step 1 — Check Consensus</b><br>Big box at top. STRONG CE = buy CE, STRONG PE = buy PE, WAIT = stay out.</div>
        <div class="gdesc" style="margin-bottom:8px"><b style="color:var(--info)">Step 2 — Check OI Intelligence</b><br>Is Market Signal STRONG? Is ATM Momentum ≥60 (BUY NOW)? Do writers agree with direction?</div>
        <div class="gdesc"><b style="color:var(--info)">Step 3 — Check Key Levels</b><br>Is SPOT near a ★★★★★ level? Plan entry above (CE) or below (PE) that level.</div>
      </div>
      <div>
        <div class="gdesc" style="margin-bottom:8px"><b style="color:var(--info)">Step 4 — Check Scalp Plan</b><br>Read ⚡ Scalp Plan at top — gives specific entry, target, SL from Claude.</div>
        <div class="gdesc" style="margin-bottom:8px"><b style="color:var(--info)">Step 5 — Execute</b><br>Use Trade Board (manual) or let Momentum Auto Bot handle entry automatically.</div>
        <div class="gdesc"><b style="color:var(--warn)">🚫 Do NOT trade if:</b><br>WAIT consensus · OI signal contradicts · R:R &lt; 1.5 · Signal &gt;5 min old · Market closes &lt;20 min.</div>
      </div>
    </div>
  </div>

</div><!-- end .guide -->
</div><!-- end #tab-guide -->

<!-- ══════════════════════════════════════════════════════════
     AI BRAIN TAB
════════════════════════════════════════════════════════════ -->
<div id="tab-aibrain" class="tab-pane" style="overflow-y:auto;max-height:calc(100vh - 118px)">
<div id="aibrain-inner" style="padding:16px 18px">

  <div class="mb-ai-wrap">

    <!-- Quick Summary card — AI-generated via Claude CLI, auto-refresh -->
    <div class="mb-ai-toggle-bar" style="background:linear-gradient(135deg,#061220,#071828);border-color:#1e3a5f;flex-direction:column;align-items:flex-start;gap:10px;padding:18px 22px">
      <div style="display:flex;align-items:center;gap:10px;width:100%">
        <div style="font-size:14px;font-weight:800;color:#38bdf8;letter-spacing:.5px">📊 QUICK MARKET SUMMARY</div>
        <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
          <span id="qs-ts" style="font-size:10px;color:var(--dim)"></span>
          <button id="qs-refresh-btn" onclick="qsRefresh()"
            style="display:none;background:rgba(56,189,248,.15);border:1px solid #1e3a5f;color:#38bdf8;border-radius:6px;padding:3px 10px;cursor:pointer;font-size:11px;font-weight:600;transition:all .2s"
            title="Regenerate via Claude AI">↻</button>
          <button id="qs-toggle-btn" class="toggle-btn toggle-off" onclick="qsToggle()" style="font-size:10px;padding:2px 10px">OFF</button>
        </div>
      </div>
      <div id="qs-text" style="font-size:14px;line-height:1.9;color:#e2e8f0;font-family:'Inter',sans-serif;width:100%">
        <span style="color:var(--dim)">Toggle ON to generate a live market summary.</span>
      </div>
      <!-- Status debug line -->
      <div id="qs-status-dbg" style="font-size:10px;color:#334155;margin-top:4px"></div>
    </div>

    <!-- AI Details toggle bar -->
    <div class="mb-ai-toggle-bar" style="margin-top:14px">
      <div>
        <div class="mb-ai-title">🧠 Get AI Details</div>
        <div class="mb-ai-subtitle">Feeds ALL bot signals to Claude AI → Intraday + Long-term view + Key levels + Risks</div>
      </div>
      <button id="mb-ai-toggle-btn" class="toggle-btn toggle-off"
              style="font-size:13px;padding:8px 22px;border-radius:8px"
              onclick="mbAiToggle()">OFF</button>
    </div>

    <!-- AI meta row -->
    <div class="mb-ai-meta" id="mb-ai-meta" style="display:none">
      <span class="mb-ai-ts" id="mb-ai-ts"></span>
      <span class="mb-ai-ctx" id="mb-ai-ctx"></span>
      <button class="mb-ai-refresh-btn" id="mb-ai-refresh-btn" onclick="mbAiRefresh()">↻ Refresh Now</button>
      <span style="font-size:10px;color:var(--dim)">Auto-refreshes every 5 min</span>
    </div>

    <!-- AI content area -->
    <div id="mb-ai-content">
      <div class="mb-ai-idle">
        <div class="idle-icon">🧠</div>
        <div class="idle-msg">AI Brain is OFF</div>
        <div class="idle-sub">Toggle ON above for a detailed Claude AI analysis of all bot signals.<br>
        Reads: India VIX · OI/PCR · Fibonacci · Chart S/R · Master Signal · Momentum Bot · Convergence · Trendline<br>
        Generates INTRADAY (next 1-2h) + LONG-TERM (2-5 day) summary via Claude AI.<br><br>
        <b style="color:#c084fc">Requires:</b> Claude Code CLI logged in (<code>claude login</code>)</div>
      </div>
    </div>

  </div><!-- end .mb-ai-wrap -->

</div><!-- end #aibrain-inner -->
</div><!-- end #tab-aibrain -->

<!-- ══════════════════════════════════════════════════════════
     BOT CONTROL CENTER TAB
════════════════════════════════════════════════════════════ -->
<div id="tab-bots" class="tab-pane" style="padding:16px 18px;overflow-y:auto;max-height:calc(100vh - 118px)">

  <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px">
    <div style="font-size:18px;font-weight:700;color:var(--info)">🤖 Bot Control Center</div>
    <button onclick="botsStartAll()" style="background:var(--bull);color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px;font-weight:600">▶ Start All</button>
    <button onclick="botsStopAll()" style="background:var(--bear);color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px;font-weight:600">■ Stop All</button>
    <span id="bots-last-refresh" style="color:var(--dim);font-size:11px;margin-left:auto"></span>
  </div>

  <div id="bots-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px"></div>

</div><!-- end #tab-bots -->

<!-- ════════════════════════════════════════════════════════════
     DECISION ENGINE TAB (trading_decision_engine)
════════════════════════════════════════════════════════════ -->
<div id="tab-engine" class="tab-pane" style="padding:16px 18px;overflow-y:auto;max-height:calc(100vh - 118px)">

  <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px;flex-wrap:wrap">
    <div style="font-size:18px;font-weight:700;color:var(--info)">⚡ Decision Engine</div>
    <span id="de-mode" class="badge off">OFFLINE</span>
    <span id="de-run" class="badge off">STOPPED</span>
    <span id="de-profile" style="color:var(--warn);font-size:12px;font-weight:600"></span>
    <span id="de-file" style="color:var(--dim);font-size:11px;margin-left:auto"></span>
  </div>

  <!-- launch / config bar -->
  <div class="card" style="margin-bottom:14px">
    <div style="font-size:11px;letter-spacing:1px;color:var(--dim);margin-bottom:10px">LAUNCH CONTROL</div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
      <label style="font-size:11px;color:var(--dim)">Mode<br>
        <select id="dec-mode" onchange="deModeChanged()" style="background:#0a111e;color:var(--txt);border:1px solid var(--bdr);border-radius:6px;padding:5px 8px;margin-top:3px">
          <option value="shadow" selected>shadow (paper)</option>
          <option value="live">live (REAL MONEY)</option>
        </select></label>
      <label style="font-size:11px;color:var(--dim)">Profile<br>
        <select id="dec-profile" style="background:#0a111e;color:var(--txt);border:1px solid var(--bdr);border-radius:6px;padding:5px 8px;margin-top:3px">
          <option value="">none (strategy.json)</option>
        </select></label>
      <label style="font-size:11px;color:var(--dim)">Index<br>
        <select id="dec-index" onchange="deLoadExpiries()" style="background:#0a111e;color:var(--txt);border:1px solid var(--bdr);border-radius:6px;padding:5px 8px;margin-top:3px">
          <option>NIFTY</option><option>BANKNIFTY</option><option>SENSEX</option><option>FINNIFTY</option>
        </select></label>
      <label style="font-size:11px;color:var(--dim)">Expiry<br>
        <select id="dec-expiry" style="background:#0a111e;color:var(--txt);border:1px solid var(--bdr);border-radius:6px;padding:5px 8px;margin-top:3px;min-width:120px">
          <option value="">loading…</option>
        </select></label>
      <label style="font-size:11px;color:var(--dim)">Lots<br>
        <input id="dec-lots" type="number" value="1" min="1" style="width:56px;background:#0a111e;color:var(--txt);border:1px solid var(--bdr);border-radius:6px;padding:5px 8px;margin-top:3px"></label>
      <label style="font-size:11px;color:var(--dim)">Premium min<br>
        <input id="dec-pmin" type="number" value="60" style="width:70px;background:#0a111e;color:var(--txt);border:1px solid var(--bdr);border-radius:6px;padding:5px 8px;margin-top:3px"></label>
      <label style="font-size:11px;color:var(--dim)">Premium max<br>
        <input id="dec-pmax" type="number" value="250" style="width:70px;background:#0a111e;color:var(--txt);border:1px solid var(--bdr);border-radius:6px;padding:5px 8px;margin-top:3px"></label>
      <label style="font-size:11px;color:var(--dim);display:flex;align-items:center;gap:6px;padding-bottom:6px">
        <input id="dec-validate" type="checkbox" checked> validate orders</label>
      <span id="dec-live-confirm-wrap" style="display:none">
        <label style="font-size:11px;color:var(--bear)">Type YES to arm LIVE<br>
          <input id="dec-live-confirm" type="text" placeholder="YES" style="width:80px;background:#1a0a0a;color:var(--bear);border:1px solid var(--bear);border-radius:6px;padding:5px 8px;margin-top:3px"></label></span>
      <button id="dec-start-btn" onclick="deStart()" style="background:var(--bull);color:#04110b;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-size:13px;font-weight:700">▶ Start</button>
      <button id="dec-stop-btn" onclick="deStop()" style="background:var(--bear);color:#fff;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-size:13px;font-weight:700">■ Stop</button>
      <span id="dec-launch-msg" style="font-size:12px;color:var(--dim)"></span>
    </div>
    <div id="dec-console" style="display:none;margin-top:10px;background:#050a12;border:1px solid var(--bdr);border-radius:6px;padding:8px 10px;max-height:180px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--dim);white-space:pre-wrap"></div>
  </div>

  <div id="de-offline-msg" class="card" style="color:var(--dim)">
    No decision-engine session yet — configure above and press <b style="color:var(--bull)">▶ Start</b>
    (shadow mode recommended first). This tab lights up automatically once
    <code>trading_decision_engine/logs/events_*.jsonl</code> starts flowing.
  </div>

  <div id="de-body" style="display:none">
    <!-- row 1: live decision + confidences | stage gates -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;margin-bottom:14px">
      <div class="card" id="de-decision-card"></div>
      <div class="card" id="de-gates-card"></div>
    </div>
    <!-- row 2: engine grid -->
    <div class="card" id="de-engines-card" style="margin-bottom:14px"></div>
    <!-- row 3: session stats | trades -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;margin-bottom:14px">
      <div class="card" id="de-stats-card"></div>
      <div class="card" id="de-trades-card"></div>
    </div>
    <!-- row 4: config / profile -->
    <div class="card" id="de-config-card"></div>
  </div>

</div><!-- end #tab-engine -->

<div id="tab-vix" class="tab-pane" style="padding:16px 18px;overflow-y:auto;max-height:calc(100vh - 118px)">

  <!-- Market Regime card — top of VIX tab -->
  <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:10px;padding:16px;margin-bottom:16px">

    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <span style="font-size:10px;letter-spacing:1px;color:var(--dim)">MARKET REGIME DETECTOR
        <span style="font-size:9px;color:#475569;font-weight:400;letter-spacing:0;margin-left:6px">hover ⓘ cards for details</span>
      </span>
      <span id="mr-badge" style="font-size:11px;font-weight:800;letter-spacing:1.5px;padding:4px 14px;border-radius:20px;border:1px solid currentColor">—</span>
    </div>

    <!-- 4-metric grid — no position:relative so fixed tooltip never clips -->
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px">

      <div style="background:#060d1a;border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:10px 12px;cursor:default"
           data-mrtip="range">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">NIFTY DAY RANGE ⓘ</div>
        <div id="mr-range-pts" style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--txt)">—</div>
        <div id="mr-range-pct" style="font-size:10px;margin-top:3px">—</div>
        <div id="mr-range-lbl" style="font-size:9px;color:var(--dim);margin-top:2px;letter-spacing:.5px">—</div>
      </div>

      <div style="background:#060d1a;border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:10px 12px;cursor:default"
           data-mrtip="straddle">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">ATM STRADDLE ⓘ</div>
        <div id="mr-straddle" style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--txt)">—</div>
        <div id="mr-straddle-sub" style="font-size:10px;color:var(--dim);margin-top:3px">CE + PE LTP</div>
        <div id="mr-straddle-lbl" style="font-size:9px;color:var(--dim);margin-top:2px;letter-spacing:.5px">—</div>
      </div>

      <div style="background:#060d1a;border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:10px 12px;cursor:default"
           data-mrtip="prem">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">PREMIUM ACTIVITY ⓘ</div>
        <div id="mr-prem-move" style="font-size:15px;font-weight:700">—</div>
        <div id="mr-prem-sub" style="font-size:10px;margin-top:3px;color:var(--dim)">CE/PE LTP change</div>
        <div id="mr-prem-lbl" style="font-size:9px;color:var(--dim);margin-top:2px;letter-spacing:.5px">—</div>
      </div>

      <div style="background:#060d1a;border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:10px 12px;cursor:default"
           data-mrtip="score">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:6px">REGIME SCORE ⓘ</div>
        <div id="mr-score-bar" style="display:flex;gap:4px;margin-bottom:6px">
          <span class="mrseg" data-pos="1" style="flex:1;height:8px;border-radius:3px;background:#1e293b"></span>
          <span class="mrseg" data-pos="2" style="flex:1;height:8px;border-radius:3px;background:#1e293b"></span>
          <span class="mrseg" data-pos="3" style="flex:1;height:8px;border-radius:3px;background:#1e293b"></span>
          <span class="mrseg" data-pos="4" style="flex:1;height:8px;border-radius:3px;background:#1e293b"></span>
          <span class="mrseg" data-pos="5" style="flex:1;height:8px;border-radius:3px;background:#1e293b"></span>
          <span class="mrseg" data-pos="6" style="flex:1;height:8px;border-radius:3px;background:#1e293b"></span>
        </div>
        <div id="mr-score" style="font-size:13px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--txt)">—</div>
        <div id="mr-score-breakdown" style="font-size:9px;color:var(--dim);margin-top:3px">Range · Prem · VIX</div>
        <div id="mr-score-lbl" style="font-size:9px;color:var(--dim);margin-top:2px;letter-spacing:.5px">—</div>
      </div>

    </div>

    <!-- Regime verdict text -->
    <div id="mr-verdict" style="font-size:12px;line-height:1.7;color:var(--txt);border-top:1px solid rgba(255,255,255,.06);padding-top:10px">
      Waiting for market data…
    </div>

  </div>

  <!-- Global fixed tooltip for Market Regime cards — never affects layout -->
  <div id="mr-tooltip" style="display:none;position:fixed;z-index:9999;pointer-events:none;
       background:#0c1a30;border:1px solid var(--bdr);border-radius:10px;padding:12px 15px;
       max-width:340px;font-size:11px;line-height:1.7;color:var(--txt);
       box-shadow:0 10px 32px rgba(0,0,0,.8);font-family:'Inter',sans-serif"></div>

  <!-- Top row: big VIX number + stats -->
  <div style="display:grid;grid-template-columns:auto 1fr;gap:16px;align-items:start;margin-bottom:16px">

    <!-- VIX Big Number card -->
    <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:10px;padding:20px 28px;text-align:center;min-width:160px">
      <div style="font-size:10px;letter-spacing:1.5px;color:var(--dim);margin-bottom:6px">INDIA VIX</div>
      <div id="vt-curr" style="font-size:52px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1">—</div>
      <div id="vt-regime" style="font-size:11px;font-weight:700;letter-spacing:1.2px;margin-top:6px">—</div>
      <div style="margin-top:10px;display:flex;gap:10px;justify-content:center;font-size:10px;font-family:'JetBrains Mono',monospace">
        <span style="color:var(--dim)">SESS Δ <span id="vt-sess-chg" style="color:var(--txt)">—</span></span>
        <span style="color:var(--dim)">10m <span id="vt-10m" style="color:var(--txt)">—</span></span>
      </div>
      <div style="margin-top:6px;font-size:9px;color:var(--dim)">Updated <span id="vt-ts">—</span></div>
    </div>

    <!-- Stats row -->
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px">

      <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:8px;padding:12px 14px">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">SESSION HIGH</div>
        <div id="vt-hi" style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--bear)">—</div>
      </div>

      <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:8px;padding:12px 14px">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">SESSION LOW</div>
        <div id="vt-lo" style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--bull)">—</div>
      </div>

      <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:8px;padding:12px 14px">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">SESSION OPEN</div>
        <div id="vt-open" style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--txt)">—</div>
      </div>

      <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:8px;padding:12px 14px">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">VIX RANGE</div>
        <div id="vt-range" style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--info)">—</div>
        <div style="font-size:9px;color:var(--dim);margin-top:2px">session spread</div>
      </div>

      <!-- Velocity card -->
      <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:8px;padding:12px 14px">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">VELOCITY (30m)</div>
        <div id="vt-vel" style="font-size:16px;font-weight:700;font-family:'JetBrains Mono',monospace">—</div>
        <div id="vt-vel-lbl" style="font-size:9px;color:var(--dim);margin-top:2px">—</div>
      </div>

      <!-- Trend -->
      <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:8px;padding:12px 14px">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">TREND</div>
        <div id="vt-trend" style="font-size:16px;font-weight:700">—</div>
        <div id="vt-trend-lbl" style="font-size:9px;color:var(--dim);margin-top:2px">—</div>
      </div>

      <!-- Options premium implication -->
      <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:8px;padding:12px 14px">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">OPTION PREMIUM</div>
        <div id="vt-prem" style="font-size:13px;font-weight:700">—</div>
        <div id="vt-prem-lbl" style="font-size:9px;color:var(--dim);margin-top:2px">—</div>
      </div>

      <!-- Data points -->
      <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:8px;padding:12px 14px">
        <div style="font-size:9px;color:var(--dim);letter-spacing:.8px;margin-bottom:4px">DATA POINTS</div>
        <div id="vt-pts" style="font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--txt)">—</div>
        <div style="font-size:9px;color:var(--dim);margin-top:2px">2-min ticks today</div>
      </div>

    </div>
  </div>

  <!-- Full-width sparkline -->
  <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:10px;padding:14px;margin-bottom:16px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <span style="font-size:10px;letter-spacing:1px;color:var(--dim)">INTRADAY VIX CHART</span>
      <span id="vt-spark-tt" style="display:none;font-size:10px;font-family:'JetBrains Mono',monospace;color:var(--info)"></span>
    </div>
    <canvas id="vt-sparkline" style="width:100%;height:130px;display:block"></canvas>
    <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--dim);margin-top:4px;font-family:'JetBrains Mono',monospace">
      <span id="vt-spark-t0">—</span>
      <span style="opacity:.5">hover for value</span>
      <span id="vt-spark-tn">—</span>
    </div>
  </div>

  <!-- Analysis + Regime + Recent ticks -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">

    <!-- Analysis card -->
    <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:10px;padding:16px">
      <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:12px">ANALYSIS</div>
      <div id="vt-analysis" style="font-size:12px;line-height:1.7;color:var(--txt)">Waiting for VIX data…</div>
    </div>

    <!-- Recent ticks log -->
    <div style="background:#0a111e;border:1px solid var(--bdr);border-radius:10px;padding:16px">
      <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:10px">RECENT TICKS (last 15)</div>
      <div id="vt-ticks" style="font-size:11px;font-family:'JetBrains Mono',monospace;line-height:1.9;max-height:260px;overflow-y:auto"></div>
    </div>

  </div>

</div><!-- end #tab-vix -->

<div class="footer" id="footer">Last updated: — | Read-only — no API calls — log aggregator only</div>

<script>
const R=15; let cd=R, tim;
const $=id=>document.getElementById(id);

function fmt(n,d=2){
  if(n==null||n===0) return '—';
  return Number(n).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d});
}

function badge(data, label){
  if(!data||!data.ts) return `<div class="badge off"><div class="dot doff"></div>${label} OFFLINE</div>`;
  const live=data._live, cls=live?'live':'stale', dc=live?'dlive':'dstale', tag=live?'LIVE':'STALE';
  return `<div class="badge ${cls}"><div class="dot ${dc}"></div>${label} ${tag} <span style="opacity:.6">${data._age||''}</span></div>`;
}
function oiBadge(oi, label){
  if(!oi||!oi.time) return `<div class="badge off"><div class="dot doff"></div>${label} OFFLINE</div>`;
  const live=!oi._stale, cls=live?'live':'stale', dc=live?'dlive':'dstale', tag=live?'LIVE':'STALE';
  const s=oi._age_sec||0, age=s<60?s+'s ago':Math.floor(s/60)+'m ago';
  return `<div class="badge ${cls}"><div class="dot ${dc}"></div>${label} ${tag} <span style="opacity:.6">${age}</span></div>`;
}

function chip(lbl, v){
  v=parseFloat(v)||0;
  const cls=v>0?'cup':v<0?'cdn':'cfl';
  const arr=v>0?'▲'.repeat(Math.min(3,Math.abs(Math.round(v)))):v<0?'▼'.repeat(Math.min(3,Math.abs(Math.round(v)))):'─';
  return `<div class="chip ${cls}">${lbl} ${arr}</div>`;
}

function flowCls(s){
  s=(s||'').toUpperCase();
  if(s.includes('UP')||s.includes('BULL')) return 'fup';
  if(s.includes('DOWN')||s.includes('BEAR')||s.includes('FALL')) return 'fdn';
  return 'fst';
}

function render(d){
  if(!d) return;
  renderPnl(d);
  renderVix(d);
  renderVixTab(d);
  const b=d.bots||{}, master=b.master||{}, fibo=b.fibo||{},
        csig=b.chart_signal||{}, cdec=b.chart_decision||{},
        prem=b.premium||{}, trade=b.trade||{}, momentum_bot=b.momentum_bot||{},
        trendline_bot=b.trendline_bot||{},
        sigmon=b.signal_monitor||{},
        cons=d.consensus||{}, liveChain=(d.live_chain||{}).chain||{};
  const spot=d.spot||0;

  // Recompute fib/confluence distances against the LIVE spot — the dist_pts
  // parsed from bot logs were computed at log time and go stale off-market
  if(spot>0){
    (fibo.fib_levels||[]).forEach(f=>{ if(f.price) f.dist_pts=f.price-spot; });
    (fibo.confluence||[]).forEach(c=>{ if(c.price) c.dist_pts=c.price-spot; });
  }

  // Header
  $('htitle').textContent=`📡 ${d.index||'NIFTY'} LIVE DASHBOARD`;
  // icp-nifty and chain-spot updated by _pollIndices every 1s — fallback from bots
  if(spot && spot > 0 && !_tbChainSpot){ _tbChainSpot=spot; const np=$('icp-nifty'); if(np) np.textContent=fmt(spot); }

  // Market indices ticker
  // mkt-ticker is driven by its own 1s poller (startIdxTick) — not from 15s snapshot
  // htime is now driven by startTick() live clock — not overwritten here

  // Bot bar
  $('bbar').innerHTML=`<span style="color:var(--dim);font-size:11px">BOT STATUS:</span>
    ${oiBadge(d.oi_snapshot,'OI PCR')}
    ${badge(master,'MASTER SIGNAL')}
    ${badge(fibo,'FIBONACCI')}
    ${badge(cdec.ts?cdec:csig,'CHART LEVEL')}
    ${badge(prem,'PREMIUM TRACKER')}
    ${badge(trade,'PROD10 BOT')}
    ${badge(momentum_bot,'MOMENTUM BOT')}
    ${badge(trendline_bot,'SCANNER')}
    ${badge(sigmon,'SIGNAL MONITOR')}`;

  // Consensus
  const cb=$('cbox'); cb.className=`cons ${cons.cls||'neutral'}`;
  $('csig').textContent=cons.signal||'—';
  $('csmry').textContent=cons.summary||'';
  $('csrc').textContent=(cons.sources||[]).join('  │  ');
  $('cbull').textContent=cons.bull??'—';
  $('cbear').textContent=cons.bear??'—';

  // Key Levels — confluence zones first, then individual non-overlapping fib levels
  // Excludes extension levels (E127%, E161%, E261%) which are far projections not S/R
  if(fibo.fib_levels&&fibo.fib_levels.length){
    const conf   = fibo.confluence || [];
    const fibs   = fibo.fib_levels || [];
    const TOL    = 12; // pts — levels within this of a confluence are "covered"

    // Helper: find stars for a price (tolerance-based, not exact match)
    function getStars(price){
      const c = conf.find(c => Math.abs(c.price - price) <= TOL);
      return c ? c.stars : 0;
    }

    // Build merged level list
    const merged = [];

    // 1. All confluence zones
    conf.forEach(c => {
      merged.push({ price: c.price, label: c.tags.split(',')[0].trim(),
                    dist_pts: c.price - spot, stars: c.stars, isConf: true });
    });

    // 2. Individual fib levels NOT within TOL of any confluence, NOT extension levels
    fibs.forEach(f => {
      if(f.label.startsWith('E')) return; // skip extension projections
      const covered = conf.some(c => Math.abs(c.price - f.price) <= TOL);
      if(!covered) merged.push({ price: f.price, label: f.label,
                                  dist_pts: f.price - spot, stars: 0, isConf: false });
    });

    // 3. Swing levels are always important — force-add if not covered
    fibs.filter(f => f.label.includes('SWING')).forEach(f => {
      const already = merged.some(m => Math.abs(m.price - f.price) <= TOL);
      if(!already) merged.push({ price: f.price, label: f.label,
                                  dist_pts: f.price - spot, stars: 0, isConf: false });
    });

    const sorted = merged.sort((a,b) => b.price - a.price);
    const above  = sorted.filter(l => l.price > spot).slice(-5);
    const below  = sorted.filter(l => l.price < spot).slice(0, 5);

    // Mark only the single nearest level on each side for blinking
    if(above.length) above[above.length-1]._nearestSide = true;   // last in above = closest to spot
    if(below.length) below[0]._nearestSide = true;                 // first in below = closest to spot

    function starTooltip(n){
      const rows=[
        ['★☆☆☆☆','Weak — 1 source. Price may or may not react.'],
        ['★★☆☆☆','Moderate — 2 sources agree. Worth watching.'],
        ['★★★☆☆','Good — 3 sources confluent. Likely reaction zone.'],
        ['★★★★☆','Strong — 4 sources. High probability S/R level.'],
        ['★★★★★','Very Strong — 5+ sources. Major level. Almost always reacts.'],
      ];
      const tip = rows.slice(0, Math.min(n,5)).map(([s,d])=>
        `<div class="stip-row"><span class="stip-star">${s}</span><span>${d}</span></div>`
      ).join('');
      return `<div class="stip"><div class="stip-title">Confluence Stars</div>${tip}</div>`;
    }

    const SWING_DANGER_PTS = 30; // highlight SWING levels within this many pts

    function lvlRow(l, isAbove){
      const dist    = Math.abs(l.dist_pts);
      const atl     = l._nearestSide && dist <= 15 && spot > 0;
      const stars   = l.stars || getStars(l.price);
      const starStr = stars > 0 ? '★'.repeat(Math.min(stars,5)) + '☆'.repeat(Math.max(0,5-stars)) : '';
      const starCls = stars >= 3 ? 's3' : stars > 0 ? 's2' : '';
      const distCol = isAbove ? 'color:var(--bear)' : 'color:var(--bull)';
      const arr     = isAbove ? '▲' : '▼';
      const rowCls  = isAbove ? 'ar' : 'br';
      const bold    = l.isConf ? 'font-weight:bold' : '';
      const starCell= stars > 0
        ? `<td class="${starCls} star-cell" title="">${starStr}${starTooltip(stars)}</td>`
        : `<td></td>`;
      const isSwingDanger = l.label.includes('SWING') && dist <= SWING_DANGER_PTS;
      const warnTag = isSwingDanger
        ? `<span style="font-size:9px;background:var(--warn);color:#000;font-weight:700;padding:1px 5px;border-radius:3px;margin-right:5px">⚠</span>`
        : '';
      const rowStyle = isSwingDanger ? 'background:rgba(255,170,0,0.1);border-left:3px solid var(--warn)' : '';
      return `<tr class="${rowCls}" style="${rowStyle}">
        <td style="${bold}">${warnTag}${l.label}</td>
        <td class="${atl?'atl':''}" style="${bold}">${fmt(l.price)}</td>
        <td style="${distCol}">${arr}${dist.toFixed(0)}pts</td>
        ${starCell}</tr>`;
    }

    let html = '';
    above.forEach(l => html += lvlRow(l, true));
    html += `<tr class="srow"><td colspan="4">◄ SPOT ${fmt(spot,2)} ►</td></tr>`;
    below.forEach(l => html += lvlRow(l, false));
    $('lvlbody').innerHTML = html;
    $('lvl-age').textContent = fibo._age||'';
    $('lvl-chart-tip').innerHTML = buildLvlChartTip(fibo);

    // Swing danger banner
    const sd = $('swing-danger');
    const dangerBelow = below.find(l => l.label.includes('SWING') && Math.abs(l.dist_pts) <= SWING_DANGER_PTS);
    const dangerAbove = above.find(l => l.label.includes('SWING') && Math.abs(l.dist_pts) <= SWING_DANGER_PTS);
    let bannerHtml = '';
    if(dangerBelow){
      const pts = Math.abs(dangerBelow.dist_pts).toFixed(0);
      bannerHtml += `<div style="padding:7px 10px;background:rgba(255,170,0,0.1);border:1px solid var(--warn);border-radius:6px;font-size:11px;color:var(--warn);line-height:1.5">
        ⚠ <b>SUPPORT ZONE — ${dangerBelow.label} only ${pts}pts below (${fmt(dangerBelow.price)})</b><br>
        <span style="color:var(--fg)">Market may bounce here. If in PE → <b>book partial profit</b> or wait for a confirmed candle close <b>below ${fmt(dangerBelow.price)}</b> before holding.</span>
      </div>`;
    }
    if(dangerAbove){
      const pts = Math.abs(dangerAbove.dist_pts).toFixed(0);
      bannerHtml += `<div style="padding:7px 10px;margin-top:${bannerHtml?'6px':'0'};background:rgba(255,170,0,0.1);border:1px solid var(--warn);border-radius:6px;font-size:11px;color:var(--warn);line-height:1.5">
        ⚠ <b>RESISTANCE ZONE — ${dangerAbove.label} only ${pts}pts above (${fmt(dangerAbove.price)})</b><br>
        <span style="color:var(--fg)">Market may reject here. If in CE → <b>book partial profit</b> or wait for a confirmed candle close <b>above ${fmt(dangerAbove.price)}</b> before holding.</span>
      </div>`;
    }
    if(bannerHtml){ sd.style.display=''; sd.innerHTML=bannerHtml; }
    else { sd.style.display='none'; sd.innerHTML=''; }
  } else if(fibo.ts) {
    // Bot is running but levels not built yet (market closed / building)
    $('lvlbody').innerHTML='<tr><td colspan="4" style="color:var(--warn);text-align:center;padding:14px">⏳ Market closed — Fibonacci levels will appear when market opens</td></tr>';
    $('lvl-age').textContent=fibo._age||'';
    const sd=$('swing-danger'); if(sd){sd.style.display='none';sd.innerHTML='';}
  }

  // Master Signal
  if(master.direction){
    const dc=master.direction==='CE'?'vbull':master.direction==='PE'?'vbear':'vwarn';
    const rrc=parseFloat(master.rr||0)>=2?'vbull':parseFloat(master.rr||0)>=1?'vwarn':'vbear';
    $('master-body').innerHTML=`
      <div class="row"><span class="lbl">Direction</span><span class="${dc}" style="font-size:15px">${master.direction} ${parseFloat(master.confidence||0).toFixed(1)}%</span></div>
      <div class="row"><span class="lbl">Zone</span><span class="vinfo" style="font-size:11px">${master.zone||'—'}</span></div>
      <div class="row"><span class="lbl">Pattern</span><span class="v">${master.pattern||'—'}</span></div>
      <div class="chips">${chip('1H',master.s1h)}${chip('15M',master.s15m)}${chip('5M',master.s5m)}${chip('Prem',master.sprem)}</div>
      <div class="row"><span class="lbl">RSI 1H/15M</span><span class="v">${parseFloat(master.rsi1h||0).toFixed(0)} / ${parseFloat(master.rsi15m||0).toFixed(0)}</span></div>
      ${master.stop?`<div class="row"><span class="lbl">Stop</span><span class="vbear">${fmt(master.stop,1)}</span></div>`:''}
      ${master.target?`<div class="row"><span class="lbl">Target</span><span class="vbull">${fmt(master.target,1)}</span></div>`:''}
      ${master.rr?`<div class="row"><span class="lbl">R:R</span><span class="${rrc}">${parseFloat(master.rr).toFixed(1)}:1</span></div>`:''}
      <div style="margin-top:8px;border-top:1px solid var(--bdr);padding-top:7px;">
        ${master.sl15m?`<div class="row"><span class="lbl">15M Floor</span><span class="vbear">${fmt(master.sl15m,1)}</span></div>`:''}
        ${master.sh15m?`<div class="row"><span class="lbl">15M Ceiling</span><span class="vbull">${fmt(master.sh15m,1)}</span></div>`:''}
      </div>`;
    $('master-age').textContent=master._age||'';
  }

  // ── Chart-drawing tips (dynamic, based on live fibo data) ──────────────────
  function buildFiboChartTip(fb) {
    if (!fb.day_high || !fb.day_low) return '';
    const bearish  = (fb.day_dir || 'bearish').toLowerCase().startsWith('bear');
    const dH = fb.day_high, dL = fb.day_low;
    const dRange   = Math.round(dH - dL);
    const dayS1    = bearish ? dH : dL, dayS2 = bearish ? dL : dH;
    const dayArrow = bearish ? 'HIGH → LOW' : 'LOW → HIGH';
    const dayDesc  = bearish ? 'bearish day — high formed first, then fell'
                             : 'bullish day — low formed first, then rose';
    const dayCol   = bearish ? 'var(--bear)' : 'var(--bull)';

    // pick key day fib levels from the parsed array
    const WANTED   = ['R23.6%','R38.2%','R50.0%','R61.8%','R78.6%',
                       'S23.6%','S38.2%','S50.0%','S61.8%','S78.6%'];
    const dayFibs  = (fb.fib_levels||[])
      .filter(f => WANTED.includes(f.label))
      .sort((a,b) => b.price - a.price)
      .slice(0, 4)
      .map(f => `<span style="color:var(--dim)">${f.label}=</span><b>${Math.round(f.price)}</b>`)
      .join('&nbsp;&nbsp;');

    let h = `<div class="ctip-wrap">`;
    h += `<div class="ctip-title">✏️ HOW TO DRAW ON CHART &nbsp;<span style="color:var(--dim);font-weight:normal;letter-spacing:0">(TradingView / Zerodha Kite)</span></div>`;
    h += `<div class="ctip-sub">Tool: <b style="color:var(--txt)">Fib Retracement</b> &nbsp;|&nbsp; Step 1 = first click &nbsp;|&nbsp; Step 2 = drag &amp; release</div>`;

    // [1] Day Fib
    h += `<div class="ctip-block">`;
    h += `<span class="ctip-num">[1] DAY FIB</span> &nbsp;<span style="color:${dayCol}">(${dayArrow}</span> &nbsp;|&nbsp; <span style="color:var(--dim)">${dayDesc})</span><br>`;
    h += `&nbsp;&nbsp;Step 1 → click &nbsp;<b style="color:var(--info)">${Math.round(dayS1)}</b> <span style="color:var(--dim)">(Day ${bearish?'HIGH':'LOW'} 0%)</span><br>`;
    h += `&nbsp;&nbsp;Step 2 → drag &nbsp;&nbsp;<b style="color:var(--info)">${Math.round(dayS2)}</b> <span style="color:var(--dim)">(Day ${bearish?'LOW':'HIGH'} 100%) &nbsp;range ${dRange} pts</span>`;
    if (dayFibs) h += `<br>&nbsp;&nbsp;<span style="color:var(--dim)">Key levels: </span>${dayFibs}`;
    h += `</div>`;

    // [2] 15-Min Fib
    const sw15H = fb.swing_high_15m, sw15L = fb.swing_low_15m;
    if (sw15H && sw15L) {
      const r15    = Math.round(Math.abs(sw15H - sw15L));
      const s1_15  = bearish ? sw15H : sw15L, s2_15 = bearish ? sw15L : sw15H;
      const col15  = bearish ? 'var(--bear)' : 'var(--bull)';
      const arr15  = bearish ? 'HIGH → LOW' : 'LOW → HIGH';
      // fib levels in the 15m range
      const lo15 = Math.min(sw15H, sw15L), hi15 = Math.max(sw15H, sw15L);
      const fibs15 = (fb.fib_levels||[])
        .filter(f => !f.label.includes('SWING') && !f.label.startsWith('E'))
        .filter(f => f.price >= lo15 - 5 && f.price <= hi15 + 5)
        .sort((a,b) => b.price - a.price)
        .slice(0, 3)
        .map(f => `<span style="color:var(--dim)">${f.label}=</span><b>${Math.round(f.price)}</b>`)
        .join('&nbsp;&nbsp;');
      h += `<div class="ctip-block">`;
      h += `<span class="ctip-num">[2] 15-MIN FIB</span> &nbsp;<span style="color:${col15}">(${arr15}</span> &nbsp;|&nbsp; <span style="color:var(--dim)">bearish swing on 15-min | range ${r15} pts)</span><br>`;
      h += `&nbsp;&nbsp;Step 1 → click &nbsp;<b style="color:var(--info)">${Math.round(s1_15)}</b> <span style="color:var(--dim)">(Swing ${bearish?'HIGH':'LOW'} 0%)</span><br>`;
      h += `&nbsp;&nbsp;Step 2 → drag &nbsp;&nbsp;<b style="color:var(--info)">${Math.round(s2_15)}</b> <span style="color:var(--dim)">(Swing ${bearish?'LOW':'HIGH'} 100%)</span>`;
      if (fibs15) h += `<br>&nbsp;&nbsp;<span style="color:var(--dim)">Key levels: </span>${fibs15}`;
      h += `</div>`;
    }

    // [3] Top confluence zones
    const topConf = (fb.confluence||[]).filter(c => c.stars >= 3).slice(0, 4);
    if (topConf.length) {
      h += `<div class="ctip-block">`;
      h += `<span class="ctip-num">[3] MARK THESE CONFLUENCE ZONES</span> <span style="color:var(--dim)">(horizontal lines)</span><br>`;
      topConf.forEach(c => {
        const above  = c.dist_pts > 0;
        const col    = above ? 'var(--bear)' : 'var(--bull)';
        const dir    = above ? 'resistance ↑' : 'support ↓';
        const stars  = '★'.repeat(Math.min(c.stars, 5));
        const absDp  = Math.abs(c.dist_pts).toFixed(0);
        h += `&nbsp;&nbsp;<span style="color:var(--warn)">${stars}</span>&nbsp; <b>${Math.round(c.price)}</b> <span style="color:var(--dim)">(${c.dist_pts>0?'+':''}${absDp} pts) </span><span style="color:${col}">${dir}</span> <span style="color:var(--dim);font-size:9.5px">[${c.tags}]</span><br>`;
      });
      h += `<span style="color:var(--dim);font-size:9.5px">&nbsp;&nbsp;More stars = stronger zone — price likely to react here</span>`;
      h += `</div>`;
    }

    h += `<div style="color:var(--dim);font-size:9.5px;margin-top:4px">💡 TIP: On TradingView use 'Fib Retracement' from the left toolbar.</div>`;
    h += `</div>`;
    return h;
  }

  function buildLvlChartTip(fb) {
    const fibs  = fb.fib_levels || [];
    const conf  = fb.confluence || [];
    if (!fibs.length && !conf.length) return '';

    // Key structural levels to always draw
    const structural = fibs.filter(f => f.label.includes('SWING'));
    // Top confluence zones
    const top = conf.filter(c => c.stars >= 3).slice(0, 4);
    // Individual high-star fib levels not already in confluence
    const TOL = 12;
    const solo = fibs
      .filter(f => !f.label.includes('SWING') && !f.label.startsWith('E'))
      .filter(f => !conf.some(c => Math.abs(c.price - f.price) <= TOL))
      .sort((a,b) => Math.abs(a.dist_pts) - Math.abs(b.dist_pts))
      .slice(0, 3);

    let h = `<div class="ctip-wrap">`;
    h += `<div class="ctip-title">✏️ HOW TO DRAW ON CHART &nbsp;<span style="color:var(--dim);font-weight:normal;letter-spacing:0">(TradingView / Zerodha Kite)</span></div>`;
    h += `<div class="ctip-sub">Draw <b style="color:var(--txt)">horizontal lines</b> at each price. Thicker line = more stars. Right-click → "Add horizontal line".</div>`;

    if (structural.length) {
      h += `<div class="ctip-block">`;
      h += `<span class="ctip-num">[1] STRUCTURAL LEVELS</span> <span style="color:var(--dim)">(always mark — key swing points)</span><br>`;
      structural.forEach(f => {
        const above = f.dist_pts > 0;
        const col   = above ? 'var(--bear)' : 'var(--bull)';
        const role  = above ? 'resistance' : 'support';
        h += `&nbsp;&nbsp;<span style="color:var(--warn)">⚠</span>&nbsp; <b>${Math.round(f.price)}</b> <span style="color:var(--dim)">(${f.label}) </span><span style="color:${col}">${role}</span> <span style="color:var(--dim)">${Math.abs(f.dist_pts).toFixed(0)} pts away</span><br>`;
      });
      h += `</div>`;
    }

    if (top.length) {
      h += `<div class="ctip-block">`;
      h += `<span class="ctip-num">[2] CONFLUENCE ZONES</span> <span style="color:var(--dim)">(draw thicker / shade as zone)</span><br>`;
      top.forEach(c => {
        const above = c.dist_pts > 0;
        const col   = above ? 'var(--bear)' : 'var(--bull)';
        const role  = above ? 'resistance ↑' : 'support ↓';
        const stars = '★'.repeat(Math.min(c.stars, 5));
        h += `&nbsp;&nbsp;<span style="color:var(--warn)">${stars}</span>&nbsp; <b>${Math.round(c.price)}</b> <span style="color:${col}">${role}</span> <span style="color:var(--dim);font-size:9.5px">[${c.tags}]</span><br>`;
      });
      h += `<span style="color:var(--dim);font-size:9.5px">&nbsp;&nbsp;Shade ±5 pts around each zone for reaction buffer</span>`;
      h += `</div>`;
    }

    if (solo.length) {
      h += `<div class="ctip-block">`;
      h += `<span class="ctip-num">[3] INDIVIDUAL FIB LEVELS</span> <span style="color:var(--dim)">(thin dashed lines)</span><br>`;
      solo.forEach(f => {
        const above = f.dist_pts > 0;
        const col   = above ? 'var(--bear)' : 'var(--bull)';
        h += `&nbsp;&nbsp;<b>${Math.round(f.price)}</b> <span style="color:var(--dim)">${f.label}</span> <span style="color:${col}">${above?'↑':'↓'}${Math.abs(f.dist_pts).toFixed(0)} pts</span><br>`;
      });
      h += `</div>`;
    }

    h += `<div style="color:var(--dim);font-size:9.5px;margin-top:4px">💡 Color code: <span style="color:var(--bear)">Red = resistance</span> &nbsp;|&nbsp; <span style="color:var(--bull)">Green = support</span> &nbsp;|&nbsp; <span style="color:var(--warn)">Yellow = structural</span></div>`;
    h += `</div>`;
    return h;
  }

  // Fibonacci
  if(fibo.day_high){
    const pct=fibo.day_high&&fibo.day_low?((spot-fibo.day_low)/(fibo.day_high-fibo.day_low)*100).toFixed(0):'—';
    let h=`<div class="row"><span class="lbl">Day Range</span><span class="v">
              <span class="vbull">H ${fmt(fibo.day_high,0)}</span>  <span class="vbear">L ${fmt(fibo.day_low,0)}</span></span></div>
           <div class="row"><span class="lbl">Position</span><span class="v">${pct}% (${(fibo.day_dir||'').toUpperCase()})</span></div>`;
    if(fibo.zone_1h) h+=`<div class="row"><span class="lbl">1H Zone</span><span class="vinfo" style="font-size:11px">${fibo.zone_1h}</span></div>`;
    (fibo.confluence||[]).slice(0,4).forEach(c=>{
      const dp     = parseFloat(c.dist_pts), cls=dp>0?'vbear':'vbull', arr=dp>0?'▲':'▼';
      const n      = Math.min(c.stars, 10);
      const filled = '★'.repeat(n);
      const empty  = '☆'.repeat(Math.max(0, 5 - n));
      const starCls= n >= 7 ? 's3' : n >= 4 ? 's2' : '';
      // Tooltip data stored in data-fstars / data-ftags — built in showFibTip()
      // Store tooltip data in data-* attrs (no HTML in attributes = no escaping issues)
      h+=`<div class="row" style="cursor:help"
            data-fstars="${n}" data-ftags="${(c.tags||'').replace(/"/g,'&quot;')}"
            onmouseenter="showFibTip(this)" onmouseleave="hideFibTip()">
        <span class="lbl ${starCls}" style="letter-spacing:1px">${filled}${empty} ${fmt(c.price,0)}</span>
        <span class="${cls}">${arr}${Math.abs(dp).toFixed(0)}pts <span style="color:var(--dim);font-size:10px">[${c.tags}]</span></span>
      </div>`;
    });
    h+=`<div style="margin-top:8px;border-top:1px solid var(--bdr);padding-top:7px;">`;
    if(fibo.ce_trigger) h+=`<div class="row"><span class="lbl" style="color:var(--bull)">CE trigger</span><span class="vbull">${fibo.ce_trigger}</span></div>`;
    if(fibo.pe_trigger) h+=`<div class="row"><span class="lbl" style="color:var(--bear)">PE trigger</span><span class="vbear">${fibo.pe_trigger}</span></div>`;
    h+=`</div>`;
    if(fibo.trade_setup) h+=`<div style="margin-top:7px;font-size:11px;color:#9ca3af">${fibo.trade_setup.substring(0,140)}</div>`;
    $('fibo-body').innerHTML=h;
    $('fibo-age').textContent=fibo._age||'';
    $('fibo-chart-tip').innerHTML=buildFiboChartTip(fibo);
  } else if(fibo.ts) {
    // Bot is running but day range not available yet (market closed / building)
    const sumText = fibo.summary ? `<div style="color:var(--dim);font-size:11px;margin-top:6px;line-height:1.5">${fibo.summary}</div>` : '';
    $('fibo-body').innerHTML=`<div style="color:var(--warn);font-size:11px;padding:2px 0">⏳ Market closed — Day Fib builds after market opens</div>${sumText}`;
    $('fibo-age').textContent=fibo._age||'';
  }

  // Option suggestion — border colour reflects freshness: green<2m, yellow<5m, grey=stale
  const oc=$('opt-card');
  if(csig.direction&&csig.direction!=='NONE'){
    const isCE   = csig.direction==='CE';
    const ageSec = csig._age ? (csig._age.includes('m') ? parseInt(csig._age)*60 :
                                csig._age.includes('h') ? parseInt(csig._age)*3600 :
                                parseInt(csig._age)) : 9999;
    const fresh  = ageSec <= 120;   // ≤2min
    const recent = ageSec <= 300;   // ≤5min

    // Border: green if fresh, yellow if recent, grey if old
    let bdrStyle = '';
    if(fresh)       bdrStyle = `border:2px solid var(--${isCE?'bull':'bear'})`;
    else if(recent) bdrStyle = 'border:2px solid var(--warn)';
    else            bdrStyle = 'border:1px solid var(--bdr)';
    oc.className='card'; oc.setAttribute('style', bdrStyle);

    const rrc = parseFloat(csig.rr_ratio||0)>=2?'vbull':'vwarn';
    const freshTag = fresh  ? '' :
                     recent ? `<span style="color:var(--warn);font-size:11px"> ⚠ ${csig._age} — verify before entry</span>` :
                              `<span style="color:var(--dim);font-size:11px"> ✗ OLD SIGNAL (${csig._age}) — do not act</span>`;
    const dirStyle = fresh ? (isCE?'ce':'pe') : '';

    // LTP comparison: signal LTP (when alarm fired) vs current live LTP
    // Priority: 1) live_option_ltp (direct Groww API call, ~30s)
    //           2) live_chain.json (written by CHART_LEVEL_ANALYZER)
    //           3) ltp_by_key from log   4) exact-match current
    const sigLTP    = parseFloat(csig.option_ltp || 0);
    const loltp     = d.live_option_ltp || {};
    const directLTP = (loltp.strike == csig.strike && loltp.direction === csig.direction && loltp.ltp > 0)
                      ? parseFloat(loltp.ltp) : 0;
    const chainEntry = liveChain[String(csig.strike)];
    const chainLTP   = chainEntry ? parseFloat(csig.direction==='PE' ? chainEntry.pe_ltp : chainEntry.ce_ltp) || 0 : 0;
    const ltpKey  = `${csig.strike}_${csig.direction}`;
    const nowLTP  = directLTP > 0 ? directLTP
                    : chainLTP > 0 ? chainLTP
                    : (cdec.ltp_by_key && cdec.ltp_by_key[ltpKey])
                      ? parseFloat(cdec.ltp_by_key[ltpKey])
                      : (cdec.current_strike === csig.strike && cdec.current_dir === csig.direction)
                        ? parseFloat(cdec.current_ltp || 0) : 0;
    const ltpDiff = (nowLTP > 0 && sigLTP > 0) ? (nowLTP - sigLTP) : null;
    const ltpDiffStr = ltpDiff !== null
      ? (ltpDiff >= 0
          ? `<span class="vbull">▲ +₹${ltpDiff.toFixed(0)}</span>`
          : `<span class="vbear">▼ −₹${Math.abs(ltpDiff).toFixed(0)}</span>`)
      : '';
    const ltpRow = nowLTP > 0
      ? `<div class="row" style="background:var(--bg3);border-radius:6px;padding:6px 8px;margin-bottom:6px;">
           <span class="lbl">LTP at signal</span>
           <span style="display:flex;gap:12px;align-items:center">
             <span class="vdim" style="font-size:11px;text-decoration:line-through">₹${sigLTP.toFixed(0)}</span>
             <span style="color:var(--dim);font-size:10px">→</span>
             <span class="v" style="font-size:15px;font-family:'JetBrains Mono',monospace">₹${nowLTP.toFixed(0)}</span>
             ${ltpDiffStr}
             <span style="color:var(--dim);font-size:10px">(now)</span>
           </span>
         </div>`
      : `<div class="row"><span class="lbl">LTP at signal</span><span class="v">₹${sigLTP.toFixed(0)}</span></div>`;

    $('opt-body').innerHTML=`
      <div class="odir ${dirStyle}" style="${!fresh?'color:var(--'+(recent?'warn':'dim')+')':''}">
        ${fresh?'🔔 ':''}BUY ${csig.direction} ${csig.strike||'~ATM'}${freshTag}</div>
      ${ltpRow}
      <div class="row"><span class="lbl">Confidence</span><span class="${csig.confidence==='HIGH'?'vbull':'vwarn'}">${csig.confidence||'—'}</span></div>
      <div class="row"><span class="lbl">Entry type</span><span class="v">${csig.entry_type||'—'}</span></div>
      <div class="row"><span class="lbl">Spot Target</span><span class="vbull">${fmt(csig.spot_target,0)} (+${parseFloat(csig.target_pts||0).toFixed(0)}pts)</span></div>
      <div class="row"><span class="lbl">Spot SL</span><span class="vbear">${fmt(csig.spot_sl,0)} (−${parseFloat(csig.sl_pts||0).toFixed(0)}pts)</span></div>
      <div class="row"><span class="lbl">R:R</span><span class="${rrc}">${parseFloat(csig.rr_ratio||0).toFixed(1)}:1</span></div>
      <div style="margin-top:7px;font-size:11px;color:#9ca3af">${csig.reason||''}</div>`;
    $('opt-age').textContent=csig._age||'';
  } else if(cdec.option_text){
    oc.className='card';
    $('opt-body').innerHTML=`<div style="color:#9ca3af;font-size:12px">${cdec.option_text}</div>
      ${cdec.decision?`<div style="margin-top:6px;font-size:11px;color:var(--dim)">${cdec.decision}</div>`:''}`;
    $('opt-age').textContent=cdec._age||'';
  } else {
    oc.className='card';
    $('opt-body').innerHTML=`<div style="color:var(--dim)">Waiting for signal — start CHART_LEVEL_ANALYZER.py</div>`;
  }

  // Premium
  if(prem.ce_ltp||prem.raw){
    const cefc=flowCls(prem.ce_flow), pefc=flowCls(prem.pe_flow);
    $('prem-body').innerHTML= prem.ce_ltp ? `
      <div class="row"><span class="lbl">Spot</span><span class="v">${fmt(prem.spot,2)}</span></div>
      <div class="row"><span class="lbl">CE ${prem.ce_strike||''}</span><span class="${cefc}">${prem.ce_flow} ₹${prem.ce_ltp}</span></div>
      <div class="row"><span class="lbl">PE ${prem.pe_strike||''}</span><span class="${pefc}">${prem.pe_flow} ₹${prem.pe_ltp}</span></div>`
      : `<div style="font-size:11px;color:#9ca3af">${prem.raw||''}</div>`;
    $('prem-age').textContent=prem._age||'';
  }

  // Trade bot
  if(trade._live!==undefined||trade.ts){
    let th='';
    if(trade.active){
      th=`<div class="row"><span class="lbl">Status</span><span class="vwarn">🔴 IN TRADE</span></div>
          <div class="atrade">
            ${trade.symbol?`<div class="row"><span class="lbl">Symbol</span><span class="v">${trade.symbol}</span></div>`:''}
            ${trade.entry_price?`<div class="row"><span class="lbl">Entry</span><span class="v">₹${trade.entry_price}</span></div>`:''}
            ${trade.last_ltp?`<div class="row"><span class="lbl">LTP now</span><span class="v">₹${trade.last_ltp}</span></div>`:''}
            ${trade.trailing_sl?`<div class="row"><span class="lbl">Trail SL</span><span class="vbear">₹${trade.trailing_sl}</span></div>`:''}
          </div>`;
    } else {
      th=`<div class="row"><span class="lbl">Status</span><span class="vbull">✅ Idle — Ready</span></div>`;
      if(trade.symbol) th+=`<div class="row"><span class="lbl">Last trade</span><span class="vdim">${trade.symbol}</span></div>`;
    }
    $('trade-body').innerHTML=th;
    $('trade-age').textContent=trade._age||'';
  }

  // Signal Monitor
  if(sigmon.combined||sigmon.pdt||sigmon.fibo){
    const sc=sigmon.combined.toUpperCase();
    const scls=sc.includes('CE')?'vbull':sc.includes('PE')?'vbear':'vwarn';
    $('sigmon-body').innerHTML=`
      <div class="row"><span class="lbl">PDT signal</span><span class="${sigmon.pdt==='CE'?'vbull':sigmon.pdt==='PE'?'vbear':'vwarn'}">${sigmon.pdt||'—'}</span></div>
      <div class="row"><span class="lbl">FIBO signal</span><span class="${sigmon.fibo==='CE'?'vbull':sigmon.fibo==='PE'?'vbear':'vwarn'}">${sigmon.fibo||'—'}</span></div>
      <div class="row" style="margin-top:7px;border-top:1px solid var(--bdr);padding-top:7px">
        <span class="lbl">Combined</span><span class="${scls}" style="font-weight:bold">${sigmon.combined||'—'}</span></div>`;
    $('sigmon-age').textContent=sigmon._age||'';
  }

  // Market time remaining
  // Market timer is now driven by startTick() — no longer uses server mins_to_close

  // Feature toggle button states
  const feat = d.features || {};
  function syncToggleBtn(id, on){
    const b = $(id); if(!b) return;
    b.textContent = on ? 'ON' : 'OFF';
    b.className   = `toggle-btn ${on ? 'toggle-on' : 'toggle-off'}`;
  }
  syncToggleBtn('scalp-toggle',    feat.scalp    !== false);
  syncToggleBtn('ai-toggle',       feat.ai       !== false);
  syncToggleBtn('ptai_ai-toggle',  feat.ptai_ai  === true);

  // Scalp Plan
  const sp = d.scalp_plan || {};
  const stEl = $('scalp-text');
  if(feat.scalp === false){
    stEl.innerHTML = '<span class="scalp-dim">Scalp plan disabled — click ON to enable</span>';
    $('scalp-ts').textContent = '';
  } else if(sp.status === 'ok' && sp.text){
    const t = sp.text;
    const cls = t.toUpperCase().includes('BUY CE') ? 'scalp-ce' :
                t.toUpperCase().includes('BUY PE') ? 'scalp-pe' : 'scalp-wait';
    stEl.innerHTML = `<span class="${cls}">${t}</span>`;
    $('scalp-ts').textContent = sp.ts ? sp.ts.replace('T',' ') : '';
  } else if(sp.status === 'no_subscription'){
    stEl.innerHTML = '<span class="scalp-dim">⚠ Claude CLI not found — see AI Summary section for setup</span>';
  } else {
    stEl.innerHTML = '<span class="scalp-dim pulse">◌ Generating scalp plan…</span>';
  }

  // AI Summary
  const ai = d.ai_summary || {};
  const aiBody = $('ai-body');
  const aiBadge = $('ai-source-badge');
  $('ai-age').textContent = ai.ts ? ai.ts.replace('T',' ') : '';

  if(feat.ai === false){
    aiBadge.style.display='none';
    aiBody.innerHTML=`<div style="color:var(--dim);font-size:12px;padding:6px 0">AI Summary is <b>OFF</b> — click the toggle to enable.</div>`;
  } else if(ai.status === 'ok' && ai.text){
    aiBadge.textContent = ai.source || 'AI';
    aiBadge.style.display = 'inline';
    // Colour-code the structured sections
    let html = ai.text
      .replace(/(📍 SITUATION:)/g, '<span class="sit">$1</span>')
      .replace(/(🎯 LEVELS TO WATCH:)/g, '<span style="color:#a78bfa;font-weight:bold">$1</span>')
      .replace(/(⚡ ACTION NOW:.*)/g, m => {
        if(m.includes('BUY CE'))   return `<span class="act-ce">${m}</span>`;
        if(m.includes('BUY PE'))   return `<span class="act-pe">${m}</span>`;
        return `<span class="act-wait">${m}</span>`;
      })
      .replace(/(⚠️ KEY RISK:.*)/g, '<span class="risk">$1</span>');
    aiBody.innerHTML = `<div class="ai-text">${html}</div>`;

  } else if(ai.status === 'init' || ai.status === 'no_data'){
    aiBadge.style.display='none';
    aiBody.innerHTML=`<div style="color:var(--dim);font-size:12px"><span class="ai-spinner">◌</span> Generating first summary…</div>`;

  } else if(ai.status === 'no_subscription' || ai.error === 'no_cli'){
    aiBadge.style.display='none';
    aiBody.innerHTML=`<div class="ai-no-sub">
      <div class="title">🤖 AI Summary requires Claude Code CLI</div>
      <div class="msg">
        Claude Code CLI is not installed or not found in PATH.<br>
        Install it once — it uses your existing Claude.ai subscription at no extra cost.
      </div>
      <div class="plans">
        <div class="plan-card">
          <div class="plan-name">Step 1 — Install</div>
          <div class="plan-price" style="font-size:11px;font-family:monospace">npm install -g @anthropic-ai/claude-code</div>
          <div class="plan-note">Requires Node.js 18+</div>
        </div>
        <div class="plan-card">
          <div class="plan-name">Step 2 — Login</div>
          <div class="plan-price" style="font-size:11px;font-family:monospace">claude login</div>
          <div class="plan-note">Sign in with claude.ai account</div>
        </div>
        <div class="plan-card">
          <div class="plan-name">Subscription needed</div>
          <div class="plan-price">Claude Pro $20/mo</div>
          <div class="plan-note">claude.ai/upgrade<br>Enables CLI usage</div>
        </div>
      </div>
    </div>`;

  } else {
    // Other errors (auth, rate limit, SDK missing etc.)
    aiBadge.style.display='none';
    const errIcons = {auth_error:'🔑', rate_limit:'⏱', no_sdk:'📦', no_credits:'💳'};
    const icon = errIcons[ai.status] || '⚠️';
    aiBody.innerHTML=`<div class="ai-error">${icon} ${ai.error||'Unknown error'}</div>`;
  }

  $('footer').textContent=`Last updated: ${d.ts||'—'}  │  Read-only — no API calls — log aggregator  │  Auto-refresh every ${R}s`;

  // ── OI Chart: cache history + refresh if open ──
  window._oiHistData = d.oi_history || [];
  if(typeof _oiChartVisible!=='undefined' && _oiChartVisible){
    _oiChartData = window._oiHistData.slice();
    _oiChartDrawFull();
  }
}

/* ── Color Picker ── */
const DEFAULTS = {
  '--bg':'#070b14','--bg2':'#0c1220','--bg3':'#131c30','--hdr-bg':'#080f1e',
  '--bull':'#00e5a0','--bull2':'#001a10','--bear':'#ff4d6d','--bear2':'#1a0010',
  '--warn':'#ffc107','--info':'#38bdf8',
  '--txt':'#e2e8f0','--dim':'#5a7298',
  '--bdr':'#1c2d48','--accent':'#a855f7',
  '--buy-ce':'#00e5a0','--buy-ce-dark':'#00875e',
  '--buy-pe':'#ff4d6d','--buy-pe-dark':'#be1a3c'
};
// Map CSS var name → picker element id suffix
const VAR_ID = {
  '--bg':'bg','--bg2':'bg2','--bg3':'bg3','--hdr-bg':'hdrbg',
  '--bull':'bull','--bull2':'bull2','--bear':'bear','--bear2':'bear2',
  '--warn':'warn','--info':'info',
  '--txt':'txt','--dim':'dim',
  '--bdr':'bdr','--accent':'accent',
  '--buy-ce':'buyce','--buy-ce-dark':'buyced',
  '--buy-pe':'buype','--buy-pe-dark':'buyped'
};
const SAVED_KEY = 'nifty_dash_theme';

function setVar(v, val){
  document.documentElement.style.setProperty(v, val);
  const saved = JSON.parse(localStorage.getItem(SAVED_KEY)||'{}');
  saved[v] = val; localStorage.setItem(SAVED_KEY, JSON.stringify(saved));
}
function updHex(id, val){
  const el = document.getElementById('hx-'+id);
  if(el) el.textContent = val.toUpperCase();
}
function resetColors(){
  localStorage.removeItem(SAVED_KEY);
  Object.entries(DEFAULTS).forEach(([v,c])=>{
    document.documentElement.style.setProperty(v,c);
    const id = VAR_ID[v];
    const inp = document.getElementById('pk-'+id);
    if(inp){ inp.value = c; updHex(id, c); }
  });
}
function resetOne(cssVar, id){
  const def = DEFAULTS[cssVar];
  if(!def) return;
  document.documentElement.style.setProperty(cssVar, def);
  updHex(id, def);
  const inp = document.getElementById('pk-'+id);
  if(inp) inp.value = def;
  const saved = JSON.parse(localStorage.getItem(SAVED_KEY)||'{}');
  delete saved[cssVar]; localStorage.setItem(SAVED_KEY, JSON.stringify(saved));
}
function togglePicker(){
  document.getElementById('picker-panel').classList.toggle('open');
}
function loadSavedColors(){
  const saved = JSON.parse(localStorage.getItem(SAVED_KEY)||'{}');
  Object.entries(saved).forEach(([v,c])=>{
    document.documentElement.style.setProperty(v, c);
    const id = VAR_ID[v];
    if(id){
      const inp = document.getElementById('pk-'+id);
      if(inp){ inp.value = c; updHex(id, c); }
    }
    // Restore glow slider
    if(v === '--glow-a'){
      const pct = Math.round(parseFloat(c) * 100);
      const sl = document.getElementById('pk-glow');
      if(sl){ sl.value = pct; $('pk-glow-val').textContent = pct+'%'; }
    }
  });
}
/* ── Tab switching ── */
function switchTab(id, btn){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  btn.classList.add('active');
}

/* ── Trade Control tab (standalone panel on :8790) ── */
function initControlTab(){
  const f = document.getElementById('controlFrame');
  const url = 'http://' + (location.hostname || '127.0.0.1') + ':8790/';
  fetch(url, {mode:'no-cors'}).then(()=>{
    f.style.display = '';
    document.getElementById('controlHint').style.display = 'none';
    if(f.getAttribute('src') !== url) f.setAttribute('src', url);
  }).catch(()=>{
    f.style.display = 'none';
    document.getElementById('controlHint').style.display = 'block';
  });
}

/* ── Glow intensity ── */
function setGlow(pct){
  const a = parseFloat(pct) / 100;
  document.documentElement.style.setProperty('--glow-a', a);
  const saved = JSON.parse(localStorage.getItem(SAVED_KEY)||'{}');
  saved['--glow-a'] = a; localStorage.setItem(SAVED_KEY, JSON.stringify(saved));
}

// Close picker on outside click
document.addEventListener('click', e=>{
  const panel=$('picker-panel'), btn=$('picker-btn');
  const clickedToggle = btn&&(e.target===btn||btn.contains(e.target));
  if(panel && !panel.contains(e.target) && !clickedToggle)
    panel.classList.remove('open');
});
loadSavedColors();

async function toggle(f){
  try{
    const r = await fetch(`/api/toggle?f=${f}`);
    const feat = await r.json();
    // Sync button immediately without waiting for next poll
    syncToggleBtn(`${f}-toggle`, feat[f] !== false);
    if(feat[f] === false){
      if(f==='scalp') $('scalp-text').innerHTML='<span class="scalp-dim">Scalp plan disabled — click ON to enable</span>';
      if(f==='ai')    $('ai-body').innerHTML='<div style="color:var(--dim);font-size:12px">AI summary disabled — click ON to enable</div>';
    } else {
      if(f==='scalp') $('scalp-text').innerHTML='<span class="scalp-dim pulse">◌ Generating new plan…</span>';
      if(f==='ai')    $('ai-body').innerHTML='<div style="color:var(--dim);font-size:12px"><span class="ai-spinner">◌</span> Generating summary…</div>';
    }
  } catch(e){ console.error(e); }
}

// syncToggleBtn needs to be defined before toggle() is called
function syncToggleBtn(id, on){
  const b=$(id); if(!b) return;
  b.textContent=on?'ON':'OFF';
  b.className=`toggle-btn ${on?'toggle-on':'toggle-off'}`;
}

let _pivotTimer = null;
let _pivotSpot  = 0;

async function loadPivots(index){
  try{
    const idx = index || ($('tb-index')&&$('tb-index').value) || 'NIFTY';
    const r = await fetch(`/api/pivots?index=${idx}`);
    const d = await r.json();
    renderPivots(d);
    const ageEl=$('pivot-age'); if(ageEl) ageEl.textContent=d.ts||'';
    const srcEl=$('pivot-src'); if(srcEl) srcEl.textContent=d._source||'';
  }catch(e){}
}

function renderPivots(d){
  const el=$('pivot-body'); if(!el) return;
  const spot = _pivotSpot || 0;
  const keys=['R4','R3','R2','R1','PP','S1','S2','S3','S4'];
  const typeMap={R4:'res',R3:'res',R2:'res',R1:'res',PP:'pp',S1:'sup',S2:'sup',S3:'sup',S4:'sup'};
  const clrMap={res:'var(--bear)',pp:'var(--warn)',sup:'var(--bull)'};

  // Build ordered list (high → low)
  const levels = keys.map(k=>({label:k, price:d[k]||0, type:typeMap[k]}))
                     .filter(l=>l.price>0)
                     .sort((a,b)=>b.price-a.price);

  if(!levels.length){
    el.innerHTML='<div style="color:var(--dim);font-size:11px;padding:12px;text-align:center">'+
      (d.error?'⚠ '+d.error:'No pivot data — run CHART_LEVEL_ANALYZER.py or install yfinance')+'</div>';
    return;
  }

  // Render as a compact horizontal grid: one row per level, spot marker inserted between
  let html='<div class="pv-stack">';
  let priceInserted = !spot; // if no spot, skip marker
  for(const lvl of levels){
    if(!priceInserted && spot>=lvl.price){
      const fmt=spot.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
      html+=`<div class="pv-price-bar"><div class="pv-price-line"></div><div class="pv-price-chip">PRICE ${fmt}</div><div class="pv-price-line"></div></div>`;
      priceInserted=true;
    }
    const dist=lvl.price-spot;
    const distPct=(Math.abs(dist)/spot*100).toFixed(2);
    const sign=dist>=0?'+':'';
    const clr=clrMap[lvl.type];
    html+=`<div class="pv-row pv-row-${lvl.type}">
      <span class="pv-lbl" style="color:${clr}">${lvl.label}</span>
      <span class="pv-val">${lvl.price.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
      <span class="pv-dist">${spot?sign+Math.round(dist)+' pts ('+sign+distPct+'%)':''}</span>
    </div>`;
  }
  if(!priceInserted && spot){
    const fmt=spot.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
    html+=`<div class="pv-price-bar"><div class="pv-price-line"></div><div class="pv-price-chip">PRICE ${fmt}</div><div class="pv-price-line"></div></div>`;
  }
  html+='</div>';

  // Show prev-day OHLC used for calculation
  if(d._prev_h){
    html+=`<div style="margin-top:8px;font-size:10px;color:var(--dim);font-family:'JetBrains Mono',monospace">
      Prev day: H ${d._prev_h.toLocaleString('en-IN')} &nbsp;L ${d._prev_l.toLocaleString('en-IN')} &nbsp;C ${d._prev_c.toLocaleString('en-IN')}
    </div>`;
  }
  el.innerHTML=html;
}

/* ─── Decision Engine tab (trading_decision_engine) ─────────── */
function deBar(pct,color){
  pct=Math.max(0,Math.min(100,pct||0));
  return `<div style="background:var(--bdr);height:9px;border-radius:5px;flex:1;min-width:70px">
    <div style="width:${pct}%;background:${color};height:9px;border-radius:5px"></div></div>`;
}
function dePassBadge(ok){
  return ok? '<span style="color:var(--bull);font-weight:700;font-size:11px">PASS</span>'
           : '<span style="color:var(--bear);font-weight:700;font-size:11px">FAIL</span>';
}
const DE_ENGINE_ORDER=[['trend','Trend'],['market_structure','Market Structure'],['support_resistance','Support/Resist'],
  ['premium_momentum','Premium Momentum'],['breakout','Breakout'],['market_strength','Market Strength'],
  ['option_selection','Option Selection'],['volatility','Volatility'],['trading_rules','Trading Rules'],
  ['risk','Risk'],['signal_stability','Signal Stability']];

/* launch control */
let _deExpiriesLoaded=false, _deConsoleTimer=null;
function deModeChanged(){
  const live=document.getElementById('dec-mode').value==='live';
  document.getElementById('dec-live-confirm-wrap').style.display=live?'inline-block':'none';
}
async function deLoadExpiries(){
  const idx=document.getElementById('dec-index').value;
  const sel=document.getElementById('dec-expiry');
  sel.innerHTML='<option value="">loading…</option>';
  try{
    const r=await fetch('/api/engine/expiries?index='+idx); const d=await r.json();
    sel.innerHTML=(d.expiries||[]).map((e,i)=>`<option value="${e}"${i===0?' selected':''}>${e}${i===0?' (current)':i===1?' (next)':''}</option>`).join('')
      ||'<option value="">none found — refresh instrument.csv</option>';
  }catch(e){sel.innerHTML='<option value="">error loading</option>';}
}
async function deStart(){
  const msg=document.getElementById('dec-launch-msg');
  const mode=document.getElementById('dec-mode').value;
  const body={
    mode, profile:document.getElementById('dec-profile').value,
    index:document.getElementById('dec-index').value,
    expiry:document.getElementById('dec-expiry').value,
    lots:parseInt(document.getElementById('dec-lots').value||'1'),
    premium_min:parseFloat(document.getElementById('dec-pmin').value||'60'),
    premium_max:parseFloat(document.getElementById('dec-pmax').value||'250'),
    validate_orders:document.getElementById('dec-validate').checked,
    confirm_live:document.getElementById('dec-live-confirm').value,
  };
  if(mode==='live' && body.confirm_live.trim().toUpperCase()!=='YES'){
    msg.textContent='⚠ LIVE mode: type YES in the confirmation box first'; msg.style.color='var(--bear)'; return;
  }
  msg.textContent='starting…'; msg.style.color='var(--dim)';
  try{
    const r=await fetch('/api/engine/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){ msg.textContent='✓ started (pid '+d.pid+')'; msg.style.color='var(--bull)'; deShowConsole(); }
    else    { msg.textContent='✗ '+(d.error||'failed'); msg.style.color='var(--bear)'; }
  }catch(e){ msg.textContent='✗ '+e; msg.style.color='var(--bear)'; }
}
async function deStop(){
  const msg=document.getElementById('dec-launch-msg');
  if(!confirm('Stop the decision engine? Any open trade keeps running on the broker — the engine exits after printing session stats.')) return;
  msg.textContent='stopping…'; msg.style.color='var(--dim)';
  try{
    const r=await fetch('/api/engine/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    msg.textContent=d.ok?'✓ stopped':'✗ '+(d.error||'failed');
    msg.style.color=d.ok?'var(--bull)':'var(--bear)';
  }catch(e){ msg.textContent='✗ '+e; msg.style.color='var(--bear)'; }
}
function deShowConsole(){
  const el=document.getElementById('dec-console');
  el.style.display='block';
  if(_deConsoleTimer) clearInterval(_deConsoleTimer);
  const poll=async()=>{
    try{
      const r=await fetch('/api/engine/console?n=40'); const d=await r.json();
      if((d.lines||[]).length){ el.textContent=d.lines.join('\\n'); el.scrollTop=el.scrollHeight; }
      if(!d.running && _deConsoleTimer && el.textContent.length){ /* keep last output visible */ }
    }catch(e){}
  };
  poll(); _deConsoleTimer=setInterval(poll,3000);
}

function renderDecisionEngine(de){
  const pane=document.getElementById('tab-engine'); if(!pane||!de) return;
  /* one-time init of launch-control dropdowns */
  if(!_deExpiriesLoaded){ _deExpiriesLoaded=true; deLoadExpiries(); }
  const profSel=document.getElementById('dec-profile');
  if(profSel && profSel.options.length<=1 && (de.profiles||[]).length){
    (de.profiles||[]).forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;profSel.appendChild(o);});
  }
  const runEl=document.getElementById('de-run');
  runEl.className='badge '+(de.running?'live':'off');
  runEl.textContent=de.running?('RUNNING pid '+de.pid):'STOPPED';
  document.getElementById('dec-start-btn').style.opacity=de.running?0.35:1;
  document.getElementById('dec-stop-btn').style.opacity=de.running?1:0.35;

  const modeEl=document.getElementById('de-mode');
  const live=de.available && de.age_seconds!=null && de.age_seconds<60;
  modeEl.className='badge '+(live?'live':(de.available?'stale':'off'));
  modeEl.textContent=de.available?((de.mode||'?').toUpperCase()+(live?'':' · stale '+Math.round(de.age_seconds)+'s')):'OFFLINE';
  document.getElementById('de-profile').textContent=(de.config&&de.config.active_profile)?('profile: '+de.config.active_profile):'';
  document.getElementById('de-file').textContent=de.file||'';
  document.getElementById('de-offline-msg').style.display=de.available?'none':'block';
  document.getElementById('de-body').style.display=de.available?'block':'none';
  if(!de.available){ renderDeConfig(de); return; }

  const L=de.latest||{};
  const conf=L.confidence||{buy:0,sell:0,hold:100};
  const act=L.action||'—';
  const actColor=act==='BUY'?'var(--bull)':act==='SELL'?'var(--bear)':act==='REJECT'?'var(--bear)':'var(--warn)';

  /* decision card */
  let h=`<div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
    <span style="font-size:11px;letter-spacing:1px;color:var(--dim)">LIVE DECISION</span>
    <span style="font-size:20px;font-weight:800;color:${actColor}">${act}</span>
    <span style="color:var(--dim);font-size:11px;margin-left:auto">${(L.timestamp||'').split('T')[1]||''}</span></div>`;
  [['BUY',conf.buy,'var(--bull)'],['SELL',conf.sell,'var(--bear)'],['HOLD',conf.hold,'var(--dim)']].forEach(([lbl,v,c])=>{
    h+=`<div style="display:flex;align-items:center;gap:10px;margin:5px 0">
      <span style="width:40px;font-size:11px;color:var(--dim)">${lbl}</span>${deBar(v,c)}
      <span style="width:48px;text-align:right;font-size:12px;font-weight:600">${(v||0).toFixed(1)}%</span></div>`;
  });
  const s2=L.stage2||{};
  if(s2.evaluated){
    h+=`<div style="margin-top:10px;font-size:12px;color:var(--txt)">buy <b>${s2.buy_score}</b>/${s2.required_buy}
      &nbsp;sell <b>${s2.sell_score}</b>/${s2.required_sell}
      &nbsp;quality <b>${s2.trade_quality}</b>&nbsp;agreement <b>${s2.engine_agreement||''}</b></div>`;
  }
  const reasons=(L.final&&L.final.reasons)||[];
  if(reasons.length){
    h+='<div style="margin-top:8px;font-size:11px;color:var(--dim)">'+reasons.slice(0,5).map(r=>'• '+r).join('<br>')+'</div>';
  }
  document.getElementById('de-decision-card').innerHTML=h;

  /* gates card */
  const s1=L.stage1||{checks:{}};
  h=`<div style="font-size:11px;letter-spacing:1px;color:var(--dim);margin-bottom:10px">STAGE 1 — MANDATORY GATES
     <span style="float:right;font-weight:700;color:${s1.passed?'var(--bull)':'var(--bear)'}">${s1.passed?'PASS':'FAIL'}</span></div>`;
  Object.entries(s1.checks||{}).forEach(([gate,c])=>{
    if(!c.enabled){h+=`<div style="display:flex;gap:8px;margin:4px 0;font-size:12px;opacity:.4">
      <span style="width:150px">${gate.replace(/_/g,' ')}</span><span style="color:var(--dim)">gate off</span></div>`;return;}
    h+=`<div style="display:flex;gap:8px;margin:4px 0;font-size:12px">
      <span style="width:150px">${gate.replace(/_/g,' ')}</span>${dePassBadge(c.passed)}
      ${c.passed?'':`<span style="color:var(--dim);font-size:11px">${c.actual} <span style="color:var(--bear)">vs</span> ${c.required}</span>`}</div>`;
  });
  document.getElementById('de-gates-card').innerHTML=h;

  /* engines card */
  h=`<div style="font-size:11px;letter-spacing:1px;color:var(--dim);margin-bottom:10px">ENGINES — CURRENT CYCLE</div>`;
  const eng=L.engines||{};
  DE_ENGINE_ORDER.forEach(([key,label])=>{
    const e=eng[key]; if(!e) return;
    const dirC=e.direction==='BULLISH'?'var(--bull)':e.direction==='BEARISH'?'var(--bear)':'var(--dim)';
    h+=`<div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-size:12px">
      <span style="width:132px">${label}</span>${deBar(e.score,dirC)}
      <span style="width:44px;text-align:right;font-weight:600">${(e.score||0).toFixed(0)}</span>
      <span style="width:56px;color:var(--dim);font-size:11px">w ${((e.weight||0)*100).toFixed(1)}%</span>
      <span style="width:52px;color:${e.contribution?'var(--bull)':'var(--dim)'};font-size:11px">${e.contribution?('+'+e.contribution.toFixed(1)):'—'}</span>
      ${dePassBadge(e.passed)}</div>`;
  });
  const mom=eng.premium_momentum||{}, sr=eng.support_resistance||{}, st=eng.signal_stability||{};
  h+=`<div style="margin-top:8px;font-size:11px;color:var(--dim)">
    velocity ${mom.velocity!=null?mom.velocity.toFixed(2):'—'}/s · accel ${mom.acceleration!=null?mom.acceleration.toFixed(2):'—'}
    · consistency ${mom.consistency_pct!=null?mom.consistency_pct.toFixed(0):'—'}%
    &nbsp;|&nbsp; room: res ${sr.distance_to_resistance!=null?sr.distance_to_resistance:'—'} / sup ${sr.distance_to_support!=null?sr.distance_to_support:'—'}
    &nbsp;|&nbsp; stability ${st.elapsed_seconds!=null?st.elapsed_seconds.toFixed(1):'—'}s of ${st.required_seconds!=null?st.required_seconds.toFixed(1):'—'}s</div>`;
  document.getElementById('de-engines-card').innerHTML=h;

  /* session stats card */
  const S=de.stats||{cycles:0,actions:{},rejections:[],engines:{}};
  h=`<div style="font-size:11px;letter-spacing:1px;color:var(--dim);margin-bottom:10px">SESSION STATISTICS${de.partial?' <span style="color:var(--warn)">(partial)</span>':''}</div>
  <div style="font-size:12px;margin-bottom:8px">cycles <b>${(S.cycles||0).toLocaleString()}</b>
    &nbsp;BUY <b style="color:var(--bull)">${S.actions.BUY||0}</b>
    &nbsp;SELL <b style="color:var(--bear)">${S.actions.SELL||0}</b>
    &nbsp;HOLD <b>${S.actions.HOLD||0}</b>
    &nbsp;REJECT <b>${(S.actions.REJECT||0).toLocaleString()}</b></div>`;
  if((S.rejections||[]).length){
    h+='<div style="font-size:11px;color:var(--dim);margin:6px 0 4px">TOP REJECTION REASONS</div>';
    S.rejections.slice(0,6).forEach(r=>{
      h+=`<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:11px">
        <span style="width:130px">${r.gate.replace(/_/g,' ')}</span>${deBar(r.pct,'var(--bear)')}
        <span style="width:70px;text-align:right">${r.pct}% (${r.count.toLocaleString()})</span></div>`;
    });
  }
  const engStats=Object.entries(S.engines||{});
  if(engStats.length){
    h+=`<table class="perf-table" style="margin-top:8px;font-size:11px"><tr><th style="text-align:left">engine</th><th>pass%</th><th>avg score</th></tr>`;
    engStats.forEach(([n,e])=>{h+=`<tr><td>${n.replace(/_/g,' ')}</td>
      <td style="text-align:center;color:${e.pass_pct>=50?'var(--bull)':'var(--bear)'}">${e.pass_pct}%</td>
      <td style="text-align:center">${e.avg_score}</td></tr>`;});
    h+='</table>';
  }
  document.getElementById('de-stats-card').innerHTML=h;

  /* trades card */
  h=`<div style="font-size:11px;letter-spacing:1px;color:var(--dim);margin-bottom:10px">RECENT TRADES</div>`;
  const trades=de.trades||[];
  if(!trades.length){h+='<div style="color:var(--dim);font-size:12px">No trades yet this session.</div>';}
  else{
    h+='<table class="perf-table" style="font-size:11px"><tr><th style="text-align:left">time</th><th style="text-align:left">event</th><th style="text-align:left">instrument</th><th>price</th><th>P&L / reason</th></tr>';
    trades.forEach(t=>{
      const time=(t.timestamp||'').split('T')[1]||'';
      if(t.event==='trade_opened'){
        h+=`<tr><td>${time}</td><td style="color:var(--bull)">OPEN</td><td>${t.instrument||''}</td>
          <td style="text-align:center">${(t.entry_price!=null?t.entry_price.toFixed(2):'')}</td><td style="text-align:center;color:var(--dim)">x${t.lots||''} lot</td></tr>`;
      }else{
        const pc=(t.pnl||0)>=0?'var(--bull)':'var(--bear)';
        h+=`<tr><td>${time}</td><td style="color:var(--bear)">CLOSE</td><td>${t.instrument||''}</td>
          <td style="text-align:center">${(t.exit_price!=null?t.exit_price.toFixed(2):'')}</td>
          <td style="text-align:center"><b style="color:${pc}">₹${(t.pnl||0).toFixed(0)}</b> <span style="color:var(--dim);font-size:10px">${(t.exit_reason||'').split(':')[0]}</span></td></tr>`;
      }
    });
    h+='</table>';
  }
  document.getElementById('de-trades-card').innerHTML=h;
  renderDeConfig(de);
}
function renderDeConfig(de){
  const el=document.getElementById('de-config-card'); if(!el) return;
  const c=de.config||{};
  let h=`<div style="font-size:11px;letter-spacing:1px;color:var(--dim);margin-bottom:10px">STRATEGY CONFIG
    <span style="float:right;color:var(--dim);font-weight:400">profiles: ${(de.profiles||[]).join(' · ')||'—'} — edits hot-reload in ~5s</span></div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:6px;font-size:12px">`;
  Object.entries(c).forEach(([k,v])=>{
    if(k==='active_profile') return;
    h+=`<div style="display:flex;justify-content:space-between;background:#0a111e;border:1px solid var(--bdr);border-radius:6px;padding:5px 9px">
      <span style="color:var(--dim)">${k.replace(/_/g,' ')}</span><b>${v!=null?v:'—'}</b></div>`;
  });
  h+='</div>';
  el.innerHTML=h;
}

async function load(){
  try{const r=await fetch('/api/data'); const d=await r.json(); render(d);
    // Update pivot spot from live data and re-render
    if(d.spot){ _pivotSpot=d.spot; renderPivots(_pivotCache||{}); }
    try{renderDecisionEngine(d.decision_engine);}catch(e){console.error('decision engine tab:',e);}
  }catch(e){console.error(e);}
}
let _pivotCache={};

function _isWeekday(d){ return d.getDay()>=1&&d.getDay()<=5; }
function _nextMarketOpen(now){
  // Returns Date of next 9:15 AM on a weekday
  let t=new Date(now);
  t.setHours(9,15,0,0);
  if(t<=now){ t.setDate(t.getDate()+1); }  // today's open has passed
  while(!_isWeekday(t)){ t.setDate(t.getDate()+1); }  // skip weekends
  return t;
}
function updateMarketBadge(){
  const now=new Date();
  const h=now.getHours(), m=now.getMinutes(), s=now.getSeconds();
  const isWD=_isWeekday(now);
  const afterOpen  = h>9||(h===9&&m>=15);
  const beforeClose= h<15||(h===15&&m<30);
  const isOpen     = isWD&&afterOpen&&beforeClose;
  const el=$('mtc-badge'); if(!el) return;
  if(isOpen){
    // Countdown to 3:30 PM in HH:MM:SS
    const close=new Date(now); close.setHours(15,30,0,0);
    const sec=Math.max(0,Math.floor((close-now)/1000));
    const hh=String(Math.floor(sec/3600)).padStart(2,'0');
    const mm=String(Math.floor(sec%3600/60)).padStart(2,'0');
    const ss=String(sec%60).padStart(2,'0');
    el.textContent=`${hh}:${mm}:${ss} left`;
    el.className=`mtc ${sec>3600?'mtc-ok':sec>1200?'mtc-warn':'mtc-close'}`;
  } else {
    // Time until next market open in Xh Ym format
    const next=_nextMarketOpen(now);
    const sec=Math.max(0,Math.floor((next-now)/1000));
    const hh=Math.floor(sec/3600);
    const mm=Math.floor(sec%3600/60);
    el.textContent=`Opens ${hh}h ${mm}m`;
    el.className='mtc mtc-ok';
  }
}
function startTick(){
  clearInterval(tim); cd=R; $('countdown').textContent=cd;
  tim=setInterval(()=>{
    // Live clock in header
    const now=new Date();
    const ht=$('htime');
    if(ht) ht.textContent=now.getFullYear()+'-'
      +String(now.getMonth()+1).padStart(2,'0')+'-'
      +String(now.getDate()).padStart(2,'0')+' '
      +String(now.getHours()).padStart(2,'0')+':'
      +String(now.getMinutes()).padStart(2,'0')+':'
      +String(now.getSeconds()).padStart(2,'0');
    // Market badge
    updateMarketBadge();
    // Data refresh countdown
    cd--; $('countdown').textContent=Math.max(0,cd);
    if(cd<=0){load(); cd=R;}
  },1000);
  updateMarketBadge();  // run immediately on start
}

load(); startTick();

/* ── PnL Tab ── */
const PNL_TARGET_KEY  = 'nifty_pnl_target';
const PNL_ALARM_KEY   = 'nifty_pnl_alarm';
let   _pnlAlarmFired  = false;  // reset when target changes or day changes
let   _pnlAlarmOn     = localStorage.getItem(PNL_ALARM_KEY) !== 'off';
let   _pnlAlarmDate   = '';     // track which day the alarm fired

function savePnlTarget(v){
  const n = parseFloat(v);
  if(!isNaN(n) && n > 0){
    localStorage.setItem(PNL_TARGET_KEY, String(n));
    _pnlAlarmFired = false;  // reset when target changes
  }
}
function getPnlTarget(){
  return parseFloat(localStorage.getItem(PNL_TARGET_KEY) || '10000');
}
function toggleAlarm(){
  _pnlAlarmOn = !_pnlAlarmOn;
  localStorage.setItem(PNL_ALARM_KEY, _pnlAlarmOn ? 'on' : 'off');
  const btn = $('pnl-alarm-btn');
  const lbl = $('pnl-alarm-status');
  if(btn){ btn.className = 'pnl-alarm-btn' + (_pnlAlarmOn ? ' active' : ''); }
  if(lbl){ lbl.textContent = _pnlAlarmOn ? 'Alarm ON' : 'Alarm OFF'; }
}
function playPnlAlarm(){
  try{
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const notes = [880,1100,880,1100,880,1320];
    let t = ctx.currentTime;
    notes.forEach(freq=>{
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.type = 'sine'; o.frequency.value = freq;
      g.gain.setValueAtTime(0.3, t);
      g.gain.exponentialRampToValueAtTime(0.001, t+0.28);
      o.start(t); o.stop(t+0.28);
      t += 0.33;
    });
  }catch(e){}
}
function fmt(n,pre=0){ return new Intl.NumberFormat('en-IN',{minimumFractionDigits:pre,maximumFractionDigits:pre}).format(n); }
function pnlCls(n){ return n>0?'pnl-pos':n<0?'pnl-neg':'pnl-zero'; }
function scoreCls(s){ return s>=61?'score-green':s>=41?'score-yellow':'score-red'; }

function renderPnl(d){
  /* ── Today PnL ── */
  const pt = d.pnl_today || {};
  const pa = d.pnl_analysis || {};
  const ai = d.pnl_ai || {};
  const pnl = pt.total_pnl || 0;
  const target = getPnlTarget();

  // Update target input to saved value
  const tinp = $('pnl-target-inp');
  if(tinp && tinp !== document.activeElement) tinp.value = target;

  // Alarm button state
  const abtn = $('pnl-alarm-btn');
  const albl = $('pnl-alarm-status');
  if(abtn) abtn.className = 'pnl-alarm-btn' + (_pnlAlarmOn ? ' active' : '');
  if(albl) albl.textContent = _pnlAlarmOn ? 'Alarm ON' : 'Alarm OFF';

  // Big PnL number
  const bigEl = $('pnl-big');
  if(bigEl){
    bigEl.textContent = (pnl>0?'+':pnl<0?'-':'') + '₹' + fmt(Math.abs(pnl),0);
    bigEl.className = 'pnl-big ' + pnlCls(pnl);
  }
  setText('pnl-count', (pt.count||0) + ' closed');
  setText('pnl-wins',  (pt.wins||0) + 'W');
  setText('pnl-losses',(pt.losses||0) + 'L');
  setText('pnl-open',  (pt.open||0) + ' open');
  // Unrealised (now computed via LTP)
  const unr = pt.unrealised||0;
  const unrEl=$('pnl-unrealised');
  if(unrEl){ unrEl.textContent=(unr>0?'+':unr<0?'-':'')+'₹'+fmt(Math.abs(unr),0); unrEl.style.color=unr>0?'var(--bull)':unr<0?'var(--bear)':'var(--dim)'; }
  const totOpen = pt.total_with_open||0;
  const toEl=$('pnl-total-open');
  if(toEl){ toEl.textContent=(totOpen>0?'+':totOpen<0?'-':'')+'₹'+fmt(Math.abs(totOpen),0); toEl.style.color=totOpen>0?'var(--bull)':totOpen<0?'var(--bear)':'var(--dim)'; }
  // Bar: for profit show progress to target; for loss show how deep the loss is (capped at 100%)
  const pct = target > 0 ? Math.min(Math.abs(pnl)/target*100, 100) : 0;
  const barEl = $('pnl-bar');
  if(barEl){
    barEl.style.width = pct + '%';
    barEl.className = 'pnl-bar-fill ' + (pnl >= 0 ? 'pnl-bar-pos' : 'pnl-bar-neg');
  }
  setText('pnl-pct', pct.toFixed(0) + (pnl >= 0 ? '%' : '% (loss)'));
  if(pt.ts){ setText('pnl-ts', _age(pt.ts)); }

  // Target hit alarm
  const card = $('pnl-card');
  const today = new Date().toDateString();
  if(pnl >= target && target > 0){
    if(card) card.classList.add('target-hit');
    if(_pnlAlarmOn && !_pnlAlarmFired){
      _pnlAlarmFired = true; _pnlAlarmDate = today;
      playPnlAlarm();
    }
  } else {
    if(card) card.classList.remove('target-hit');
    if(_pnlAlarmDate !== today) _pnlAlarmFired = false;
  }

  if(!pa || !pa.ts){
    // No analysis yet
    return;
  }

  /* ── Trading Permission ── */
  const ps = pa.perm_score || 0;
  const verd = pa.verdict || '';
  const verdMap = {
    'NO_TRADE':       ['v-no-trade','NO TRADE'],
    'CAUTION':        ['v-caution', 'CAUTION'],
    'NORMAL':         ['v-normal',  'NORMAL'],
    'HIGH_CONFIDENCE':['v-high',    'HIGH CONFIDENCE'],
  };
  const [vcls, vlbl] = verdMap[verd] || ['v-caution','—'];
  const vEl = $('perm-verdict');
  if(vEl){ vEl.textContent = vlbl; vEl.className = 'verdict-chip ' + vcls; }
  setText('perm-score', ps + '/100');
  setBar('perm-bar', ps, scoreCls(ps));
  setText('perm-ts', pa.ts ? _age(pa.ts) : '');

  const ms = pa.mkt_score || 0;
  setText('mkt-score', ms + '/100');
  setBar('mkt-bar', ms, scoreCls(ms));

  const bh = pa.behav || {};
  setText('perm-recwr', (bh.recent_wr||0).toFixed(1) + '%');
  // Direction bias
  const lv = pa.live || {};
  const gap = lv.gap_pct||0, pcr = lv.pcr||0, simwr = (pa.sim||{}).win_rate||50;
  let bull=0,bear=0;
  if(gap>0.4) bull++; else if(gap<-0.4) bear++;
  if(pcr>1.2) bull++; else if(pcr<0.8) bear++;
  if(simwr>60) bull++; else if(simwr<40) bear++;
  const biasEl = $('perm-bias');
  if(biasEl){
    if(bull>bear){ biasEl.textContent='BULLISH'; biasEl.className='v vbull'; }
    else if(bear>bull){ biasEl.textContent='BEARISH'; biasEl.className='v vbear'; }
    else { biasEl.textContent='NEUTRAL'; biasEl.className='v vwarn'; }
  }
  const sizeMap = {'NO_TRADE':'0% — Do not trade','CAUTION':'25–40% of normal size',
                   'NORMAL':'50–75% of normal size','HIGH_CONFIDENCE':'75–100% of normal size'};
  setText('perm-size', sizeMap[verd]||'—');

  // Perm score breakdown
  const pbEl = $('perm-bkdwn');
  if(pbEl && pa.perm_bkdwn){
    pbEl.innerHTML = Object.entries(pa.perm_bkdwn).map(([k,v])=>{
      const n=parseFloat(v); const c=n>=0?'color:var(--bull)':'color:var(--bear)';
      return `<div class="row"><span class="lbl" style="font-size:10px">${k}</span><span style="${c};font-family:'JetBrains Mono',monospace;font-size:11px">${n>=0?'+':''}${n}</span></div>`;
    }).join('');
  }

  /* ── Live Market ── */
  const nifty = lv.nifty; const vix = lv.vix; const vixchg = lv.vix_chg_pct;
  const niftyPrev = lv.nifty_prev;
  setText('mkt-ts', pa.ts ? _age(pa.ts) : '');
  const niftyEl = $('mkt-nifty');
  if(niftyEl){
    niftyEl.textContent = nifty ? fmt(nifty,2) : '—';
    niftyEl.className = nifty&&niftyPrev ? (nifty>=niftyPrev?'v vbull':'v vbear') : 'v';
  }
  const vixEl = $('mkt-vix');
  if(vixEl){
    const vt = vix ? vix.toFixed(2) + (vixchg!=null?` (${vixchg>=0?'+':''}${vixchg.toFixed(1)}%)`:'') : '—';
    vixEl.textContent = vt;
    vixEl.className = vix ? (vix<15?'v vbull':vix<19?'v vwarn':'v vbear') : 'v';
  }
  const gapEl = $('mkt-gap');
  if(gapEl){
    const g = lv.gap_pct;
    gapEl.textContent = g!=null ? (g>=0?'+':'')+g.toFixed(2)+'%' : '—';
    gapEl.className = g!=null ? (g>=0?'v vbull':'v vbear') : 'v';
  }
  const pcrEl = $('mkt-pcr');
  if(pcrEl){
    const p = lv.pcr;
    if(p){
      pcrEl.textContent = p.toFixed(3) + (p>=0.9&&p<=1.3?' (balanced)':p>1.5?' (put-heavy)':p<0.7?' (call-heavy)':'');
      pcrEl.className = p>=0.9&&p<=1.3?'v vbull':p>1.5||p<0.7?'v vbear':'v vwarn';
    } else {
      pcrEl.textContent = lv.market_open===false ? 'N/A (market closed)' : '—';
      pcrEl.className = 'v vdim';
    }
  }
  const openEl = $('mkt-open');
  if(openEl){
    openEl.textContent = lv.market_open ? 'OPEN' : 'CLOSED';
    openEl.className = lv.market_open ? 'v vbull' : 'v vbear';
  }
  setBar('mkt-bar2', ms, scoreCls(ms));
  const ms2El = $('mkt-score2');
  if(ms2El){
    ms2El.textContent = ms + '/100';
    ms2El.style.color = ms>=61?'var(--bull)':ms>=41?'var(--warn)':'var(--bear)';
  }

  // Market breakdown
  const mbEl = $('mkt-bkdwn');
  if(mbEl && pa.mkt_bkdwn){
    mbEl.innerHTML = Object.entries(pa.mkt_bkdwn).map(([k,v])=>{
      const bar = '▪'.repeat(v.pts||0) + '·'.repeat(Math.max(0,(v.max||0)-(v.pts||0)));
      const pct2 = v.max>0 ? (v.pts/v.max*100) : 0;
      const c = pct2>=75?'color:var(--bull)':pct2>=50?'color:var(--warn)':'color:var(--bear)';
      const isNA = !v.val || v.val==='N/A';
      const valDisplay = isNA ? '<span style="color:var(--warn);font-size:9px">N/A (market closed)</span>'
                               : `<span style="color:var(--dim);font-size:9px">${v.val}</span>`;
      return `<div class="has-tip" style="margin-bottom:3px;padding:2px 0;">
        <span style="color:var(--dim)">${k}</span>
        <span style="${c}"> [${bar}] ${v.pts}/${v.max}</span>
        &nbsp;${valDisplay}
        <div class="tip-box">
          <div class="tip-title" style="color:var(--info)">${k} — ${v.pts}/${v.max} pts</div>
          ${isNA?'<div style="color:var(--warn);font-size:10px;margin-bottom:4px">⚠ Live data unavailable (market closed) — using neutral default</div>':''}
          <div style="color:var(--txt);font-size:11px;line-height:1.6">${v.meaning||''}</div>
        </div>
      </div>`;
    }).join('');
  }

  /* ── 3-Year Stats ── */
  const st = pa.stats || {};
  if(st.total_days){
    setText('st-days', st.total_days + ' days  (' + st.win_days + 'W / ' + st.loss_days + 'L)');
    const wrEl=$('st-wr'); if(wrEl){ wrEl.textContent=st.win_rate.toFixed(1)+'%'; wrEl.className='v '+(st.win_rate>=55?'vbull':st.win_rate>=45?'vwarn':'vbear'); }
    setText('st-avgwin',  '₹+'+fmt(st.avg_win, 0));
    setText('st-avgloss', '₹'+fmt(st.avg_loss, 0));
    const tpEl=$('st-total'); if(tpEl){ tpEl.textContent='₹'+(st.total_pnl>=0?'+':'')+fmt(st.total_pnl,0); tpEl.className='v '+(st.total_pnl>=0?'vbull':'vbear'); }
    setText('st-best',  st.best_date ? '₹+'+fmt(st.best_pnl,0)+' ('+st.best_date+')' : '—');
    setText('st-worst', st.worst_date ? '₹'+fmt(st.worst_pnl,0)+' ('+st.worst_date+')' : '—');
    const yrEl=$('st-yearly');
    if(yrEl && st.yearly){
      yrEl.innerHTML = Object.entries(st.yearly).sort().map(([yr,p])=>{
        const bar='█'.repeat(Math.min(Math.round(Math.abs(p)/50000),20));
        const c=p>=0?'color:var(--bull)':'color:var(--bear)';
        return `<div><span style="color:var(--dim)">${yr}</span>  <span style="${c}">${bar}</span>  <span style="${c}">${p>=0?'+':''}₹${fmt(p,0)}</span></div>`;
      }).join('');
    }
  }

  /* ── Behavioral Risks ── */
  const risksEl = $('behav-risks');
  if(risksEl){
    const risks = bh.risks || [];
    risksEl.innerHTML = risks.length ? risks.map(r=>{
      const dots = '●'.repeat(r.weight||1)+'○'.repeat(4-(r.weight||1));
      return `<div class="risk-item"><div class="risk-type">${r.type} <span style="color:var(--bear)">${dots}</span></div><div>${r.detail||''}</div></div>`;
    }).join('') : '<div style="color:var(--bull);font-size:11px">✓ No major behavioral risks detected</div>';
  }
  const insEl = $('behav-insights');
  if(insEl){
    const ins = bh.insights || [];
    insEl.innerHTML = ins.length ? ins.map(i=>`<div class="insight-item">• ${i}</div>`).join('') : '';
  }

  /* ── Similar Days ── */
  const sim = pa.sim || {};
  setText('sim-count', (sim.count||0) + ' similar days');
  const swEl=$('sim-wr'); if(swEl){ swEl.textContent=(sim.win_rate||0).toFixed(1)+'%'; swEl.className='v '+(sim.win_rate>=55?'vbull':sim.win_rate>=45?'vwarn':'vbear'); }
  setText('sim-avgwin',  sim.avg_win!=null  ? '₹+'+fmt(sim.avg_win,0)  : '—');
  setText('sim-avgloss', sim.avg_loss!=null ? '₹'+fmt(sim.avg_loss,0)  : '—');
  setText('sim-best',    sim.best_pnl!=null  ? '₹+'+fmt(sim.best_pnl,0)+' ('+sim.best_date+')' : '—');
  setText('sim-worst',   sim.worst_pnl!=null ? '₹'+fmt(sim.worst_pnl,0)+' ('+sim.worst_date+')' : '—');
  const top5El = $('sim-top5');
  if(top5El && sim.top5){
    top5El.innerHTML = sim.top5.map(r=>
      `<div class="sim-row"><span style="color:var(--dim)">${r.date}</span><span>${r.dow}</span><span style="color:var(--warn)">VIX ${r.vix.toFixed(1)}</span><span style="color:var(--dim)">${r.gap>=0?'+':''}${r.gap.toFixed(2)}%</span><span class="${r.pnl>=0?'vbull':'vbear'}">${r.pnl>=0?'+':''}₹${fmt(r.pnl,0)}</span><span style="color:var(--dim)">${r.sim.toFixed(0)}%</span></div>`
    ).join('');
  }

  /* ── Today's Positions (from Groww API) ── */
  const tlEl = $('trades-list');
  if(tlEl){
    const trades = pt.trades || [];
    const cnt = $('trades-count');
    const err = pt.error || '';
    if(cnt) cnt.textContent = trades.length ? trades.length + ' positions' : '';
    if(err && !trades.length){
      const msgs = {'no_token':'Waiting for Groww auth token…','auth_expired':'Token expired — re-authenticating…'};
      tlEl.innerHTML = `<div class="ptai-no-data" style="color:var(--warn)">${msgs[err]||('API error: '+err)}</div>`;
    } else if(trades.length){
      tlEl.innerHTML = `<div style="display:grid;grid-template-columns:1fr 85px 80px 55px 55px 55px 68px;gap:4px 6px;font-size:10px;color:var(--dim);padding:4px 0 6px;border-bottom:1px solid var(--bdr);letter-spacing:.3px">
        <span>SYMBOL</span><span style="text-align:right">REALISED</span><span style="text-align:right">UNREALISED</span><span style="text-align:right">LTP</span><span style="text-align:right">B.QTY</span><span style="text-align:right">OPEN</span><span style="text-align:right">AVG</span></div>` +
        trades.map(t=>{
          const rCls = t.realised>0?'vbull':t.realised<0?'vbear':'vdim';
          const uCls = (t.unrealised||0)>0?'vbull':(t.unrealised||0)<0?'vbear':'vdim';
          const openTag = t.is_open ? ` <span style="color:var(--warn);font-size:9px">●</span>` : '';
          return `<div class="pnl-trade-row" style="display:grid;grid-template-columns:1fr 85px 80px 55px 55px 55px 68px;gap:4px 6px;align-items:center">
            <span class="t-sym">${t.sym}${openTag}</span>
            <span class="t-pnl ${rCls}" style="text-align:right">${t.realised>0?'+':t.realised<0?'-':''}₹${fmt(Math.abs(t.realised),0)}</span>
            <span class="t-pnl ${uCls}" style="text-align:right">${t.is_open?((t.unrealised||0)>0?'+':''+(t.unrealised||0)<0?'-':'')+'₹'+fmt(Math.abs(t.unrealised||0),0):'—'}</span>
            <span style="text-align:right;color:var(--info);font-family:'JetBrains Mono',monospace">${t.ltp>0?fmt(t.ltp,1):'—'}</span>
            <span style="text-align:right;color:var(--dim)">${t.buy_qty||0}</span>
            <span style="text-align:right;color:${t.is_open?'var(--warn)':'var(--dim)'};font-weight:${t.is_open?'700':'400'}">${t.net_qty||0}</span>
            <span style="text-align:right;color:var(--dim)">${t.avg_price>0?fmt(t.avg_price,1):'-'}</span>
          </div>`;
        }).join('');
    } else {
      tlEl.innerHTML = '<div class="ptai-no-data">No positions today — market may be closed</div>';
    }
  }

  /* ── Margin / Capital ── */
  const mg = d.margin || {};
  if(mg.ts){
    setText('margin-ts', _age(mg.ts));
    const f = (v,cls)=>{ const e=document.createElement('span'); e.className='v '+cls; e.textContent=(v>0?'₹':'')+fmt(v,0); return e.outerHTML; };
    const fmtM = (v,cls='')=>`<span class="v ${cls}" style="font-family:'JetBrains Mono',monospace">${v>=0?'₹':'-₹'}${fmt(Math.abs(v),0)}</span>`;
    setText('m-cash',     ''); document.getElementById('m-cash')    && (document.getElementById('m-cash').innerHTML    = fmtM(mg.clear_cash,    'vbull'));
    setText('m-opt-buy',  ''); document.getElementById('m-opt-buy') && (document.getElementById('m-opt-buy').innerHTML = fmtM(mg.opt_buy_avail,  'vbull'));
    setText('m-opt-sell', ''); document.getElementById('m-opt-sell')&& (document.getElementById('m-opt-sell').innerHTML= fmtM(mg.opt_sell_avail, ''));
    setText('m-used',     ''); document.getElementById('m-used')    && (document.getElementById('m-used').innerHTML    = fmtM(mg.margin_used,    'vwarn'));
    setText('m-span',     ''); document.getElementById('m-span')    && (document.getElementById('m-span').innerHTML    = fmtM(mg.span_used,      'vdim'));
    setText('m-exp',      ''); document.getElementById('m-exp')     && (document.getElementById('m-exp').innerHTML     = fmtM(mg.exposure_used,  'vdim'));
    setText('m-brok',     ''); document.getElementById('m-brok')    && (document.getElementById('m-brok').innerHTML    = fmtM(mg.brokerage,      'vbear'));
  }

  /* ── Today's Orders ── */
  const od = d.orders || {};
  const olEl = $('orders-list');
  if(olEl && od.ts){
    setText('orders-ts', _age(od.ts));
    const orders = od.orders || [];
    if(!orders.length){
      olEl.innerHTML = '<div class="ptai-no-data">No orders today</div>';
    } else {
      const statusCls = s => s==='COMPLETE'||s==='EXECUTED'?'os-complete':s==='REJECTED'||s==='FAILED'?'os-rejected':s==='CANCELLED'?'os-cancelled':'os-pending';
      olEl.innerHTML =
        `<div class="ord-row" style="font-size:10px;color:var(--dim);border-bottom:1px solid var(--bdr);padding-bottom:4px">
          <span>SYMBOL</span><span style="text-align:center">STATUS</span><span style="text-align:right">QTY</span><span style="text-align:right">FILL</span><span style="text-align:right">AVG PRICE</span><span style="text-align:right">TYPE</span>
        </div>` +
        orders.map(o=>{
          const bCls = o.type==='BUY'?'vbull':'vbear';
          return `<div class="ord-row">
            <span class="t-sym" style="font-size:10px">${o.sym}</span>
            <span><span class="ord-status ${statusCls(o.status)}">${o.status.slice(0,4)}</span></span>
            <span style="text-align:right;color:var(--dim)">${o.qty}</span>
            <span style="text-align:right;color:var(--dim)">${o.filled}</span>
            <span style="text-align:right;font-family:'JetBrains Mono',monospace;color:var(--txt)">${o.avg_fill>0?'₹'+fmt(o.avg_fill,1):(o.price>0?'₹'+fmt(o.price,1):'—')}</span>
            <span style="text-align:right" class="${bCls}">${o.type}</span>
          </div>`;
        }).join('');
    }
  }

  /* ── AI Advisory ── */
  const aiEl = $('ptai-ai-body');
  if(aiEl){
    if(d.features&&d.features.ptai_ai===false){
      aiEl.innerHTML = '<div class="ptai-no-data" style="color:var(--dim)">Enable AI Assistance to generate advisory.<br><span style="font-size:10px">Uses Claude CLI · refreshes every 30 min</span></div>';
    } else if(ai.status==='loading'){
      aiEl.innerHTML = '<div class="ptai-no-data"><span style="color:#a855f7;animation:pulse 1.5s infinite">⏳ Generating AI advisory…</span></div>';
    } else if(ai.status==='ok' && ai.text){
      aiEl.innerHTML = `<div class="ai-text" style="font-size:12px;line-height:1.8;white-space:pre-wrap;color:#cbd5e1">${ai.text}</div><div style="font-size:10px;color:var(--dim);margin-top:8px">Generated ${_age(ai.ts)}</div>`;
    } else if(ai.status==='no_cli'){
      aiEl.innerHTML = '<div class="ptai-no-data" style="color:var(--warn)">Claude CLI not found. Install Claude Code to enable AI advisory.</div>';
    } else {
      aiEl.innerHTML = '<div class="ptai-no-data" style="color:var(--dim)">Enable AI Assistance to generate advisory.<br><span style="font-size:10px">Uses Claude CLI · refreshes every 30 min</span></div>';
    }
  }
}

// ── VIX Analysis ─────────────────────────────────────────────────────────────
let _vixAlarmOn = true;
let _vixAlarmLastFired = 0;

function vixToggleAlarm(){
  _vixAlarmOn = !_vixAlarmOn;
  const btn = $('vix-alarm-btn');
  const lbl = $('vix-alarm-label');
  if(btn){
    btn.textContent = _vixAlarmOn ? '🔔 Alarm ON' : '🔕 Alarm OFF';
    btn.classList.toggle('vix-alarm-on', _vixAlarmOn);
  }
  if(lbl) lbl.style.opacity = _vixAlarmOn ? '1' : '0.4';
}

function _vixRegime(v){
  if(!v) return {label:'—', cls:'', color:'var(--dim)'};
  if(v < 12)  return {label:'ULTRA CALM', cls:'vix-calm',     color:'#4ade80'};
  if(v < 15)  return {label:'CALM',       cls:'vix-calm',     color:'#4ade80'};
  if(v < 18)  return {label:'MODERATE',   cls:'vix-moderate', color:'var(--warn)'};
  if(v < 22)  return {label:'ELEVATED',   cls:'vix-elevated', color:'#fb923c'};
  return              {label:'DANGER',    cls:'vix-danger',   color:'var(--bear)'};
}

function _vixChgClr(pct){
  if(pct == null) return 'var(--dim)';
  if(pct > 3)  return 'var(--bear)';
  if(pct > 1)  return '#fb923c';
  if(pct < -3) return '#4ade80';
  if(pct < -1) return 'var(--bull)';
  return 'var(--txt)';
}

function renderVix(d){
  const hist = d.vix_history || [];
  const sessOpen = d.vix_session_open || 0;

  if(!hist.length){
    const cv = $('vix-curr-val'); if(cv) cv.textContent = '—';
    return;
  }

  const curr = hist[hist.length - 1].v;
  const lv   = d.pnl_analysis && d.pnl_analysis.live ? d.pnl_analysis.live : {};

  // Current VIX
  const regime = _vixRegime(curr);
  const cvEl = $('vix-curr-val');
  if(cvEl){ cvEl.textContent = curr.toFixed(2); cvEl.className = 'vix-stat-num ' + regime.cls; }

  // Day change % (from pnl_analysis live data)
  const dayChgPct = lv.vix_chg_pct != null ? lv.vix_chg_pct : (sessOpen ? (curr - sessOpen) / sessOpen * 100 : null);
  const dcEl = $('vix-day-chg');
  if(dcEl){
    dcEl.textContent = dayChgPct != null ? (dayChgPct >= 0 ? '+' : '') + dayChgPct.toFixed(1) + '%' : '—';
    dcEl.style.color = _vixChgClr(dayChgPct);
  }

  // Session Hi / Lo
  const vals = hist.map(h => h.v);
  const hi = Math.max(...vals), lo = Math.min(...vals);
  const hlEl = $('vix-hilow');
  if(hlEl) hlEl.innerHTML = `<span style="color:var(--bear)">${hi.toFixed(2)}</span> / <span style="color:var(--bull)">${lo.toFixed(2)}</span>`;

  // 10-min change (~5 ticks × 2 min)
  const refIdx = Math.max(0, hist.length - 6);
  const refV   = hist[refIdx].v;
  const tenPct = refV ? (curr - refV) / refV * 100 : null;
  const t10El  = $('vix-10m-chg');
  if(t10El){
    t10El.textContent = tenPct != null ? (tenPct >= 0 ? '+' : '') + tenPct.toFixed(1) + '%' : '—';
    t10El.style.color = _vixChgClr(tenPct);
  }

  // Regime label
  const rgEl = $('vix-regime');
  if(rgEl){ rgEl.textContent = regime.label; rgEl.className = 'vix-stat-num ' + regime.cls; rgEl.style.fontSize = '12px'; }

  // Timestamp
  const tsEl = $('vix-hist-ts');
  if(tsEl && hist.length) tsEl.textContent = hist[hist.length-1].t;

  // Draw sparkline
  _drawVixSparkline(hist, curr);

  // Store globally for VIX Auto Config
  window._mbVixCurrent = curr;
  window._mbVixDayChg  = dayChgPct;

  // Browser alarm — VIX fast spike (>3% in 10 min)
  if(_vixAlarmOn && tenPct != null && tenPct > 3){
    const now = Date.now();
    if(now - _vixAlarmLastFired > 300000){  // max once per 5 min
      _vixAlarmLastFired = now;
      _notifSound && _notifSound('warn', 'VIX');
    }
  }
}

function _drawVixSparkline(hist, curr){
  const canvas = $('vix-sparkline');
  if(!canvas || !hist.length) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth || 600, H = 72;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const vals = hist.map(h => h.v);
  const lo = Math.min(...vals) * 0.998, hi = Math.max(...vals) * 1.002;
  const range = hi - lo || 1;
  const pad = {l:36, r:8, t:8, b:8};
  const w = W - pad.l - pad.r, h = H - pad.t - pad.b;

  const xOf = i => pad.l + (i / (hist.length - 1 || 1)) * w;
  const yOf = v => pad.t + h - ((v - lo) / range) * h;

  // Danger / elevated zones
  const zoneData = [{thr:22, color:'rgba(239,68,68,.08)'},{thr:18, color:'rgba(251,146,60,.06)'},{thr:15, color:'rgba(250,204,21,.05)'}];
  zoneData.forEach(({thr, color}) => {
    if(thr > lo && thr < hi * 1.1){
      const y = yOf(Math.min(thr, hi));
      ctx.fillStyle = color;
      ctx.fillRect(pad.l, y, w, H - pad.b - y);
    }
  });

  // Reference lines
  [15, 18, 22].forEach(thr => {
    if(thr >= lo && thr <= hi * 1.05){
      const y = yOf(thr);
      ctx.beginPath(); ctx.setLineDash([3,3]);
      ctx.strokeStyle = thr === 22 ? 'rgba(239,68,68,.4)' : thr === 18 ? 'rgba(251,146,60,.3)' : 'rgba(250,204,21,.25)';
      ctx.lineWidth = 1;
      ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + w, y);
      ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(100,116,139,.6)'; ctx.font = '8px JetBrains Mono, monospace';
      ctx.fillText(thr, 2, y + 3);
    }
  });

  // Gradient fill under line
  const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
  grad.addColorStop(0, 'rgba(56,189,248,.25)');
  grad.addColorStop(1, 'rgba(56,189,248,.02)');
  ctx.beginPath();
  hist.forEach((h, i) => { i === 0 ? ctx.moveTo(xOf(i), yOf(h.v)) : ctx.lineTo(xOf(i), yOf(h.v)); });
  ctx.lineTo(xOf(hist.length-1), H - pad.b);
  ctx.lineTo(xOf(0), H - pad.b);
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();

  // Line
  ctx.beginPath();
  hist.forEach((h, i) => { i === 0 ? ctx.moveTo(xOf(i), yOf(h.v)) : ctx.lineTo(xOf(i), yOf(h.v)); });
  ctx.strokeStyle = '#38bdf8'; ctx.lineWidth = 1.5; ctx.lineJoin = 'round';
  ctx.stroke();

  // Current dot
  const cx = xOf(hist.length - 1), cy = yOf(curr);
  ctx.beginPath(); ctx.arc(cx, cy, 3, 0, Math.PI * 2);
  ctx.fillStyle = _vixRegime(curr).color; ctx.fill();

  // Current value label
  ctx.fillStyle = _vixRegime(curr).color; ctx.font = 'bold 10px JetBrains Mono, monospace';
  ctx.fillText(curr.toFixed(2), cx - 14, cy - 6);

  // Timeline labels
  const t0El = $('vix-spark-t0'), tnEl = $('vix-spark-tn');
  if(t0El) t0El.textContent = hist[0].t;
  if(tnEl) tnEl.textContent = hist[hist.length-1].t;

  // Hover tooltip on canvas
  canvas.onmousemove = function(e){
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const idx = Math.round((mx - pad.l) / w * (hist.length - 1));
    const tt = $('vix-spark-tt');
    if(tt && idx >= 0 && idx < hist.length){
      tt.textContent = hist[idx].t + '  VIX: ' + hist[idx].v.toFixed(2);
      tt.style.display = 'block';
    }
  };
  canvas.onmouseleave = function(){
    const tt = $('vix-spark-tt'); if(tt) tt.style.display = 'none';
  };
}

// ─────────────────────────────── VIX TAB ────────────────────────────────────

let _vtLastData = null;

function initVixTab(){
  if(_vtLastData) renderVixTab(_vtLastData);
}

function renderVixTab(d){
  _vtLastData = d;
  const hist = d.vix_history || [];
  const sessOpen = d.vix_session_open || 0;
  if(!hist.length){
    const h=new Date().getHours();
    const mktClosed=h<9||h>=16;
    $('vt-analysis').textContent=mktClosed
      ? 'Market is closed. VIX data appears when market opens at 9:15 AM. Cached data from the current session loads automatically on restart.'
      : 'Fetching VIX from NSE — data will appear within 2 minutes. If this persists, NSE API may be rate-limiting. The fetch loop retries automatically.';
    return;
  }

  const curr   = hist[hist.length-1].v;
  const vals   = hist.map(h=>h.v);
  const hi     = Math.max(...vals), lo = Math.min(...vals);
  const regime = _vixRegime(curr);

  // Big number
  const cEl=$('vt-curr'); if(cEl){ cEl.textContent=curr.toFixed(2); cEl.style.color=regime.color; }
  const rEl=$('vt-regime'); if(rEl){ rEl.textContent=regime.label; rEl.style.color=regime.color; }

  // Session stats
  const sessChg = sessOpen ? (curr-sessOpen)/sessOpen*100 : null;
  const scEl=$('vt-sess-chg'); if(scEl){ scEl.textContent=sessChg!=null?(sessChg>=0?'+':'')+sessChg.toFixed(2)+'%':'—'; scEl.style.color=_vixChgClr(sessChg); }

  // 10-min change (~5 ticks at 2-min interval)
  const ref10Idx=Math.max(0,hist.length-6); const ref10V=hist[ref10Idx].v;
  const tenPct=ref10V?(curr-ref10V)/ref10V*100:null;
  const t10El=$('vt-10m'); if(t10El){ t10El.textContent=tenPct!=null?(tenPct>=0?'+':'')+tenPct.toFixed(2)+'%':'—'; t10El.style.color=_vixChgClr(tenPct); }

  // Hi / Lo / Open / Range
  const hiEl=$('vt-hi'); if(hiEl) hiEl.textContent=hi.toFixed(2);
  const loEl=$('vt-lo'); if(loEl) loEl.textContent=lo.toFixed(2);
  const opEl=$('vt-open'); if(opEl) opEl.textContent=sessOpen?sessOpen.toFixed(2):'—';
  const rgEl=$('vt-range'); if(rgEl) rgEl.textContent=(hi-lo).toFixed(2);

  // Data points
  const ptEl=$('vt-pts'); if(ptEl) ptEl.textContent=hist.length;

  // Timestamp
  const tsEl=$('vt-ts'); if(tsEl) tsEl.textContent=hist[hist.length-1].t;

  // Velocity (30-min = ~15 ticks)
  const ref30Idx=Math.max(0,hist.length-16); const ref30V=hist[ref30Idx].v;
  const vel30Pct=ref30V?(curr-ref30V)/ref30V*100:null;
  const velEl=$('vt-vel');
  const velLblEl=$('vt-vel-lbl');
  if(velEl){
    velEl.textContent=vel30Pct!=null?(vel30Pct>=0?'↑+':'↓')+Math.abs(vel30Pct).toFixed(2)+'%':'—';
    velEl.style.color=vel30Pct!=null?(vel30Pct>2?'var(--bear)':vel30Pct<-2?'var(--bull)':'var(--txt)'):'var(--dim)';
    if(velLblEl) velLblEl.textContent=vel30Pct==null?'—':vel30Pct>3?'FAST RISE — PANIC':vel30Pct>1.5?'Rising — caution':vel30Pct<-3?'FAST DROP — RELIEF':vel30Pct<-1.5?'Falling — calming':'Stable';
  }

  // Trend (compare first-half avg vs second-half avg)
  const mid=Math.floor(vals.length/2);
  const avgFirst=vals.slice(0,mid).reduce((a,b)=>a+b,0)/(mid||1);
  const avgSecond=vals.slice(mid).reduce((a,b)=>a+b,0)/((vals.length-mid)||1);
  const trendDiff=avgSecond-avgFirst;
  const trendEl=$('vt-trend'); const trendLblEl=$('vt-trend-lbl');
  if(trendEl){
    if(Math.abs(trendDiff)<0.1){ trendEl.textContent='→ SIDEWAYS'; trendEl.style.color='var(--dim)'; if(trendLblEl) trendLblEl.textContent='VIX drifting sideways'; }
    else if(trendDiff>0){ trendEl.textContent='↑ RISING'; trendEl.style.color='var(--bear)'; if(trendLblEl) trendLblEl.textContent='Anxiety building intraday'; }
    else { trendEl.textContent='↓ FALLING'; trendEl.style.color='var(--bull)'; if(trendLblEl) trendLblEl.textContent='Market calming intraday'; }
  }

  // Option premium implication
  const premEl=$('vt-prem'); const premLblEl=$('vt-prem-lbl');
  if(premEl){
    if(curr<12){      premEl.textContent='CHEAP'; premEl.style.color='#4ade80'; if(premLblEl) premLblEl.textContent='Buy options — low IV'; }
    else if(curr<15){ premEl.textContent='FAIR'; premEl.style.color='var(--bull)'; if(premLblEl) premLblEl.textContent='Normal pricing'; }
    else if(curr<18){ premEl.textContent='MODERATE'; premEl.style.color='var(--warn)'; if(premLblEl) premLblEl.textContent='Slightly elevated IV'; }
    else if(curr<22){ premEl.textContent='EXPENSIVE'; premEl.style.color='#fb923c'; if(premLblEl) premLblEl.textContent='Sell premium bias'; }
    else{             premEl.textContent='VERY RICH'; premEl.style.color='var(--bear)'; if(premLblEl) premLblEl.textContent='High IV — extreme caution'; }
  }

  // Analysis text
  const analysisEl=$('vt-analysis');
  if(analysisEl){
    const trendWord=trendDiff>0.1?'rising':'falling';
    const velWord=vel30Pct!=null?(vel30Pct>2?'rapidly rising':vel30Pct<-2?'rapidly falling':'stable'):'stable';
    let analysis='';
    const sessNote=sessChg!=null?` Session change: ${sessChg>=0?'+':''}${sessChg.toFixed(2)}%.`:'';
    if(curr<12)
      analysis=`VIX is at an ultra-low level (${curr.toFixed(2)}), signaling extreme complacency. Options premiums are cheap — IV is depressed, making directional buys attractive.${sessNote} Watch for a sudden VIX spike which can catch sellers off-guard. Trend today: ${trendWord}. Velocity: ${velWord}. Strategy: prefer buying cheap options; avoid writing spreads at very thin premium.`;
    else if(curr<15)
      analysis=`VIX is calm (${curr.toFixed(2)}). Market participants are relaxed, options are fairly priced.${sessNote} Momentum moves tend to be steady. Trend: ${trendWord}. Velocity: ${velWord}. Strategy: directional option buying works well; SL distances can be tighter than usual.`;
    else if(curr<18)
      analysis=`VIX is moderate (${curr.toFixed(2)}). IV is slightly elevated — options are a bit pricier than normal.${sessNote} Trend: ${trendWord}. Velocity: ${velWord}. Intraday moves can be choppy. Strategy: use quick-target mode with tighter points; avoid holding options overnight.`;
    else if(curr<22)
      analysis=`VIX is elevated (${curr.toFixed(2)}) — fear is present.${sessNote} Options are expensive; buying premium has negative edge unless the directional move is strong. Trend: ${trendWord}. Velocity: ${velWord}. Strategy: reduce lot size, widen targets, be ready for whipsaw. Avoid FOMO entries.`;
    else
      analysis=`VIX is in DANGER territory (${curr.toFixed(2)}).${sessNote} Extreme fear / panic in the market. Options are very expensive — every buy is against a headwind of high IV. Trend: ${trendWord}. Velocity: ${velWord}. Strategy: strongly consider sitting out or using very small size. If trading, prefer selling far-OTM spreads or wait for VIX to peak and start reversing.`;
    analysisEl.textContent=analysis;
  }

  // Recent ticks log (last 15, newest first)
  const ticksEl=$('vt-ticks');
  if(ticksEl){
    const recent=[...hist].reverse().slice(0,15);
    ticksEl.innerHTML=recent.map((h,i)=>{
      const prev=i<recent.length-1?recent[i+1].v:h.v;
      const delta=h.v-prev;
      const arrow=delta>0.01?'▲':delta<-0.01?'▼':'─';
      const color=delta>0.01?'var(--bear)':delta<-0.01?'var(--bull)':'var(--dim)';
      return `<div style="display:flex;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,.04);padding:1px 0">
        <span style="color:var(--dim)">${h.t}</span>
        <span style="font-weight:600;color:${_vixRegime(h.v).color}">${h.v.toFixed(2)}</span>
        <span style="color:${color}">${arrow} ${delta!==0?Math.abs(delta).toFixed(2):''}</span>
      </div>`;
    }).join('');
  }

  // Big sparkline
  _drawVixTabSparkline(hist, curr);

  // Market Regime section (uses full snapshot data stored in _vtLastData)
  renderMarketRegime(_vtLastData);
}

function _drawVixTabSparkline(hist, curr){
  const canvas=$('vt-sparkline');
  if(!canvas||!hist.length) return;
  const dpr=window.devicePixelRatio||1;
  const W=canvas.offsetWidth||900, H=130;
  canvas.width=W*dpr; canvas.height=H*dpr;
  const ctx=canvas.getContext('2d');
  ctx.scale(dpr,dpr);

  const vals=hist.map(h=>h.v);
  const lo=Math.min(...vals)*0.997, hi=Math.max(...vals)*1.003;
  const range=hi-lo||1;
  const pad={l:42,r:12,t:10,b:14};
  const w=W-pad.l-pad.r, h=H-pad.t-pad.b;
  const xOf=i=>pad.l+(i/(hist.length-1||1))*w;
  const yOf=v=>pad.t+h-((v-lo)/range)*h;

  // Zone fills
  [{thr:22,color:'rgba(239,68,68,.09)'},{thr:18,color:'rgba(251,146,60,.07)'},{thr:15,color:'rgba(250,204,21,.05)'}].forEach(({thr,color})=>{
    if(thr>lo&&thr<hi*1.05){
      const y=yOf(Math.min(thr,hi));
      ctx.fillStyle=color; ctx.fillRect(pad.l,y,w,H-pad.b-y);
    }
  });

  // Reference lines
  [12,15,18,22].forEach(thr=>{
    if(thr>=lo&&thr<=hi*1.05){
      const y=yOf(thr);
      ctx.beginPath(); ctx.setLineDash([4,4]);
      ctx.strokeStyle=thr>=22?'rgba(239,68,68,.5)':thr>=18?'rgba(251,146,60,.4)':thr>=15?'rgba(250,204,21,.3)':'rgba(74,222,128,.3)';
      ctx.lineWidth=1; ctx.moveTo(pad.l,y); ctx.lineTo(pad.l+w,y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle='rgba(148,163,184,.7)'; ctx.font='9px JetBrains Mono,monospace';
      ctx.fillText(thr,3,y+3);
    }
  });

  // Gradient area fill
  const grad=ctx.createLinearGradient(0,pad.t,0,H-pad.b);
  const col=_vixRegime(curr).color;
  grad.addColorStop(0,col.replace('#','rgba(').replace(/(..)(..)(..)$/,(_,r,g,b)=>`${parseInt(r,16)},${parseInt(g,16)},${parseInt(b,16)},.25)`)||'rgba(56,189,248,.25)');
  grad.addColorStop(1,'rgba(56,189,248,.02)');
  ctx.beginPath();
  hist.forEach((h,i)=>{ i===0?ctx.moveTo(xOf(i),yOf(h.v)):ctx.lineTo(xOf(i),yOf(h.v)); });
  ctx.lineTo(xOf(hist.length-1),H-pad.b); ctx.lineTo(xOf(0),H-pad.b); ctx.closePath();
  ctx.fillStyle=grad; ctx.fill();

  // Line
  ctx.beginPath();
  hist.forEach((h,i)=>{ i===0?ctx.moveTo(xOf(i),yOf(h.v)):ctx.lineTo(xOf(i),yOf(h.v)); });
  ctx.strokeStyle=col||'#38bdf8'; ctx.lineWidth=2; ctx.lineJoin='round'; ctx.stroke();

  // Dot + label at current
  const cx=xOf(hist.length-1),cy=yOf(curr);
  ctx.beginPath(); ctx.arc(cx,cy,4,0,Math.PI*2); ctx.fillStyle=col||'#38bdf8'; ctx.fill();
  ctx.fillStyle=col||'#38bdf8'; ctx.font='bold 11px JetBrains Mono,monospace';
  ctx.fillText(curr.toFixed(2),cx-16,cy-8);

  // Time axis labels
  const t0El=$('vt-spark-t0'), tnEl=$('vt-spark-tn');
  if(t0El) t0El.textContent=hist[0].t;
  if(tnEl) tnEl.textContent=hist[hist.length-1].t;

  // Hover tooltip
  canvas.onmousemove=function(e){
    const rect=canvas.getBoundingClientRect();
    const mx=e.clientX-rect.left;
    const idx=Math.round((mx-pad.l)/w*(hist.length-1));
    const tt=$('vt-spark-tt');
    if(tt&&idx>=0&&idx<hist.length){ tt.textContent=hist[idx].t+'  VIX: '+hist[idx].v.toFixed(2); tt.style.display='inline'; }
  };
  canvas.onmouseleave=function(){ const tt=$('vt-spark-tt'); if(tt) tt.style.display='none'; };
}

// ─────────────────────── MARKET REGIME RENDERER ─────────────────────────────

function renderMarketRegime(d){
  const oi   = d.oi_snapshot  || {};
  const fibo = (d.bots || {}).fibo || {};
  const spot = d.spot || oi.price || 0;
  const hist = d.vix_history  || [];

  // ── 1. NIFTY day range — fibo bot preferred; Groww Quote API as live fallback ──
  const ohlcFallback = ((d.mkt_idx || {})._ohlc || {}).nifty || {};
  const dh = fibo.day_high || ohlcFallback.high || 0;
  const dl = fibo.day_low  || ohlcFallback.low  || 0;
  const rangeSrc = (fibo.day_high && fibo.day_low) ? '' : (ohlcFallback.high ? ' (live)' : '');
  const rangePts = (dh && dl) ? Math.round((dh - dl) * 10) / 10 : 0;
  const rangePct = (rangePts && spot) ? rangePts / spot * 100 : 0;

  // ── 2. ATM straddle from oi_snapshot.atm_momentum ───────────────────────
  const mom = oi.atm_momentum || {};
  const ceLtp = mom.ce_ltp || 0, peLtp = mom.pe_ltp || 0;
  const straddle = ceLtp + peLtp;
  const ceDelta = mom.ce_ltp_chg || 0, peDelta = mom.pe_ltp_chg || 0;
  const totalPremMove = Math.abs(ceDelta) + Math.abs(peDelta);

  // ── 3. VIX ───────────────────────────────────────────────────────────────
  const vix = hist.length ? hist[hist.length-1].v : 0;
  const sessOpen = d.vix_session_open || 0;
  const vixSessChgPct = (vix && sessOpen) ? (vix - sessOpen) / sessOpen * 100 : 0;

  // ── 4. IV squeeze proxy ─ avg ATM IV vs expected (VIX/√252·10) ──────────
  const ceIV = oi.atm_ce_iv || 0, peIV = oi.atm_pe_iv || 0;
  const avgIV = (ceIV && peIV) ? (ceIV + peIV) / 2 : (ceIV || peIV);
  // expected 1-day move% implied by VIX: VIX/√252
  const expectedMovePct = vix ? vix / Math.sqrt(252) : 0;
  const ivSqueeze = (avgIV && expectedMovePct) ? avgIV < expectedMovePct * 0.8 : false;

  // ── 5. Scoring ───────────────────────────────────────────────────────────
  // Range score (0-2)
  let rangeScore = rangePct >= 1.5 ? 2 : rangePct >= 0.75 ? 1 : 0;
  // Premium movement score (0-2)
  let premScore  = totalPremMove >= 8 ? 2 : totalPremMove >= 3 ? 1 : 0;
  // VIX score (0-2): rising VIX = more trending; high VIX = more volatile
  let vixScore   = (vix >= 15 || vixSessChgPct >= 2) ? 2 : (vix >= 12 || vixSessChgPct >= 0.8) ? 1 : 0;

  const totalScore = rangeScore + premScore + vixScore; // 0–6

  // Both-side whipsaw: range present but premiums stagnant/falling
  const bothSideWhipsaw = rangePct >= 0.8 && totalPremMove < 3 && (ceDelta < 0 || peDelta < 0);
  // Premium crush: both CE and PE declining
  const premCrush = ceDelta < 0 && peDelta < 0;

  // ── 6. Verdict ───────────────────────────────────────────────────────────
  let regimeLabel, regimeColor, verdictText;

  if(premCrush && rangePct < 1.2){
    regimeLabel = '⚡ THETA DECAY';
    regimeColor = '#94a3b8';
    verdictText = `Market is extremely range-bound with both call (${ceDelta>0?'+':''}${ceDelta.toFixed(1)}) and put (${peDelta>0?'+':''}${peDelta.toFixed(1)}) premiums bleeding. NIFTY day range is only ${rangePts} pts (${rangePct.toFixed(2)}% of spot). This is a classic theta-decay / time-value bleed session — option buyers are losing on both sides regardless of direction. VIX is ${vix ? vix.toFixed(2) : '—'}. Strategy: avoid directional option buying; consider iron condors or simply stay out of intraday options.`;
  } else if(bothSideWhipsaw){
    regimeLabel = '↔ BOTH-SIDE CHOP';
    regimeColor = '#f59e0b';
    verdictText = `Market is oscillating both ways (day range: ${rangePts} pts, ${rangePct.toFixed(2)}%) but option premiums are barely moving (CE Δ${ceDelta>0?'+':''}${ceDelta.toFixed(1)}, PE Δ${peDelta>0?'+':''}${peDelta.toFixed(1)}). This is exactly the whipsaw/sideways pattern — index moves are not sustained in any direction, so premiums decay. VIX ${vix ? vix.toFixed(2) : '—'}, straddle ₹${straddle.toFixed(0)}. Strategy: avoid momentum trades; premium buys will be eaten by chop. Wait for a clear directional break with premium expansion before entering.`;
  } else if(totalScore <= 1){
    regimeLabel = '— SIDEWAYS';
    regimeColor = '#64748b';
    verdictText = `Low-activity sideways market. NIFTY range ${rangePts} pts (${rangePct.toFixed(2)}%), ATM straddle ₹${straddle.toFixed(0)}, VIX ${vix ? vix.toFixed(2) : '—'}. Premium activity is minimal (CE Δ${ceDelta>0?'+':''}${ceDelta.toFixed(1)}, PE Δ${peDelta>0?'+':''}${peDelta.toFixed(1)}). No clear edge for directional buying. Strategy: reduce position size, widen targets if trading, or sit out and wait for setup.`;
  } else if(totalScore <= 3){
    regimeLabel = '〜 MIXED';
    regimeColor = '#a78bfa';
    verdictText = `Mixed conditions — some range (${rangePts} pts, ${rangePct.toFixed(2)}%) but not a clean trend. ATM straddle ₹${straddle.toFixed(0)}, VIX ${vix ? vix.toFixed(2) : '—'}. Premium activity moderate (CE Δ${ceDelta>0?'+':''}${ceDelta.toFixed(1)}, PE Δ${peDelta>0?'+':''}${peDelta.toFixed(1)}). Moves are happening but with inconsistent follow-through. Strategy: take quick scalps with tight targets; avoid holding for big moves. Use ATR-based SL.`;
  } else if(totalScore <= 5){
    regimeLabel = '↗ TRENDING';
    regimeColor = '#4ade80';
    verdictText = `Trending conditions developing. NIFTY range ${rangePts} pts (${rangePct.toFixed(2)}%), ATM straddle ₹${straddle.toFixed(0)}, VIX ${vix ? vix.toFixed(2) : '—'}. Premiums are moving (CE Δ${ceDelta>0?'+':''}${ceDelta.toFixed(1)}, PE Δ${peDelta>0?'+':''}${peDelta.toFixed(1)}). A directional bias is forming — follow premium expansion to identify the favored direction. Strategy: standard momentum setups with 1:1.5+ R:R.`;
  } else {
    regimeLabel = '🔥 STRONG TREND';
    regimeColor = '#ef4444';
    verdictText = `Strong trending day. NIFTY range ${rangePts} pts (${rangePct.toFixed(2)}%), ATM straddle ₹${straddle.toFixed(0)}, VIX ${vix ? vix.toFixed(2) : '—'} (session ${vixSessChgPct>=0?'+':''}${vixSessChgPct.toFixed(1)}%). Premiums are surging (CE Δ${ceDelta>0?'+':''}${ceDelta.toFixed(1)}, PE Δ${peDelta>0?'+':''}${peDelta.toFixed(1)}). This is a high-conviction directional session. Strategy: ride the trend — wider targets justified, use trailing SL rather than fixed exit. Follow the premium that's expanding.`;
  }

  if(ivSqueeze && regimeLabel.includes('SIDEWAYS'))
    verdictText += ` IV squeeze detected (ATM IV ${avgIV.toFixed(1)}% vs expected ${expectedMovePct.toFixed(1)}%) — market may be coiling for a breakout.`;

  // ── 7. Render ─────────────────────────────────────────────────────────────
  const badge=$('mr-badge');
  if(badge){ badge.textContent=regimeLabel; badge.style.color=regimeColor; badge.style.borderColor=regimeColor; badge.style.background=regimeColor+'18'; }

  // ── Range card ───────────────────────────────────────────────────────────
  const rpEl=$('mr-range-pts');
  if(rpEl){ rpEl.textContent=rangePts ? rangePts+' pts'+rangeSrc : '—'; rpEl.style.color='var(--txt)'; }
  const rpcEl=$('mr-range-pct');
  if(rpcEl){
    const rColor=rangePct>=1.5?'var(--bear)':rangePct>=0.75?'var(--warn)':'#64748b';
    rpcEl.textContent=rangePct ? rangePct.toFixed(2)+'% of spot' : 'starting up…';
    rpcEl.style.color=rColor;
  }
  const rlblEl=$('mr-range-lbl');
  if(rlblEl){
    if(!rangePts){     rlblEl.textContent=''; }
    else if(rangePct>=1.5){ rlblEl.textContent='▲ WIDE RANGE'; rlblEl.style.color='var(--bear)'; }
    else if(rangePct>=0.75){ rlblEl.textContent='◈ MODERATE'; rlblEl.style.color='var(--warn)'; }
    else {             rlblEl.textContent='▬ NARROW RANGE'; rlblEl.style.color='#64748b'; }
  }

  // ── Straddle card ─────────────────────────────────────────────────────────
  const strEl=$('mr-straddle');
  if(strEl){ strEl.textContent=straddle ? '₹'+straddle.toFixed(0) : '—'; strEl.style.color=straddle>200?'var(--bear)':straddle>120?'var(--warn)':'var(--txt)'; }
  const strSubEl=$('mr-straddle-sub');
  if(strSubEl) strSubEl.textContent=ceLtp&&peLtp ? `CE ₹${ceLtp.toFixed(0)} + PE ₹${peLtp.toFixed(0)}` : 'CE + PE LTP';
  const strLblEl=$('mr-straddle-lbl');
  if(strLblEl){
    if(!straddle){         strLblEl.textContent=''; }
    else if(straddle>200){ strLblEl.textContent='HIGH IV — costly premiums'; strLblEl.style.color='var(--bear)'; }
    else if(straddle>120){ strLblEl.textContent='NORMAL IV — fair pricing'; strLblEl.style.color='var(--warn)'; }
    else {                 strLblEl.textContent='LOW IV — cheap premiums'; strLblEl.style.color='#4ade80'; }
  }

  // ── Premium Activity card ─────────────────────────────────────────────────
  const pmEl=$('mr-prem-move');
  if(pmEl){
    if(!ceLtp && !peLtp){ pmEl.textContent='—'; pmEl.style.color='var(--dim)'; }
    else if(premCrush){        pmEl.textContent='▼ BOTH FALLING'; pmEl.style.color='var(--txt)'; }
    else if(totalPremMove>=8){ pmEl.textContent='⚡ HIGH ACTIVITY'; pmEl.style.color='var(--bull)'; }
    else if(totalPremMove>=3){ pmEl.textContent='◐ MODERATE';      pmEl.style.color='var(--warn)'; }
    else {                     pmEl.textContent='○ STAGNANT';      pmEl.style.color='var(--txt)'; }
  }
  const pmSubEl=$('mr-prem-sub');
  if(pmSubEl){
    pmSubEl.textContent=ceLtp ? `CE Δ${ceDelta>=0?'+':''}${ceDelta.toFixed(1)}  PE Δ${peDelta>=0?'+':''}${peDelta.toFixed(1)}` : 'OI PCR bot required';
    pmSubEl.style.color=ceLtp?'var(--dim)':'#ef4444';
  }
  const pmLblEl=$('mr-prem-lbl');
  if(pmLblEl){
    if(!ceLtp){          pmLblEl.textContent='start OI PCR bot'; pmLblEl.style.color='#ef4444'; }
    else if(premCrush){  pmLblEl.textContent='theta eating both sides'; pmLblEl.style.color='#94a3b8'; }
    else if(ceDelta<0 && peDelta>0){ pmLblEl.textContent='bias: BEARISH'; pmLblEl.style.color='var(--bear)'; }
    else if(ceDelta>0 && peDelta<0){ pmLblEl.textContent='bias: BULLISH'; pmLblEl.style.color='var(--bull)'; }
    else if(ceDelta>0 && peDelta>0){ pmLblEl.textContent='both rising — big move ahead'; pmLblEl.style.color='var(--warn)'; }
    else {               pmLblEl.textContent='no directional bias'; pmLblEl.style.color='#64748b'; }
  }

  // ── Regime Score card ─────────────────────────────────────────────────────
  // Dot bar: positions 1-2=sideways(slate), 3-4=mixed(yellow), 5-6=trending(green)
  const _segColors=['#64748b','#64748b','#eab308','#eab308','#4ade80','#4ade80'];
  document.querySelectorAll('.mrseg').forEach(seg=>{
    const pos=parseInt(seg.dataset.pos);
    seg.style.background = pos<=totalScore ? _segColors[pos-1] : '#1e293b';
  });
  const scEl=$('mr-score');
  const scoreWord=totalScore>=5?'TRENDING':totalScore>=3?'MIXED':'SIDEWAYS';
  const scoreColor=totalScore>=5?'#4ade80':totalScore>=3?'#eab308':'#94a3b8';
  if(scEl){ scEl.textContent=`${totalScore} pts — ${scoreWord}`; scEl.style.color=scoreColor; }
  const scBdEl=$('mr-score-breakdown');
  if(scBdEl) scBdEl.textContent=`Range ${rangeScore}/2 · Prem ${premScore}/2 · VIX ${vixScore}/2`;
  const scLblEl=$('mr-score-lbl');
  if(scLblEl){
    const lbl=totalScore>=5?'strong trend — ride with trail SL':totalScore>=4?'trending — momentum setups valid':totalScore>=3?'mixed — quick scalps only':totalScore>=2?'choppy — reduce size':'sideways — sit out or scalp only';
    scLblEl.textContent=lbl; scLblEl.style.color=totalScore>=4?'#4ade80':totalScore>=2?'#eab308':'#64748b';
  }

  const verdEl=$('mr-verdict'); if(verdEl) verdEl.textContent=verdictText;
}

// ── Market Regime fixed tooltip (position:fixed — never causes scroll jitter) ─
const _mrTips = {
  range: `<b style="font-size:11px;letter-spacing:.8px">NIFTY DAY RANGE</b>
<div style="color:#64748b;font-size:10px;margin:4px 0 8px">Today's high minus low — how far the index actually moved regardless of direction.</div>
<div style="display:grid;grid-template-columns:80px 1fr;gap:3px 8px;font-size:10px">
  <span style="color:var(--info)">&lt; 80 pts</span><span style="color:#94a3b8">NARROW — sideways / theta-decay day, avoid directional buys</span>
  <span style="color:var(--warn)">80–150 pts</span><span style="color:#94a3b8">MODERATE — some movement but no clear trend</span>
  <span style="color:var(--bear)">&gt; 150 pts</span><span style="color:#94a3b8">WIDE — trending or event-driven session, momentum valid</span>
</div>
<div style="margin-top:8px;font-size:9px;color:#475569">Source: Fibonacci bot (live) · Groww Quote API (60s fallback)</div>`,

  straddle: `<b style="font-size:11px;letter-spacing:.8px">ATM STRADDLE</b>
<div style="color:#64748b;font-size:10px;margin:4px 0 8px">ATM Call LTP + ATM Put LTP. What option writers are pricing in as the expected daily move.</div>
<div style="display:grid;grid-template-columns:80px 1fr;gap:3px 8px;font-size:10px">
  <span style="color:#4ade80">&lt; ₹100</span><span style="color:#94a3b8">LOW IV — premiums cheap, very quiet market</span>
  <span style="color:var(--warn)">₹100–₹200</span><span style="color:#94a3b8">NORMAL IV — standard pricing, fair to buy</span>
  <span style="color:var(--bear)">&gt; ₹200</span><span style="color:#94a3b8">HIGH IV — expensive, buying is uphill battle</span>
</div>
<div style="margin-top:8px;font-size:9px;color:#475569">Low straddle + wide range = theta eating buyers · High straddle + narrow range = writers winning</div>`,

  prem: `<b style="font-size:11px;letter-spacing:.8px">PREMIUM ACTIVITY</b>
<div style="color:#64748b;font-size:10px;margin:4px 0 8px">How much ATM CE and PE prices moved since the last OI update (~60s). Shows if real money is entering options.</div>
<div style="display:grid;grid-template-columns:100px 1fr;gap:3px 8px;font-size:10px">
  <span style="color:#94a3b8">STAGNANT</span><span style="color:#94a3b8">Total Δ &lt; 3 pts — no conviction, premiums flat</span>
  <span style="color:var(--warn)">MODERATE</span><span style="color:#94a3b8">Total Δ 3–8 pts — some directional flow</span>
  <span style="color:var(--bull)">HIGH ACTIVITY</span><span style="color:#94a3b8">Total Δ &gt; 8 pts — strong trend, follow the money</span>
  <span style="color:#94a3b8">BOTH FALLING</span><span style="color:#94a3b8">CE↓ + PE↓ = theta crush, stay out of buys</span>
</div>
<div style="margin-top:8px;font-size:9px;color:#475569">CE↓ PE↑ = bearish · CE↑ PE↓ = bullish · CE↑ PE↑ = big undirected move coming</div>`,

  score: `<b style="font-size:11px;letter-spacing:.8px">REGIME SCORE (0–6)</b>
<div style="color:#64748b;font-size:10px;margin:4px 0 8px">Composite of 3 signals (2 pts each): NIFTY range + ATM premium movement + VIX level/direction.</div>
<div style="display:grid;grid-template-columns:50px 1fr;gap:3px 8px;font-size:10px">
  <span style="color:#94a3b8">0–1</span><span style="color:#94a3b8">SIDEWAYS — sit out or very small scalps only</span>
  <span style="color:var(--warn)">2–3</span><span style="color:#94a3b8">MIXED / CHOPPY — quick scalps, tight targets, reduce size</span>
  <span style="color:#4ade80">4–5</span><span style="color:#94a3b8">TRENDING — standard momentum setups valid</span>
  <span style="color:var(--bull)">6</span><span style="color:#94a3b8">STRONG TREND — ride it, use trailing SL not fixed exit</span>
</div>
<div style="margin-top:8px;font-size:9px;color:#475569">Range 0–2 + Premium Δ 0–2 + VIX 0–2 = total</div>`
};

(function(){
  const tip=$('mr-tooltip');
  if(!tip) return;
  document.querySelectorAll('[data-mrtip]').forEach(el=>{
    el.addEventListener('mouseenter', e=>{
      const key=el.dataset.mrtip;
      if(!_mrTips[key]) return;
      tip.innerHTML=_mrTips[key];
      tip.style.display='block';
      _mrPositionTip(e);
    });
    el.addEventListener('mousemove', _mrPositionTip);
    el.addEventListener('mouseleave', ()=>{ tip.style.display='none'; });
  });
  function _mrPositionTip(e){
    const pad=12, W=tip.offsetWidth||320, H=tip.offsetHeight||160;
    let x=e.clientX+16, y=e.clientY+16;
    if(x+W > window.innerWidth-pad)  x=e.clientX-W-8;
    if(y+H > window.innerHeight-pad) y=e.clientY-H-8;
    tip.style.left=x+'px'; tip.style.top=y+'px';
  }
})();

// ─────────────────────────────────────────────────────────────────────────────

function _age(ts){
  if(!ts) return '—';
  const s = Math.max(0, (Date.now() - new Date(ts).getTime())/1000);
  if(s<60) return Math.round(s)+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  return Math.floor(s/3600)+'h ago';
}
function setText(id,v){ const e=$(id); if(e) e.textContent=v; }
function setBar(id,pct,cls){ const e=$(id); if(e){e.style.width=Math.min(pct,100)+'%'; e.className='score-bar '+cls;} }

/* ── Global fixed-position tooltip (bypasses all CSS stacking context issues) ── */
let _gTip = null;
function _ensureGTip(){
  if(_gTip) return;
  _gTip = document.createElement('div');
  _gTip.style.cssText = [
    'position:fixed','z-index:99999','display:none','pointer-events:none',
    'min-width:260px','max-width:380px','padding:10px 13px','border-radius:8px',
    'font-size:11px','line-height:1.7','font-family:Inter,sans-serif',
    'background:#0c1a30','border:1px solid #1c2d48',
    'box-shadow:0 12px 40px rgba(0,0,0,.85)',
    'color:#e2e8f0','white-space:normal'
  ].join(';');
  document.body.appendChild(_gTip);
}
function showFibTip(el){
  _ensureGTip();
  const n    = parseInt(el.dataset.fstars)||0;
  const tags = el.dataset.ftags||'';
  const filled='★'.repeat(Math.min(n,10));
  const empty ='☆'.repeat(Math.max(0,5-Math.min(n,10)));
  const meanings=['','Weak — single source.','Moderate — 2 sources agree.',
    'Good — 3 sources confluent. Likely reaction zone.',
    'Strong — 4 sources. High-probability S/R level.',
    'Very Strong — 5+ sources. Major level. Almost always reacts.'];
  const meaning = meanings[Math.min(n,5)]||meanings[5];
  const tagDesc = {'R23.6':'23.6% retrace','R38.2':'38.2% retrace','R50.0':'50% midpoint',
    'R61.8':'61.8% golden ratio','R78.6':'78.6% deep retrace',
    'E127.2':'127.2% extension','E161.8':'161.8% extension','E261.8':'261.8% extension',
    'SWING_HIGH':'15M swing high','SWING_LOW':'15M swing low',
    'DAY_HIGH':'day high','DAY_LOW':'day low'};
  const detail = tags.split(',').map(t=>{
    const k=t.trim().toUpperCase().replace(/\s+/g,'_');
    const d=Object.entries(tagDesc).find(([key])=>k.includes(key));
    return `<span style="color:#38bdf8">${t.trim()}</span>${d?' <span style="color:#5a7298">('+d[1]+')</span>':''}`;
  }).join(' · ');
  _gTip.innerHTML=`<div style="color:#ffd700;font-size:10px;letter-spacing:1px;font-weight:700;margin-bottom:6px;text-transform:uppercase">${filled}${empty} — ${n} Star${n!==1?'s':''}</div><div style="color:#e2e8f0;font-size:11px;margin-bottom:8px">${meaning}</div><div style="border-top:1px solid #1c2d48;padding-top:6px;font-size:10px;line-height:1.8">${detail}</div>`;
  _gTip.style.display='block';
  const r=el.getBoundingClientRect();
  const tw=_gTip.offsetWidth||280, th=_gTip.offsetHeight||100;
  let top=r.top-th-6; if(top<4) top=r.bottom+6;
  let left=r.left; if(left+tw>window.innerWidth-8) left=window.innerWidth-tw-8; if(left<4) left=4;
  _gTip.style.top=top+'px'; _gTip.style.left=left+'px';
}
function hideFibTip(){ if(_gTip) _gTip.style.display='none'; }

/* ── OI Intelligence tooltip engine ── */
const OI_TIPS = (function(){
  const T = (title,body,blink)=>`
    <div style="color:#ffd700;font-size:10px;letter-spacing:1px;font-weight:700;margin-bottom:7px;text-transform:uppercase">${title}</div>
    ${body}
    ${blink?`<div style="margin-top:8px;padding-top:6px;border-top:1px solid #1c2d48;color:#f59e0b;font-size:10px">⚠️ ${blink}</div>`:''}`;
  const R = (range,txt)=>`<div style="display:flex;gap:8px;margin:2px 0;align-items:baseline"><span style="color:#38bdf8;font-family:monospace;min-width:80px;flex-shrink:0">${range}</span><span style="color:#94a3b8;font-size:10.5px">${txt}</span></div>`;
  const P = txt=>`<div style="color:#cbd5e1;font-size:11px;margin-bottom:6px;line-height:1.6">${txt}</div>`;
  return {
    pcr_all: T('PCR — All Strikes',
      P('Put-Call Ratio = Total PE OI ÷ Total CE OI across every strike.<br>High PCR means more puts outstanding → institutions hedging → <b>supports the market rising</b> (put writers provide a floor).')+
      R('>1.5','🟢 Strong Bullish — heavy put writing floor')+
      R('1.2–1.5','🟢 Bullish — market well supported')+
      R('0.8–1.2','🟡 Neutral — balanced')+
      R('<0.8','🔴 Bearish — CE OI dominant')+
      R('<0.6','🔴 Very Bearish — severe call dominance'),
      'Blinks when PCR > 1.5 (extreme bullish) or < 0.6 (extreme bearish) — act immediately'),

    pcr_atm: T('PCR — ATM ±3 Strikes Only',
      P('Same formula but restricted to the 6 strikes nearest the spot price.<br>ATM strikes have the most liquidity — this is where institutions actively trade. <b>More responsive than all-strike PCR</b> and changes intraday as spot moves.')+
      R('>1.2','More puts near ATM → support floor is close')+
      R('<0.8','More calls near ATM → resistance wall is close'),
      'Blinks when diverging from PCR ALL by >0.3 — suggests spot is near a key level'),

    oi_sentiment: T('OI Sentiment',
      P('Derived from the <b>session change</b> in CE vs PE OI — what institutions added or removed today (not all-time).')+
      R('BULLISH','More PE than CE added this session → put writers building floor')+
      R('BEARISH','More CE than PE added → call writers building ceiling')+
      R('NEUTRAL','Balanced session activity'),
      ''),

    writer_bias: T('Writer Bias (Tick-Over-Tick)',
      P("Compares this tick's CE and PE OI to the <b>previous tick</b> (60 sec ago) — not the session open.<br>Catches momentum shifts that slow-moving sentiment misses.")+
      R('CE WRITING','CE OI ticking up → resistance being built → favours PE buyers')+
      R('PE WRITING','PE OI ticking up → support being built → favours CE buyers')+
      R('NEUTRAL','No dominant tick activity'),
      'Blinks on strong one-sided bias'),

    total_ce_oi: T('Total Call OI (CE)',
      P('Sum of all outstanding Call option contracts across every strike.<br>High CE OI = big resistance overhead. The session Δ below shows whether call writers are <b>adding</b> (more resistance) or <b>closing</b> (resistance weakening).')+
      P('🔴 Rising CE OI with falling market = shorts adding → stay bearish<br>🟢 Falling CE OI with rising market = short covering → rally may extend'),
      ''),

    total_pe_oi: T('Total Put OI (PE)',
      P('Sum of all outstanding Put option contracts across every strike.<br>High PE OI = strong support below. Session Δ shows whether support is being <b>built</b> or <b>unwound</b>.')+
      P('🟢 Rising PE OI with rising market = longs adding → trend intact<br>🔴 Falling PE OI with falling market = longs giving up → trend reversing'),
      ''),

    max_pain: T('Max Pain Strike',
      P('The strike where <b>option writers lose the least money</b> if the index expires here. Calculated by summing the total payout of all options at every possible expiry strike.')+
      P('Theory: since writers are well-capitalised institutions, they can nudge markets. Spot tends to <b>drift toward max pain</b> as expiry approaches — more relevant on expiry day and the day before.')+
      R('Spot > Max Pain','Market above max pain — bearish pull (writers want it lower)')+
      R('Spot < Max Pain','Market below max pain — bullish pull (writers want it higher)'),
      'Blinks when spot is within 30 pts of max pain — expiry gravitational pull strong'),

    vol_pcr: T('Volume PCR (Intraday)',
      P('<b>PE Volume ÷ CE Volume</b> traded today — uses actual contracts bought/sold, not OI.<br>Faster and more current than OI PCR. Shows what traders are <b>buying right now</b>.')+
      R('>1.2','Put buyers active — fear or downside bet')+
      R('0.9–1.2','Balanced buying')+
      R('<0.8','Call buyers dominant — bullish')+
      P('<i>Divergence: Vol PCR bearish but OI PCR bullish = retail fear but institutions holding floor — possible bounce.</i>'),
      ''),

    resistance_wall: T('Resistance Wall 🔴',
      P("Strike with the <b>highest total Call OI</b>. This is where the largest concentration of call sellers (writers) have positioned — they collect premium by betting the market won't cross this strike.")+
      P('Why it matters: A cluster of call writers actively defend this level. If spot tries to breach, they sell more calls to push it back. <b>Sell CEs or buy PEs near this level.</b>')+
      P('🔥 <b>BREAKOUT CONFIRMED</b> = spot crossed above this strike AND volume at that strike is ≥1.5× the average per-strike CE volume — real money pushing through. Strong upside momentum. Consider BUY CE.')+
      P('⚡ <b>TENTATIVE breach</b> = spot above the level but volume is not yet elevated — could be a fake-out, wait for confirmation.'),
      'Blinks orange on confirmed high-volume breakout · bear blink when spot within 50 pts'),

    support_floor: T('Support Floor 🟢',
      P("Strike with the <b>highest total Put OI</b>. Put writers (sellers) are heavily positioned here — they profit as long as the market stays above this level and actively defend it.")+
      P('Why it matters: Put writers will buy the market at dips near this level to avoid losses. <b>Buy CEs or sell PEs near this level.</b>')+
      P('🔻 <b>BREAKDOWN CONFIRMED</b> = spot fell below this strike AND volume at that strike is ≥1.5× the average per-strike PE volume — real selling pressure. Sharp downside likely. Consider BUY PE.')+
      P('⚡ <b>TENTATIVE break</b> = spot below the level but volume not elevated — could bounce back, watch for follow-through.'),
      'Blinks orange on confirmed high-volume breakdown · bull blink when spot within 50 pts'),

    oi_range_band: T('OI Range Band',
      P('Visual bracket showing where the <b>strongest support</b> (peak PE OI) and <b>strongest resistance</b> (peak CE OI) sit relative to the current spot price.')+
      P('The spot marker ▼ shows where you are in the range:')+
      R('Near left (support)','Spot close to floor — safer to buy CE')+
      R('Near right (resist.)','Spot close to ceiling — safer to buy PE')+
      R('Middle',"No-man's land — wait for breakout direction")+
      P('Top CE/PE lists show the 3 strongest strikes with ★ strength rating.'),
      ''),

    market_signal: T('Market Direction Signal (10-Factor)',
      P('Synthesises <b>10 independent OI signals</b> into a single bull/bear score.')+
      R('STRONG BULLISH','Bull ≥70 — multiple factors aligned bullish')+
      R('BULLISH','Bull ≥45 and > Bear+10')+
      R('NEUTRAL','Neither side dominant')+
      R('BEARISH','Bear ≥45 and > Bull+10')+
      R('STRONG BEARISH','Bear ≥70 — major downside pressure')+
      P('Factors: PCR · OI Imbalance · Session OI Δ · Writer tick · Smart Money · Vol PCR · Max Pain · IV Skew · Price Buildup · Volume Spike'),
      'Blinks on STRONG BULLISH or STRONG BEARISH — high-conviction signal'),

    bull_score: T('Bull Score (0–100)',
      P('Composite bullish score from all 10 factors. Each factor contributes proportionally based on how strongly bullish the signal is.')+
      R('≥70','Strong bullish confidence')+
      R('45–70','Moderate bullish lean')+
      R('<30','Weak — no clear bullish case')+
      P('When Bull Score >> Bear Score: bulls in control. When both high (>50): volatile, mixed signals.'),
      ''),

    bear_score: T('Bear Score (0–100)',
      P('Mirror of Bull Score — higher values mean stronger bearish pressure from OI data.')+
      R('≥70','Strong bearish dominance')+
      R('45–70','Moderate bearish lean')+
      R('<30','Bears not in control')+
      P('Trade the DIFFERENCE: if Bull=65 and Bear=30 → clear bull case. If Bull=55 and Bear=50 → confused market.'),
      ''),

    momentum_score: T('OI Momentum Score (0–100)',
      P('Rate at which <b>new OI is being added</b> relative to total OI — how fast institutions are building positions.')+
      R('≥60','High activity — directional move likely soon')+
      R('30–60','Moderate building')+
      R('<30','Low momentum — sideways/wait')+
      P('Best used with Market Signal: Strong Bullish + High Momentum = strong buy CE signal.'),
      ''),

    signal_breakdown: T('Signal Breakdown — 10 Factors',
      P("Each chip shows one factor's contribution. Format: <b>[direction +pts] Factor name</b>")+
      R('[BULL +N]','N points added to bull score from this factor')+
      R('[BEAR +N]','N points added to bear score')+
      P('Factors are independent — green (bull) and red (bear) chips can coexist. The net determines the final signal.')+
      P('Hover individual chips for factor-specific explanation.'),
      ''),

    smart_money_ce: T('Smart Money — CE OI Additions',
      P('Top 5 strikes where <b>Call OI increased most this session</b> — fresh call selling by institutions.')+
      P("These are the resistance levels being actively reinforced TODAY. Fresh CE writing = institution betting market won't cross this strike.")+
      P('💡 Trade: The #1 strike (largest addition) is the most defended ceiling → <b>BUY PE at that strike or below</b>, or sell CE at/above it.')+
      P('SESSION OI CHG = contracts added since market open, not tick-by-tick.'),
      'Blinks when top CE addition > 10L contracts — major resistance being built'),

    smart_money_pe: T('Smart Money — PE OI Additions',
      P('Top 5 strikes where <b>Put OI increased most this session</b> — institutions building fresh support floors.')+
      P("Fresh PE writing = institution betting market won't fall below this strike. They will buy the dip here.")+
      P('💡 Trade: #1 PE addition strike = strongest support → <b>BUY CE near this strike on dips</b>.')+
      P('High PE addition + rising spot = strong bull confirmation.'),
      'Blinks when top PE addition > 10L contracts — major support being built'),

    call_writing: T('Call Writing Detection',
      P('<b>CE OI↑ + CE LTP↓</b> = someone is SELLING (writing) call options at this strike.')+
      P('Call writers pocket premium and want market to stay below this strike. More writing = stronger resistance.')+
      R('CONFIRMED','Both OI rising AND LTP falling in same tick — high confidence write')+
      R('OTM','Strike above spot — very common, mild resistance')+
      R('ITM',"Strike below spot — strong signal, writer is very confident market won't rally")+
      P('🔴 Multiple confirmed writes at same strike = do NOT buy CE above that level.'),
      'Blinks when CONFIRMED writing detected — resistance actively being reinforced'),

    put_writing: T('Put Writing Detection',
      P('<b>PE OI↑ + PE LTP↓</b> = someone is SELLING (writing) put options — building a support floor.')+
      P('Put writers profit as long as market stays above that strike — they defend it aggressively.')+
      R('CONFIRMED','Both OI rising AND LTP falling — high confidence write')+
      R('OTM','Strike below spot — common hedging, moderate support')+
      R('ITM','Strike above spot — very strong signal')+
      P('🟢 Confirmed put writing = buy CE dips near that strike — supported.'),
      'Blinks when CONFIRMED writing detected — support floor actively reinforced'),

    atm_momentum: T('ATM Momentum Signal — BUY NOW?',
      P('Scores the <b>ATM strike only</b> for real directional conviction by combining LTP change + OI change for both CE and PE in the same tick.')+
      R('LONG BUILDUP at ATM','CE/PE price up + OI up = new money entering = conviction signal')+
      R('SHORT COVERING at ATM','Price up + OI down = old shorts closing = quick spike, not sustained')+
      R('CE score ≥60','🚀 BUY CE NOW — CE momentum confirmed at ATM')+
      R('PE score ≥60','🔻 BUY PE NOW — PE momentum confirmed at ATM')+
      R('< 60','⏳ WAIT — not enough conviction yet')+
      P('Target = +6% from ATM LTP. Stop = -3% from ATM LTP.'),
      'Blinks when BUY CE or BUY PE NOW signal fires — immediate action opportunity'),

    strike_buildup: T('Per-Strike Buildup (ATM ±3)',
      P("Classifies each ATM-area strike's CE and PE separately by comparing <b>LTP and OI from the previous tick to this tick</b>.")+
      R('LONG BUILDUP','Price↑ + OI↑ = new longs opening, bullish for that option')+
      R('SHORT BUILDUP','Price↓ + OI↑ = fresh shorts, bearish for that option')+
      R('SHORT COVERING','Price↑ + OI↓ = old shorts closing — quick rally, weak')+
      R('LONG UNWINDING','Price↓ + OI↓ = bulls exiting — trend exhaustion')+
      P('<b>CE LONG BUILDUP</b> = buyers of calls entering = bullish market view<br><b>PE LONG BUILDUP</b> = buyers of puts entering = bearish market view'),
      ''),

    iv_changes: T('IV Change Spikes',
      P('Detects <b>sudden Implied Volatility jumps > 1.5% in one tick</b> at any strike. IV spikes indicate urgent option buying — someone knows something or fears an event.')+
      R('PE IV spike ↑','Fear buying — traders scrambling for downside protection → BEARISH')+
      R('PE IV cooling ↓','Fear fading — sellers returning → BULLISH')+
      R('CE IV spike ↑','Call buying / short covering rush → BULLISH')+
      R('CE IV cooling ↓','Call rally fading → BEARISH')+
      P('ATM IV spikes matter most. OTM spikes can be noise.'),
      'Blinks when ATM IV spikes — potential fast move imminent'),

    pcr_change: T('PCR Change (Tick Delta)',
      P('<b>How PCR moved since the last 60-second tick.</b> Not just the level — the rate of change.')+
      R('Δ > +0.05','Put writing accelerating → bullish MOMENTUM building')+
      R('-0.05 to +0.05','Steady — no shift in institutional positioning')+
      R('Δ < -0.05','Puts being shed → bullish momentum fading or unwinding')+
      P('Rising PCR + bullish signal = double confirmation. Falling PCR + bearish signal = double confirmation.'),
      'Blinks when |Δ| > 0.10 — significant tick-shift in institutional position'),

    writer_activity: T('Writer Activity (Tick-Level OI Δ)',
      P("Compares each strike's OI from the previous tick to find who is <b>actively writing options right now</b>.")+
      R('CE OI rising','Call writers adding → resistance building → favour PE buyers')+
      R('PE OI rising','Put writers adding → support building → favour CE buyers')+
      P('Bullish/Bearish score = how many strikes show each type of writing activity. High bullish score = broad-based support writing across many strikes.'),
      ''),

    atm_oi_table: T('ATM ±3 Strike OI Breakdown',
      P('Shows all strikes within 3 steps of ATM with their key metrics for both CE (calls) and PE (puts).')+
      R('CE OI','Call open interest at this strike — higher = stronger resistance')+
      R('PE OI','Put open interest — higher = stronger support')+
      R('PE-CE DIFF +','PE dominant — support floor here')+
      R('PE-CE DIFF −','CE dominant — resistance ceiling here')+
      R('CE/PE IV%','Implied volatility — spike means urgent buying')+
      R('CE/PE LTP ₹','Last traded price of the option')+
      P('ATM row (←ATM) highlighted in blue — most active strike.'),
      ''),

    oi_history: T('OI Tick History',
      P('Every 60-second snapshot from this session. Newest row on top.')+
      R('CE Δ','Change in total call OI vs previous tick — rising = more resistance')+
      R('PE Δ','Change in total put OI vs previous tick — rising = more support')+
      R('SIGNAL','10-factor combined direction for that tick')+
      R('🟢/🔴','Bull score / Bear score for that tick')+
      P('Look for consistent SIGNAL direction across ticks — confirms a trend. Flip from BULLISH to BEARISH = watch out.'),
      ''),

    sm_session_chg: T('Session OI Change',
      P('<b>Contracts added at this strike since market open today</b> (not tick-by-tick).')+
      P('Large positive value = institution has been steadily building this position all session → high conviction level.')+
      P('The #1 entry (most OI added) is the freshest, highest-conviction institutional bet of the day.'),
      ''),
  };
})();

function showOITip(el){
  _ensureGTip();
  const key = el.dataset.oiTip;
  if(!key || !OI_TIPS[key]) return;
  _gTip.innerHTML = OI_TIPS[key];
  _gTip.style.display = 'block';
  const r=el.getBoundingClientRect();
  const tw=_gTip.offsetWidth||300, th=_gTip.offsetHeight||130;
  let top=r.top-th-8; if(top<4) top=r.bottom+8;
  let left=r.left; if(left+tw>window.innerWidth-8) left=window.innerWidth-tw-8; if(left<4) left=4;
  _gTip.style.top=top+'px'; _gTip.style.left=left+'px';
}
function hideOITip(){ if(_gTip) _gTip.style.display='none'; }
/* delegate hover on all [data-oi-tip] elements inside the OI tab */
document.addEventListener('mouseover', function(e){
  const el = e.target.closest('[data-oi-tip]');
  if(el){ showOITip(el); }
  else if(!e.target.closest('[data-fstars]')){ hideOITip(); }
});

/* ── Trade Board ── */
let _tbSym='',_tbExch='NSE',_tbDir='',_tbStrike=0,_tbLogTimer=null,_tbPaper=false,_tbAtr=false,_tbMock=false,_tbValidate=false,_tbClickTs=0,_tbExpiry='',_tbQuickPts=1.5,_tbAtrSource='candle',_tbPartial=false,_tbPartialPct=50;
let _tbQuickTargetMode='points';  // 'points' | 'profit' — QK TGT input interpretation
let _mbValidate=true;   // Auto bot: validate_orders — ON by default (matches CONFIG default)
let _mbChopEnabled=true; // Auto bot: choppiness_enabled — ON by default
let _mbConsSL=true;      // Auto bot: consec_sl_brake — ON by default
let _mbAtrSL=false;          // Auto bot: HARD_SL_ATR_BASED — OFF by default
let _mbAtrSource='candle';   // Auto bot: atr_source — "candle" (PROD10 EMA) or "scan" (window range)
let _mbMinScoreFilter=true;  // Auto bot: min_score_filter — ON by default
let _mbVelFilter=true;      // Auto bot: velocity_filter — ON by default
let _mbVixAutoConfig=false;  // VIX Auto Config — computes vel%/cons% from India VIX level+trend
let _mbVixPanelExpanded=true; // VIX panel detail body open/closed
// safe ms-precision timestamp formatter (toLocaleTimeString fractionalSecondDigits not supported in all browsers)
const _fmtTs=ts=>{if(!ts)return'—';const d=new Date(ts);return`${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}.${String(d.getMilliseconds()).padStart(3,'0')}`;};
const _fmtExpiry=e=>{if(!e)return'';const d=new Date(e+'T00:00:00');const m=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];return`${String(d.getDate()).padStart(2,'0')}${m[d.getMonth()]}${String(d.getFullYear()).slice(2)}`;}
let _tbLotSize=75;      // updated from live chain response
let _tbLotsLocked=false;
let _tbGreeks=false; // false = LTP only, true = full chain (OI/VOL/IV)
let _tbPrevUnr=null; // for trend arrow (↑↓) detection
let _tbQuickTradeMode=false;  // Quick Trade Mode: premium filter + instant buy
let _tbCapitalSource='api';   // 'api' | 'manual'
let _tbAvailableCapital=0;    // total capital to trade with
let _tbMaxPremium=0;          // max affordable premium per share (floor(capital/qty))
let _tbQuickRefreshTimer=null; // 5s chain refresh timer when Quick Trade is ON
let _tbSelectedLtp=0;         // LTP of selected strike (from chain data) — passed to bot to skip LTP fetch

function initTradeTab(){
  if(!$('tb-expiry').options.length) tbLoadExpiries();
  if(!$('mb-expiry').options.length) mbLoadExpiries();
  tbRestoreState();
  initTbResizer();
  tbRenderChainHeaders();
  tbRestoreLots();
  tbRestoreCfg();
  mbUpdateLotInfo();
  // Attach lots save listener once (via addEventListener, never in HTML attribute)
  const lotsEl=$('tb-lots');
  if(lotsEl && !lotsEl._tbLockAttached){
    lotsEl.addEventListener('input', function(){
      if(_tbLotsLocked) localStorage.setItem('tb_lots_locked_val', this.value);
    });
    lotsEl._tbLockAttached = true;
  }
}

function tbToggleLots(){
  _tbLotsLocked = !_tbLotsLocked;
  const btn=$('tb-lots-lock');
  if(btn){
    btn.textContent  = _tbLotsLocked ? '🔒' : '🔓';
    btn.style.opacity= _tbLotsLocked ? '1' : '.45';
    btn.title = _tbLotsLocked ? 'Lots LOCKED — persists across trades & refreshes' : 'Lock lots value';
  }
  if(_tbLotsLocked){
    localStorage.setItem('tb_lots_locked', '1');
    const v=$('tb-lots'); if(v) localStorage.setItem('tb_lots_locked_val', v.value);
  } else {
    localStorage.removeItem('tb_lots_locked');
    localStorage.removeItem('tb_lots_locked_val');
  }
  tbUpdateLotInfo();
}

function tbRestoreLots(){
  if(localStorage.getItem('tb_lots_locked') !== '1') return;
  _tbLotsLocked = true;
  const saved = localStorage.getItem('tb_lots_locked_val');
  if(saved){ const el=$('tb-lots'); if(el) el.value=saved; }
  const btn=$('tb-lots-lock');
  if(btn){ btn.textContent='🔒'; btn.style.opacity='1'; btn.title='Lots LOCKED — persists across trades & refreshes'; }
  tbUpdateLotInfo();
}

function initTbResizer(){
  const handle = $('tb-drag-handle');
  const right  = document.querySelector('.tb-right-panel');
  if(!handle || !right) return;
  // Restore saved width
  const saved = localStorage.getItem('tb_right_w');
  if(saved) right.style.width = saved + 'px';
  let dragging = false, startX = 0, startW = 0;
  handle.addEventListener('mousedown', e=>{
    dragging=true; startX=e.clientX; startW=right.offsetWidth;
    document.body.style.cursor='col-resize'; e.preventDefault();
  });
  document.addEventListener('mousemove', e=>{
    if(!dragging) return;
    const newW = Math.max(240, Math.min(window.innerWidth - 150, startW - (e.clientX - startX)));
    right.style.width = newW + 'px';
  });
  document.addEventListener('mouseup', ()=>{
    if(dragging){
      dragging=false; document.body.style.cursor='';
      localStorage.setItem('tb_right_w', parseInt(right.style.width||360));
    }
  });
}

async function tbRestoreState(){
  const r=await fetch('/api/trade/status'); const t=await r.json();
  tbRenderHistory(t.history||[]);
  tbStartLogPoll();
}

async function tbLoadExpiries(){
  const idx=$('tb-index').value;
  const r=await fetch(`/api/trade/expiries?index=${idx}`);
  const d=await r.json();
  const sel=$('tb-expiry');
  sel.innerHTML=(d.expiries||[]).map(e=>`<option value="${e}">${e}</option>`).join('');
  if(d.expiries&&d.expiries.length) tbLoadChain();
}

let _tbChainData  = [];   // full strike list from last fetch
let _tbChainSpot  = 0;
let _tbPrevLTPs   = {};   // for flash animation (tick-to-tick)
let _tbCloseLTPs  = {};   // prev-day close: {ce23500: 85, pe23500: 120, ...}
let _tbChainTimer = null;
let _tbLtpSyms    = [];   // [{k, ce, pe}] — exchange symbols for visible strikes (ltp_batch)
// Rate budget: 10/sec, 300/min
// LTP batch: 2 API calls per refresh (batch of 50 each covers ~80 visible strikes)
// No trade:     2 calls × (1/0.3s) = 6.67/sec, 240/min  ✅
// Active trade: trail 5/sec + 2 calls × (1/0.5s) = 9/sec, 540/min (burst OK, stays under 10/sec)
// ±12 strikes=48 syms → 1 LTP call/refresh. Trail 0.25s=240/min. Chain 150/min. Total ~404/min+bots. Per-sec≤6.7✅
const _TB_CHAIN_REFRESH_MS        = 400;   // 1 call × 150/min = 150/min (no trade)
const _TB_CHAIN_REFRESH_ACTIVE_MS = 600;   // 1 call × 100/min = 100/min (trail 240+chain 100+bots 64 = 404/min)

function tbRenderChainHeaders(){
  const el = $('chain-col-hdr'); if(!el) return;
  el.innerHTML = `<div class="tb-chain-minimal">
    <span class="tc-lbl" style="text-align:center;color:var(--bull);font-weight:700">▼ CE</span>
    <span class="tc-lbl" style="text-align:center;font-weight:700;color:var(--info)">STRIKE</span>
    <span class="tc-lbl" style="text-align:center;color:var(--bear);font-weight:700">PE ▼</span>
  </div>`;
}

function tbUpdateLotInfo(_skipRender){
  const e=$('chain-lotinfo');
  if(!e) return;
  const lots=parseInt($('tb-lots').value||1);
  e.textContent=`Lot size: ${_tbLotSize} · ${lots} lot${lots>1?'s':''} = ${lots*_tbLotSize} qty`;
  tbUpdateQuickTargetHint();
  if(!_skipRender&&_tbQuickTradeMode&&_tbAvailableCapital>0){ tbComputeMaxPremium(); tbUpdateMaxPremiumDisplay(); tbRenderChain(); }
}

function mbUpdateLotInfo(){
  const e=$('mb-lotinfo');
  if(!e) return;
  const idx = ($('mb-index')||{}).value || 'NIFTY';
  const lots = parseInt(($('mb-lots')||{}).value) || 1;
  const _defaults = {NIFTY:75,BANKNIFTY:15,FINNIFTY:40,MIDCPNIFTY:75,SENSEX:10,BANKEX:15};
  const lotSize = _mbLotSizeMap[idx] || _defaults[idx] || 75;
  e.textContent = `Auto: Lot size: ${lotSize} · ${lots} lot${lots>1?'s':''} = ${lots*lotSize} qty`;
}

function _fmtOI(n){ if(!n&&n!==0) return '—'; const a=Math.abs(n),s=n<0?'-':''; if(a>=1e7) return s+(a/1e7).toFixed(2)+'Cr'; return s+(a/1e5).toFixed(2)+'L'; }
function _fmtChg(ltp,prev){ if(!prev||!ltp) return {html:'',cls:'flat'}; const d=ltp-prev; if(Math.abs(d)<0.01) return {html:'',cls:'flat'}; const p=(d/prev*100); const cls=d>0?'up':d<0?'dn':'flat'; const s=(d>0?'+':'')+d.toFixed(2)+' ('+(p>0?'+':'')+p.toFixed(1)+'%)'; return {html:s,cls}; }

async function tbFetchPrevClose(){
  const syms=_tbLtpSyms.flatMap(({ce,pe})=>[ce,pe].filter(Boolean));
  if(!syms.length) return;
  try{
    const r=await fetch(`/api/trade/chain_quotes?s=${encodeURIComponent(syms.join(','))}`);
    const d=await r.json();
    if(!d.prev_close) return;
    // populate _tbCloseLTPs from quote response
    _tbLtpSyms.forEach(({k,ce,pe})=>{
      if(ce&&d.prev_close[ce]) _tbCloseLTPs['ce'+k]=d.prev_close[ce];
      if(pe&&d.prev_close[pe]) _tbCloseLTPs['pe'+k]=d.prev_close[pe];
    });
    // refresh all visible change spans with current LTPs
    _tbLtpSyms.forEach(({k})=>{
      const ceLtp=_tbPrevLTPs['ce'+k]||0; const peLtp=_tbPrevLTPs['pe'+k]||0;
      const ceEl=$('ceCHG'+k); const peEl=$('peCHG'+k);
      if(ceEl&&ceLtp&&_tbCloseLTPs['ce'+k]){const c=_fmtChg(ceLtp,_tbCloseLTPs['ce'+k]);ceEl.textContent=c.html;ceEl.className='tc-chg '+c.cls;}
      if(peEl&&peLtp&&_tbCloseLTPs['pe'+k]){const c=_fmtChg(peLtp,_tbCloseLTPs['pe'+k]);peEl.textContent=c.html;peEl.className='tc-chg '+c.cls;}
    });
  }catch(e){}
}

async function tbLoadChain(_skipLoading){
  const idx=$('tb-index').value, expiry=$('tb-expiry').value;
  if(!expiry) return;
  // Save scroll position before re-render so auto-refresh doesn't jump user back to ATM
  const _chainEl=$('chain-list');
  const _savedScroll=(_skipLoading && _chainEl) ? _chainEl.scrollTop : -1;
  if(!_skipLoading)
    _chainEl.innerHTML='<div style="text-align:center;color:var(--dim);padding:30px">Loading option chain…</div>';
  const r=await fetch(`/api/trade/chain?index=${idx}&expiry=${expiry}`);
  const d=await r.json();
  if(d.error&&!d.strikes?.length){
    _chainEl.innerHTML=`<div style="color:var(--warn);padding:12px">⚠ ${d.error}</div>`; return;
  }
  // Expiry exists in CSV but Groww returned no strikes (e.g. non-Thursday date for NIFTY)
  if(!d.strikes?.length){
    const sel=$('tb-expiry');
    if(sel.selectedIndex+1 < sel.options.length){
      sel.selectedIndex++;   // jump to next expiry automatically
      tbLoadChain(true); return;
    }
    _chainEl.innerHTML='<div style="color:var(--warn);padding:12px">⚠ No strikes available for this expiry</div>';
    return;
  }
  _tbChainData = d.strikes||[];
  _tbChainSpot = d.spot||0;
  _tbLotSize   = d.lot_size||75;
  _tbPrevLTPs  = {};
  _tbCloseLTPs = {};
  (_tbChainData).forEach(s=>{ const k=Math.round(s.strike); if(s.ce_prev>0) _tbCloseLTPs['ce'+k]=s.ce_prev; if(s.pe_prev>0) _tbCloseLTPs['pe'+k]=s.pe_prev; });
  tbUpdateLotInfo(true);  // skip inner tbRenderChain — we call it below with the saved scroll
  tbRenderChain(_savedScroll);  // pass saved scroll: ≥0 = restore, -1 = initial → scroll to ATM
  tbStartChainRefresh();
}

function tbRenderChain(_savedScroll){
  if(_savedScroll===undefined) _savedScroll=-1;
  const idx    = $('tb-index')?.value||'NIFTY';
  const exch   = (idx==='SENSEX'||idx==='BANKEX')?'BSE':'NSE';
  const spot   = _tbChainSpot;
  const strikes= _tbChainData;
  if(!strikes.length) return;

  $('chain-spot').textContent = spot ? 'SPOT  ₹'+fmtN(spot,2) : '';
  tbRenderChainHeaders();

  let atmIdx=0; let minDiff=Infinity;
  strikes.forEach((s,i)=>{ const df=Math.abs(s.strike-spot); if(df<minDiff){minDiff=df;atmIdx=i;} });
  const step = strikes.length>1 ? strikes[1].strike-strikes[0].strike : 50;
  const fr=Math.max(0,atmIdx-12); const to=Math.min(strikes.length,atmIdx+13);

  $('chain-list').innerHTML = strikes.slice(fr,to).map(s=>{
    const isATM  = Math.abs(s.strike-spot)<step/2;
    const isITMce= s.strike<spot;
    const isITMpe= s.strike>spot;
    const rowCls = isATM?'atm-row':isITMce?'itm-ce':isITMpe?'itm-pe':'';
    const stk    = Math.round(s.strike);
    const atmTag = isATM?'<span style="font-size:8px;color:var(--info);margin-left:3px;font-weight:700">ATM</span>':'';
    // Quick Trade: live LTP first, fall back to prev close for premium comparison
    const ceLtp = (s.ce_ltp && s.ce_ltp>0) ? s.ce_ltp : (s.ce_prev||0);
    const peLtp = (s.pe_ltp && s.pe_ltp>0) ? s.pe_ltp : (s.pe_prev||0);
    const qtOn  = _tbQuickTradeMode && _tbMaxPremium>0;
    const ceOverBudget = qtOn && ceLtp>0 && ceLtp>_tbMaxPremium;
    const peOverBudget = qtOn && peLtp>0 && peLtp>_tbMaxPremium;
    const ceDisabled = (!s.ce_sym || ceOverBudget) ? 'disabled' : '';
    const peDisabled = (!s.pe_sym || peOverBudget) ? 'disabled' : '';
    const ceCls = 'chain-btn ce'+(ceOverBudget?' qt-over-budget':'');
    const peCls = 'chain-btn pe'+(peOverBudget?' qt-over-budget':'');
    const cePremLabel = _tbQuickTradeMode && ceLtp>0 ? ` <span style="font-size:8px;opacity:.65;font-family:'JetBrains Mono',monospace">₹${ceLtp}</span>` : '';
    const pePremLabel = _tbQuickTradeMode && peLtp>0 ? ` <span style="font-size:8px;opacity:.65;font-family:'JetBrains Mono',monospace">₹${peLtp}</span>` : '';
    const ceTitle = ceOverBudget ? ` title="₹${ceLtp} > max ₹${_tbMaxPremium} — over budget"` : '';
    const peTitle = peOverBudget ? ` title="₹${peLtp} > max ₹${_tbMaxPremium} — over budget"` : '';
    return `<div class="tb-row ${rowCls}">
      <div class="tb-chain-minimal">
        <span style="text-align:center;padding:2px 6px"><button class="${ceCls}" onclick="tbSelect('${s.ce_sym}','${exch}','CE',${stk})" ${ceDisabled}${ceTitle}>BUY CE${cePremLabel}</button></span>
        <span class="tc-strike" style="text-align:center;color:${isATM?'var(--info)':'var(--txt)'};font-size:13px">${fmtN(s.strike,0)}${atmTag}</span>
        <span style="text-align:center;padding:2px 6px"><button class="${peCls}" onclick="tbSelect('${s.pe_sym}','${exch}','PE',${stk})" ${peDisabled}${peTitle}>BUY PE${pePremLabel}</button></span>
      </div>
    </div>`;
  }).join('');

  if(_savedScroll>=0){
    // Auto-refresh: restore user's scroll position — do NOT snap back to ATM
    const _cl=$('chain-list');
    if(_cl) _cl.scrollTop=_savedScroll;
  } else {
    // Initial load: scroll ATM row into view
    setTimeout(()=>{
      const atm=document.querySelector('#chain-list .atm-row');
      if(atm) atm.scrollIntoView({block:'center',behavior:'smooth'});
    }, 60);
  }

  _tbLtpSyms = [];  // no LTP polling needed
}

function tbUpdateChainLTPs(){}

function _isMarketOpen(){
  const n=new Date(); const h=n.getHours(); const m=n.getMinutes();
  const dayOk=n.getDay()>=1&&n.getDay()<=5;
  const timeOk=(h>9||(h===9&&m>=15))&&(h<15||(h===15&&m<=30));
  return dayOk&&timeOk;
}

function tbStartChainRefresh(){
  if(_tbChainTimer) clearInterval(_tbChainTimer);
  const badge=$('chain-refresh-badge');
  if(!_isMarketOpen()){
    if(badge) badge.style.display='none';
    return;
  }
  if(badge){
    badge.style.display='inline';
    badge.style.background='rgba(0,229,160,.1)';badge.style.color='var(--bull)';badge.style.border='1px solid var(--bull)';
    badge.textContent='● LIVE';
  }
  // No LTP polling — chain is static; all API budget goes to trail loop
}

function tbSelect(sym,exch,dir,strike){
  _tbSym=sym; _tbExch=exch; _tbDir=dir; _tbStrike=strike||0;
  _tbExpiry=$('tb-expiry')?.value||'';
  // Capture LTP from chain data so bot can skip a redundant LTP API call
  const _chainEntry=_tbChainData.find(s=>Math.round(s.strike)===strike);
  _tbSelectedLtp=_chainEntry
    ? (dir==='CE' ? (_chainEntry.ce_ltp||_chainEntry.ce_prev||0) : (_chainEntry.pe_ltp||_chainEntry.pe_prev||0))
    : 0;
  $('tb-sym-inp').value=sym; $('tb-exch-inp').value=exch;
  $('tb-selected-display').innerHTML=`Selected: <span style="font-family:'JetBrains Mono',monospace;font-weight:700;color:${dir==='CE'?'var(--bull)':'var(--bear)'}">${sym}</span>`;
  const expiryLabel=_fmtExpiry(_tbExpiry);
  const btn=$('tb-p10-btn');
  if(btn){
    btn.disabled=false; btn.style.opacity='1';
    btn.textContent=`BUY ${dir} ${strike}  ${expiryLabel} → PROD10`;
    btn.style.background=dir==='CE'
      ? 'linear-gradient(135deg,var(--buy-ce-dark),var(--buy-ce))'
      : 'linear-gradient(135deg,var(--buy-pe-dark),var(--buy-pe))';
    btn.style.color=dir==='CE'?'#000':'#fff';
  }
  // Quick Trade Mode: skip confirmation, fire immediately
  if(_tbQuickTradeMode){ tbSendToProd10(); }
}

async function tbSendToProd10(){
  if(!_tbSym || !_tbStrike){ alert('Select a strike first'); return; }
  // Always read ALL config fresh from DOM — no cached variables — so changing any value
  // in the UI takes effect on the very next trade without restarting anything.
  const lots        = parseInt($('tb-lots')?.value||'1') || 1;
  const mode        = $('tb-p10-mode')?.value || 'manual';
  const expiry      = $('tb-expiry')?.value || '';
  const index       = $('tb-index')?.value  || 'NIFTY';
  // QK TGT resolves to premium points whether the input is in PTS or ₹ profit mode
  const quick_pts   = tbQuickTargetPoints() || 1.5;
  const partial_pct = parseInt($('tb-partial-pct')?.value||'50') || 50;
  // sync cached vars so they stay consistent with what we're about to send
  _tbQuickPts = quick_pts; _tbPartialPct = partial_pct;
  _tbClickTs   = Date.now();
  const clickTimeStr = _fmtTs(_tbClickTs);
  // immediately log the click time in the session log
  const logEl=$('tb-log');
  if(logEl){
    const row=document.createElement('div');
    row.style.cssText='color:var(--info);padding:2px 4px;border-bottom:1px solid rgba(255,255,255,.03);font-weight:600';
    row.textContent=`[${clickTimeStr}] 🖱️ Dashboard → PROD10: ${index} ${_tbStrike}${_tbDir} ×${lots}lots (${mode}${mode==='quick'?` tgt+${quick_pts}pt`:''}${_tbPaper?' PAPER':''}${_tbMock?' MOCK RUN':''}${_tbValidate?' VALIDATE':''})`;
    logEl.appendChild(row);
    logEl.scrollTop=logEl.scrollHeight;
  }
  const btn=$('tb-p10-btn');
  btn.disabled=true; btn.textContent='⏳ Sending to PROD10…';
  try{
    const r=await fetch('/api/prod10_buy',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({index,expiry,strike:_tbStrike,opt_type:_tbDir,lots,mode,paper:_tbPaper,atr:_tbAtr,atr_source:_tbAtrSource,mock:_tbMock,validate_orders:_tbValidate,quick_pts,partial:_tbPartial,partial_pct,ltp:_tbSelectedLtp})});
    const d=await r.json();
    if(d.ok){
      const orig=`BUY ${_tbDir} ${_tbStrike} → PROD10`;
      btn.textContent=`✅ Sent: ${d.command}`;
      btn.style.background='linear-gradient(135deg,#16a34a,#22c55e)';
      btn.style.color='#fff';
      setTimeout(()=>{
        btn.style.background=_tbDir==='CE'
          ?'linear-gradient(135deg,var(--buy-ce-dark),var(--buy-ce))'
          :'linear-gradient(135deg,var(--buy-pe-dark),var(--buy-pe))';
        btn.style.color=_tbDir==='CE'?'#000':'#fff';
        btn.disabled=false; btn.textContent=orig;
      },3000);
    } else {
      alert('PROD10 error: '+(d.error||'unknown'));
      btn.disabled=false; btn.textContent=`BUY ${_tbDir} ${_tbStrike} → PROD10`;
    }
  }catch(e){
    alert('Failed: '+e);
    btn.disabled=false; btn.textContent=`BUY ${_tbDir} ${_tbStrike} → PROD10`;
  }
}


function tbRenderHistory(history){
  const tbody=$('th-body'); if(!tbody) return;
  const count=$('th-count'); const pnlEl=$('th-session-pnl');
  if(!history.length){
    tbody.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:14px;font-size:10px">No trades yet this session</td></tr>';
    if(count) count.textContent='';
    if(pnlEl) pnlEl.textContent='';
    return;
  }
  const sessionPnl = history.reduce((s,h)=>s+h.pnl,0);
  const sign = sessionPnl>=0?'+':'';
  if(count) count.textContent=`(${history.length})`;
  if(pnlEl){
    pnlEl.textContent=`Session: ${sign}₹${Math.abs(sessionPnl).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
    pnlEl.style.color=sessionPnl>=0?'var(--bull)':'var(--bear)';
  }
  // newest trade first
  tbody.innerHTML=[...history].reverse().map(h=>{
    const dir=h.direction||'—';
    const pnlSign=h.pnl>=0?'+':'';
    const pnlClr=h.pnl>=0?'var(--bull)':'var(--bear)';
    const dirClr=dir==='CE'?'var(--bull)':dir==='PE'?'var(--bear)':'var(--dim)';
    // show only last part of symbol after stripping date prefix (e.g. NIFTY2660923500CE → 23500CE)
    const shortSym = h.symbol.replace(/^[A-Z]+\d{5}/,'') || h.symbol;
    const paper = h.paper?'<span style="color:var(--warn);font-size:8px"> P</span>':'';
    return `<tr class="${h.paper?'th-paper':''}">
      <td style="padding:3px 6px;color:var(--dim)">${h.exit_ts}</td>
      <td style="padding:3px 4px"><span style="color:${dirClr};font-weight:700">${shortSym}</span>${paper}</td>
      <td style="padding:3px 4px;text-align:right;color:var(--txt)">₹${h.buy.toFixed(2)}</td>
      <td style="padding:3px 4px;text-align:right;color:var(--txt)">₹${h.sell.toFixed(2)}</td>
      <td style="padding:3px 4px;text-align:right;color:var(--dim)">${h.qty}</td>
      <td style="padding:3px 6px;text-align:right;color:${pnlClr};font-weight:700">${pnlSign}₹${Math.abs(h.pnl).toFixed(2)}</td>
    </tr>`;
  }).join('');
}

function fmtN(n,dec=0){ return new Intl.NumberFormat('en-IN',{minimumFractionDigits:dec,maximumFractionDigits:dec}).format(n||0); }

function tbTogglePaper(){
  _tbPaper=!_tbPaper;
  const btn=$('tb-paper-btn');
  btn.textContent=_tbPaper?'ON':'OFF';
  btn.className=`toggle-btn ${_tbPaper?'toggle-on':'toggle-off'}`;
  tbSaveCfg();
}

function tbToggleAtr(){
  _tbAtr=!_tbAtr;
  const btn=$('tb-atr-btn');
  btn.textContent=_tbAtr?'ON':'OFF';
  btn.className=`toggle-btn ${_tbAtr?'toggle-on':'toggle-off'}`;
  _tbSyncAtrSrcBtn();
  tbSaveCfg();
}

function tbTogglePartial(){
  _tbPartial=!_tbPartial;
  const btn=$('tb-partial-btn');
  btn.textContent=_tbPartial?'ON':'OFF';
  btn.className=`toggle-btn ${_tbPartial?'toggle-on':'toggle-off'}`;
  tbSaveCfg();
}

function _tbSyncAtrSrcBtn(){
  const srcBtn=$('tb-atr-src-btn');
  if(!srcBtn) return;
  if(_tbAtr){
    const isCandle=_tbAtrSource==='candle';
    srcBtn.style.borderColor=isCandle?'#f59e0b':'#a78bfa';
    srcBtn.style.background =isCandle?'rgba(245,158,11,.15)':'rgba(167,139,250,.15)';
    srcBtn.style.color      =isCandle?'#f59e0b':'#a78bfa';
    srcBtn.style.cursor     ='pointer';
    srcBtn.style.opacity    ='1';
    srcBtn.title=isCandle
      ?'Currently: HIST ATR — 14-period EMA ATR from 1-min candles (accurate, no 3-pt floor). Click to switch to TICK RNG.'
      :'Currently: TICK RNG — 8-sec live tick range × multiplier (fast, 3-pt floor). Click to switch to HIST ATR.';
  } else {
    srcBtn.style.borderColor='#374151';
    srcBtn.style.background ='rgba(55,65,81,.15)';
    srcBtn.style.color      ='#4b5563';
    srcBtn.style.cursor     ='not-allowed';
    srcBtn.style.opacity    ='0.45';
    srcBtn.title='Disabled — turn ATR-SL ON first.';
  }
}

function tbToggleAtrSource(){
  if(!_tbAtr) return;
  _tbAtrSource=(_tbAtrSource==='candle')?'scan':'candle';
  const btn=$('tb-atr-src-btn');
  btn.textContent=_tbAtrSource==='candle'?'HIST ATR':'TICK RNG';
  _tbSyncAtrSrcBtn();
  tbSaveCfg();
}

function tbToggleMock(){
  _tbMock=!_tbMock;
  const btn=$('tb-mock-btn');
  btn.textContent=_tbMock?'ON':'OFF';
  btn.className=`toggle-btn ${_tbMock?'toggle-on':'toggle-off'}`;
  btn.style.borderColor=_tbMock?'var(--warn)':'';
  if(_tbMock) btn.style.background='rgba(234,179,8,.15)';
  else btn.style.background='';
  tbSaveCfg();
}

function tbToggleValidate(){
  _tbValidate=!_tbValidate;
  const btn=$('tb-validate-btn');
  btn.textContent=_tbValidate?'ON':'OFF';
  btn.className=`toggle-btn ${_tbValidate?'toggle-on':'toggle-off'}`;
  btn.style.borderColor='#4ade80';
  btn.style.background=_tbValidate?'rgba(74,222,128,.15)':'';
  btn.style.color=_tbValidate?'#4ade80':'';
  tbSaveCfg();
}

function tbOnModeChange(){
  const mode=$('tb-p10-mode')?.value||'manual';
  const isQ=mode==='quick';
  const qkGrp=$('tb-quick-pts-grp');   if(qkGrp) qkGrp.style.display=isQ?'':'none';
  const pGrp=$('tb-partial-grp');      if(pGrp)  pGrp.style.display=isQ?'':'none';
  tbSaveCfg();
}

// Effective quantity for the current QK TGT config (lots × lot size for the selected index).
function tbQuickQty(){
  const lots=parseInt($('tb-lots')?.value||'1')||1;
  return lots*(_tbLotSize||75);
}

// Resolve the QK TGT input to premium points, regardless of PTS/₹ mode.
// In profit mode: points = profit / qty, snapped to the nearest 5 paise so the
// resulting limit price lands on a valid tick (the bot re-snaps on the real fill).
function tbQuickTargetPoints(){
  const raw=parseFloat($('tb-quick-pts')?.value||'0')||0;
  if(_tbQuickTargetMode==='profit'){
    const qty=tbQuickQty();
    if(qty<=0||raw<=0) return 0;
    return Math.round((raw/qty)/0.05)*0.05;
  }
  return raw;
}

// Toggle the QK TGT input between points (PTS) and profit amount (₹).
function tbToggleQuickTargetMode(){
  _tbQuickTargetMode=(_tbQuickTargetMode==='profit')?'points':'profit';
  const inp=$('tb-quick-pts');
  const btn=$('tb-quick-mode-btn');
  if(_tbQuickTargetMode==='profit'){
    if(btn) btn.textContent='₹';
    if(inp){ inp.min='100'; inp.max='500000'; inp.step='100'; inp.value='2000'; inp.style.width='72px'; }
  } else {
    if(btn) btn.textContent='PTS';
    if(inp){ inp.min='0.5'; inp.max='20'; inp.step='0.5'; inp.value='1.5'; inp.style.width='56px'; }
  }
  tbSaveCfg();
  tbUpdateQuickTargetHint();
}

// Show the conversion the other way so the trader always sees both numbers.
function tbUpdateQuickTargetHint(){
  const el=$('tb-quick-tgt-hint'); if(!el) return;
  const qty=tbQuickQty();
  if(_tbQuickTargetMode==='profit'){
    const pts=tbQuickTargetPoints();
    el.textContent=pts>0?`≈ ${pts.toFixed(2)} pts · ${qty} qty`:'';
  } else {
    const pts=parseFloat($('tb-quick-pts')?.value||'0')||0;
    const profit=pts*qty;
    el.textContent=profit>0?`≈ ₹${profit.toLocaleString('en-IN')} · ${qty} qty`:'';
  }
}

// ── Quick Trade Mode ──────────────────────────────────────────────────────────

function tbToggleQuickTrade(){
  _tbQuickTradeMode=!_tbQuickTradeMode;
  const btn=$('tb-quick-trade-btn');
  const row=$('tb-quick-trade-row');
  if(btn){
    btn.textContent=_tbQuickTradeMode?'⚡ Quick: ON':'⚡ Quick Trade';
    btn.className=`toggle-btn ${_tbQuickTradeMode?'toggle-on':'toggle-off'}`;
    btn.style.borderColor='#f59e0b';
    btn.style.background=_tbQuickTradeMode?'rgba(245,158,11,.2)':'';
    btn.style.color=_tbQuickTradeMode?'#f59e0b':'';
  }
  if(row) row.style.display=_tbQuickTradeMode?'':'none';
  if(_tbQuickTradeMode){
    _tbQuickRefreshStart();
  } else {
    _tbQuickRefreshStop();
    _tbAvailableCapital=0; _tbMaxPremium=0;
    const badge=$('chain-qt-badge'); if(badge) badge.style.display='none';
    tbUpdateMaxPremiumDisplay();
  }
  tbRenderChain();
}

function _tbQuickRefreshStart(){
  _tbQuickRefreshStop();
  _tbQuickRefreshTimer=setInterval(async()=>{
    if(!_tbQuickTradeMode){ _tbQuickRefreshStop(); return; }
    const expiry=$('tb-expiry')?.value;
    if(!expiry) return;
    await tbLoadChain(true);  // silent — no spinner, just updates data + re-renders chain
    if(_tbAvailableCapital>0){ tbComputeMaxPremium(); tbUpdateMaxPremiumDisplay(); }
  }, 5000);
}

function _tbQuickRefreshStop(){
  if(_tbQuickRefreshTimer){ clearInterval(_tbQuickRefreshTimer); _tbQuickRefreshTimer=null; }
}

function tbOnCapSourceChange(){
  _tbCapitalSource=$('tb-cap-source')?.value||'api';
  const fg=$('tb-cap-fetch-grp'), mg=$('tb-cap-manual-grp');
  if(fg) fg.style.display=_tbCapitalSource==='api'?'':'none';
  if(mg) mg.style.display=_tbCapitalSource==='manual'?'':'none';
  _tbAvailableCapital=0; _tbMaxPremium=0;
  tbUpdateMaxPremiumDisplay();
  tbRenderChain();
}

async function tbFetchCapital(){
  const btn=$('tb-cap-fetch-btn');
  const orig=btn.textContent;
  btn.textContent='⏳ Fetching…'; btn.disabled=true;
  try{
    const r=await fetch('/api/data'); const d=await r.json();
    const mg=d.margin||{};
    const cap=parseFloat(mg.opt_buy_avail||0);
    if(cap<=0){
      alert('F&O Buy Balance is ₹0 or unavailable. Use "Pass Manually" or check your Groww account.');
      btn.textContent=orig; btn.disabled=false; return;
    }
    _tbAvailableCapital=cap;
    tbComputeMaxPremium();
    tbUpdateMaxPremiumDisplay();
    tbRenderChain();
    btn.textContent=`✅ ₹${Math.round(cap).toLocaleString('en-IN')}`;
    setTimeout(()=>{ btn.textContent=orig; btn.disabled=false; },4000);
  }catch(e){ alert('Failed to fetch capital: '+e); btn.textContent=orig; btn.disabled=false; }
}

function tbOnManualCapital(){
  const val=parseFloat($('tb-cap-manual')?.value||'0');
  _tbAvailableCapital=isNaN(val)||val<=0?0:val;
  tbComputeMaxPremium();
  tbUpdateMaxPremiumDisplay();
  tbRenderChain();
}

function tbComputeMaxPremium(){
  const lots=parseInt($('tb-lots')?.value||'1');
  const qty=lots*(_tbLotSize||75);
  _tbMaxPremium=qty>0&&_tbAvailableCapital>0?Math.floor(_tbAvailableCapital/qty):0;
}

function tbUpdateMaxPremiumDisplay(){
  const capGrp=$('tb-cap-display-grp'), maxGrp=$('tb-max-prem-grp'), calcGrp=$('tb-max-prem-calc-grp');
  const capDisp=$('tb-cap-display'), maxDisp=$('tb-max-prem-display'), calcDisp=$('tb-max-prem-calc');
  const badge=$('chain-qt-badge');
  const lots=parseInt($('tb-lots')?.value||'1');
  const qty=lots*(_tbLotSize||75);
  if(_tbAvailableCapital>0 && _tbQuickTradeMode){
    if(capGrp) capGrp.style.display='';
    if(capDisp) capDisp.textContent='₹'+Math.round(_tbAvailableCapital).toLocaleString('en-IN');
    if(maxGrp) maxGrp.style.display='';
    if(maxDisp) maxDisp.textContent=_tbMaxPremium>0?'≤ ₹'+_tbMaxPremium:'—';
    if(calcGrp) calcGrp.style.display='';
    if(calcDisp) calcDisp.textContent=`${lots}L × ${_tbLotSize||75} = ${qty} qty`;
    // chain header badge
    if(badge){
      badge.style.display=_tbMaxPremium>0?'':'none';
      badge.textContent=_tbMaxPremium>0?`⚡ MAX ₹${_tbMaxPremium}  (${qty} qty)`:'';
    }
  } else {
    if(capGrp) capGrp.style.display='none';
    if(maxGrp) maxGrp.style.display='none';
    if(calcGrp) calcGrp.style.display='none';
    if(badge) badge.style.display='none';
  }
}

function tbSaveCfg(){
  const mode=$('tb-p10-mode')?.value||'manual';
  const qraw=parseFloat($('tb-quick-pts')?.value||'0');      // raw field value (points or ₹, per mode)
  _tbQuickPts=tbQuickTargetPoints()||1.5;                    // cached resolved points
  try{
    const ppct=parseInt($('tb-partial-pct')?.value||'50');
    _tbPartialPct=isNaN(ppct)?50:ppct;
    localStorage.setItem('tb_cfg',JSON.stringify({
      mode, paper:_tbPaper, atr:_tbAtr, atr_source:_tbAtrSource, mock:_tbMock, validate:_tbValidate,
      quick_pts:isNaN(qraw)?1.5:qraw, quick_target_mode:_tbQuickTargetMode,
      partial:_tbPartial, partial_pct:_tbPartialPct
    }));
  }catch(e){}
}

function tbRestoreCfg(){
  try{
    const s=localStorage.getItem('tb_cfg'); if(!s) return;
    const c=JSON.parse(s);
    // mode
    const modeEl=$('tb-p10-mode');
    if(modeEl && c.mode){ modeEl.value=c.mode; tbOnModeChange(); }
    // quick target mode (PTS / ₹) — apply attrs/button before restoring the field value
    _tbQuickTargetMode=(c.quick_target_mode==='profit')?'profit':'points';
    const qEl=$('tb-quick-pts');
    const qBtn=$('tb-quick-mode-btn');
    if(_tbQuickTargetMode==='profit'){
      if(qBtn) qBtn.textContent='₹';
      if(qEl){ qEl.min='100'; qEl.max='500000'; qEl.step='100'; qEl.style.width='72px'; }
    } else {
      if(qBtn) qBtn.textContent='PTS';
      if(qEl){ qEl.min='0.5'; qEl.max='20'; qEl.step='0.5'; qEl.style.width='56px'; }
    }
    if(qEl && c.quick_pts!=null){ qEl.value=c.quick_pts; }
    _tbQuickPts=tbQuickTargetPoints()||1.5;
    tbUpdateQuickTargetHint();
    // paper
    if(c.paper){ _tbPaper=true; const b=$('tb-paper-btn'); if(b){b.textContent='ON';b.className='toggle-btn toggle-on';} }
    // atr
    if(c.atr){ _tbAtr=true; const b=$('tb-atr-btn'); if(b){b.textContent='ON';b.className='toggle-btn toggle-on';} }
    // atr source
    if(c.atr_source){ _tbAtrSource=c.atr_source; const b=$('tb-atr-src-btn'); if(b) b.textContent=_tbAtrSource==='candle'?'HIST ATR':'TICK RNG'; }
    _tbSyncAtrSrcBtn();
    // partial
    if(c.partial){ _tbPartial=true; const b=$('tb-partial-btn'); if(b){b.textContent='ON';b.className='toggle-btn toggle-on';} }
    if(c.partial_pct!=null){ _tbPartialPct=c.partial_pct; const e=$('tb-partial-pct'); if(e) e.value=c.partial_pct; }
    // mock
    if(c.mock){ _tbMock=true; const b=$('tb-mock-btn'); if(b){b.textContent='ON';b.className='toggle-btn toggle-off';b.style.borderColor='var(--warn)';b.style.background='rgba(234,179,8,.15)';} }
    // validate
    if(c.validate){ _tbValidate=true; const b=$('tb-validate-btn'); if(b){b.textContent='ON';b.className='toggle-btn toggle-on';b.style.borderColor='#4ade80';b.style.background='rgba(74,222,128,.15)';b.style.color='#4ade80';} }
  }catch(e){}
}

async function tbUpdateQuickTarget(){
  const qpts=tbQuickTargetPoints();
  if(isNaN(qpts)||qpts<=0){alert('Enter a valid target value first.');return;}
  const btn=event.currentTarget;
  const orig=btn.textContent;
  btn.textContent='...'; btn.disabled=true;
  try{
    const r=await fetch('/api/prod10_set_target',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({quick_pts:qpts})});
    const j=await r.json();
    if(j.ok){btn.textContent='✓';btn.style.background='rgba(0,200,130,.35)';}
    else{btn.textContent='✗';alert('Failed: '+(j.error||'unknown'));}
  }catch(e){btn.textContent='✗';alert('Error: '+e);}
  setTimeout(()=>{btn.textContent=orig;btn.style.background='rgba(0,200,130,.12)';btn.disabled=false;},2000);
}

async function tbUpdatePartial(){
  const pct=parseInt($('tb-partial-pct')?.value||'50');
  if(isNaN(pct)||pct<10||pct>90){alert('Partial % must be between 10 and 90.');return;}
  const btn=event.currentTarget;
  const orig=btn.textContent;
  btn.textContent='...'; btn.disabled=true;
  try{
    const r=await fetch('/api/prod10_set_partial',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({partial:_tbPartial,partial_pct:pct})});
    const j=await r.json();
    if(j.ok){btn.textContent='✓';btn.style.background='rgba(0,200,130,.35)';}
    else{btn.textContent='✗';alert('Failed: '+(j.error||'unknown'));}
  }catch(e){btn.textContent='✗';alert('Error: '+e);}
  setTimeout(()=>{btn.textContent=orig;btn.style.background='rgba(0,200,130,.12)';btn.disabled=false;},2000);
}

function mbToggleValidate(){
  _mbValidate=!_mbValidate;
  const btn=$('mb-validate-btn');
  btn.textContent=_mbValidate?'ON':'OFF';
  btn.className=`toggle-btn ${_mbValidate?'toggle-on':'toggle-off'}`;
  btn.style.borderColor='#4ade80';
  btn.style.background=_mbValidate?'rgba(74,222,128,.15)':'';
  btn.style.color=_mbValidate?'#4ade80':'';
  _mbPushConfig();
}

function mbToggleChop(){
  _mbChopEnabled=!_mbChopEnabled;
  const btn=$('mb-chop-btn');
  btn.textContent=_mbChopEnabled?'ON':'OFF';
  btn.className=`toggle-btn ${_mbChopEnabled?'toggle-on':'toggle-off'}`;
  btn.style.borderColor='#4ade80';
  btn.style.background=_mbChopEnabled?'rgba(74,222,128,.15)':'';
  btn.style.color=_mbChopEnabled?'#4ade80':'';
  _mbPushConfig();
}

function mbToggleConsSL(){
  _mbConsSL=!_mbConsSL;
  const btn=$('mb-cons-sl-btn');
  btn.textContent=_mbConsSL?'ON':'OFF';
  btn.className=`toggle-btn ${_mbConsSL?'toggle-on':'toggle-off'}`;
  btn.style.borderColor='#4ade80';
  btn.style.background=_mbConsSL?'rgba(74,222,128,.15)':'';
  btn.style.color=_mbConsSL?'#4ade80':'';
  _mbPushConfig();
}

function _mbSyncAtrSrcBtn(){
  // Keep ATR SRC button grayed-out when ATR SL is OFF; restore color when ON
  const srcBtn=$('mb-atr-src-btn');
  if(!srcBtn) return;
  if(_mbAtrSL){
    const isCandle=_mbAtrSource==='candle';
    srcBtn.style.borderColor=isCandle?'#f59e0b':'#a78bfa';
    srcBtn.style.background =isCandle?'rgba(245,158,11,.15)':'rgba(167,139,250,.15)';
    srcBtn.style.color      =isCandle?'#f59e0b':'#a78bfa';
    srcBtn.style.cursor     ='pointer';
    srcBtn.style.opacity    ='1';
    srcBtn.title=isCandle
      ?'Currently: HIST ATR — 14-period EMA ATR from 1-min candles (accurate, no 3-pt floor). Click to switch to TICK RNG.'
      :'Currently: TICK RNG — scan-window tick range × multiplier (fast, floor 3 pts). Click to switch to HIST ATR.';
  } else {
    srcBtn.style.borderColor='#374151';
    srcBtn.style.background ='rgba(55,65,81,.15)';
    srcBtn.style.color      ='#4b5563';
    srcBtn.style.cursor     ='not-allowed';
    srcBtn.style.opacity    ='0.45';
    srcBtn.title='Disabled — turn ATR SL ON first. HIST ATR: 14-period EMA ATR from 1-min candles (accurate, no floor). TICK RNG: scan-window tick range × multiplier (fast, floor 3 pts).';
  }
}

function mbToggleAtrSL(){
  _mbAtrSL=!_mbAtrSL;
  const btn=$('mb-atr-sl-btn');
  btn.textContent=_mbAtrSL?'ON':'OFF';
  btn.className=`toggle-btn ${_mbAtrSL?'toggle-on':'toggle-off'}`;
  btn.style.borderColor='#4ade80';
  btn.style.background=_mbAtrSL?'rgba(74,222,128,.15)':'';
  btn.style.color=_mbAtrSL?'#4ade80':'';
  _mbSyncAtrSrcBtn();
  _mbPushConfig();
}

function mbToggleAtrSource(){
  if(!_mbAtrSL) return;  // only interactive when ATR SL is ON
  _mbAtrSource = (_mbAtrSource === 'candle') ? 'scan' : 'candle';
  const btn=$('mb-atr-src-btn');
  const isCandle = _mbAtrSource === 'candle';
  btn.textContent = isCandle ? 'HIST ATR' : 'TICK RNG';
  _mbSyncAtrSrcBtn();
  _mbPushConfig();
}

function mbToggleMinScore(){
  _mbMinScoreFilter=!_mbMinScoreFilter;
  const btn=$('mb-min-score-btn');
  btn.textContent=_mbMinScoreFilter?'ON':'OFF';
  btn.className=`toggle-btn ${_mbMinScoreFilter?'toggle-on':'toggle-off'}`;
  btn.style.borderColor='#4ade80';
  btn.style.background=_mbMinScoreFilter?'rgba(74,222,128,.15)':'';
  btn.style.color=_mbMinScoreFilter?'#4ade80':'';
  _mbPushConfig();
}

function mbToggleVelFilter(){
  _mbVelFilter=!_mbVelFilter;
  const btn=$('mb-vel-filter-btn');
  btn.textContent=_mbVelFilter?'ON':'OFF';
  btn.className=`toggle-btn ${_mbVelFilter?'toggle-on':'toggle-off'}`;
  btn.style.borderColor='#4ade80';
  btn.style.background=_mbVelFilter?'rgba(74,222,128,.15)':'';
  btn.style.color=_mbVelFilter?'#4ade80':'';
  _mbPushConfig();
}

// ── VIX Auto Config ─────────────────────────────────────────────────────────
function mbVixComputeConfig(vix, chgPct){
  // Returns rich config object based on VIX level + day-over-day % change
  // Derived from 5-day backtest (Jun 15-19 2026)
  if(vix == null || isNaN(vix)) return null;
  const chg = chgPct || 0;
  const falling = chg < 0;

  if(vix > 15){
    return {vel:1.5, cons:60, zone:'HIGH  (>15)', color:'#f87171',
      velWhy:'VIX is elevated — real intraday moves present. vel≥1.5% confirms genuine premium acceleration.',
      consWhy:'cons≥60% ensures at least 60% of ticks moved in the signal direction — directional conviction.',
      cautions:['High VIX means wide ATM spreads — slippage on entry/exit can be larger','Options premiums decay fast after spike — hold time matters more'],
      positives:['Strong trending moves are common on high-VIX days — momentum is real','Wider premium swings mean bigger per-lot profit when direction is right'],
      ref:'Historical: best results on VIX>15 days come from catching 1 clean directional move rather than 6 choppy ones.'};
  }
  if(vix >= 14){
    if(falling){
      return {vel:1.0, cons:70, zone:'MOD-HIGH  FALLING (14–15)', color:'#fb923c',
        velWhy:'VIX falling from moderate level — signals still have some strength, vel≥1.0% avoids very weak entries.',
        consWhy:'HIGH consistency≥70% is the key filter here — falling VIX compresses premiums, so only high-consistency directional moves are worth trading.',
        cautions:['Falling VIX = premiums shrinking — profits per trade will be smaller than yesterday','Avoid low-consistency signals even if velocity looks okay'],
        positives:['Market is calming down — less whipsaw risk','Moderate vol still allows good momentum entries if consistency is strong'],
        ref:'Jun 15 (VIX=14.24 falling): actual +₹30k → with cons≥70% filter → +₹60k (+₹29k saved). Consistency was the key lever.'};
    }
    return {vel:1.5, cons:60, zone:'MOD-HIGH  RISING (14–15)', color:'#f59e0b',
      velWhy:'VIX rising = real fear entering the market. vel≥1.5% ensures signal is driven by genuine panic/directional flow.',
      consWhy:'cons≥60% filters out momentary spikes that reverse quickly. Rising VIX still has noise.',
      cautions:['Rising VIX can cause sudden reversals mid-trade — watch trail SL closely','Multiple hard SL hits in a row signal choppy conditions — enable CONS SL brake'],
      positives:['Rising VIX creates strong one-directional momentum bursts','Premium expansion works in your favor on correct-side entries'],
      ref:'Moderate-high rising VIX days: standard vel+cons filter captures the best 30–40% of signals with positive PnL.'};
  }
  if(vix >= 13){
    if(chg < -3){
      return {vel:2.5, cons:60, zone:'⚠ MOD  SHARP DROP (13–14, chg<-3%)', color:'#ef4444',
        velWhy:'VIX dropping >3% in one day means gamma is being crushed and option premiums are collapsing. Only extreme velocity signals (≥2.5%) have real momentum behind them.',
        consWhy:'cons≥60% ensures the move is directional, not just noise from decaying premiums.',
        cautions:['🚨 DANGER ZONE — this is the Jun 16 pattern that caused -₹48,262','Majority of signals will be weak, low-velocity noise from premium decay','Expect only 2–5 tradeable signals all day — most should be skipped'],
        positives:['With vel≥2.5% filter: Jun 16 would have been +₹2,808 instead of -₹48k','Very selective entries on this day type can still be profitable'],
        ref:'Jun 16 (VIX=13.39, -6% drop): actual -₹48,262 (42 trades) → vel≥2.5%+cons≥60% → +₹2,808 (only 2 trades). Saved ₹51,070.'};
    }
    if(falling){
      return {vel:1.5, cons:60, zone:'MOD  DRIFTING (13–14, slowly falling)', color:'#f59e0b',
        velWhy:'VIX drifting down gently — market is slowly calming. vel≥1.5% filters out weak signals from decaying vol.',
        consWhy:'cons≥60% separates directional momentum from premium-decay noise on low-conviction days.',
        cautions:['Signals will look weaker than yesterday — average velocity will be lower','Be patient — wait for clean high-consistency signals, do not chase'],
        positives:['Gentle decline is more stable than sharp drop — fewer whipsaws than -3% VIX days','Filtered signals should perform reasonably on this day type'],
        ref:'Jun 17 (VIX=13.20, -1.4%): actual +₹3,383 (51 trades) → vel≥1.5%+cons≥60% → +₹12,119 (only 6 trades). Quality over quantity.'};
    }
    return {vel:1.8, cons:60, zone:'MOD  RISING (13–14)', color:'#facc15',
      velWhy:'VIX moderately rising — some real momentum exists but market is not yet in strong trending mode. vel≥1.8% filters low-quality marginal signals.',
      consWhy:'cons≥60% ensures the signal direction has real tick-level confirmation.',
      cautions:['Rising from a moderate base can stall quickly — trail SL tightly','Watch for OI data to confirm direction before entry'],
      positives:['Rising VIX creates real option premium expansion','Moderate level means less spread — execution quality is better'],
      ref:'Moderate rising VIX: expect 5–8 quality signals. First 2–3 hours tend to have the best momentum.'};
  }
  if(vix >= 12){
    if(falling){
      return {vel:1.0, cons:55, zone:'LOW  FALLING (12–13)', color:'#4ade80',
        velWhy:'VIX low and falling — market is calm and stable. vel≥1.0% allows more trades since stability means fewer false signals.',
        consWhy:'cons≥55% is the standard floor. Low VIX days can still be profitable with relaxed filters (Jun 18 proof).',
        cautions:['Premium ranges are small — profit per trade will be lower, do not over-expect','Very low VIX means option premiums move slowly — be patient on exits'],
        positives:['Stable market = fewer whipsaws = higher trade completion rate','Jun 18 (VIX=12.73 falling): vel≥0.5%+cons≥50% → +₹17,726 from just 10 trades'],
        ref:'Jun 18 (VIX=12.73, -3.6%): actual +₹3,861 (19 trades) → vel≥0.5%+cons≥50% → +₹17,726 (10 trades). Calm day, quality wins.'};
    }
    return {vel:1.8, cons:55, zone:'LOW  RISING (12–13)', color:'#60b8f0',
      velWhy:'VIX rising from a very low base can produce sudden sharp moves ("snap moves") as fear re-enters. vel≥1.8% targets these sharp entries only.',
      consWhy:'cons≥55% ensures the snap move has directional consistency across ticks — not just one noisy spike.',
      cautions:['⚠ Jun 19 pattern — rising VIX from low base also coincided with infrastructure failure and -₹68k loss','Snap moves can reverse sharply — keep trail SL tight, enable CHOP and CONS SL brake','Connection reliability matters more on volatile snap-move days'],
      positives:['Snap moves from low VIX base can be fast and profitable when caught correctly','vel≥1.8%+cons≥55% filter: Jun 19 would have been +₹8,658 instead of -₹68,854'],
      ref:'Jun 19 (VIX=12.78, +0.4% rising): actual -₹68,854 → vel≥1.8%+cons≥55% → +₹8,658 (3 trades). Saved ₹77,512 with correct config.'};
  }
  return {vel:2.5, cons:65, zone:'VERY LOW  (<12)', color:'#94a3b8',
    velWhy:'VIX below 12 means near-zero fear — option premiums are barely moving. Only extreme velocity signals (≥2.5%) indicate real momentum.',
    consWhy:'cons≥65% is the highest consistency bar — on a dead market day, only the very cleanest signals should be traded.',
    cautions:['🚨 Consider NOT trading today — very low VIX days have minimal premium movement','Risk-reward is poor: small moves, but SL hit = full loss','Most signals will not meet vel≥2.5% — expect 0–2 tradeable signals all session'],
    positives:['If a signal does meet vel≥2.5%+cons≥65%, it is likely a genuine breakout move','Rare signals on low-VIX days tend to be cleaner since they need strong conviction'],
    ref:'Very low VIX (<12): best strategy is to wait for a clear OI-confirmed direction and trade max 1–2 times with small size.'};
}

function mbVixTogglePanel(){
  _mbVixPanelExpanded = !_mbVixPanelExpanded;
  const body = $('mb-vix-panel-body');
  const chev = $('mb-vix-chevron');
  if(body) body.style.display = _mbVixPanelExpanded ? 'block' : 'none';
  if(chev) chev.style.transform = _mbVixPanelExpanded ? '' : 'rotate(180deg)';
}

function mbVixAutoToggle(){
  _mbVixAutoConfig = !_mbVixAutoConfig;
  const btn   = $('mb-vix-auto-btn');
  const rfBtn = $('mb-vix-refresh-btn');
  const panel = $('mb-vix-status-panel');
  if(btn){
    btn.textContent = _mbVixAutoConfig ? 'ON' : 'OFF';
    btn.className   = `toggle-btn ${_mbVixAutoConfig ? 'toggle-on' : 'toggle-off'}`;
    btn.style.borderColor = _mbVixAutoConfig ? '#60b8f0' : '';
    btn.style.background  = _mbVixAutoConfig ? 'rgba(96,184,240,.15)' : '';
    btn.style.color       = _mbVixAutoConfig ? '#60b8f0' : '';
  }
  if(rfBtn) rfBtn.style.display = _mbVixAutoConfig ? '' : 'none';
  if(panel) panel.style.display = _mbVixAutoConfig ? 'block' : 'none';
  if(!_mbVixAutoConfig){
    // Reset vel/cons badges to defaults when toggled OFF
    const vd=$('mb-vel-display'), cd=$('mb-cons-display');
    if(vd){ vd.textContent='0.5%'; vd.className='mb-vel-cons-val'; vd.title='velocity_pct threshold (default)'; }
    if(cd){ cd.textContent='55%';  cd.className='mb-vel-cons-val'; cd.title='consistency_pct threshold (default)'; }
  }
  if(_mbVixAutoConfig) mbVixRefreshConfig();
}

async function mbVixRefreshConfig(){
  const statusEl = $('mb-vix-status-text');
  if(statusEl) statusEl.textContent = '⏳ fetching VIX…';
  try{
    const r = await fetch('/api/data');
    const d = await r.json();
    const lv = (d.pnl_analysis && d.pnl_analysis.live) ? d.pnl_analysis.live : {};
    let vix    = lv.vix    != null ? lv.vix    : (d.vix_history && d.vix_history.length ? d.vix_history[d.vix_history.length-1].v : null);
    let vixChg = lv.vix_chg_pct != null ? lv.vix_chg_pct :
                 (d.vix_session_open && vix ? (vix - d.vix_session_open) / d.vix_session_open * 100 : null);
    // Fallback: use cached globals set by renderVix
    if(vix == null && window._mbVixCurrent != null){ vix = window._mbVixCurrent; vixChg = window._mbVixDayChg; }
    if(vix == null){ if(statusEl) statusEl.textContent = '⚠ VIX data unavailable — open dashboard to fetch'; return; }
    const cfg = mbVixComputeConfig(vix, vixChg);
    if(!cfg){ if(statusEl) statusEl.textContent = '⚠ Could not compute config'; return; }
    // Build note string logged by bot
    const chgStr = vixChg != null ? (vixChg>=0?'+':'')+vixChg.toFixed(1)+'%' : 'N/A';
    const note   = `VIX=${vix.toFixed(2)} (${chgStr}) Zone=${cfg.zone} → vel≥${cfg.vel}%  cons≥${cfg.cons}%  Reason: ${cfg.reason}`;
    // Push to override file (picked up by running bot on next scan cycle)
    await fetch('/api/momentum/config',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        velocity_pct:    cfg.vel,
        consistency_pct: cfg.cons,
        velocity_filter: true,
        min_score_filter: true,
        _vix_config_note: note
      })
    });
    // Sync filter toggles to ON (VIX auto always enables them)
    _mbVelFilter=true; _mbMinScoreFilter=true;
    ['mb-vel-filter-btn','mb-min-score-btn'].forEach(id=>{
      const b=$(id); if(!b) return;
      b.textContent='ON'; b.className='toggle-btn toggle-on';
      b.style.borderColor='#4ade80'; b.style.background='rgba(74,222,128,.15)'; b.style.color='#4ade80';
    });
    // Update vel% and cons% display badges
    const velDisp = $('mb-vel-display');
    const consDisp = $('mb-cons-display');
    if(velDisp){  velDisp.textContent = cfg.vel+'%';  velDisp.className='mb-vel-cons-val vix-set'; velDisp.title=`velocity_pct set by VIX AUTO (${cfg.zone})`; }
    if(consDisp){ consDisp.textContent = cfg.cons+'%'; consDisp.className='mb-vel-cons-val vix-set'; consDisp.title=`consistency_pct set by VIX AUTO (${cfg.zone})`; }
    // Update header summary (always visible even when collapsed)
    if(statusEl) statusEl.textContent = `VIX ${vix.toFixed(2)} (${chgStr}) · ${cfg.zone} · vel≥${cfg.vel}%  cons≥${cfg.cons}%`;
    // Update collapsible body — rich card
    const panelBody = $('mb-vix-panel-body');
    if(panelBody){
      const cautionRows = (cfg.cautions||[]).map(c=>`<div style="display:flex;gap:5px;margin:1px 0"><span style="color:#fbbf24">⚠</span><span>${c}</span></div>`).join('');
      const posRows     = (cfg.positives||[]).map(p=>`<div style="display:flex;gap:5px;margin:1px 0"><span style="color:#4ade80">✓</span><span>${p}</span></div>`).join('');
      panelBody.innerHTML = `
<div style="display:grid;grid-template-columns:auto 1fr;gap:0 18px;width:100%">
  <!-- Left column: VIX + config set -->
  <div style="min-width:220px">
    <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
      <span style="color:${cfg.color};font-size:16px;font-weight:800">${vix.toFixed(2)}</span>
      <span style="color:${(vixChg||0)>=0?'#f87171':'#4ade80'};font-size:11px;font-weight:700">${chgStr} day</span>
      <span style="color:var(--dim);font-size:9px">India VIX</span>
    </div>
    <div style="color:${cfg.color};font-size:10px;font-weight:700;letter-spacing:.4px;margin-bottom:8px">Zone: ${cfg.zone}</div>
    <div style="font-size:9px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase;margin-bottom:4px">Config Applied to Bot</div>
    <div style="display:flex;flex-direction:column;gap:3px">
      <div style="display:flex;gap:8px;align-items:center">
        <span style="color:var(--dim);width:96px">velocity_pct</span>
        <span style="color:#4ade80;font-weight:800;font-size:13px">≥ ${cfg.vel}%</span>
      </div>
      <div style="font-size:9px;color:var(--dim);margin-left:104px;margin-top:-2px;margin-bottom:3px">${cfg.velWhy}</div>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="color:var(--dim);width:96px">consistency_pct</span>
        <span style="color:#4ade80;font-weight:800;font-size:13px">≥ ${cfg.cons}%</span>
      </div>
      <div style="font-size:9px;color:var(--dim);margin-left:104px;margin-top:-2px;margin-bottom:3px">${cfg.consWhy}</div>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="color:var(--dim);width:96px">velocity_filter</span>
        <span style="color:#4ade80;font-weight:700;font-size:11px">ON</span>
        <span style="color:var(--dim);font-size:9px">gates entry on vel threshold</span>
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:1px">
        <span style="color:var(--dim);width:96px">min_score_filter</span>
        <span style="color:#4ade80;font-weight:700;font-size:11px">ON</span>
        <span style="color:var(--dim);font-size:9px">gates entry on score floor</span>
      </div>
    </div>
  </div>
  <!-- Right column: cautions + positives + ref -->
  <div style="border-left:1px solid rgba(96,184,240,.2);padding-left:14px;font-size:9px;line-height:1.5;color:var(--dim)">
    ${cautionRows ? `<div style="font-size:9px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase;margin-bottom:4px">Cautions</div>${cautionRows}<div style="height:6px"></div>` : ''}
    <div style="font-size:9px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase;margin-bottom:4px">Positives</div>
    ${posRows}
    ${cfg.ref ? `<div style="margin-top:8px;padding-top:6px;border-top:1px solid rgba(255,255,255,.06);color:#60b8f0;font-size:9px;line-height:1.6">📊 ${cfg.ref}</div>` : ''}
  </div>
</div>`;
      // Keep body expanded by default when refreshed
      panelBody.style.display = 'block';
      const chev = $('mb-vix-chevron');
      if(chev) chev.style.transform = '';
      _mbVixPanelExpanded = true;
    }
  }catch(e){
    if(statusEl) statusEl.textContent = '⚠ Error: '+e.message;
  }
}

function _mbPushConfig(){
  // Push current toggle state to running bot via override file (no-op if bot not running)
  fetch('/api/momentum/config',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      validate_orders:    _mbValidate,
      choppiness_enabled: _mbChopEnabled,
      consec_sl_brake:    _mbConsSL,
      HARD_SL_ATR_BASED:  _mbAtrSL,
      atr_source:         _mbAtrSource,
      min_score_filter:   _mbMinScoreFilter,
      velocity_filter:    _mbVelFilter
    })
  }).catch(()=>{});
}

async function tbStartProd10(){
  const btn=$('tb-start-p10-btn');
  btn.disabled=true; btn.textContent='⏳ Starting…';
  try{
    const r=await fetch('/api/start_prod10',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    if(d.ok){ btn.textContent='✅ Started'; setTimeout(()=>{ btn.disabled=false; btn.textContent='▶ Start PROD10'; },4000); }
    else{ alert('Failed: '+(d.error||'unknown')); btn.disabled=false; btn.textContent='▶ Start PROD10'; }
  }catch(e){ alert('Error: '+e); btn.disabled=false; btn.textContent='▶ Start PROD10'; }
}

async function tbStartAutoV2(){
  const btn=$('tb-auto-v2-btn');
  if(!btn) return;
  const paperLabel = _tbPaper ? ' [PAPER]' : ' [LIVE]';
  if(!_tbPaper){
    if(!confirm(`Launch AUTO MODE v2 in LIVE mode?\n\nThis will automatically place real BUY/SELL orders.\nPAPER toggle is OFF.\n\nProceed?`)) return;
  }
  btn.disabled=true; btn.textContent='⏳ Launching…';
  // Log to trade board status
  const logEl=$('tb-log');
  if(logEl){
    const ts=new Date().toLocaleTimeString('en-IN',{hour12:false});
    const row=document.createElement('div');
    row.style.cssText='padding:2px 0;border-bottom:1px solid var(--bdr)';
    row.textContent=`[${ts}] 🤖 Dashboard → PROD10 AUTO v2${paperLabel}`;
    logEl.prepend(row);
  }
  try{
    const r=await fetch('/api/prod10_auto',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({paper:_tbPaper})
    });
    const d=await r.json();
    if(d.ok){
      btn.textContent='🔄 Auto Running'+paperLabel;
      btn.style.background='linear-gradient(135deg,#15803d,#22c55e)';
      // Don't re-enable automatically — auto mode is long-running
      // User must restart PROD10 or wait for target P&L
    } else {
      alert('Auto v2 error: '+(d.error||'unknown'));
      btn.disabled=false; btn.textContent='🤖 Auto v2';
      btn.style.background='';
    }
  }catch(e){
    alert('Error: '+e);
    btn.disabled=false; btn.textContent='🤖 Auto v2';
    btn.style.background='';
  }
}

// ── Momentum Auto Bot toolbar controls ──────────────────────────────────────
let _mbLotsLocked = false;
let _mbMode = 'paper';

function mbSetMode(m){
  _mbMode = m;
  ['paper','mock','live'].forEach(x=>{
    const btn = $('mb-mode-'+x);
    if(!btn) return;
    btn.className = 'mb-mode-btn' + (x===m ? ' mb-on-'+x : '');
  });
}

// ── Log tab switcher ────────────────────────────────────────────────────────
let _tbLogTab = 'p10';
let _tbAutoLogTimer = null;

function tbSwitchLogTab(tab){
  _tbLogTab = tab;
  $('tb-log-tab-p10').classList.toggle('active', tab==='p10');
  $('tb-log-tab-auto').classList.toggle('active', tab==='auto');
  $('tb-log').style.display      = tab==='p10'  ? 'block' : 'none';
  $('tb-auto-log').style.display = tab==='auto' ? 'block' : 'none';
  if(tab==='auto'){
    tbLoadAutoLog();
    if(!_tbAutoLogTimer) _tbAutoLogTimer = setInterval(tbLoadAutoLog, 2000);
  } else {
    if(_tbAutoLogTimer){ clearInterval(_tbAutoLogTimer); _tbAutoLogTimer=null; }
  }
}

async function _tbLoadOiSummary(){
  try{
    const bar = $('tb-oi-summary'); if(!bar) return;
    const r = await fetch('/api/oi_verdict_summary');
    const d = await r.json();
    $('tb-oi-aw-n').textContent = d.ALIGNED_WIN  || 0;
    $('tb-oi-al-n').textContent = d.ALIGNED_LOSS || 0;
    $('tb-oi-ow-n').textContent = d.OPPOSED_WIN  || 0;
    $('tb-oi-ol-n').textContent = d.OPPOSED_LOSS || 0;
    $('tb-oi-nt-n').textContent = d.NEUTRAL      || 0;
    // Overall verdict: filter saves (OPPOSED_LOSS) vs misses (OPPOSED_WIN)
    const saves  = d.OPPOSED_LOSS || 0;
    const misses = d.OPPOSED_WIN  || 0;
    const vEl = $('tb-oi-verdict');
    if(saves+misses === 0){ vEl.textContent=''; }
    else if(saves > misses){ vEl.style.color='#4ade80'; vEl.textContent=`→ Filter helps (${saves} saves vs ${misses} blocked winners)`; }
    else if(misses > saves){ vEl.style.color='#f59e0b'; vEl.textContent=`→ Filter hurts (${misses} blocked winners vs ${saves} saves)`; }
    else { vEl.style.color='#9ab'; vEl.textContent=`→ Filter neutral (${saves} saves = ${misses} blocked winners)`; }
    bar.style.display='flex';
    bar.style.alignItems='center';
    bar.style.flexWrap='wrap';
    bar.style.gap='4px';
  }catch(e){}
}

async function tbLoadAutoLog(){
  const el = $('tb-auto-log'); if(!el) return;
  try{
    const r = await fetch('/api/momentum_bot_logs');
    const d = await r.json();
    if(d.offline || d.error){
      el.innerHTML = `<div style="color:var(--dim);font-size:10px;padding:10px;font-style:italic">${d.error||'Auto Bot not running — no log file found'}</div>`;
      return;
    }
    _tbLoadOiSummary();
    const lines = d.lines || [];
    if(!lines.length){ el.innerHTML='<div style="color:var(--dim);font-size:10px;padding:8px">Waiting for Auto Bot activity…</div>'; return; }
    const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.innerHTML = lines.map(line=>{
      // OI verdict lines — color by outcome
      if(line.includes('🔎') || line.includes('OI VERDICT')){
        const c = line.includes('ALIGNED & WON')  ? '#4ade80' :   // green  — aligned + win
                  line.includes('ALIGNED, STILL') ? '#f59e0b' :   // amber  — aligned but lost
                  line.includes('OPPOSED, BUT')   ? '#f59e0b' :   // amber  — opposed but won (filter would have missed)
                  line.includes('OPPOSED & LOST') ? '#60a5fa' :   // blue   — opposed + lost (filter would have saved)
                  line.includes('NEUTRAL')        ? '#6b7280' :   // grey   — neutral/stale
                  '#a78bfa';                                       // purple — fallback
        return `<div style="color:${c};padding:2px 4px;border-left:3px solid ${c};background:rgba(255,255,255,.03);margin:2px 0;white-space:pre-wrap;word-break:break-all;font-weight:600">${line}</div>`;
      }
      const c = line.includes('MOMENTUM ENTRY')||line.includes('BUY simulated')||line.includes('Quick target')||line.includes('Trail SL hit')?'var(--bull)':
                line.includes('SELL simulated')||line.includes('CLOSED')?'var(--warn)':
                line.includes('🎭')||line.includes('MOCK')||line.includes('SIM')?'#a78bfa':
                line.includes('❌')||line.includes('HARD SL')||line.includes('failed')?'var(--bear)':
                line.includes('Signal')||line.includes('Momentum')||line.includes('✅')?'var(--info)':
                line.includes('second')||(line.includes('CE =')||line.includes('PE ='))?'var(--txt)':'var(--dim)';
      return `<div style="color:${c};padding:1px 4px;border-bottom:1px solid rgba(255,255,255,.03);white-space:pre-wrap;word-break:break-all">${line}</div>`;
    }).join('');
    if(wasAtBottom) el.scrollTop = el.scrollHeight;
  }catch(e){}
}

async function mbLoadExpiries(){
  const idx = $('mb-index').value;
  try{
    const r = await fetch(`/api/trade/expiries?index=${idx}`);
    const d = await r.json();
    const sel = $('mb-expiry');
    sel.innerHTML = (d.expiries||[]).map(e=>`<option value="${e}">${e}</option>`).join('');
    await mbFetchLotSize();
  }catch(e){}
}

async function mbFetchLotSize(){
  const index  = ($('mb-index')||{}).value;
  const expiry = ($('mb-expiry')||{}).value;
  if(!index || !expiry) return;
  try{
    const r = await fetch(`/api/lot_size?index=${index}&expiry=${encodeURIComponent(expiry)}`);
    const d = await r.json();
    _mbLotSizeMap[index] = d.lot_size || 75;
  }catch(e){}
  mbUpdateLotInfo();
}

function mbToggleLots(){
  _mbLotsLocked = !_mbLotsLocked;
  const inp = $('mb-lots'), btn = $('mb-lots-lock');
  inp.disabled = _mbLotsLocked;
  inp.style.opacity = _mbLotsLocked ? '.5' : '1';
  btn.textContent = _mbLotsLocked ? '🔒' : '🔓';
  btn.style.opacity = _mbLotsLocked ? '1' : '.45';
}

let _mbCapital    = null;   // option buy balance (from last fetch)
let _mbClearCash  = null;   // clear cash (fallback)
let _mbLotSizeMap = {};     // {index: lotSize} cached from last fetch

async function mbFetchCapital(){
  const btn = $('mb-cap-btn');
  btn.disabled = true; btn.textContent = '⏳…';
  try{
    const r = await fetch('/api/groww_capital');
    const d = await r.json();
    if(!d.ok){
      btn.disabled=false; btn.textContent='💰 Show';
      alert('Could not fetch capital: '+(d.error||'unknown'));
      return;
    }
    _mbCapital   = d.option_buy_balance;
    _mbClearCash = d.clear_cash;

    // Fetch lot size for current index+expiry
    const index  = $('mb-index').value;
    const expiry = $('mb-expiry').value;
    const ls = await fetch(`/api/lot_size?index=${index}&expiry=${encodeURIComponent(expiry)}`);
    const ld = await ls.json();
    _mbLotSizeMap[index] = ld.lot_size || 75;

    mbRenderCapital();
    mbUpdateLotInfo();
    btn.disabled=false; btn.textContent='↻ Refresh';
  }catch(e){
    btn.disabled=false; btn.textContent='💰 Show';
    alert('Error: '+e);
  }
}

function mbRenderCapital(){
  if(_mbCapital === null) return;
  const lots     = parseInt($('mb-cap-lots').value) || 1;
  const index    = $('mb-index').value;
  const lotSize  = _mbLotSizeMap[index] || 75;
  const qty      = lots * lotSize;

  // Use option buy balance if positive, else fall back to clear cash
  const usable   = _mbCapital > 0 ? _mbCapital : _mbClearCash;
  const maxPrem  = qty > 0 ? Math.floor(usable / qty) : 0;
  const targetP  = parseInt($('mb-cap-prem').value) || 200;
  const needed   = targetP * qty - usable;   // extra capital required for target premium

  // Row 1: balances
  $('mb-cap-val').textContent  = `₹${Math.round(usable).toLocaleString('en-IN')}`;
  $('mb-cap-cash').textContent = `₹${Math.round(_mbClearCash).toLocaleString('en-IN')}`;
  $('mb-cap-row1').style.display = 'block';

  // Row 2: max premium + capital needed
  const maxEl  = $('mb-cap-maxprem');
  const needEl = $('mb-cap-need');
  maxEl.textContent  = `Max ₹${maxPrem} (${lots}L×${lotSize})`;
  maxEl.style.color  = maxPrem >= targetP ? '#4ade80' : '#f59e0b';

  if(needed > 0){
    needEl.textContent = `Need ₹${Math.round(needed).toLocaleString('en-IN')} more`;
    needEl.style.color = '#f87171';
  } else {
    needEl.textContent = `✅ Sufficient`;
    needEl.style.color = '#4ade80';
  }
  $('mb-cap-row2').style.display = 'block';
}

// Re-render capital calc and lot info whenever relevant inputs change
document.addEventListener('DOMContentLoaded', ()=>{
  // Capital calc: dedicated inputs
  ['mb-cap-lots','mb-cap-prem'].forEach(id=>{
    const el = document.getElementById(id);
    if(el) el.addEventListener('input', mbRenderCapital);
  });

  // Auto lot info: update immediately on lots change
  const lotsEl = document.getElementById('mb-lots');
  if(lotsEl) lotsEl.addEventListener('input', mbUpdateLotInfo);

  // Re-fetch lot size (and re-render capital) when expiry changes
  const expEl = document.getElementById('mb-expiry');
  if(expEl) expEl.addEventListener('change', async ()=>{
    await mbFetchLotSize();
    if(_mbCapital !== null) mbRenderCapital();
  });
});

async function mbStartAutoBot(){
  const btn      = $('mb-start-btn');
  const index    = $('mb-index').value;
  const expiry   = $('mb-expiry').value;
  const lots     = parseInt($('mb-lots').value) || 1;
  const exitMode = $('mb-exit-mode').value;
  const premMin  = parseInt($('mb-prem-min').value) || 50;
  const premMax  = parseInt($('mb-prem-max').value) || 200;
  const strikes  = parseInt($('mb-strikes').value) || 3;
  const scanSec  = parseInt($('mb-scan-sec').value) || 10;
  const pollSec  = parseInt($('mb-poll-sec').value) || 1;
  const mode     = _mbMode;
  if(!expiry){ alert('Select an expiry first'); return; }
  if(premMin >= premMax){ alert('Min premium must be less than max premium'); return; }
  const modeLabel = mode.toUpperCase();
  if(mode === 'live'){
    if(!confirm(`Launch Momentum Auto Bot in LIVE mode?\n\nIndex: ${index}  Expiry: ${expiry}  Lots: ${lots}\nPremium: ₹${premMin}–₹${premMax}  Strikes: ±${strikes}\n\nThis will place REAL orders on Groww.\n\nProceed?`)) return;
  }
  btn.disabled = true; btn.textContent = '⏳ Launching…';
  btn.classList.remove('running');
  try{
    const r = await fetch('/api/bot/start',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id:'momentum', config:{
        trade_mode: mode, index, expiry, lots,
        exit_mode: exitMode,
        min_premium: premMin, max_premium: premMax,
        atm_range: strikes,
        validate_orders: _mbValidate,
        scan_seconds: scanSec,
        poll_seconds: pollSec,
        choppiness_enabled: _mbChopEnabled,
        consec_sl_brake: _mbConsSL,
        HARD_SL_ATR_BASED: _mbAtrSL,
        atr_source: _mbAtrSource,
        min_score_filter: _mbMinScoreFilter,
        velocity_filter:  _mbVelFilter
      }})
    });
    const d = await r.json();
    if(d.ok){
      btn.textContent = `🔄 [${modeLabel}]`;
      btn.classList.add('running');
      setTimeout(()=>{ btn.disabled=false; btn.textContent='🚀 Auto Bot'; btn.classList.remove('running'); }, 8000);
    } else {
      alert('Could not start: '+(d.error||'unknown'));
      btn.disabled=false; btn.textContent='🚀 Auto Bot';
    }
  }catch(e){
    alert('Error: '+e);
    btn.disabled=false; btn.textContent='🚀 Auto Bot';
  }
}

// ── AUTO MODE v2 status poller ──────────────────────────────────────────────
let _autoStatusTimer = null;
const _AUTO_STATE_COLOR = {
  STARTING:     '#f59e0b',
  SCANNING:     '#3b82f6',
  IN_TRADE:     '#22c55e',
  TRADE_CLOSED: '#a855f7',
  STOPPED:      '#6b7280',
  IDLE:         '#6b7280',
};

function tbStartAutoStatusPoll(){
  if(_autoStatusTimer) clearInterval(_autoStatusTimer);
  _autoStatusTimer = setInterval(tbPollAutoStatus, 5000);
  tbPollAutoStatus();
}

async function tbPollAutoStatus(){
  try{
    const r = await fetch('/api/auto_mode_status');
    const d = await r.json();
    tbRenderAutoStatus(d);
  }catch(e){}
}

function tbRenderAutoStatus(d){
  const panel = $('tb-auto-status-panel');
  const badge = $('tb-auto-mode-badge');
  const body  = $('tb-auto-status-body');
  if(!panel || !badge || !body) return;

  const state = (d.state || 'IDLE').toUpperCase();
  if(state === 'IDLE'){
    panel.style.display = 'none';
    return;
  }
  panel.style.display = '';
  badge.textContent = state;
  badge.style.color = _AUTO_STATE_COLOR[state] || '#a855f7';
  badge.style.background = (_AUTO_STATE_COLOR[state]||'#a855f7') + '22';

  const label  = d.mode_label || '';
  const index  = d.index || '';
  const expiry = d.expiry || '';
  const pnl    = typeof d.total_pnl === 'number' ? '₹'+d.total_pnl.toFixed(2) : '—';
  const trades = typeof d.trade_count === 'number' ? d.trade_count : '—';

  let html = '';
  if(state === 'SCANNING'){
    const ce = typeof d.votes_ce==='number' ? d.votes_ce : '?';
    const pe = typeof d.votes_pe==='number' ? d.votes_pe : '?';
    const dir = d.direction || 'WAIT';
    const conf = d.confidence || '';
    const dirColor = dir==='CE' ? '#22c55e' : dir==='PE' ? '#ef4444' : '#6b7280';
    html = `<span style="color:var(--dim)">${index} ${label} | exp ${expiry}</span><br>
      <span style="color:#3b82f6">Votes</span> CE=<b style="color:#22c55e">${ce}</b> &nbsp;PE=<b style="color:#ef4444">${pe}</b>
      &nbsp;→&nbsp;<b style="color:${dirColor}">${dir}</b> [${conf}]<br>
      <span style="color:var(--dim)">P&amp;L: ${pnl} | Trades: ${trades}</span>`;
  } else if(state === 'IN_TRADE'){
    const sym   = d.symbol || '';
    const entry = typeof d.entry_price==='number' ? '₹'+d.entry_price.toFixed(2) : '—';
    const dir   = d.direction || '';
    const dirColor = dir==='CE' ? '#22c55e' : '#ef4444';
    html = `<span style="color:#22c55e">▲ ACTIVE TRADE — ${index} ${label}</span><br>
      <b style="color:${dirColor}">${dir}</b> <span style="color:var(--fg)">${sym}</span>
      &nbsp;Entry: <b>${entry}</b> [${d.confidence||''}]<br>
      <span style="color:var(--dim)">Session P&amp;L: ${pnl} | Trade #${trades}</span>`;
  } else if(state === 'TRADE_CLOSED'){
    const sym   = d.symbol || '';
    const ep    = typeof d.entry_price==='number' ? '₹'+d.entry_price.toFixed(2) : '—';
    const xp    = typeof d.exit_price==='number'  ? '₹'+d.exit_price.toFixed(2)  : '—';
    const tp    = typeof d.trade_pnl==='number'   ? d.trade_pnl : 0;
    const tpCol = tp >= 0 ? '#22c55e' : '#ef4444';
    const reason = (d.exit_reason||'').replace(/[🔻🎯⏰]/g,'').trim();
    html = `<span style="color:#a855f7">● Last trade closed — ${index} ${label}</span><br>
      ${sym} &nbsp;${ep} → ${xp} &nbsp;<b style="color:${tpCol}">₹${tp.toFixed(2)}</b><br>
      <span style="color:var(--dim)">${reason} | Session P&amp;L: ${pnl}</span>`;
  } else if(state === 'STOPPED'){
    const reason = d.stop_reason || '';
    html = `<span style="color:#6b7280">■ AUTO MODE stopped — ${reason}</span><br>
      <span style="color:var(--dim)">Session P&amp;L: ${pnl} | Trades: ${trades}</span>`;
  } else {
    html = `<span style="color:var(--dim)">${state} — ${index} ${label}</span>`;
  }
  body.innerHTML = html;
}

// start polling when the Trade Board tab is active
(function(){
  const origTabSwitch = window.showTab;
  window.showTab = function(id){
    if(typeof origTabSwitch==='function') origTabSwitch(id);
    if(id==='tab-trade') tbStartAutoStatusPoll();
    else if(_autoStatusTimer){ clearInterval(_autoStatusTimer); _autoStatusTimer=null; }
  };
})();

function tbStartLogPoll(){
  if(_tbLogTimer) clearInterval(_tbLogTimer);
  _tbLogTimer=setInterval(tbPollProd10Logs,500);
  tbPollProd10Logs();
}

async function tbPollProd10Logs(){
  try{
    const [r, rs] = await Promise.all([fetch('/api/prod10_logs'), fetch('/api/trade/status')]);
    const d=await r.json(); const ts=await rs.json();
    tbRenderHistory(ts.history||[]);
    const el=$('tb-log'); if(!el) return;
    // PROD10 is offline
    if(d.offline){
      const statusEl=$('tb-trade-status');
      if(statusEl) statusEl.innerHTML=`<div style="padding:10px 4px">
        <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:6px;font-weight:600">PROD10 STATUS <span style="color:var(--bear)">● OFFLINE</span></div>
        <div style="font-size:11px;color:var(--dim)">${d.error||'PROD10 not running'}</div>
        <div style="font-size:10px;color:var(--bdr);margin-top:8px">Click ▶ Start PROD10 in the toolbar above.</div>
      </div>`;
      if(!el.querySelector('.offline-note'))
        el.innerHTML=`<div class="offline-note" style="color:var(--dim);font-size:10px;padding:10px;font-style:italic">${d.error||'PROD10 not running'}</div>`;
      return;
    }
    const lines=d.lines||[];
    if(!lines.length){ el.innerHTML='<div style="color:var(--dim);font-size:10px;padding:8px">Waiting for PROD10 activity…</div>'; return; }
    const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.innerHTML=lines.map(line=>{
      const c=line.includes('SELL EXECUTED')||line.includes('PROFIT')||line.includes('Trailing started')||line.includes('BUY Order placed')?'var(--bull)':
              line.includes('Trailing HIT')||line.includes('DYNAMIC SL')||line.includes('SL HIT')?'#f97316':
              line.includes('SELL Order placed')||line.includes('Placing SELL')?'var(--warn)':
              line.includes('❌')||line.includes('FAIL')||line.includes('LOSS')?'var(--bear)':
              line.includes('🎭')||line.includes('MOCK')?'#a78bfa':
              line.includes('🌐')||line.includes('DASHBOARD')?'var(--info)':
              line.includes('⚠')?'var(--warn)':'var(--dim)';
      return `<div style="color:${c};padding:1px 4px;border-bottom:1px solid rgba(255,255,255,.03);white-space:pre-wrap;word-break:break-all">${line}</div>`;
    }).join('');
    if(wasAtBottom) el.scrollTop=el.scrollHeight;
    // update status area with timing breakdown + last active line
    const statusEl=$('tb-trade-status');
    if(statusEl){
      const cmdLine   = lines.filter(l=>l.includes('Command entered')||l.includes('[DASHBOARD]')).slice(-1)[0]||'';
      const ltpLine   = lines.filter(l=>l.includes('Entry price')||l.includes('LTP for')||l.includes('LTP from cache')).slice(-1)[0]||'';
      const buyLine   = lines.filter(l=>l.includes('BUY Order placed')||l.includes('BUY placed')).slice(-1)[0]||'';
      const trailLine = lines.filter(l=>l.includes('Trail started')||l.includes('Trailing started')).slice(-1)[0]||'';
      const hitLine   = lines.filter(l=>l.includes('Trailing HIT')||l.includes('DYNAMIC SL HIT')||l.includes('Max trail time')).slice(-1)[0]||'';
      const sellLine  = lines.filter(l=>l.includes('Placing SELL')).slice(-1)[0]||'';
      const sellDone  = lines.filter(l=>l.includes('SELL Order placed')||l.includes('SELL EXECUTED')).slice(-1)[0]||'';
      const monLine   = lines.filter(l=>l.includes('Monitoring')||l.includes('💓')).slice(-1)[0]||'';
      const errLine   = lines.filter(l=>l.includes('FAIL')||l.includes('❌')).slice(-1)[0]||'';
      const isActive  = trailLine&&!hitLine;
      // extract timestamps like [HH:MM:SS.mmm]
      const tsOf = s=>{ const m=s.match(/\[(\d{2}:\d{2}:\d{2}[.,]\d+)\]/); return m?m[1]:''; };
      const row = (lbl,val,clr='var(--dim)')=>val?`<div style="display:flex;gap:8px;font-size:10px;padding:1px 0"><span style="color:var(--dim);min-width:100px">${lbl}</span><span style="color:${clr};font-family:monospace">${val}</span></div>`:'';
      statusEl.innerHTML=`<div style="padding:8px 10px">
        <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:6px;font-weight:600">PROD10 STATUS ${isActive?'<span style="color:var(--bull)">● ACTIVE</span>':''}</div>
        ${_tbClickTs?row('🖱 Dashboard click', _fmtTs(_tbClickTs),'var(--info)'):''}
        ${row('📥 PROD10 received', tsOf(cmdLine),'var(--info)')}
        ${row('💰 LTP fetched', tsOf(ltpLine)||ltpLine.match(/₹[\d.]+/)?.[0]||'','var(--txt)')}
        ${row('✅ BUY placed', tsOf(buyLine)||buyLine.match(/took [\d.]+s/)?.[0]||'','var(--bull)')}
        ${row('📈 Trail started', tsOf(trailLine),'var(--bull)')}
        ${hitLine ?row('🔻 Trail/SL hit',  tsOf(hitLine),'#f97316'):''}
        ${sellLine?row('🔄 SELL started',  tsOf(sellLine),'var(--warn)'):''}
        ${sellDone?row('✅ SELL placed',   tsOf(sellDone)||sellDone.match(/took [\d.]+s/)?.[0]||'','var(--bull)'):''}
        ${monLine ?row('💓 Heartbeat',     tsOf(monLine),'var(--dim)'):''}
        ${errLine ?row('❌ Last error',    errLine.slice(-60),'var(--bear)'):''}
        ${d.file?`<div style="font-size:9px;color:var(--bdr);margin-top:6px">log: ${d.file}</div>`:''}
      </div>`;
    }
  }catch(e){}
}

function tbRenderLog(entries){
  const el=$('tb-log'); if(!el) return;
  if(!entries.length){el.innerHTML='<div style="color:var(--dim);font-size:11px;padding:10px">No activity yet</div>'; return;}
  el.innerHTML=entries.map(e=>{
    const parts=e.split(/  +/); const ts=parts[0]||''; const msg=parts.slice(1).join(' ');
    const c=msg.includes('DONE')||msg.includes('EXECUTED')?'var(--bull)':
            msg.includes('FAIL')||msg.includes('SL')||msg.includes('REJECTED')?'var(--bear)':
            msg.includes('EXIT')||msg.includes('SELL')?'var(--warn)':'var(--dim)';
    return `<div class="tlog-entry"><span class="tlog-ts">${ts}</span><span style="color:${c}">${msg}</span></div>`;
  }).join('');
}

function tbClearLog(){
  $('tb-log').innerHTML='<div style="color:var(--dim);font-size:11px;padding:10px">Log cleared</div>';
}

/* ── Market index ticker — equal cards, click-to-primary (◆), 1s poller ── */
const _IDX_CFG = {
  nifty:     {label:'NIFTY 50',  dec:0},
  banknifty: {label:'BANKNIFTY', dec:0},
  sensex:    {label:'SENSEX',    dec:0},
};
let _idxData  = {};
let _idxOrder = JSON.parse(localStorage.getItem('idx_order')||'null') || ['nifty','banknifty','sensex'];

function idxPin(key){
  _idxOrder = [key, ..._idxOrder.filter(k=>k!==key)];
  localStorage.setItem('idx_order', JSON.stringify(_idxOrder));
  renderIdxCards();
}
function renderIdxCards(){
  const el=$('mkt-ticker'); if(!el) return;
  el.innerHTML = _idxOrder.map((key,i)=>{
    const cfg=_IDX_CFG[key]; if(!cfg) return '';
    const v=_idxData[key]; const isPrimary=(i===0);
    const price = v&&v.last ? fmtN(v.last,cfg.dec) : '—';
    const chg = v&&v.last
      ? `<span style="color:${v.chg>=0?'var(--bull)':'var(--bear)'}">${v.chg>=0?'+':''}${fmtN(v.chg,cfg.dec)} (${v.chg>=0?'+':''}${(v.pct||0).toFixed(2)}%)</span>`
      : `<span style="color:var(--dim)">—</span>`;
    // ◆ = pinned primary, ◇ = click to set primary
    const pin = isPrimary
      ? `<span style="font-size:7px;color:var(--info);margin-left:4px;vertical-align:middle">◆</span>`
      : `<span style="font-size:7px;color:var(--bdr);margin-left:4px;vertical-align:middle" title="Set as primary">◇</span>`;
    return `<div class="idx-card${isPrimary?' primary':''}" onclick="idxPin('${key}')" title="Click to set as primary index">
      <div class="idx-card-header"><span class="idx-card-name">${cfg.label}</span>${pin}</div>
      <div class="idx-card-price" id="icp-${key}">${price}</div>
      <div class="idx-card-chg"  id="icc-${key}">${chg}</div>
    </div>`;
  }).join('');
}
async function _pollIndices(){
  try{
    const r=await fetch('/api/indices'); const d=await r.json();
    _idxData=d;
    // Update cells in-place (no re-render = no layout shift)
    let missing=false;
    _idxOrder.forEach(key=>{
      const v=d[key]; const cfg=_IDX_CFG[key]; if(!cfg) return;
      const pe=$('icp-'+key); const ce=$('icc-'+key);
      if(!pe){missing=true;return;}
      if(v&&v.last){
        pe.textContent=fmtN(v.last,cfg.dec);
        if(ce) ce.innerHTML=`<span style="color:${v.chg>=0?'var(--bull)':'var(--bear)'}">${v.chg>=0?'+':''}${fmtN(v.chg,cfg.dec)} (${v.chg>=0?'+':''}${(v.pct||0).toFixed(2)}%)</span>`;
      }
    });
    if(missing) renderIdxCards();
    // Live NIFTY spot → chain-spot and header
    if(d.nifty&&d.nifty.last>0){
      _tbChainSpot=d.nifty.last;
      const cs=$('chain-spot'); if(cs) cs.textContent='SPOT  ₹'+fmtN(d.nifty.last,2);
    }
  }catch(e){}
}
function startIdxTick(){
  renderIdxCards();
  _pollIndices();
  setInterval(_pollIndices,1000);
}

async function initPivots(){
  await loadPivots();
  _pivotTimer = setInterval(loadPivots, 60000);
}

// patch loadPivots to cache result
const _origLoadPivots = loadPivots;
loadPivots = async function(index){
  try{
    const idx = index || 'NIFTY';
    const r = await fetch(`/api/pivots?index=${idx}`);
    const d = await r.json();
    _pivotCache = d;
    renderPivots(d);
    const ageEl=$('pivot-age'); if(ageEl) ageEl.textContent=d.ts||'';
    const srcEl=$('pivot-src'); if(srcEl) srcEl.textContent=d._source||'';
  }catch(e){}
};

load(); startTick(); startIdxTick(); initPivots();

// ── Performance / Proof-of-Concept tab ───────────────────────────────────────
let _perfTimer = null;

function initPerfTab(){
  if(_perfTimer) return;
  _perfTimer = setInterval(loadPerf, 30000);
  loadPerf();
}

async function loadPerf(){
  try{
    const r = await fetch('/api/performance');
    const d = await r.json();
    renderPerf(d);
  }catch(e){}
}

function renderPerf(d){
  const outcomeHtml = o => o==='WIN'?'<span class="perf-win">✅ WIN</span>':
                           o==='LOSS'?'<span class="perf-loss">❌ LOSS</span>':
                           '<span class="perf-pend">⏳</span>';

  // S/R level respect log
  const srt = $('perf-sr-tbody');
  const srEvts = d.sr_events||[];
  if(srt){
    if(!srEvts.length){
      srt.innerHTML='<tr><td colspan="7" style="color:var(--dim);padding:14px;text-align:center">No fib NEAR events yet — run FIBONACCI_TREND_ANALYZER</td></tr>';
    } else {
      srt.innerHTML=srEvts.map(e=>{
        const resHtml = e.result==='RESPECTED'
          ? '<span class="perf-win">✅ Respected</span>'
          : e.result==='BROKE'
          ? '<span class="perf-loss">❌ Broke</span>'
          : '<span class="perf-pend">👀 Watching</span>';
        const typeClr = e.type==='RESIST'?'var(--bear)':'var(--bull)';
        const favorable = (e.type==='RESIST'&&e.move<0)||(e.type==='SUPPORT'&&e.move>0);
        const moveClr = e.move===0?'var(--dim)': favorable?'var(--bull)':'var(--bear)';
        return `<tr>
          <td style="color:var(--dim);font-family:monospace">${e.ts||'—'}</td>
          <td style="font-family:monospace;font-weight:700">${(e.level||0).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
          <td style="color:var(--info);font-size:10px">${e.label||'—'}</td>
          <td style="color:${typeClr};font-size:10px;font-weight:600">${e.type||'—'}</td>
          <td style="font-family:monospace;color:var(--dim)">${(e.spot_near||0).toLocaleString('en-IN')}</td>
          <td style="font-family:monospace;color:${moveClr}">${e.move>0?'+':''}${e.move||0}</td>
          <td>${resHtml}</td>
        </tr>`;
      }).join('');
    }
  }

  // Option signal outcomes
  const sigt = $('perf-sig-tbody');
  const sigEvts = d.signal_events||[];
  if(sigt){
    if(!sigEvts.length){
      sigt.innerHTML='<tr><td colspan="8" style="color:var(--dim);padding:14px;text-align:center">No CE/PE signals yet — run CHART_LEVEL_ANALYZER</td></tr>';
    } else {
      sigt.innerHTML=sigEvts.map(s=>{
        const dir=s.dir||'—';
        const badge=dir==='CE'?'perf-badge-ce':'perf-badge-pe';
        const moveClr=s.max_fav>0?'var(--bull)':'var(--dim)';
        return `<tr>
          <td style="color:var(--dim);font-family:monospace">${s.ts||'—'}</td>
          <td><span class="perf-badge ${badge}">${dir}</span></td>
          <td style="font-family:monospace">${(s.spot||0).toLocaleString('en-IN')}</td>
          <td style="color:var(--dim);font-size:10px">${s.reason||'—'}</td>
          <td style="color:var(--bull)">${s.t_pts>0?'+'+s.t_pts:'—'}</td>
          <td style="color:var(--bear)">${s.sl_pts>0?'-'+s.sl_pts:'—'}</td>
          <td style="font-family:monospace;color:${moveClr}">${s.max_fav>0?'+'+s.max_fav:'—'}</td>
          <td>${outcomeHtml(s.outcome)}</td>
        </tr>`;
      }).join('');
    }
  }

  const tsEl = $('perf-last-ts');
  if(tsEl) tsEl.textContent = 'Updated: '+(d.ts||'—');
}

// ─────────────────────────────────────────────────────────────
//  OI INTELLIGENCE TAB
// ─────────────────────────────────────────────────────────────
let _oiTimer = null;

function initOITab(){
  if(_oiTimer) return;
  _oiTimer = setInterval(renderOITab, 20000);
  renderOITab();
}

async function toggleOIAI(){
  const btn = document.getElementById('oi-ai-toggle-btn');
  // Optimistic: flip immediately so it feels instant
  const curOn = btn && btn.textContent.includes('ON');
  const nextOn = !curOn;
  if(btn){
    btn.textContent = '🤖 OI AI: ' + (nextOn ? 'ON' : 'OFF');
    btn.style.color       = nextOn ? 'var(--bull)' : 'var(--dim)';
    btn.style.borderColor = nextOn ? 'var(--bull)' : 'var(--bdr)';
    btn.disabled = true;
  }
  try {
    const r = await fetch('/api/toggle?f=oi_ai');
    const d = await r.json();
    // /api/toggle returns the _features dict directly (not nested)
    const on = d.oi_ai;
    if(btn){
      btn.textContent = '🤖 OI AI: ' + (on ? 'ON' : 'OFF');
      btn.style.color       = on ? 'var(--bull)' : 'var(--dim)';
      btn.style.borderColor = on ? 'var(--bull)' : 'var(--bdr)';
    }
  } catch(e){}
  if(btn) btn.disabled = false;
}

function renderOITab(){
  fetch('/api/data').then(r=>r.json()).then(d=>{
    const oi  = d.oi_snapshot || {};
    const oiAI = d.oi_ai || {};
    const feat = d.features || {};

    // ── Toggle button state ──
    const btn = document.getElementById('oi-ai-toggle-btn');
    if(btn){
      const on = feat.oi_ai;
      btn.textContent = '🤖 OI AI: ' + (on ? 'ON' : 'OFF');
      btn.style.color  = on ? 'var(--bull)' : 'var(--dim)';
      btn.style.borderColor = on ? 'var(--bull)' : 'var(--bdr)';
    }

    // ── Age label ──
    const ageEl = document.getElementById('oi-tab-age');
    if(ageEl){
      if(!oi.time){ ageEl.textContent = 'No OI data — run calculate_oi_pcr.py'; ageEl.style.color='var(--bear)'; }
      else{
        const stale = oi._stale;
        ageEl.textContent = `OI data: ${oi._ts_disp || oi.time} (${oi._age_sec||0}s ago)${stale?' ⚠️ STALE':''}`;
        ageEl.style.color = stale ? 'var(--warn)' : 'var(--dim)';
      }
    }

    if(!oi.price){ return; }   // no OI data yet

    // ── PCR cards ──
    const fmtPCR = v => {
      const n = parseFloat(v||0).toFixed(2);
      const el = document.createElement('span');
      el.textContent = n;
      el.style.color = v>1.1 ? 'var(--bull)' : v<0.9 ? 'var(--bear)' : 'var(--warn)';
      return el.outerHTML;
    };
    function setH(id, html){ const e=document.getElementById(id); if(e) e.innerHTML=html; }
    function setT(id, txt, clr){ const e=document.getElementById(id); if(e){e.textContent=txt; if(clr)e.style.color=clr;} }

    setH('oi-pcr-all', fmtPCR(oi.pcr_all));
    setH('oi-pcr-atm', fmtPCR(oi.pcr_atm));

    const sentClr = {BULLISH:'var(--bull)', BEARISH:'var(--bear)', NEUTRAL:'var(--warn)'};
    setT('oi-sentiment', oi.sentiment||'—', sentClr[oi.sentiment]||'var(--dim)');
    setT('oi-writer-bias', oi.writer_bias||'NEUTRAL', sentClr[oi.writer_bias]||'var(--dim)');

    const fmtCr = v => { const a=Math.abs(v),s=v<0?'-':''; return a>=1e7?s+(a/1e7).toFixed(2)+'Cr':s+(a/1e5).toFixed(1)+'L'; };
    setT('oi-total-ce', oi.total_oi_ce ? fmtCr(oi.total_oi_ce) : '—');
    setT('oi-total-pe', oi.total_oi_pe ? fmtCr(oi.total_oi_pe) : '—');
    const ceCh = oi.total_chg_ce||0, peCh = oi.total_chg_pe||0;
    setT('oi-chg-ce', (ceCh>=0?'+':'')+fmtCr(ceCh)+' session', ceCh>=0?'var(--bull)':'var(--bear)');
    setT('oi-chg-pe', (peCh>=0?'+':'')+fmtCr(peCh)+' session', peCh>=0?'var(--bull)':'var(--bear)');

    // ── Score bars (tick writer activity) ──
    const bull = oi.bullish_score||0, bear = oi.bearish_score||0;
    const total = bull + bear || 1;
    const bullPct = Math.round(bull/total*100), bearPct = Math.round(bear/total*100);
    setT('oi-bull-score', bull ? fmtCr(bull*1e6) : '0', 'var(--bull)');
    setT('oi-bear-score', bear ? fmtCr(bear*1e6) : '0', 'var(--bear)');
    const bb = document.getElementById('oi-bull-bar'); if(bb) bb.style.width = bullPct+'%';
    const rb = document.getElementById('oi-bear-bar'); if(rb) rb.style.width = bearPct+'%';

    // ── Market Direction Signal Banner ──
    const bs  = oi.bull_score_v2 || 0;
    const brs = oi.bear_score_v2 || 0;
    const ms  = oi.market_signal || '';
    const mom = oi.momentum_score || 0;
    const msClr = {'STRONG BULLISH':'var(--bull)','BULLISH':'#4ade80','NEUTRAL':'var(--warn)','BEARISH':'#f87171','STRONG BEARISH':'var(--bear)'}[ms] || 'var(--dim)';
    const msIcon = {'STRONG BULLISH':'🟢🟢','BULLISH':'🟢','NEUTRAL':'🟡','BEARISH':'🔴','STRONG BEARISH':'🔴🔴'}[ms] || '⬜';
    setT('oi-market-signal', ms ? msIcon+' '+ms : '— AWAITING DATA', msClr);
    setT('oi-bull-score-v2', bs||'—', 'var(--bull)');
    setT('oi-bear-score-v2', brs||'—', 'var(--bear)');
    setT('oi-momentum-score', mom||'—', 'var(--warn)');
    const bullBar = document.getElementById('oi-signal-bull-bar');
    const bearBar = document.getElementById('oi-signal-bear-bar');
    if(bullBar) bullBar.style.width = (bs/2)+'%';
    if(bearBar) bearBar.style.width = (brs/2)+'%';

    // ── Signal Components grid ──
    const scEl = document.getElementById('oi-signal-components');
    if(scEl){
      const sigList = oi.signal_list || [];
      if(!sigList.length){
        scEl.innerHTML = '<div style="color:var(--dim);font-size:11px;padding:8px">Awaiting OI data…</div>';
      } else {
        scEl.innerHTML = sigList.map(s => {
          const clr = s.dir==='bull'?'var(--bull)':s.dir==='bear'?'var(--bear)':'var(--dim)';
          const bg  = s.dir==='bull'?'rgba(52,211,153,.08)':s.dir==='bear'?'rgba(248,113,113,.08)':'var(--bg3)';
          const bdr = s.dir==='bull'?'rgba(52,211,153,.22)':s.dir==='bear'?'rgba(248,113,113,.22)':'var(--bdr)';
          const pts = s.pts>0 ? `<span style="font-size:9px;padding:1px 4px;border-radius:3px;background:${clr}22;color:${clr};margin-right:5px">+${s.pts}pts</span>` : '';
          return `<div style="padding:6px 8px;border-radius:5px;background:${bg};border:1px solid ${bdr}">
            <div style="font-size:10px;color:${clr}">${pts}${s.label}</div>
          </div>`;
        }).join('');
      }
    }

    // ── Smart Money Flow tables ──
    const smCE = oi.smart_money_ce || [];
    const smPE = oi.smart_money_pe || [];
    const smCETb = document.getElementById('sm-ce-tbody');
    if(smCETb){
      smCETb.innerHTML = smCE.length
        ? smCE.map((x,i)=>{
            const clr = i===0?'var(--bear)':'var(--txt)'; const fw = i===0?'700':'400';
            return `<tr style="border-bottom:1px solid var(--bdr)">
              <td style="text-align:right;padding:4px 6px;color:${clr};font-weight:${fw}">${x.strike}</td>
              <td style="text-align:right;padding:4px 6px;color:var(--bear)">+${(x.oi_change/1e3).toFixed(0)}K</td>
              <td style="text-align:right;padding:4px 6px;color:var(--dim)">₹${x.ltp||'—'}</td>
              <td style="text-align:right;padding:4px 6px;color:var(--dim)">${x.vol?(x.vol/1e3).toFixed(0)+'K':'—'}</td>
            </tr>`;
          }).join('')
        : '<tr><td colspan="4" style="color:var(--dim);text-align:center;padding:10px">No v3 data yet</td></tr>';
    }
    const smPETb = document.getElementById('sm-pe-tbody');
    if(smPETb){
      smPETb.innerHTML = smPE.length
        ? smPE.map((x,i)=>{
            const clr = i===0?'var(--bull)':'var(--txt)'; const fw = i===0?'700':'400';
            return `<tr style="border-bottom:1px solid var(--bdr)">
              <td style="text-align:right;padding:4px 6px;color:${clr};font-weight:${fw}">${x.strike}</td>
              <td style="text-align:right;padding:4px 6px;color:var(--bull)">+${(x.oi_change/1e3).toFixed(0)}K</td>
              <td style="text-align:right;padding:4px 6px;color:var(--dim)">₹${x.ltp||'—'}</td>
              <td style="text-align:right;padding:4px 6px;color:var(--dim)">${x.vol?(x.vol/1e3).toFixed(0)+'K':'—'}</td>
            </tr>`;
          }).join('')
        : '<tr><td colspan="4" style="color:var(--dim);text-align:center;padding:10px">No v3 data yet</td></tr>';
    }

    // ── Call Writing / Put Writing detection ──
    const cwRows = oi.call_writing || [];
    const pwRows = oi.put_writing  || [];
    const writeRowHtml = (x, side) => {
      const clr = side==='ce' ? 'var(--bear)' : 'var(--bull)';
      const confBg = x.tag==='CONFIRMED' ? (side==='ce'?'rgba(248,113,113,.15)':'rgba(52,211,153,.15)') : 'var(--bg3)';
      const confClr = x.tag==='CONFIRMED' ? clr : 'var(--dim)';
      const ltpChgHtml = x.ltp_chg ? `<span style="color:${x.ltp_chg<0?'var(--bull)':'var(--bear)'}"> LTP${x.ltp_chg>0?'+':''}${x.ltp_chg}</span>` : '';
      return `<div style="padding:5px 0;border-bottom:1px solid var(--bdr);display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span style="color:${clr};font-weight:600">Strike ${x.strike}</span>
        <span style="color:var(--dim)">OI+${(x.oi_change/1e3).toFixed(0)}K</span>
        <span style="color:var(--dim)">₹${x.ltp}</span>
        ${ltpChgHtml}
        <span style="padding:1px 6px;border-radius:3px;font-size:9px;background:${confBg};color:${confClr};border:1px solid ${confClr}44">${x.tag}</span>
      </div>`;
    };
    const cwEl = document.getElementById('oi-call-writing-rows');
    if(cwEl) cwEl.innerHTML = cwRows.length
      ? cwRows.slice(0,6).map(x=>writeRowHtml(x,'ce')).join('')
      : '<div style="color:var(--dim);text-align:center;padding:10px">No call writing detected yet</div>';
    const pwEl = document.getElementById('oi-put-writing-rows');
    if(pwEl) pwEl.innerHTML = pwRows.length
      ? pwRows.slice(0,6).map(x=>writeRowHtml(x,'pe')).join('')
      : '<div style="color:var(--dim);text-align:center;padding:10px">No put writing detected yet</div>';

    // ── ATM Momentum Signal ──
    const atmMom = oi.atm_momentum || null;
    const momCard = document.getElementById('oi-atm-momentum-card');
    if(momCard){
      const action = (atmMom && atmMom.action) || '⏳ WAIT';
      const isBuyCE = action.includes('BUY CE');
      const isBuyPE = action.includes('BUY PE');
      const acClr = isBuyCE ? 'var(--bull)' : isBuyPE ? 'var(--bear)' : 'var(--dim)';
      momCard.style.borderLeftColor = isBuyCE ? 'var(--bull)' : isBuyPE ? 'var(--bear)' : 'var(--dim)';
      setT('oi-momentum-action', action, acClr);
      setT('oi-momentum-reason', (atmMom && atmMom.reason) || 'Awaiting 2nd tick…', 'var(--dim)');
      setT('oi-ce-momentum-score', atmMom ? atmMom.ce_momentum : '—', 'var(--bull)');
      setT('oi-pe-momentum-score', atmMom ? atmMom.pe_momentum : '—', 'var(--bear)');
      setT('oi-momentum-atm', atmMom ? atmMom.atm : '—');
      const tgtEl = document.getElementById('oi-momentum-targets');
      if(tgtEl){
        if(atmMom && atmMom.target){
          tgtEl.style.display = 'block';
          setT('oi-momentum-target', '₹'+atmMom.target, 'var(--bull)');
          setT('oi-momentum-stop',   '₹'+atmMom.stop,   'var(--bear)');
        } else { tgtEl.style.display = 'none'; }
      }
    }

    // ── Per-Strike Buildup table ──
    const buTb = document.getElementById('oi-buildup-tbody');
    if(buTb){
      const buildups = oi.strike_buildups || [];
      const buTypeClr = t => {
        if(!t) return 'var(--dim)';
        if(t==='LONG BUILDUP')   return 'var(--bull)';
        if(t==='SHORT BUILDUP')  return 'var(--bear)';
        if(t==='SHORT COVERING') return '#4ade80';
        if(t==='LONG UNWINDING') return '#f87171';
        return 'var(--dim)';
      };
      buTb.innerHTML = buildups.length ? buildups.map(b => {
        const rowBg = b.is_atm ? 'background:rgba(56,189,248,.07);' : '';
        const ceClr = buTypeClr(b.ce_buildup); const peClr = buTypeClr(b.pe_buildup);
        const fmtChg = v => v > 0 ? `<span style="color:var(--bull)">+${fmtCr(v)}</span>` : v < 0 ? `<span style="color:var(--bear)">${fmtCr(v)}</span>` : '<span style="color:var(--dim)">—</span>';
        return `<tr style="${rowBg}">
          <td style="text-align:right;padding:5px 8px;color:${b.is_atm?'var(--info)':'var(--txt)'};font-weight:${b.is_atm?700:400}">${b.strike}${b.is_atm?' ←ATM':''}</td>
          <td style="text-align:center;padding:5px 8px;color:${ceClr};font-size:10px;font-weight:600">${b.ce_buildup||'—'}</td>
          <td style="text-align:right;padding:5px 6px">${fmtChg(b.ce_oi_chg)}</td>
          <td style="text-align:right;padding:5px 6px;color:var(--dim)">${b.ce_ltp?'₹'+b.ce_ltp.toFixed(1):'—'}</td>
          <td style="text-align:center;padding:5px 8px;color:${peClr};font-size:10px;font-weight:600">${b.pe_buildup||'—'}</td>
          <td style="text-align:right;padding:5px 6px">${fmtChg(b.pe_oi_chg)}</td>
          <td style="text-align:right;padding:5px 6px;color:var(--dim)">${b.pe_ltp?'₹'+b.pe_ltp.toFixed(1):'—'}</td>
        </tr>`;
      }).join('')
      : '<tr><td colspan="7" style="color:var(--dim);text-align:center;padding:12px">Awaiting 2nd tick for LTP comparison…</td></tr>';
    }

    // ── IV Change Spikes ──
    const ivChEl = document.getElementById('oi-iv-changes');
    if(ivChEl){
      const ivChs = oi.iv_changes || [];
      ivChEl.innerHTML = ivChs.length ? ivChs.map(iv => {
        const clr = iv.signal.includes('BULLISH') ? 'var(--bull)' : iv.signal.includes('BEARISH') ? 'var(--bear)' : 'var(--warn)';
        const atmTag = iv.is_atm ? '<span style="font-size:9px;background:var(--info)22;color:var(--info);padding:1px 5px;border-radius:3px;margin-left:4px">ATM</span>' : '';
        return `<div style="padding:5px 0;border-bottom:1px solid var(--bdr)">
          <span style="color:var(--txt);font-weight:600">${iv.strike}</span>${atmTag}
          <span style="color:var(--dim);margin:0 6px">·</span>
          <span style="color:var(--bear)">CE IV ${iv.ce_iv_chg>0?'+':''}${iv.ce_iv_chg}%</span>
          <span style="color:var(--dim);margin:0 4px">·</span>
          <span style="color:var(--bull)">PE IV ${iv.pe_iv_chg>0?'+':''}${iv.pe_iv_chg}%</span>
          <div style="color:${clr};font-size:10px;margin-top:2px">${iv.signal}</div>
        </div>`;
      }).join('')
      : '<div style="color:var(--dim);text-align:center;padding:10px">No spikes detected yet</div>';
    }

    // ── PCR Change ──
    const pcrChEl = document.getElementById('oi-pcr-change');
    if(pcrChEl){
      const pch = oi.pcr_change || null;
      if(pch){
        const dClr = pch.delta > 0.05 ? 'var(--bull)' : pch.delta < -0.05 ? 'var(--bear)' : 'var(--warn)';
        pcrChEl.style.color = dClr;
        pcrChEl.textContent = pch.label || '—';
      } else {
        pcrChEl.style.color = 'var(--dim)';
        pcrChEl.textContent = 'Awaiting 2nd tick…';
      }
    }

    // ── Writer Activity rows ──
    const writerEl = document.getElementById('oi-writer-rows');
    if(writerEl){
      const rows = [];
      const addRow = (icon, label, strikes, implication, clr) => {
        if(!strikes || !strikes.length) return;
        rows.push(`
          <div style="display:flex;align-items:baseline;gap:6px;padding:5px 0;border-bottom:1px solid var(--bdr);flex-wrap:wrap">
            <span style="font-size:13px">${icon}</span>
            <div style="flex:1;min-width:180px">
              <div style="color:var(--txt);font-weight:600;font-size:11px">${label}</div>
              <div style="font-family:'JetBrains Mono',monospace;font-size:12px;color:${clr};margin-top:2px">
                ${strikes.slice(0,4).join('  ·  ')}
              </div>
            </div>
            <div style="font-size:10px;padding:2px 8px;border-radius:4px;background:${clr}22;color:${clr};white-space:nowrap;border:1px solid ${clr}44">
              ${implication}
            </div>
          </div>`);
      };
      addRow('📈', 'CALL writers ADDING (CE OI ↑) → BUY PE', oi.ce_writing_strikes, 'Resistance building — sell above', 'var(--bear)');
      addRow('📉', 'PUT writers ADDING (PE OI ↑) → BUY CE',  oi.pe_writing_strikes, 'Support building — buy above',    'var(--bull)');
      writerEl.innerHTML = rows.length ? rows.join('') :
        '<div style="color:var(--dim);text-align:center;padding:12px">No writer activity data yet (needs 2nd OI tick)</div>';
    }

    // ── Resistance / Support ──
    const res = (oi.resistance||[]).slice(0,3).join(' → ');
    const sup = (oi.support||[]).slice(0,3).join(' → ');
    setT('oi-resistance', res||'—'); setT('oi-support', sup||'—');
    setT('oi-spot', oi.price ? oi.price.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—');
    setT('oi-atm', oi.atm||'—');

    // ── Strongest Support / Resistance pulse cards + OI Range Band ──
    const resStr = oi.resistance_strength || [];
    const supStr = oi.support_strength    || [];
    const fmtCrR = v => { const a=Math.abs(v),s=v<0?'-':''; return a>=1e7?s+(a/1e7).toFixed(2)+'Cr':s+(a/1e5).toFixed(1)+'L'; };

    if(resStr.length){
      const top = resStr[0];
      setT('oi-res-strike', top.strike.toLocaleString('en-IN'), 'var(--bear)');
      setT('oi-res-oi',     'CE OI: '+fmtCrR(top.ce_oi||0), 'var(--dim)');
    }
    if(supStr.length){
      const top = supStr[0];
      setT('oi-sup-strike', top.strike.toLocaleString('en-IN'), 'var(--bull)');
      setT('oi-sup-oi',     'PE OI: '+fmtCrR(top.pe_oi||0), 'var(--dim)');
    }

    // ── Volume-backed breakout / breakdown indicators ──
    const resBreak = oi.resistance_breakout;
    const resBoEl = document.getElementById('oi-res-breakout');
    if(resBoEl){
      if(resBreak){
        const conf = resBreak.confirmed;
        const ratio = resBreak.vol_ratio ? resBreak.vol_ratio.toFixed(1)+'×' : '';
        resBoEl.textContent = conf ? `🔥 BREAKOUT vol ${ratio} avg` : `⚡ breach vol ${ratio} avg`;
        resBoEl.style.display    = 'block';
        resBoEl.style.background = conf ? 'rgba(251,146,60,.18)' : 'rgba(251,146,60,.07)';
        resBoEl.style.color      = conf ? '#fb923c' : '#fbbf24';
        resBoEl.style.border     = conf ? '1px solid rgba(251,146,60,.5)' : '1px solid rgba(251,146,60,.2)';
      } else {
        resBoEl.style.display = 'none';
      }
    }

    const supBreak = oi.support_breakdown;
    const supBdEl = document.getElementById('oi-sup-breakdown');
    if(supBdEl){
      if(supBreak){
        const conf = supBreak.confirmed;
        const ratio = supBreak.vol_ratio ? supBreak.vol_ratio.toFixed(1)+'×' : '';
        supBdEl.textContent = conf ? `🔻 BREAKDOWN vol ${ratio} avg` : `⚡ break vol ${ratio} avg`;
        supBdEl.style.display    = 'block';
        supBdEl.style.background = conf ? 'rgba(248,113,113,.18)' : 'rgba(248,113,113,.07)';
        supBdEl.style.color      = conf ? 'var(--bear)' : '#f87171';
        supBdEl.style.border     = conf ? '1px solid rgba(248,113,113,.5)' : '1px solid rgba(248,113,113,.2)';
      } else {
        supBdEl.style.display = 'none';
      }
    }

    // OI Range Band
    if(resStr.length && supStr.length){
      const rStrike = resStr[0].strike, sStrike = supStr[0].strike;
      const spot3 = oi.price || 0;
      setT('oi-range-res', rStrike.toLocaleString('en-IN'), 'var(--bear)');
      setT('oi-range-res-oi', 'CE OI '+fmtCrR(resStr[0].ce_oi||0), 'var(--dim)');
      setT('oi-range-sup', sStrike.toLocaleString('en-IN'), 'var(--bull)');
      setT('oi-range-sup-oi', 'PE OI '+fmtCrR(supStr[0].pe_oi||0), 'var(--dim)');

      // Position spot marker on the range bar
      const rangeSpan = rStrike - sStrike;
      const spotMarker = document.getElementById('oi-range-spot-marker');
      if(spotMarker && rangeSpan > 0 && spot3 >= sStrike && spot3 <= rStrike){
        const pct = ((spot3 - sStrike) / rangeSpan * 100).toFixed(1);
        spotMarker.style.left = pct + '%';
        spotMarker.textContent = `▼ ${spot3.toLocaleString('en-IN',{maximumFractionDigits:0})}`;
      }

      // Verdict
      const distToRes = rStrike - spot3, distToSup = spot3 - sStrike;
      let verdict = '';
      if(spot3 > rStrike){
        verdict = `🚀 Spot ABOVE resistance ${rStrike} — breakout! Watch for rejection or continuation.`;
      } else if(spot3 < sStrike){
        verdict = `🔻 Spot BELOW support ${sStrike} — breakdown! Strong bearish signal.`;
      } else {
        const nearRes = distToRes < distToSup;
        verdict = nearRes
          ? `⚠️ Spot ${distToRes.toFixed(0)}pts from resistance ${rStrike} (${distToSup.toFixed(0)}pts from support ${sStrike}) — near ceiling, cautious on CE`
          : `✅ Spot ${distToSup.toFixed(0)}pts above support ${sStrike} (${distToRes.toFixed(0)}pts from resistance ${rStrike}) — floor holding, CE bias`;
      }
      const vEl = document.getElementById('oi-range-verdict');
      if(vEl){ vEl.textContent = verdict; vEl.style.color = spot3 > rStrike ? 'var(--warn)' : spot3 < sStrike ? 'var(--bear)' : distToRes < distToSup ? 'var(--warn)' : 'var(--bull)'; }

      // Top 3 lists
      const resList = document.getElementById('oi-res-list');
      if(resList) resList.innerHTML = resStr.slice(0,3).map((x,i)=>
        `<div style="padding:2px 0;${i===0?'color:var(--bear);font-weight:700':'color:var(--txt)'}">
          ${i===0?'★':' '} ${x.strike.toLocaleString('en-IN')} &nbsp; CE OI ${fmtCrR(x.ce_oi||0)}
        </div>`).join('');
      const supList = document.getElementById('oi-sup-list');
      if(supList) supList.innerHTML = supStr.slice(0,3).map((x,i)=>
        `<div style="padding:2px 0;${i===0?'color:var(--bull);font-weight:700':'color:var(--txt)'}">
          ${i===0?'★':' '} ${x.strike.toLocaleString('en-IN')} &nbsp; PE OI ${fmtCrR(x.pe_oi||0)}
        </div>`).join('');
    }

    // ── Max Pain + Vol PCR + ATM IV ──
    const mp = oi.max_pain||0, spot2 = oi.price||0;
    if(mp){
      const mpDist = (spot2 - mp).toFixed(0);
      setT('oi-max-pain', mp.toLocaleString('en-IN'), mp > spot2 ? 'var(--bull)' : 'var(--bear)');
      setT('oi-max-pain-dist', (mpDist>=0?'+':'')+mpDist+' pts from spot', Math.abs(mpDist)<100?'var(--warn)':'var(--dim)');
    }
    const vpcr = oi.vol_pcr||0;
    if(vpcr){
      const vpClr = vpcr>1.1?'var(--bull)':vpcr<0.9?'var(--bear)':'var(--warn)';
      setT('oi-vol-pcr', vpcr.toFixed(2), vpClr);
    }
    const ceIV = oi.atm_ce_iv||0, peIV = oi.atm_pe_iv||0, skew = oi.iv_skew||0;
    if(ceIV||peIV){
      const skewTxt = skew>0?` ↑ CE costly`:`↓ PE costly`;
      setT('oi-atm-iv', `ATM IV: CE ${ceIV}%  PE ${peIV}%  skew${skew>0?'+':''}${skew}%${skewTxt}`, skew>1?'var(--bear)':skew<-1?'var(--bull)':'var(--dim)');
    }

    // ── ATM Strike OI table ──
    const tbody = document.getElementById('oi-atm-tbody');
    if(tbody){
      const atmMap  = oi.atm_strikes_oi||{};
      const atmEx   = oi.atm_extras||{};
      const keys = Object.keys(atmMap).map(Number).sort((a,b)=>a-b);
      if(!keys.length){
        tbody.innerHTML='<tr><td colspan="9" style="color:var(--dim);text-align:center;padding:14px">No ATM data</td></tr>';
      } else {
        const atm = oi.atm || 0;
        tbody.innerHTML = keys.map(s=>{
          const sd = atmMap[String(s)]||{};
          const ex = atmEx[String(s)]||{};
          const ceOI = sd.ce_oi||0, peOI = sd.pe_oi||0;
          const diff = peOI - ceOI;
          const isAtm = s === atm;
          const diffClr = diff>0 ? 'var(--bull)' : diff<0 ? 'var(--bear)' : 'var(--dim)';
          const bias = diff>0 ? '<span style="color:var(--bull)">CE favours</span>' : diff<0 ? '<span style="color:var(--bear)">PE favours</span>' : '<span style="color:var(--dim)">Neutral</span>';
          const rowBg = isAtm ? 'background:rgba(56,189,248,.07);' : '';
          const ceIVcol = ex.ce_iv ? ex.ce_iv.toFixed(1)+'%' : '—';
          const peIVcol = ex.pe_iv ? ex.pe_iv.toFixed(1)+'%' : '—';
          const ceLtp  = ex.ce_ltp ? '₹'+ex.ce_ltp.toFixed(1) : '—';
          const peLtp  = ex.pe_ltp ? '₹'+ex.pe_ltp.toFixed(1) : '—';
          return `<tr style="${rowBg}">
            <td style="text-align:right;padding:5px 6px;color:${isAtm?'var(--info)':'var(--txt)'};font-weight:${isAtm?'700':'400'}">${s}${isAtm?' ←ATM':''}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--bear)">${fmtCr(ceOI)}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--bull)">${fmtCr(peOI)}</td>
            <td style="text-align:right;padding:5px 6px;color:${diffClr}">${diff>=0?'+':''}${fmtCr(Math.abs(diff))}</td>
            <td style="text-align:center;padding:5px 6px">${bias}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--bear)">${ceIVcol}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--bull)">${peIVcol}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--dim)">${ceLtp}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--dim)">${peLtp}</td>
          </tr>`;
        }).join('');
      }
    }

    // ── OI History table ──
    const histTbody = document.getElementById('oi-history-tbody');
    if(histTbody){
      const hist = (d.oi_history || []).slice().reverse();  // newest first
      const sentClrH = {BULLISH:'var(--bull)', BEARISH:'var(--bear)', NEUTRAL:'var(--warn)'};
      if(!hist.length){
        histTbody.innerHTML='<tr><td colspan="12" style="color:var(--dim);text-align:center;padding:14px">Collecting OI history… (updates every ~60s)</td></tr>';
      } else {
        histTbody.innerHTML = hist.map((h, i) => {
          const prev  = hist[i+1];
          const dCE   = prev ? h.total_oi_ce - prev.total_oi_ce : null;
          const dPE   = prev ? h.total_oi_pe - prev.total_oi_pe : null;
          const fmtCrH = v => { const a=Math.abs(v),s=v<0?'-':''; return a>=1e7?s+(a/1e7).toFixed(2)+'Cr':s+(a/1e5).toFixed(1)+'L'; };
          const fmtM  = v => v ? fmtCrH(v) : '—';
          const fmtD  = v => v==null ? '—' : v===0 ? '—' : (v>0?'+':'')+fmtCrH(v);
          const dCEClr = dCE==null?'var(--dim)':dCE>0?'var(--bear)':'var(--bull)';
          const dPEClr = dPE==null?'var(--dim)':dPE>0?'var(--bull)':'var(--bear)';
          const pcrClr = h.pcr_all>1.1?'var(--bull)':h.pcr_all<0.9?'var(--bear)':'var(--warn)';
          const sClr   = sentClrH[h.sentiment]||'var(--dim)';
          const wClr   = sentClrH[h.writer_bias]||'var(--dim)';
          const msig   = h.market_signal||'';
          const msClrH = {'STRONG BULLISH':'var(--bull)','BULLISH':'#4ade80','NEUTRAL':'var(--warn)','BEARISH':'#f87171','STRONG BEARISH':'var(--bear)'}[msig]||'var(--dim)';
          const bsH = h.bull_score_v2||0, brH = h.bear_score_v2||0;
          const rowBg  = i===0 ? 'background:rgba(56,189,248,.05);' : '';
          return `<tr style="${rowBg}">
            <td style="padding:5px 8px;color:${i===0?'var(--info)':'var(--txt)'}${i===0?';font-weight:600':''}">${h.time}${i===0?' ←':''}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--txt)">${h.price?h.price.toLocaleString('en-IN',{minimumFractionDigits:1,maximumFractionDigits:1}):'—'}</td>
            <td style="text-align:right;padding:5px 6px;color:${pcrClr}">${parseFloat(h.pcr_all||0).toFixed(2)}</td>
            <td style="text-align:right;padding:5px 6px;color:${pcrClr}">${parseFloat(h.pcr_atm||0).toFixed(2)}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--bear)">${fmtM(h.total_oi_ce)}</td>
            <td style="text-align:right;padding:5px 6px;color:${dCEClr}">${fmtD(dCE)}</td>
            <td style="text-align:right;padding:5px 6px;color:var(--bull)">${fmtM(h.total_oi_pe)}</td>
            <td style="text-align:right;padding:5px 6px;color:${dPEClr}">${fmtD(dPE)}</td>
            <td style="text-align:center;padding:5px 6px;color:${sClr}">${h.sentiment||'—'}</td>
            <td style="text-align:center;padding:5px 6px;color:${wClr}">${h.writer_bias||'—'}</td>
            <td style="text-align:center;padding:5px 6px;color:${msClrH};font-size:10px">${msig||'—'}</td>
            <td style="text-align:right;padding:5px 6px;font-size:10px"><span style="color:var(--bull)">${bsH}</span>/<span style="color:var(--bear)">${brH}</span></td>
          </tr>`;
        }).join('');
      }
    }

    // ── OI AI text ──
    const aiEl = document.getElementById('oi-ai-text');
    if(aiEl){
      if(oiAI.status === 'ok' && oiAI.text){
        aiEl.textContent = oiAI.text;
        aiEl.style.color = 'var(--txt)';
      } else if(feat.oi_ai){
        aiEl.innerHTML = oiAI.error === 'no_cli'
          ? '<span style="color:var(--warn)">Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code</span>'
          : '<span style="color:var(--dim)">Generating OI Intelligence summary… (every 2 min)</span>';
      } else {
        aiEl.innerHTML = 'Enable OI AI (toggle above) to get Claude combined OI + signal market view every 2 minutes.<br>Requires: <b>calculate_oi_pcr.py</b> running in background.';
        aiEl.style.color = 'var(--dim)';
      }
    }
    const tsAI = document.getElementById('oi-ai-ts');
    if(tsAI) tsAI.textContent = oiAI.ts ? 'Updated '+oiAI.ts.slice(11,16) : (feat.oi_ai ? 'Pending…' : '—');
    const statAI = document.getElementById('oi-ai-status');
    if(statAI){
      statAI.textContent = oiAI.status||'—';
      statAI.style.color = oiAI.status==='ok' ? 'var(--bull)' : 'var(--dim)';
    }

    // ── Blink Logic: add/remove attention classes based on live data ──
    const _blink = (id, cls) => { const el=document.getElementById(id); if(el){ el.classList.remove('oi-blink-bull','oi-blink-bear','oi-blink-warn'); if(cls) el.classList.add(cls); }};

    // PCR ALL extremes (> 1.5 or < 0.6)
    const pcrVal = oi.pcr_all||0;
    if(pcrVal>1.5)       _blink('oi-pcr-all-card','oi-blink-bull');
    else if(pcrVal<0.6)  _blink('oi-pcr-all-card','oi-blink-bear');
    else                 _blink('oi-pcr-all-card','');

    // Market signal extremes (ms is already declared above as const ms = oi.market_signal||'')
    if(ms==='STRONG BULLISH')     _blink('oi-signal-banner','oi-blink-bull');
    else if(ms==='STRONG BEARISH')_blink('oi-signal-banner','oi-blink-bear');
    else                          _blink('oi-signal-banner','');

    // BUY NOW momentum
    const momAction = (oi.atm_momentum||{}).action||'';
    if(momAction.includes('BUY CE'))   _blink('oi-atm-momentum-card','oi-blink-bull');
    else if(momAction.includes('BUY PE'))_blink('oi-atm-momentum-card','oi-blink-bear');
    else                               _blink('oi-atm-momentum-card','');

    // Confirmed call/put writing
    const hasCW = (oi.call_writing||[]).some(x=>x.tag==='CONFIRMED');
    const hasPW = (oi.put_writing||[]).some(x=>x.tag==='CONFIRMED');
    _blink('oi-call-writing-card', hasCW ? 'oi-blink-bear' : '');
    _blink('oi-put-writing-card',  hasPW ? 'oi-blink-bull' : '');

    // IV spike at ATM
    const hasATMiv = (oi.iv_changes||[]).some(x=>x.is_atm);
    _blink('oi-iv-card', hasATMiv ? 'oi-blink-warn' : '');

    // PCR change significant shift
    const pcrDelta = Math.abs(((oi.pcr_change)||{}).delta||0);
    _blink('oi-pcr-change-card', pcrDelta>0.1 ? 'oi-blink-warn' : '');

    // Smart money: top CE addition > 10L (1M)
    const topCEAdd = ((oi.smart_money_ce||[])[0]||{}).oi_change||0;
    const topPEAdd = ((oi.smart_money_pe||[])[0]||{}).oi_change||0;
    _blink('oi-sm-ce-card', topCEAdd>1000000 ? 'oi-blink-bear' : '');
    _blink('oi-sm-pe-card', topPEAdd>1000000 ? 'oi-blink-bull' : '');

    // Resistance/Support: blink orange on confirmed breakout/breakdown, bear/bull on proximity
    const spotNow = oi.price||0;
    const resS = resStr.length ? resStr[0].strike : 0;
    const supS = supStr.length ? supStr[0].strike : 0;
    const resBreakConfirmed = resBreak && resBreak.confirmed;
    const supBreakConfirmed = supBreak && supBreak.confirmed;
    _blink('oi-res-card', resBreakConfirmed ? 'oi-blink-warn' : (resS && Math.abs(spotNow-resS)<50 ? 'oi-blink-bear' : ''));
    _blink('oi-sup-card', supBreakConfirmed ? 'oi-blink-warn' : (supS && Math.abs(spotNow-supS)<50 ? 'oi-blink-bull' : ''));

    // Max pain: spot within 30 pts
    _blink('oi-max-pain-card', mp && Math.abs(spot2-mp)<30 ? 'oi-blink-warn' : '');

    // ── Update OI chart data cache ──
    window._oiHistData = d.oi_history || [];
    if(typeof _oiChartVisible!=='undefined' && _oiChartVisible){
      _oiChartData = window._oiHistData.slice();
      _oiChartDrawFull();
    }
  }).catch(()=>{});
}

// ══════════════════════════════════════════════════════════════
//  BOT CONTROL CENTER
// ══════════════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════════════
//  TRADE HISTORY
// ══════════════════════════════════════════════════════════════
var _thBotFilter  = 'ALL';
var _thModeFilter = 'ALL';
var _thAllTrades  = [];

function _todayStr(){
  return new Date().toISOString().slice(0,10);
}

function resetThDates(){
  const t = _todayStr();
  document.getElementById('th-from').value = t;
  document.getElementById('th-to').value   = t;
  loadTradeHistory();
}

function setThBot(btn){
  document.querySelectorAll('.th-filter-btn[data-bot]').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  _thBotFilter = btn.dataset.bot;
  _renderThTable();
}

function setThMode(btn){
  document.querySelectorAll('.th-filter-btn[data-mode]').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  _thModeFilter = btn.dataset.mode;
  _renderThTable();
}

function _oiVerdictCell(r){
  // Build the OI column cell from oi_verdict_tag (new records) or
  // oi_bias + opt_type derived from symbol (backfill for older records)
  const TAG_CFG = {
    ALIGNED_WIN:  {e:'✅', c:'#4ade80', t:'OI Aligned & Won — filter would ALLOW this trade'},
    ALIGNED_LOSS: {e:'⚠️', c:'#f59e0b', t:'OI Aligned but Lost — OI was wrong about the move'},
    OPPOSED_WIN:  {e:'🚫', c:'#f59e0b', t:'OI Opposed but Won — filter would have BLOCKED this winner'},
    OPPOSED_LOSS: {e:'🛡️', c:'#60a5fa', t:'OI Opposed & Lost — filter WOULD HAVE SAVED this loss'},
  };
  const tag = r.oi_verdict_tag;
  if(tag && TAG_CFG[tag]){
    const {e,c,t}=TAG_CFG[tag];
    return `<span style="color:${c};font-size:9.5px;cursor:default;white-space:nowrap" title="${t}">${e} ${tag.replace('_',' ')}</span>`;
  }
  // Older records may have oi_bias but no verdict_tag — compute client-side
  const bias = r.oi_bias;
  if(bias && bias !== 'NEUTRAL' && bias !== 'n/a'){
    const sym = (r.symbol||'').toUpperCase();
    const ot  = sym.endsWith('CE')?'CE':sym.endsWith('PE')?'PE':null;
    if(ot){
      const win     = (r.pnl||0) > 0;
      const aligned = (bias==='BEARISH'&&ot==='PE')||(bias==='BULLISH'&&ot==='CE');
      const tag2    = aligned?(win?'ALIGNED_WIN':'ALIGNED_LOSS'):(win?'OPPOSED_WIN':'OPPOSED_LOSS');
      const {e,c,t} = TAG_CFG[tag2];
      return `<span style="color:${c};font-size:9.5px;cursor:default;white-space:nowrap" title="${t} (OI=${bias}, computed)">${e} ${tag2.replace('_',' ')}</span>`;
    }
  }
  return '<span style="color:#374151;font-size:9.5px">—</span>';
}

function _parseFNOSym(sym){
  const IDX = '(NIFTY|BANKNIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|BANKEX)';
  const MO = {JAN:'Jan',FEB:'Feb',MAR:'Mar',APR:'Apr',MAY:'May',JUN:'Jun',
              JUL:'Jul',AUG:'Aug',SEP:'Sep',OCT:'Oct',NOV:'Nov',DEC:'Dec'};
  const MC = {'1':'Jan','2':'Feb','3':'Mar','4':'Apr','5':'May','6':'Jun',
              '7':'Jul','8':'Aug','9':'Sep','O':'Oct','N':'Nov','D':'Dec'};
  // Monthly: INDEX + YY + 3-letter-month + STRIKE + TYPE
  let m = sym.match(new RegExp(IDX+'(\\d{2})([A-Z]{3})(\\d{4,6})(CE|PE)$','i'));
  if(m){
    const [,idx,yy,mon,strike,opt]=m;
    return {index:idx.toUpperCase(), option:`${strike}${opt.toUpperCase()}`, expiry:`${MO[mon.toUpperCase()]||mon}20${yy}`};
  }
  // Weekly compact: INDEX + YY + 1-char-month-code + DD + STRIKE + TYPE
  m = sym.match(new RegExp(IDX+'(\\d{2})([1-9OND])(\\d{2})(\\d{4,6})(CE|PE)$','i'));
  if(m){
    const [,idx,yy,mc,dd,strike,opt]=m;
    const mon = MC[mc.toUpperCase()]||mc;
    return {index:idx.toUpperCase(), option:`${strike}${opt.toUpperCase()}`, expiry:`${dd}${mon}20${yy}`};
  }
  return {index:'—', option:sym, expiry:'—'};
}

function loadTradeHistory(){
  const df = document.getElementById('th-from').value || _todayStr();
  const dt = document.getElementById('th-to').value   || _todayStr();
  const tbody = document.getElementById('th-tbody');
  if(tbody) tbody.innerHTML='<tr><td colspan="13" style="padding:20px;text-align:center;color:var(--dim)">Loading…</td></tr>';
  fetch(`/api/trade_history?from=${df}&to=${dt}`)
    .then(r=>r.json())
    .then(d=>{
      _thAllTrades = d.trades || [];
      _renderThTable();
    })
    .catch(()=>{
      if(tbody) tbody.innerHTML='<tr><td colspan="12" style="padding:20px;text-align:center;color:var(--bear)">Failed to load</td></tr>';
    });
}

function _renderThTable(){
  const tbody = document.getElementById('th-tbody');
  const sumEl = document.getElementById('th-summary');
  if(!tbody) return;

  let rows = _thAllTrades;
  if(_thBotFilter  !== 'ALL') rows = rows.filter(r=>r.bot  === _thBotFilter);
  if(_thModeFilter !== 'ALL') rows = rows.filter(r=>r.mode === _thModeFilter);

  if(!rows.length){
    tbody.innerHTML='<tr><td colspan="13" style="padding:20px;text-align:center;color:var(--dim)">No trades found for selected filters</td></tr>';
    if(sumEl) sumEl.style.display='none';
    return;
  }

  // Summary
  const totalPnl   = rows.reduce((s,r)=>s+(r.pnl||0),0);
  const wins       = rows.filter(r=>(r.pnl||0)>0).length;
  const losses     = rows.filter(r=>(r.pnl||0)<0).length;
  const winRate    = rows.length ? Math.round(wins/rows.length*100) : 0;
  const pnlClr     = totalPnl>=0?'var(--bull)':'var(--bear)';
  if(sumEl){
    sumEl.style.display='flex';
    sumEl.innerHTML=`
      <span>${rows.length} trade${rows.length!==1?'s':''}</span>
      <span style="color:${pnlClr};font-weight:700">P&L: ₹${totalPnl>=0?'+':''}${totalPnl.toFixed(2)}</span>
      <span style="color:var(--bull)">${wins}W</span>
      <span style="color:var(--bear)">${losses}L</span>
      <span style="color:var(--dim)">WR ${winRate}%</span>`;
  }

  // Table rows
  const modeClr = {live:'var(--bull)',mock:'var(--warn)',paper:'var(--dim)'};
  const botClr  = {Auto:'var(--info)',PROD10:'#c084fc',Trendline:'#f97316'};

  tbody.innerHTML = rows.map(r=>{
    const pnl   = r.pnl || 0;
    const pnlClr2 = pnl>0?'var(--bull)':pnl<0?'var(--bear)':'var(--dim)';
    const pnlStr  = (pnl>=0?'+':'')+pnl.toFixed(2);
    // parse option/expiry from stored fields or symbol
    const idx  = r.index  || _parseFNOSym(r.symbol||'').index;
    const opt  = r.option || _parseFNOSym(r.symbol||'').option;
    const exp  = r.expiry || _parseFNOSym(r.symbol||'').expiry;
    const mode = (r.mode||'').toLowerCase();
    const bot  = r.bot||'—';
    return `<tr>
      <td><span style="color:${botClr[bot]||'var(--txt)'};font-weight:600">${bot}</span></td>
      <td><span style="color:${modeClr[mode]||'var(--dim)'};text-transform:uppercase;font-size:9.5px">${mode}</span></td>
      <td style="color:var(--txt)">${idx}</td>
      <td style="color:var(--txt);font-weight:600">${opt}</td>
      <td style="color:var(--dim)">${exp}</td>
      <td style="text-align:right">₹${(r.buy_price||0).toFixed(2)}</td>
      <td style="text-align:right">₹${(r.sell_price||0).toFixed(2)}</td>
      <td style="text-align:right">${r.qty||0}</td>
      <td style="text-align:right;color:${pnlClr2};font-weight:700">₹${pnlStr}</td>
      <td style="color:var(--dim);font-size:10px">${r.time_entry||'—'}</td>
      <td style="color:var(--dim);font-size:10px">${r.time_exit||'—'}</td>
      <td style="color:var(--dim);font-size:10px;max-width:160px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${r.exit_reason||''}">${r.exit_reason||'—'}</td>
      <td style="font-size:10px;white-space:nowrap">${_oiVerdictCell(r)}</td>
    </tr>`;
  }).join('');
}

// Initialise dates and load on PnL tab open
(function(){
  const t = _todayStr();
  const f = document.getElementById('th-from');
  const d = document.getElementById('th-to');
  if(f) f.value = t;
  if(d) d.value = t;
})();

var _botsTimer = null;
// ── SCANNER TAB ──────────────────────────────────────────────────────────
let _scTimer = null;

function initScannerTab() {
  scLoadConfig();
  scLoadExpiries();
  scPollSignals();
  scRefreshLogs();
  scFetchChartData();
  // Set default backtest date range to today
  const today = new Date().toISOString().slice(0,10);
  const fromEl = document.getElementById('sc-bt-from');
  const toEl   = document.getElementById('sc-bt-to');
  if (fromEl && !fromEl.value) fromEl.value = today;
  if (toEl   && !toEl.value)   toEl.value   = today;
  if (!_scTimer) _scTimer = setInterval(() => { scPollSignals(); scRefreshLogs(); scFetchChartData(); }, 8000);
  // Mirror expiry into fresh backtest label
  setTimeout(() => {
    const ex = document.getElementById('sc-expiry');
    const wrap = document.getElementById('sc-bt-expiry-wrap');
    if (ex && wrap) wrap.textContent = ex.value || '—';
  }, 800);
}

async function scLoadConfig() {
  try {
    const r = await fetch('/api/trendline_config');
    if (!r.ok) return;
    const c = await r.json();
    if (c.expiry_date) document.getElementById('sc-expiry').value = c.expiry_date;
    if (c.premium_min) document.getElementById('sc-prem-min').value = c.premium_min;
    if (c.premium_max) document.getElementById('sc-prem-max').value = c.premium_max;
    if (c.lots)        document.getElementById('sc-lots').value = c.lots;
    // Trendline type toggles
    if (c.tl_ascending_enabled  != null) scSetToggle('sc-tl-asc',   c.tl_ascending_enabled);
    if (c.tl_descending_enabled != null) scSetToggle('sc-tl-desc',  c.tl_descending_enabled);
    if (c.tl_horizontal_enabled != null) scSetToggle('sc-tl-horiz', c.tl_horizontal_enabled);
    // Signal quality filter toggles
    if (c.spot_confirm_enabled   != null) scSetToggle('sc-spot-confirm', c.spot_confirm_enabled);
    if (c.volume_confirm_enabled != null) scSetToggle('sc-vol-confirm',  c.volume_confirm_enabled);
    if (c.volume_confirm_mult    != null) document.getElementById('sc-vol-mult').value = c.volume_confirm_mult;
    if (c.pct_confirm_enabled    != null) scSetToggle('sc-pct-confirm',  c.pct_confirm_enabled);
    if (c.bounce_confirm_pct     != null) document.getElementById('sc-pct-val').value  = c.bounce_confirm_pct;
    scCheckIdeal();
  } catch(e) {}
}

function _scConfigBody() {
  return {
    expiry_date:  document.getElementById('sc-expiry').value.trim(),
    premium_min:  parseFloat(document.getElementById('sc-prem-min').value) || 85,
    premium_max:  parseFloat(document.getElementById('sc-prem-max').value) || 200,
    lots:         parseInt(document.getElementById('sc-lots').value) || 18,
    tl_ascending_enabled:   scGetToggle('sc-tl-asc'),
    tl_descending_enabled:  scGetToggle('sc-tl-desc'),
    tl_horizontal_enabled:  scGetToggle('sc-tl-horiz'),
    spot_confirm_enabled:   scGetToggle('sc-spot-confirm'),
    volume_confirm_enabled: scGetToggle('sc-vol-confirm'),
    volume_confirm_mult:    parseFloat(document.getElementById('sc-vol-mult').value) || 1.3,
    pct_confirm_enabled:    scGetToggle('sc-pct-confirm'),
    bounce_confirm_pct:     parseFloat(document.getElementById('sc-pct-val').value) || 0.8,
  };
}

async function scSaveConfig() {
  try {
    const r = await fetch('/api/trendline_config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(_scConfigBody())});
    const j = await r.json();
    if (j.ok) alert('Config saved. Restart the bot to apply.');
  } catch(e) { alert('Save failed: ' + e); }
}

async function scSaveFilters() {
  try {
    await fetch('/api/trendline_config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(_scConfigBody())});
  } catch(e) {}
}

// ── IDEAL CONFIG PRESET ──────────────────────────────────────────────────────
// Backtest result: nb=N rc=N ds=N sc=N → ₹3,47,841 / 30 days / 18 lots
// Ascending ✓  Descending ✓  Horizontal ✗  All filters OFF
const IDEAL_CONFIG = {
  tl_ascending_enabled:  true,
  tl_descending_enabled: true,
  tl_horizontal_enabled: false,
  spot_confirm_enabled:  false,
  volume_confirm_enabled:false,
  pct_confirm_enabled:   false,
};

async function scApplyIdeal() {
  scSetToggle('sc-tl-asc',      IDEAL_CONFIG.tl_ascending_enabled);
  scSetToggle('sc-tl-desc',     IDEAL_CONFIG.tl_descending_enabled);
  scSetToggle('sc-tl-horiz',    IDEAL_CONFIG.tl_horizontal_enabled);
  scSetToggle('sc-spot-confirm',IDEAL_CONFIG.spot_confirm_enabled);
  scSetToggle('sc-vol-confirm', IDEAL_CONFIG.volume_confirm_enabled);
  scSetToggle('sc-pct-confirm', IDEAL_CONFIG.pct_confirm_enabled);
  scCheckIdeal();
  // Save to disk immediately
  try {
    const body = {..._scConfigBody(), ...IDEAL_CONFIG};
    const r = await fetch('/api/trendline_config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if (j.ok) {
      const btn = document.querySelector('button[onclick="scApplyIdeal()"]');
      if (btn) { const orig = btn.innerHTML; btn.innerHTML = '✅ Applied!'; setTimeout(() => { btn.innerHTML = orig; }, 1800); }
    }
  } catch(e) {}
}

function scGetToggle(id) {
  const el = document.getElementById(id);
  return el ? el.dataset.checked === 'true' : false;
}

function scSetToggle(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  el.dataset.checked = val ? 'true' : 'false';
  if (val) {
    el.textContent = 'ON';
    const onColors = {
      'sc-tl-asc':      ['#0d2e1a','#00c853'],
      'sc-tl-desc':     ['#2a0808','#ff5252'],
      'sc-tl-horiz':    ['#2a1f00','#ffd740'],
      'sc-spot-confirm':['#0a1f2e','#40c4ff'],
      'sc-vol-confirm': ['#0a1f2e','#40c4ff'],
      'sc-pct-confirm': ['#0a1f2e','#40c4ff'],
    };
    const [bg, fg] = onColors[id] || ['#0d2e1a','#00c853'];
    el.style.background = bg; el.style.color = fg; el.style.borderColor = fg;
  } else {
    el.textContent = 'OFF';
    el.style.background = '#111'; el.style.color = '#555'; el.style.borderColor = '#333';
  }
}

function scToggle(id) {
  scSetToggle(id, !scGetToggle(id));
  scSaveFilters();
  scCheckIdeal();
}

function scCheckIdeal() {
  const badge = document.getElementById('sc-ideal-badge');
  if (!badge) return;
  const match =
    scGetToggle('sc-tl-asc')      === IDEAL_CONFIG.tl_ascending_enabled  &&
    scGetToggle('sc-tl-desc')     === IDEAL_CONFIG.tl_descending_enabled &&
    scGetToggle('sc-tl-horiz')    === IDEAL_CONFIG.tl_horizontal_enabled &&
    scGetToggle('sc-spot-confirm')=== IDEAL_CONFIG.spot_confirm_enabled  &&
    scGetToggle('sc-vol-confirm') === IDEAL_CONFIG.volume_confirm_enabled &&
    scGetToggle('sc-pct-confirm') === IDEAL_CONFIG.pct_confirm_enabled;
  badge.style.display = match ? '' : 'none';
}

// ── TRENDLINE CHART RENDERER ─────────────────────────────────────────────────
let _scChartData = null;

// ── Demo data generator (deterministic — no Math.random()) ──────────────────
function _scDemoCandles(n, base, ampFrac, sineFreq, trend) {
  const candles = [];
  for (let i = 0; i < n; i++) {
    const t  = i / (n - 1);
    const s  = Math.sin(i * sineFreq);
    const s2 = Math.sin(i * sineFreq * 2.3 + 1.1);
    const mid = base * (1 + trend * t) + base * ampFrac * (0.6 * s + 0.25 * s2);
    const range = base * 0.008 * (0.5 + 0.5 * Math.abs(Math.sin(i * 0.7)));
    const o = mid - range * 0.3;
    const c = mid + range * 0.3 * (s > 0 ? 1 : -1);
    const h = Math.max(o, c) + range * 0.5;
    const l = Math.min(o, c) - range * 0.4;
    candles.push({ o, h, l, c, ts: 34200 + i * 900 });
  }
  return candles;
}

function _scBuildDemoData() {
  // Ascending channel: lower rail from lows (swing lows rise), upper rail from highs
  const ascCandles = _scDemoCandles(40, 150, 0.035, 0.55, 0.06);
  const ascP1L = {idx: 3,  price: ascCandles[3].l  - 1};
  const ascP2L = {idx: 18, price: ascCandles[18].l - 1};
  const slopeAL = (ascP2L.price - ascP1L.price) / (ascP2L.idx - ascP1L.idx);
  const ascP1H = {idx: 8,  price: ascCandles[8].h  + 1};
  const ascP2H = {idx: 22, price: ascCandles[22].h + 1};

  // Descending channel: upper rail from highs (swing highs fall), lower rail from lows
  const descCandles = _scDemoCandles(40, 150, 0.030, 0.50, -0.055);
  const descP1H = {idx: 4,  price: descCandles[4].h  + 1};
  const descP2H = {idx: 20, price: descCandles[20].h + 1};
  const descP1L = {idx: 6,  price: descCandles[6].l  - 1};
  const descP2L = {idx: 21, price: descCandles[21].l - 1};

  // Horizontal zone: flat range, both highs and lows within ~0.15%
  const horizCandles = _scDemoCandles(40, 150, 0.012, 0.80, 0.002);
  const hMid = horizCandles[horizCandles.length - 1].c;

  return {
    asc: {
      candles: ascCandles,
      ltp: ascCandles[ascCandles.length - 1].c,
      trendlines: [
        { type: 'ASC_SUPPORT', color: '#00c853', p1: ascP1L, p2: ascP2L },
        { type: 'ASC_RESIST',  color: '#69f0ae', p1: ascP1H, p2: ascP2H }
      ]
    },
    desc: {
      candles: descCandles,
      ltp: descCandles[descCandles.length - 1].c,
      trendlines: [
        { type: 'DESC_RESIST',  color: '#ff5252', p1: descP1H, p2: descP2H },
        { type: 'DESC_SUPPORT', color: '#ff8a80', p1: descP1L, p2: descP2L }
      ]
    },
    horiz: {
      candles: horizCandles,
      ltp: horizCandles[horizCandles.length - 1].c,
      trendlines: [
        { type: 'HORIZONTAL', color: '#ffd740', price: hMid }
      ]
    }
  };
}

async function scFetchChartData() {
  try {
    const r = await fetch('/api/trendline_chart');
    if (!r.ok) { _scLoadDemoFallback(); return; }
    const data = await r.json();
    const hasSpot = data.spot && data.spot.candles && data.spot.candles.length > 0;
    const hasInst = data.instruments && data.instruments.length > 0;
    if (!hasSpot && !hasInst) { _scLoadDemoFallback(); return; }

    _scChartData = data;

    // Hide DEMO badge
    const badge = document.getElementById('sc-chart-demo-badge');
    if (badge) badge.style.display = 'none';

    // Update status panel
    _scUpdateStatusPanel(data);

    // Populate instrument selector — only show instruments within premium range
    const sel     = document.getElementById('sc-chart-select');
    const prev    = sel ? sel.value : '';
    const pmMin   = _scChartData.premium_min || 0;
    const pmMax   = _scChartData.premium_max || 99999;
    if (sel) {
      sel.innerHTML = '<option value="">&#9660; pick instrument below</option>';
      (_scChartData.instruments || [])
        .filter(inst => inst.ltp >= pmMin && inst.ltp <= pmMax)
        .forEach(inst => {
          const o = document.createElement('option');
          o.value = inst.symbol;
          o.text  = inst.symbol.slice(-12) + '  ₹' + (inst.ltp || 0).toFixed(0);
          sel.appendChild(o);
        });
      if (prev) sel.value = prev;
    }

    // Timestamp
    const tsEl = document.getElementById('sc-chart-ts');
    if (tsEl && _scChartData.ts) tsEl.textContent = _scChartData.ts.slice(11);

    // Render NIFTY spot
    if (_scChartData.spot && _scChartData.spot.candles && _scChartData.spot.candles.length) {
      scRenderChart(_scChartData.spot, 'sc-chart-spot', 'NIFTY SPOT');
    }

    // Auto-select first instrument
    if (sel && !sel.value && _scChartData.instruments && _scChartData.instruments.length) {
      sel.value = _scChartData.instruments[0].symbol;
    }
    scRenderSelectedChart();

    // If instruments list is empty (after-hours), show demo on option canvas
    if (!hasInst) {
      const demo = _scBuildDemoData();
      scRenderChart(demo.asc, 'sc-chart-option', 'OPTION (no data yet — demo)');
    }
  } catch(e) { _scLoadDemoFallback(); }
}

function _scUpdateStatusPanel(data) {
  const s = data && data.status;

  // Dot + label
  const dot   = document.getElementById('sc-st-dot-circle');
  const label = document.getElementById('sc-st-dot-label');
  if (s) {
    dot.style.background   = '#00c853';
    dot.style.boxShadow    = '0 0 5px #00c853';
    label.textContent      = 'LIVE';
    label.style.color      = '#00c853';
  } else {
    dot.style.background   = '#555';
    dot.style.boxShadow    = 'none';
    label.textContent      = 'OFFLINE';
    label.style.color      = '#666';
  }

  // Spot + bars + tl counts
  const spotEl    = document.getElementById('sc-st-spot');
  const barsEl    = document.getElementById('sc-st-bars');
  const tlEl      = document.getElementById('sc-st-tl');
  const inRangeEl = document.getElementById('sc-st-inrange');
  if (s) {
    spotEl.textContent    = s.spot_ltp ? '₹' + s.spot_ltp.toFixed(0) : '—';
    barsEl.textContent    = s.spot_bars != null ? s.spot_bars : '—';
    tlEl.textContent      = s.tl_active + '/' + s.total;
    inRangeEl.textContent = s.in_range;
  } else {
    spotEl.textContent = barsEl.textContent = tlEl.textContent = inRangeEl.textContent = '—';
  }

  // Open trade row
  const tradeRow  = document.getElementById('sc-st-trade-row');
  const tradeInfo = document.getElementById('sc-st-trade-info');
  if (s && s.open_trade) {
    const t = s.open_trade;
    const pnl = ((t.ltp - t.entry) * 75).toFixed(0);   // approx, 75 qty per lot
    const trailMark = t.trail_active ? ' 🔄 trailing' : '';
    tradeInfo.textContent = `${t.symbol}  [${t.type}]  entry=₹${t.entry}  SL=₹${t.sl}  LTP=₹${t.ltp}${trailMark}`;
    tradeRow.style.display = '';
  } else {
    tradeRow.style.display = 'none';
  }

  // Watching list chips
  const watchEl = document.getElementById('sc-st-watching');
  if (s && s.near_signal && s.near_signal.length) {
    watchEl.innerHTML = s.near_signal.map(w => {
      const isClose  = Math.abs(w.dist) <= 6;
      const chipClr  = w.type === 'ASC'
        ? (isClose ? '#00c853' : '#1a5e33')
        : (isClose ? '#ff5252' : '#5e1a1a');
      const txtClr   = isClose ? '#fff' : '#999';
      const distStr  = w.dist >= 0 ? `+${w.dist}` : `${w.dist}`;
      const arrow    = w.dist >= 0 ? '↑' : '↓';
      return `<span title="LTP ₹${w.ltp}  support ₹${w.support}" style="display:inline-flex;align-items:center;gap:3px;background:${chipClr}22;border:1px solid ${chipClr};border-radius:4px;padding:2px 6px;font-size:10px;color:${txtClr};cursor:default">
        ${w.symbol} <span style="color:${chipClr}">${arrow}${distStr}</span>
      </span>`;
    }).join('');
  } else {
    watchEl.innerHTML = '<span style="color:#444;font-size:10px">no instruments near signal yet</span>';
  }
}

function _scLoadDemoFallback() {
  _scUpdateStatusPanel(null);
  const badge = document.getElementById('sc-chart-demo-badge');
  if (badge) badge.style.display = '';
  const tsEl = document.getElementById('sc-chart-ts');
  if (tsEl) tsEl.textContent = 'bot offline — showing demo';
  const demo = _scBuildDemoData();
  scRenderChart(demo.asc,  'sc-chart-spot',   'NIFTY SPOT (demo)');
  scRenderChart(demo.desc, 'sc-chart-option', 'OPTION (demo)');
  const lbl = document.getElementById('sc-chart-opt-label');
  if (lbl) lbl.textContent = 'OPTION';
  // Pre-fill selector with demo placeholder
  const sel = document.getElementById('sc-chart-select');
  if (sel && sel.options.length <= 1) {
    const o = document.createElement('option');
    o.value = '__demo__'; o.text = 'DEMO — start bot for live data';
    sel.appendChild(o);
    sel.value = '__demo__';
  }
}

function scRenderSelectedChart() {
  if (!_scChartData) return;
  const sel  = document.getElementById('sc-chart-select');
  const sym  = sel ? sel.value : '';
  const inst = (_scChartData.instruments || []).find(i => i.symbol === sym);
  const lbl  = document.getElementById('sc-chart-opt-label');
  if (inst) {
    if (lbl) lbl.textContent = sym.slice(-14) + ' (' + (inst.opt_type || '') + ')';
    scRenderChart(inst, 'sc-chart-option', sym.slice(-14));
  } else {
    if (lbl) lbl.textContent = 'OPTION';
    const cv = document.getElementById('sc-chart-option');
    if (cv) {
      const ctx = cv.getContext('2d');
      cv.width  = cv.parentElement ? cv.parentElement.clientWidth - 2 : 400;
      ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.fillStyle = '#444'; ctx.font = '12px monospace'; ctx.textAlign = 'center';
      ctx.fillText('Select an instrument above', cv.width / 2, cv.height / 2);
    }
  }
}

function scShowDemoGuide() {
  const modal = document.getElementById('sc-demo-modal');
  if (!modal) return;
  modal.style.display = 'flex';
  const demo = _scBuildDemoData();
  // Render all three guide charts; each canvas gets its own data
  // Use a timeout so the modal is visible (sized) before we draw
  setTimeout(() => {
    scRenderChart(demo.asc,   'sc-demo-asc',   'Ascending Channel');
    scRenderChart(demo.desc,  'sc-demo-desc',  'Descending Channel');
    scRenderChart(demo.horiz, 'sc-demo-horiz', 'Horizontal Zone');
  }, 30);
}

function scRenderChart(data, canvasId, title) {
  const cv = document.getElementById(canvasId);
  if (!cv) return;
  const ctx = cv.getContext('2d');
  cv.width  = cv.parentElement ? cv.parentElement.clientWidth - 2 : 400;
  const W = cv.width, H = cv.height;

  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, W, H);

  const allCandles = data.candles || [];
  if (!allCandles.length) {
    ctx.fillStyle = '#444'; ctx.font = '12px monospace'; ctx.textAlign = 'center';
    ctx.fillText('No candle data', W / 2, H / 2);
    return;
  }

  const totalN  = allCandles.length;
  const MAX_VIS = 35;
  // Always show the latest MAX_VIS candles — right-aligned
  const startIdx = Math.max(0, totalN - MAX_VIS);
  const candles  = allCandles.slice(startIdx);
  const nv       = candles.length;

  const mL = 4, mR = 56, mT = 20, mB = 18;
  const cW = W - mL - mR, cH = H - mT - mB;

  // Price range from visible candles only
  let lo = Infinity, hi = -Infinity;
  candles.forEach(c => { lo = Math.min(lo, c.l); hi = Math.max(hi, c.h); });
  // Also include trendline projected prices in range
  (data.trendlines || []).forEach(tl => {
    if (tl.type !== 'HORIZONTAL' && tl.p1 && tl.p2) {
      const slope = (tl.p2.price - tl.p1.price) / (tl.p2.idx - tl.p1.idx);
      const cur   = tl.p2.price + slope * (totalN - 1 - tl.p2.idx);
      lo = Math.min(lo, cur); hi = Math.max(hi, cur);
    }
    if (tl.type === 'HORIZONTAL') { lo = Math.min(lo, tl.price); hi = Math.max(hi, tl.price); }
  });
  if (data.ltp > 0) { lo = Math.min(lo, data.ltp); hi = Math.max(hi, data.ltp); }
  const pad = (hi - lo) * 0.08 || 1;
  lo -= pad; hi += pad;

  const toY  = p  => mT + (1 - (p - lo) / (hi - lo)) * cH;
  // Slot-based X: each candle gets an equal slot, centered in it
  const slotW = cW / nv;
  const toX  = vi => mL + (vi + 0.5) * slotW;          // vi = visible index (0..nv-1)
  const toXa = ai => toX(ai - startIdx);                // ai = absolute candle index
  const bW   = Math.max(2, slotW * 0.65);

  // Grid lines
  ctx.strokeStyle = '#1c2330'; ctx.lineWidth = 1;
  for (let g = 1; g <= 3; g++) {
    const y = mT + (g / 4) * cH;
    ctx.beginPath(); ctx.moveTo(mL, y); ctx.lineTo(W - mR, y); ctx.stroke();
    const p = hi - (g / 4) * (hi - lo);
    ctx.fillStyle = '#4a5568'; ctx.font = '9px monospace'; ctx.textAlign = 'left';
    ctx.fillText('₹' + p.toFixed(0), W - mR + 3, y + 3);
  }

  // Candles
  candles.forEach((c, vi) => {
    const x   = toX(vi);
    const isG = c.c >= c.o;
    const col = isG ? '#00c853' : '#ff5252';
    ctx.strokeStyle = col; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, toY(c.h)); ctx.lineTo(x, toY(c.l)); ctx.stroke();
    const bT = toY(Math.max(c.o, c.c)), bB = toY(Math.min(c.o, c.c));
    ctx.fillStyle   = isG ? '#00c85355' : '#ff525255';
    ctx.strokeStyle = col;
    ctx.fillRect(x - bW/2, bT, bW, Math.max(1, bB - bT));
    ctx.strokeRect(x - bW/2, bT, bW, Math.max(1, bB - bT));
  });

  // Trendlines
  (data.trendlines || []).forEach(tl => {
    ctx.save(); ctx.lineWidth = 1.5; ctx.strokeStyle = tl.color;
    if (tl.type === 'HORIZONTAL') {
      const y = toY(tl.price);
      if (y < mT || y > mT + cH) { ctx.restore(); return; }
      ctx.setLineDash([5, 4]);
      ctx.beginPath(); ctx.moveTo(mL, y); ctx.lineTo(W - mR, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = tl.color; ctx.font = 'bold 9px monospace'; ctx.textAlign = 'left';
      ctx.fillText('₹' + tl.price.toFixed(0), W - mR + 3, y + 3);
    } else if (tl.p1 && tl.p2 && tl.p2.idx > tl.p1.idx) {
      const slope = (tl.p2.price - tl.p1.price) / (tl.p2.idx - tl.p1.idx);
      const curP  = tl.p2.price + slope * (totalN - 1 - tl.p2.idx);
      // Start: p1 if visible, else extrapolate to left edge of visible window
      let x0, y0;
      if (tl.p1.idx >= startIdx) {
        x0 = toXa(tl.p1.idx); y0 = toY(tl.p1.price);
      } else {
        const priceAtStart = tl.p2.price + slope * (startIdx - tl.p2.idx);
        x0 = toX(0); y0 = toY(priceAtStart);
      }
      // End: always the right-most visible slot
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(toX(nv - 1), toY(curP));
      ctx.stroke();
      // Pivot dots (only if inside visible window)
      ctx.fillStyle = tl.color;
      [tl.p1, tl.p2].forEach(p => {
        if (p.idx >= startIdx && p.idx < totalN) {
          ctx.beginPath(); ctx.arc(toXa(p.idx), toY(p.price), 3, 0, Math.PI * 2); ctx.fill();
        }
      });
      // Current level label
      const yLbl = toY(curP);
      if (yLbl >= mT && yLbl <= mT + cH) {
        ctx.fillStyle = tl.color; ctx.font = 'bold 9px monospace'; ctx.textAlign = 'left';
        ctx.fillText('₹' + curP.toFixed(0), W - mR + 3, yLbl + 3);
      }
    }
    ctx.restore();
  });

  // LTP dashed line
  if (data.ltp > 0 && data.ltp >= lo && data.ltp <= hi) {
    const y = toY(data.ltp);
    ctx.strokeStyle = '#888'; ctx.lineWidth = 1; ctx.setLineDash([3, 5]);
    ctx.beginPath(); ctx.moveTo(mL, y); ctx.lineTo(W - mR, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#ccc'; ctx.font = 'bold 9px monospace'; ctx.textAlign = 'left';
    ctx.fillText('₹' + data.ltp.toFixed(0), W - mR + 3, y + 3);
  }

  // Candle count badge (top-right of chart area)
  ctx.fillStyle = '#445'; ctx.font = '9px monospace'; ctx.textAlign = 'right';
  ctx.fillText(totalN + ' bars', W - mR - 4, mT - 5);

  // Title
  ctx.fillStyle = '#667'; ctx.font = '10px monospace'; ctx.textAlign = 'left';
  ctx.fillText(title, mL + 4, mT - 5);

  // Watermark for demo/guide charts
  if (title && title.indexOf('demo') !== -1) {
    ctx.save();
    ctx.globalAlpha = 0.12;
    ctx.fillStyle = '#ffd740'; ctx.font = 'bold 28px monospace'; ctx.textAlign = 'center';
    ctx.fillText('DEMO', W / 2, H / 2 + 10);
    ctx.restore();
  }
}

function scStartBot() {
  fetch('/api/bot/start', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:'trendline_scanner'})})
  .then(r=>r.json()).then(j=>{ if(!j.ok) alert(j.error||'Start failed'); else scRefreshLogs(); });
}

function scStopBot() {
  fetch('/api/bot/stop', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:'trendline_scanner'})})
  .then(r=>r.json()).then(j=>{ if(!j.ok) alert(j.error||'Stop failed'); });
}

async function scPollSignals() {
  try {
    const r = await fetch('/api/trendline_signals');
    if (!r.ok) return;
    const d = await r.json();

    // Stats
    const s = d.stats || {};
    document.getElementById('sc-stat-trades').textContent  = s.trades  != null ? s.trades  : '—';
    document.getElementById('sc-stat-wins').textContent    = s.wins    != null ? s.wins    : '—';
    document.getElementById('sc-stat-losses').textContent  = s.losses  != null ? s.losses  : '—';
    const pnl = s.pnl != null ? s.pnl : null;
    const pnlEl = document.getElementById('sc-stat-pnl');
    if (pnl !== null) {
      pnlEl.textContent = '₹' + (pnl >= 0 ? '+' : '') + pnl.toLocaleString('en-IN', {minimumFractionDigits:2});
      pnlEl.style.color = pnl >= 0 ? 'var(--bull)' : 'var(--bear)';
    }

    // Active trade
    const at = d.active_trade;
    const atDiv = document.getElementById('sc-active-trade');
    if (at && at.symbol) {
      atDiv.style.display = 'block';
      document.getElementById('sc-active-content').innerHTML =
        '<b>' + at.symbol + '</b>  [' + (at.type||'') + ']<br>' +
        'Entry ₹' + (at.entry ? at.entry.toFixed(2) : '—') + '  &nbsp; SL ₹' + (at.sl ? at.sl.toFixed(2) : '—') +
        (at.peak ? '  &nbsp; Peak ₹' + at.peak.toFixed(2) : '');
    } else {
      atDiv.style.display = 'none';
    }

    // Signals list
    const sigs = (d.signals || []).slice().reverse();
    document.getElementById('sc-signals-ts').textContent = d.ts ? 'Updated: ' + d.ts.slice(11) : '';
    const list = document.getElementById('sc-signals-list');
    if (!sigs.length) {
      list.innerHTML = '<div style="color:var(--dim);font-size:12px;text-align:center;padding:20px">No signals yet today</div>';
    } else {
      list.innerHTML = sigs.map(sig => {
        const isConf = sig.status === 'CONFIRMED';
        const isFail = sig.status === 'FAILED';
        const clr = isConf ? 'var(--bull)' : isFail ? 'var(--bear)' : 'var(--dim)';
        const icon = isConf ? '✅' : isFail ? '⏭' : '⏳';
        const sym = sig.symbol || sig.candidate || '';
        let detail = '';
        if (isConf) {
          detail = 'Entry ₹' + (sig.entry ? sig.entry.toFixed(2) : '—') + '  SL ₹' + (sig.sl ? sig.sl.toFixed(2) : '—');
          if (sig.target) detail += '  Tgt ₹' + sig.target.toFixed(2);
        } else if (isFail) {
          detail = 'Confirmation failed';
        } else {
          detail = 'Waiting for momentum...';
        }
        return '<div style="display:flex;gap:8px;align-items:flex-start;padding:6px 8px;background:var(--bg);border-radius:5px;border-left:3px solid ' + clr + '">' +
          '<span style="font-size:14px">' + icon + '</span>' +
          '<div style="flex:1;min-width:0">' +
            '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' +
              '<span style="font-size:11px;font-weight:700;color:' + clr + '">' + (sig.type||'') + '</span>' +
              '<span style="font-size:11px;color:var(--txt);font-family:monospace">' + sym + '</span>' +
              '<span style="font-size:10px;color:var(--dim);margin-left:auto">' + (sig.ts||'') + '</span>' +
            '</div>' +
            '<div style="font-size:11px;color:var(--dim);margin-top:2px">' + detail + '</div>' +
            (sig.direction ? '<div style="font-size:10px;color:var(--info);margin-top:1px">' + sig.direction + '</div>' : '') +
          '</div>' +
        '</div>';
      }).join('');
    }
  } catch(e) {}
}

async function scRefreshLogs() {
  try {
    const r = await fetch('/api/bot/logs?id=trendline_scanner&n=80');
    if (!r.ok) return;
    const j = await r.json();
    const el = document.getElementById('sc-log-viewer');
    if (j.lines && j.lines.length) {
      el.textContent = j.lines.join('\\n');
      el.scrollTop = el.scrollHeight;
    } else {
      el.textContent = '(no logs yet — start the bot first)';
    }
  } catch(e) {}
}
async function scLoadExpiries() {
  try {
    const r = await fetch('/api/trendline_expiries');
    if (!r.ok) return;
    const d = await r.json();
    const sel = document.getElementById('sc-expiry');
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">-- select expiry --</option>' +
      (d.expiries||[]).map(e => `<option value="${e}"${e===cur?' selected':''}>${e}</option>`).join('');
    // Restore saved config value if not yet selected
    if (!sel.value) {
      fetch('/api/trendline_config').then(r=>r.json()).then(c=>{
        if (c.expiry_date) sel.value = c.expiry_date;
      }).catch(()=>{});
    }
  } catch(e) {}
}

async function scRunBacktest() {
  const from = document.getElementById('sc-bt-from').value;
  const to   = document.getElementById('sc-bt-to').value;
  const mode = document.getElementById('sc-bt-mode').value;
  const tbody = document.getElementById('sc-bt-tbody');
  const sumEl = document.getElementById('sc-bt-summary');
  tbody.innerHTML = '<tr><td colspan="10" style="padding:16px;text-align:center;color:var(--dim)">Loading...</td></tr>';
  try {
    const url = `/api/trendline_history?from=${from}&to=${to}&mode=${mode}`;
    const r = await fetch(url);
    const d = await r.json();
    const trades = d.trades || [];
    if (!trades.length) {
      tbody.innerHTML = '<tr><td colspan="10" style="padding:16px;text-align:center;color:var(--dim)">No trades found for this range</td></tr>';
      if(sumEl) sumEl.textContent = '';
      return;
    }
    // Summary
    const totalPnl = trades.reduce((s,t)=>s+(t.pnl||0),0);
    const wins  = trades.filter(t=>(t.pnl||0)>0).length;
    const losses= trades.filter(t=>(t.pnl||0)<0).length;
    const wr    = Math.round(wins/trades.length*100);
    const pclr  = totalPnl>=0?'var(--bull)':'var(--bear)';
    if(sumEl) sumEl.innerHTML =
      `${trades.length} trades &nbsp; <span style="color:${pclr};font-weight:700">P&L ₹${totalPnl>=0?'+':''}${totalPnl.toFixed(2)}</span>` +
      ` &nbsp; <span style="color:var(--bull)">${wins}W</span> <span style="color:var(--bear)">${losses}L</span>` +
      ` &nbsp; WR ${wr}%`;
    // Rows
    tbody.innerHTML = trades.map(t => {
      const pnl = t.pnl || 0;
      const pc  = pnl>0?'var(--bull)':pnl<0?'var(--bear)':'var(--dim)';
      const opt = t.play_type || t.signal || t.opt_type || '';
      const sym = (t.symbol||'').slice(-16);
      const reason = (t.exit_reason||'—').replace(/[^\x20-\x7E₹]/g,'').slice(0,40);
      return `<tr style="border-bottom:1px solid var(--bdr)">
        <td style="padding:5px 8px;color:var(--dim)">${t.date||''}</td>
        <td style="padding:5px 8px;font-family:monospace;font-size:10px">${sym}</td>
        <td style="padding:5px 8px"><span style="color:var(--info);font-size:10px;font-weight:700">${opt}</span></td>
        <td style="padding:5px 8px;text-align:right">₹${(t.buy_price||t.entry_price||0).toFixed(2)}</td>
        <td style="padding:5px 8px;text-align:right">₹${(t.sell_price||t.exit_price||0).toFixed(2)}</td>
        <td style="padding:5px 8px;text-align:right">${t.qty||0}</td>
        <td style="padding:5px 8px;text-align:right;color:${pc};font-weight:700">₹${pnl>=0?'+':''}${pnl.toFixed(2)}</td>
        <td style="padding:5px 8px;color:var(--dim);font-size:10px">${t.time_entry||'—'}</td>
        <td style="padding:5px 8px;color:var(--dim);font-size:10px">${t.time_exit||'—'}</td>
        <td style="padding:5px 8px;color:var(--dim);font-size:10px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.exit_reason||''}">${reason}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="10" style="padding:16px;text-align:center;color:var(--bear)">Error: ' + e + '</td></tr>';
  }
}

async function scRunFreshBacktest() {
  const expiry  = document.getElementById('sc-expiry') ? document.getElementById('sc-expiry').value : '';
  const pmMin   = parseFloat(document.getElementById('sc-prem-min') ? document.getElementById('sc-prem-min').value : 85) || 85;
  const pmMax   = parseFloat(document.getElementById('sc-prem-max') ? document.getElementById('sc-prem-max').value : 200) || 200;
  const lots    = parseInt(document.getElementById('sc-lots') ? document.getElementById('sc-lots').value : 18) || 18;
  const days    = parseInt(document.getElementById('sc-bt-days') ? document.getElementById('sc-bt-days').value : 31) || 31;
  const tbody   = document.getElementById('sc-bt-tbody');
  const sumEl   = document.getElementById('sc-bt-summary');
  const statEl  = document.getElementById('sc-bt-status');
  if (statEl) statEl.innerHTML = '<span style="color:var(--info)">&#8987; Running... (~1-3 min)</span>';
  tbody.innerHTML = '<tr><td colspan="10" style="padding:20px;text-align:center;color:var(--info)">&#8987; Running historical backtest via Groww API... this may take 1–3 minutes</td></tr>';
  try {
    const r = await fetch('/api/run_trendline_backtest', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({expiry, days, premium_min: pmMin, premium_max: pmMax, lots})
    });
    const d = await r.json();
    if (d.error) {
      tbody.innerHTML = '<tr><td colspan="10" style="padding:16px;text-align:center;color:var(--bear)">Error: ' + d.error + '</td></tr>';
      if (statEl) statEl.textContent = '';
      return;
    }
    if (statEl) statEl.innerHTML = '<span style="color:var(--bull)">&#10004; Done</span>';
    const trades = d.trades || [];
    if (!trades.length) {
      tbody.innerHTML = '<tr><td colspan="10" style="padding:16px;text-align:center;color:var(--dim)">No trades generated</td></tr>';
      if (sumEl) sumEl.textContent = '';
      return;
    }
    const totalPnl = trades.reduce((s,t)=>s+(t.pnl||0),0);
    const wins  = trades.filter(t=>(t.pnl||0)>0).length;
    const losses= trades.filter(t=>(t.pnl||0)<0).length;
    const wr    = Math.round(wins/trades.length*100);
    const pclr  = totalPnl>=0?'var(--bull)':'var(--bear)';
    if (sumEl) sumEl.innerHTML =
      `${trades.length} trades &nbsp; <span style="color:${pclr};font-weight:700">P&L ₹${totalPnl>=0?'+':''}${totalPnl.toFixed(2)}</span>` +
      ` &nbsp; <span style="color:var(--bull)">${wins}W</span> <span style="color:var(--bear)">${losses}L</span>` +
      ` &nbsp; WR ${wr}%`;
    tbody.innerHTML = trades.map(t => {
      const pnl = t.pnl || 0;
      const pc  = pnl>0?'var(--bull)':pnl<0?'var(--bear)':'var(--dim)';
      const opt = t.signal || t.play_type || t.opt_type || '';
      const sym = (t.symbol||'').slice(-16);
      const reason = (t.exit_reason||'—').replace(/[^\x20-\x7E₹]/g,'').slice(0,40);
      return `<tr style="border-bottom:1px solid var(--bdr)">
        <td style="padding:5px 8px;color:var(--dim)">${t.date||''}</td>
        <td style="padding:5px 8px;font-family:monospace;font-size:10px">${sym}</td>
        <td style="padding:5px 8px"><span style="color:#8b5cf6;font-size:10px;font-weight:700">${opt}</span></td>
        <td style="padding:5px 8px;text-align:right">₹${(t.entry_price||t.buy_price||0).toFixed(2)}</td>
        <td style="padding:5px 8px;text-align:right">₹${(t.exit_price||t.sell_price||0).toFixed(2)}</td>
        <td style="padding:5px 8px;text-align:right">${t.qty||0}</td>
        <td style="padding:5px 8px;text-align:right;color:${pc};font-weight:700">₹${pnl>=0?'+':''}${pnl.toFixed(2)}</td>
        <td style="padding:5px 8px;color:var(--dim);font-size:10px">${t.entry_time||'—'}</td>
        <td style="padding:5px 8px;color:var(--dim);font-size:10px">${t.exit_time||'—'}</td>
        <td style="padding:5px 8px;color:var(--dim);font-size:10px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.exit_reason||''}">${reason}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="10" style="padding:16px;text-align:center;color:var(--bear)">Error: ' + e + '</td></tr>';
    if (statEl) statEl.textContent = '';
  }
}
// ── END SCANNER TAB ──────────────────────────────────────────────────────────

var _botsRegistry = [];
var _botsStatus = {};

function initBotsTab(){
  if(_botsRegistry.length > 0){ refreshBotsStatus(); return; }
  fetch('/api/bot/registry').then(r=>r.json()).then(d=>{
    _botsRegistry = d.bots || [];
    renderBotCards();
    refreshBotsStatus();
    if(_botsTimer) clearInterval(_botsTimer);
    _botsTimer = setInterval(refreshBotsStatus, 4000);
  }).catch(()=>{});
}

function refreshBotsStatus(){
  fetch('/api/bot/status').then(r=>r.json()).then(status=>{
    _botsStatus = status;
    _botsRegistry.forEach(b=>{
      var s = status[b.id] || 'stopped';
      var card = document.getElementById('bc-'+b.id);
      if(!card) return;
      card.className = 'bot-card ' + s;
      var badge = document.getElementById('bb-'+b.id);
      if(badge){ badge.className='bot-badge '+s; badge.textContent=s==='running'?'RUNNING':'STOPPED'; }
      var startBtn = document.getElementById('bstart-'+b.id);
      var stopBtn  = document.getElementById('bstop-'+b.id);
      if(startBtn) startBtn.disabled = (s==='running');
      if(stopBtn)  stopBtn.disabled  = (s!=='running');
    });
    var el = document.getElementById('bots-last-refresh');
    if(el) el.textContent = 'Updated ' + new Date().toLocaleTimeString();
    // Only load logs for background bots (terminal bots output goes to their own window)
    _botsRegistry.forEach(b=>{ if(b.terminal===false && (status[b.id]||'stopped')==='running') loadBotLog(b.id); });
  }).catch(()=>{});
}

function loadBotLog(botId){
  fetch('/api/bot/logs?id='+botId+'&n=30').then(r=>r.json()).then(d=>{
    var el = document.getElementById('blog-'+botId);
    if(!el) return;
    var lines = d.lines||[];
    if(lines.length===0){ el.textContent='(no output yet)'; return; }
    el.textContent = lines.join('\\n');
    el.scrollTop = el.scrollHeight;
  }).catch(()=>{});
}

function botStart(id){
  var cfg = {};
  if(id === 'momentum'){
    var modeEl = document.querySelector('input[name="momentum-mode"]:checked');
    cfg.trade_mode = modeEl ? modeEl.value : 'paper';
  }
  fetch('/api/bot/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id, config:cfg})})
  .then(r=>r.json()).then(d=>{
    if(!d.ok){ alert('Could not start: '+(d.error||'unknown error')); }
    else{ setTimeout(refreshBotsStatus,1200); }
  }).catch(e=>alert('Error: '+e));
}

function botStop(id){
  fetch('/api/bot/stop',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id})})
  .then(r=>r.json()).then(d=>{
    if(!d.ok){ alert('Could not stop: '+(d.error||'unknown error')); }
    else{ setTimeout(refreshBotsStatus,1200); }
  }).catch(e=>alert('Error: '+e));
}

function botsStartAll(){
  _botsRegistry.forEach(b=>{
    if((_botsStatus[b.id]||'stopped')==='stopped') botStart(b.id);
  });
}

function botsStopAll(){
  _botsRegistry.forEach(b=>{
    if((_botsStatus[b.id]||'stopped')==='running') botStop(b.id);
  });
}

function renderBotCards(){
  var grid = document.getElementById('bots-grid');
  if(!grid) return;
  grid.innerHTML = '';
  _botsRegistry.forEach(function(b){
    var isTerminal = b.terminal !== false;
    var logHtml = isTerminal
      ? '<div class="bot-log" id="blog-'+b.id+'" style="color:var(--dim);font-style:italic">(opens in Terminal)</div>'
      : '<div class="bot-log" id="blog-'+b.id+'">(stopped)</div>';
    var html = '<div class="bot-card stopped" id="bc-'+b.id+'">';
    html += '<div class="bot-card-top">';
    html += '<span class="bot-badge stopped" id="bb-'+b.id+'">STOPPED</span>';
    html += '<div><div class="bot-name">'+b.name+'</div>';
    html += '<div class="bot-desc">'+b.desc+'</div></div>';
    html += '</div>';
    if(b.id === 'momentum'){
      html += '<div class="bot-mode-sel">';
      html += '<label class="mode-paper"><input type="radio" name="momentum-mode" value="paper" checked><span>&#128203; Paper</span></label>';
      html += '<label class="mode-mock"><input type="radio" name="momentum-mode" value="mock"><span>&#128242; Mock</span></label>';
      html += '<label class="mode-live"><input type="radio" name="momentum-mode" value="live"><span>&#9889; Live</span></label>';
      html += '</div>';
    }
    html += '<div class="bot-actions">';
    html += '<button class="bot-start-btn" id="bstart-'+b.id+'" onclick="botStart(&#39;'+b.id+'&#39;)">&#9654; Start</button>';
    html += '<button class="bot-stop-btn"  id="bstop-'+b.id+'"  onclick="botStop(&#39;'+b.id+'&#39;)"  disabled>&#9632; Stop</button>';
    html += '</div>';
    html += logHtml;
    html += '</div>';
    grid.insertAdjacentHTML('beforeend', html);
  });
}

/* ── Notification / Alert system ── */
let _notifMuted    = false;
let _notifUnread   = 0;
let _notifItems    = [];          // all accumulated alerts (newest last)
let _notifAudioCtx = null;
let _notifPollTimer = null;

// source → display label (overrides type label when source is known)
const _NOTIF_SRC_LABEL = {
  'PROD10':'PROD10', 'MOMENTUM':'AUTO BOT', 'MASTER':'MASTER SIG',
  'OI·FIBO':'OI·FIBO', 'OI·PCR':'OI·PCR', 'OI·SIGNAL':'OI SIGNAL',
  'OI·WRITER':'OI WRITER', 'PREMIUM':'PREMIUM', 'CONSENSUS':'CONSENSUS',
};
const _NOTIF_ICONS = {
  buy:         '📈', sell:        '📤', sl:          '🛑',
  target:      '🎯', profit:      '💰', loss:        '🔻',
  error:       '❌', signal_buy:  '🟢', signal_sell: '🔴',
};
const _NOTIF_LABELS = {
  buy:'BUY ENTRY', sell:'SELL/EXIT', sl:'SL HIT', target:'TARGET HIT',
  profit:'PROFIT CLOSE', loss:'LOSS CLOSE', error:'BOT ERROR',
  signal_buy:'BUY SIGNAL', signal_sell:'SELL SIGNAL',
};
// Source-specific sound overrides — CONSENSUS gets louder multi-note
const _NOTIF_SRC_SOUND = {
  'CONSENSUS': 'consensus', 'OI·SIGNAL': 'oi_signal',
};
const _NOTIF_NOTES = {
  buy:         [{f:587,d:.08},{f:740,d:.08},{f:880,d:.18}],
  sell:        [{f:784,d:.09},{f:659,d:.09},{f:523,d:.18}],
  sl:          [{f:330,d:.13,g:.9},{f:330,d:.13,g:.9},{f:262,d:.28,g:1}],
  target:      [{f:523,d:.07},{f:659,d:.07},{f:784,d:.07},{f:1047,d:.25}],
  profit:      [{f:523,d:.07},{f:659,d:.07},{f:784,d:.07},{f:1047,d:.25}],
  loss:        [{f:392,d:.16,g:.6},{f:311,d:.28,g:.5}],
  error:       [{f:220,d:.15,g:1},{f:185,d:.15,g:1},{f:220,d:.15,g:1},{f:185,d:.3,g:1}],
  // Consensus STRONG signal — louder 5-note fanfare
  consensus:   [{f:523,d:.07,g:.5},{f:659,d:.07,g:.5},{f:784,d:.07,g:.5},{f:1047,d:.12,g:.7},{f:1319,d:.25,g:.8}],
  // OI signal — double ping
  oi_signal:   [{f:880,d:.12,g:.4},{f:880,d:.18,g:.5}],
  signal_buy:  [{f:880,d:.18,g:.4}],
  signal_sell: [{f:659,d:.18,g:.4}],
};

function _notifSound(type, source){
  if(_notifMuted) return;
  try{
    if(!_notifAudioCtx) _notifAudioCtx = new (window.AudioContext||window.webkitAudioContext)();
    const ctx = _notifAudioCtx;
    if(ctx.state === 'suspended') ctx.resume();
    const soundKey = (source && _NOTIF_SRC_SOUND[source]) || type;
    const notes = _NOTIF_NOTES[soundKey] || _NOTIF_NOTES[type] || _NOTIF_NOTES.signal_buy;
    let off = ctx.currentTime + 0.05;
    notes.forEach(({f, d, g=0.35})=>{
      const osc  = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain); gain.connect(ctx.destination);
      osc.type = 'sine';
      osc.frequency.value = f;
      gain.gain.setValueAtTime(g, off);
      gain.gain.exponentialRampToValueAtTime(0.001, off + d);
      osc.start(off); osc.stop(off + d + 0.01);
      off += d + 0.025;
    });
  }catch(e){}
}

function notifToggleMute(){
  _notifMuted = !_notifMuted;
  const btn = $('notif-mute-btn');
  btn.textContent = _notifMuted ? '🔇 Muted' : '🔊 Sound ON';
  btn.classList.toggle('muted', _notifMuted);
}

function notifTogglePanel(){
  const p = $('notif-panel');
  const isOpen = p.classList.toggle('open');
  if(isOpen){
    _notifUnread = 0;
    const badge = $('notif-badge');
    badge.textContent = '0';
    badge.classList.remove('show');
  }
}

function notifClear(){
  _notifItems = [];
  _notifUnread = 0;
  $('notif-badge').classList.remove('show');
  $('notif-list').innerHTML = '<div class="notif-empty">No alerts yet — bots will notify you here</div>';
}

function _notifRender(alert){
  const type   = alert.type || 'buy';
  const icon   = _NOTIF_ICONS[type]  || '🔔';
  const label  = _NOTIF_LABELS[type] || type.toUpperCase();
  const src    = alert.source || '';
  const srcLbl = _NOTIF_SRC_LABEL[src] || src;
  const msg    = alert.msg   || '';
  const now    = new Date().toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const list   = $('notif-list');
  if(list.querySelector('.notif-empty')) list.innerHTML = '';
  const div = document.createElement('div');
  div.className = `notif-item nt-${type}`;
  div.innerHTML = `
    <div class="notif-icon">${icon}</div>
    <div class="notif-body">
      <div class="notif-source">${srcLbl} · ${label}</div>
      <div class="notif-msg">${msg.replace(/</g,'&lt;')}</div>
      <div class="notif-time">${now}</div>
    </div>`;
  list.insertBefore(div, list.firstChild);
  // keep max 60 items
  while(list.children.length > 60) list.removeChild(list.lastChild);
}

async function _notifPoll(){
  try{
    const r = await fetch('/api/alerts');
    const d = await r.json();
    const alerts = d.alerts || [];
    if(alerts.length){
      alerts.forEach(a=>{
        _notifItems.push(a);
        _notifRender(a);
        _notifSound(a.type, a.source);
      });
      // Only count unread when panel is closed
      if(!$('notif-panel').classList.contains('open')){
        _notifUnread += alerts.length;
        const badge = $('notif-badge');
        badge.textContent = _notifUnread > 99 ? '99+' : String(_notifUnread);
        badge.classList.add('show');
      }
    }
  }catch(e){}
}

function notifStart(){
  if(_notifPollTimer) return;
  _notifPoll();   // immediate first poll (initialises offsets, returns nothing)
  _notifPollTimer = setInterval(_notifPoll, 5000);
}

// Close panel on outside click
document.addEventListener('click', e=>{
  const panel = $('notif-panel');
  if(!panel) return;
  if(panel.classList.contains('open') && !panel.contains(e.target) && !e.target.closest('.notif-bell-wrap')){
    panel.classList.remove('open');
  }
});

// Start polling once page is ready
document.addEventListener('DOMContentLoaded', notifStart);

// ── OI Intraday Chart ─────────────────────────────────────────────────────────
let _oiChartVisible = false;
let _oiChartData    = [];   // oldest → newest
let _oiChartHovIdx  = -1;
let _oiChartEvtSet  = false;
let _oiChartTogs    = {ce:true, pe:true, pcr:true, spot:false, delta:true};

function _oiCrFmt(v){
  if(v == null) return '—';
  const a=Math.abs(v), s=v<0?'-':'';
  return a>=1e7?s+(a/1e7).toFixed(2)+'Cr':a>=1e5?s+(a/1e5).toFixed(1)+'L':s+Math.round(a)+'';
}

function oiChartOpen(){
  const modal = document.getElementById('oi-chart-modal');
  if(!modal) return;
  modal.style.display = 'flex';
  _oiChartVisible = true;
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(()=>{
    _oiChartResize();
    _oiChartData = (window._oiHistData || []).slice();
    _oiChartDrawFull();
    if(!_oiChartEvtSet){ _oiChartSetupEvents(); _oiChartEvtSet = true; }
  });
}
function oiChartClose(){
  const m = document.getElementById('oi-chart-modal');
  if(m) m.style.display = 'none';
  _oiChartVisible = false;
  document.body.style.overflow = '';
}
function _oiChartResize(){
  const wrap   = document.getElementById('oi-chart-wrap');
  const canvas = document.getElementById('oi-chart-canvas');
  if(!wrap || !canvas) return;
  const dpr  = window.devicePixelRatio || 1;
  const cssW = wrap.clientWidth;
  const cssH = wrap.clientHeight;
  // Physical pixels = CSS pixels × DPR — keeps text/lines crisp on HiDPI/Retina
  canvas.width       = Math.round(cssW * dpr);
  canvas.height      = Math.round(cssH * dpr);
  canvas.style.width  = cssW + 'px';
  canvas.style.height = cssH + 'px';
  // setTransform resets prior transforms then applies new scale — all drawing uses CSS pixel coords
  canvas.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
  canvas._cssW = cssW;
  canvas._cssH = cssH;
}
function oiChartToggle(key){
  _oiChartTogs[key] = !_oiChartTogs[key];
  const b = document.getElementById('oi-tog-'+key);
  if(b){ b.style.opacity = _oiChartTogs[key]?'1':'0.3'; b.style.textDecoration = _oiChartTogs[key]?'none':'line-through'; }
  _oiChartDrawFull();
}
function _oiChartDrawFull(){
  const canvas = document.getElementById('oi-chart-canvas');
  if(!canvas) return;
  const tEl = document.getElementById('oi-chart-ticks');
  if(tEl) tEl.textContent = _oiChartData.length ? _oiChartData.length+' ticks' : 'No data yet';
  if(!_oiChartData.length){
    const ctx = canvas.getContext('2d');
    const W = canvas._cssW || canvas.clientWidth || canvas.width;
    const H = canvas._cssH || canvas.clientHeight || canvas.height;
    ctx.fillStyle = '#080f1e'; ctx.fillRect(0,0,W,H);
    ctx.fillStyle = '#475569'; ctx.font = '13px JetBrains Mono,monospace';
    ctx.textAlign = 'center';
    ctx.fillText('No OI history yet — start calculate_oi_pcr.py to collect ticks', W/2, H/2);
    return;
  }
  _oiChartDraw(canvas, _oiChartData);
}

function _oiChartDraw(canvas, hist){
  const ctx = canvas.getContext('2d');
  const W = canvas._cssW || canvas.clientWidth || canvas.width;
  const H = canvas._cssH || canvas.clientHeight || canvas.height;
  ctx.clearRect(0,0,W,H);
  const n = hist.length;

  // ── Layout: 3 panels stacked — OI | PCR | Net Flow ──
  const ML=72, MR=12, MT=32, MB=26, GAP=7;
  const usableH = H - MT - MB - GAP*2;
  const oiH   = Math.floor(usableH * 0.50);
  const pcrH  = Math.floor(usableH * 0.28);
  const flowH = usableH - oiH - pcrH;

  const oiTop   = MT,                oiBot   = oiTop  + oiH;
  const pcrTop  = oiBot  + GAP,      pcrBot  = pcrTop + pcrH;
  const flowTop = pcrBot + GAP,      flowBot = flowTop + flowH;

  const xL=ML, xR=W-MR, plotW=xR-xL;
  const xOf = i => n<2 ? xL+plotW/2 : xL+(i/(n-1))*plotW;

  // ── Data ──
  const ceArr  = hist.map(h=>h.total_oi_ce ||0);
  const peArr  = hist.map(h=>h.total_oi_pe ||0);
  const pcrArr = hist.map(h=>h.pcr_all     ||0);
  const cgArr  = hist.map(h=>h.total_chg_ce||0);
  const pgArr  = hist.map(h=>h.total_chg_pe||0);
  const spArr  = hist.map(h=>h.price       ||0);

  const validPCR = pcrArr.filter(v=>v>0);
  const validSp  = spArr.filter(v=>v>0);

  // OI range (CE and PE on same axis — they're the same unit)
  const allOI = [...ceArr,...peArr].filter(v=>v>0);
  const minOI = allOI.length ? Math.min(...allOI) : 0;
  const maxOI = allOI.length ? Math.max(...allOI) : 1;
  const oiPad = (maxOI-minOI)*0.10 || 5e5;
  const oi_lo = minOI-oiPad, oi_hi = maxOI+oiPad;

  // PCR range
  const pcr_lo = validPCR.length ? Math.max(0.4, Math.min(...validPCR)-0.15) : 0.5;
  const pcr_hi = validPCR.length ? Math.max(2.2, Math.max(...validPCR)+0.15) : 2.2;

  // Net flow (PE change - CE change): positive = bullish, negative = bearish
  const netFlow = hist.map((_,i)=>(pgArr[i]||0)-(cgArr[i]||0));
  const maxFlow = Math.max(1e5,...netFlow.map(v=>Math.abs(v)));

  // Y-mappers (all in CSS pixels)
  const yOI   = v => oiBot  - Math.max(0,Math.min(1,(v-oi_lo)/(oi_hi-oi_lo)))*oiH;
  const yPCR  = v => pcrBot - Math.max(0,Math.min(1,(v-pcr_lo)/(pcr_hi-pcr_lo)))*pcrH;
  const flowMid = (flowTop+flowBot)/2;
  const yFlow = v => flowMid - Math.max(-1,Math.min(1,v/maxFlow))*(flowH/2-2);

  // ── Fill backgrounds ──
  ctx.fillStyle='#070d1c'; ctx.fillRect(0,0,W,H);
  ctx.fillStyle='#0b1626'; ctx.fillRect(xL,oiTop,  plotW,oiH);
  ctx.fillStyle='#090e1c'; ctx.fillRect(xL,pcrTop, plotW,pcrH);
  ctx.fillStyle='#080d1b'; ctx.fillRect(xL,flowTop,plotW,flowH);

  // ── PCR zone background (colored per tick) ──
  if(_oiChartTogs.pcr && n>=1){
    const sw = n<2 ? plotW : plotW/(n-1);
    hist.forEach((h,i)=>{
      const pcr=h.pcr_all||0; if(!pcr) return;
      ctx.fillStyle = pcr>=1.2?'rgba(74,222,128,0.10)':pcr<=0.8?'rgba(248,113,113,0.10)':'rgba(251,191,36,0.07)';
      ctx.fillRect(xOf(i)-sw/2, pcrTop, sw+1, pcrH);
    });
  }

  // ── Grid lines ──
  ctx.strokeStyle='#101d30'; ctx.lineWidth=1;
  for(let i=1;i<=5;i++){ const y=oiTop+(i/5)*oiH;   ctx.beginPath(); ctx.moveTo(xL,y); ctx.lineTo(xR,y); ctx.stroke(); }
  for(let i=1;i<=4;i++){ const y=pcrTop+(i/4)*pcrH;  ctx.beginPath(); ctx.moveTo(xL,y); ctx.lineTo(xR,y); ctx.stroke(); }
  // vertical grid
  const vStep=Math.max(1,Math.round(n/8));
  ctx.strokeStyle='#0e1a2c';
  for(let i=0;i<n;i+=vStep){
    const x=xOf(i);
    [oiTop,oiBot,pcrTop,pcrBot,flowTop,flowBot].forEach((y,j)=>{
      if(j%2===0){ ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(x,y+(j===0?oiH:j===2?pcrH:flowH)); ctx.stroke(); }
    });
  }

  // ── PCR reference lines ──
  if(_oiChartTogs.pcr){
    [[1.2,'#4ade8055'],[1.0,'#fbbf2455'],[0.8,'#f8717155']].forEach(([pv,clr])=>{
      if(pv<pcr_lo||pv>pcr_hi) return;
      const y=yPCR(pv);
      ctx.strokeStyle=clr; ctx.lineWidth=1; ctx.setLineDash([3,4]);
      ctx.beginPath(); ctx.moveTo(xL,y); ctx.lineTo(xR,y); ctx.stroke(); ctx.setLineDash([]);
    });
  }

  // ── Net flow zero line ──
  ctx.strokeStyle='#1c3050'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(xL,flowMid); ctx.lineTo(xR,flowMid); ctx.stroke();

  // ── Draw helper ──
  function drawLine(pts,clr,lw,dash=[]){
    const vp=pts.filter(p=>p&&p.y!=null&&!isNaN(p.y));
    if(vp.length<2) return;
    ctx.strokeStyle=clr; ctx.lineWidth=lw; ctx.setLineDash(dash);
    ctx.shadowColor=clr; ctx.shadowBlur=6;
    ctx.beginPath(); vp.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
    ctx.stroke(); ctx.shadowBlur=0; ctx.setLineDash([]);
  }

  // ── CE vs PE dominance fill (gap between the two lines) ──
  if(_oiChartTogs.ce && _oiChartTogs.pe && n>=2){
    for(let i=0;i<n-1;i++){
      const x1=xOf(i), x2=xOf(i+1);
      const cY1=yOI(ceArr[i]),  cY2=yOI(ceArr[i+1]);
      const pY1=yOI(peArr[i]),  pY2=yOI(peArr[i+1]);
      const bullish = (peArr[i]+peArr[i+1]) > (ceArr[i]+ceArr[i+1]);
      ctx.fillStyle = bullish ? 'rgba(74,222,128,0.11)' : 'rgba(248,113,113,0.09)';
      ctx.beginPath();
      ctx.moveTo(x1,cY1); ctx.lineTo(x2,cY2); // CE top path
      ctx.lineTo(x2,pY2); ctx.lineTo(x1,pY1);  // PE reverse
      ctx.closePath(); ctx.fill();
    }
  }

  // ── CE OI line ──
  if(_oiChartTogs.ce){
    const pts=ceArr.map((v,i)=>({x:xOf(i),y:yOI(v)}));
    drawLine(pts,'#f87171',2.2);
    if(n>=1){
      const p=pts[n-1];
      ctx.fillStyle='#f87171'; ctx.beginPath(); ctx.arc(p.x,p.y,4,0,Math.PI*2); ctx.fill();
      // value label right of endpoint
      ctx.fillStyle='#f87171cc'; ctx.font='bold 9px JetBrains Mono,monospace'; ctx.textAlign='left';
      ctx.fillText(_oiCrFmt(ceArr[n-1]), Math.min(p.x+7, xR-38), p.y+3);
    }
  }

  // ── PE OI line ──
  if(_oiChartTogs.pe){
    const pts=peArr.map((v,i)=>({x:xOf(i),y:yOI(v)}));
    drawLine(pts,'#4ade80',2.2);
    if(n>=1){
      const p=pts[n-1];
      ctx.fillStyle='#4ade80'; ctx.beginPath(); ctx.arc(p.x,p.y,4,0,Math.PI*2); ctx.fill();
      ctx.fillStyle='#4ade80cc'; ctx.font='bold 9px JetBrains Mono,monospace'; ctx.textAlign='left';
      ctx.fillText(_oiCrFmt(peArr[n-1]), Math.min(p.x+7, xR-38), p.y+3);
    }
  }

  // ── PCR line ──
  if(_oiChartTogs.pcr){
    const pts=hist.map((h,i)=>({x:xOf(i),y:h.pcr_all>0?yPCR(h.pcr_all):null})).filter(p=>p.y!=null);
    drawLine(pts,'#fbbf24',2.2);
    if(pts.length){
      const p=pts[pts.length-1], pv=validPCR[validPCR.length-1]||0;
      const pClr=pv>=1.2?'#4ade80':pv<=0.8?'#f87171':'#fbbf24';
      ctx.fillStyle=pClr; ctx.beginPath(); ctx.arc(p.x,p.y,4,0,Math.PI*2); ctx.fill();
      ctx.fillStyle=pClr; ctx.font='bold 10px JetBrains Mono,monospace'; ctx.textAlign='left';
      ctx.fillText(pv.toFixed(2), Math.min(p.x+7,xR-32), p.y+3);
    }
  }

  // ── Net OI Flow bars ──
  if(_oiChartTogs.delta){
    const bw=Math.max(5, Math.floor(plotW/n*0.72));
    netFlow.forEach((v,i)=>{
      if(!v) return;
      const x=xOf(i), y1=yFlow(v), y2=flowMid;
      const top=Math.min(y1,y2), bh=Math.max(2,Math.abs(y1-y2));
      ctx.fillStyle = v>0 ? 'rgba(74,222,128,0.90)' : 'rgba(248,113,113,0.90)';
      ctx.fillRect(x-bw/2, top, bw, bh);
    });
  }

  // ── Panel labels (top-left of each panel) ──
  ctx.font='8.5px JetBrains Mono,monospace'; ctx.textAlign='left';
  if(_oiChartTogs.ce||_oiChartTogs.pe){
    ctx.fillStyle='#2d4a6a';
    ctx.fillText('CE OI  /  PE OI', xL+7, oiTop+12);
  }
  if(_oiChartTogs.pcr){
    ctx.fillStyle='#3d4a1a';
    ctx.fillText('PCR', xL+7, pcrTop+12);
  }
  if(_oiChartTogs.delta){
    ctx.fillStyle='#2d4a3a'; ctx.fillText('NET OI FLOW', xL+7, flowTop+12);
    ctx.fillStyle='#4ade8055'; ctx.fillText('▲ PE builds', xL+92, flowTop+12);
    ctx.fillStyle='#f8717155'; ctx.fillText('▼ CE builds', xL+167, flowTop+12);
  }

  // ── Left Y-axis: OI ──
  ctx.fillStyle='#3d5575'; ctx.textAlign='right'; ctx.font='9px JetBrains Mono,monospace';
  for(let i=0;i<=4;i++){
    const v=oi_lo+(oi_hi-oi_lo)*(1-i/4);
    ctx.fillText(_oiCrFmt(v), xL-5, oiTop+(i/4)*oiH+3);
  }
  // Left Y-axis: PCR
  if(_oiChartTogs.pcr){
    ctx.fillStyle='#4d4020';
    [[1.2,'#4ade8070'],[1.0,'#fbbf2470'],[0.8,'#f8717170']].forEach(([pv,clr])=>{
      if(pv>=pcr_lo&&pv<=pcr_hi){
        ctx.fillStyle=clr; ctx.fillText(pv.toFixed(1), xL-5, yPCR(pv)+3);
      }
    });
  }
  // Net flow: 0 label
  if(_oiChartTogs.delta){
    ctx.fillStyle='#2d4060'; ctx.textAlign='right'; ctx.font='8px JetBrains Mono,monospace';
    ctx.fillText('0', xL-5, flowMid+3);
  }

  // ── X-axis time labels ──
  ctx.fillStyle='#3d5575'; ctx.textAlign='center'; ctx.font='9px JetBrains Mono,monospace';
  const lStep=Math.max(1,Math.round(n/8));
  for(let i=1;i<n-1;i+=lStep) ctx.fillText(hist[i].time||'', xOf(i), flowBot+15);
  if(n>=1){ ctx.fillStyle='#38bdf870'; ctx.fillText(hist[0].time||'',    xOf(0),   flowBot+15); }
  if(n>=2){ ctx.fillStyle='#38bdf8';   ctx.fillText(hist[n-1].time||'',  xOf(n-1), flowBot+15); }

  // ── Spot label top-right ──
  if(validSp.length){
    const sp=validSp[validSp.length-1];
    ctx.fillStyle='#38bdf8cc'; ctx.font='10px JetBrains Mono,monospace'; ctx.textAlign='right';
    ctx.fillText('Spot  '+sp.toLocaleString('en-IN',{maximumFractionDigits:1}), xR, MT-6);
  }

  // ── Legend (top-left of chart) ──
  const leg=[{k:'ce',l:'CE OI',c:'#f87171'},{k:'pe',l:'PE OI',c:'#4ade80'},
             {k:'pcr',l:'PCR',c:'#fbbf24'},{k:'delta',l:'Net Flow',c:'#6ee7b7'}];
  ctx.font='9px JetBrains Mono,monospace'; ctx.textAlign='left';
  let lx=xL, ly=MT-6;
  leg.forEach(g=>{
    ctx.globalAlpha=_oiChartTogs[g.k]?1:0.3;
    ctx.strokeStyle=g.c; ctx.lineWidth=2; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(lx,ly); ctx.lineTo(lx+11,ly); ctx.stroke();
    ctx.fillStyle='#94a3b8'; ctx.fillText(g.l, lx+14, ly+3);
    lx+=14+ctx.measureText(g.l).width+12; ctx.globalAlpha=1;
  });

  // ── Panel borders ──
  ctx.strokeStyle='#182840'; ctx.lineWidth=1;
  ctx.strokeRect(xL,oiTop,  plotW,oiH);
  ctx.strokeRect(xL,pcrTop, plotW,pcrH);
  ctx.strokeRect(xL,flowTop,plotW,flowH);

  // ── Hover crosshair ──
  if(_oiChartHovIdx>=0&&_oiChartHovIdx<n){
    const i=_oiChartHovIdx, x=xOf(i), hd=hist[i];
    ctx.strokeStyle='rgba(255,255,255,0.14)'; ctx.lineWidth=1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(x,oiTop); ctx.lineTo(x,flowBot); ctx.stroke(); ctx.setLineDash([]);
    const dr=5;
    if(_oiChartTogs.ce&&hd.total_oi_ce){ ctx.fillStyle='#f87171'; ctx.beginPath(); ctx.arc(x,yOI(hd.total_oi_ce),dr,0,Math.PI*2); ctx.fill(); }
    if(_oiChartTogs.pe&&hd.total_oi_pe){ ctx.fillStyle='#4ade80'; ctx.beginPath(); ctx.arc(x,yOI(hd.total_oi_pe),dr,0,Math.PI*2); ctx.fill(); }
    if(_oiChartTogs.pcr&&hd.pcr_all>0){ ctx.fillStyle='#fbbf24'; ctx.beginPath(); ctx.arc(x,yPCR(hd.pcr_all),4,0,Math.PI*2); ctx.fill(); }
  }

  // Store layout for hover events
  canvas._oiLayout={xL,xR,n,xOf,hist,fn:{yOI,yPCR,fmtCr:_oiCrFmt},
    ranges:{oi_lo,oi_hi,pcr_lo,pcr_hi}};
}

function _oiChartSetupEvents(){
  const canvas = document.getElementById('oi-chart-canvas');
  const tt     = document.getElementById('oi-chart-tt');
  if(!canvas||!tt) return;

  canvas.addEventListener('mousemove', function(e){
    const lay=canvas._oiLayout; if(!lay) return;
    const rect=canvas.getBoundingClientRect();
    const mx=e.clientX-rect.left;  // already CSS pixels — DPR handled by ctx.setTransform
    const {xL,xR,n,xOf,hist}=lay;
    if(mx<xL||mx>xR){ _oiChartHovIdx=-1; _oiChartDrawFull(); tt.style.display='none'; return; }
    const idx=Math.max(0,Math.min(n-1,Math.round(((mx-xL)/(xR-xL))*(n-1))));
    if(idx!==_oiChartHovIdx){
      _oiChartHovIdx=idx; _oiChartDrawFull();
    }
    // Always update tooltip (position may change without index change)
    const h=hist[idx], prev=idx>0?hist[idx-1]:null;
    const dCE=prev?h.total_oi_ce-prev.total_oi_ce:null;
    const dPE=prev?h.total_oi_pe-prev.total_oi_pe:null;
    const pcr=h.pcr_all||0;
    const pcrClr=pcr>=1.2?'#4ade80':pcr<=0.8?'#f87171':'#fbbf24';
    const sc={BULLISH:'#4ade80',BEARISH:'#f87171',NEUTRAL:'#fbbf24'};
    const sentC=sc[h.sentiment]||'#94a3b8', wC=sc[h.writer_bias]||'#94a3b8';
    const dArr=(v,uc,dc)=>v==null?'':` <span style="color:${v>0?uc:dc}">${v>0?'▲':'▼'}${_oiCrFmt(Math.abs(v))}</span>`;
    const ceChgClr=(h.total_chg_ce||0)>0?'#f87171':'#4ade80';
    const peChgClr=(h.total_chg_pe||0)>0?'#4ade80':'#f87171';
    tt.innerHTML=`
      <div style="font-size:10px;font-weight:700;color:#38bdf8;margin-bottom:5px;border-bottom:1px solid #1e3058;padding-bottom:4px">
        ${h.time||'—'} <span style="color:#475569;font-weight:400;font-size:9px">tick ${idx+1}/${n}</span>
      </div>
      <table style="border-collapse:collapse;font-size:10px;line-height:1.6">
        <tr><td style="color:#64748b;padding-right:10px">Spot</td><td style="color:#38bdf8">${h.price?(+h.price).toLocaleString('en-IN',{minimumFractionDigits:1,maximumFractionDigits:1}):'—'}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">CE OI</td><td style="color:#f87171">${_oiCrFmt(h.total_oi_ce||0)}${dArr(dCE,'#f87171','#4ade80')}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">PE OI</td><td style="color:#4ade80">${_oiCrFmt(h.total_oi_pe||0)}${dArr(dPE,'#4ade80','#f87171')}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">PCR ALL</td><td style="color:${pcrClr}">${pcr.toFixed(3)}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">PCR ATM</td><td style="color:${pcrClr}">${(h.pcr_atm||0).toFixed(3)}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">CE Δ session</td><td style="color:${ceChgClr}">${_oiCrFmt(h.total_chg_ce||0)}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">PE Δ session</td><td style="color:${peChgClr}">${_oiCrFmt(h.total_chg_pe||0)}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">Sentiment</td><td style="color:${sentC}">${h.sentiment||'—'}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">Writer Bias</td><td style="color:${wC}">${h.writer_bias||'—'}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">Market Sig</td><td style="color:#cbd5e1;font-size:9px">${h.market_signal||'—'}</td></tr>
        <tr><td style="color:#64748b;padding-right:10px">🟢/🔴</td><td><span style="color:#4ade80">${h.bull_score_v2||0}</span><span style="color:#64748b">/</span><span style="color:#f87171">${h.bear_score_v2||0}</span></td></tr>
      </table>`;
    const mr=document.getElementById('oi-chart-modal').getBoundingClientRect();
    const tx=e.clientX-mr.left+14, ty=e.clientY-mr.top-60;
    const ttW=215, ttH=310;
    tt.style.left=(tx+ttW>mr.width-10?tx-ttW-22:tx)+'px';
    tt.style.top=Math.max(4,Math.min(mr.height-ttH-4,ty))+'px';
    tt.style.display='block';
  });
  canvas.addEventListener('mouseleave',()=>{ _oiChartHovIdx=-1; _oiChartDrawFull(); tt.style.display='none'; });
  window.addEventListener('resize',()=>{ if(_oiChartVisible){ _oiChartResize(); _oiChartDrawFull(); } });
}

</script>

<!-- OI Intraday Chart Modal -->
<div id="oi-chart-modal" onclick="if(event.target===this)oiChartClose()">
  <div id="oi-chart-inner">
    <div id="oi-chart-hdr">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:13px;font-weight:700;color:#e2e8f0">📊 OI Intraday Chart</span>
        <span id="oi-chart-ticks" style="font-size:10px;color:#64748b;font-family:'JetBrains Mono',monospace">—</span>
        <span style="font-size:9px;color:#334155">3 panels: OI levels · PCR zone · Net flow — hover for values</span>
      </div>
      <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap">
        <button id="oi-tog-ce"    class="oi-tog-btn" onclick="oiChartToggle('ce')"    style="background:#f8717118;border:1px solid #f87171;color:#f87171">CE OI</button>
        <button id="oi-tog-pe"    class="oi-tog-btn" onclick="oiChartToggle('pe')"    style="background:#4ade8018;border:1px solid #4ade80;color:#4ade80">PE OI</button>
        <button id="oi-tog-pcr"   class="oi-tog-btn" onclick="oiChartToggle('pcr')"   style="background:#fbbf2418;border:1px solid #fbbf24;color:#fbbf24">PCR</button>
        <button id="oi-tog-delta" class="oi-tog-btn" onclick="oiChartToggle('delta')" style="background:#6ee7b718;border:1px solid #6ee7b7;color:#6ee7b7">Net Flow</button>
        <div style="width:1px;height:14px;background:#1e3058;margin:0 3px"></div>
        <button onclick="oiChartClose()" style="font-size:10px;padding:3px 10px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#94a3b8;cursor:pointer">✕</button>
      </div>
    </div>
    <div id="oi-chart-wrap">
      <canvas id="oi-chart-canvas"></canvas>
      <div id="oi-chart-tt"></div>
    </div>
  </div>
</div>

<!-- ── Personal Trading AI Modal ───────────────────────────────────────── -->
<div id="pai-modal" onclick="if(event.target===this)paiClose()"
  style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:1200;align-items:center;justify-content:center;backdrop-filter:blur(6px)">
  <div style="background:#080f1e;border:1px solid #3730a3;border-radius:12px;width:88vw;max-width:860px;height:87vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 30px 90px #000c">

    <!-- header -->
    <div style="display:flex;align-items:center;justify-content:space-between;padding:11px 16px;border-bottom:1px solid #1e1b4b;flex-shrink:0;gap:10px">
      <div style="display:flex;align-items:center;gap:12px">
        <span style="font-size:14px;font-weight:700;color:#e2e8f0">🧠 Personal Trading AI — Pre-Market Check</span>
        <span id="pai-ts" style="font-size:10px;color:#475569;font-family:'JetBrains Mono',monospace"></span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <button onclick="paiRun()" id="pai-run-btn"
          style="font-size:11px;padding:4px 14px;background:#1e1b4b;border:1px solid #6366f1;border-radius:6px;color:#a5b4fc;cursor:pointer;font-family:'JetBrains Mono',monospace">
          ▶ Run Check
        </button>
        <button onclick="paiClose()" style="font-size:11px;padding:4px 10px;background:#1e293b;border:1px solid #334155;border-radius:6px;color:#94a3b8;cursor:pointer">✕</button>
      </div>
    </div>

    <!-- score bar -->
    <div id="pai-score-bar" style="flex-shrink:0;padding:14px 18px;border-bottom:1px solid #1e1b4b;display:none">
      <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
        <div style="display:flex;align-items:baseline;gap:8px">
          <span style="font-size:11px;color:#64748b;font-family:'JetBrains Mono',monospace">PERMISSION SCORE</span>
          <span id="pai-score-num" style="font-size:38px;font-weight:800;font-family:'JetBrains Mono',monospace">—</span>
          <span style="font-size:18px;color:#475569">/100</span>
        </div>
        <div id="pai-verdict-badge" style="font-size:13px;font-weight:700;padding:5px 16px;border-radius:20px;font-family:'JetBrains Mono',monospace">—</div>
        <div id="pai-verdict-msg" style="font-size:12px;color:#94a3b8;max-width:400px;line-height:1.5"></div>
      </div>
    </div>

    <!-- output body -->
    <div id="pai-body" style="flex:1;overflow-y:auto;padding:14px 18px">
      <div id="pai-placeholder" style="color:#475569;font-size:13px;text-align:center;padding:60px 0">
        Click <b style="color:#a5b4fc">▶ Run Check</b> to fetch today's pre-market analysis.<br>
        <span style="font-size:11px;color:#334155">Takes ~30 seconds — fetches VIX, NIFTY, PCR and analyses your 3-year trade history.</span>
      </div>
      <pre id="pai-output" style="display:none;font-family:'JetBrains Mono',monospace;font-size:11px;color:#94a3b8;white-space:pre-wrap;word-break:break-word;line-height:1.6;margin:0"></pre>
    </div>

  </div>
</div>

<script>
var _paiPollTimer = null;

function paiOpen(){
  document.getElementById('pai-modal').style.display='flex';
  document.body.style.overflow='hidden';
  // If we already have a cached result, show it
  fetch('/api/personal_ai').then(r=>r.json()).then(d=>{
    if(d.output) _paiShowResult(d);
    else if(d.running) _paiStartPolling();
  }).catch(()=>{});
}
function paiClose(){
  document.getElementById('pai-modal').style.display='none';
  document.body.style.overflow='';
  if(_paiPollTimer){ clearInterval(_paiPollTimer); _paiPollTimer=null; }
}
function paiRun(){
  const btn=document.getElementById('pai-run-btn');
  btn.disabled=true; btn.textContent='⏳ Running…';
  document.getElementById('pai-placeholder').style.display='block';
  document.getElementById('pai-placeholder').innerHTML='<span style="color:#a5b4fc">⏳ Running analysis… (~30 seconds)</span><br><span style="font-size:11px;color:#334155">Fetching VIX · NIFTY · PCR · 3-year history…</span>';
  document.getElementById('pai-output').style.display='none';
  document.getElementById('pai-score-bar').style.display='none';
  fetch('/api/personal_ai/run',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.status==='started'||d.status==='already_running') _paiStartPolling();
  }).catch(()=>{ btn.disabled=false; btn.textContent='▶ Run Check'; });
}
function _paiStartPolling(){
  if(_paiPollTimer) clearInterval(_paiPollTimer);
  _paiPollTimer=setInterval(()=>{
    fetch('/api/personal_ai').then(r=>r.json()).then(d=>{
      if(!d.running && d.output){
        clearInterval(_paiPollTimer); _paiPollTimer=null;
        _paiShowResult(d);
      }
    }).catch(()=>{});
  }, 3000);
}
function _paiShowResult(d){
  const btn=document.getElementById('pai-run-btn');
  btn.disabled=false; btn.textContent='↺ Re-run';

  // Score bar
  const s=d.score, v=d.verdict||'';
  const clr = s==null?'#64748b':s>=81?'#4ade80':s>=61?'#a3e635':s>=41?'#fbbf24':'#f87171';
  const vMap={NO_TRADE:{bg:'#3f0f0f',c:'#f87171',msg:'Do NOT trade today. High risk day based on your history.'},
              CAUTION: {bg:'#3f2d0a',c:'#fbbf24',msg:'Caution — only take ★★★★★ Fibonacci zone setups.'},
              NORMAL:  {bg:'#0f2a1a',c:'#4ade80',msg:'Normal trading day. Follow your 3-check rule.'},
              HIGH_CONFIDENCE:{bg:'#0a1f2e',c:'#38bdf8',msg:'High confidence day. Trade your full plan.'}};
  const vm=vMap[v]||{bg:'#1e293b',c:'#94a3b8',msg:''};
  if(s!=null){
    document.getElementById('pai-score-bar').style.display='';
    document.getElementById('pai-score-num').textContent=s;
    document.getElementById('pai-score-num').style.color=clr;
    const vb=document.getElementById('pai-verdict-badge');
    vb.textContent=v.replace(/_/g,' ');
    vb.style.cssText+=`;background:${vm.bg};color:${vm.c};border:1px solid ${vm.c}40`;
    document.getElementById('pai-verdict-msg').textContent=vm.msg;
  }

  // Full text output
  document.getElementById('pai-placeholder').style.display='none';
  const pre=document.getElementById('pai-output');
  pre.style.display='block';
  pre.textContent=d.output||(d.error?'Script returned an error. Check terminal.':'No output.');

  // Timestamp
  if(d.ts) document.getElementById('pai-ts').textContent='Last run: '+d.ts;
}

// ── AI Brain ──────────────────────────────────────────────
let _mbAiEnabled = false;
let _mbAiPollTimer = null;
let _mbAiAutoTimer = null;
let _qsRefreshTimer = null;
let _qsPollTimer    = null;

function _qsApplyToggleState(enabled){
  const btn    = document.getElementById('qs-toggle-btn');
  const rBtn   = document.getElementById('qs-refresh-btn');
  const el     = document.getElementById('qs-text');
  if(btn){
    btn.textContent = enabled ? 'ON' : 'OFF';
    btn.classList.toggle('toggle-on', enabled);
    btn.classList.toggle('toggle-off', !enabled);
  }
  if(rBtn) rBtn.style.display = enabled ? '' : 'none';
  if(!enabled && el){
    el.innerHTML = '<span style="color:var(--dim)">Toggle ON to generate a live market summary.</span>';
    const tsEl = document.getElementById('qs-ts');
    if(tsEl) tsEl.textContent = '';
    _qsSetDebug('', '');
  }
}

function qsToggle(){
  fetch('/api/toggle?f=qs_ai').then(r=>r.json()).then(feat=>{
    const enabled = feat.qs_ai === true;
    _qsApplyToggleState(enabled);
    if(enabled){
      // Auto-start a generation immediately after turning ON
      qsRefresh();
    } else {
      // Stop any in-progress poll
      if(_qsPollTimer){ clearInterval(_qsPollTimer); _qsPollTimer=null; }
      if(_qsRefreshTimer){ clearInterval(_qsRefreshTimer); _qsRefreshTimer=null; }
    }
  }).catch(()=>{});
}

function _mbAiUpdateQuickSummary(d){
  const el    = document.getElementById('qs-text');
  const tsEl  = document.getElementById('qs-ts');
  const btnEl = document.getElementById('qs-refresh-btn');
  if(!el) return;

  // If feature is disabled, show the OFF state and stop
  if(d && d.qs_ai_enabled === false){
    _qsApplyToggleState(false);
    return;
  }

  const qs     = (d && d.qs) ? d.qs : {};
  const status = qs.status || 'idle';
  _qsSetDebug(status, qs.ts || '');

  if(status === 'running'){
    el.innerHTML = '<span style="display:inline-flex;align-items:center;gap:8px;color:#c084fc">'
      + '<span class="mb-ai-spinner" style="width:14px;height:14px;border-width:2px"></span>'
      + 'Claude is analysing market data…</span>';
    if(tsEl) tsEl.textContent = '';
    if(btnEl){ btnEl.disabled=true; btnEl.textContent='⏳'; }
    if(!_qsPollTimer) _qsPollTimer = setInterval(_qsPoll, 2500);
    return;
  }

  // Not running — clear poll timer, re-enable button
  if(_qsPollTimer){ clearInterval(_qsPollTimer); _qsPollTimer=null; }
  if(btnEl){ btnEl.disabled=false; btnEl.textContent='↻'; }

  if(status === 'no_cli'){
    el.innerHTML = '<span style="color:var(--warn)">Claude CLI not found — run: <code>npm install -g @anthropic-ai/claude-code</code> then <code>claude login</code></span>';
    return;
  }
  if(status === 'error'){
    el.innerHTML = '<span style="color:var(--bear)">⚠ Error: ' + (qs.error||'unknown') + '</span>';
    return;
  }
  if(status === 'no_data' || status === 'idle'){
    el.innerHTML = '<span style="color:var(--dim)">Waiting for OI data — start <code>calculate_oi_pcr.py</code>, then click ↻ to generate.</span>';
    return;
  }
  if(status === 'ok'){
    const txt = (qs.text||'').trim();
    if(!txt){
      el.innerHTML = '<span style="color:var(--dim)">Claude returned an empty response — click ↻ to retry.</span>';
      return;
    }
    const safe = txt.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const html = safe
      .replace(/\\b(Bullish|bullish)\\b/g, '<b style="color:#4ade80">$1</b>')
      .replace(/\\b(Bearish|bearish)\\b/g, '<b style="color:#f87171">$1</b>')
      .replace(/\\b(Sideways|sideways)\\b/g, '<b style="color:#facc15">$1</b>')
      .replace(/(₹[\\d,.]+(?:Cr)?)/g, '<b style="color:#38bdf8">$1</b>')
      .replace(/\\b(\\d{5})\\b/g, '<b style="color:#c084fc">$1</b>')
      .replace(/\\b(BUY CE|Buy CE)\\b/g, '<b style="color:#4ade80">$1</b>')
      .replace(/\\b(BUY PE|Buy PE)\\b/g, '<b style="color:#f87171">$1</b>')
      .replace(/\\b(STAY CASH|Stay Cash)\\b/g, '<b style="color:#facc15">$1</b>')
      .replace(/(Action:)/g, '<b style="color:#f97316">$1</b>')
      .replace(/\\n/g, '<br>');
    el.innerHTML = html;
    if(tsEl) tsEl.textContent = qs.ts ? '🤖 ' + qs.ts.replace('T',' ') : '';
    return;
  }
  // Fallback — unrecognised status
  el.innerHTML = '<span style="color:var(--dim)">Status: ' + status + ' — click ↻ to refresh.</span>';
}

function _qsSetDebug(status, ts){
  const dbg = document.getElementById('qs-status-dbg');
  if(dbg) dbg.textContent = 'status: ' + status + (ts ? '  |  ' + ts : '');
}

function _qsPoll(){
  fetch('/api/mb_ai').then(r=>r.json()).then(d=>{
    _mbAiUpdateQuickSummary(d);
    if((d.qs||{}).status !== 'running'){
      if(_qsPollTimer){ clearInterval(_qsPollTimer); _qsPollTimer=null; }
    }
  }).catch(()=>{});
}

function qsRefresh(){
  const btnEl = document.getElementById('qs-refresh-btn');
  if(btnEl){ btnEl.disabled=true; btnEl.textContent='⏳'; }
  fetch('/api/qs_ai/refresh', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .then(r=>r.json())
    .then(d=>{
      if(!_qsPollTimer) _qsPollTimer = setInterval(_qsPoll, 2500);
      const el = document.getElementById('qs-text');
      if(el) el.innerHTML = `<span style="display:inline-flex;align-items:center;gap:8px;color:#c084fc">
        <span class="mb-ai-spinner" style="width:14px;height:14px;border-width:2px"></span>
        Claude is analysing market data…
      </span>`;
    }).catch(()=>{ if(btnEl){ btnEl.disabled=false; btnEl.textContent='↻'; }});
}

function initAiBrainTab(){
  // Immediately replace "Loading…" with a progress indicator
  const el = document.getElementById('qs-text');
  fetch('/api/mb_ai')
    .then(function(r){
      if(!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(d){
      const enabled = d.qs_ai_enabled === true;
      _qsApplyToggleState(enabled);
      if(enabled){
        _mbAiUpdateQuickSummary(d);
        // Auto-trigger if idle or stale (> 6 min old)
        const qs = d.qs || {};
        const isStale = !qs.ts || ((Date.now() - new Date(qs.ts).getTime()) > 360000);
        if(qs.status === 'idle' || qs.status === 'no_data' || (qs.status === 'ok' && isStale)){
          qsRefresh();
        } else if(qs.status === 'running'){
          if(!_qsPollTimer) _qsPollTimer = setInterval(_qsPoll, 2500);
        }
      }
      _mbAiRender(d);
    })
    .catch(function(err){
      const el2 = document.getElementById('qs-text');
      if(el2) el2.innerHTML = '<span style="color:var(--warn)">⚠ Could not reach server: ' + err.message + '</span>';
    });

  // Poll quick summary every 30s while tab is open (only updates if enabled)
  if(!_qsRefreshTimer) _qsRefreshTimer = setInterval(function(){
    fetch('/api/mb_ai').then(function(r){ return r.json(); }).then(function(d){ _mbAiUpdateQuickSummary(d); }).catch(function(){});
  }, 30000);
}

function mbAiToggle(){
  fetch('/api/toggle?f=mb_ai').then(r=>r.json()).then(feat=>{
    _mbAiEnabled = feat.mb_ai === true;
    const btn = document.getElementById('mb-ai-toggle-btn');
    if(_mbAiEnabled){
      btn.textContent='ON'; btn.classList.add('toggle-on'); btn.classList.remove('toggle-off');
      btn.classList.add('mb-ai-on');
      document.getElementById('mb-ai-meta').style.display='flex';
      // Trigger first generation immediately
      mbAiRefresh();
      // Start 5-min auto-refresh timer
      if(_mbAiAutoTimer) clearInterval(_mbAiAutoTimer);
      _mbAiAutoTimer = setInterval(mbAiRefresh, 300000);
    } else {
      btn.textContent='OFF'; btn.classList.remove('toggle-on'); btn.classList.add('toggle-off');
      btn.classList.remove('mb-ai-on');
      document.getElementById('mb-ai-meta').style.display='none';
      if(_mbAiAutoTimer){ clearInterval(_mbAiAutoTimer); _mbAiAutoTimer=null; }
      if(_mbAiPollTimer){ clearInterval(_mbAiPollTimer); _mbAiPollTimer=null; }
      document.getElementById('mb-ai-content').innerHTML = `
        <div class="mb-ai-idle">
          <div class="idle-icon">🧠</div>
          <div class="idle-msg">AI Brain is OFF</div>
          <div class="idle-sub">Toggle ON to generate a live market summary.</div>
        </div>`;
    }
  }).catch(()=>{});
}

async function mbAiRefresh(){
  if(!_mbAiEnabled) return;
  const btn = document.getElementById('mb-ai-refresh-btn');
  if(btn) { btn.disabled=true; btn.textContent='⏳ Refreshing…'; }
  // Show loading state
  document.getElementById('mb-ai-content').innerHTML = `
    <div class="mb-ai-loading">
      <div class="mb-ai-spinner"></div>
      <div>
        <div class="mb-ai-loading-text">Collecting data from all bots…</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px">OI · VIX · Fibonacci · Master Signal · Convergence · Trendline → GPT-4o</div>
      </div>
    </div>`;
  try {
    const r = await fetch('/api/mb_ai/refresh', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const d = await r.json();
    if(!d.ok){ _mbAiRenderError(d.error||'Failed to start'); return; }
    // Poll until done
    if(_mbAiPollTimer) clearInterval(_mbAiPollTimer);
    _mbAiPollTimer = setInterval(async ()=>{
      const r2 = await fetch('/api/mb_ai');
      const d2 = await r2.json();
      if(d2.status !== 'running'){
        clearInterval(_mbAiPollTimer); _mbAiPollTimer=null;
        _mbAiRender(d2);
        if(btn){ btn.disabled=false; btn.textContent='↻ Refresh Now'; }
      }
    }, 2000);
  } catch(e) {
    _mbAiRenderError(String(e));
    if(btn){ btn.disabled=false; btn.textContent='↻ Refresh Now'; }
  }
}

function _mbAiRenderError(msg){
  document.getElementById('mb-ai-content').innerHTML = `
    <div class="mb-ai-card risks-card">
      <div class="mb-ai-card-title">⚠️ Error</div>
      <div class="mb-ai-body">${msg}</div>
    </div>`;
}

function _mbAiRender(d){
  const content = document.getElementById('mb-ai-content');
  const meta    = document.getElementById('mb-ai-meta');
  const btn     = document.getElementById('mb-ai-toggle-btn');

  if(!content) return;
  _mbAiUpdateQuickSummary(d);

  // Sync enabled state
  _mbAiEnabled = (d.status !== 'idle' && d.status !== undefined);

  // Update meta
  if(d.ts){
    const tsEl = document.getElementById('mb-ai-ts');
    if(tsEl) tsEl.textContent = '🕐 Last updated: ' + d.ts.replace('T',' ');
    if(meta) meta.style.display='flex';
  }
  if(d.context_lines){
    const ctxEl = document.getElementById('mb-ai-ctx');
    if(ctxEl) ctxEl.textContent = d.context_lines + ' data points fed to AI';
  }

  if(d.status === 'idle'){
    content.innerHTML = `
      <div class="mb-ai-idle">
        <div class="idle-icon">🧠</div>
        <div class="idle-msg">AI Brain is OFF</div>
        <div class="idle-sub">Toggle ON to generate a live market summary.</div>
      </div>`;
    return;
  }
  if(d.status === 'running'){
    content.innerHTML = `
      <div class="mb-ai-loading">
        <div class="mb-ai-spinner"></div>
        <div class="mb-ai-loading-text">Generating summary… please wait (up to 45s)</div>
      </div>`;
    return;
  }
  if(d.status === 'no_cli'){
    content.innerHTML = `
      <div class="mb-ai-card risks-card">
        <div class="mb-ai-card-title">🤖 Claude CLI Required</div>
        <div class="mb-ai-body">Claude Code CLI not found or not logged in.\n\nInstall: npm install -g @anthropic-ai/claude-code\nLogin:   claude login\n\nRequires Claude Pro or higher subscription.</div>
      </div>`;
    return;
  }
  if(d.status === 'no_data'){
    content.innerHTML = `
      <div class="mb-ai-card risks-card">
        <div class="mb-ai-card-title">📡 No Bot Data Yet</div>
        <div class="mb-ai-body">Start the bots first (MASTER_SIGNAL_BOT, FIBONACCI_TREND_ANALYZER, calculate_oi_pcr).\nThen click ↻ Refresh Now.</div>
      </div>`;
    return;
  }
  if(d.status === 'auth_error' || d.status === 'rate_limit' || d.status === 'error'){
    const icons = {auth_error:'🔑', rate_limit:'⏱', error:'⚠️'};
    content.innerHTML = `
      <div class="mb-ai-card risks-card">
        <div class="mb-ai-card-title">${icons[d.status]||'⚠️'} ${d.status}</div>
        <div class="mb-ai-body">${d.error||'Unknown error'}</div>
      </div>`;
    return;
  }

  if(d.status === 'ok'){
    // Build the full panel
    const fmt = t => (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

    let html = '';

    // Bottom line — most prominent, at the top
    if(d.bottom_line){
      const bl = fmt(d.bottom_line);
      const blColor = bl.toUpperCase().includes('BUY CE')?'var(--bull)':
                      bl.toUpperCase().includes('BUY PE')?'var(--bear)':'#f97316';
      html += `<div class="mb-ai-card bottom-card">
        <div class="mb-ai-card-title" style="color:${blColor}">💡 BOTTOM LINE</div>
        <div class="mb-ai-body" style="color:${blColor}">${bl}</div>
      </div>`;
    }

    // Intraday + Long-term in 2 columns
    html += `<div class="mb-ai-2col">`;
    if(d.intraday){
      html += `<div class="mb-ai-card intraday-card">
        <div class="mb-ai-card-title">📊 INTRADAY VIEW — Next 1-2 Hours</div>
        <div class="mb-ai-body">${fmt(d.intraday)}</div>
      </div>`;
    }
    if(d.longterm){
      html += `<div class="mb-ai-card longterm-card">
        <div class="mb-ai-card-title">📈 LONG-TERM VIEW — 2-5 Days</div>
        <div class="mb-ai-body">${fmt(d.longterm)}</div>
      </div>`;
    }
    html += `</div>`;

    // Key levels + Risks in 2 columns
    html += `<div class="mb-ai-2col">`;
    if(d.key_levels){
      html += `<div class="mb-ai-card levels-card">
        <div class="mb-ai-card-title">⚡ KEY LEVELS TO WATCH</div>
        <div class="mb-ai-body">${fmt(d.key_levels)}</div>
      </div>`;
    }
    if(d.risks){
      html += `<div class="mb-ai-card risks-card">
        <div class="mb-ai-card-title">⚠️ RISKS</div>
        <div class="mb-ai-body">${fmt(d.risks)}</div>
      </div>`;
    }
    html += `</div>`;

    content.innerHTML = html || '<div class="mb-ai-idle"><div class="idle-msg">No summary content received</div></div>';
  }
}
</script>

</body>
</html>"""

# ─────────────────────────────────────────────────────────────
#  HTTP HANDLER
# ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _json(self, body: dict, code: int = 200):
        try:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Access-Control-Allow-Origin','*')
            self.end_headers(); self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path); qs = parse_qs(parsed.query)

        if parsed.path.startswith('/api/toggle'):
            f = qs.get("f",[""])[0]
            if f in _features:
                _features[f] = not _features[f]
                if not _features[f]:
                    with _proc_lock:
                        proc = _running_procs.get(f)
                        if proc:
                            try: proc.terminate()
                            except Exception: pass
            self._json(_features)

        elif parsed.path.startswith('/api/data'):
            with _lock: snap = dict(_snapshot)
            snap["features"] = dict(_features)
            self._json(snap)

        elif parsed.path == '/api/prod10_logs':
            try:
                import glob as _glob
                log_dir = os.path.join(BASE, "logs", "groww_bot")
                files = sorted(_glob.glob(os.path.join(log_dir, "Groww_Bot_*.log")))
                if not files:
                    self._json({"lines":[],"offline":True,"error":"PROD10 not running — no log file found"})
                else:
                    latest = files[-1]
                    # If file not modified in last 90s, PROD10 is offline
                    _age = time.time() - os.path.getmtime(latest)
                    if _age > 90:
                        self._json({"lines":[],"offline":True,
                            "error":f"PROD10 offline — last active {int(_age//60)}m {int(_age%60)}s ago"})
                    else:
                        with open(latest,'r',encoding='utf-8',errors='replace') as _f:
                            _lines = _f.readlines()[-300:]
                        _keywords = ('DASHBOARD','BUY','Trailing','Monitoring','Trail','SELL','Exit',
                                     'SL HIT','profit','PROFIT','loss','LOSS','✅','❌','🌐','📈','💓',
                                     'Placed','Order placed','Command','ACTIVE','Entry price','LTP for')
                        # exclude noisy repetitive LTP error lines
                        _noise = ('Error fetching LTP','LTP is None','retrying','streak=')
                        _relevant = [l.rstrip() for l in _lines
                                     if any(k in l for k in _keywords)
                                     and not any(n in l for n in _noise)]
                        # collapse identical consecutive lines (e.g. repeated heartbeats)
                        _deduped = []
                        for _l in _relevant:
                            if _deduped and _deduped[-1].split(']',1)[-1] == _l.split(']',1)[-1]:
                                continue
                            _deduped.append(_l)
                        self._json({"lines":_deduped[-60:],"file":os.path.basename(latest),"offline":False})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as ex:
                try: self._json({"lines":[],"error":str(ex)})
                except (BrokenPipeError, ConnectionResetError): pass

        elif parsed.path == '/api/groww_capital':
            try:
                token = _get_ltp_token()
                if not token:
                    self._json({"ok": False, "error": "No Groww token — check API key / TOTP in ai_config.json"})
                else:
                    resp = _req.get(
                        "https://api.groww.in/v1/margins/detail/user",
                        headers={"Accept":"application/json","Authorization":f"Bearer {token}","X-API-VERSION":"1.0"},
                        timeout=10
                    )
                    if resp.status_code != 200:
                        self._json({"ok": False, "error": f"Groww API HTTP {resp.status_code}"})
                    else:
                        data = resp.json()
                        if data.get("status") != "SUCCESS":
                            self._json({"ok": False, "error": data.get("message","Groww API error")})
                        else:
                            pl = data.get("payload", {})
                            fno = pl.get("fno_margin_details", {})
                            ob  = fno.get("option_buy_balance_available", 0)
                            cc  = pl.get("clear_cash", 0)
                            self._json({
                                "ok": True,
                                "option_buy_balance": float(ob),
                                "clear_cash":         float(cc),
                            })
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as ex:
                try: self._json({"ok": False, "error": str(ex)})
                except (BrokenPipeError, ConnectionResetError): pass

        elif parsed.path == '/api/lot_size':
            idx = qs.get("index",["NIFTY"])[0].upper()
            exp = qs.get("expiry",[""])[0]
            self._json({"lot_size": _lot_size_from_csv(idx, exp)})

        elif parsed.path == '/api/momentum_bot_logs':
            try:
                import glob as _glob
                log_dir = os.path.join(BASE, "logs", "momentum_bot")
                files = sorted(_glob.glob(os.path.join(log_dir, "Momentum_Bot_*.log")))
                if not files:
                    self._json({"lines":[],"offline":True,"error":"Auto Bot not running — no log file found"})
                else:
                    latest = files[-1]
                    _age = time.time() - os.path.getmtime(latest)
                    if _age > 120:
                        self._json({"lines":[],"offline":True,
                            "error":f"Auto Bot offline — last active {int(_age//60)}m {int(_age%60)}s ago"})
                    else:
                        with open(latest,'r',encoding='utf-8',errors='replace') as _f:
                            _lines = _f.readlines()[-500:]
                        # filter to meaningful lines, skip repetitive trail monitoring
                        _keep = ('ENTRY','BUY','SELL','Trail','Signal','Momentum','MOCK TRAIL',
                                 'SIM','Quick target','HARD SL','Max hold','P&L','CLOSED',
                                 'CONFIG','starting','Ready','✅','❌','⚠','📈','💰','🎯','🔻','🛑','🎭',
                                 'second','CE =','PE =','vel=','score','Scanning','Override','Groww API',
                                 '🔎','OI VERDICT')
                        _skip = ('Monitoring |', '💓')
                        _relevant = [l.rstrip() for l in _lines
                                     if any(k in l for k in _keep)
                                     and not any(n in l for n in _skip)]
                        self._json({"lines":_relevant[-80:],"file":os.path.basename(latest),"offline":False})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as ex:
                try: self._json({"lines":[],"error":str(ex)})
                except (BrokenPipeError, ConnectionResetError): pass

        elif parsed.path == '/api/oi_verdict_summary':
            # Read today's trade-history JSONL and tally OI verdict tags
            _date = datetime.now().strftime("%Y-%m-%d")
            _th_path = os.path.join(BASE, "logs", "trade_history", f"{_date}.jsonl")
            _counts = {"ALIGNED_WIN":0,"ALIGNED_LOSS":0,"OPPOSED_WIN":0,"OPPOSED_LOSS":0,"NEUTRAL":0}
            try:
                if os.path.exists(_th_path):
                    with open(_th_path, encoding="utf-8") as _f:
                        for _line in _f:
                            try:
                                _r = json.loads(_line)
                                _tag = _r.get("oi_verdict_tag","NEUTRAL")
                                if _tag in _counts:
                                    _counts[_tag] += 1
                                elif _tag:
                                    _counts["NEUTRAL"] += 1
                            except Exception:
                                pass
            except Exception:
                pass
            self._json(_counts)

        elif parsed.path == '/api/alerts':
            import re as _re_mod, json as _json_mod2, time as _time_mod
            _now = _time_mod.time()
            # Purge dedup entries older than 5 min
            for _k in list(_alert_dedup.keys()):
                if _now - _alert_dedup[_k] > 300:
                    del _alert_dedup[_k]

            def _dedup_ok(src, atype, msg_key):
                k = (src, atype, msg_key[:60])
                if k in _alert_dedup:
                    return False
                _alert_dedup[k] = _now
                return True

            def _emit(src, atype, msg, ts=''):
                if _dedup_ok(src, atype, msg):
                    new_alerts.append({'source': src, 'type': atype, 'msg': msg, 'ts': ts})

            LOG_BASE = os.path.join(BASE, 'logs')
            new_alerts = []

            # ── 1. Log file scanning ──────────────────────────────────
            # (source, dir, prefix, is_json_lines, patterns_override or None)
            _SRCS = [
                ('PROD10',   os.path.join(LOG_BASE, 'groww_bot'),      'Groww_Bot_',     False, None),
                ('MOMENTUM', os.path.join(LOG_BASE, 'momentum_bot'),   'Momentum_Bot_',  False, None),
                ('MASTER',   os.path.join(LOG_BASE, 'master_signal'),   '',               True,  None),
                ('OI·FIBO',  os.path.join(LOG_BASE, 'signal_monitor'), 'Signal_Monitor_', False, None),
                ('PREMIUM',  os.path.join(LOG_BASE, 'premium_tracker'), 'Premium_Tracker_', False, None),
            ]
            # Default text patterns — first match wins
            # Covers both MOMENTUM_AUTO_BOT ([MOCK] prefix) and PROD10 (emoji prefix) log formats
            _PAT = [
                # ── BUY entry (PROD10: "Buy Order placed", "BUY EXECUTED", "[PAPER] MARKET/LIMIT BUY"; MOMENTUM: "BUY simulated", "MOMENTUM ENTRY") ──
                (r'BUY EXECUTED|BUY simulated|MOMENTUM ENTRY|Buy Order placed|BUY Order placed|\[PAPER\].*BUY', 'buy'),
                # ── SELL/exit (PROD10: "LIMIT SELL placed", "Market SELL placed", "[PAPER] SELL"; MOMENTUM: "SELL simulated") ──
                (r'SELL EXECUTED|SELL simulated|SELL Order placed|LIMIT SELL placed|Market SELL placed|\[PAPER\].*SELL', 'sell'),
                # ── SL hit (PROD10: "SL HIT", "TRAIL STOP HIT"; MOMENTUM: "Trail SL hit", "Hard SL hit") ──
                (r'SL HIT|Trail SL hit|Hard SL hit|TRAIL STOP HIT',               'sl'),
                # ── Target hit ──
                (r'TARGET HIT|Quick target hit',                                   'target'),
                # ── Profit close ──
                # PROD10: "💰 TRAIL PROFIT: ₹195 (Buy @ ₹107)", "💰 PROFIT: ₹7800 (Buy @ ₹137)"
                # PROD10: "💰 Estimated PROFIT/TRAIL PROFIT"
                # MOMENTUM: "[MOCK] CLOSED @ ₹110.15 Profit=+195.00"
                # NOT matched: "📈 NIFTY...: 🟢 PROFIT: ₹331.5" (position reporting) or "Total Realised P&L"
                (r'CLOSED.*Profit=.*\+|SELL EXECUTED.*P&L.*\+|TRAIL PROFIT|PROFIT.*Buy @|Estimated.*PROFIT', 'profit'),
                # ── Loss close ──
                # PROD10 loss prints start with 💸 emoji; position reporting uses 📈/💰 — so 💸 uniquely IDs trade losses
                (r'CLOSED.*Profit=.*-|LOSS.*Buy @|💸.*LOSS',            'loss'),
                # ── Errors ──
                (r'Traceback|❌.*[Ff]ail|❌.*[Ee]rror',                           'error'),
                # ── OI / FIBO / signal_monitor ──
                (r'BUY CE NOW|BREAKOUT SIGNAL',                                    'signal_buy'),
                (r'BUY PE NOW|BREAKDOWN SIGNAL',                                   'signal_sell'),
                (r'Action\s+STRONG CE|STRONGLY BULL',                              'signal_buy'),
                (r'Action\s+STRONG PE|STRONGLY BEAR',                              'signal_sell'),
                # ── Premium tracker ──
                (r'STRONG CE|STRONGLY BULLISH',                                    'signal_buy'),
                (r'STRONG PE|STRONGLY BEARISH',                                    'signal_sell'),
            ]
            for src, log_dir, prefix, is_json, _ in _SRCS:
                if not os.path.isdir(log_dir):
                    continue
                try:
                    files = sorted(
                        [f for f in os.listdir(log_dir)
                         if f.endswith('.log') and (not prefix or f.startswith(prefix))],
                        reverse=True)
                    if not files:
                        continue
                    log_path = os.path.join(log_dir, files[0])
                    with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
                        fh.seek(0, 2)
                        file_size = fh.tell()
                        offset = _alert_state.get(log_path, file_size)
                        if offset > file_size:
                            offset = 0
                        if offset == file_size:
                            _alert_state[log_path] = file_size
                            continue
                        fh.seek(offset)
                        new_lines = fh.readlines()
                        _alert_state[log_path] = fh.tell()
                    if is_json:
                        for line in new_lines:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = _json_mod2.loads(line)
                                direction  = rec.get('direction', '')
                                confidence = float(rec.get('confidence', 0))
                                if direction in ('BUY', 'SELL') and confidence >= 65:
                                    idx  = rec.get('index', '')
                                    spot = rec.get('spot', '')
                                    atype = f'signal_{direction.lower()}'
                                    _emit(src, atype,
                                          f'{idx} {direction} signal  Conf:{confidence:.0f}%  Spot:{spot}',
                                          rec.get('ts', ''))
                            except Exception:
                                pass
                    else:
                        for line in new_lines:
                            clean = line.strip()
                            if not clean:
                                continue
                            for pat, atype in _PAT:
                                if _re_mod.search(pat, clean):
                                    _emit(src, atype,
                                          _re_mod.sub(r'\s+', ' ', clean)[:140])
                                    break
                except Exception:
                    pass

            # ── 2. In-memory OI PCR bot stdout ───────────────────────
            _OI_PAT = [
                (r'BUY CE NOW|BREAKOUT SIGNAL',  'signal_buy'),
                (r'BUY PE NOW|BREAKDOWN SIGNAL', 'signal_sell'),
                (r'Signal\s*:.*STRONG BULL',     'signal_buy'),
                (r'Signal\s*:.*STRONG BEAR',     'signal_sell'),
            ]
            with _bot_lock:
                oi_lines = list(_bot_logs.get('oi_pcr', []))
            oi_start = _alert_bot_idx.get('oi_pcr', len(oi_lines))
            _alert_bot_idx['oi_pcr'] = len(oi_lines)
            for line in oi_lines[oi_start:]:
                clean = line.strip()
                if not clean:
                    continue
                for pat, atype in _OI_PAT:
                    if _re_mod.search(pat, clean):
                        _emit('OI·PCR', atype,
                              _re_mod.sub(r'\s+', ' ', clean)[:140])
                        break

            # ── 3. OI snapshot state change detection ────────────────
            try:
                oi = read_oi_snapshot()
                if oi:
                    mkt = oi.get('market_signal') or ''
                    pcr = float(oi.get('pcr_all') or 0)
                    pcr_atm = float(oi.get('pcr_atm') or 0)
                    wb  = oi.get('writer_bias', 'NEUTRAL') or 'NEUTRAL'
                    prev_mkt = _oi_snap_last.get('market_signal', '')
                    prev_pcr = float(_oi_snap_last.get('pcr_all') or 0)
                    prev_wb  = _oi_snap_last.get('writer_bias', 'NEUTRAL') or 'NEUTRAL'

                    # market_signal flipped to STRONG
                    if mkt != prev_mkt and mkt in ('STRONG BULLISH', 'STRONG BEARISH'):
                        atype = 'signal_buy' if 'BULL' in mkt else 'signal_sell'
                        _emit('OI·SIGNAL', atype,
                              f'OI Market Signal → {mkt}  PCR:{pcr:.2f}  PCR-ATM:{pcr_atm:.2f}')
                    # market_signal flipped to BULLISH/BEARISH (not strong)
                    elif mkt != prev_mkt and mkt in ('BULLISH', 'BEARISH'):
                        atype = 'signal_buy' if mkt == 'BULLISH' else 'signal_sell'
                        _emit('OI·SIGNAL', atype,
                              f'OI Market Signal → {mkt}  PCR:{pcr:.2f}')

                    # PCR crossed extreme threshold
                    if pcr >= 1.5 and prev_pcr < 1.5:
                        _emit('OI·PCR', 'signal_buy',
                              f'PCR (all strikes) {pcr:.2f} ≥ 1.5 — STRONG BULLISH  ATM-PCR:{pcr_atm:.2f}')
                    elif pcr <= 0.6 and prev_pcr > 0.6:
                        _emit('OI·PCR', 'signal_sell',
                              f'PCR (all strikes) {pcr:.2f} ≤ 0.6 — STRONG BEARISH  ATM-PCR:{pcr_atm:.2f}')
                    elif pcr >= 1.2 and prev_pcr < 1.2:
                        _emit('OI·PCR', 'signal_buy',
                              f'PCR (all strikes) {pcr:.2f} ≥ 1.2 — BULLISH  ATM-PCR:{pcr_atm:.2f}')
                    elif pcr <= 0.8 and prev_pcr > 0.8:
                        _emit('OI·PCR', 'signal_sell',
                              f'PCR (all strikes) {pcr:.2f} ≤ 0.8 — BEARISH  ATM-PCR:{pcr_atm:.2f}')

                    # Writer bias flipped away from NEUTRAL
                    if wb != prev_wb and wb in ('BULLISH', 'BEARISH'):
                        bs = oi.get('bullish_score', 0) or 0
                        brs = oi.get('bearish_score', 0) or 0
                        atype = 'signal_buy' if wb == 'BULLISH' else 'signal_sell'
                        _emit('OI·WRITER', atype,
                              f'Writer Bias → {wb}  (Bull:{bs:.1f}M  Bear:{brs:.1f}M)')

                    _oi_snap_last.update({
                        'market_signal': mkt, 'pcr_all': pcr,
                        'writer_bias': wb,
                    })
            except Exception:
                pass

            # ── 4. Live Dashboard consensus signal change ─────────────
            try:
                cons = _snapshot.get('consensus', {})
                sig  = cons.get('signal', '')
                prev_sig = _consensus_last.get('signal', '')
                if sig and sig != prev_sig:
                    _consensus_last['signal'] = sig
                    bull = cons.get('bull', 0)
                    bear = cons.get('bear', 0)
                    srcs = ', '.join(cons.get('sources', [])[:4])
                    if 'STRONG CE' in sig:
                        _emit('CONSENSUS', 'signal_buy',
                              f'STRONG CE ▲▲ — all bots aligned  bull:{bull} bear:{bear}  [{srcs}]')
                    elif 'STRONG PE' in sig:
                        _emit('CONSENSUS', 'signal_sell',
                              f'STRONG PE ▼▼ — all bots aligned  bull:{bull} bear:{bear}  [{srcs}]')
                    elif sig.startswith('CE'):
                        _emit('CONSENSUS', 'signal_buy',
                              f'Consensus → CE ▲  bull:{bull} bear:{bear}  [{srcs}]')
                    elif sig.startswith('PE'):
                        _emit('CONSENSUS', 'signal_sell',
                              f'Consensus → PE ▼  bull:{bull} bear:{bear}  [{srcs}]')
            except Exception:
                pass

            # ── 5. VIX spike / threshold alerts ──────────────────────────────
            try:
                with _vix_history_lock: _vh = list(_vix_history)
                if len(_vh) >= 2:
                    _cv = _vh[-1]["v"]
                    # fast spike: compare to reading ~10 min ago (5 ticks × 2 min)
                    _ref_idx = max(0, len(_vh) - 6)
                    _rv = _vh[_ref_idx]["v"]
                    if _rv:
                        _spk = (_cv - _rv) / _rv * 100
                        if _spk >= 3:
                            _emit('VIX', 'warn',
                                  f'⚠ VIX spiking +{_spk:.1f}% in last ~10min → {_cv:.2f}. Panic injection — widen stops, avoid naked shorts.')
                        elif _spk <= -3:
                            _emit('VIX', 'info',
                                  f'VIX dropped {_spk:.1f}% in last ~10min → {_cv:.2f}. Fear receding — premium decay likely to accelerate.')
                    # threshold crossings (prev vs current)
                    _pv = _vh[-2]["v"]
                    for _thr, _lbl in [(25,'DANGER'), (20,'HIGH'), (18,'ELEVATED'), (15,'CAUTION')]:
                        if _pv < _thr <= _cv:
                            _emit('VIX', 'warn',
                                  f'🔴 VIX crossed above {_thr} ({_lbl}) — now {_cv:.2f}. Avoid directional trades.')
                        elif _pv >= _thr > _cv:
                            _emit('VIX', 'info',
                                  f'🟢 VIX fell below {_thr} — now {_cv:.2f}. Fear easing.')
            except Exception:
                pass

            self._json({'alerts': new_alerts})

        elif parsed.path == '/api/trade/status':
            with _trade_lock:
                resp = dict(_trade_state)
            resp["history"] = list(_trade_history)
            self._json(resp)

        elif parsed.path == '/api/trade/chain':
            idx    = qs.get("index",["NIFTY"])[0].upper()
            expiry = qs.get("expiry",[""])[0]
            if not expiry:
                self._json({"error":"expiry required"},400); return
            self._json(fetch_option_chain(idx, expiry))

        elif parsed.path == '/api/trade/expiries':
            idx = qs.get("index",["NIFTY"])[0].upper()
            self._json({"expiries": fetch_expiries(idx)})

        elif parsed.path == '/api/indices':
            self._json(read_market_indices())

        elif parsed.path == '/api/trade/chain_quotes':
            from concurrent.futures import ThreadPoolExecutor
            raw  = qs.get("s",[""])[0]
            syms = [s.strip() for s in raw.split(",") if s.strip()][:48]
            def _quote_one(esym):
                parts = esym.split("_", 1)
                if len(parts) != 2: return esym, 0
                xch, sym_ts = parts
                pl = _groww_get("/v1/live-data/quote",
                                {"exchange": xch, "segment": "FNO", "trading_symbol": sym_ts})
                if pl:
                    last_p = float(pl.get("last_price") or pl.get("ltp") or 0)
                    day_c  = float(pl.get("day_change") or 0)
                    prev   = round(last_p - day_c, 2)
                    if prev > 0: return esym, prev
                return esym, 0
            prev_close = {}
            with ThreadPoolExecutor(max_workers=8) as ex:
                for esym, prev in ex.map(_quote_one, syms):
                    if prev > 0: prev_close[esym] = prev
            self._json({"prev_close": prev_close})

        elif parsed.path == '/api/trade/ltp_batch':
            # Fast LTP-only update: takes comma-separated exchange_symbols, returns {sym: ltp}
            raw = qs.get("s",[""])[0]
            syms = [s.strip() for s in raw.split(",") if s.strip()]
            if not syms:
                self._json({"ltp":{}, "spot":0})
            else:
                result = {}
                # Batch into groups of 50 (Groww limit per call)
                for i in range(0, len(syms), 50):
                    batch = syms[i:i+50]
                    pl = _groww_get("/v1/live-data/ltp",
                                    {"segment":"FNO","exchange_symbols":batch})
                    if pl: result.update(pl)
                self._json({"ltp": result,
                            "ts": datetime.now().isoformat(timespec="seconds")})

        elif parsed.path == '/api/mb_ai':
            with _mb_ai_lock:
                result = dict(_mb_ai_cache)
            with _qs_lock:
                result["qs"] = dict(_qs_cache)
            result["qs_ai_enabled"] = _features.get("qs_ai", False)
            self._json(result)

        elif parsed.path == '/api/personal_ai':
            with _pai_lock:
                self._json(dict(_pai_cache))

        elif parsed.path == '/api/performance':
            self._json(_parse_perf_data())

        elif parsed.path == '/api/pivots':
            idx = qs.get("index", ["NIFTY"])[0].upper()
            self._json(_read_pivots(idx))

        elif parsed.path == '/api/bot/status':
            self._json(_bot_status_all())

        elif parsed.path == '/api/bot/registry':
            self._json({"bots": _BOT_REGISTRY})

        elif parsed.path == '/api/bot/logs':
            bid = qs.get("id", [""])[0]
            n   = int(qs.get("n", ["60"])[0])
            self._json({"lines": _bot_get_logs(bid, n)})

        elif parsed.path == '/api/engine/expiries':
            idx = qs.get("index", ["NIFTY"])[0].upper()
            self._json({"index": idx, "expiries": _engine_expiries(idx)})

        elif parsed.path == '/api/engine/console':
            n = int(qs.get("n", ["40"])[0])
            with _bot_lock:
                lines = list(_bot_logs.get("decision_engine", [])[-n:])
            self._json({"lines": lines, **_engine_running()})

        elif parsed.path == '/api/trendline_config':
            cfg_path = os.path.join(BASE, "trendline_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    self._json(json.load(f))
            else:
                self._json({"premium_min": 85.0, "premium_max": 200.0, "lots": 18, "expiry_date": ""})

        elif parsed.path == '/api/trendline_signals':
            sig_file = os.path.join(BASE, ".trendline_signals.json")
            if os.path.exists(sig_file):
                with open(sig_file) as f:
                    self._json(json.load(f))
            else:
                self._json({"signals": [], "active_trade": None, "stats": {}})

        elif parsed.path == '/api/trendline_chart':
            chart_file = os.path.join(BASE, ".trendline_chart_data.json")
            if os.path.exists(chart_file):
                with open(chart_file) as f:
                    self._json(json.load(f))
            else:
                self._json({"instruments": [], "spot": None})

        elif parsed.path == '/api/trendline_expiries':
            # Return upcoming NIFTY weekly expiry dates from instrument.csv
            import csv as _csv
            expiries = set()
            csv_path = os.path.join(BASE, "instrument.csv")
            today_str = datetime.now().strftime("%Y-%m-%d")
            try:
                with open(csv_path, encoding="utf-8") as f:
                    for row in _csv.DictReader(f):
                        sym = row.get("trading_symbol", "")
                        exp = row.get("expiry_date", "")
                        und = row.get("underlying_symbol", "")
                        if und == "NIFTY" and exp >= today_str:
                            expiries.add(exp)
            except Exception:
                pass
            self._json({"expiries": sorted(expiries)[:12]})

        elif parsed.path == '/api/trendline_history':
            # Read trendline trade history JSONL files for a date range
            df = qs.get("from", [""])[0]
            dt = qs.get("to",   [""])[0]
            mode_f = qs.get("mode", ["ALL"])[0]
            today_str = datetime.now().strftime("%Y-%m-%d")
            df = df or today_str
            dt = dt or today_str
            hist_dir = os.path.join(BASE, "logs", "trade_history")
            trades = []
            if os.path.isdir(hist_dir):
                for fname in sorted(os.listdir(hist_dir)):
                    if not fname.startswith("trendline_") or not fname.endswith(".jsonl"):
                        continue
                    if "backtest" in fname:
                        continue
                    date_part = fname[len("trendline_"):-6]
                    if date_part < df or date_part > dt:
                        continue
                    try:
                        with open(os.path.join(hist_dir, fname), encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    try:
                                        rec = json.loads(line)
                                        if mode_f != "ALL" and rec.get("mode","") != mode_f:
                                            continue
                                        if "date" not in rec:
                                            rec["date"] = date_part
                                        trades.append(rec)
                                    except Exception:
                                        pass
                    except Exception:
                        pass
            self._json({"trades": trades})

        elif parsed.path == '/api/trade_history':
            df = qs.get("from", [""])[0]
            dt = qs.get("to",   [""])[0]
            self._json({"trades": read_trade_history(df, dt)})

        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length','0') or 0)
        body   = json.loads(self.rfile.read(length) or b'{}')

        if path == '/api/prod10_buy':
            index          = body.get("index","NIFTY").strip().upper()
            expiry         = body.get("expiry","").strip()
            strike         = int(body.get("strike",0))
            opt_type       = body.get("opt_type","CE").strip().upper()
            lots           = int(body.get("lots",1))
            mode           = body.get("mode","manual")
            paper          = bool(body.get("paper", False))
            atr            = bool(body.get("atr",   False))
            atr_source     = str(body.get("atr_source", "candle"))
            mock           = bool(body.get("mock",  False))
            quick_pts      = float(body.get("quick_pts", 1.5))
            partial        = bool(body.get("partial", False))
            partial_pct    = int(body.get("partial_pct", 50))
            ltp_hint       = float(body.get("ltp", 0) or 0)   # chain LTP — bot skips redundant LTP fetch
            validate_orders = body.get("validate_orders", None)   # None = keep bot's CONFIG default
            if validate_orders is not None:
                validate_orders = bool(validate_orders)
            if not expiry or strike <= 0 or lots <= 0:
                self._json({"ok":False,"error":"index, expiry, strike and lots required"},400); return
            try:
                from datetime import datetime as _dt
                _d = _dt.strptime(expiry, "%Y-%m-%d")
                expiry_token = f"{_d.day:02d}{_d.strftime('%b').upper()}{_d.year}"
                prod10_sym = f"{index}{expiry_token}{strike}{opt_type}"
                command    = f"{lots} {prod10_sym}"
                import json as _json
                bridge = {"command":command,"mode":mode,"paper":paper,"atr":atr,"atr_source":atr_source,"mock":mock,"quick_pts":quick_pts,"partial":partial,"partial_pct":partial_pct,"ltp":ltp_hint}
                if validate_orders is not None:
                    bridge["validate_orders"] = validate_orders
                with open(PROD10_BRIDGE_FILE,"w") as _f:
                    _json.dump(bridge, _f)
                self._json({"ok":True,"command":command,"mode":mode})
            except Exception as ex:
                self._json({"ok":False,"error":str(ex)},500)

        elif path == '/api/prod10_set_target':
            quick_pts = float(body.get("quick_pts", 0))
            if quick_pts <= 0:
                self._json({"ok": False, "error": "quick_pts must be > 0"}, 400); return
            try:
                import json as _json
                with open(PROD10_BRIDGE_FILE, "w") as _f:
                    _json.dump({"command": "set_quick_pts", "quick_pts": quick_pts}, _f)
                self._json({"ok": True, "quick_pts": quick_pts})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)}, 500)

        elif path == '/api/prod10_set_partial':
            partial     = bool(body.get("partial", False))
            partial_pct = int(body.get("partial_pct", 50))
            if not (10 <= partial_pct <= 90):
                self._json({"ok": False, "error": "partial_pct must be 10–90"}, 400); return
            try:
                import json as _json
                with open(PROD10_BRIDGE_FILE, "w") as _f:
                    _json.dump({"command": "set_partial", "partial": partial, "partial_pct": partial_pct}, _f)
                self._json({"ok": True, "partial": partial, "partial_pct": partial_pct})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)}, 500)

        elif path == '/api/start_prod10':
            try:
                import subprocess as _sp
                # Delete any stale bridge command left from a previous session so
                # the new PROD10 process doesn't accidentally read an old auto/quick command.
                if os.path.exists(PROD10_BRIDGE_FILE):
                    os.remove(PROD10_BRIDGE_FILE)
                _script = os.path.join(BASE, "PROD10FEB_ManualBOT_groww_option_trading_final_bot.py")
                _sp.Popen(['osascript','-e',
                    f'tell application "Terminal" to do script "cd {BASE} && python3 \\"{_script}\\""'])
                self._json({"ok":True})
            except Exception as ex:
                self._json({"ok":False,"error":str(ex)},500)

        elif path == '/api/prod10_auto':
            paper = bool(body.get("paper", False))
            try:
                import json as _json
                with open(PROD10_BRIDGE_FILE, "w") as _f:
                    _json.dump({"command": "__AUTO__", "mode": "auto", "paper": paper}, _f)
                self._json({"ok": True, "mode": "auto", "paper": paper})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)}, 500)

        elif path == '/api/auto_mode_status':
            _status_path = os.path.join(BASE, ".auto_mode_status.json")
            try:
                import json as _json
                if os.path.exists(_status_path):
                    with open(_status_path) as _f:
                        _data = _json.load(_f)
                    self._json(_data)
                else:
                    self._json({"state": "IDLE"})
            except Exception as ex:
                self._json({"state": "IDLE", "error": str(ex)})

        elif path == '/api/trendline_config':
            cfg_path = os.path.join(BASE, "trendline_config.json")
            with open(cfg_path, "w") as f:
                json.dump(body, f, indent=2)
            self._json({"ok": True})

        elif path == '/api/run_trendline_backtest':
            import subprocess, sys as _sys
            expiry      = str(body.get("expiry",      "")).strip() or "2026-06-23"
            days        = int(body.get("days",         31))
            premium_min = float(body.get("premium_min", 85))
            premium_max = float(body.get("premium_max", 200))
            lots        = int(body.get("lots",          18))
            exp_tag     = expiry.replace("-", "")
            out_path    = os.path.join(BASE, "logs", "trade_history",
                                       f"trendline_backtest_{exp_tag}.jsonl")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            cmd = [
                _sys.executable,
                os.path.join(BASE, "TRENDLINE_BACKTEST.py"),
                "--expiry",      expiry,
                "--days",        str(days),
                "--premium_min", str(premium_min),
                "--premium_max", str(premium_max),
                "--lots",        str(lots),
                "--out",         out_path,
            ]
            try:
                subprocess.run(cmd, cwd=BASE, timeout=300,
                               capture_output=True, text=True)
            except subprocess.TimeoutExpired:
                self._json({"error": "Backtest timed out (>5 min)"}); return
            except Exception as e:
                self._json({"error": str(e)}); return
            bt_trades = []
            if os.path.exists(out_path):
                with open(out_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                bt_trades.append(json.loads(line))
                            except Exception:
                                pass
            self._json({"trades": bt_trades, "out": out_path})

        elif path == '/api/bot/start':
            bot_id = body.get("id", "")
            config = body.get("config", {})
            self._json(_bot_start(bot_id, config))

        elif path == '/api/bot/stop':
            bot_id = body.get("id", "")
            self._json(_bot_stop(bot_id))

        elif path == '/api/engine/start':
            self._json(_engine_start(body))

        elif path == '/api/engine/stop':
            self._json(_engine_stop())

        elif path == '/api/momentum/config':
            # Live config update — merges into existing override file so running bot
            # picks it up on next scan (bot calls _reload_override() each cycle)
            _live_cast = {
                "validate_orders": bool, "choppiness_enabled": bool,
                "consec_sl_brake": bool, "HARD_SL_ATR_BASED": bool,
                "atr_source": str,
                "min_score_filter": bool,
                "velocity_filter":  bool,
                "min_premium": float, "max_premium": float,
                "lots": int, "atm_range": int,
                "scan_seconds": int, "poll_seconds": int,
                "velocity_pct": float, "consistency_pct": float,
                "_vix_config_note": str,
            }
            ov_path = os.path.join(BASE, "momentum_config_override.json")
            existing = {}
            try:
                if os.path.exists(ov_path):
                    with open(ov_path) as _f:
                        existing = json.load(_f)
            except Exception:
                pass
            for _k, _cast in _live_cast.items():
                if _k in body:
                    try:
                        existing[_k] = _cast(body[_k])
                    except Exception:
                        pass
            try:
                with open(ov_path, "w") as _f:
                    json.dump(existing, _f)
                self._json({"ok": True})
            except Exception as _e:
                self._json({"ok": False, "error": str(_e)})

        elif path == '/api/personal_ai/run':
            with _pai_lock:
                already = _pai_cache["running"]
                if not already:
                    _pai_cache["running"] = True
            if already:
                self._json({"status": "already_running"})
            else:
                threading.Thread(target=_run_pai_bg, daemon=True).start()
                self._json({"status": "started"})

        elif path == '/api/qs_ai/refresh':
            if not _features.get("qs_ai"):
                self._json({"ok": False, "status": "disabled"})
            else:
                with _qs_lock:
                    already = _qs_cache.get("status") == "running"
                if already:
                    self._json({"ok": False, "status": "already_running"})
                else:
                    with _lock: snap_copy = dict(_snapshot)
                    threading.Thread(target=generate_qs_ai, args=(snap_copy,), daemon=True).start()
                    self._json({"ok": True, "status": "started"})

        elif path == '/api/mb_ai/refresh':
            if not _features.get("mb_ai"):
                self._json({"ok": False, "error": "AI Brain is OFF — toggle it on first"})
            else:
                with _mb_ai_lock:
                    already = _mb_ai_cache.get("status") == "running"
                if already:
                    self._json({"ok": False, "status": "already_running"})
                else:
                    with _lock: snap_copy = dict(_snapshot)
                    threading.Thread(target=generate_mb_ai, args=(snap_copy,), daemon=True).start()
                    self._json({"ok": True, "status": "started"})

        else:
            self._json({"error":"not found"},404)

# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def _ensure_control_panel():
    """Auto-start TRADE_CONTROL_PANEL.py (port 8790) if it isn't running,
    so the 🛡 Control tab always has something to embed."""
    import socket, subprocess
    try:
        s = socket.create_connection(("127.0.0.1", 8790), timeout=0.5)
        s.close()
        return  # already running
    except Exception:
        pass
    try:
        log = open(os.path.join(BASE, "logs", "control_panel.log"), "a")
        subprocess.Popen([sys.executable, os.path.join(BASE, "TRADE_CONTROL_PANEL.py")],
                         stdout=log, stderr=subprocess.STDOUT, cwd=BASE)
        print(f"  🛡  Trade Control Panel auto-started → http://127.0.0.1:8790")
    except Exception as e:
        print(f"  ⚠️ Could not auto-start Trade Control Panel: {e}")


def main():
    print(f"\n{'═'*60}")
    print(f"  📊 LIVE TRADING DASHBOARD")
    print(f"{'═'*60}")
    print(f"\n  Start these bots first (each in its own terminal):")
    print(f"  ✅  python3 MASTER_SIGNAL_BOT.py          [required]")
    print(f"  ✅  python3 FIBONACCI_TREND_ANALYZER.py   [required]")
    print(f"  🔶  python3 CHART_LEVEL_ANALYZER.py       [optional]")
    print(f"  🔶  python3 PREMIUM_DIRECTION_TRACKER.py  [optional]")
    print(f"\n  Loading data…", end="", flush=True)
    _refresh()
    print(" done.")
    threading.Thread(target=_loop, daemon=True).start()
    threading.Thread(target=_ltp_fetcher_loop, daemon=True).start()
    threading.Thread(target=_run_ptai_analysis, daemon=True).start()
    threading.Thread(target=_idx_refresh_loop, daemon=True).start()
    _load_vix_cache()
    threading.Thread(target=_vix_fetch_loop, daemon=True).start()
    _ensure_control_panel()
    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True   # avoids "Address already in use" on quick restart
        def handle_error(self, request, client_address):
            import sys
            exc = sys.exc_info()[1]
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                return  # browser closed connection — not an error
            super().handle_error(request, client_address)
    server = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
    print(f"\n  ✅  Open in browser →  http://localhost:{PORT}")
    print(f"  ↻   Updates every {REFRESH_SEC}s automatically")
    print(f"\n  Ctrl+C to stop.\n")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n  Stopped.\n")

if __name__ == '__main__':
    main()
