#!/usr/bin/env python3
"""
LIVE_SUMMARY.py
===============
Comprehensive meta-dashboard that reads ALL running bot logs and shows a
single unified live view — no API calls, no orders, purely a log aggregator.

╔══════════════════════════════════════════════════════════════╗
║  START THESE BOTS FIRST (in separate terminals):             ║
║                                                              ║
║  1.  python3 MASTER_SIGNAL_BOT.py          ← primary signal ║
║  2.  python3 FIBONACCI_TREND_ANALYZER.py   ← fib levels     ║
║  3.  python3 CHART_LEVEL_ANALYZER.py       ← S/R + options  ║
║  4.  python3 PREMIUM_DIRECTION_TRACKER.py  ← optional       ║
║                                                              ║
║  Then run:  python3 LIVE_SUMMARY.py                         ║
╚══════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations
import os
import sys
import json
import time
import re as _re
from datetime import datetime
from typing import Optional

# ─────────────────────────────────────────────────────────────
#  ANSI COLORS
# ─────────────────────────────────────────────────────────────
class C:
    RESET   = "\033[0m";  BOLD    = "\033[1m";  DIM     = "\033[2m"
    RED     = "\033[91m"; GREEN   = "\033[92m"; YELLOW  = "\033[93m"
    CYAN    = "\033[96m"; WHITE   = "\033[97m"; MAGENTA = "\033[95m"
    ORANGE  = "\033[38;5;214m"
    B_RED   = "\033[1;91m"; B_GREEN  = "\033[1;92m"; B_YELLOW = "\033[1;93m"
    B_CYAN  = "\033[1;96m"; B_WHITE  = "\033[1;97m"; B_ORANGE = "\033[1;38;5;214m"
    B_MAGENTA = "\033[1;95m"

_ANSI_RE = _re.compile(r'\x1b\[[0-9;]*m')

def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)

def vlen(s: str) -> int:
    return len(_strip(s))

def rpad(s: str, w: int) -> str:
    return s + " " * max(0, w - vlen(s))

BASE        = os.path.dirname(os.path.abspath(__file__))
REFRESH_SEC = 30
STALE_SECS  = 300   # >5 min old → flag as stale


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def _latest_file(subdir: str, prefix: str, ext: str = ".log") -> Optional[str]:
    d = os.path.join(BASE, subdir)
    if not os.path.isdir(d):
        return None
    files = sorted(
        [f for f in os.listdir(d) if f.startswith(prefix) and f.endswith(ext)],
        reverse=True,
    )
    return os.path.join(d, files[0]) if files else None


def _parse_ts(ts_str: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(ts_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _age(ts_str: str) -> str:
    dt = _parse_ts(ts_str)
    if not dt:
        return "?"
    secs = (datetime.now() - dt).total_seconds()
    if secs < 60:   return f"{int(secs)}s ago"
    if secs < 3600: return f"{int(secs // 60)}m ago"
    return f"{int(secs // 3600)}h ago"


def _is_stale(ts_str: str) -> bool:
    dt = _parse_ts(ts_str)
    if not dt:
        return True
    return (datetime.now() - dt).total_seconds() > STALE_SECS


def _bot_status_line(name: str, ts_str: Optional[str], start_cmd: str, W: int = 108) -> str:
    if not ts_str:
        icon = f"{C.B_RED}✗{C.RESET}"
        status = f"{C.RED}NOT RUNNING{C.RESET}"
        hint = f"{C.DIM}→ python3 {start_cmd}{C.RESET}"
    elif _is_stale(ts_str):
        icon = f"{C.B_YELLOW}⚠{C.RESET}"
        status = f"{C.B_YELLOW}STALE{C.RESET}  {C.DIM}({_age(ts_str)}){C.RESET}"
        hint = ""
    else:
        icon = f"{C.B_GREEN}✓{C.RESET}"
        status = f"{C.B_GREEN}LIVE{C.RESET}  {C.DIM}({_age(ts_str)}){C.RESET}"
        hint = ""
    return f"  {icon}  {rpad(C.WHITE + name + C.RESET, 44)}  {status}  {hint}"


# ─────────────────────────────────────────────────────────────
#  MASTER SIGNAL READER
# ─────────────────────────────────────────────────────────────
def read_master() -> Optional[dict]:
    path = _latest_file("logs/master_signal", "Master_Signal_")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
                if d.get("ts"):
                    return d
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
#  FIBONACCI ANALYZER READER
# ─────────────────────────────────────────────────────────────
def read_fibo() -> Optional[dict]:
    path = _latest_file("logs/fibo_analyzer", "Fibo_Analyzer_")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return None

    # Find last cycle header: "FIBONACCI ANALYZER  |  NIFTY  |  2026-06-01 11:33:06  |  Spot 23549"
    headers = list(_re.finditer(
        r'FIBONACCI ANALYZER\s+\|\s+(\w+)\s+\|\s+([\d\-: ]+)\s+\|\s+Spot (\d+)',
        content
    ))
    if not headers:
        return None
    last  = headers[-1]
    seg   = content[last.start():]

    result: dict = {
        "ts":            last.group(2).strip(),
        "index":         last.group(1).strip(),
        "spot":          float(last.group(3)),
        "day_high":      None,
        "day_low":       None,
        "day_dir":       "",
        "fib_levels":    [],
        "confluence":    [],
        "swing_high_15m":None,
        "swing_low_15m": None,
        "zone_1h":       "",
        "trade_setup":   "",
        "summary":       "",
        "ce_trigger":    "",
        "pe_trigger":    "",
    }

    # Day range
    dm = _re.search(r'DAY FIB\s+H\s+([\d.]+)\s+L\s+([\d.]+)\s+\([\d]+ pts\s+(\w+) day\)', seg)
    if dm:
        result["day_high"] = float(dm.group(1))
        result["day_low"]  = float(dm.group(2))
        result["day_dir"]  = dm.group(3)

    # Individual fib levels: "  23596  R38.2%   +47 pts ★"
    for m in _re.finditer(r'^\s+([\d.]+)\s+([\w%.]+)\s+([+-][\d.]+) pts', seg, _re.MULTILINE):
        label = m.group(2).strip()
        result["fib_levels"].append({
            "price":    float(m.group(1)),
            "label":    label,
            "dist_pts": float(m.group(3)),
        })
        if "SWING_HIGH" in label:
            result["swing_high_15m"] = float(m.group(1))
        if "SWING_LOW" in label:
            result["swing_low_15m"]  = float(m.group(1))

    # Confluence: "  ***     23596     +47 pts  [R23.6%, R38.2%, R50.0%]"
    for m in _re.finditer(r'(\*+)\s+([\d.]+)\s+([+\-][\d.]+) pts\s+\[([^\]]+)\]', seg):
        result["confluence"].append({
            "stars":    len(m.group(1)),
            "price":    float(m.group(2)),
            "dist_pts": float(m.group(3)),
            "tags":     m.group(4),
        })

    # 1H zone: "1-HR  → NEUTRAL   →   BOTH SIDES — wait for clarity"
    h1 = _re.search(r'1-HR\s+→\s+(\w+)\s+→\s+(.+)', seg)
    if h1:
        result["zone_1h"] = f"{h1.group(1)} — {h1.group(2).strip()}"

    # CE/PE triggers
    trig = _re.search(r'PE trigger:\s*([^|]+)\s*\|\s*CE trigger:\s*(.+)', seg)
    if trig:
        result["pe_trigger"] = trig.group(1).strip()
        result["ce_trigger"] = trig.group(2).strip()

    # Trade setup (after "─── 🎯 TRADE SETUP ───")
    setup = _re.search(r'TRADE SETUP\s*─+\n(.*?)(?=\n\n|\Z)', seg, _re.DOTALL)
    if setup:
        result["trade_setup"] = " │ ".join(
            ln.strip() for ln in setup.group(1).strip().splitlines() if ln.strip()
        )

    # Summary
    smry = _re.search(r'--- SUMMARY ---\n(.*?)(?=\n\n|\Z)', seg, _re.DOTALL)
    if smry:
        result["summary"] = " │ ".join(
            ln.strip() for ln in smry.group(1).strip().splitlines() if ln.strip()
        )

    return result


# ─────────────────────────────────────────────────────────────
#  CHART LEVEL READER
# ─────────────────────────────────────────────────────────────
def read_chart_signal() -> Optional[dict]:
    """Latest option signal from signals JSONL (written when alarm fires)."""
    today    = datetime.now().strftime("%Y-%m-%d")
    sig_path = os.path.join(BASE, "logs", "chart_level", f"signals_{today}.jsonl")
    if not os.path.exists(sig_path):
        return None
    try:
        with open(sig_path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def read_chart_decision() -> Optional[dict]:
    """Latest TRADE DECISION + OPTION SUGGESTION from the session log."""
    path = _latest_file("logs/chart_level", "Chart_Level_")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return None

    decisions = list(_re.finditer(r'TRADE DECISION\s+\│\s+(.+)', content))
    options   = list(_re.finditer(r'OPTION SUGGESTION\s+\│\s+(.+)', content))
    # also grab the timestamp from the header line
    ts_matches = list(_re.finditer(
        r'CHART LEVEL ANALYZER.*?(\d{2}:\d{2}:\d{2})', content))

    return {
        "ts":              (datetime.now().strftime("%Y-%m-%d ") +
                            ts_matches[-1].group(1)) if ts_matches else "",
        "decision":        decisions[-1].group(1).strip() if decisions else "",
        "option_text":     options[-1].group(1).strip()  if options  else "",
    }


# ─────────────────────────────────────────────────────────────
#  PREMIUM TRACKER READER
# ─────────────────────────────────────────────────────────────
def read_premium() -> Optional[dict]:
    """Latest line from PREMIUM_DIRECTION_TRACKER showing CE/PE premium flow."""
    path = _latest_file("logs/premium_tracker", "Premium_Tracker_")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        # Lines look like: [11:51:09]  SPOT 23561.5  (23550 CE) → STABLE ₹ 123.05  |  (23550 PE) ↑ UP  ₹ 79.70
        for raw in reversed(lines):
            raw = raw.strip()
            m = _re.search(r'\[(\d{2}:\d{2}:\d{2})\]\s+SPOT\s+([\d.]+)\s+(.+)', raw)
            if m:
                return {
                    "ts":   datetime.now().strftime("%Y-%m-%d ") + m.group(1),
                    "spot": float(m.group(2)),
                    "line": m.group(3).strip(),
                }
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
#  CONSENSUS ENGINE
# ─────────────────────────────────────────────────────────────
def build_consensus(
    master: Optional[dict],
    fibo:   Optional[dict],
    csig:   Optional[dict],
) -> dict:
    bull, bear = 0, 0
    sources: list[str] = []

    # ── Master Signal ─────────────────────────────────────────
    if master and not _is_stale(master.get("ts", "")):
        d    = master.get("direction", "WAIT")
        conf = float(master.get("confidence", 0))
        if d == "CE" and conf >= 60:
            w = 3 if conf >= 75 else 2
            bull += w
            sources.append(f"MASTER→CE({conf:.0f}%)")
        elif d == "PE" and conf >= 60:
            w = 3 if conf >= 75 else 2
            bear += w
            sources.append(f"MASTER→PE({conf:.0f}%)")
        else:
            sources.append("MASTER→WAIT")

        pat = master.get("pattern", "")
        if any(k in pat.upper() for k in ("HAMMER", "BULL ENGULF", "MORNING STAR")):
            bull += 1
        elif any(k in pat.upper() for k in ("SHOOTING STAR", "BEAR ENGULF", "EVENING STAR", "STRONG BEAR")):
            bear += 1

        s5m  = int(master.get("s5m", 0))
        sprem= int(master.get("sprem", 0))
        if s5m  > 0: bull += 1
        if s5m  < 0: bear += 1
        if sprem > 0: bull += 1
        if sprem < 0: bear += 1

    # ── Fibo ──────────────────────────────────────────────────
    if fibo and not _is_stale(fibo.get("ts", "")):
        setup = fibo.get("trade_setup", "").upper()
        if "CE" in setup and "NO TRADE" not in setup and "CONFLICT" not in setup:
            bull += 2
            sources.append("FIBO→CE")
        elif "PE" in setup and "NO TRADE" not in setup and "CONFLICT" not in setup:
            bear += 2
            sources.append("FIBO→PE")
        else:
            sources.append("FIBO→WAIT")

    # ── Chart Signal ──────────────────────────────────────────
    if csig and csig.get("direction") in ("CE", "PE") and not _is_stale(csig.get("ts", "")):
        d    = csig["direction"]
        conf = csig.get("confidence", "MEDIUM")
        w    = 3 if conf == "HIGH" else 2
        if d == "CE":
            bull += w
            sources.append(f"CHART→CE({conf})")
        else:
            bear += w
            sources.append(f"CHART→PE({conf})")

    # ── Resolve ───────────────────────────────────────────────
    if bull >= 5 and bull > bear:
        sig, col = "STRONG CE ▲", C.B_GREEN
        summary  = f"Strong bullish consensus  (bull:{bull}  bear:{bear})"
    elif bear >= 5 and bear > bull:
        sig, col = "STRONG PE ▼", C.B_RED
        summary  = f"Strong bearish consensus  (bull:{bull}  bear:{bear})"
    elif bull >= 3 and bull > bear:
        sig, col = "CE ▲", C.GREEN
        summary  = f"Bullish lean — wait for entry trigger  (bull:{bull}  bear:{bear})"
    elif bear >= 3 and bear > bull:
        sig, col = "PE ▼", C.RED
        summary  = f"Bearish lean — wait for entry trigger  (bull:{bull}  bear:{bear})"
    else:
        sig, col = "WAIT ─", C.B_YELLOW
        summary  = f"No directional edge  (bull:{bull}  bear:{bear})"

    return {"signal": sig, "color": col, "summary": summary,
            "bull": bull, "bear": bear, "sources": sources}


# ─────────────────────────────────────────────────────────────
#  RENDER
# ─────────────────────────────────────────────────────────────
def render(
    master:   Optional[dict],
    fibo:     Optional[dict],
    cdec:     Optional[dict],
    csig:     Optional[dict],
    prem:     Optional[dict],
    consensus: dict,
) -> None:
    os.system("clear")
    W = 110
    border = C.B_CYAN + "═" * W + C.RESET
    sep    = C.DIM + "  " + "─" * (W - 4) + C.RESET

    now   = datetime.now()
    idx   = ((master or {}).get("index")
             or (fibo or {}).get("index")
             or "NIFTY")
    spot  = (master or {}).get("spot") or (fibo or {}).get("spot") or 0

    # ── Header ────────────────────────────────────────────────
    print(border)
    print(f"  📊 {C.B_WHITE}LIVE TRADING SUMMARY{C.RESET}  ─  "
          f"{C.B_WHITE}{idx}{C.RESET}  │  "
          f"{now.strftime('%a %d-%b-%Y')}  │  "
          f"{C.DIM}{now.strftime('%H:%M:%S')}{C.RESET}  │  "
          f"Refresh {REFRESH_SEC}s")
    print(border)

    # ── Bot status ────────────────────────────────────────────
    print()
    print(f"  {C.DIM}{'BOT STATUS':─<106}{C.RESET}")
    print(_bot_status_line("MASTER_SIGNAL_BOT.py",
                            (master or {}).get("ts"),
                            "MASTER_SIGNAL_BOT.py"))
    print(_bot_status_line("FIBONACCI_TREND_ANALYZER.py",
                            (fibo or {}).get("ts"),
                            "FIBONACCI_TREND_ANALYZER.py"))
    print(_bot_status_line("CHART_LEVEL_ANALYZER.py",
                            (cdec or {}).get("ts") or (csig or {}).get("ts"),
                            "CHART_LEVEL_ANALYZER.py"))
    print(_bot_status_line("PREMIUM_DIRECTION_TRACKER.py",
                            (prem or {}).get("ts"),
                            "PREMIUM_DIRECTION_TRACKER.py  (optional)"))

    # ── Spot ──────────────────────────────────────────────────
    if spot:
        print()
        vwap_line = ""
        if fibo and fibo.get("day_high") and fibo.get("day_low"):
            dh, dl = fibo["day_high"], fibo["day_low"]
            pct_into = (spot - dl) / (dh - dl) * 100 if dh != dl else 50
            vwap_line = (f"  │  Day range: {C.GREEN}H {dh:,.0f}{C.RESET}  "
                         f"{C.RED}L {dl:,.0f}{C.RESET}  "
                         f"({pct_into:.0f}% into range)")
        print(f"  SPOT  {C.B_WHITE}{spot:>10,.2f}{C.RESET}{vwap_line}")

    print()
    print(sep)

    # ── MASTER SIGNAL ─────────────────────────────────────────
    def _arr(v):
        v = float(v)
        if v > 0: return C.B_GREEN + "▲" * min(3, int(abs(v))) + C.RESET
        if v < 0: return C.B_RED   + "▼" * min(3, int(abs(v))) + C.RESET
        return C.DIM + "─" + C.RESET

    print()
    if master:
        d    = master.get("direction", "WAIT")
        conf = float(master.get("confidence", 0))
        pat  = master.get("pattern", "—")
        zone = master.get("zone", "—")
        stop = master.get("stop", 0)
        tgt  = master.get("target", 0)
        rr   = master.get("rr", 0)
        s1h  = master.get("s1h", 0)
        s15m = master.get("s15m", 0)
        s5m  = master.get("s5m", 0)
        sp   = master.get("sprem", 0)
        rsi1h  = master.get("rsi1h", 0)
        rsi15m = master.get("rsi15m", 0)
        sl15m  = master.get("sl15m", 0)
        sh15m  = master.get("sh15m", 0)

        dcol   = C.B_GREEN if d == "CE" else C.B_RED if d == "PE" else C.B_YELLOW
        rr_col = C.B_GREEN if rr >= 2 else C.B_YELLOW if rr >= 1 else C.RED
        pat_col= C.B_GREEN if any(k in pat.upper() for k in ("HAMMER","BULL","MORNING")) else \
                 C.B_RED   if any(k in pat.upper() for k in ("BEAR","SHOOT","EVENING"))  else C.WHITE

        print(f"  {C.B_ORANGE}▌ MASTER SIGNAL{C.RESET}  "
              f"Direction {dcol}{d:<5}{C.RESET}  "
              f"Confidence {C.WHITE}{conf:.1f}%{C.RESET}  │  "
              f"Pattern {pat_col}{pat}{C.RESET}")
        print(f"    Zone: {C.CYAN}{zone}{C.RESET}  "
              f"│  RSI  1H:{C.WHITE}{rsi1h:.0f}{C.RESET}  15M:{C.WHITE}{rsi15m:.0f}{C.RESET}")
        print(f"    Scores  1H:{_arr(s1h)}  15M:{_arr(s15m)}  "
              f"5M:{_arr(s5m)}  Prem:{_arr(sp)}")
        if stop and tgt:
            pts_tgt = abs(tgt - spot) if spot else 0
            pts_sl  = abs(spot - stop) if spot else 0
            print(f"    Stop: {C.RED}{stop:,.1f}{C.RESET} (-{pts_sl:.0f}pts)  "
                  f"Target: {C.GREEN}{tgt:,.1f}{C.RESET} (+{pts_tgt:.0f}pts)  "
                  f"R:R {rr_col}{rr:.1f}:1{C.RESET}")
        if sl15m or sh15m:
            print(f"    15M Swing  {C.RED}Low {sl15m:,.1f}{C.RESET}  "
                  f"{C.GREEN}High {sh15m:,.1f}{C.RESET}")
    else:
        print(f"  {C.B_ORANGE}▌ MASTER SIGNAL{C.RESET}  "
              f"{C.RED}⚠  Not running — start MASTER_SIGNAL_BOT.py{C.RESET}")

    print()
    print(sep)

    # ── FIBONACCI ──────────────────────────────────────────────
    print()
    if fibo:
        print(f"  {C.B_CYAN}▌ FIBONACCI ANALYZER{C.RESET}  "
              f"Spot {C.WHITE}{fibo['spot']:,.0f}{C.RESET}  "
              f"│  Day: {C.GREEN}H {fibo.get('day_high',0):,.0f}{C.RESET}  "
              f"{C.RED}L {fibo.get('day_low',0):,.0f}{C.RESET}  "
              f"({fibo.get('day_dir','').upper()} day)")

        # Confluence zones (strongest first)
        for cf in sorted(fibo.get("confluence", []), key=lambda x: -x["stars"])[:4]:
            d    = cf["dist_pts"]
            stars= "★" * cf["stars"] + "☆" * max(0, 3 - cf["stars"])
            col  = C.B_GREEN if d > 0 else C.B_RED
            arr  = "▲" if d > 0 else "▼"
            print(f"    {C.B_YELLOW}{stars}{C.RESET}  "
                  f"Confluence {C.WHITE}{cf['price']:,.0f}{C.RESET}  "
                  f"{col}{arr}{abs(d):.0f}pts{C.RESET}  "
                  f"{C.DIM}[{cf['tags']}]{C.RESET}")

        if fibo.get("zone_1h"):
            print(f"    1H: {C.CYAN}{fibo['zone_1h']}{C.RESET}")

        if fibo.get("ce_trigger") or fibo.get("pe_trigger"):
            print(f"    {C.B_GREEN}CE trigger:{C.RESET} {fibo.get('ce_trigger','')}  "
                  f"│  {C.B_RED}PE trigger:{C.RESET} {fibo.get('pe_trigger','')}")

        if fibo.get("trade_setup"):
            setup_col = (C.B_GREEN if "CE" in fibo["trade_setup"].upper() and "NO TRADE" not in fibo["trade_setup"].upper()
                         else C.B_RED if "PE" in fibo["trade_setup"].upper() and "NO TRADE" not in fibo["trade_setup"].upper()
                         else C.DIM)
            setup_text = fibo["trade_setup"][:100]
            print(f"    Setup: {setup_col}{setup_text}{C.RESET}")

        if fibo.get("summary"):
            print(f"    {C.DIM}{fibo['summary'][:100]}{C.RESET}")
    else:
        print(f"  {C.B_CYAN}▌ FIBONACCI ANALYZER{C.RESET}  "
              f"{C.RED}⚠  Not running — start FIBONACCI_TREND_ANALYZER.py{C.RESET}")

    print()
    print(sep)

    # ── CHART LEVEL + OPTION SIGNAL ───────────────────────────
    print()
    has_chart = cdec or csig
    if has_chart:
        print(f"  {C.B_MAGENTA}▌ CHART LEVEL ANALYZER{C.RESET}")
        if cdec and cdec.get("decision"):
            dec_text = cdec["decision"]
            dec_col  = (C.B_RED if "WAIT" in dec_text or "⛔" in dec_text
                        else C.B_YELLOW if "CAUTION" in dec_text or "🟡" in dec_text
                        else C.B_GREEN)
            print(f"    Decision: {dec_col}{dec_text[:90]}{C.RESET}")
        if cdec and cdec.get("option_text"):
            print(f"    Option:   {C.DIM}{cdec['option_text'][:90]}{C.RESET}")
        if csig and csig.get("direction") in ("CE", "PE"):
            d    = csig["direction"]
            dcol = C.B_GREEN if d == "CE" else C.B_RED
            age  = _age(csig.get("ts", ""))
            stale= _is_stale(csig.get("ts", ""))
            age_col = C.B_YELLOW if stale else C.DIM
            print(f"    🔔 Last alarm signal: {dcol}BUY {d}{C.RESET}  "
                  f"~{csig.get('strike','?')} {d}  "
                  f"LTP ₹{csig.get('option_ltp',0):.0f}  "
                  f"Conf {csig.get('confidence','?')}  "
                  f"R:R {csig.get('rr_ratio',0):.1f}:1  "
                  f"Target +{csig.get('target_pts',0):.0f}pts  "
                  f"SL -{csig.get('sl_pts',0):.0f}pts  "
                  f"{age_col}[{age}]{C.RESET}")
    else:
        print(f"  {C.B_MAGENTA}▌ CHART LEVEL ANALYZER{C.RESET}  "
              f"{C.RED}⚠  Not running — start CHART_LEVEL_ANALYZER.py{C.RESET}")

    # ── PREMIUM TRACKER ───────────────────────────────────────
    if prem:
        print()
        print(f"  {C.ORANGE}▌ PREMIUM TRACKER{C.RESET}  "
              f"Spot {C.WHITE}{prem['spot']:,.1f}{C.RESET}  │  "
              f"{C.DIM}{prem['line']}{C.RESET}")

    print()
    print(sep)

    # ── CONSENSUS ──────────────────────────────────────────────
    print()
    sig   = consensus["signal"]
    scol  = consensus["color"]
    smry  = consensus["summary"]
    srcs  = "  │  ".join(consensus["sources"]) if consensus["sources"] else "—"

    # Box
    bw = W - 6
    print(f"  {scol}┌─ CONSENSUS {'─' * (bw - 14)}┐{C.RESET}")
    l1 = f"  Signal: {scol}{sig}{C.RESET}  ─  {smry}"
    print(f"  {scol}│{C.RESET}  {rpad(l1, bw - 2)}  {scol}│{C.RESET}")
    l2 = f"  Sources: {C.DIM}{srcs}{C.RESET}"
    print(f"  {scol}│{C.RESET}  {rpad(l2, bw - 2)}  {scol}│{C.RESET}")

    # Key levels to watch
    watch = []
    if fibo and fibo.get("ce_trigger"):
        watch.append(f"{C.GREEN}CE:{C.RESET} {fibo['ce_trigger']}")
    if fibo and fibo.get("pe_trigger"):
        watch.append(f"{C.RED}PE:{C.RESET} {fibo['pe_trigger']}")
    if master and master.get("sl15m"):
        watch.append(f"{C.RED}Floor:{C.RESET} {master['sl15m']:,.0f}")
    if watch:
        l3 = "  Watch  " + "  │  ".join(watch)
        print(f"  {scol}│{C.RESET}  {rpad(l3, bw - 2)}  {scol}│{C.RESET}")

    print(f"  {scol}└{'─' * bw}┘{C.RESET}")

    print()
    print(f"  {C.DIM}Refresh {REFRESH_SEC}s  │  Ctrl+C to quit  │  "
          f"Read-only — no API calls  │  Logs only{C.RESET}")
    print(border)


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main() -> None:
    print(f"\n{C.B_CYAN}{'═' * 72}{C.RESET}")
    print(f"{C.B_WHITE}  📊 LIVE TRADING SUMMARY  — Starting…{C.RESET}")
    print(f"{C.B_CYAN}{'═' * 72}{C.RESET}\n")

    print(f"  {C.B_WHITE}Start these bots FIRST (each in its own terminal):{C.RESET}\n")
    print(f"  {C.B_GREEN}[Required]{C.RESET}  python3 MASTER_SIGNAL_BOT.py")
    print(f"  {C.B_GREEN}[Required]{C.RESET}  python3 FIBONACCI_TREND_ANALYZER.py")
    print(f"  {C.B_YELLOW}[Optional]{C.RESET}  python3 CHART_LEVEL_ANALYZER.py    "
          f"{C.DIM}← adds S/R levels + option alarm{C.RESET}")
    print(f"  {C.B_YELLOW}[Optional]{C.RESET}  python3 PREMIUM_DIRECTION_TRACKER.py  "
          f"{C.DIM}← adds live CE/PE premium flow{C.RESET}")
    print(f"\n  {C.DIM}This bot reads only from log files — no API calls needed.{C.RESET}")
    print(f"\n  Starting dashboard in 3s…\n")
    time.sleep(3)

    while True:
        try:
            master = read_master()
            fibo   = read_fibo()
            cdec   = read_chart_decision()
            csig   = read_chart_signal()
            prem   = read_premium()
            cons   = build_consensus(master, fibo, csig)
            render(master, fibo, cdec, csig, prem, cons)
        except KeyboardInterrupt:
            print(f"\n{C.B_YELLOW}📊 Live Summary stopped.{C.RESET}\n")
            break
        except Exception as exc:
            import traceback
            print(f"{C.RED}⚠  Error: {exc}{C.RESET}")
            traceback.print_exc()

        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    main()
