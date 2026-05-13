#!/usr/bin/env python3
"""
ANALYZE_BOT.py
==============
Post-session performance analyzer for the three trading bots.
Reads logs from:
  logs/groww_bot/      – PROD10FEB (actual trades)
  logs/fibo_analyzer/  – Fibonacci Trend Analyzer (direction signals)
  logs/premium_tracker/ – Premium Direction Tracker (premium flow)

Run any time to get a full trading performance report.
"""

from __future__ import annotations
import os, re, json, sys
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

try:
    import openai as _openai_lib
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

# ── Paths ────────────────────────────────────────────────────────────────────
BASE      = os.path.dirname(os.path.abspath(__file__))
LOG_ROOT  = os.path.join(BASE, "logs")
GROWW_DIR = os.path.join(LOG_ROOT, "groww_bot")
FIBO_DIR  = os.path.join(LOG_ROOT, "fibo_analyzer")
PREM_DIR  = os.path.join(LOG_ROOT, "premium_tracker")
OUT_DIR   = os.path.join(LOG_ROOT, "analysis")
AI_CFG    = os.path.join(BASE, "ai_config.json")
os.makedirs(OUT_DIR, exist_ok=True)

# ── AI Config helpers ────────────────────────────────────────────────────────
def _load_ai_cfg() -> dict:
    if os.path.exists(AI_CFG):
        try:
            with open(AI_CFG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_ai_cfg(cfg: dict):
    with open(AI_CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def _get_openai_key() -> str | None:
    """Return API key from env → ai_config.json → prompt user."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    cfg = _load_ai_cfg()
    if cfg.get("openai_api_key"):
        return cfg["openai_api_key"].strip()
    return None

def _prompt_and_save_key() -> str | None:
    """Ask user for API key, validate format, save to ai_config.json."""
    print(f"\n  {C.YEL}OpenAI API key not configured.{C.RST}")
    print(f"  Get yours at: https://platform.openai.com/api-keys")
    print(f"  {C.DIM}(Key will be saved to ai_config.json in this folder){C.RST}\n")
    try:
        key = input(f"  Paste your OpenAI API key (sk-...): ").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    if not key.startswith("sk-") or len(key) < 20:
        print(f"  {C.RED}Invalid key format. Skipping ChatGPT analysis.{C.RST}")
        return None
    cfg = _load_ai_cfg()
    cfg["openai_api_key"] = key
    cfg.setdefault("model", "gpt-4o")
    cfg.setdefault("enabled", True)
    _save_ai_cfg(cfg)
    print(f"  {C.GRN}✅ Key saved to ai_config.json{C.RST}")
    return key

# ── ANSI Colors ───────────────────────────────────────────────────────────────
class C:
    GRN  = "\033[92m"; RED  = "\033[91m"; YEL  = "\033[93m"
    CYN  = "\033[96m"; BLD  = "\033[1m";  DIM  = "\033[2m"
    WHT  = "\033[97m"; MAG  = "\033[95m"; RST  = "\033[0m"

W = 72   # display width

# ─────────────────────────────────────────────────────────────────────────────
#  SYMBOL PARSER
# ─────────────────────────────────────────────────────────────────────────────
def parse_symbol(sym: str) -> dict:
    """NIFTY2651223600PE → {index, strike, opt_type}"""
    if not sym:
        return {"index": "UNK", "strike": None, "opt_type": None}
    for idx in ("BANKNIFTY", "FINNIFTY", "SENSEX", "NIFTY"):
        if sym.startswith(idx):
            m = re.search(r"(\d{4,6})(CE|PE)$", sym)
            if m:
                return {"index": idx, "strike": int(m.group(1)), "opt_type": m.group(2)}
            return {"index": idx, "strike": None, "opt_type": None}
    return {"index": "UNK", "strike": None, "opt_type": None}

# ─────────────────────────────────────────────────────────────────────────────
#  GROWW BOT LOG PARSER
# ─────────────────────────────────────────────────────────────────────────────
def parse_groww_logs(date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    trades: list[dict] = []
    log_files = sorted(Path(GROWW_DIR).glob("Groww_Bot_*.log"))
    for lf in log_files:
        m = re.search(r"Groww_Bot_(\d{4}-\d{2}-\d{2})_", lf.name)
        if not m:
            continue
        sess_date = m.group(1)
        if date_from and sess_date < date_from:
            continue
        if date_to   and sess_date > date_to:
            continue
        trades.extend(_parse_groww_file(lf, sess_date))
    return trades


def _parse_groww_file(path: Path, sess_date: str) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    # ── detect mode / index from header ──────────────────────────────────
    mode  = "UNKNOWN"
    index = "NIFTY"
    for ln in lines[:40]:
        if "PAPER TRADING MODE" in ln:
            mode = "PAPER"
        elif "LIVE TRADING MODE" in ln:
            mode = "LIVE"
        m = re.search(r"Index:\s*(BANKNIFTY|FINNIFTY|SENSEX|NIFTY)", ln)
        if m:
            index = m.group(1)

    # ── try new [TRADE_RECORD] format first ───────────────────────────────
    records = [ln for ln in lines if ln.startswith("[TRADE_RECORD]")]
    if records:
        result = []
        for rl in records:
            try:
                data = json.loads(rl[len("[TRADE_RECORD]"):].strip())
                sym  = data.get("symbol", "")
                p    = parse_symbol(sym)
                ts   = data.get("ts", "")
                dt   = None
                try:
                    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    pass
                result.append({
                    "date":        ts[:10] if ts else sess_date,
                    "time":        ts[11:19] if len(ts) > 10 else "",
                    "dt":          dt,
                    "symbol":      sym,
                    "index":       p["index"],
                    "strike":      p["strike"],
                    "opt_type":    p["opt_type"],
                    "buy_px":      data.get("buy_px"),
                    "sell_px":     data.get("sell_px"),
                    "qty":         data.get("qty"),
                    "pnl":         data.get("pnl"),
                    "mode":        data.get("mode", mode),
                    "exit_reason": data.get("exit_reason", "UNKNOWN"),
                    "hold_secs":   data.get("hold_secs"),
                    "session":     path.name,
                    "fibo_signal": None,
                    "fibo_aligned": None,
                })
            except Exception:
                pass
        if result:
            return result

    # ── fall back to regex parse (legacy logs) ────────────────────────────
    return _parse_groww_legacy(lines, sess_date, mode, index, path.name)


def _parse_groww_legacy(lines, sess_date, mode, index, session_name) -> list[dict]:
    trades = []
    ctx: dict = {}          # current trade context
    exit_reason = "UNKNOWN"

    def _reset():
        nonlocal exit_reason
        ctx.clear()
        ctx.update({"mode": mode, "index": index, "session": session_name, "date": sess_date})
        exit_reason = "UNKNOWN"

    _reset()

    for ln in lines:
        # ── entry command time ────────────────────────────────────────────
        m = re.search(r"⏱️\s+Command entered at:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
        if not m:
            m = re.search(r"\[(\d{2}:\d{2}:\d{2})\.\d+\]\s+⏱️\s+Command entered:", ln)
        if m:
            _reset()
            raw = m.group(1)
            try:
                if len(raw) == 8:
                    ctx["dt"]   = datetime.strptime(f"{sess_date} {raw}", "%Y-%m-%d %H:%M:%S")
                    ctx["time"] = raw
                else:
                    ctx["dt"]   = datetime.fromisoformat(raw)
                    ctx["time"] = ctx["dt"].strftime("%H:%M:%S")
            except Exception:
                pass

        # ── symbol ────────────────────────────────────────────────────────
        m = re.search(r"🔍 Parsing symbol:\s*(\w+)", ln)
        if m:
            sym = m.group(1)
            ctx["symbol"] = sym
            ctx.update(parse_symbol(sym))

        # ── BUY executed price ────────────────────────────────────────────
        m = re.search(r"Executed avg price:\s*₹([\d.]+),\s*Qty:\s*(\d+)", ln)
        if m:
            ctx["buy_px"] = float(m.group(1))
            ctx["qty"]    = int(m.group(2))

        # ── entry price fallback ──────────────────────────────────────────
        m = re.search(r"💵 Entry price:\s*₹([\d.]+)", ln)
        if m and "buy_px" not in ctx:
            ctx["buy_px"] = float(m.group(1))

        # ── exit reason ───────────────────────────────────────────────────
        if "🔻 Trailing HIT"   in ln: exit_reason = "TRAILING_SL"
        elif "🛑 DYNAMIC SL"   in ln: exit_reason = "DYNAMIC_SL"
        elif "🎯 TARGET HIT"   in ln: exit_reason = "TARGET"
        elif "🛑 SL HIT"       in ln: exit_reason = "SL_HIT"

        # ── hold duration (manual mode) ───────────────────────────────────
        m = re.search(r"Complete trade:\s*([\d.]+)s", ln)
        if m:
            ctx["hold_secs"] = float(m.group(1))

        # ── P&L completion ────────────────────────────────────────────────
        m_pr = re.search(r"💰 PROFIT: ₹(-?[\d.]+) \(Buy @ ₹([\d.]+), Sell @ ₹([\d.]+)\)", ln)
        m_ls = re.search(r"💸 LOSS: ₹(-?[\d.]+) \(Buy @ ₹([\d.]+), Sell @ ₹([\d.]+)\)",   ln)

        if m_pr or m_ls:
            mm     = m_pr or m_ls
            pnl    = float(mm.group(1))
            buy_px = float(mm.group(2))
            sel_px = float(mm.group(3))
            if m_ls:
                pnl = -abs(pnl)

            # exit time from bracket prefix
            exit_dt = None
            mt = re.search(r"\[(\d{2}:\d{2}:\d{2})", ln)
            if mt:
                try:
                    exit_dt = datetime.strptime(f"{sess_date} {mt.group(1)}", "%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            hold = ctx.get("hold_secs")
            if hold is None and exit_dt and ctx.get("dt"):
                hold = (exit_dt - ctx["dt"]).total_seconds()

            if exit_reason == "UNKNOWN":
                exit_reason = "TRAILING_SL" if pnl >= 0 else "SL_HIT"

            trades.append({
                "date":        ctx.get("date", sess_date),
                "time":        ctx.get("time", ""),
                "dt":          ctx.get("dt"),
                "symbol":      ctx.get("symbol", ""),
                "index":       ctx.get("index", index),
                "strike":      ctx.get("strike"),
                "opt_type":    ctx.get("opt_type"),
                "buy_px":      buy_px,
                "sell_px":     sel_px,
                "qty":         ctx.get("qty"),
                "pnl":         pnl,
                "mode":        ctx.get("mode", mode),
                "exit_reason": exit_reason,
                "hold_secs":   hold,
                "session":     session_name,
                "fibo_signal":  None,
                "fibo_aligned": None,
            })
            _reset()

    return trades

# ─────────────────────────────────────────────────────────────────────────────
#  FIBO ANALYZER LOG PARSER
# ─────────────────────────────────────────────────────────────────────────────
def parse_fibo_logs(date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    cycles: list[dict] = []
    for lf in sorted(Path(FIBO_DIR).glob("Fibo_Analyzer_*.log")):
        m = re.search(r"Fibo_Analyzer_(\d{4}-\d{2}-\d{2})_", lf.name)
        if not m:
            continue
        sess_date = m.group(1)
        if date_from and sess_date < date_from:
            continue
        if date_to   and sess_date > date_to:
            continue
        try:
            text = lf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for cm in re.finditer(r"🔄 Analysis cycle #(\d+)\s+\[(\d{2}:\d{2}:\d{2})\]", text):
            try:
                dt = datetime.strptime(f"{sess_date} {cm.group(2)}", "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            start = cm.end()
            nxt   = re.search(r"🔄 Analysis cycle #", text[start:])
            block = text[start: start + nxt.start() if nxt else len(text)]

            cyc: dict = {"dt": dt, "date": sess_date, "time": cm.group(2), "session": lf.name}

            # strip ANSI for cleaner parsing of text fields
            clean_block = re.sub(r"\033\[[0-9;]*m", "", block)

            # spot (two formats)
            ms = re.search(r"Spot\s+([\d.]+)", clean_block)
            if not ms:
                ms = re.search(r"Spot Price\s*:\s*([\d.]+)", clean_block)
            if ms:
                cyc["spot"] = float(ms.group(1))

            # trend bias
            mb = re.search(r"Trend(?:\s+Bias)?\s*:\s*[⬆⬇→]?\s*(BULLISH|BEARISH|NEUTRAL)", clean_block, re.I)
            if mb:
                cyc["trend_bias"] = mb.group(1).upper()

            # candle counts (15m and 1hr)
            m15 = re.search(r"15m(?:\s+data)?\s*:.*?(\d+)c", clean_block, re.I)
            m1h = re.search(r"1.?hr?\s+(?:data\s*)?:.*?(\d+)c", clean_block, re.I)
            if m15:
                cyc["candles_15m"] = int(m15.group(1))
            if m1h:
                cyc["candles_1h"]  = int(m1h.group(1))

            # pattern
            mp = re.search(r"Pattern\s*:\s*([^\n]+)", clean_block)
            if mp:
                pat = mp.group(1).strip()
                # remove emoji and extra whitespace
                pat = re.sub(r"[^\x00-\x7F]+", "", pat).strip()
                cyc["pattern"] = pat if pat else None

            # why no signal — extract the specific reason line
            no_sig_reason = None
            if re.search(r"15.min data not yet available", clean_block, re.I):
                no_sig_reason = "15m data still building (< ~8 candles needed)"
            elif re.search(r"1.hr data not yet available", clean_block, re.I):
                no_sig_reason = "1hr data not yet available"
            elif re.search(r"data too thin", clean_block, re.I):
                no_sig_reason = "Insufficient candle data"
            elif re.search(r"NEUTRAL|wait for clarity|BOTH SIDES", clean_block, re.I):
                no_sig_reason = "Market neutral — no clear direction"
            elif re.search(r"No candle data", clean_block, re.I):
                no_sig_reason = "No candle data (market closed / API)"
            cyc["no_signal_reason"] = no_sig_reason

            # recommended direction
            md = re.search(r"→\s+(CE|PE)\s*\(([^)]+)\)", clean_block)
            if md:
                cyc["signal"]   = md.group(1)
                cyc["quality"]  = md.group(2).strip()
            else:
                md = re.search(r"(CE|PE)\s+\((?:good|weak|strong|fair)\s+setup\)", clean_block, re.I)
                if md:
                    cyc["signal"]  = md.group(1).upper()
                    cyc["quality"] = "ok"
                else:
                    cyc["signal"] = None

            # R:R
            mr = re.search(r"R:R\s*([\d.]+):1", clean_block)
            cyc["rr"] = float(mr.group(1)) if mr else None

            # 15m + 1h direction
            mt = re.search(r"1-hr\s*(\w+)\s*\+\s*15m\s*(\w+)\s*=", clean_block, re.I)
            if mt:
                cyc["dir_1h"]  = mt.group(1).upper()
                cyc["dir_15m"] = mt.group(2).upper()

            cycles.append(cyc)

    return cycles

# ─────────────────────────────────────────────────────────────────────────────
#  PREMIUM TRACKER LOG PARSER
# ─────────────────────────────────────────────────────────────────────────────
def parse_premium_logs() -> list[dict]:
    ticks: list[dict] = []
    for lf in sorted(Path(PREM_DIR).glob("Premium_Tracker_*.log")):
        m = re.search(r"Premium_Tracker_(\d{4}-\d{2}-\d{2})_", lf.name)
        if not m:
            continue
        sess_date = m.group(1)
        try:
            lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for ln in lines:
            # [09:32:15]  SPOT 23567.5  (23700 CE) UP  122.25  |  (23700 PE) DOWN  88.00
            mt = re.search(r"\[(\d{2}:\d{2}:\d{2})\].*SPOT ([\d.]+).*\((\d+) CE\) (UP|DOWN|STABLE).*\((\d+) PE\) (UP|DOWN|STABLE)", ln)
            if mt:
                try:
                    dt = datetime.strptime(f"{sess_date} {mt.group(1)}", "%Y-%m-%d %H:%M:%S")
                    ticks.append({
                        "dt":       dt,
                        "spot":     float(mt.group(2)),
                        "strike":   int(mt.group(3)),
                        "ce_dir":   mt.group(4),
                        "pe_dir":   mt.group(6),
                    })
                except Exception:
                    pass
    return ticks

# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL CORRELATION
# ─────────────────────────────────────────────────────────────────────────────
def correlate_signals(trades: list[dict], fibo_cycles: list[dict]) -> list[dict]:
    for trade in trades:
        if not trade.get("dt"):
            continue
        tdt  = trade["dt"]
        ttyp = trade.get("opt_type")
        # look for fibo signal in 30-min window before the trade
        cands = [c for c in fibo_cycles
                 if c.get("dt") and timedelta(0) <= (tdt - c["dt"]) <= timedelta(minutes=30)]
        if not cands:
            cands = [c for c in fibo_cycles
                     if c.get("dt") and abs((tdt - c["dt"]).total_seconds()) <= 900]
        if cands:
            nearest = min(cands, key=lambda c: abs((tdt - c["dt"]).total_seconds()))
            trade["fibo_signal"]        = nearest.get("signal")
            trade["fibo_quality"]       = nearest.get("quality")
            trade["fibo_rr"]            = nearest.get("rr")
            trade["fibo_session"]       = nearest.get("session", "")
            trade["fibo_cycle_dt"]      = nearest.get("dt")
            trade["fibo_trend_bias"]    = nearest.get("trend_bias")
            trade["fibo_pattern"]       = nearest.get("pattern")
            trade["fibo_no_sig_reason"] = nearest.get("no_signal_reason")
            trade["fibo_candles_15m"]   = nearest.get("candles_15m")
            trade["fibo_candles_1h"]    = nearest.get("candles_1h")
            trade["fibo_spot"]          = nearest.get("spot")
            if ttyp and nearest.get("signal"):
                trade["fibo_aligned"] = (ttyp == nearest["signal"])
    return trades

# ─────────────────────────────────────────────────────────────────────────────
#  ANALYSIS HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def stats(pnls: list[float]) -> dict:
    if not pnls:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0, "total": 0,
                "avg": 0, "avg_win": 0, "avg_loss": 0, "best": 0, "worst": 0,
                "profit_factor": 0, "expectancy": 0}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf     = abs(sum(wins) / sum(losses)) if losses else float("inf")
    return {
        "n":             len(pnls),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      len(wins) / len(pnls) * 100,
        "total":         sum(pnls),
        "avg":           sum(pnls) / len(pnls),
        "avg_win":       sum(wins)   / len(wins)   if wins   else 0,
        "avg_loss":      sum(losses) / len(losses) if losses else 0,
        "best":          max(pnls),
        "worst":         min(pnls),
        "profit_factor": pf,
        "expectancy":    sum(pnls) / len(pnls),
    }

def pnl_color(v: float) -> str:
    return C.GRN if v > 0 else (C.RED if v < 0 else C.YEL)

def fmt_pnl(v: float) -> str:
    return f"{pnl_color(v)}₹{v:+,.2f}{C.RST}"

def bar(value: float, max_abs: float, width: int = 20, win: bool = True) -> str:
    if max_abs == 0:
        return " " * width
    frac = min(abs(value) / max_abs, 1.0)
    filled = int(frac * width)
    ch = "█" if win else "▓"
    col = C.GRN if value >= 0 else C.RED
    return f"{col}{ch * filled}{C.DIM}{'░' * (width - filled)}{C.RST}"

def sparkline(values: list[float]) -> str:
    if not values:
        return ""
    chars = " ▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    rng = mx - mn or 1
    result = []
    for v in values:
        idx = int((v - mn) / rng * (len(chars) - 1))
        col = C.GRN if v >= 0 else C.RED
        result.append(f"{col}{chars[idx]}{C.RST}")
    return "".join(result)

# ─────────────────────────────────────────────────────────────────────────────
#  DISPLAY SECTIONS
# ─────────────────────────────────────────────────────────────────────────────
def rule(title="", ch="═") -> str:
    if title:
        pad = W - len(title) - 4
        return f"{C.CYN}{ch*2} {C.BLD}{title}{C.RST}{C.CYN} {ch * pad}{C.RST}"
    return f"{C.CYN}{ch * W}{C.RST}"

def section(title: str):
    print(f"\n{rule(title)}")

# ── 1. BANNER ────────────────────────────────────────────────────────────────
def print_banner():
    print(f"\n{C.CYN}{'═'*W}{C.RST}")
    print(f"{C.BLD}{C.WHT}{'TRADING PERFORMANCE ANALYZER':^{W}}{C.RST}")
    print(f"{C.DIM}{'Powered by ANALYZE_BOT  |  All three bots':^{W}}{C.RST}")
    print(f"{C.CYN}{'═'*W}{C.RST}")

# ── 2. OVERVIEW ──────────────────────────────────────────────────────────────
def print_overview(trades: list[dict]):
    section("📊  OVERVIEW")
    pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
    if not pnls:
        print("  No completed trades found in logs.")
        return
    s = stats(pnls)
    paper_pnls = [t["pnl"] for t in trades if t.get("pnl") is not None and t.get("mode") == "PAPER"]
    live_pnls  = [t["pnl"] for t in trades if t.get("pnl") is not None and t.get("mode") == "LIVE"]

    dates   = sorted({t["date"] for t in trades if t.get("date")})
    date_rng = f"{dates[0]}  →  {dates[-1]}" if dates else "—"

    print(f"  Period     : {C.CYN}{date_rng}{C.RST}")
    print(f"  Total Trades: {C.BLD}{s['n']}{C.RST}  |  "
          f"Wins: {C.GRN}{s['wins']}{C.RST}  Losses: {C.RED}{s['losses']}{C.RST}  "
          f"Win Rate: {C.BLD}{s['win_rate']:.1f}%{C.RST}")
    print(f"  Total P&L  : {fmt_pnl(s['total'])}  |  "
          f"Expectancy: {fmt_pnl(s['expectancy'])} per trade")
    print(f"  Avg Win    : {fmt_pnl(s['avg_win'])}  |  "
          f"Avg Loss: {fmt_pnl(s['avg_loss'])}")
    print(f"  Best Trade : {fmt_pnl(s['best'])}  |  "
          f"Worst: {fmt_pnl(s['worst'])}")
    pf_str = f"{s['profit_factor']:.2f}" if s['profit_factor'] != float("inf") else "∞"
    print(f"  Profit Factor: {C.BLD}{pf_str}{C.RST}  "
          f"{C.DIM}(>1.5 = good trading edge){C.RST}")
    if paper_pnls:
        sp = stats(paper_pnls)
        print(f"\n  {C.YEL}[PAPER]{C.RST}  {sp['n']} trades | Win {sp['win_rate']:.1f}% | P&L {fmt_pnl(sp['total'])}")
    if live_pnls:
        sl = stats(live_pnls)
        print(f"  {C.GRN}[LIVE] {C.RST}  {sl['n']} trades | Win {sl['win_rate']:.1f}% | P&L {fmt_pnl(sl['total'])}")

# ── 3. P&L CURVE ─────────────────────────────────────────────────────────────
def print_pnl_curve(trades: list[dict]):
    section("📈  CUMULATIVE P&L CURVE")
    valid = sorted([t for t in trades if t.get("pnl") is not None and t.get("dt")],
                   key=lambda t: t["dt"])
    if not valid:
        return
    cum  = []
    acc  = 0.0
    for t in valid:
        acc += t["pnl"]
        cum.append(acc)

    # Print sparkline
    chunk = max(1, len(cum) // 60)
    buckets = [cum[i] for i in range(0, len(cum), chunk)]
    print(f"  {sparkline(buckets)}")
    print(f"  {C.DIM}← start  {len(cum)} trades  end →{C.RST}   "
          f"Final: {fmt_pnl(cum[-1])}")

    # Recent 10 trades table
    print(f"\n  {C.BLD}Last 10 trades:{C.RST}")
    for t in valid[-10:]:
        dt_s  = t["dt"].strftime("%d-%b %H:%M") if t.get("dt") else "—"
        sym   = (t.get("symbol") or "")[-14:]
        xr    = t.get("exit_reason", "—")[:12]
        pnl   = t["pnl"]
        col   = pnl_color(pnl)
        print(f"  {C.DIM}{dt_s}{C.RST}  {sym:<14}  {xr:<12}  {col}₹{pnl:>+9,.2f}{C.RST}")

# ── 4. DAILY P&L ─────────────────────────────────────────────────────────────
def print_daily_pnl(trades: list[dict]):
    section("📅  DAILY P&L BREAKDOWN")
    daily: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.get("pnl") is not None and t.get("date"):
            daily[t["date"]].append(t["pnl"])

    if not daily:
        return

    max_abs = max(abs(sum(v)) for v in daily.values()) or 1
    for day in sorted(daily):
        pnls  = daily[day]
        total = sum(pnls)
        wins  = sum(1 for p in pnls if p > 0)
        col   = C.GRN if total >= 0 else C.RED
        b     = bar(total, max_abs, width=18, win=total >= 0)
        print(f"  {C.DIM}{day}{C.RST}  {b}  "
              f"{col}₹{total:>+9,.2f}{C.RST}  "
              f"{len(pnls)} trades  {wins}W/{len(pnls)-wins}L")

# ── 5. TIME-OF-DAY HEATMAP ───────────────────────────────────────────────────
def print_time_heatmap(trades: list[dict]):
    section("⏰  TIME-OF-DAY PERFORMANCE  (30-min slots)")
    slots: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if not t.get("pnl") or not t.get("time"):
            continue
        try:
            h, mn, _ = t["time"].split(":")
            slot = f"{h}:{('00' if int(mn) < 30 else '30')}"
            slots[slot].append(t["pnl"])
        except Exception:
            pass

    if not slots:
        return

    max_abs = max(abs(sum(v)) for v in slots.values()) or 1
    for slot in sorted(slots):
        pnls  = slots[slot]
        total = sum(pnls)
        wins  = sum(1 for p in pnls if p > 0)
        wr    = wins / len(pnls) * 100
        b     = bar(total, max_abs, width=16, win=total >= 0)
        col   = pnl_color(total)
        print(f"  {C.BLD}{slot}{C.RST}  {b}  "
              f"{col}₹{total:>+9,.2f}{C.RST}  "
              f"{len(pnls):>3} trades  {wr:>5.1f}% win")

# ── 6. DIRECTION ANALYSIS ────────────────────────────────────────────────────
def print_direction_analysis(trades: list[dict]):
    section("🎯  CE vs PE DIRECTION ANALYSIS")
    groups: dict[str, list[float]] = {"CE": [], "PE": [], "UNKNOWN": []}
    for t in trades:
        if t.get("pnl") is None:
            continue
        k = t.get("opt_type") or "UNKNOWN"
        groups[k].append(t["pnl"])

    for otype in ("CE", "PE"):
        pnls = groups[otype]
        if not pnls:
            continue
        s  = stats(pnls)
        col = pnl_color(s["total"])
        print(f"\n  {C.BLD}{otype}{C.RST}  {s['n']} trades | "
              f"Win {s['win_rate']:.1f}% | Total {fmt_pnl(s['total'])}")
        print(f"       Avg Win {fmt_pnl(s['avg_win'])}  "
              f"Avg Loss {fmt_pnl(s['avg_loss'])}  "
              f"PF {s['profit_factor']:.2f}")
        # breakdown by time period
        morning   = [t["pnl"] for t in trades if t.get("opt_type") == otype
                     and t.get("time") and "09" <= t["time"][:2] <= "10"]
        midday    = [t["pnl"] for t in trades if t.get("opt_type") == otype
                     and t.get("time") and "11" <= t["time"][:2] <= "13"]
        afternoon = [t["pnl"] for t in trades if t.get("opt_type") == otype
                     and t.get("time") and "14" <= t["time"][:2] <= "15"]
        for label, subset in [("9-10 am", morning), ("11am-1pm", midday), ("2-3:30pm", afternoon)]:
            if subset:
                sm = stats(subset)
                print(f"       {C.DIM}{label:<10}{C.RST} {sm['n']:>2} trades | "
                      f"{sm['win_rate']:.0f}% win | {fmt_pnl(sum(subset))}")

# ── 7. EXIT REASON BREAKDOWN ─────────────────────────────────────────────────
def print_exit_analysis(trades: list[dict]):
    section("🚪  EXIT REASON BREAKDOWN")
    reasons: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.get("pnl") is not None:
            reasons[t.get("exit_reason", "UNKNOWN")].append(t["pnl"])

    max_abs = max(abs(sum(v)) for v in reasons.values()) if reasons else 1
    for reason, pnls in sorted(reasons.items(), key=lambda x: -sum(x[1])):
        s = stats(pnls)
        b = bar(s["total"], max_abs, width=14, win=s["total"] >= 0)
        print(f"  {C.BLD}{reason:<14}{C.RST}  {b}  "
              f"{s['n']:>3} trades | {s['win_rate']:>5.1f}% win | "
              f"Avg {fmt_pnl(s['avg'])}  Total {fmt_pnl(s['total'])}")

# ── 8. FIBO SIGNAL ALIGNMENT ─────────────────────────────────────────────────
def _fibo_trade_row(t: dict, show_mistake: bool = False) -> str:
    """Format a single trade row for the Fibo alignment table."""
    time_s   = t.get("time", "—")[:8]
    sym      = (t.get("symbol") or "")[-16:]
    traded   = t.get("opt_type") or "?"
    signal   = t.get("fibo_signal") or "—"
    quality  = (t.get("fibo_quality") or "—")[:12]
    rr       = f"R:R {t['fibo_rr']:.1f}" if t.get("fibo_rr") else "—"
    pnl      = t.get("pnl", 0)
    pnl_s    = f"{pnl_color(pnl)}₹{pnl:>+9,.2f}{C.RST}"

    # Which fibo log file to cross-reference
    fibo_log = t.get("fibo_session", "")
    fibo_ts  = t["fibo_cycle_dt"].strftime("%H:%M") if t.get("fibo_cycle_dt") else "—"

    mistake = ""
    if show_mistake and t.get("fibo_aligned") is False:
        mistake = (f"  {C.YEL}⚠ Traded {traded} but Fibo said {signal} "
                   f"→ direction wrong{C.RST}")

    row = (f"  {C.DIM}{time_s}{C.RST}  {sym:<16}  "
           f"Traded:{C.BLD}{traded}{C.RST}  Fibo:{C.CYN}{signal}{C.RST}  "
           f"{C.DIM}{quality:<12}{C.RST}  {rr:<7}  {pnl_s}")
    if fibo_log:
        row += f"\n    {C.DIM}→ Fibo log: {fibo_log}  (cycle at {fibo_ts}){C.RST}"
    if mistake:
        row += f"\n    {mistake}"
    return row


def print_fibo_alignment(trades: list[dict]):
    section("🔍  FIBO SIGNAL ALIGNMENT ANALYSIS")

    aligned     = sorted([t for t in trades if t.get("fibo_aligned") is True  and t.get("pnl") is not None],
                         key=lambda t: t.get("dt") or datetime.min)
    not_aligned = sorted([t for t in trades if t.get("fibo_aligned") is False and t.get("pnl") is not None],
                         key=lambda t: t.get("dt") or datetime.min)
    no_signal   = sorted([t for t in trades if t.get("fibo_aligned") is None  and t.get("pnl") is not None],
                         key=lambda t: t.get("dt") or datetime.min)

    # ── summary table ─────────────────────────────────────────────────────
    all_groups = [
        ("✅  ALIGNED",      aligned,     C.GRN),
        ("❌  AGAINST FIBO", not_aligned, C.RED),
        ("❓  NO SIGNAL",    no_signal,   C.YEL),
    ]
    print(f"\n  {'Group':<22} {'Trades':>6}  {'Win%':>6}  {'Avg P&L':>12}  {'Total P&L':>13}")
    print(f"  {C.DIM}{'─'*66}{C.RST}")
    for label, subset, col in all_groups:
        if not subset:
            continue
        s = stats([t["pnl"] for t in subset])
        print(f"  {col}{label:<22}{C.RST}  {s['n']:>6}  "
              f"{s['win_rate']:>5.1f}%  {fmt_pnl(s['avg']):>12}  {fmt_pnl(s['total']):>13}")

    # ── comparative insight ───────────────────────────────────────────────
    if aligned and not_aligned:
        wa  = stats([t["pnl"] for t in aligned])["win_rate"]
        wna = stats([t["pnl"] for t in not_aligned])["win_rate"]
        diff = wa - wna
        col  = C.GRN if diff > 0 else C.RED
        print(f"\n  {C.BLD}Following Fibo signal improves win rate by {col}{diff:+.1f}%{C.RST}")

    # ── per-trade detail: aligned ─────────────────────────────────────────
    if aligned:
        print(f"\n  {C.GRN}{C.BLD}✅  ALIGNED TRADES — detail{C.RST}")
        print(f"  {C.DIM}{'─'*70}{C.RST}")
        for t in aligned:
            print(_fibo_trade_row(t))

    # ── per-trade detail: against signal — show each mistake ─────────────
    if not_aligned:
        print(f"\n  {C.RED}{C.BLD}❌  TRADES AGAINST FIBO SIGNAL — mistakes to avoid{C.RST}")
        print(f"  {C.DIM}{'─'*70}{C.RST}")
        for t in not_aligned:
            print(_fibo_trade_row(t, show_mistake=True))
        # aggregate mistake analysis
        losses_here = [t["pnl"] for t in not_aligned if t["pnl"] < 0]
        if losses_here:
            print(f"\n  {C.RED}  Total money lost by going against Fibo: "
                  f"₹{sum(losses_here):,.2f} across {len(losses_here)} trades{C.RST}")

    # ── per-trade detail: no signal ───────────────────────────────────────
    if no_signal:
        # split into: fibo was running (signal=None) vs fibo not running at all
        fibo_running_no_sig = [t for t in no_signal if t.get("fibo_session")]
        fibo_not_running    = [t for t in no_signal if not t.get("fibo_session")]

        # ── A: Fibo was running but gave no directional signal ─────────────
        if fibo_running_no_sig:
            print(f"\n  {C.YEL}{C.BLD}❓  FIBO RUNNING BUT NO DIRECTION SIGNAL — per-trade detail{C.RST}")
            print(f"  {C.DIM}Fibo was active but hadn't formed a trade setup yet at these times.")
            print(f"  Each row shows exactly what Fibo was showing when you entered.{C.RST}")

            by_date: dict[str, list] = defaultdict(list)
            for t in fibo_running_no_sig:
                by_date[t.get("date", "unknown")].append(t)

            for date, day_trades in sorted(by_date.items()):
                print(f"\n  {C.DIM}{'─'*72}{C.RST}")
                print(f"  {C.BLD}Date: {date}{C.RST}")
                for t in sorted(day_trades, key=lambda x: x.get("time", "")):
                    time_s   = t.get("time", "—")[:8]
                    sym      = (t.get("symbol") or "")[-16:]
                    traded   = t.get("opt_type") or "?"
                    pnl      = t.get("pnl", 0)
                    pnl_s    = f"{pnl_color(pnl)}₹{pnl:>+9,.2f}{C.RST}"
                    outcome  = "🟢 WIN " if pnl > 0 else "🔴 LOSS"

                    fibo_log  = t.get("fibo_session", "—")
                    cycle_ts  = t["fibo_cycle_dt"].strftime("%H:%M:%S") if t.get("fibo_cycle_dt") else "—"
                    trend     = t.get("fibo_trend_bias") or "—"
                    pattern   = t.get("fibo_pattern") or "—"
                    reason    = t.get("fibo_no_sig_reason") or "No active trade setup formed"
                    c15m      = t.get("fibo_candles_15m")
                    c1h       = t.get("fibo_candles_1h")
                    spot      = t.get("fibo_spot")

                    candle_info = ""
                    if c15m is not None or c1h is not None:
                        candle_info = f"15m:{c15m or '?'}c  1hr:{c1h or '?'}c"

                    trend_col = C.GRN if trend == "BULLISH" else (C.RED if trend == "BEARISH" else C.YEL)

                    print(f"\n  {C.DIM}{time_s}{C.RST}  {sym:<16}  "
                          f"Traded:{C.BLD}{traded}{C.RST}  {pnl_s}  {outcome}")
                    print(f"    {C.CYN}Fibo log :{C.RST} {C.DIM}{fibo_log}  (cycle @ {cycle_ts}){C.RST}")
                    if spot:
                        print(f"    {C.CYN}Spot     :{C.RST} {C.DIM}{spot:.1f}{C.RST}  "
                              f"{C.CYN}Trend:{C.RST} {trend_col}{trend}{C.RST}  "
                              f"{C.CYN}Pattern:{C.RST} {C.DIM}{pattern}{C.RST}")
                    if candle_info:
                        print(f"    {C.CYN}Candles  :{C.RST} {C.DIM}{candle_info}{C.RST}")
                    print(f"    {C.YEL}⚠ No signal because:{C.RST} {C.DIM}{reason}{C.RST}")
                    print(f"    {C.DIM}Trade log: {t.get('session', '—')}{C.RST}")

        # ── B: Fibo was not running at all ─────────────────────────────────
        if fibo_not_running:
            by_date2: dict[str, list] = defaultdict(list)
            for t in fibo_not_running:
                by_date2[t.get("date", "unknown")].append(t)

            print(f"\n  {C.RED}{C.BLD}🚫  FIBO ANALYZER NOT RUNNING — blind trades{C.RST}")
            print(f"  {C.DIM}No Fibo log found within 30 min of these trades.")
            print(f"  Tip: Always start all 3 bots using START_ALL_BOTS.command{C.RST}")
            for date, day_trades in sorted(by_date2.items()):
                print(f"\n  {C.DIM}Date: {date}{C.RST}")
                for t in sorted(day_trades, key=lambda x: x.get("time", "")):
                    time_s = t.get("time", "—")[:8]
                    sym    = (t.get("symbol") or "")[-16:]
                    traded = t.get("opt_type") or "?"
                    pnl    = t.get("pnl", 0)
                    pnl_s  = f"{pnl_color(pnl)}₹{pnl:>+9,.2f}{C.RST}"
                    print(f"  {C.DIM}{time_s}{C.RST}  {sym:<16}  "
                          f"{C.BLD}{traded}{C.RST}  {pnl_s}  "
                          f"{C.DIM}{t.get('session','—')}{C.RST}")

    # ── high-quality signal performance ──────────────────────────────────
    good = [t for t in trades if t.get("fibo_quality", "") in ("good setup", "strong setup", "ok")
            and t.get("fibo_aligned") is True and t.get("pnl") is not None]
    if good:
        sg = stats([t["pnl"] for t in good])
        print(f"\n  {C.DIM}High-quality Fibo setups (good/strong): "
              f"{sg['n']} trades | {sg['win_rate']:.1f}% win | Total {fmt_pnl(sg['total'])}{C.RST}")

# ── 9. HOLD DURATION ANALYSIS ────────────────────────────────────────────────
def print_hold_analysis(trades: list[dict]):
    section("⏱️   HOLD DURATION ANALYSIS")
    with_hold = [t for t in trades if t.get("hold_secs") is not None and t.get("pnl") is not None]
    if not with_hold:
        print("  No hold-time data found (only available for auto/manual mode with timestamps).")
        return

    wins   = [t["hold_secs"] for t in with_hold if t["pnl"] > 0]
    losses = [t["hold_secs"] for t in with_hold if t["pnl"] < 0]

    def hms(secs: float) -> str:
        s = int(secs)
        return f"{s//60}m {s%60}s"

    if wins:
        print(f"  Avg hold (winning trades) : {C.GRN}{hms(sum(wins)/len(wins))}{C.RST}")
    if losses:
        print(f"  Avg hold (losing  trades) : {C.RED}{hms(sum(losses)/len(losses))}{C.RST}")

    # bucket by hold duration
    buckets = {"< 2 min": [], "2-5 min": [], "5-10 min": [], "> 10 min": []}
    for t in with_hold:
        secs = t["hold_secs"]
        if   secs < 120:  buckets["< 2 min"].append(t["pnl"])
        elif secs < 300:  buckets["2-5 min"].append(t["pnl"])
        elif secs < 600:  buckets["5-10 min"].append(t["pnl"])
        else:             buckets["> 10 min"].append(t["pnl"])

    print()
    for label, pnls in buckets.items():
        if not pnls:
            continue
        s = stats(pnls)
        print(f"  {label:<10}  {s['n']:>3} trades | {s['win_rate']:>5.1f}% win | "
              f"Avg {fmt_pnl(s['avg'])}")

# ── 10. STREAK ANALYSIS ──────────────────────────────────────────────────────
def print_streak_analysis(trades: list[dict]):
    section("🔄  WIN/LOSS STREAK ANALYSIS")
    ordered = sorted([t for t in trades if t.get("pnl") is not None and t.get("dt")],
                     key=lambda t: t["dt"])
    if len(ordered) < 3:
        return

    cur_streak = 1
    max_win_streak = max_loss_streak = 0
    cur_type = "W" if ordered[0]["pnl"] > 0 else "L"
    streak_list = [cur_type]

    for t in ordered[1:]:
        typ = "W" if t["pnl"] > 0 else "L"
        streak_list.append(typ)
        if typ == cur_type:
            cur_streak += 1
        else:
            if cur_type == "W": max_win_streak  = max(max_win_streak,  cur_streak)
            else:               max_loss_streak = max(max_loss_streak, cur_streak)
            cur_streak = 1
            cur_type = typ
    if cur_type == "W": max_win_streak  = max(max_win_streak,  cur_streak)
    else:               max_loss_streak = max(max_loss_streak, cur_streak)

    print(f"  Max consecutive wins  : {C.GRN}{max_win_streak}{C.RST}")
    print(f"  Max consecutive losses: {C.RED}{max_loss_streak}{C.RST}")

    # show recent streak (last 20 trades)
    recent = streak_list[-min(40, len(streak_list)):]
    streak_display = "".join(
        f"{C.GRN}W{C.RST}" if s == "W" else f"{C.RED}L{C.RST}" for s in recent
    )
    print(f"\n  Recent ({len(recent)} trades): {streak_display}")

    # P&L after 2+ consecutive losses
    loss_after_streak = []
    for i in range(2, len(ordered)):
        if ordered[i-1]["pnl"] < 0 and ordered[i-2]["pnl"] < 0:
            loss_after_streak.append(ordered[i]["pnl"])
    if loss_after_streak:
        s = stats(loss_after_streak)
        print(f"\n  {C.DIM}Trade after 2+ consecutive losses: "
              f"{s['n']} cases | {s['win_rate']:.1f}% win | Avg {fmt_pnl(s['avg'])}{C.RST}")

# ── 11. TOP WINS / LOSSES ────────────────────────────────────────────────────
def print_top_trades(trades: list[dict], n: int = 5):
    section(f"🏆  TOP {n} WINS & LOSSES")
    valid = [t for t in trades if t.get("pnl") is not None]
    best  = sorted(valid, key=lambda t: t["pnl"], reverse=True)[:n]
    worst = sorted(valid, key=lambda t: t["pnl"])[:n]

    print(f"  {C.BLD}{C.GRN}Top Wins:{C.RST}")
    for i, t in enumerate(best, 1):
        dt_s = t["dt"].strftime("%d-%b %H:%M") if t.get("dt") else t.get("date", "—")
        sym  = t.get("symbol", "")[-14:]
        xr   = t.get("exit_reason", "")[:12]
        print(f"  {i}. {dt_s}  {sym:<14}  {xr:<12}  {C.GRN}₹{t['pnl']:>+9,.2f}{C.RST}  [{t.get('mode','?')}]")

    print(f"\n  {C.BLD}{C.RED}Top Losses:{C.RST}")
    for i, t in enumerate(worst, 1):
        dt_s = t["dt"].strftime("%d-%b %H:%M") if t.get("dt") else t.get("date", "—")
        sym  = t.get("symbol", "")[-14:]
        xr   = t.get("exit_reason", "")[:12]
        print(f"  {i}. {dt_s}  {sym:<14}  {xr:<12}  {C.RED}₹{t['pnl']:>+9,.2f}{C.RST}  [{t.get('mode','?')}]")

# ── 11b. ALL TRADES — CHRONOLOGICAL ─────────────────────────────────────────
def print_all_trades_chrono(trades: list[dict]):
    valid = sorted(
        [t for t in trades if t.get("pnl") is not None and t.get("dt")],
        key=lambda t: t["dt"],
    )
    if not valid:
        return
    section(f"📋  ALL TRADES — CHRONOLOGICAL  ({len(valid)} trades)")
    header = f"  {'#':<3}  {'Time':<8}  {'Symbol':<20}  {'Type':<4}  {'Buy':>7}  {'Sell':>7}  {'Qty':>5}  {'P&L':>11}  {'Mode':<5}"
    print(f"  {C.DIM}{header.strip()}{C.RST}")
    print(f"  {C.DIM}{'─'*90}{C.RST}")
    running = 0.0
    for i, t in enumerate(valid, 1):
        pnl    = t["pnl"]
        running += pnl
        time_s = t["dt"].strftime("%H:%M:%S")
        sym    = (t.get("symbol") or "")[-20:]
        otype  = t.get("opt_type") or "?"
        buy_s  = f"{t['buy_px']:.1f}"  if t.get("buy_px")  is not None else "—"
        sell_s = f"{t['sell_px']:.1f}" if t.get("sell_px") is not None else "—"
        qty_s  = str(t.get("qty") or "—")
        mode_s = t.get("mode", "?")[:5]
        col    = C.GRN if pnl >= 0 else C.RED
        run_col = C.GRN if running >= 0 else C.RED
        print(
            f"  {C.DIM}{i:<3}{C.RST}  {C.DIM}{time_s}{C.RST}  "
            f"{sym:<20}  {C.BLD}{otype:<4}{C.RST}  "
            f"{C.DIM}{buy_s:>7}  {sell_s:>7}  {qty_s:>5}{C.RST}  "
            f"{col}₹{pnl:>+9,.0f}{C.RST}  "
            f"{C.DIM}{mode_s:<5}  running: {run_col}₹{running:>+10,.0f}{C.RST}"
        )
    print(f"  {C.DIM}{'─'*90}{C.RST}")
    net_col = C.GRN if running >= 0 else C.RED
    print(f"  {' '*57}{net_col}{C.BLD}NET  ₹{running:>+10,.0f}{C.RST}")


# ── 12. SESSION NARRATIVE ────────────────────────────────────────────────────
def print_session_narrative(trades: list[dict]):
    section("📖  SESSION NARRATIVE — What Actually Happened")
    valid = [t for t in trades if t.get("pnl") is not None and t.get("dt")]
    if len(valid) < 3:
        print("  Not enough trades for narrative analysis.")
        return

    narratives: list[tuple] = []  # (category, dt, message)

    # ── Against-signal trades ─────────────────────────────────────────────
    against_sig = sorted([t for t in valid if t.get("fibo_aligned") is False],
                         key=lambda x: x["dt"])
    for t in against_sig:
        t_time   = t["dt"].strftime("%H:%M")
        traded   = t.get("opt_type") or "?"
        fibo_sig = t.get("fibo_signal") or "?"
        trend    = t.get("fibo_trend_bias") or "?"
        pnl      = t.get("pnl", 0)
        pattern  = t.get("fibo_pattern") or None
        pat_note = f", pattern={pattern}" if pattern and pattern not in ("—", "None") else ""
        traded_dir = "bullish (CE)" if traded == "CE" else "bearish (PE)"
        fibo_dir   = "bullish" if fibo_sig == "CE" else "bearish" if fibo_sig == "PE" else fibo_sig
        outcome    = f"won ₹{pnl:+,.0f}" if pnl > 0 else f"lost ₹{pnl:,.0f}"
        commentary = ("lucky win — Fibo was pointing the other way"
                      if pnl > 0 else "predictable loss — went against a confirmed signal")
        msg = (f"{t_time}: Traded {traded_dir} but Fibo said {fibo_dir} "
               f"(signal={fibo_sig}, trend={trend}{pat_note}) — {outcome}. {commentary}.")
        narratives.append(("against", t["dt"], msg))

    # ── No-signal trades: Fibo running but gave no direction ─────────────
    no_sig = sorted(
        [t for t in valid if t.get("fibo_aligned") is None and t.get("fibo_session")],
        key=lambda x: x["dt"],
    )
    if no_sig:
        # Group consecutive trades with same reason + same trend + gap < 2 h
        groups: list[list[dict]] = []
        current = [no_sig[0]]
        for i in range(1, len(no_sig)):
            prev = no_sig[i - 1]
            curr = no_sig[i]
            gap  = (curr["dt"] - prev["dt"]).total_seconds() / 3600
            if (prev.get("fibo_no_sig_reason", "") == curr.get("fibo_no_sig_reason", "")
                    and prev.get("fibo_trend_bias", "") == curr.get("fibo_trend_bias", "")
                    and gap < 2.0):
                current.append(curr)
            else:
                groups.append(current)
                current = [curr]
        groups.append(current)

        for group in groups:
            first_t = group[0]["dt"].strftime("%H:%M")
            last_t  = group[-1]["dt"].strftime("%H:%M")
            n       = len(group)
            trend   = group[0].get("fibo_trend_bias") or "UNKNOWN"
            reason  = group[0].get("fibo_no_sig_reason") or "no setup formed"

            patterns  = [t.get("fibo_pattern") for t in group
                         if t.get("fibo_pattern") and t.get("fibo_pattern") not in ("—", "None", None)]
            uniq_pat  = list(dict.fromkeys(patterns))
            pat_str   = " / ".join(uniq_pat[:2]) if uniq_pat else None

            c15m_vals = [t.get("fibo_candles_15m") for t in group
                         if t.get("fibo_candles_15m") is not None]

            wins  = sum(1 for t in group if t["pnl"] > 0)
            losses_n = n - wins
            total = sum(t["pnl"] for t in group)

            ce_c = sum(1 for t in group if t.get("opt_type") == "CE")
            pe_c = sum(1 for t in group if t.get("opt_type") == "PE")
            dominant = "CE" if ce_c > pe_c else "PE" if pe_c > ce_c else "mixed"
            with_trend = ((trend == "BULLISH" and dominant == "CE") or
                          (trend == "BEARISH" and dominant == "PE"))

            if n >= 3:
                time_range = first_t if first_t == last_t else f"{first_t}–{last_t}"
                fibo_ctx = f"Fibo showed Trend={trend}"
                if pat_str:
                    fibo_ctx += f", pattern={pat_str}"
                if c15m_vals:
                    c_range = (f"{min(c15m_vals)}–{max(c15m_vals)}"
                               if min(c15m_vals) != max(c15m_vals) else str(c15m_vals[0]))
                    fibo_ctx += f", 15m had {c_range} candles"
                fibo_ctx += f" — {reason}"

                behavior = (f"traded {dominant} "
                            f"({'with' if with_trend else 'against'} trend direction)")
                pnl_note = f"₹{total:+,.0f}" if total >= 0 else f"₹{total:,.0f}"
                outcome_s = f"won {wins}/{n} (net {pnl_note})"

                if wins > losses_n and with_trend:
                    conclusion = ("Trend direction was right even without a confirmed setup — "
                                  "but this is a fragile edge that won't hold long-term.")
                elif wins > losses_n and not with_trend:
                    conclusion = ("Won despite going against the trend — short-term momentum helped, "
                                  "but repeating this without a signal is dangerous.")
                elif losses_n > wins and with_trend:
                    conclusion = (f"Trend was correct but signal-less entries still lost {losses_n}/{n} — "
                                  "setup confirmation matters even when direction is right.")
                elif losses_n > wins and not with_trend:
                    conclusion = (f"Lost {losses_n}/{n} going against trend without any signal — "
                                  "worst-case entry condition: no signal AND wrong direction.")
                else:
                    conclusion = ("50/50 result without signal confirmation — entries were effectively random.")

                msg = (f"{time_range}: {n} trades, {fibo_ctx}. "
                       f"You {behavior}, {outcome_s}. {conclusion}")
                narratives.append(("no_sig_group", group[0]["dt"], msg))
            else:
                # Individual — flag notable situations only
                for t in group:
                    t_time  = t["dt"].strftime("%H:%M")
                    pnl     = t.get("pnl", 0)
                    c15m    = t.get("fibo_candles_15m")
                    traded  = t.get("opt_type") or "?"
                    pattern = t.get("fibo_pattern") or "none"
                    outcome = f"won ₹{pnl:+,.0f}" if pnl > 0 else f"lost ₹{pnl:,.0f}"

                    if ("neutral" in reason.lower() or "no clear direction" in reason.lower()
                            or "no direction" in reason.lower()):
                        cycle_t = (t["fibo_cycle_dt"].strftime("%H:%M")
                                   if t.get("fibo_cycle_dt") else "?")
                        msg = (f"{t_time}: Fibo showed no direction at {cycle_t} (market neutral) — "
                               f"you still entered {traded} and {outcome}. "
                               f"Entering without directional clarity is speculation, not trading.")
                        narratives.append(("neutral", t["dt"], msg))

                    elif c15m is not None and c15m >= 8 and pnl < -3000:
                        msg = (f"{t_time}: LOSS ₹{pnl:,.0f} — {c15m} candles available on 15m, "
                               f"enough for a signal to form but none did. "
                               f"Fibo trend={trend}, pattern={pattern}. "
                               f"You entered {traded} without any setup → avoidable loss.")
                        narratives.append(("notable_loss", t["dt"], msg))

                    elif c15m is not None and c15m < 8 and pnl < -3000:
                        msg = (f"{t_time}: LOSS ₹{pnl:,.0f} — only {c15m} candle(s) on 15m "
                               f"(still building, Fibo needs ~8 minimum). "
                               f"Trend={trend}. Premature entry before any setup was possible.")
                        narratives.append(("notable_loss", t["dt"], msg))

    if not narratives:
        print("  All trades had Fibo signals. No blind or contrary entries detected.")
        return

    against_list = [(dt, m) for cat, dt, m in narratives if cat == "against"]
    groups_list  = [(dt, m) for cat, dt, m in narratives if cat == "no_sig_group"]
    notable_list = [(dt, m) for cat, dt, m in narratives if cat == "notable_loss"]
    neutral_list = [(dt, m) for cat, dt, m in narratives if cat == "neutral"]

    if against_list:
        print(f"  {C.RED}{C.BLD}Trades Against Fibo Signal:{C.RST}")
        for _, msg in sorted(against_list):
            print(f"  {C.RED}{msg}{C.RST}")
        print()

    if groups_list:
        print(f"  {C.YEL}{C.BLD}Signal-less Entry Episodes:{C.RST}")
        for _, msg in sorted(groups_list):
            print(f"  {C.YEL}{msg}{C.RST}")
        print()

    if notable_list:
        print(f"  {C.RED}{C.BLD}Key Individual Mistakes:{C.RST}")
        for _, msg in sorted(notable_list):
            print(f"  {C.RED}{msg}{C.RST}")
        print()

    if neutral_list:
        print(f"  {C.YEL}{C.BLD}Neutral Market Entries (Speculative):{C.RST}")
        for _, msg in sorted(neutral_list):
            print(f"  {C.YEL}{msg}{C.RST}")

# ── 13. LOSS DIAGNOSIS ───────────────────────────────────────────────────────
def print_loss_diagnosis(trades: list[dict]):
    section("🔴  LOSS DIAGNOSIS")
    losses = [t for t in trades if t.get("pnl") is not None and t["pnl"] < 0]
    if not losses:
        print("  No losses found — great trading!")
        return

    total_losses = len(losses)
    findings = []

    # Direction misalignment
    against_signal = [t for t in losses if t.get("fibo_aligned") is False]
    if against_signal:
        pct = len(against_signal) / total_losses * 100
        findings.append((pct, f"{C.RED}{len(against_signal)}/{total_losses}{C.RST} losses ({pct:.0f}%) traded AGAINST the Fibo signal"))

    # Early morning losses (9:15-9:45)
    early = [t for t in losses if t.get("time") and "09:15" <= t["time"] <= "09:45"]
    if early:
        pct = len(early) / total_losses * 100
        findings.append((pct, f"{C.RED}{len(early)}/{total_losses}{C.RST} losses ({pct:.0f}%) in 09:15-09:45 opening window"))

    # Short hold time in losses
    fast_losses = [t for t in losses if t.get("hold_secs") and t["hold_secs"] < 120]
    if fast_losses:
        pct = len(fast_losses) / total_losses * 100
        findings.append((pct, f"{C.RED}{len(fast_losses)}/{total_losses}{C.RST} losses ({pct:.0f}%) exited in < 2 min — SL too tight?"))

    # CE losses in bearish sessions vs PE losses in bullish
    ce_losses = [t for t in losses if t.get("opt_type") == "CE"]
    pe_losses = [t for t in losses if t.get("opt_type") == "PE"]
    if ce_losses:
        pct = len(ce_losses) / total_losses * 100
        findings.append((pct, f"CE trades caused {len(ce_losses)}/{total_losses} ({pct:.0f}%) of losses"))
    if pe_losses:
        pct = len(pe_losses) / total_losses * 100
        findings.append((pct, f"PE trades caused {len(pe_losses)}/{total_losses} ({pct:.0f}%) of losses"))

    # Large losses (>₹5000)
    large = [t for t in losses if t["pnl"] < -5000]
    if large:
        pct = len(large) / total_losses * 100
        avg = sum(t["pnl"] for t in large) / len(large)
        findings.append((pct, f"{C.RED}{len(large)}{C.RST} large losses (>{C.RED}₹5000{C.RST}) | Avg: {fmt_pnl(avg)}"))

    # Dynamic SL vs Trailing SL losses
    dynamic_losses = [t for t in losses if t.get("exit_reason") == "DYNAMIC_SL"]
    if dynamic_losses:
        pct = len(dynamic_losses) / total_losses * 100
        findings.append((pct, f"Dynamic SL hit: {len(dynamic_losses)} losses — review SL distance config"))

    if not findings:
        print("  Loss pattern analysis inconclusive — need more data.")
    else:
        for _, msg in sorted(findings, reverse=True):
            print(f"  • {msg}")

# ── 14. RECOMMENDATIONS ──────────────────────────────────────────────────────
def print_recommendations(trades: list[dict]):
    section("💡  RECOMMENDATIONS  (data-driven)")
    valid = [t for t in trades if t.get("pnl") is not None]
    if len(valid) < 5:
        print("  Need at least 5 trades for reliable recommendations.")
        return

    recs = []

    # Fibo alignment
    al   = [t["pnl"] for t in valid if t.get("fibo_aligned") is True]
    nal  = [t["pnl"] for t in valid if t.get("fibo_aligned") is False]
    if al and nal:
        wr_al  = sum(1 for p in al  if p > 0) / len(al)  * 100
        wr_nal = sum(1 for p in nal if p > 0) / len(nal) * 100
        if wr_al > wr_nal + 10:
            recs.append(f"Fibo-aligned trades win {wr_al:.0f}% vs {wr_nal:.0f}% — "
                        f"always check Fibo signal before entering")

    # Opening window
    opening = [t["pnl"] for t in valid if t.get("time") and "09:15" <= t["time"] <= "09:30"]
    if opening:
        wr_open = sum(1 for p in opening if p > 0) / len(opening) * 100
        if wr_open < 40:
            recs.append(f"09:15-09:30 win rate is only {wr_open:.0f}% — avoid trading the first 15 min")

    # CE vs PE timing
    ce_morn = [t["pnl"] for t in valid if t.get("opt_type") == "CE" and t.get("time", "")[:2] in ("09", "10")]
    pe_aftn = [t["pnl"] for t in valid if t.get("opt_type") == "PE" and t.get("time", "")[:2] in ("14", "15")]
    if ce_morn and sum(ce_morn) > 0:
        recs.append(f"CE trades in morning (9-11am) are profitable — favour CE entries pre-noon")
    if pe_aftn and sum(pe_aftn) > 0:
        recs.append(f"PE trades in afternoon (2-3:30pm) show positive P&L — favour PE post-2pm")

    # Hold time
    short  = [t["pnl"] for t in valid if t.get("hold_secs") and t["hold_secs"] < 120]
    medium = [t["pnl"] for t in valid if t.get("hold_secs") and 120 <= t["hold_secs"] < 600]
    if short and medium:
        wr_s = sum(1 for p in short  if p > 0) / len(short)  * 100
        wr_m = sum(1 for p in medium if p > 0) / len(medium) * 100
        if wr_m > wr_s + 15:
            recs.append(f"Trades held 2-10 min win {wr_m:.0f}% vs {wr_s:.0f}% for < 2 min — let winners breathe")

    # Profit factor
    pf = stats([t["pnl"] for t in valid])["profit_factor"]
    if pf < 1.0:
        recs.append("Profit factor < 1.0 — losses exceed wins; tighten entry criteria or reduce lot size")
    elif pf > 2.5:
        recs.append(f"Profit factor {pf:.2f} is strong — consider gradually increasing position size")

    # Streak recovery
    losses_seq = sorted([t for t in valid if t["pnl"] < 0 and t.get("dt")], key=lambda t: t["dt"])
    if len(losses_seq) >= 3:
        recs.append("Take a 15-min break after 2 consecutive losses — emotional trading amplifies drawdown")

    if not recs:
        recs.append("Not enough data yet for strong recommendations. Keep trading and re-run this analyzer.")

    for i, rec in enumerate(recs, 1):
        print(f"  {i}. {rec}")

# ── 15. INDEX BREAKDOWN ──────────────────────────────────────────────────────
def print_index_breakdown(trades: list[dict]):
    section("📌  PERFORMANCE BY INDEX")
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.get("pnl") is not None:
            groups[t.get("index", "UNK")].append(t["pnl"])
    for idx, pnls in sorted(groups.items(), key=lambda x: -sum(x[1])):
        s = stats(pnls)
        print(f"  {C.BLD}{idx:<12}{C.RST}  {s['n']:>4} trades | "
              f"Win {s['win_rate']:>5.1f}% | Total {fmt_pnl(s['total'])} | "
              f"Avg {fmt_pnl(s['avg'])}")

# ─────────────────────────────────────────────────────────────────────────────
#  AI SUMMARY EXPORT  (clean markdown, paste into ChatGPT / Claude / Gemini)
# ─────────────────────────────────────────────────────────────────────────────
def save_ai_summary(trades: list[dict], date_from=None, date_to=None) -> str:
    import io
    ts           = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    date_display = (date_from if date_from == date_to
                    else f"{date_from} to {date_to}" if date_from and date_to
                    else "All Sessions")
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"AI_Summary_{ts}.md")

    valid = [t for t in trades if t.get("pnl") is not None]
    s     = stats([t["pnl"] for t in valid]) if valid else stats([])
    modes = sorted(set(t.get("mode", "?") for t in valid))

    def _cap(fn, *args):
        """Capture printed output of fn(*args) as plain text."""
        old = sys.stdout
        buf = io.StringIO()
        sys.stdout = buf
        try:
            fn(*args)
        finally:
            sys.stdout = old
        raw = re.sub(r"\033\[[0-9;]*m", "", buf.getvalue())
        # drop pure box-drawing / separator lines
        return [ln for ln in raw.splitlines()
                if ln.strip() and not set(ln.strip()).issubset(set("═─╔╗╚╝║╠╣╦╩╬ "))]

    lines: list[str] = []

    # ── Context / Header ──────────────────────────────────────────────────
    lines += [
        "# Groww Options Bot — AI Analysis Export",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Period:** {date_display}  ",
        f"**Mode:** {', '.join(modes)}",
        "",
        "> **Context for the AI tool:**  ",
        "> This data is from an algorithmic options trading bot on Indian stock exchanges (NSE/BSE).  ",
        "> - **CE** = Call option — profits when market index rises.  ",
        "> - **PE** = Put option — profits when market index falls.  ",
        "> - **NIFTY** = NSE 50-stock index (spot ~23,400 range). Lot size = 25 units.  ",
        "> - **Fibo signal** = Fibonacci trend analysis recommendation (CE or PE).  ",
        "> - **15m candles** = number of 15-minute candles available at trade time; fewer than 8 means Fibonacci cannot form a reliable signal yet.  ",
        "> - **Exit reasons:** TRAILING_SL = trailing stop-loss hit, DYNAMIC_SL = fixed stop-loss hit, TARGET = profit target reached, SL_HIT = manual stop.  ",
        "> - **Aligned** = YES means trade direction matched Fibo recommendation; NO = went against it; NO SIG = Fibo had no signal.  ",
        "",
        "---",
        "",
    ]

    # ── Overview ──────────────────────────────────────────────────────────
    pf_s = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
    lines += [
        "## Session Overview",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total Trades | {s['n']} |",
        f"| Winning Trades | {s['wins']} ({s['win_rate']:.1f}%) |",
        f"| Losing Trades | {s['losses']} |",
        f"| Total P&L | ₹{s['total']:+,.2f} |",
        f"| Average Win | ₹{s['avg_win']:+,.2f} |",
        f"| Average Loss | ₹{s['avg_loss']:+,.2f} |",
        f"| Profit Factor | {pf_s} |",
        f"| Best Single Trade | ₹{s['best']:+,.2f} |",
        f"| Worst Single Trade | ₹{s['worst']:+,.2f} |",
        "",
        "---",
        "",
    ]

    # ── All Trades ────────────────────────────────────────────────────────
    lines += [
        "## All Trades",
        "",
        "| # | Date | Time | Symbol | Type | Buy ₹ | Sell ₹ | Qty | P&L ₹ | Hold (min) | Exit | Fibo Sig | Fibo Trend | Pattern | 15m Candles | Aligned | No-Signal Reason |",
        "|---|------|------|--------|------|-------|--------|-----|-------|-----------|------|----------|-----------|---------|------------|---------|-----------------|",
    ]
    for i, t in enumerate(
            sorted(valid, key=lambda x: x.get("dt") or datetime.min), 1):
        sym     = (t.get("symbol") or "")[-20:]
        buy_px  = f"{t['buy_px']:.2f}"  if t.get("buy_px")  is not None else "—"
        sell_px = f"{t['sell_px']:.2f}" if t.get("sell_px") is not None else "—"
        qty     = str(t.get("qty") or "—")
        hold    = f"{t['hold_secs']/60:.1f}" if t.get("hold_secs") else "—"
        aligned = ("YES"    if t.get("fibo_aligned") is True
                   else "NO"     if t.get("fibo_aligned") is False
                   else "NO SIG")
        nosig   = (t.get("fibo_no_sig_reason") or "—").replace("|", "/")[:45]
        fpat    = (t.get("fibo_pattern") or "—").replace("|", "/")[:25]
        lines.append(
            f"| {i} | {t.get('date','—')} | {(t.get('time','—'))[:8]} | {sym} "
            f"| {t.get('opt_type','?')} | {buy_px} | {sell_px} | {qty} "
            f"| {t['pnl']:+,.2f} | {hold} | {t.get('exit_reason','—')} "
            f"| {t.get('fibo_signal','—')} | {t.get('fibo_trend_bias','—')} "
            f"| {fpat} | {t.get('fibo_candles_15m','—')} | {aligned} | {nosig} |"
        )
    lines += ["", "---", ""]

    # ── Fibo Alignment Table ──────────────────────────────────────────────
    def _fibo_row(label, subset):
        if not subset:
            return f"| {label} | 0 | — | ₹0.00 |"
        s2 = stats([t["pnl"] for t in subset])
        return (f"| {label} | {s2['n']} | {s2['win_rate']:.0f}% "
                f"| ₹{s2['total']:+,.2f} |")

    al      = [t for t in valid if t.get("fibo_aligned") is True]
    nal     = [t for t in valid if t.get("fibo_aligned") is False]
    ns_run  = [t for t in valid if t.get("fibo_aligned") is None and t.get("fibo_session")]
    ns_bld  = [t for t in valid if t.get("fibo_aligned") is None and not t.get("fibo_session")]

    lines += [
        "## Fibo Signal Alignment",
        "",
        "| Category | Trades | Win Rate | Total P&L |",
        "|----------|--------|----------|-----------|",
        _fibo_row("Aligned with Fibo signal", al),
        _fibo_row("Traded AGAINST Fibo signal", nal),
        _fibo_row("No signal — Fibo running, 15m still building", ns_run),
        _fibo_row("No signal — Fibo NOT running (blind trades)", ns_bld),
        "",
        "---",
        "",
    ]

    # ── Session Narrative ─────────────────────────────────────────────────
    lines += ["## Session Narrative", ""]
    lines += _cap(print_session_narrative, trades) or ["  (No narrative — all trades had confirmed signals.)"]
    lines += ["", "---", ""]

    # ── Loss Patterns ─────────────────────────────────────────────────────
    lines += ["## Key Loss Patterns", ""]
    lines += _cap(print_loss_diagnosis, trades) or ["  (No losses detected.)"]
    lines += ["", "---", ""]

    # ── Recommendations ───────────────────────────────────────────────────
    lines += ["## Recommendations", ""]
    lines += _cap(print_recommendations, trades) or ["  (Not enough data for recommendations.)"]
    lines += ["", "---", ""]

    # ── Per-Trade Timeline (verbose) ──────────────────────────────────────
    lines += [
        "## Per-Trade Timeline (verbose)",
        "",
        "_One block per trade — useful for asking the AI 'what went wrong at 11:23?'_",
        "",
    ]
    for i, t in enumerate(
            sorted(valid, key=lambda x: x.get("dt") or datetime.min), 1):
        pnl_word = "PROFIT" if t["pnl"] > 0 else "LOSS"
        hold_s   = f"{t['hold_secs']/60:.1f} min" if t.get("hold_secs") else "—"
        lines += [
            f"### Trade {i} — {t.get('date','?')} {(t.get('time','?'))[:8]}",
            f"- Symbol: {t.get('symbol','?')}  |  Type: {t.get('opt_type','?')}  |  Mode: {t.get('mode','?')}",
            f"- Buy: ₹{t.get('buy_px') or '—'}  |  Sell: ₹{t.get('sell_px') or '—'}  |  Qty: {t.get('qty') or '—'}",
            f"- **P&L: {pnl_word} ₹{t['pnl']:+,.2f}**  |  Hold: {hold_s}  |  Exit: {t.get('exit_reason','—')}",
            f"- Fibo Signal: {t.get('fibo_signal','—')}  |  Trend: {t.get('fibo_trend_bias','—')}  |  Pattern: {t.get('fibo_pattern','—')}",
            f"- 15m Candles: {t.get('fibo_candles_15m','—')}  |  1h Candles: {t.get('fibo_candles_1h','—')}  |  Aligned: {('YES' if t.get('fibo_aligned') is True else 'NO' if t.get('fibo_aligned') is False else 'NO SIG')}",
            f"- No-Signal Reason: {t.get('fibo_no_sig_reason','—')}",
            f"- Fibo Log: {t.get('fibo_session','—')}",
            "",
        ]

    lines += ["---", "", "_End of AI Analysis Export_", ""]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n{C.GRN}  ✅ AI Summary saved → {path}{C.RST}")
    print(f"{C.DIM}     Paste the file contents into ChatGPT / Claude for deeper analysis.{C.RST}")
    return path

# ─────────────────────────────────────────────────────────────────────────────
#  CHATGPT DEEP ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are an expert Indian intraday options trading analyst specialising in NIFTY and SENSEX index options traded on NSE/BSE.

You will receive structured data from an algorithmic trading bot session. Your job is to produce a comprehensive, data-driven post-session review that a trader can act on immediately.

KEY DOMAIN CONTEXT:
- CE = Call option (trader expects market to rise). PE = Put option (trader expects market to fall).
- NIFTY is the NSE 50-stock index, typical range 22,000–24,000. Lot size = 25 units.
- "Fibo signal" = Fibonacci trend analysis direction (CE or PE). "Aligned YES" = trade matched the signal.
- 15m candles: fewer than 8 means Fibonacci analysis is unreliable (insufficient history).
- Exit reasons: TRAILING_SL = trailing stop-loss, DYNAMIC_SL = fixed stop-loss, TARGET = profit target hit.
- Profit Factor = gross profit / gross loss. Healthy = above 1.5. Dangerous = below 1.2.

ANALYSIS STRUCTURE (follow this order, use markdown headers):
1. **What Actually Happened** — 2-3 sentence honest summary of the session
2. **What Worked Well** — Identify the profitable pattern(s) with specific trade times/values
3. **Root Cause of Each Significant Loss** — For every loss > ₹5,000: explain exactly what likely caused it (late entry, trend exhaustion, wrong direction, oversized SL, etc.)
4. **Structural Issues** — Recurring problems visible across multiple trades (overtrading, late trend chasing, SL too wide, no exhaustion filter, etc.)
5. **Key Metrics Insight** — What the profit factor, avg-win vs avg-loss ratio, and win rate TOGETHER reveal about the system's health
6. **Specific Bot Improvements** — Concrete, implementable filters or logic changes (e.g., "avoid PE entry if 5+ consecutive bearish candles", "exit if option doesn't move in 3 min")
7. **Most Important Single Change** — One sentence, the highest-impact improvement

STYLE RULES:
- Be direct and specific. Reference actual trade times, P&L numbers, Fibonacci context from the data.
- Do NOT be generic. Every insight must be traceable to something in the data.
- If a trade was correct and worked, say so clearly — don't just focus on losses.
- Format numbers with ₹ prefix and commas (e.g., ₹14,560).
"""

def ask_chatgpt(md_path: str, trades: list[dict]) -> str | None:
    """Send the AI summary markdown to ChatGPT and stream the response.
    Returns path to saved ChatGPT analysis file, or None on failure."""

    if not _OPENAI_AVAILABLE:
        print(f"\n  {C.YEL}openai package not installed. Run: pip install openai{C.RST}")
        return None

    cfg = _load_ai_cfg()
    if cfg.get("enabled") is False:
        return None

    api_key = _get_openai_key()
    if not api_key:
        api_key = _prompt_and_save_key()
    if not api_key:
        return None

    # Read the markdown we just wrote
    try:
        with open(md_path, encoding="utf-8") as f:
            md_content = f.read()
    except Exception as e:
        print(f"  {C.RED}Could not read AI summary: {e}{C.RST}")
        return None

    model = cfg.get("model", "gpt-4o")

    print(f"\n{C.CYN}{'═'*W}{C.RST}")
    print(f"{C.BLD}{C.WHT}{'🤖  CHATGPT DEEP ANALYSIS':^{W}}{C.RST}")
    print(f"{C.CYN}{'═'*W}{C.RST}")
    print(f"\n{C.DIM}  Sending session data to {model}... (streaming){C.RST}\n")

    client = _openai_lib.OpenAI(api_key=api_key)

    # Collect streamed response
    ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out  = os.path.join(OUT_DIR, f"ChatGPT_Analysis_{ts}.md")
    full_response = []

    try:
        with client.chat.completions.stream(
            model=model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": md_content},
            ],
        ) as stream:
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    print(delta, end="", flush=True)
                    full_response.append(delta)

    except _openai_lib.AuthenticationError:
        print(f"\n  {C.RED}❌ Invalid OpenAI API key. Edit ai_config.json and correct it.{C.RST}")
        cfg = _load_ai_cfg()
        cfg.pop("openai_api_key", None)
        _save_ai_cfg(cfg)
        return None
    except _openai_lib.RateLimitError as e:
        if "insufficient_quota" in str(e):
            print(f"\n  {C.RED}❌ OpenAI account has no credits.{C.RST}")
            print(f"  {C.YEL}Add billing at: https://platform.openai.com/billing{C.RST}")
            print(f"  {C.DIM}  OR use [b] in the menu to open ChatGPT in your browser automatically.{C.RST}")
        else:
            print(f"\n  {C.RED}❌ OpenAI rate limit hit. Wait a minute and retry with [g].{C.RST}")
            print(f"  {C.DIM}  OR use [b] to open ChatGPT in browser instead.{C.RST}")
        return None
    except _openai_lib.APIConnectionError:
        print(f"\n  {C.RED}❌ No internet connection or OpenAI unreachable.{C.RST}")
        return None
    except Exception as e:
        print(f"\n  {C.RED}❌ ChatGPT error: {e}{C.RST}")
        return None

    response_text = "".join(full_response)
    print(f"\n\n{C.CYN}{'─'*W}{C.RST}")

    # Save to file
    header = (f"# ChatGPT Deep Analysis\n\n"
              f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n"
              f"**Model:** {model}  \n"
              f"**Source:** {os.path.basename(md_path)}\n\n---\n\n")
    with open(out, "w", encoding="utf-8") as f:
        f.write(header + response_text)

    print(f"{C.GRN}  ✅ ChatGPT analysis saved → {out}{C.RST}")
    return out

# ─────────────────────────────────────────────────────────────────────────────
#  SAVE REPORT TO FILE
# ─────────────────────────────────────────────────────────────────────────────
def save_report(trades: list[dict]):
    import io
    ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(OUT_DIR, f"Analysis_{ts}.txt")

    old_stdout = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    run_analysis(trades, save=False)
    sys.stdout = old_stdout

    raw = re.sub(r"\033\[[0-9;]*m", "", buf.getvalue())
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    print(f"\n{C.DIM}  Report saved → {path}{C.RST}")

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN ANALYSIS RUNNER
# ─────────────────────────────────────────────────────────────────────────────
def run_analysis(trades: list[dict], save: bool = True):
    print_banner()
    print_overview(trades)
    print_pnl_curve(trades)
    print_daily_pnl(trades)
    print_time_heatmap(trades)
    print_direction_analysis(trades)
    print_exit_analysis(trades)
    print_fibo_alignment(trades)
    print_session_narrative(trades)
    print_hold_analysis(trades)
    print_streak_analysis(trades)
    print_index_breakdown(trades)
    print_top_trades(trades)
    print_all_trades_chrono(trades)
    print_loss_diagnosis(trades)
    print_recommendations(trades)
    print(f"\n{C.CYN}{'═'*W}{C.RST}\n")


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _load_and_run(date_from: str | None, date_to: str | None, label: str):
    """Load logs for the given date range, run analysis, return trades."""
    print(f"\n{C.CYN}Loading {label}...{C.RST}")
    trades = parse_groww_logs(date_from, date_to)
    fibo   = parse_fibo_logs(date_from, date_to)
    trades = correlate_signals(trades, fibo)

    print(f"  Groww_Bot logs  : {len(list(Path(GROWW_DIR).glob('*.log')))} total files")
    print(f"  Trades found    : {len(trades)}  |  Fibo cycles: {len(fibo)}")

    if not trades:
        print(f"\n{C.YEL}  No completed trades found for this period. "
              f"Run PROD10FEB bot first.{C.RST}")
        return [], None

    run_analysis(trades, save=False)
    md_path = save_ai_summary(trades, date_from, date_to)
    ask_chatgpt(md_path, trades)
    return trades, md_path


def _prompt_specific_date() -> str | None:
    """Ask user to enter a date and validate it."""
    while True:
        try:
            raw = input(f"  Enter date {C.DIM}(YYYY-MM-DD){C.RST}: ").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not raw:
            return None
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            return raw
        print(f"  {C.YEL}Invalid format. Please use YYYY-MM-DD (e.g. 2026-05-12){C.RST}")


def _show_startup_menu() -> tuple[str | None, str | None, str]:
    """Show the startup analysis-type menu. Returns (date_from, date_to, label)."""
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"\n{C.CYN}{'═'*W}{C.RST}")
    print(f"{C.BLD}{C.WHT}{'ANALYZE BOT — TRADING PERFORMANCE ANALYZER':^{W}}{C.RST}")
    print(f"{C.CYN}{'═'*W}{C.RST}")
    print(f"\n  {C.BLD}What would you like to analyze?{C.RST}\n")
    print(f"  {C.BLD}{C.GRN}[1]{C.RST}  Today's Analysis          {C.DIM}({today}){C.RST}")
    print(f"  {C.BLD}{C.YEL}[2]{C.RST}  Specific Date Analysis     {C.DIM}(you choose the date){C.RST}")
    print(f"  {C.BLD}{C.CYN}[3]{C.RST}  Full Analysis              {C.DIM}(all data from all sessions){C.RST}")
    print(f"  {C.BLD}{C.RED}[q]{C.RST}  Quit\n")

    while True:
        try:
            choice = input(f"  {C.BLD}Choose [1/2/3/q]:{C.RST} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return None, None, "quit"

        if choice in ("q", "quit"):
            return None, None, "quit"

        if choice == "1":
            return today, today, f"Today's Analysis  ({today})"

        if choice == "2":
            print()
            date = _prompt_specific_date()
            if not date:
                print(f"  {C.YEL}No date entered — returning to menu.{C.RST}\n")
                continue
            return date, date, f"Specific Date Analysis  ({date})"

        if choice == "3":
            return None, None, "Full Analysis  (all sessions)"

        print(f"  {C.YEL}Please enter 1, 2, 3, or q.{C.RST}")


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main():
    # ── startup: ask analysis type ────────────────────────────────────────
    date_from, date_to, label = _show_startup_menu()
    if label == "quit":
        print(f"\n{C.DIM}Analyzer closed.{C.RST}\n")
        return

    result  = _load_and_run(date_from, date_to, label)
    trades, md_path = result if isinstance(result, tuple) else (result, None)

    # ── post-analysis menu ────────────────────────────────────────────────
    while True:
        print(f"\n{C.BLD}What next?{C.RST}")
        print(f"  {C.BLD}[s]{C.RST}  Save terminal report to file")
        print(f"  {C.BLD}[a]{C.RST}  Re-export AI summary (.md)")
        print(f"  {C.BLD}[g]{C.RST}  ChatGPT analysis via API  (needs OpenAI credits)")
        print(f"  {C.BLD}[b]{C.RST}  ChatGPT analysis via Browser  (free, uses your login)")
        print(f"  {C.BLD}[1]{C.RST}  Today's Analysis")
        print(f"  {C.BLD}[2]{C.RST}  Specific Date Analysis")
        print(f"  {C.BLD}[3]{C.RST}  Full Analysis")
        print(f"  {C.BLD}[q]{C.RST}  Quit")

        try:
            choice = input(f"\n  {C.BLD}→ {C.RST}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            break

        if choice == "q":
            break

        elif choice == "s":
            if trades:
                save_report(trades)
            else:
                print(f"  {C.YEL}No data to save.{C.RST}")

        elif choice == "a":
            if trades:
                md_path = save_ai_summary(trades, date_from, date_to)
            else:
                print(f"  {C.YEL}No data to export.{C.RST}")

        elif choice == "g":
            if trades:
                if not md_path:
                    md_path = save_ai_summary(trades, date_from, date_to)
                ask_chatgpt(md_path, trades)
            else:
                print(f"  {C.YEL}No data to analyze.{C.RST}")

        elif choice == "b":
            if trades:
                if not md_path:
                    md_path = save_ai_summary(trades, date_from, date_to)
                try:
                    from chatgpt_browser import run_chatgpt_browser
                    run_chatgpt_browser(md_path)
                except ImportError:
                    print(f"  {C.RED}chatgpt_browser.py not found in project folder.{C.RST}")
            else:
                print(f"  {C.YEL}No data to analyze.{C.RST}")

        elif choice == "1":
            today = datetime.now().strftime("%Y-%m-%d")
            date_from, date_to = today, today
            result = _load_and_run(date_from, date_to, f"Today's Analysis  ({today})")
            trades, md_path = result if isinstance(result, tuple) else (result, None)

        elif choice == "2":
            print()
            date = _prompt_specific_date()
            if date:
                date_from, date_to = date, date
                result = _load_and_run(date_from, date_to, f"Specific Date Analysis  ({date})")
                trades, md_path = result if isinstance(result, tuple) else (result, None)

        elif choice == "3":
            date_from, date_to = None, None
            result = _load_and_run(date_from, date_to, "Full Analysis  (all sessions)")
            trades, md_path = result if isinstance(result, tuple) else (result, None)

    print(f"\n{C.DIM}Analyzer closed.{C.RST}\n")


if __name__ == "__main__":
    main()
