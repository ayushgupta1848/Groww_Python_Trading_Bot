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

BASE        = os.path.dirname(os.path.abspath(__file__))
PORT        = 8765
REFRESH_SEC = 15
STALE_SECS  = 300

# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def _latest(subdir: str, prefix: str, ext=".log") -> Optional[str]:
    d = os.path.join(BASE, subdir)
    if not os.path.isdir(d): return None
    files = sorted([f for f in os.listdir(d) if f.startswith(prefix) and f.endswith(ext)], reverse=True)
    return os.path.join(d, files[0]) if files else None

def _parse_ts(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try: return datetime.strptime(s.strip(), fmt)
        except ValueError: pass
    # time-only like "11:34:22"
    m = _re.match(r'^(\d{2}:\d{2}:\d{2})$', s.strip())
    if m:
        t = datetime.strptime(m.group(1), "%H:%M:%S")
        n = datetime.now()
        return n.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
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

_trade_lock = threading.Lock()
_trade_state: dict = {
    "status":"IDLE","symbol":"","exchange":"NSE","order_id":"",
    "avg_price":0.0,"qty":0,"entry_ts":"","buy_exec_ms":0,
    "ltp":0.0,"highest":0.0,"hard_sl":0.0,"trail_exit":0.0,
    "trail_active":False,"unrealised":0.0,
    "exit_reason":"","exit_price":0.0,"exit_exec_ms":0,
    "total_ms":0,"pnl":0.0,"log":[],"paper":False,"error":"",
    "atr_val":0.0,"atr_based":False,  # ATR info for UI display
}

def _tlog(msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    entry = f"{ts}  {msg}"
    with _trade_lock:
        _trade_state["log"].insert(0, entry)
        if len(_trade_state["log"]) > 300:
            _trade_state["log"] = _trade_state["log"][:300]
    print(f"[TRADE] {entry}")

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
    deadline = time.time() + max_sec
    while time.time() < deadline:
        time.sleep(0.2)
        p = _groww_get(f"/v1/order/status/{order_id}", {"segment":"FNO"})
        st = p.get("order_status","")
        if st in ("COMPLETE","EXECUTED","DELIVERY_AWAITED"):
            tp  = _groww_get(f"/v1/order/trades/{order_id}", {"segment":"FNO"})
            tl  = tp.get("trade_list",[])
            if tl:
                tv = sum(float(t["price"])*int(t["quantity"]) for t in tl)
                tq = sum(int(t["quantity"]) for t in tl)
                return round(tv/tq,2) if tq else 0.0, st, ""
            return 0.0, st, ""
        if st in ("REJECTED","FAILED","CANCELLED"):
            return 0.0, st, p.get("remark","")
    return 0.0, "TIMEOUT", ""

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
        exit_price,st,_ = _wait_fill(sid)
        exec_ms = int((time.time()-t_exit)*1000)
        if not exit_price:
            with _trade_lock: exit_price = _trade_state.get("ltp") or avg_price

    pnl   = round((exit_price-avg_price)*qty,2)
    total = int((time.time()-t0_epoch)*1000)
    with _trade_lock:
        _trade_state.update({"status":"DONE","exit_price":exit_price,
            "exit_exec_ms":int((time.time()-t_exit)*1000),
            "total_ms":total,"pnl":pnl})
    sign = "+" if pnl>=0 else ""
    _tlog(f"DONE | Sell ₹{exit_price} | P&L {sign}₹{pnl:,.2f} | "
          f"Exit exec {exec_ms}ms | Total {total//1000}s {total%1000}ms")

def _trail_loop(sym,exch,qty,avg_price,hard_sl,trail_start,trail_step,max_sec,paper,t0):
    global _trade_state
    highest = avg_price; last_trail = None
    esym = f"{exch}_{sym}"
    _tlog(f"Trail started | entry ₹{avg_price} | SL ₹{hard_sl:.2f} | trail after +{trail_start}pts")
    while True:
        with _trade_lock:
            if _trade_state["status"] != "ACTIVE": break
        p   = _groww_get("/v1/live-data/ltp",{"segment":"FNO","exchange_symbols":[esym]})
        ltp = float(p.get(esym,0) or 0)
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
        time.sleep(0.2)   # 5/sec = 300/min — exactly at Groww Live Data rate limit

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

    # ── ATR: override hard_sl and trail_step if enabled ──────
    atr_val = 0.0
    if atr_based:
        _tlog("Fetching ATR (1-min candles, up to 600min lookback)…")
        with _trade_lock: _trade_state["atr_based"] = True
        atr_val = fetch_atr(sym, exch)
        if atr_val > 0:
            hard_sl    = round(round((avg - 1.5 * atr_val) / 0.05) * 0.05, 2)
            trail_step = round(atr_val * atr_multiplier, 2)
            with _trade_lock: _trade_state["atr_val"] = atr_val
            _tlog(f"✅ ATR={atr_val:.2f} → Hard SL ₹{hard_sl:.2f} (1.5×ATR) | Trail step ₹{trail_step:.2f} ({atr_multiplier}×ATR)")
        else:
            hard_sl = round(round((avg - hard_sl_pts) / 0.05) * 0.05, 2)
            _tlog(f"⚠️ ATR fetch failed (market closed / no candles) → fallback Hard SL ₹{hard_sl:.2f} ({hard_sl_pts}pts fixed)")
    else:
        hard_sl = round(round((avg - hard_sl_pts) / 0.05) * 0.05, 2)
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

def fetch_option_chain(index:str, expiry:str) -> dict:
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
        strikes.append({
            "strike":  float(sp),
            "ce_sym":  ce.get("trading_symbol",""),
            "pe_sym":  pe.get("trading_symbol",""),
            "ce_ltp":  round(float(ce.get("ltp",0) or 0),2),
            "pe_ltp":  round(float(pe.get("ltp",0) or 0),2),
            "ce_oi":   int(ce.get("open_interest",0) or 0),
            "pe_oi":   int(pe.get("open_interest",0) or 0),
            "ce_vol":  int(ce.get("volume",0) or 0),
            "pe_vol":  int(pe.get("volume",0) or 0),
            "ce_iv":   round(float(cg.get("iv",0) or 0),1),
            "pe_iv":   round(float(pg.get("iv",0) or 0),1),
        })
    return {"strikes":strikes,"spot":spot,"lot_size":lot_size,"error":""}

def fetch_expiries(index:str) -> list:
    insts  = _load_instruments_for_ltp()
    today  = datetime.now().strftime("%Y-%m-%d")
    return sorted({i["expiry_date"].strip() for i in insts
                   if i.get("underlying_symbol","").upper()==index.upper()
                   and i.get("expiry_date","").strip()>=today})[:12]

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
        totp = pyotp.TOTP(_GROWW_TOTP_SECRET).now()
        token = GrowwAPI.get_access_token(api_key=_GROWW_API_KEY, totp=totp)
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

# Feature on/off flags (toggled via /api/toggle?f=ai or /api/toggle?f=scalp)
_features = {"ai": False, "scalp": True, "ptai_ai": False}

_ai_lock    = threading.Lock()
_scalp_lock = threading.Lock()
_ai_summary: dict = {"text": "", "ts": "", "status": "init", "error": "", "source": ""}
_scalp_plan: dict = {"text": "", "ts": "", "status": "init", "error": ""}

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

_running_procs: dict = {"ai": None, "scalp": None, "ptai_ai": None}
_proc_lock = threading.Lock()

def _try_claude_cli(prompt: str, timeout: int = 45, feature_key: str = "") -> str:
    """Run claude CLI. If feature_key is given, polls every 0.5s and kills the
    process immediately if that feature is toggled off — zero extra token waste."""
    import subprocess, shutil
    claude_bin = shutil.which("claude")
    if not claude_bin: return ""
    try:
        proc = subprocess.Popen([claude_bin, "-p", prompt],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if feature_key:
            with _proc_lock: _running_procs[feature_key] = proc

        deadline = time.time() + timeout
        while time.time() < deadline:
            # Check if the feature was disabled while we were running
            if feature_key and not _features.get(feature_key):
                proc.terminate()
                try: proc.wait(timeout=2)
                except Exception: proc.kill()
                if feature_key:
                    with _proc_lock: _running_procs[feature_key] = None
                return ""   # ← cancelled, no output, no tokens billed beyond what started
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        else:
            proc.terminate()          # timed out
            try: proc.wait(timeout=2)
            except Exception: proc.kill()
            if feature_key:
                with _proc_lock: _running_procs[feature_key] = None
            return ""

        stdout, _ = proc.communicate()
        if feature_key:
            with _proc_lock: _running_procs[feature_key] = None
        return stdout.strip() if proc.returncode == 0 and stdout.strip() else ""
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
#  DATA REFRESH LOOP
# ─────────────────────────────────────────────────────────────
_lock     = threading.Lock()
_snapshot: dict = {}

def _refresh() -> None:
    global _snapshot
    master = read_master();  fibo   = read_fibo()
    csig   = read_chart_signal(); cdec = read_chart_decision()
    prem   = read_premium(); trade  = read_trade_bot()
    sigmon = read_signal_monitor(); live_chain = read_live_chain()
    today_pnl = read_today_pnl()
    margin    = read_margin()
    orders    = read_today_orders()
    with _ltp_result_lock: ltp_result = dict(_ltp_result)
    cons   = build_consensus(master, fibo, csig, sigmon)
    # Pick spot from the freshest live source
    def _best_spot():
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

    snap = {
        "ts":    datetime.now().isoformat(timespec="seconds"),
        "index": master.get("index") or fibo.get("index") or "NIFTY",
        "spot":  _best_spot(),
        "bots":  {"master": master, "fibo": fibo, "chart_signal": csig,
                  "chart_decision": cdec, "premium": prem,
                  "trade": trade, "signal_monitor": sigmon},
        "live_chain": live_chain,
        "live_option_ltp": ltp_result,
        "consensus": cons,
        "ai_summary":  dict(_ai_summary),
        "scalp_plan":  dict(_scalp_plan),
        "features":    dict(_features),
        "mins_to_close": _mins_to_close(),
        "pnl_today":    today_pnl,
        "margin":       margin,
        "orders":       orders,
        "pnl_analysis": dict(_ptai_analysis),
        "pnl_ai":       dict(_ptai_ai),
        "ptai_ok":      _ptai_ok,
    }
    with _lock: _snapshot = snap

def _loop():
    _last: dict = {"ai": 0.0, "scalp": 0.0, "ptai": 0.0, "ptai_ai": 0.0}
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
.hdr-spot{font-family:'JetBrains Mono',monospace;font-size:30px;font-weight:700;color:#fff;letter-spacing:-1px;
  text-shadow:0 0 24px rgba(56,189,248,.25);}
.hdr-r{display:flex;gap:10px;align-items:center;font-size:11px;color:var(--dim);}
#countdown{color:var(--warn);font-weight:700;font-size:13px;font-family:'JetBrains Mono',monospace;}

/* ── Bot status bar ── */
.bbar{background:#060a12;border-bottom:1px solid var(--bdr);padding:6px 20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.badge{display:flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;border:1px solid var(--bdr);
       background:var(--bg3);font-size:10px;font-weight:500;letter-spacing:.3px;transition:all .3s;}
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
.tabbar{display:flex;gap:0;background:#050910;border-bottom:1px solid var(--bdr);padding:0 18px;}
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
.has-tip{position:relative;cursor:help;}
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
.tb-chain-side{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:400px;}
.tb-right-panel{display:flex;flex-direction:column;border-left:1px solid var(--bdr);overflow:hidden;min-width:240px;max-width:600px;width:360px;}
/* Drag handle between chain and right panel */
.tb-drag-handle{
  width:5px;background:var(--bdr);cursor:col-resize;flex-shrink:0;
  transition:background .15s;position:relative;z-index:10;
}
.tb-drag-handle:hover{background:var(--info);}
.tb-drag-handle::after{content:'⋮';position:absolute;top:50%;left:50%;
  transform:translate(-50%,-50%);color:var(--dim);font-size:14px;pointer-events:none;}
/* Config bar — horizontal compact strip */
.tb-cbar{display:flex;align-items:flex-end;gap:10px;padding:8px 14px;background:var(--bg2);
         border-bottom:1px solid var(--bdr);flex-wrap:wrap;}
.tb-cfg-grp{display:flex;flex-direction:column;gap:3px;}
.tb-lbl-sm{font-size:9px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase;}
.tb-inp-sm{background:var(--bg3);border:1px solid var(--bdr);border-radius:5px;color:var(--txt);
           font-size:11px;font-family:'JetBrains Mono',monospace;padding:3px 7px;outline:none;
           width:80px;}
.tb-inp-sm:focus{border-color:var(--info);}
select.tb-inp-sm{width:96px;}
/* Action bar */
.tb-abar{display:flex;align-items:center;gap:12px;padding:6px 14px;
         background:var(--bg3);border-bottom:1px solid var(--bdr);flex-wrap:wrap;}
/* Option chain section */
.tb-chain-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0;}
.tb-chain-hdr{display:flex;align-items:center;justify-content:space-between;
              padding:5px 14px;background:#060a12;border-bottom:1px solid var(--bdr);flex-shrink:0;}
/* Chain columns: OI|Vol|IV|LTP|BTN | STRIKE | BTN|LTP|IV|Vol|OI */
.tb-chain-cols{display:grid;grid-template-columns:75px 65px 50px 75px 46px  90px  46px 75px 50px 65px 75px;
               gap:0;align-items:center;}
.tb-chain-sub-hdr{padding:4px 14px 4px;background:#04080f;border-bottom:1px solid var(--bdr);flex-shrink:0;}
.tb-chain-body{flex:1;overflow-y:auto;min-height:0;}
/* Chain row */
.tb-row{padding:2px 14px;border-bottom:1px solid #060e1a;transition:background .15s;}
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
.tc-oi{color:var(--dim);}
.tc-iv{color:var(--warn);}
.tc-vol{color:#64748b;}
/* LTP flash animation */
@keyframes ltp-up{0%{background:rgba(0,229,160,.35)}100%{background:transparent}}
@keyframes ltp-dn{0%{background:rgba(255,77,109,.35)}100%{background:transparent}}
.ltp-up{animation:ltp-up .7s ease-out}
.ltp-dn{animation:ltp-dn .7s ease-out}
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
.chain-btn{font-size:10px;font-weight:700;padding:3px 8px;border-radius:4px;border:1px solid;
           cursor:pointer;background:none;transition:all .15s;text-align:center;}
.chain-btn.ce{color:var(--bull);border-color:var(--bull);}
.chain-btn.ce:hover{background:rgba(0,229,160,.15);}
.chain-btn.pe{color:var(--bear);border-color:var(--bear);}
.chain-btn.pe:hover{background:rgba(255,77,109,.15);}
/* Trade status panels */
.tb-status-idle{text-align:center;padding:20px 10px;color:var(--dim);}
.tb-buy-form{background:var(--bg3);border:1px solid var(--bdr);border-radius:8px;padding:14px;}
.tb-selected-sym{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;
                 padding:8px 12px;border-radius:6px;margin-bottom:10px;text-align:center;}
.tb-selected-ce{color:var(--bull);background:rgba(0,229,160,.07);border:1px solid var(--bull);}
.tb-selected-pe{color:var(--bear);background:rgba(255,77,109,.07);border:1px solid var(--bear);}
.buy-btn{width:100%;padding:10px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;
         border:none;letter-spacing:.5px;transition:all .2s;font-family:'Inter',sans-serif;}
.buy-btn.ce{background:linear-gradient(135deg,#00b87a,#00e5a0);color:#000;}
.buy-btn.pe{background:linear-gradient(135deg,#c0132e,#ff4d6d);color:#fff;}
.buy-btn:disabled{opacity:.4;cursor:not-allowed;}
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
  <div class="hdr-left">
    <div>
      <div class="hdr-title" id="htitle">📊 NIFTY LIVE DASHBOARD</div>
      <div class="hdr-spot" id="hspot">—</div>
    </div>
  </div>
  <div class="hdr-r">
    <div id="htime" style="font-family:'JetBrains Mono',monospace">—</div>
    <span id="mtc-badge" class="mtc mtc-ok">—m left</span>
    <div>Refresh <span id="countdown">15</span>s</div>
    <button id="picker-btn" onclick="togglePicker()" title="Customize Theme Colors">
      <span class="swatch"></span> Theme
    </button>
  </div>
</div>

<div class="bbar" id="bbar"><span style="color:var(--dim);font-size:11px">BOT STATUS:</span></div>

<!-- Tab bar -->
<div class="tabbar">
  <button class="tab-btn active" onclick="switchTab('dashboard',this)">📊 Live Dashboard</button>
  <button class="tab-btn" onclick="switchTab('pnl',this)">💰 PnL Status</button>
  <button class="tab-btn" onclick="switchTab('trade',this);initTradeTab()">⚡ Trade Board</button>
  <button class="tab-btn" onclick="switchTab('guide',this)">📋 Dashboard Guide</button>
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
      <button id="scalp-toggle" class="toggle-btn toggle-on" onclick="toggle('scalp')">ON</button>
    </div>
    <div id="scalp-text" class="scalp-text scalp-wait">
      <span class="scalp-dim">Generating first scalp plan…</span>
    </div>
  </div>

  <div class="g2">
    <div class="card">
      <div class="ctitle">📐 Key Levels <span class="age" id="lvl-age"></span></div>
      <table class="ltbl">
        <thead><tr><th>Level</th><th>Price</th><th>Distance</th><th>★</th></tr></thead>
        <tbody id="lvlbody"><tr><td colspan="4" style="color:var(--dim);text-align:center">Loading…</td></tr></tbody>
      </table>
      <div id="swing-danger" style="display:none"></div>
    </div>
    <div class="card" id="master-card">
      <div class="ctitle">🎯 Master Signal <span class="age" id="master-age"></span></div>
      <div id="master-body"><div class="offline-warn">⚠ Not running — start MASTER_SIGNAL_BOT.py</div></div>
    </div>
  </div>

  <div class="g2">
    <div class="card">
      <div class="ctitle">📈 Fibonacci Analyzer <span class="age" id="fibo-age"></span></div>
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
        <button id="ai-toggle" class="toggle-btn toggle-on" onclick="toggle('ai')">ON</button>
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

</div>
</div><!-- end #tab-pnl -->

<!-- Trade Board tab -->
<div id="tab-trade" class="tab-pane">

  <!-- ── Config bar ── -->
  <div class="tb-cbar" style="justify-content:flex-start">
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">INDEX</span>
      <select id="tb-index" class="tb-inp-sm" onchange="tbLoadExpiries()">
        <option>NIFTY</option><option>BANKNIFTY</option><option>SENSEX</option><option>FINNIFTY</option>
      </select>
    </div>
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">EXPIRY</span>
      <select id="tb-expiry" class="tb-inp-sm" onchange="tbLoadChain()"></select>
    </div>
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">LOTS</span>
      <input type="number" id="tb-lots" class="tb-inp-sm" value="1" min="1" max="50"
        oninput="tbUpdateLotInfo()">
    </div>
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">HARD SL (pts)</span>
      <input type="number" id="tb-hardsl" class="tb-inp-sm" value="6" step="0.25" min="0.5">
    </div>
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">TRAIL START</span>
      <input type="number" id="tb-trailstart" class="tb-inp-sm" value="1" step="0.25" min="0.25">
    </div>
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">TRAIL STEP</span>
      <input type="number" id="tb-trailstep" class="tb-inp-sm" value="0.75" step="0.25" min="0.25">
    </div>
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">MAX (min)</span>
      <input type="number" id="tb-maxtime" class="tb-inp-sm" value="60" min="5" max="360">
    </div>
    <div style="width:1px;background:var(--bdr);align-self:stretch;margin:0 4px"></div>
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">ATR-SL</span>
      <button id="tb-atr-btn" class="toggle-btn toggle-off" style="font-size:10px;padding:3px 9px" onclick="tbToggleAtr()">OFF</button>
    </div>
    <div class="tb-cfg-grp" id="tb-atr-mult-grp" style="display:none"><span class="tb-lbl-sm">ATR×</span>
      <input type="number" id="tb-atr-mult" class="tb-inp-sm" value="1.0" step="0.25" min="0.25" max="3" style="width:55px">
    </div>
    <div class="tb-cfg-grp"><span class="tb-lbl-sm">PAPER</span>
      <button id="tb-paper-btn" class="toggle-btn toggle-off" style="font-size:10px;padding:3px 9px" onclick="tbTogglePaper()">OFF</button>
    </div>
    <div style="margin-left:auto">
      <button onclick="document.getElementById('picker-panel').classList.toggle('open')"
              style="background:none;border:1px solid var(--bdr);border-radius:20px;color:var(--dim);
                     cursor:pointer;padding:3px 12px;font-size:10px;font-family:'Inter',sans-serif;
                     display:flex;align-items:center;gap:6px">
        <span class="swatch" style="width:13px;height:13px"></span> Theme
      </button>
    </div>
  </div>

  <!-- ── Action bar ── -->
  <div class="tb-abar">
    <div id="tb-selected-display" style="font-size:12px;color:var(--dim)">← Click CE / PE on any strike to select</div>
    <div id="tb-paper-indicator" style="display:none"><span class="paper-badge">PAPER MODE</span></div>
    <div id="chain-lotinfo" style="font-size:10px;color:var(--dim)"></div>
    <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
      <input type="hidden" id="tb-sym-inp">
      <input type="hidden" id="tb-exch-inp" value="NSE">
      <button id="tb-buy-btn" class="buy-btn ce" onclick="tbPlaceBuy()" disabled
              style="padding:7px 28px;font-size:13px">SELECT A STRIKE</button>
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
      </div>
      <div style="display:flex;align-items:center;gap:16px;font-size:10px;color:var(--dim)">
        <span id="chain-rate-info"></span>
        <span id="chain-last-refresh"></span>
        <button onclick="tbLoadChain()" style="background:none;border:1px solid var(--bdr);color:var(--dim);border-radius:4px;padding:2px 8px;cursor:pointer;font-size:9px">↺ Refresh</button>
      </div>
    </div>
    <!-- Column headers -->
    <div class="tb-chain-sub-hdr">
      <div class="tb-chain-cols">
        <span class="tc-lbl" style="text-align:right;color:rgba(0,229,160,.5)">OI</span>
        <span class="tc-lbl" style="text-align:right;color:rgba(0,229,160,.4)">VOL</span>
        <span class="tc-lbl" style="text-align:right;color:rgba(255,193,7,.6)">IV%</span>
        <span class="tc-lbl" style="text-align:right;color:var(--bull);font-weight:700">LTP</span>
        <span class="tc-lbl" style="text-align:center;color:var(--bull)">CE▼</span>
        <span class="tc-lbl" style="text-align:center;font-weight:700;color:var(--info)">STRIKE</span>
        <span class="tc-lbl" style="text-align:center;color:var(--bear)">PE▼</span>
        <span class="tc-lbl" style="text-align:left;color:var(--bear);font-weight:700">LTP</span>
        <span class="tc-lbl" style="text-align:right;color:rgba(255,193,7,.6)">IV%</span>
        <span class="tc-lbl" style="text-align:right;color:rgba(255,77,109,.4)">VOL</span>
        <span class="tc-lbl" style="text-align:right;color:rgba(255,77,109,.5)">OI</span>
      </div>
    </div>
    <div class="tb-chain-body" id="chain-list">
      <div style="text-align:center;color:var(--dim);padding:30px;font-size:12px">Select index &amp; expiry above to load chain</div>
    </div>
  </div><!-- end chain-wrap -->
  </div><!-- end chain-side -->

  <!-- Drag handle -->
  <div class="tb-drag-handle" id="tb-drag-handle" title="Drag to resize"></div>

  <!-- Right panel: trade status on top, log on bottom -->
  <div class="tb-right-panel">

    <!-- Trade Status / P&L (top of right panel) -->
    <div id="tb-trade-status" style="padding:10px;flex-shrink:0;overflow-y:auto;max-height:55%">
      <div style="color:var(--dim);font-size:11px;padding:10px 4px;line-height:1.7">
        <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:8px">TRADE STATUS</div>
        Select a strike (CE/PE) from the chain,<br>
        then click the BUY button.<br><br>
        <span style="color:var(--bdr);font-size:10px">
          Trail SL · ATR SL · Paper mode<br>
          all configurable above
        </span>
      </div>
    </div>

    <!-- Session Log (bottom of right panel) -->
    <div style="flex:1;display:flex;flex-direction:column;border-top:1px solid var(--bdr);overflow:hidden;min-height:0">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 10px;
                  background:var(--bg2);border-bottom:1px solid var(--bdr);flex-shrink:0">
        <span style="font-size:10px;letter-spacing:1px;color:var(--dim);font-weight:600">SESSION LOG</span>
        <button onclick="tbClearLog()" style="background:none;border:1px solid var(--bdr);color:var(--dim);
                border-radius:4px;padding:1px 7px;cursor:pointer;font-size:9px">Clear</button>
      </div>
      <div id="tb-log" style="overflow-y:auto;flex:1;padding:4px 8px;font-size:10px"></div>
    </div>

  </div><!-- end right-panel -->
  </div><!-- end tb-main -->

</div><!-- end #tab-trade -->

<!-- Guide tab -->
<div id="tab-guide" class="tab-pane">
<div class="guide" style="max-width:1400px">

<!-- ── Section 1: Data Source Map ── -->
<div class="gcard-title" style="font-size:14px;margin-bottom:16px">📡 Data Source Map — What Comes From Where</div>
<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px">

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
      <tr><td style="color:var(--txt)">Trade Bot</td><td style="color:var(--info)">logs/groww_bot/</td></tr>
      <tr><td style="color:var(--txt)">Live Option LTP</td><td style="color:var(--bull)">Groww API /v1/live-data/ltp</td></tr>
      <tr><td style="color:var(--txt)">⚡ Scalp Plan</td><td style="color:var(--accent)">Claude CLI (every 60s)</td></tr>
      <tr><td style="color:var(--txt)">🤖 AI Summary</td><td style="color:var(--accent)">Claude CLI (every 3min)</td></tr>
    </table>
  </div>

  <div class="gcard">
    <div class="gcard-title">💰 PnL Status Tab</div>
    <table style="width:100%;font-size:11px;border-collapse:collapse">
      <tr style="color:var(--dim);font-size:9px;letter-spacing:.5px"><th style="text-align:left;padding:3px 0">SECTION</th><th style="text-align:left">SOURCE</th></tr>
      <tr><td style="padding:3px 0;color:var(--txt)">Today's P&L</td><td style="color:var(--bull)">Groww /v1/positions/user</td></tr>
      <tr><td style="color:var(--txt)">Unrealised P&L</td><td style="color:var(--bull)">Groww /v1/live-data/ltp</td></tr>
      <tr><td style="color:var(--txt)">Capital & Margin</td><td style="color:var(--bull)">Groww /v1/margins/detail/user</td></tr>
      <tr><td style="color:var(--txt)">Today's Orders</td><td style="color:var(--bull)">Groww /v1/order/list</td></tr>
      <tr><td style="color:var(--txt)">VIX, NIFTY, PCR</td><td style="color:var(--info)">NSE API via PERSONAL_TRADING_AI</td></tr>
      <tr><td style="color:var(--txt)">Market Score</td><td style="color:var(--info)">PERSONAL_TRADING_AI.py</td></tr>
      <tr><td style="color:var(--txt)">Permission Score</td><td style="color:var(--info)">PERSONAL_TRADING_AI.py</td></tr>
      <tr><td style="color:var(--txt)">Behavioral Risks</td><td style="color:var(--warn)">ayush_previous_data/*.xlsx</td></tr>
      <tr><td style="color:var(--txt)">3-Year Stats</td><td style="color:var(--warn)">ayush_previous_data/*.xlsx</td></tr>
      <tr><td style="color:var(--txt)">Similar Days</td><td style="color:var(--warn)">Excel + yfinance NIFTY data</td></tr>
      <tr><td style="color:var(--txt)">AI Advisory</td><td style="color:var(--accent)">Claude CLI (from PTAI)</td></tr>
    </table>
  </div>

  <div class="gcard">
    <div class="gcard-title">⚡ Trade Board Tab</div>
    <table style="width:100%;font-size:11px;border-collapse:collapse">
      <tr style="color:var(--dim);font-size:9px;letter-spacing:.5px"><th style="text-align:left;padding:3px 0">FEATURE</th><th style="text-align:left">SOURCE</th></tr>
      <tr><td style="padding:3px 0;color:var(--txt)">Option Chain</td><td style="color:var(--bull)">Groww /v1/option-chain/</td></tr>
      <tr><td style="color:var(--txt)">Expiry / Lot Size</td><td style="color:var(--warn)">instrument.csv (local)</td></tr>
      <tr><td style="color:var(--txt)">BUY / SELL Orders</td><td style="color:var(--bull)">Groww /v1/order/create</td></tr>
      <tr><td style="color:var(--txt)">LTP polling (0.2s)</td><td style="color:var(--bull)">Groww /v1/live-data/ltp</td></tr>
      <tr><td style="color:var(--txt)">ATR Calculation</td><td style="color:var(--bull)">Groww /v1/historical/candles</td></tr>
      <tr><td style="color:var(--txt)">Fill Confirmation</td><td style="color:var(--bull)">Groww /v1/order/trades/{id}</td></tr>
    </table>
  </div>
</div>

<!-- ── Section 2: Bot Coverage ── -->
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
  <div class="gcard">
    <div class="gcard-title">✅ Bots Covered in Dashboard</div>
    <div class="grow"><span class="gtag info" style="min-width:150px;font-size:9px">MASTER_SIGNAL_BOT</span><span class="gdesc">Direction, confidence, zone, pattern, scores, SL/target</span></div>
    <div class="grow"><span class="gtag info" style="min-width:150px;font-size:9px">FIBONACCI_ANALYZER</span><span class="gdesc">Fib levels, confluence zones, trade setup, entry triggers</span></div>
    <div class="grow"><span class="gtag info" style="min-width:150px;font-size:9px">CHART_LEVEL_ANALYZER</span><span class="gdesc">Trade decision, option suggestion, S/R levels</span></div>
    <div class="grow"><span class="gtag info" style="min-width:150px;font-size:9px">PREMIUM_TRACKER</span><span class="gdesc">CE/PE flow, LTP, direction</span></div>
    <div class="grow"><span class="gtag info" style="min-width:150px;font-size:9px">SIGNAL_MONITOR</span><span class="gdesc">Combined signal, PDT+FIBO signals</span></div>
    <div class="grow"><span class="gtag info" style="min-width:150px;font-size:9px">PERSONAL_TRADING_AI</span><span class="gdesc">Full PnL tab — market score, permission, behavioral analysis</span></div>
    <div class="grow"><span class="gtag info" style="min-width:150px;font-size:9px">Trade bot logs</span><span class="gdesc">Status card — active/idle, entry, trailing SL</span></div>
  </div>
  <div class="gcard">
    <div class="gcard-title">❌ Not Covered &amp; Why</div>
    <div class="grow"><span class="gtag warn" style="min-width:120px;font-size:9px">ANALYZE_BOT</span><span class="gdesc">Post-trade analysis only — not live signals, runs after market</span></div>
    <div class="grow"><span class="gtag warn" style="min-width:120px;font-size:9px">WEB_TRADING_SERVER</span><span class="gdesc">Replaced by the Trade Board tab in this dashboard</span></div>
    <div class="grow"><span class="gtag warn" style="min-width:120px;font-size:9px">SCALPING_AUTO</span><span class="gdesc">Fully automated loop — dashboard can't control/inject into it</span></div>
    <div class="grow"><span class="gtag warn" style="min-width:120px;font-size:9px">NEWPROD / QA / PROD10FEB</span><span class="gdesc">Legacy backup files — functionality replaced by Trade Board</span></div>
  </div>
</div>

<!-- ── Section 3: Features ── -->
<div class="gcard" style="margin-bottom:16px">
  <div class="gcard-title">🌟 Features &amp; Benefits</div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;font-size:11px">
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Unified view</span><span class="gdesc">All 6 bots on one page — no switching terminals</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Stale detection</span><span class="gdesc">Per-bot timeouts (30–200s) — instant bot death alert</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Consensus signal</span><span class="gdesc">Weighted bull/bear score from 4 sources</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">15s auto-refresh</span><span class="gdesc">Always fresh, no manual reload needed</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Scalp plan (60s)</span><span class="gdesc">AI trade suggestion with entry/target/SL</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">AI summary (3min)</span><span class="gdesc">Full situational analysis from Claude</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Color theme editor</span><span class="gdesc">All colors customizable, saved in browser</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">PnL tab</span><span class="gdesc">Live realised + unrealised P&L + margin + orders</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">3-year stats</span><span class="gdesc">Your personal win rate, behavioral risks</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Similar days</span><span class="gdesc">Your hist. win rate on days matching today's VIX/gap</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Trade Board</span><span class="gdesc">Full option buying with trailing SL — no terminal</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">ATR dynamic SL</span><span class="gdesc">Adapts SL/trail to volatility (same as PROD10FEB)</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Paper trading</span><span class="gdesc">Full test flow without real orders</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">Exec timing</span><span class="gdesc">Buy/sell exec ms — measure Groww server speed</span></div>
    <div class="grow"><span class="gtag bull" style="min-width:0;font-size:9px">P&L range bar</span><span class="gdesc">Visual SL↔Entry↔High bar with live LTP dot</span></div>
  </div>
</div>

<!-- ── Section 4: Known Issues ── -->
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
  <div class="gcard">
    <div class="gcard-title">⚠️ Trade Board Known Issues</div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">ATR when closed</span><span class="gdesc">ATR fetch fails after market hours → falls back to fixed SL. Works fine during market hours.</span></div>
    <div class="grow"><span class="gtag bear" style="font-size:9px">Server restart</span><span class="gdesc">If Python crashes during active trade, trailing SL dies. Exit manually from Groww app.</span></div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">1 trade at a time</span><span class="gdesc">Dashboard supports only 1 active trade simultaneously (by design).</span></div>
    <div class="grow"><span class="gtag dim" style="font-size:9px">Token expiry</span><span class="gdesc">Groww token lasts 2h, auto-renews. Brief LTP gap possible during re-auth.</span></div>
    <div class="grow"><span class="gtag dim" style="font-size:9px">Order TIMEOUT</span><span class="gdesc">If fill takes >8s, uses LTP as fill price — minor inaccuracy.</span></div>
  </div>
  <div class="gcard">
    <div class="gcard-title">🔴 Not Yet Implemented</div>
    <div class="grow"><span class="gtag bear" style="font-size:9px">Auto square-off</span><span class="gdesc">No auto-exit at 3:20 PM. You must exit manually before market close.</span></div>
    <div class="grow"><span class="gtag bear" style="font-size:9px">Telegram alerts</span><span class="gdesc">No Telegram notification on trade entry/exit. Watch session log.</span></div>
    <div class="grow"><span class="gtag bear" style="font-size:9px">Persistent trade state</span><span class="gdesc">Trade state lost if server restarts. No recovery after Python crash.</span></div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">NSE API</span><span class="gdesc">VIX/PCR only works during market hours — shows N/A when closed.</span></div>
    <div class="grow"><span class="gtag warn" style="font-size:9px">yfinance</span><span class="gdesc">Yahoo Finance for NIFTY history — occasionally breaks (third-party).</span></div>
  </div>
</div>

<!-- ── Section 5: Trade Board Complete Flow ── -->
<div class="gcard" style="margin-bottom:16px">
  <div class="gcard-title">⚡ Trade Board — Complete Flow</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;font-size:11px">
    <div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">SETUP</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">1.</b> Select Index + Expiry (from instrument.csv)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">2.</b> Set Lots · Hard SL · Trail Start · Trail Step · Max Time</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">3.</b> Toggle ATR-based SL (optional) + ATR multiplier</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">4.</b> Toggle Paper mode for safe testing</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">5.</b> Click CE/PE on any chain strike → fills symbol</div>
      <div class="gdesc" style="margin-bottom:12px"><b style="color:var(--txt)">6.</b> Click BUY button</div>
      <div style="color:var(--info);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">BUY EXECUTION</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">7.</b> POST /v1/order/create (market, with order_reference_id)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">8.</b> Poll /v1/order/status every 0.2s until COMPLETE</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">9.</b> Fetch fill price from /v1/order/trades/{id}</div>
      <div class="gdesc"><b style="color:var(--bull)">→ BUY EXECUTED logged with millisecond timing</b></div>
    </div>
    <div>
      <div style="color:var(--warn);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">TRAILING MONITOR (0.2s loop)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">10.</b> Calculate Hard SL: entry − 1.5×ATR or entry − fixed_pts</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">11.</b> Every 0.2s: fetch LTP → update unrealised P&L</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">12.</b> If LTP ≤ Hard SL → EXIT (hard stop)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">13.</b> If LTP > entry + trail_start → trail activates</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">14.</b> Trail exit = highest − trail_step (rounded ₹0.05)</div>
      <div class="gdesc" style="margin-bottom:12px"><b style="color:var(--txt)">15.</b> If LTP ≤ trail_exit → EXIT (trail hit)</div>
      <div style="color:var(--bear);font-weight:700;margin-bottom:8px;font-size:10px;letter-spacing:.5px">EXIT &amp; RESULT</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">16.</b> POST /v1/order/create (SELL market)</div>
      <div class="gdesc" style="margin-bottom:5px"><b style="color:var(--txt)">17.</b> Fetch actual sell price from trades API</div>
      <div class="gdesc"><b style="color:var(--bull)">→ P&L · Buy exec ms · Sell exec ms · Total time shown</b></div>
    </div>
  </div>
</div>

<!-- ── Section 6: Safety Assessment ── -->
<div class="gcard" style="margin-bottom:16px">
  <div class="gcard-title">🛡️ Is It Safe to Use Live?</div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;font-size:11px">
    <div>
      <div style="color:var(--bull);font-weight:700;margin-bottom:8px">✅ Safe</div>
      <div class="gdesc" style="margin-bottom:5px">Order logic identical to PROD10FEB</div>
      <div class="gdesc" style="margin-bottom:5px">ATR, trailing, Hard SL ported correctly</div>
      <div class="gdesc" style="margin-bottom:5px">Paper mode works perfectly for rehearsal</div>
      <div class="gdesc" style="margin-bottom:5px">Lot size from instruments.csv (correct)</div>
      <div class="gdesc">order_reference_id fixed + timing verified</div>
    </div>
    <div>
      <div style="color:var(--warn);font-weight:700;margin-bottom:8px">⚠️ Be Careful</div>
      <div class="gdesc" style="margin-bottom:5px">Don't restart Python during active trade</div>
      <div class="gdesc" style="margin-bottom:5px">Test with 1 lot first before full size</div>
      <div class="gdesc" style="margin-bottom:5px">ATR unreliable in first 10-15 min after open</div>
      <div class="gdesc">NIFTY options fine; SENSEX/BANKEX can gap</div>
    </div>
    <div>
      <div style="color:var(--dim);font-weight:700;margin-bottom:8px">🔴 Manual Workarounds</div>
      <div class="gdesc" style="margin-bottom:5px">Square off before 3:20 PM manually</div>
      <div class="gdesc" style="margin-bottom:5px">No Telegram — watch session log</div>
      <div class="gdesc">Trade state lost on server restart — monitor from Groww app if Python dies</div>
    </div>
  </div>
</div>

<!-- ── Section 7: Original Beginner Guide ── -->
<div class="gcard-title" style="font-size:14px;margin-bottom:16px">📖 Trading Signals Guide</div>
<div class="guide-grid">
  <div class="guide-grid">

    <!-- What each signal means -->
    <div class="gcard">
      <div class="gcard-title">🚦 What Do CE and PE Mean?</div>
      <div class="grow"><span class="gtag bull">BUY CE ▲</span><span class="gdesc"><b>Call Option = Bullish.</b> You expect price to go UP. You profit if NIFTY rises above your strike.</span></div>
      <div class="grow"><span class="gtag bear">BUY PE ▼</span><span class="gdesc"><b>Put Option = Bearish.</b> You expect price to go DOWN. You profit if NIFTY falls below your strike.</span></div>
      <div class="grow"><span class="gtag warn">WAIT ─</span><span class="gdesc"><b>No clear direction.</b> Bots disagree or price is at a major level. Do not enter — wait for confirmation.</span></div>
      <hr class="gdivider">
      <div class="grow"><span class="gtag info">BREAK</span><span class="gdesc">Entry type — wait for the option price to break <b>above the trigger level</b> before buying.</span></div>
      <div class="grow"><span class="gtag bull">NOW</span><span class="gdesc">Entry type — buy <b>at current market price</b> immediately; setup is confirmed.</span></div>
    </div>

    <!-- Consensus box -->
    <div class="gcard">
      <div class="gcard-title">📊 Consensus Box (Top Section)</div>
      <div class="grow"><span class="gtag bull">STRONG CE</span><span class="gdesc">6+ bull votes. <b>All bots agree</b> — high-confidence bullish. Strongest buy signal.</span></div>
      <div class="grow"><span class="gtag bull">CE ▲</span><span class="gdesc">3–5 bull votes. <b>Mild bullish lean.</b> Wait for entry trigger before buying.</span></div>
      <div class="grow"><span class="gtag warn">WAIT ─</span><span class="gdesc"><b>Balanced or unclear.</b> Bots conflict. Stay out until a clear signal forms.</span></div>
      <div class="grow"><span class="gtag bear">PE ▼</span><span class="gdesc">3–5 bear votes. <b>Mild bearish lean.</b> Wait for breakdown confirmation.</span></div>
      <div class="grow"><span class="gtag bear">STRONG PE</span><span class="gdesc">6+ bear votes. <b>All bots agree</b> — high-confidence bearish. Strongest sell signal.</span></div>
      <hr class="gdivider">
      <div class="gdesc" style="font-size:11px">Each bot casts votes: MASTER SIGNAL (up to 3), FIBO (2), CHART (3), SIGNAL MONITOR (2). Scores are added to Bull or Bear total.</div>
    </div>

    <!-- Key Levels & Stars -->
    <div class="gcard">
      <div class="gcard-title">📐 Key Levels Table + ★ Stars</div>
      <div class="gdesc" style="margin-bottom:10px">Levels are <b>price zones</b> where the market is likely to react (reverse or accelerate). The star rating shows <b>how strong</b> each level is.</div>
      <div class="gstar-row"><span class="gstar-val">★☆☆☆☆</span><span class="gstar-meaning"><b>Weak</b> — single source, 1–2 touches. Often ignored by market.</span></div>
      <div class="gstar-row"><span class="gstar-val">★★☆☆☆</span><span class="gstar-meaning"><b>Moderate</b> — 2 sources or multiple touches. Watch but don't rely on it alone.</span></div>
      <div class="gstar-row"><span class="gstar-val">★★★☆☆</span><span class="gstar-meaning"><b>Good</b> — 3 sources agree at this price. Likely to cause a bounce or break.</span></div>
      <div class="gstar-row"><span class="gstar-val">★★★★☆</span><span class="gstar-meaning"><b>Strong</b> — 4 sources. High-probability reaction zone. Plan your trade here.</span></div>
      <div class="gstar-row"><span class="gstar-val">★★★★★</span><span class="gstar-meaning"><b>Very Strong</b> — 5+ sources. <b>Major S/R level.</b> Price almost always reacts here.</span></div>
      <hr class="gdivider">
      <div class="grow"><span class="gtag bear" style="min-width:100px">▲ Red rows</span><span class="gdesc">Resistance above spot — levels that may <b>stop price from rising</b>.</span></div>
      <div class="grow"><span class="gtag bull" style="min-width:100px">▼ Green rows</span><span class="gdesc">Support below spot — levels that may <b>stop price from falling</b>.</span></div>
      <div class="grow"><span class="gtag warn" style="min-width:100px">BLINKING</span><span class="gdesc">Price is within 6 points of this level — <b>at the level right now</b>. Be extra careful.</span></div>
    </div>

    <!-- Master Signal scores -->
    <div class="gcard">
      <div class="gcard-title">🎯 Master Signal — Score Chips</div>
      <div class="gdesc" style="margin-bottom:10px">Each chip shows the signal from one timeframe. More <b>▲ arrows = stronger bullish</b>. More <b>▼ arrows = stronger bearish</b>.</div>
      <div class="grow"><span class="gtag info">1H</span><span class="gdesc"><b>1-Hour timeframe</b> trend. Most reliable for direction. ▲▲▲ = strong bull. ▼▼▼ = strong bear.</span></div>
      <div class="grow"><span class="gtag info">15M</span><span class="gdesc"><b>15-Minute timeframe</b>. Shows near-term momentum. ▲ = bullish candles forming. ▼ = sellers active.</span></div>
      <div class="grow"><span class="gtag info">5M</span><span class="gdesc"><b>5-Minute timeframe</b>. Latest candle direction. Useful for entry timing.</span></div>
      <div class="grow"><span class="gtag info">Prem</span><span class="gdesc"><b>Option Premium Flow</b>. ▲ = CE premium rising (buyers entering). ▼ = PE premium rising (sellers active).</span></div>
      <hr class="gdivider">
      <div class="gdesc" style="font-size:11px;margin-bottom:6px"><b>R:R</b> = Reward to Risk ratio. 2:1 means potential gain is 2× the potential loss. Aim for ≥ 2:1.</div>
      <div class="gdesc" style="font-size:11px"><b>Zone</b> = Where price sits in the Fibonacci range. "0–23.6% near swing low" means price is near the bottom of its range.</div>
    </div>

    <!-- Fibonacci -->
    <div class="gcard">
      <div class="gcard-title">📈 Fibonacci Analyzer</div>
      <div class="gdesc" style="margin-bottom:10px">Fibonacci levels divide the day's price range into mathematically significant zones. Traders worldwide watch these levels.</div>
      <div class="grow"><span class="gtag dim">R23.6%</span><span class="gdesc">Shallow retracement. Price often bounces here during strong trends.</span></div>
      <div class="grow"><span class="gtag dim">R38.2%</span><span class="gdesc">Common retracement zone. Strong support/resistance in trending markets.</span></div>
      <div class="grow"><span class="gtag dim">R50.0%</span><span class="gdesc">Midpoint. Psychological level — half of the day's move.</span></div>
      <div class="grow"><span class="gtag dim">R61.8%</span><span class="gdesc"><b>Golden ratio.</b> Most important Fib level. Very high probability of reaction here.</span></div>
      <div class="grow"><span class="gtag dim">R78.6%</span><span class="gdesc">Deep retracement. If price reaches here in a trend, the trend may be weakening.</span></div>
      <div class="grow"><span class="gtag dim">E127–261%</span><span class="gdesc">Extension targets — where price may go <b>beyond the day's range</b> if it breaks out.</span></div>
      <hr class="gdivider">
      <div class="gdesc" style="font-size:11px"><b>CE trigger</b> = price level that confirms bullish entry. <b>PE trigger</b> = level that confirms bearish entry. Do NOT enter before price reaches these.</div>
    </div>

    <!-- Bots explained -->
    <div class="gcard">
      <div class="gcard-title">🤖 What Each Bot Does</div>
      <div class="grow"><span class="gtag info" style="min-width:120px">MASTER SIGNAL</span><span class="gdesc"><b>Core signal bot.</b> Combines 1H+15M+5M Fibonacci analysis + premium flow to give CE/PE/WAIT signal with confidence %.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">FIBONACCI</span><span class="gdesc"><b>Level detector.</b> Calculates day + 15M Fibonacci zones, confluence levels, and gives specific entry/target/SL.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">CHART LEVEL</span><span class="gdesc"><b>S/R analyzer.</b> Finds support and resistance from swing highs/lows, VWAP, pivot points. Triggers sound alarm on CE/PE signal.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">PREMIUM TRACKER</span><span class="gdesc"><b>Options flow.</b> Watches CE and PE option prices in real-time. UP = buyers entering, DOWN = price falling.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">SIGNAL MONITOR</span><span class="gdesc"><b>Signal combiner.</b> Merges MASTER + FIBONACCI signals into a single STRONG CE / STRONG PE verdict.</span></div>
      <div class="grow"><span class="gtag info" style="min-width:120px">TRADE BOT</span><span class="gdesc"><b>Order executor.</b> Places and monitors actual trades on Groww. Shows entry price, trailing SL, live LTP.</span></div>
    </div>

  </div><!-- end inner guide-grid -->
  </div><!-- end outer guide-grid -->

  <!-- Color coding full reference -->
  <div class="gcard" style="margin-bottom:14px">
    <div class="gcard-title">🎨 Color Coding Reference</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px">
      <div><div style="color:var(--bull);font-weight:700;margin-bottom:4px">■ Green / Teal</div><div class="gdesc">Bullish · Up · Profit · CE · Support · Good R:R</div></div>
      <div><div style="color:var(--bear);font-weight:700;margin-bottom:4px">■ Red / Pink</div><div class="gdesc">Bearish · Down · Loss · PE · Resistance · AT LEVEL</div></div>
      <div><div style="color:var(--warn);font-weight:700;margin-bottom:4px">■ Yellow / Amber</div><div class="gdesc">Caution · WAIT · Stale data · Moderate signal</div></div>
      <div><div style="color:var(--info);font-weight:700;margin-bottom:4px">■ Blue / Cyan</div><div class="gdesc">Info · SPOT marker · 1H data · Dashboard headers</div></div>
      <div><div style="color:var(--accent);font-weight:700;margin-bottom:4px">■ Purple</div><div class="gdesc">AI Summary · Claude-generated analysis</div></div>
      <div><div style="color:var(--dim);font-weight:700;margin-bottom:4px">■ Gray / Dim</div><div class="gdesc">Secondary data · Old/stale signal · Labels</div></div>
    </div>
  </div>

  <!-- Quick how-to -->
  <div class="gcard">
    <div class="gcard-title">⚡ Quick Trading Workflow</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div>
        <div class="gdesc" style="margin-bottom:8px"><b style="color:var(--info)">Step 1 — Check Consensus</b><br>Look at the big box at the top. STRONG CE = bullish, STRONG PE = bearish, WAIT = stay out.</div>
        <div class="gdesc" style="margin-bottom:8px"><b style="color:var(--info)">Step 2 — Check Key Levels</b><br>Is SPOT near a ★★★★★ level? If yes → expect a reaction. Plan entry above (CE) or below (PE) that level.</div>
        <div class="gdesc"><b style="color:var(--info)">Step 3 — Check Master Signal</b><br>Confirm 15M + 5M chips agree with consensus direction. R:R should be ≥ 2.0.</div>
      </div>
      <div>
        <div class="gdesc" style="margin-bottom:8px"><b style="color:var(--info)">Step 4 — Check Scalp Plan</b><br>Read the single-line ⚡ Scalp Plan at the top. It gives you specific entry, target, and SL.</div>
        <div class="gdesc" style="margin-bottom:8px"><b style="color:var(--info)">Step 5 — Check Option Suggestion</b><br>Note the strike and LTP. Compare <i>LTP at signal</i> vs <i>Now</i> — if now price moved in your direction, setup is still valid.</div>
        <div class="gdesc"><b style="color:var(--warn)">🚫 Do NOT trade if:</b><br>WAIT consensus · All chips are ─ · R:R &lt; 1.5 · Signal is OLD (>5 min) · Market closes in &lt;20 min.</div>
      </div>
    </div>
  </div>

</div><!-- end .guide -->
</div><!-- end #tab-guide -->

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
  const b=d.bots||{}, master=b.master||{}, fibo=b.fibo||{},
        csig=b.chart_signal||{}, cdec=b.chart_decision||{},
        prem=b.premium||{}, trade=b.trade||{}, sigmon=b.signal_monitor||{},
        cons=d.consensus||{}, liveChain=(d.live_chain||{}).chain||{};
  const spot=d.spot||0;

  // Header
  $('htitle').textContent=`📊 ${d.index||'NIFTY'} LIVE DASHBOARD`;
  $('hspot').textContent=spot?fmt(spot):'—';
  $('htime').textContent=(d.ts||'').replace('T',' ');

  // Bot bar
  $('bbar').innerHTML=`<span style="color:var(--dim);font-size:11px">BOT STATUS:</span>
    ${badge(master,'MASTER SIGNAL')}
    ${badge(fibo,'FIBONACCI')}
    ${badge(cdec.ts?cdec:csig,'CHART LEVEL')}
    ${badge(prem,'PREMIUM TRACKER')}
    ${badge(trade,'TRADE BOT')}
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
    const above  = sorted.filter(l => l.price > spot).slice(-5).reverse();
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

  // Fibonacci
  if(fibo.day_high){
    const pct=fibo.day_high&&fibo.day_low?((spot-fibo.day_low)/(fibo.day_high-fibo.day_low)*100).toFixed(0):'—';
    let h=`<div class="row"><span class="lbl">Day Range</span><span class="v">
              <span class="vbull">H ${fmt(fibo.day_high,0)}</span>  <span class="vbear">L ${fmt(fibo.day_low,0)}</span></span></div>
           <div class="row"><span class="lbl">Position</span><span class="v">${pct}% (${(fibo.day_dir||'').toUpperCase()})</span></div>`;
    if(fibo.zone_1h) h+=`<div class="row"><span class="lbl">1H Zone</span><span class="vinfo" style="font-size:11px">${fibo.zone_1h}</span></div>`;
    (fibo.confluence||[]).slice(0,4).forEach(c=>{
      const dp=parseFloat(c.dist_pts), cls=dp>0?'vbear':'vbull', arr=dp>0?'▲':'▼';
      h+=`<div class="row"><span class="lbl">${'★'.repeat(c.stars)} ${fmt(c.price,0)}</span>
          <span class="${cls}">${arr}${Math.abs(dp).toFixed(0)}pts <span style="color:var(--dim);font-size:10px">[${c.tags}]</span></span></div>`;
    });
    h+=`<div style="margin-top:8px;border-top:1px solid var(--bdr);padding-top:7px;">`;
    if(fibo.ce_trigger) h+=`<div class="row"><span class="lbl" style="color:var(--bull)">CE trigger</span><span class="vbull">${fibo.ce_trigger}</span></div>`;
    if(fibo.pe_trigger) h+=`<div class="row"><span class="lbl" style="color:var(--bear)">PE trigger</span><span class="vbear">${fibo.pe_trigger}</span></div>`;
    h+=`</div>`;
    if(fibo.trade_setup) h+=`<div style="margin-top:7px;font-size:11px;color:#9ca3af">${fibo.trade_setup.substring(0,140)}</div>`;
    $('fibo-body').innerHTML=h;
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
  const mtc = d.mins_to_close ?? 0;
  const mtcEl = $('mtc-badge');
  mtcEl.textContent = mtc > 0 ? `${mtc}m left` : 'CLOSED';
  mtcEl.className = `mtc ${mtc > 60 ? 'mtc-ok' : mtc > 20 ? 'mtc-warn' : 'mtc-close'}`;

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
}

/* ── Color Picker ── */
const DEFAULTS = {
  '--bg':'#070b14','--bg2':'#0c1220','--bg3':'#131c30','--hdr-bg':'#080f1e',
  '--bull':'#00e5a0','--bull2':'#001a10','--bear':'#ff4d6d','--bear2':'#1a0010',
  '--warn':'#ffc107','--info':'#38bdf8',
  '--txt':'#e2e8f0','--dim':'#5a7298',
  '--bdr':'#1c2d48','--accent':'#a855f7'
};
// Map CSS var name → picker element id suffix
const VAR_ID = {
  '--bg':'bg','--bg2':'bg2','--bg3':'bg3','--hdr-bg':'hdrbg',
  '--bull':'bull','--bull2':'bull2','--bear':'bear','--bear2':'bear2',
  '--warn':'warn','--info':'info',
  '--txt':'txt','--dim':'dim',
  '--bdr':'bdr','--accent':'accent'
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
  if(panel && !panel.contains(e.target) && e.target!==btn && !btn.contains(e.target))
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

async function load(){
  try{const r=await fetch('/api/data'); render(await r.json());}
  catch(e){console.error(e);}
}

function startTick(){
  clearInterval(tim); cd=R; $('countdown').textContent=cd;
  tim=setInterval(()=>{
    cd--; $('countdown').textContent=Math.max(0,cd);
    if(cd<=0){load(); cd=R;}
  },1000);
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

function _age(ts){
  if(!ts) return '—';
  const s = Math.max(0, (Date.now() - new Date(ts).getTime())/1000);
  if(s<60) return Math.round(s)+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  return Math.floor(s/3600)+'h ago';
}
function setText(id,v){ const e=$(id); if(e) e.textContent=v; }
function setBar(id,pct,cls){ const e=$(id); if(e){e.style.width=Math.min(pct,100)+'%'; e.className='score-bar '+cls;} }

/* ── Trade Board ── */
let _tbPaper=false,_tbAtr=false,_tbSym='',_tbExch='NSE',_tbDir='',_tbPoll=null,_tbEntryEpoch=0,_tbTradeTimer=null;
let _tbLotSize=75;   // updated from live chain response
let _tbPrevUnr=null; // for trend arrow (↑↓) detection

function initTradeTab(){
  if(!$('tb-expiry').options.length) tbLoadExpiries();
  tbRestoreState();
  initTbResizer();
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
    const newW = Math.max(200, Math.min(700, startW - (e.clientX - startX)));
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
  if(t.status&&t.status!=='IDLE'){
    tbRenderStatus(t); tbRenderLog(t.log||[]);
    if(t.status==='ACTIVE'||t.status==='BUYING'||t.status==='EXITING') tbStartPoll();
  }
}

async function tbLoadExpiries(){
  const idx=$('tb-index').value;
  const r=await fetch(`/api/trade/expiries?index=${idx}`);
  const d=await r.json();
  const sel=$('tb-expiry');
  sel.innerHTML=(d.expiries||[]).map(e=>`<option value="${e}">${e}</option>`).join('');
  if(d.expiries&&d.expiries.length) tbLoadChain();
}

let _tbChainData = [];   // full strike list from last fetch
let _tbChainSpot = 0;
let _tbPrevLTPs  = {};   // {strike+"CE": ltp, strike+"PE": ltp} for flash detection
let _tbChainTimer = null;
// Rate budget: 10/sec, 300/min (Live Data)
// Trail loop: 5/sec (0.2s sleep) = 300/min
// Chain refresh (no trade):     2/sec → 500ms  (total: 2/sec,  120/min  — 40% budget)
// Chain refresh (active trade): 1/sec → 1000ms (total: 6/sec,  360/min  — within 10/sec burst; 1-min window: trail uses 300 + chain 60 = 360 — some risk but practical limit is higher)
const _TB_CHAIN_REFRESH_MS        = 500;   // 500ms — 2 refreshes/sec when no trade
const _TB_CHAIN_REFRESH_ACTIVE_MS = 1000;  // 1s — when trail loop running concurrently

function tbUpdateLotInfo(){
  const e=$('chain-lotinfo');
  if(!e) return;
  const lots=parseInt($('tb-lots').value||1);
  e.textContent=`Lot size: ${_tbLotSize} · ${lots} lot${lots>1?'s':''} = ${lots*_tbLotSize} qty`;
}

function _fmtOI(n){ if(!n) return '—'; if(n>=10000000) return (n/10000000).toFixed(1)+'Cr'; if(n>=100000) return (n/100000).toFixed(1)+'L'; if(n>=1000) return (n/1000).toFixed(1)+'K'; return n+''; }

async function tbLoadChain(){
  const idx=$('tb-index').value, expiry=$('tb-expiry').value;
  if(!expiry) return;
  $('chain-list').innerHTML='<div style="text-align:center;color:var(--dim);padding:30px">Loading option chain…</div>';
  const r=await fetch(`/api/trade/chain?index=${idx}&expiry=${expiry}`);
  const d=await r.json();
  if(d.error&&!d.strikes?.length){
    $('chain-list').innerHTML=`<div style="color:var(--warn);padding:12px">⚠ ${d.error}</div>`; return;
  }
  _tbChainData = d.strikes||[];
  _tbChainSpot = d.spot||0;
  _tbLotSize   = d.lot_size||75;
  _tbPrevLTPs  = {};
  tbUpdateLotInfo();
  tbRenderChain();
  tbStartChainRefresh();
}

function tbRenderChain(){
  const idx    = $('tb-index')?.value||'NIFTY';
  const exch   = (idx==='SENSEX'||idx==='BANKEX')?'BSE':'NSE';
  const spot   = _tbChainSpot;
  const strikes= _tbChainData;
  if(!strikes.length) return;

  $('chain-spot').textContent = spot ? 'SPOT  ₹'+fmtN(spot,2) : '';

  let atmIdx=0; let minDiff=Infinity;
  strikes.forEach((s,i)=>{ const df=Math.abs(s.strike-spot); if(df<minDiff){minDiff=df;atmIdx=i;} });
  const step = strikes.length>1 ? strikes[1].strike-strikes[0].strike : 50;
  const fr=Math.max(0,atmIdx-20); const to=Math.min(strikes.length,atmIdx+21);

  $('chain-list').innerHTML = strikes.slice(fr,to).map(s=>{
    const isATM  = Math.abs(s.strike-spot)<step/2;
    const isITMce= s.strike<spot;   // CE ITM when strike < spot
    const isITMpe= s.strike>spot;   // PE ITM when strike > spot
    const rowCls = isATM?'atm-row':isITMce?'itm-ce':isITMpe?'itm-pe':'';
    const stk    = Math.round(s.strike);
    const atmTag = isATM?'<span style="font-size:8px;color:var(--info);margin-left:3px;font-weight:700">ATM</span>':'';
    return `<div class="tb-row ${rowCls}">
      <div class="tb-chain-cols">
        <span class="tc tc-oi">${_fmtOI(s.ce_oi)}</span>
        <span class="tc tc-vol">${_fmtOI(s.ce_vol)}</span>
        <span class="tc tc-iv">${s.ce_iv>0?s.ce_iv.toFixed(1)+'%':'—'}</span>
        <span class="tc tc-ce-ltp" id="ceLTP${stk}">${s.ce_ltp>0?fmtN(s.ce_ltp,2):'—'}</span>
        <span style="text-align:center">
          <button class="chain-btn ce" onclick="tbSelect('${s.ce_sym}','${exch}','CE')" ${s.ce_sym?'':'disabled'} style="padding:2px 6px;font-size:10px">▼CE</button>
        </span>
        <span class="tc tc-strike" style="color:${isATM?'var(--info)':'var(--txt)'}">${fmtN(s.strike,0)}${atmTag}</span>
        <span style="text-align:center">
          <button class="chain-btn pe" onclick="tbSelect('${s.pe_sym}','${exch}','PE')" ${s.pe_sym?'':'disabled'} style="padding:2px 6px;font-size:10px">▼PE</button>
        </span>
        <span class="tc tc-pe-ltp" id="peLTP${stk}">${s.pe_ltp>0?fmtN(s.pe_ltp,2):'—'}</span>
        <span class="tc tc-iv">${s.pe_iv>0?s.pe_iv.toFixed(1)+'%':'—'}</span>
        <span class="tc tc-vol">${_fmtOI(s.pe_vol)}</span>
        <span class="tc tc-oi">${_fmtOI(s.pe_oi)}</span>
      </div>
    </div>`;
  }).join('');

  // Init prev LTP map for flash detection
  strikes.slice(fr,to).forEach(s=>{
    const k=Math.round(s.strike);
    _tbPrevLTPs['ce'+k]=s.ce_ltp||0; _tbPrevLTPs['pe'+k]=s.pe_ltp||0;
  });
}

function tbUpdateChainLTPs(newStrikes){
  // Update only LTP cells in-place with flash animation — no full re-render
  newStrikes.forEach(s=>{
    const k=Math.round(s.strike);
    const ceEl=$('ceLTP'+k); const peEl=$('peLTP'+k);
    if(ceEl&&s.ce_ltp>0){
      const prev=_tbPrevLTPs['ce'+k]||0;
      ceEl.textContent=fmtN(s.ce_ltp,2);
      if(s.ce_ltp>prev&&prev>0){ceEl.classList.remove('ltp-dn','ltp-up');void ceEl.offsetWidth;ceEl.classList.add('ltp-up');}
      else if(s.ce_ltp<prev&&prev>0){ceEl.classList.remove('ltp-up','ltp-dn');void ceEl.offsetWidth;ceEl.classList.add('ltp-dn');}
      _tbPrevLTPs['ce'+k]=s.ce_ltp;
    }
    if(peEl&&s.pe_ltp>0){
      const prev=_tbPrevLTPs['pe'+k]||0;
      peEl.textContent=fmtN(s.pe_ltp,2);
      if(s.pe_ltp>prev&&prev>0){peEl.classList.remove('ltp-dn','ltp-up');void peEl.offsetWidth;peEl.classList.add('ltp-up');}
      else if(s.pe_ltp<prev&&prev>0){peEl.classList.remove('ltp-up','ltp-dn');void peEl.offsetWidth;peEl.classList.add('ltp-dn');}
      _tbPrevLTPs['pe'+k]=s.pe_ltp;
    }
  });
  // Update spot
  if(newStrikes._spot) $('chain-spot').textContent='SPOT  ₹'+fmtN(newStrikes._spot,2);
}

function _isMarketOpen(){
  const n=new Date(); const h=n.getHours(); const m=n.getMinutes();
  const dayOk=n.getDay()>=1&&n.getDay()<=5;
  const timeOk=(h>9||(h===9&&m>=15))&&(h<15||(h===15&&m<30));
  return dayOk&&timeOk;
}

function tbStartChainRefresh(){
  if(_tbChainTimer) clearInterval(_tbChainTimer);
  const interval = _tbPoll ? _TB_CHAIN_REFRESH_ACTIVE_MS : _TB_CHAIN_REFRESH_MS;
  const badge=$('chain-refresh-badge');
  const rateInfo=$('chain-rate-info');
  if(!_isMarketOpen()){
    if(badge){badge.style.display='none';}
    if(rateInfo) rateInfo.textContent='Market closed — manual refresh only';
    return;
  }
  if(badge){
    badge.style.display='inline';
    badge.style.background='rgba(0,229,160,.1)';badge.style.color='var(--bull)';badge.style.border='1px solid var(--bull)';
    badge.textContent='● LIVE';
  }
  const callsPerMin=Math.round(60000/interval);
  const trailCalls = _tbPoll ? 300 : 0;  // trail loop uses ~300/min when active
  const totalMin   = callsPerMin + trailCalls;
  const pct        = Math.round(totalMin/300*100);
  if(rateInfo) rateInfo.textContent=`Chain: ${callsPerMin}/min${_tbPoll?' + Trail: 300/min = '+totalMin+'/min ('+pct+'% of 300 budget)':' ('+pct+'% of 300/min budget)'}  |  ↻ every ${interval<1000?interval+'ms':interval/1000+'s'}`;
  _tbChainTimer=setInterval(async()=>{
    const idx=$('tb-index')?.value; const expiry=$('tb-expiry')?.value;
    if(!idx||!expiry||!_isMarketOpen()) return;
    try{
      const r=await fetch(`/api/trade/chain?index=${idx}&expiry=${expiry}`);
      const d=await r.json();
      if(d.strikes?.length){
        _tbChainData=d.strikes; _tbChainSpot=d.spot||_tbChainSpot;
        d.strikes._spot=d.spot; tbUpdateChainLTPs(d.strikes);
        $('chain-spot').textContent='SPOT  ₹'+fmtN(d.spot,2);
        const lr=$('chain-last-refresh'); if(lr) lr.textContent='Updated '+new Date().toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      }
    }catch(e){}
  }, interval);
}

function tbSelect(sym,exch,dir){
  _tbSym=sym; _tbExch=exch; _tbDir=dir;
  $('tb-sym-inp').value=sym; $('tb-exch-inp').value=exch;
  $('tb-selected-display').innerHTML=`Selected: <span style="font-family:'JetBrains Mono',monospace;font-weight:700;color:${dir==='CE'?'var(--bull)':'var(--bear)'}">${sym}</span>`;
  const btn=$('tb-buy-btn');
  btn.disabled=false; btn.textContent=`BUY ${dir} — ${sym}`;
  btn.className=`buy-btn ${dir.toLowerCase()}`;
}

function tbTogglePaper(){
  _tbPaper=!_tbPaper;
  const btn=$('tb-paper-btn');
  btn.textContent=_tbPaper?'ON':'OFF';
  btn.className=`toggle-btn ${_tbPaper?'toggle-on':'toggle-off'}`;
  $('tb-paper-indicator').style.display=_tbPaper?'block':'none';
}

function tbToggleAtr(){
  _tbAtr=!_tbAtr;
  const btn=$('tb-atr-btn');
  btn.textContent=_tbAtr?'ON':'OFF';
  btn.className=`toggle-btn ${_tbAtr?'toggle-on':'toggle-off'}`;
  const mg=$('tb-atr-mult-grp'); if(mg) mg.style.display=_tbAtr?'flex':'none';
  const hsl=$('tb-hardsl'); const ts=$('tb-trailstep');
  if(hsl){ hsl.disabled=_tbAtr; hsl.style.opacity=_tbAtr?'0.4':'1'; }
  if(ts){  ts.disabled=_tbAtr;  ts.style.opacity=_tbAtr?'0.4':'1'; }
}

async function tbPlaceBuy(){
  if(!_tbSym){ alert('Select a strike first'); return; }
  const qty=parseInt($('tb-lots').value||1)*_tbLotSize;
  const payload={symbol:_tbSym,exchange:_tbExch,qty,paper:_tbPaper,
    hard_sl_pts:parseFloat($('tb-hardsl').value||6),
    trail_start:parseFloat($('tb-trailstart').value||1),
    trail_step:parseFloat($('tb-trailstep').value||0.75),
    max_sec:parseInt($('tb-maxtime').value||60)*60,
    atr_based:_tbAtr,
    atr_multiplier:parseFloat($('tb-atr-mult')?.value||1.0)};
  if(_tbAtr) $('tb-buy-btn').textContent='⏳ Fetching ATR…';
  $('tb-buy-btn').disabled=true; $('tb-buy-btn').textContent='⏳ Placing order…';
  const r=await fetch('/api/trade/buy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d=await r.json();
  if(!d.ok){ $('tb-buy-btn').disabled=false; $('tb-buy-btn').textContent=`BUY ${_tbDir} — ${_tbSym}`; alert('Error: '+(d.error||'unknown')); return; }
  tbStartPoll();
}

async function tbForceExit(){
  const btn=$('tb-exit-btn'); if(btn) btn.disabled=true;
  await fetch('/api/trade/exit',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
}

async function tbReset(){
  await fetch('/api/trade/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  $('tb-trade-status').innerHTML=`<div style="color:var(--dim);font-size:11px;padding:10px 4px;line-height:1.7">
    <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:8px">TRADE STATUS</div>
    Select a strike (CE/PE) from the chain,<br>then click the BUY button.
  </div>`;
  $('tb-buy-btn').disabled=false;
  $('tb-buy-btn').textContent=_tbSym?`BUY ${_tbDir} — ${_tbSym}`:'SELECT A STRIKE TO BUY';
}

function tbStartPoll(){ if(_tbPoll) clearInterval(_tbPoll); _tbPoll=setInterval(tbPollStatus,500); tbPollStatus(); }

async function tbPollStatus(){
  const r=await fetch('/api/trade/status'); const t=await r.json();
  tbRenderStatus(t); tbRenderLog(t.log||[]);
  if(t.status==='DONE'||t.status==='IDLE'){
    clearInterval(_tbPoll); _tbPoll=null;
    if(_tbTradeTimer){clearInterval(_tbTradeTimer);_tbTradeTimer=null;}
    if(t.status==='DONE'){ $('tb-buy-btn').disabled=false; $('tb-buy-btn').textContent=`BUY ${_tbDir} — ${_tbSym}`; }
  }
}

function fmtN(n,dec=0){ return new Intl.NumberFormat('en-IN',{minimumFractionDigits:dec,maximumFractionDigits:dec}).format(n||0); }
function msStr(ms){ if(!ms) return '—'; if(ms<1000) return ms+'ms'; return (ms/1000).toFixed(2)+'s'; }

function tbRenderStatus(t){
  const el=$('tb-trade-status');
  if(!t||t.status==='IDLE'){
    el.innerHTML=`<div style="color:var(--dim);font-size:11px;padding:10px 4px;line-height:1.7">
      <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:8px">TRADE STATUS</div>
      Select a strike (CE/PE) from the chain,<br>then click the BUY button.<br><br>
      <span style="color:var(--bdr);font-size:10px">Trail SL · ATR SL · Paper mode<br>all configurable above</span>
    </div>`;
    return;
  }
  if(t.status==='BUYING'){
    el.innerHTML=`<div class="card" style="text-align:center;padding:20px">
      <div style="color:var(--warn);font-size:14px;font-weight:700;animation:pulse 1.5s infinite">⏳ Placing BUY order…</div>
      <div style="color:var(--dim);font-size:11px;margin-top:6px">Waiting for Groww execution confirmation</div>
    </div>`; return;
  }
  if(t.status==='EXITING'){
    el.innerHTML=`<div class="card" style="text-align:center;padding:20px">
      <div style="color:var(--bear);font-size:14px;font-weight:700;animation:pulse 1.5s infinite">⏳ Placing SELL order…</div>
      <div style="color:var(--warn);font-size:11px;margin-top:6px">${t.exit_reason||''}</div>
      <div style="color:var(--dim);font-size:10px;margin-top:4px">Measuring execution time…</div>
    </div>`; return;
  }
  if(t.status==='ACTIVE'){
    if(!_tbEntryEpoch&&t.entry_ts) _tbEntryEpoch=new Date(t.entry_ts).getTime();
    const unr    = t.unrealised||0;
    const unrCls = unr>0?'pnl-pos':unr<0?'pnl-neg':'pnl-zero';
    const perUnit= t.ltp&&t.avg_price ? t.ltp-t.avg_price : 0;
    const perUnitStr=(perUnit>0?'+':'')+fmtN(perUnit,2);
    // Trend arrow
    let trendHtml='';
    if(_tbPrevUnr!==null){
      if(unr>_tbPrevUnr)       trendHtml='<span class="pnl-trend-up">↑</span>';
      else if(unr<_tbPrevUnr)  trendHtml='<span class="pnl-trend-down">↓</span>';
    }
    _tbPrevUnr=unr;
    const pb    = t.paper?'<span class="paper-badge" style="margin-left:8px">PAPER</span>':'';
    const symClr= (t.symbol||'').includes('CE')?'var(--bull)':'var(--bear)';
    const trailHtml=t.trail_active
      ?`<div class="row"><span class="lbl">Trail Exit</span><span class="v vwarn" style="font-weight:700">₹${fmtN(t.trail_exit,2)}</span></div>`
      :`<div class="row"><span class="lbl">Trail</span><span class="v vdim">waiting… activates after +${fmtN(t.trail_start_pts||1,2)}pts profit</span></div>`;

    // Range bar: sl=left, highest=right, entry & ltp as markers
    const sl  = t.hard_sl  ||0;
    const hi  = t.highest  ||t.avg_price||0;
    const avg = t.avg_price||0;
    const ltp = t.ltp      ||avg;
    const rng = Math.max(hi - sl, 1);
    const pad = rng * 0.05;  // 5% padding each side
    const rMin= sl  - pad;
    const rMax= hi  + pad;
    const rTot= rMax - rMin;
    const pct  = v => Math.min(100, Math.max(0, (v-rMin)/rTot*100)).toFixed(1);
    const ltpColor= ltp>=avg?'var(--bull)':'var(--bear)';
    const rangeBar=`
      <div class="pnl-range-wrap">
        <div style="font-size:9px;color:var(--dim);margin-bottom:4px">LTP POSITION (SL ←→ ENTRY ←→ HIGH)</div>
        <div class="pnl-range-track">
          <div class="pnl-range-fill-neg" style="width:${pct(avg)}%"></div>
          <div class="pnl-range-fill-pos" style="left:${pct(avg)}%;width:${Math.max(0,pct(hi)-parseFloat(pct(avg)))}%"></div>
          <div class="pnl-range-sl"   style="left:${pct(sl)}%"  title="Hard SL ₹${fmtN(sl,2)}"></div>
          <div class="pnl-range-entry" style="left:${pct(avg)}%" title="Entry ₹${fmtN(avg,2)}"></div>
          <div class="pnl-range-high" style="left:${pct(hi)}%"  title="Highest ₹${fmtN(hi,2)}"></div>
          <div class="pnl-range-ltp"  style="left:${pct(ltp)}%;background:${ltpColor}" title="LTP ₹${fmtN(ltp,2)}"></div>
        </div>
        <div class="pnl-range-labels">
          <span style="color:var(--bear)">SL ₹${fmtN(sl,2)}</span>
          <span>Entry ₹${fmtN(avg,2)}</span>
          <span style="color:var(--bull)">High ₹${fmtN(hi,2)}</span>
        </div>
      </div>`;

    el.innerHTML=`<div class="active-trade-card" style="border-radius:0;border-left:none;border-right:none;border-top:none">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="font-size:11px;font-weight:700;color:var(--warn);letter-spacing:1px">ACTIVE TRADE${pb}</div>
        <div style="font-size:10px;color:var(--dim)" id="tb-elapsed">—</div>
      </div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;margin-bottom:6px;color:${symClr}">${t.symbol||'—'}</div>

      <!-- Big P&L with trend -->
      <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:2px">
        <div class="trade-big-num ${unrCls}">${unr>0?'+':unr<0?'-':''}₹${fmtN(Math.abs(unr),0)}</div>
        ${trendHtml}
      </div>
      <div class="pnl-per-unit">
        ${t.qty}qty × (LTP ₹${fmtN(ltp,2)} − Entry ₹${fmtN(avg,2)}) = <span style="color:${perUnit>=0?'var(--bull)':'var(--bear)'};font-weight:600">${perUnitStr}/unit</span>
      </div>

      ${rangeBar}

      <div class="row" style="margin-top:6px"><span class="lbl">Live LTP</span><span class="v vinfo" style="font-size:14px;font-weight:700">₹${fmtN(ltp,2)}</span></div>
      <div class="row"><span class="lbl">Entry Price</span><span class="v">₹${fmtN(avg,2)}</span></div>
      <div class="row"><span class="lbl">Highest Seen</span><span class="v vbull">₹${fmtN(hi,2)}</span></div>
      <div class="row">
        <span class="lbl">Hard SL ${t.atr_based&&t.atr_val>0?'<small style="color:var(--info)">(ATR×1.5)</small>':'<small style="color:var(--dim)">(fixed)</small>'}</span>
        <span class="v vbear">₹${fmtN(sl,2)}${t.atr_based&&t.atr_val>0?' <small style="color:var(--dim)">ATR='+fmtN(t.atr_val,2)+'</small>':''}</span>
      </div>
      ${trailHtml}
      <div class="timing-row">
        <div class="timing-item"><div class="timing-val">${msStr(t.buy_exec_ms)}</div><div class="timing-lbl">Buy Exec</div></div>
        <div class="timing-item"><div class="timing-val" id="tb-live-time">—</div><div class="timing-lbl">In Trade</div></div>
        <div class="timing-item"><div class="timing-val">${t.qty}</div><div class="timing-lbl">Qty</div></div>
      </div>
      <button class="exit-btn" id="tb-exit-btn" onclick="tbForceExit()">⛔ FORCE EXIT NOW</button>
    </div>`;
    if(!_tbTradeTimer){
      _tbTradeTimer=setInterval(()=>{
        const sec=Math.round((Date.now()-_tbEntryEpoch)/1000);
        const e=$('tb-live-time'); if(e) e.textContent=sec+'s';
        const el2=$('tb-elapsed'); if(el2) el2.textContent=sec+'s in trade';
      },1000);
    }
    return;
  }
  if(t.status==='DONE'){
    _tbEntryEpoch=0; _tbPrevUnr=null;
    const pnl=t.pnl||0; const ip=pnl>=0;
    const pnlStr=(pnl>0?'+':pnl<0?'-':'')+'₹'+fmtN(Math.abs(pnl),2);
    const pb=t.paper?'<span class="paper-badge" style="margin-left:8px">PAPER</span>':'';
    el.innerHTML=`<div class="done-card ${ip?'done-profit':'done-loss'}">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="font-size:11px;font-weight:700;letter-spacing:1px;color:${ip?'var(--bull)':'var(--bear)'}">TRADE COMPLETE${pb}</div>
        <div style="font-size:11px;color:var(--dim);font-style:italic">${t.exit_reason||''}</div>
      </div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;margin-bottom:6px;color:${(t.symbol||'').includes('CE')?'var(--bull)':'var(--bear)'}">${t.symbol||'—'}</div>
      <div class="trade-big-num ${ip?'pnl-pos':'pnl-neg'}" style="margin-bottom:10px">${pnlStr}</div>
      <div class="row"><span class="lbl">Entry Price</span><span class="v">₹${fmtN(t.avg_price,2)}</span></div>
      <div class="row"><span class="lbl">Exit Price</span><span class="v">₹${fmtN(t.exit_price,2)}</span></div>
      <div class="row"><span class="lbl">Qty</span><span class="v vdim">${t.qty}</span></div>
      ${t.atr_based?`<div class="row"><span class="lbl">ATR used</span><span class="v vinfo">${t.atr_val>0?'₹'+fmtN(t.atr_val,2):'fetch failed (fallback)'}</span></div>`:''}
      <div class="row"><span class="lbl">SL mode</span><span class="v vdim">${t.atr_based?(t.atr_val>0?'ATR×1.5':'Fixed fallback'):'Fixed pts'}</span></div>
      <div class="timing-row">
        <div class="timing-item"><div class="timing-val">${msStr(t.buy_exec_ms)}</div><div class="timing-lbl">Buy Exec</div></div>
        <div class="timing-item"><div class="timing-val">${msStr(t.exit_exec_ms)}</div><div class="timing-lbl">Sell Exec</div></div>
        <div class="timing-item"><div class="timing-val">${msStr(t.total_ms)}</div><div class="timing-lbl">Total Time</div></div>
      </div>
      <button onclick="tbReset()" style="width:100%;margin-top:12px;padding:8px;background:none;border:1px solid var(--bdr);color:var(--dim);border-radius:6px;cursor:pointer;font-size:12px">↺ Start New Trade</button>
    </div>`;
  }
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

load(); startTick();
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────
#  HTTP HANDLER
# ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _json(self, body: dict, code: int = 200):
        data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json')
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers(); self.wfile.write(data)

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

        elif parsed.path == '/api/trade/status':
            with _trade_lock: self._json(dict(_trade_state))

        elif parsed.path == '/api/trade/chain':
            idx    = qs.get("index",["NIFTY"])[0].upper()
            expiry = qs.get("expiry",[""])[0]
            if not expiry:
                self._json({"error":"expiry required"},400); return
            self._json(fetch_option_chain(idx, expiry))

        elif parsed.path == '/api/trade/expiries':
            idx = qs.get("index",["NIFTY"])[0].upper()
            self._json({"expiries": fetch_expiries(idx)})

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

        if path == '/api/trade/buy':
            sym    = body.get("symbol","")
            exch   = body.get("exchange","NSE")
            qty      = int(body.get("qty",0))
            paper    = bool(body.get("paper",False))
            hsl      = float(body.get("hard_sl_pts",6.0))
            ts_pts   = float(body.get("trail_start",1.0))
            tstep    = float(body.get("trail_step",0.75))
            maxs     = int(body.get("max_sec",3600))
            atr_b    = bool(body.get("atr_based",False))
            atr_mult = float(body.get("atr_multiplier",1.0))
            if not sym or qty<=0:
                self._json({"ok":False,"error":"symbol and qty required"},400); return
            self._json(trade_start(sym,exch,qty,paper,hsl,ts_pts,tstep,maxs,atr_b,atr_mult))

        elif path == '/api/trade/exit':
            self._json(trade_force_exit())

        elif path == '/api/trade/reset':
            with _trade_lock:
                if _trade_state["status"] in ("IDLE","DONE"):
                    _trade_state.update({"status":"IDLE","symbol":"","error":"",
                        "exit_reason":"","pnl":0.0,"log":[]})
            self._json({"ok":True})

        else:
            self._json({"error":"not found"},404)

# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
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
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f"\n  ✅  Open in browser →  http://localhost:{PORT}")
    print(f"  ↻   Updates every {REFRESH_SEC}s automatically")
    print(f"\n  Ctrl+C to stop.\n")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n  Stopped.\n")

if __name__ == '__main__':
    main()
