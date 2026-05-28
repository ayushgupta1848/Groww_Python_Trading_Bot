#!/usr/bin/env python3
"""
SIGNAL_MONITOR.py — read-only live dashboard that monitors
PREMIUM_DIRECTION_TRACKER and FIBONACCI_TREND_ANALYZER log files
and shows a compact combined signal summary.

No API calls. No orders. Just reads log files the other bots write.
"""

import os, re, glob, time, sys
from typing import Optional
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLL_MS  = 0.1  # file-change poll interval in seconds (no fixed refresh — updates instantly when logs change)

# ── Logging (tee to file, same pattern as other bots) ────────────────────────

def setup_logger() -> str:
    log_d = os.path.join(BASE_DIR, "logs", "signal_monitor")
    os.makedirs(log_d, exist_ok=True)
    ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(log_d, f"Signal_Monitor_{ts}.log")
    import builtins as _b, re as _re
    _strip = _re.compile(r'\033\[[0-9;]*[mKHFABCDEFGJRSTihlnpu]')
    lf = open(path, "a", buffering=1, encoding="utf-8")
    _real = sys.__stdout__
    _orig = _b.print
    def _tee(*args, sep=' ', end='\n', file=None, flush=False):
        if file is None:
            _orig(*args, sep=sep, end=end, file=_real, flush=True)
            try: lf.write(_strip.sub('', sep.join(str(a) for a in args) + end)); lf.flush()
            except: pass
        else:
            _orig(*args, sep=sep, end=end, file=file, flush=flush)
    _b.print = _tee
    return path

# ── File helpers ──────────────────────────────────────────────────────────────

def latest_log(folder: str, prefix: str) -> Optional[str]:
    files = glob.glob(os.path.join(BASE_DIR, "logs", folder, f"{prefix}_*.log"))
    return max(files, key=os.path.getmtime) if files else None

def tail(path: Optional[str], n: int = 400) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return ""

# ── PREMIUM DIRECTION TRACKER parser ─────────────────────────────────────────

def parse_premium(text: str) -> dict:
    d = {}
    if not text:
        return d

    # Latest tick line
    tick = re.findall(
        r'\[(\d{2}:\d{2}:\d{2})\]\s+SPOT\s+([\d.]+)\s+'
        r'\((\d+)\s+CE\)\s*([↑↓→])\s*(\S+)\s+₹\s*([\d.]+)\s+'
        r'\|\s+\((\d+)\s+PE\)\s*([↑↓→])\s*(\S+)\s+₹\s*([\d.]+)',
        text
    )
    if tick:
        t = tick[-1]
        d.update(tick_time=t[0], spot=float(t[1]), strike=int(t[2]),
                 ce_arrow=t[3], ce_dir=t[4], ce_ltp=float(t[5]),
                 pe_arrow=t[7], pe_dir=t[8], pe_ltp=float(t[9]))

    # Latest FIB MENTOR block
    blocks = list(re.finditer(
        r'FIB MENTOR\s+\[(\d{2}:\d{2}:\d{2})\](.*?)(?=\n[-─]{40,})',
        text, re.DOTALL
    ))
    if blocks:
        b = blocks[-1].group(0)
        for key, pat in [
            ("zone",     r'Zone\s+(.+)'),
            ("trend",    r'Trend\s+(.+)'),
            ("action",   r'ACTION\s+(.+)'),
        ]:
            m = re.search(pat, b)
            if m: d[key] = m.group(1).strip()

        m = re.search(r'CE\s+(\d+)%\s+[█░]+\s+PE\s+(\d+)%', b)
        if m: d["ce_pct"] = int(m.group(1)); d["pe_pct"] = int(m.group(2))

        m = re.search(r'Score.*?CE\s+(\d+)/10', b)
        if m: d["score"] = int(m.group(1))

        m = re.search(r'BREAKOUT\s+→\s+([\d.]+)', b)
        if m: d["breakout"] = float(m.group(1))

        m = re.search(r'SUPPORT\s+↓\s+([\d.]+)', b)
        if m: d["support"] = float(m.group(1))

        m = re.search(r'Target\s+([\d.]+)', b)
        if m: d["target"] = float(m.group(1))

    # Flow direction (last occurrence)
    m = re.search(
        r'→\s+(NEUTRAL|BULL|BEAR)\s+CE\s+(\d+)%\s+current flow.*?CE\s+(\d+)%',
        text
    )
    if m:
        d["flow"] = m.group(1)
        d["flow_ce_pct"] = int(m.group(3))

    return d

# ── FIBONACCI TREND ANALYZER parser ──────────────────────────────────────────

def parse_fibo(text: str) -> dict:
    d = {}
    if not text:
        return d

    # Latest dashboard header
    headers = re.findall(
        r'FIBONACCI ANALYZER\s+\|\s+(\w+)\s+\|\s+([\d-]+ [\d:]+)\s+\|\s+Spot\s+([\d.]+)\s+\|\s+(\S+)',
        text
    )
    if headers:
        h = headers[-1]
        d.update(index=h[0], dt=h[1], spot=float(h[2]), market=h[3])

    # Work on the last dashboard block (after the last ====)
    segs = re.split(r'={40,}', text)
    block = "\n".join(segs[-3:]) if len(segs) >= 3 else text

    m = re.search(r'RSI\s+([\d.]+\s*\[[\w\s…]+\]|[\w…]+)', block)
    if m: d["rsi"] = m.group(1).strip()

    m = re.search(r'Pattern\s+(\w+)', block)
    if m: d["pattern"] = m.group(1).strip()

    # 1-HR line
    m = re.search(r'1-HR\s+→\s*(\w+)\s+→\s+(.+)', block)
    if m:
        d["hr1_dir"]  = m.group(1).strip()
        d["hr1_note"] = m.group(2).strip()

    # 15-min line — either a direction or "building" message
    m = re.search(r'15-[Mm][Ii][Nn]\s+[→↑↓]\s*(\w+)\s+→\s+(.+)', block)
    if m:
        d["m15_dir"]  = m.group(1).strip()
        d["m15_note"] = m.group(2).strip()
    elif re.search(r'15-min data building', block, re.IGNORECASE):
        d["m15_dir"]  = "building…"
        d["m15_note"] = "check back in 1-2 cycles"
    elif re.search(r'15m score', block, re.IGNORECASE):
        m2 = re.search(r'15m score:\s*([+-]?\d+)', block)
        if m2: d["m15_dir"] = f"score {m2.group(1)}"

    # Trade setup line (⬆⬇ are thick arrows used by FIBO; ↑↓ kept for safety)
    m = re.search(r'1-hr\s+[↑↓→⬆⬇]\s+\+\s+15m\s+[↑↓→⬆⬇]\s+→\s+(.+)', block)
    if m: d["trade"] = m.group(1).strip()

    # Summary (after --- SUMMARY ---)
    m = re.search(r'--- SUMMARY ---\s*\n(.*?)(?=─{10,}|={10,}|\Z)', block, re.DOTALL)
    if m:
        lines = [l.strip() for l in m.group(1).strip().splitlines() if l.strip()]
        if lines:
            d["summary"] = " ".join(lines)

    return d

# ── Consensus logic ───────────────────────────────────────────────────────────

def consensus(pdt: dict, fibo: dict):
    ce_pct = pdt.get("ce_pct", 50)
    pdt_sig = "CE" if ce_pct >= 60 else ("PE" if ce_pct <= 40 else "NEUTRAL")

    trade = (fibo.get("trade") or "").upper()
    if "NO TRADE" in trade or "CONFLICT" in trade or "WAIT" in trade:
        fibo_sig = "WAIT"
    elif "STRONG CE" in trade or ("CE" in trade and "PE" not in trade):
        fibo_sig = "CE"
    elif "STRONG PE" in trade or ("PE" in trade and "CE" not in trade):
        fibo_sig = "PE"
    elif "LEAN CE" in trade:
        fibo_sig = "CE (lean)"
    elif "LEAN PE" in trade:
        fibo_sig = "PE (lean)"
    else:
        fibo_sig = "WAIT"

    if not pdt and not fibo:
        return "⏳  No data yet — waiting for bots to start", "WAIT"
    if not pdt:
        return "⏳  PDT not running — cannot determine consensus", "WAIT"
    if not fibo:
        return "⏳  FIBO not running — cannot determine consensus", "WAIT"

    if pdt_sig == "CE" and fibo_sig in ("CE", "CE (lean)"):
        return "✅  STRONG CE — both bots aligned", "CE"
    if pdt_sig == "PE" and fibo_sig in ("PE", "PE (lean)"):
        return "✅  STRONG PE — both bots aligned", "PE"
    if fibo_sig == "WAIT":
        if fibo.get("trade"):
            return f"⚠️   PDT says {pdt_sig} but FIBO says NO TRADE — wait for alignment", "WAIT"
        return f"⚠️   PDT says {pdt_sig} but FIBO needs more data — wait", "WAIT"
    if pdt_sig == "NEUTRAL":
        return f"⚠️   PDT neutral — FIBO says {fibo_sig} — wait for PDT confirmation", "WAIT"
    if pdt_sig != fibo_sig.split()[0]:
        return f"🔴  CONFLICT — PDT:{pdt_sig}  FIBO:{fibo_sig} — do NOT trade", "CONFLICT"
    return f"⏳  {pdt_sig} lean — not strong enough yet", "WAIT"

# ── Dashboard ─────────────────────────────────────────────────────────────────

W = 68

def _row(label: str, value: str) -> str:
    return f"  │  {label:<14} {value}"

def _sep() -> str:
    return f"  │  {'·' * (W - 7)}"

def print_dashboard(pdt: dict, fibo: dict, pdt_file: str, fibo_file: str):
    os.system("clear")
    now = datetime.now().strftime("%H:%M:%S")

    print(f"{'═'*W}")
    print(f"  SIGNAL MONITOR  |  {now}  |  LIVE")
    print(f"  [Ctrl+C to quit]")
    print(f"{'═'*W}")
    print()

    # ── PREMIUM DIRECTION TRACKER panel ──────────────────────────────────────
    fname = os.path.basename(pdt_file) if pdt_file else "no log found"
    print(f"  ┌─ PREMIUM DIRECTION TRACKER  [{fname}]")
    if not pdt:
        print(f"  │  ⏳ Waiting for bot to produce data…")
    else:
        spot_s = f"{pdt.get('spot', '?')}"
        print(_row("Spot / Strike", f"{spot_s}  │  {pdt.get('strike','?')} ATM  │  [{pdt.get('tick_time','?')}]"))
        ce_s = f"{pdt.get('ce_arrow','')} {pdt.get('ce_dir','?'):<8} ₹{pdt.get('ce_ltp','?')}"
        pe_s = f"{pdt.get('pe_arrow','')} {pdt.get('pe_dir','?'):<8} ₹{pdt.get('pe_ltp','?')}"
        print(_row("CE Premium", ce_s))
        print(_row("PE Premium", pe_s))
        print(_sep())
        print(_row("Trend", pdt.get("trend", "—")))
        print(_row("Zone", pdt.get("zone", "—")))
        ce_p = pdt.get("ce_pct"); pe_p = pdt.get("pe_pct")
        score = pdt.get("score")
        if ce_p is not None:
            bar = f"CE {ce_p}%  PE {pe_p}%  │  Score {score}/10" if score else f"CE {ce_p}%  PE {pe_p}%"
            print(_row("Probability", bar))
        flow = pdt.get("flow"); flow_ce = pdt.get("flow_ce_pct")
        if flow:
            print(_row("Flow", f"{flow}  CE {flow_ce}%"))
        print(_sep())
        print(_row("Action", pdt.get("action", "—")))
        if "target" in pdt:
            print(_row("Target / Stop", f"{pdt.get('target','—')}  │  Support {pdt.get('support','—')}  │  Breakout {pdt.get('breakout','—')}"))
    print(f"  └{'─'*(W-4)}")
    print()

    # ── FIBONACCI TREND ANALYZER panel ───────────────────────────────────────
    fname2 = os.path.basename(fibo_file) if fibo_file else "no log found"
    print(f"  ┌─ FIBONACCI TREND ANALYZER  [{fname2}]")
    if not fibo:
        print(f"  │  ⏳ Waiting for bot to produce data…")
    else:
        idx = fibo.get("index","?"); spot = fibo.get("spot","?")
        mkt = fibo.get("market","?"); dt = fibo.get("dt","?")
        print(_row("Index / Spot", f"{idx}  {spot}  │  {mkt}  [{dt}]"))
        print(_row("RSI / Pattern", f"{fibo.get('rsi','—')}  │  {fibo.get('pattern','—')}"))
        print(_sep())
        hr1 = f"{fibo.get('hr1_dir','—'):<10} {fibo.get('hr1_note','')}"
        print(_row("1-HR", hr1))
        m15 = f"{fibo.get('m15_dir','—'):<10} {fibo.get('m15_note','')}"
        print(_row("15-MIN", m15))
        print(_sep())
        print(_row("Trade Setup", fibo.get("trade", "—")))
        summary = fibo.get("summary", "")
        if summary:
            words = summary.split()
            line = ""
            for w in words:
                if len(line) + len(w) + 1 > 50:
                    print(f"  │  {'Summary':<14} {line.strip()}")
                    line = "";
                line += w + " "
            if line.strip():
                print(f"  │  {'':14} {line.strip()}" if any(fibo.get("summary","")) else "")
    print(f"  └{'─'*(W-4)}")
    print()

    # ── COMBINED CONSENSUS ────────────────────────────────────────────────────
    msg, sig = consensus(pdt, fibo)
    pdt_label = ("CE" if pdt.get("ce_pct",50) >= 60 else
                 "PE" if pdt.get("ce_pct",50) <= 40 else
                 "NEUTRAL") if pdt else "?"
    fibo_label = fibo.get("trade", "—") if fibo else "?"
    print(f"  ┌─ COMBINED SIGNAL {'─'*(W-22)}")
    print(_row("PDT signal", pdt_label))
    print(_row("FIBO signal", fibo_label))
    print(_sep())
    print(f"  │  {msg}")
    print(f"  └{'─'*(W-4)}")
    print()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log_path = setup_logger()
    print(f"📝 Log: {log_path}")
    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║   SIGNAL MONITOR  (read-only)                ║")
    print("  ║   Watches PDT + FIBO logs live               ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()
    print("  Scanning for latest log files…")
    time.sleep(1)

    last_mtime = {"pdt": 0.0, "fibo": 0.0}

    while True:
        try:
            pdt_file  = latest_log("premium_tracker", "Premium_Tracker")
            fibo_file = latest_log("fibo_analyzer",   "Fibo_Analyzer")

            pdt_mtime  = os.path.getmtime(pdt_file)  if pdt_file  else 0.0
            fibo_mtime = os.path.getmtime(fibo_file) if fibo_file else 0.0

            if pdt_mtime != last_mtime["pdt"] or fibo_mtime != last_mtime["fibo"]:
                last_mtime["pdt"]  = pdt_mtime
                last_mtime["fibo"] = fibo_mtime

                pdt_data  = parse_premium(tail(pdt_file,  500)) if pdt_file  else {}
                fibo_data = parse_fibo   (tail(fibo_file, 400)) if fibo_file else {}

                print_dashboard(pdt_data, fibo_data, pdt_file, fibo_file)

            time.sleep(POLL_MS)

        except KeyboardInterrupt:
            print("\n  Signal Monitor stopped.")
            break

if __name__ == "__main__":
    main()
