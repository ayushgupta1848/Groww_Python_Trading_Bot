"""
SIGNAL ANALYZER  —  Self-Analysis & Auto-Tuning Engine
=======================================================
Run this whenever you want a performance review.

What it does:
  1. Parses logs from FIBO, PDT, MASTER_SIGNAL bots (no API calls)
  2. Evaluates whether CE/PE signals actually moved price the right way
  3. Identifies broken zones, patterns, hours, and directional biases
  4. Generates BOT_TUNING.json — MASTER_SIGNAL_BOT reads this each cycle
     and applies the corrections automatically (threshold, exclusions,
     directional multipliers)
  5. Shows a clear before/after diff of what changed and why

Run:  python3 SIGNAL_ANALYZER.py
"""

import os, re, sys, json, glob, time
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, List, Dict, Tuple

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TUNING_PATH  = os.path.join(PROJECT_ROOT, "BOT_TUNING.json")

# ══════════════════════════════════════════════════════════════
#  ANSI
# ══════════════════════════════════════════════════════════════
class C:
    RESET    = "\033[0m"
    BOLD     = "\033[1m"
    DIM      = "\033[2m"
    CYAN     = "\033[96m"
    WHITE    = "\033[97m"
    B_CYAN   = "\033[1;96m"
    B_WHITE  = "\033[1;97m"

_ANSI = re.compile(r'\x1b\[[0-9;]*m')

def vlen(s: str) -> int:
    return len(_ANSI.sub("", s))

def rpad(s: str, w: int) -> str:
    return s + " " * max(0, w - vlen(s))

# ══════════════════════════════════════════════════════════════
#  THEMES
# ══════════════════════════════════════════════════════════════
THEMES = {
    "1": {
        "_name": "Groww Classic",
        "_desc": "green / red / cyan",
        "BULLISH": "#00ff00", "BEARISH": "#ff4444", "NEUTRAL": "#ffff00",
        "BORDER": "#00ffff", "HEADER": "#ffffff", "SPOT_VAL": "#ffff00",
        "DIM_TEXT": "#666666", "GOLDEN_ZONE": "#ffcc00",
        "SCORE_HIGH": "#00ff00", "SCORE_MID": "#ffff00", "SCORE_LOW": "#ff4444",
        "ACTION_CE": "#00ff00", "ACTION_PE": "#ff4444", "ACTION_WAIT": "#ffff00",
        "WIN": "#00ff00", "LOSS": "#ff4444", "SECTION_HDR": "#ffff00",
        "HEALTH_OK": "#00ff00", "HEALTH_WARN": "#ffaa00", "HEALTH_BAD": "#ff4444",
        "CHANGE_ADD": "#00ff00", "CHANGE_REM": "#ff4444", "CHANGE_MOD": "#ffaa00",
    },
    "2": {
        "_name": "Amber Night",
        "_desc": "orange / red / gold",
        "BULLISH": "#ffaa00", "BEARISH": "#ff4444", "NEUTRAL": "#ffee88",
        "BORDER": "#ffcc44", "HEADER": "#ffeecc", "SPOT_VAL": "#ffdd88",
        "DIM_TEXT": "#886644", "GOLDEN_ZONE": "#ffcc00",
        "SCORE_HIGH": "#ffaa00", "SCORE_MID": "#ffdd66", "SCORE_LOW": "#ff4444",
        "ACTION_CE": "#ffaa00", "ACTION_PE": "#ff4444", "ACTION_WAIT": "#ffdd88",
        "WIN": "#ffaa00", "LOSS": "#ff4444", "SECTION_HDR": "#ffcc44",
        "HEALTH_OK": "#ffaa00", "HEALTH_WARN": "#ffdd66", "HEALTH_BAD": "#ff4444",
        "CHANGE_ADD": "#ffaa00", "CHANGE_REM": "#ff4444", "CHANGE_MOD": "#ffdd66",
    },
    "3": {
        "_name": "Ocean Blue",
        "_desc": "blue / pink / cyan",
        "BULLISH": "#44aaff", "BEARISH": "#ff4488", "NEUTRAL": "#aaddff",
        "BORDER": "#00ccff", "HEADER": "#cceeff", "SPOT_VAL": "#00ccff",
        "DIM_TEXT": "#446688", "GOLDEN_ZONE": "#ffcc44",
        "SCORE_HIGH": "#44aaff", "SCORE_MID": "#aaddff", "SCORE_LOW": "#ff4488",
        "ACTION_CE": "#44aaff", "ACTION_PE": "#ff4488", "ACTION_WAIT": "#aaddff",
        "WIN": "#44aaff", "LOSS": "#ff4488", "SECTION_HDR": "#aaddff",
        "HEALTH_OK": "#44aaff", "HEALTH_WARN": "#aaddff", "HEALTH_BAD": "#ff4488",
        "CHANGE_ADD": "#44aaff", "CHANGE_REM": "#ff4488", "CHANGE_MOD": "#aaddff",
    },
    "4": {
        "_name": "Minimal",
        "_desc": "no bright colors",
        "BULLISH": "#aaaaaa", "BEARISH": "#888888", "NEUTRAL": "#999999",
        "BORDER": "#888888", "HEADER": "#cccccc", "SPOT_VAL": "#ffffff",
        "DIM_TEXT": "#555555", "GOLDEN_ZONE": "#bbbbbb",
        "SCORE_HIGH": "#aaaaaa", "SCORE_MID": "#888888", "SCORE_LOW": "#666666",
        "ACTION_CE": "#cccccc", "ACTION_PE": "#888888", "ACTION_WAIT": "#999999",
        "WIN": "#cccccc", "LOSS": "#666666", "SECTION_HDR": "#999999",
        "HEALTH_OK": "#aaaaaa", "HEALTH_WARN": "#888888", "HEALTH_BAD": "#666666",
        "CHANGE_ADD": "#cccccc", "CHANGE_REM": "#666666", "CHANGE_MOD": "#888888",
    },
}

COLOR_CONFIG: dict = {}

def _hex_to_ansi(h: str) -> str:
    h = h.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"\033[1;38;2;{r};{g};{b}m"
    except Exception:
        return C.WHITE

def _cc(key: str) -> str:
    return _hex_to_ansi(COLOR_CONFIG.get(key, "#ffffff"))

def select_theme() -> None:
    global COLOR_CONFIG
    print(f"\n{C.B_CYAN}Select color theme:{C.RESET}")
    for k, t in THEMES.items():
        print(f"  {C.B_WHITE}{k}.{C.RESET} {t['_name']:<18}  {C.DIM}{t['_desc']}{C.RESET}")
    choice = input(f"\n{C.B_WHITE}Enter 1–4 (default 1): {C.RESET}").strip() or "1"
    if choice not in THEMES:
        choice = "1"
    COLOR_CONFIG = {k: v for k, v in THEMES[choice].items() if not k.startswith("_")}

# ══════════════════════════════════════════════════════════════
#  DISPLAY
# ══════════════════════════════════════════════════════════════
WIDTH = 68

def hdiv() -> str:
    return _cc("BORDER") + "═" * WIDTH + C.RESET

def sdiv(label: str) -> str:
    right = "─" * max(0, WIDTH - len(label) - 7)
    return _cc("SECTION_HDR") + f"─── {label} {right}" + C.RESET

def bar(ratio: float, width: int = 14) -> str:
    filled = max(0, min(width, round(ratio * width)))
    empty  = width - filled
    col = _cc("SCORE_HIGH") if ratio >= 0.6 else (_cc("SCORE_MID") if ratio >= 0.45 else _cc("SCORE_LOW"))
    return col + "█" * filled + _cc("DIM_TEXT") + "░" * empty + C.RESET

def pct_col(pct: float) -> str:
    col = _cc("WIN") if pct >= 60 else (_cc("NEUTRAL") if pct >= 45 else _cc("LOSS"))
    return col + f"{pct:5.1f}%" + C.RESET

# ══════════════════════════════════════════════════════════════
#  BOT HEALTH
# ══════════════════════════════════════════════════════════════
BOT_SPECS = {
    "MASTER SIGNAL": {"log_dir": os.path.join(PROJECT_ROOT,"logs","master_signal"),
                      "pattern": "Master_Signal_*.log", "refresh": 60,  "stale_x": 5},
    "FIBO ANALYZER": {"log_dir": os.path.join(PROJECT_ROOT,"logs","fibo_analyzer"),
                      "pattern": "Fibo_Analyzer_*.log", "refresh": 90,  "stale_x": 5},
    "PDT TRACKER":   {"log_dir": os.path.join(PROJECT_ROOT,"logs","premium_tracker"),
                      "pattern": "Premium_Tracker_*.log","refresh": 2,   "stale_x": 30},
    "MANUAL BOT":    {"log_dir": os.path.join(PROJECT_ROOT,"logs","groww_bot"),
                      "pattern": "Groww_Bot_*.log",      "refresh": 60,  "stale_x": 10},
}

def check_bot_health() -> List[dict]:
    now_ts = time.time()
    results = []
    for name, spec in BOT_SPECS.items():
        files = sorted(glob.glob(os.path.join(spec["log_dir"], spec["pattern"])))
        if not files:
            results.append({"name": name, "status": "OFFLINE", "last_seen": None, "age_s": None})
            continue
        mtime = os.path.getmtime(files[-1])
        age_s = now_ts - mtime
        stale = spec["refresh"] * spec["stale_x"]
        status = "RUNNING" if age_s < stale else ("STALE" if age_s < stale * 6 else "OFFLINE")
        results.append({"name": name, "status": status,
                        "last_seen": datetime.fromtimestamp(mtime).strftime("%H:%M:%S"),
                        "age_s": int(age_s)})
    return results

# ══════════════════════════════════════════════════════════════
#  LOG PARSERS
# ══════════════════════════════════════════════════════════════
def parse_master_signal_logs(days_back: int = 7) -> List[dict]:
    log_dir = os.path.join(PROJECT_ROOT, "logs", "master_signal")
    files   = sorted(glob.glob(os.path.join(log_dir, "Master_Signal_*.log")))
    cutoff  = datetime.now() - timedelta(days=days_back)
    records = []
    for f in files:
        m = re.search(r"Master_Signal_(\d{4}-\d{2}-\d{2})_", f)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d") < cutoff:
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        rec = json.loads(line)
                        if "ts" in rec and "direction" in rec and "spot" in rec:
                            rec["_source"] = "master"
                            records.append(rec)
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
    records.sort(key=lambda r: r["ts"])
    return records

_RE_FIBO_CYCLE   = re.compile(r"🔄 Analysis cycle #\d+\s+\[(\d{2}:\d{2}:\d{2})\]")
_RE_FIBO_DATE    = re.compile(r"FIBONACCI ANALYZER\s+\|\s+\S+\s+\|\s+(\d{4}-\d{2}-\d{2})")
_RE_FIBO_SPOT    = re.compile(r"\|\s+Spot\s+(\d+(?:\.\d+)?)")
_RE_FIBO_SCORE15 = re.compile(r"15m score:\s*([+-]?\d+)")
_RE_FIBO_PATTERN = re.compile(r"Pattern\s+([A-Z][A-Z _]+?)(?:\s{2,}|$|\|)")
_RE_FIBO_ZONE    = re.compile(r"pos:\s*([^\(]+?)(?:\s*[\(]|\s*$)", re.MULTILINE)
_RE_FIBO_SETUP   = re.compile(r"→\s+(LEAN\s+)?(CE|PE|WAIT|CONFLICT|NEUTRAL)")

def parse_fibo_logs(days_back: int = 7) -> List[dict]:
    log_dir = os.path.join(PROJECT_ROOT, "logs", "fibo_analyzer")
    files   = sorted(glob.glob(os.path.join(log_dir, "Fibo_Analyzer_*.log")))
    cutoff  = datetime.now() - timedelta(days=days_back)
    records = []
    for f in files:
        m = re.search(r"Fibo_Analyzer_(\d{4}-\d{2}-\d{2})_", f)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d") < cutoff:
            continue
        try:
            dt_str   = m.group(1) if m else datetime.now().strftime("%Y-%m-%d")
            cur_date = dt_str
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            for block in re.split(r"(?=🔄 Analysis cycle)", content):
                m_time = _RE_FIBO_CYCLE.search(block)
                if not m_time:
                    continue
                m_date = _RE_FIBO_DATE.search(block)
                if m_date:
                    cur_date = m_date.group(1)
                ts = f"{cur_date}T{m_time.group(1)}"
                m_spot    = _RE_FIBO_SPOT.search(block)
                m_score15 = _RE_FIBO_SCORE15.search(block)
                m_pat     = _RE_FIBO_PATTERN.search(block)
                m_zone    = _RE_FIBO_ZONE.search(block)
                m_setup   = _RE_FIBO_SETUP.findall(block)
                spot    = float(m_spot.group(1)) if m_spot else None
                score15 = int(m_score15.group(1)) if m_score15 else 0
                pattern = m_pat.group(1).strip() if m_pat else "NONE"
                zone    = m_zone.group(1).strip() if m_zone else "UNKNOWN"
                direction = "WAIT"
                for (lean, d) in m_setup:
                    direction = d if d in ("CE", "PE") else "WAIT"
                if spot is not None:
                    records.append({"ts": ts, "spot": spot, "direction": direction,
                                    "score15": score15, "pattern": pattern,
                                    "zone": zone, "_source": "fibo"})
        except Exception:
            pass
    records.sort(key=lambda r: r["ts"])
    return records

_RE_PDT_TICK = re.compile(
    r"\[(\d{2}:\d{2}:\d{2})\].*?SPOT\s+([\d.]+).*?CE\).*?₹\s*([\d.]+).*?PE\).*?₹\s*([\d.]+)",
    re.DOTALL)

def parse_pdt_logs(days_back: int = 7) -> List[dict]:
    log_dir = os.path.join(PROJECT_ROOT, "logs", "premium_tracker")
    files   = sorted(glob.glob(os.path.join(log_dir, "Premium_Tracker_*.log")))
    cutoff  = datetime.now() - timedelta(days=days_back)
    records = []
    for f in files:
        m = re.search(r"Premium_Tracker_(\d{4}-\d{2}-\d{2})_", f)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d") < cutoff:
            continue
        cur_date = m.group(1) if m else datetime.now().strftime("%Y-%m-%d")
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    hit = _RE_PDT_TICK.search(line)
                    if hit:
                        records.append({"ts": f"{cur_date}T{hit.group(1)}",
                                        "spot": float(hit.group(2)),
                                        "ce_ltp": float(hit.group(3)),
                                        "pe_ltp": float(hit.group(4))})
        except Exception:
            pass
    records.sort(key=lambda r: r["ts"])
    return records

# ══════════════════════════════════════════════════════════════
#  OUTCOME EVALUATION
# ══════════════════════════════════════════════════════════════
OUTCOME_WIN_MIN  = 10
OUTCOME_WIN_MAX  = 25
SPOT_THRESH_PCT  = 0.05   # 0.05% spot move = confirmed direction

def _find_next_spot(records: List[dict], after_ts: str) -> Optional[float]:
    try:
        base = datetime.fromisoformat(after_ts)
    except ValueError:
        return None
    lo = base + timedelta(minutes=OUTCOME_WIN_MIN)
    hi = base + timedelta(minutes=OUTCOME_WIN_MAX)
    for r in records:
        try:
            rdt = datetime.fromisoformat(r["ts"])
        except ValueError:
            continue
        if lo <= rdt <= hi:
            return r["spot"]
    return None

def evaluate_outcomes(signals: List[dict]) -> List[dict]:
    for sig in signals:
        if sig["direction"] not in ("CE", "PE"):
            sig.update({"outcome": "SKIP", "spot_exit": None, "move_pts": None, "move_pct": None})
            continue
        next_spot = _find_next_spot(signals, sig["ts"])
        if next_spot is None:
            sig.update({"outcome": "NO_DATA", "spot_exit": None, "move_pts": None, "move_pct": None})
            continue
        move = next_spot - sig["spot"]
        pct  = (move / sig["spot"]) * 100 if sig["spot"] else 0
        sig["spot_exit"] = next_spot
        sig["move_pts"]  = round(move, 2)
        sig["move_pct"]  = round(pct, 4)
        if abs(pct) < SPOT_THRESH_PCT:
            sig["outcome"] = "SCRATCH"
        elif (sig["direction"] == "CE" and pct >= SPOT_THRESH_PCT) or \
             (sig["direction"] == "PE" and pct <= -SPOT_THRESH_PCT):
            sig["outcome"] = "WIN"
        else:
            sig["outcome"] = "LOSS"
    return signals

def evaluate_premium_correlation(signals: List[dict], pdt: List[dict]) -> List[dict]:
    for sig in signals:
        if sig["direction"] not in ("CE", "PE") or not pdt:
            sig["prem_outcome"] = "NO_DATA"
            continue
        try:
            base = datetime.fromisoformat(sig["ts"])
        except ValueError:
            sig["prem_outcome"] = "NO_DATA"
            continue
        window = [t for t in pdt
                  if base <= datetime.fromisoformat(t["ts"]) <= base + timedelta(minutes=15)]
        if len(window) < 2:
            sig["prem_outcome"] = "NO_DATA"
            continue
        if sig["direction"] == "CE":
            sig["prem_outcome"] = "WIN" if window[-1]["ce_ltp"] > window[0]["ce_ltp"] else "LOSS"
        else:
            sig["prem_outcome"] = "WIN" if window[-1]["pe_ltp"] > window[0]["pe_ltp"] else "LOSS"
    return signals

# ══════════════════════════════════════════════════════════════
#  METRICS
# ══════════════════════════════════════════════════════════════
def _wr(wins: int, total: int) -> float:
    return round(100 * wins / total, 1) if total > 0 else 0.0

def aggregate_metrics(signals: List[dict]) -> dict:
    traded  = [s for s in signals if s.get("outcome") in ("WIN", "LOSS")]
    today   = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    wins_total = sum(1 for s in traded if s["outcome"] == "WIN")

    ce_sigs = [s for s in traded if s["direction"] == "CE"]
    pe_sigs = [s for s in traded if s["direction"] == "PE"]
    ce_wins = sum(1 for s in ce_sigs if s["outcome"] == "WIN")
    pe_wins = sum(1 for s in pe_sigs if s["outcome"] == "WIN")

    today_sigs = [s for s in traded if s["ts"][:10] == today]
    week_sigs  = [s for s in traded if s["ts"][:10] >= week_ago]

    by_zone: Dict[str, dict] = defaultdict(lambda: {"w": 0, "n": 0})
    by_pat:  Dict[str, dict] = defaultdict(lambda: {"w": 0, "n": 0})
    by_hour: Dict[int, dict] = defaultdict(lambda: {"w": 0, "n": 0})
    by_conf: Dict[str, dict] = defaultdict(lambda: {"w": 0, "n": 0})

    for s in traded:
        z = (s.get("zone") or "UNKNOWN").strip()[:35]
        p = (s.get("pattern") or "NONE").strip()[:25]
        by_zone[z]["n"] += 1
        by_pat[p]["n"]  += 1
        try:
            h = datetime.fromisoformat(s["ts"]).hour
        except ValueError:
            h = 0
        by_hour[h]["n"] += 1
        conf = s.get("confidence")
        if conf is not None:
            bk = ("85-100%" if conf >= 85 else "75-84%" if conf >= 75 else
                  "65-74%" if conf >= 65 else "<65%")
            by_conf[bk]["n"] += 1
        if s["outcome"] == "WIN":
            by_zone[z]["w"] += 1
            by_pat[p]["w"]  += 1
            by_hour[h]["w"] += 1
            if conf is not None:
                by_conf[bk]["w"] += 1

    zone_wr = {z: (_wr(v["w"], v["n"]), v["n"]) for z, v in by_zone.items() if v["n"] >= 2}
    pat_wr  = {p: (_wr(v["w"], v["n"]), v["n"]) for p, v in by_pat.items()  if v["n"] >= 2}
    hour_wr = {h: (_wr(v["w"], v["n"]), v["n"]) for h, v in by_hour.items() if v["n"] >= 2}
    conf_wr = {b: (_wr(v["w"], v["n"]), v["n"]) for b, v in by_conf.items() if v["n"] >= 1}

    prem_sigs = [s for s in signals if s.get("prem_outcome") in ("WIN", "LOSS")]
    prem_wins  = sum(1 for s in prem_sigs if s["prem_outcome"] == "WIN")

    streak = 0; streak_type = "—"
    if traded:
        streak_type = traded[-1]["outcome"]
        for s in reversed(traded):
            if s["outcome"] == streak_type:
                streak += 1
            else:
                break

    return {
        "total": len(traded), "wins": wins_total, "losses": len(traded) - wins_total,
        "overall_wr": _wr(wins_total, len(traded)),
        "today_total": len(today_sigs), "today_wr": _wr(sum(1 for s in today_sigs if s["outcome"] == "WIN"), len(today_sigs)),
        "week_total": len(week_sigs),   "week_wr":  _wr(sum(1 for s in week_sigs  if s["outcome"] == "WIN"), len(week_sigs)),
        "ce_total": len(ce_sigs), "ce_wr": _wr(ce_wins, len(ce_sigs)),
        "pe_total": len(pe_sigs), "pe_wr": _wr(pe_wins, len(pe_sigs)),
        "by_zone": zone_wr, "by_pattern": pat_wr,
        "by_hour": {str(h): v for h, v in hour_wr.items()},
        "by_confidence": conf_wr,
        "streak": streak, "streak_type": streak_type,
        "prem_total": len(prem_sigs), "prem_wr": _wr(prem_wins, len(prem_sigs)),
        "no_data_count": sum(1 for s in signals if s.get("outcome") == "NO_DATA"),
        "scratch_count": sum(1 for s in signals if s.get("outcome") == "SCRATCH"),
    }

# ══════════════════════════════════════════════════════════════
#  AUTO-TUNING ENGINE
#  Identifies what's broken and computes corrective parameters.
#  Returns (new_tuning_dict, list_of_change_descriptions)
# ══════════════════════════════════════════════════════════════
MIN_ZONE_SAMPLES    = 4    # need this many signal to block a zone
MIN_PATTERN_SAMPLES = 4
BAD_ZONE_WR         = 35   # block zone if win-rate below this %
BAD_PATTERN_WR      = 35
CE_PENALTY_BELOW    = 45   # apply CE multiplier if CE WR below this
PE_PENALTY_BELOW    = 45
HIGH_CONF_THRESHOLD = 72   # raise threshold if <65% signals have bad WR
GOOD_CONF_THRESHOLD = 65   # don't lower below this
MAX_THRESHOLD       = 80   # never raise above this

def compute_tuning(metrics: dict) -> Tuple[dict, List[str]]:
    """
    Analyze metrics and produce BOT_TUNING.json content + human-readable change list.
    """
    changes: List[str] = []
    tuning: dict = {
        "generated_at": datetime.now().isoformat(),
        "generated_by": "SIGNAL_ANALYZER",
        "confidence_threshold": GOOD_CONF_THRESHOLD,
        "excluded_zones":    [],
        "excluded_patterns": [],
        "ce_multiplier":     1.0,
        "pe_multiplier":     1.0,
        "notes": [],
    }

    total = metrics["total"]
    if total < 5:
        changes.append(f"Insufficient data ({total} signals). Keeping defaults — need 5+ evaluated signals.")
        tuning["notes"].append("Insufficient data for tuning.")
        return tuning, changes

    # ── 1. Confidence threshold ──────────────────────────────
    overall_wr = metrics["overall_wr"]
    cur_threshold = GOOD_CONF_THRESHOLD
    conf_wr = metrics.get("by_confidence", {})

    # If low-confidence signals (65-74%) have bad WR → raise threshold
    low_conf = conf_wr.get("65-74%")
    if low_conf and low_conf[0] < 45 and low_conf[1] >= 3:
        new_thr = 75
        tuning["confidence_threshold"] = new_thr
        changes.append(f"RAISE threshold  {cur_threshold}% → {new_thr}%  "
                       f"(65–74% signals only {low_conf[0]}% accurate, n={low_conf[1]})")
        cur_threshold = new_thr
    elif overall_wr >= 65 and total >= 10:
        # Good performance — allow lower threshold to catch more signals
        new_thr = GOOD_CONF_THRESHOLD
        tuning["confidence_threshold"] = new_thr
        if cur_threshold > GOOD_CONF_THRESHOLD:
            changes.append(f"LOWER threshold  {cur_threshold}% → {new_thr}%  "
                           f"(overall WR {overall_wr}% is strong)")

    # ── 2. Zone exclusions ───────────────────────────────────
    by_zone = metrics.get("by_zone", {})
    blocked_zones = []
    for zone, (wr_val, n) in sorted(by_zone.items(), key=lambda x: x[1][0]):
        if n >= MIN_ZONE_SAMPLES and wr_val <= BAD_ZONE_WR:
            blocked_zones.append(zone)
            changes.append(f"BLOCK zone  '{zone}'  "
                           f"({wr_val}% win-rate, n={n}) — signals here consistently fail")
    tuning["excluded_zones"] = blocked_zones

    # ── 3. Pattern exclusions ────────────────────────────────
    by_pat = metrics.get("by_pattern", {})
    blocked_pats = []
    for pat, (wr_val, n) in sorted(by_pat.items(), key=lambda x: x[1][0]):
        if n >= MIN_PATTERN_SAMPLES and wr_val <= BAD_PATTERN_WR and pat not in ("NONE", "Normal", "Doji"):
            blocked_pats.append(pat)
            changes.append(f"BLOCK pattern  '{pat}'  "
                           f"({wr_val}% win-rate, n={n}) — this pattern is misleading")
    tuning["excluded_patterns"] = blocked_pats

    # ── 4. CE / PE directional multipliers ───────────────────
    ce_wr = metrics["ce_wr"]; ce_n = metrics["ce_total"]
    pe_wr = metrics["pe_wr"]; pe_n = metrics["pe_total"]

    if ce_n >= 5 and ce_wr < CE_PENALTY_BELOW:
        # CE is underperforming — reduce its effective confidence
        # Multiplier calculated so that current average CE confidence × mult < threshold
        mult = round(max(0.65, ce_wr / 65), 2)
        tuning["ce_multiplier"] = mult
        changes.append(f"DAMPEN CE  confidence ×{mult}  "
                       f"(CE win-rate {ce_wr}% < {CE_PENALTY_BELOW}%, n={ce_n})")
    else:
        tuning["ce_multiplier"] = 1.0

    if pe_n >= 5 and pe_wr < PE_PENALTY_BELOW:
        mult = round(max(0.65, pe_wr / 65), 2)
        tuning["pe_multiplier"] = mult
        changes.append(f"DAMPEN PE  confidence ×{mult}  "
                       f"(PE win-rate {pe_wr}% < {PE_PENALTY_BELOW}%, n={pe_n})")
    else:
        tuning["pe_multiplier"] = 1.0

    # ── 5. Good-zone boosts (informational only, noted) ──────
    best_zones = [(z, wr, n) for z, (wr, n) in by_zone.items() if wr >= 70 and n >= 3]
    if best_zones:
        best_zones.sort(key=lambda x: -x[1])
        names = ", ".join(f"'{z}' ({wr}%)" for z, wr, _ in best_zones[:3])
        changes.append(f"RELIABLE zones  {names} — signals here are working well")

    # ── Build notes for JSON ──────────────────────────────────
    tuning["notes"] = changes[:]

    if not [c for c in changes if c.startswith(("RAISE", "LOWER", "BLOCK", "DAMPEN"))]:
        changes.append("No parameter changes needed — system is well-calibrated.")

    return tuning, changes


def apply_tuning(tuning: dict) -> None:
    with open(TUNING_PATH, "w", encoding="utf-8") as f:
        json.dump(tuning, f, indent=2)


def load_existing_tuning() -> Optional[dict]:
    try:
        with open(TUNING_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ══════════════════════════════════════════════════════════════
#  DASHBOARD
# ══════════════════════════════════════════════════════════════
def render(health: List[dict], metrics: dict,
           tuning: dict, changes: List[str],
           signals: List[dict], prev_tuning: Optional[dict]) -> None:
    os.system("clear")
    now = datetime.now()

    print(hdiv())
    title = "SIGNAL ANALYZER  —  SELF-ANALYSIS & AUTO-TUNING"
    print(_cc("HEADER") + " " * ((WIDTH - len(title)) // 2) + title + C.RESET)
    sub = f"{now.strftime('%Y-%m-%d  %H:%M:%S')}   read-only  |  no API calls"
    print(_cc("DIM_TEXT") + " " * ((WIDTH - len(sub)) // 2) + sub + C.RESET)
    print(hdiv())

    # ── BOT HEALTH ──────────────────────────────────────────
    print(sdiv("BOT HEALTH"))
    for b in health:
        icon, col = (("●", "HEALTH_OK") if b["status"] == "RUNNING" else
                     ("◐", "HEALTH_WARN") if b["status"] == "STALE" else
                     ("○", "HEALTH_BAD"))
        age_str = (f"last seen {b['last_seen']}  ({b['age_s']//60}m ago)"
                   if b["last_seen"] else "no log files found")
        print(f"  {_cc(col)}{icon} {rpad(b['name'],16)}  "
              f"{rpad(b['status'],8)}{C.RESET}  {_cc('DIM_TEXT')}{age_str}{C.RESET}")
    print()

    # ── SIGNAL ACCURACY ─────────────────────────────────────
    print(sdiv("SIGNAL ACCURACY  (T+10–25 min spot evaluation)"))
    total = metrics["total"]
    if total == 0:
        print(f"  {_cc('DIM_TEXT')}No evaluated signals yet. "
              f"Run MASTER_SIGNAL_BOT for a session first.{C.RESET}")
    else:
        def acc_row(label, wr, n, extra=""):
            b = bar(wr / 100, 14)
            print(f"  {rpad(label,20)}  {b}  {pct_col(wr)}  "
                  f"{_cc('DIM_TEXT')}({n} signals){extra}{C.RESET}")

        acc_row("Overall (7-day)", metrics["overall_wr"], metrics["total"])
        acc_row("Today",           metrics["today_wr"],   metrics["today_total"])
        acc_row("CE signals",      metrics["ce_wr"],      metrics["ce_total"])
        acc_row("PE signals",      metrics["pe_wr"],      metrics["pe_total"])
        if metrics["prem_total"] >= 3:
            acc_row("Premium corr.", metrics["prem_wr"], metrics["prem_total"])

        # streak
        st = metrics["streak"]; stt = metrics["streak_type"]
        col = "WIN" if stt == "WIN" else "LOSS" if stt == "LOSS" else "NEUTRAL"
        sc  = metrics.get("scratch_count", 0); nd = metrics.get("no_data_count", 0)
        print(f"  Current streak : {_cc(col)}{st} × {stt}{C.RESET}  "
              f"{_cc('DIM_TEXT')}| scratch={sc}  no-data={nd}{C.RESET}")
    print()

    # ── BY ZONE ─────────────────────────────────────────────
    by_zone = metrics.get("by_zone", {})
    if by_zone:
        print(sdiv("WIN-RATE BY FIBONACCI ZONE"))
        blocked = tuning.get("excluded_zones", [])
        for zone, (wr_val, n) in sorted(by_zone.items(), key=lambda x: -x[1][0])[:8]:
            is_blocked = any(b.lower() in zone.lower() for b in blocked)
            tag = f"  {_cc('LOSS')}[BLOCKED]{C.RESET}" if is_blocked else ""
            print(f"  {rpad(zone,35)}  {bar(wr_val/100,10)}  {pct_col(wr_val)}"
                  f"  {_cc('DIM_TEXT')}n={n}{C.RESET}{tag}")
        print()

    # ── BY PATTERN ──────────────────────────────────────────
    by_pat = metrics.get("by_pattern", {})
    if by_pat:
        print(sdiv("WIN-RATE BY CANDLE PATTERN"))
        blocked_p = tuning.get("excluded_patterns", [])
        for pat, (wr_val, n) in sorted(by_pat.items(), key=lambda x: -x[1][0])[:6]:
            is_blocked = any(b.lower() in pat.lower() for b in blocked_p)
            tag = f"  {_cc('LOSS')}[BLOCKED]{C.RESET}" if is_blocked else ""
            print(f"  {rpad(pat,25)}  {bar(wr_val/100,10)}  {pct_col(wr_val)}"
                  f"  {_cc('DIM_TEXT')}n={n}{C.RESET}{tag}")
        print()

    # ── BY CONFIDENCE ────────────────────────────────────────
    conf_wr = metrics.get("by_confidence", {})
    if conf_wr:
        print(sdiv("WIN-RATE BY CONFIDENCE LEVEL"))
        for bk in ["85-100%", "75-84%", "65-74%", "<65%"]:
            if bk not in conf_wr:
                continue
            wr_val, n = conf_wr[bk]
            print(f"  {rpad(bk,12)}  {bar(wr_val/100,10)}  {pct_col(wr_val)}"
                  f"  {_cc('DIM_TEXT')}n={n}{C.RESET}")
        print()

    # ── AUTO-TUNING CHANGES ─────────────────────────────────
    print(sdiv("AUTO-TUNING  —  CHANGES APPLIED TO BOT_TUNING.json"))

    actionable = [c for c in changes if any(c.startswith(w)
                  for w in ("RAISE", "LOWER", "BLOCK", "DAMPEN"))]
    informational = [c for c in changes if c not in actionable]

    if actionable:
        for c in actionable:
            if c.startswith("RAISE") or c.startswith("BLOCK") or c.startswith("DAMPEN"):
                col = "CHANGE_REM"
            elif c.startswith("LOWER"):
                col = "CHANGE_ADD"
            else:
                col = "CHANGE_MOD"
            print(f"  {_cc(col)}▶ {c}{C.RESET}")
    else:
        print(f"  {_cc('WIN')}✓ System is well-calibrated — no corrections needed.{C.RESET}")

    if informational:
        print()
        for c in informational:
            print(f"  {_cc('DIM_TEXT')}· {c}{C.RESET}")
    print()

    # ── CURRENT TUNING STATE ────────────────────────────────
    print(sdiv("ACTIVE TUNING PARAMETERS  (written to BOT_TUNING.json)"))
    thr  = tuning.get("confidence_threshold", 65)
    ce_m = tuning.get("ce_multiplier", 1.0)
    pe_m = tuning.get("pe_multiplier", 1.0)
    excl_z = tuning.get("excluded_zones", [])
    excl_p = tuning.get("excluded_patterns", [])

    print(f"  {_cc('SECTION_HDR')}Confidence threshold :{C.RESET}  {_cc('SPOT_VAL')}{thr}%{C.RESET}")
    print(f"  {_cc('SECTION_HDR')}CE multiplier        :{C.RESET}  "
          f"{_cc('ACTION_CE') if ce_m == 1.0 else _cc('LOSS')}×{ce_m:.2f}{C.RESET}")
    print(f"  {_cc('SECTION_HDR')}PE multiplier        :{C.RESET}  "
          f"{_cc('ACTION_PE') if pe_m == 1.0 else _cc('LOSS')}×{pe_m:.2f}{C.RESET}")
    print(f"  {_cc('SECTION_HDR')}Blocked zones        :{C.RESET}  "
          f"{_cc('LOSS')}{len(excl_z)} zone(s){C.RESET}  "
          f"{_cc('DIM_TEXT')}{', '.join(excl_z[:2])}{'...' if len(excl_z) > 2 else ''}{C.RESET}")
    print(f"  {_cc('SECTION_HDR')}Blocked patterns     :{C.RESET}  "
          f"{_cc('LOSS')}{len(excl_p)} pattern(s){C.RESET}  "
          f"{_cc('DIM_TEXT')}{', '.join(excl_p)}{C.RESET}")
    print()

    # ── RECENT SIGNALS ───────────────────────────────────────
    recent = [s for s in signals[-12:] if s.get("outcome") not in (None, "SKIP")]
    if recent:
        print(sdiv("LAST 12 SIGNALS"))
        print(_cc("DIM_TEXT") +
              f"  {'Time':8}  {'Dir':4}  {'Spot':9}  {'Zone':28}  {'Move':7}  Outcome"
              + C.RESET)
        for s in recent:
            ts   = s["ts"][11:16] if len(s["ts"]) >= 16 else s["ts"][:8]
            d    = s.get("direction", "?")
            spt  = f"₹{s.get('spot',0):,.0f}"
            zone = (s.get("zone") or "—")[:28]
            mv   = s.get("move_pts")
            mv_s = f"{mv:+.0f}pt" if mv is not None else "  —"
            oc   = s.get("outcome", "—")
            dcol = "ACTION_CE" if d == "CE" else "ACTION_PE" if d == "PE" else "ACTION_WAIT"
            ocol = "WIN" if oc == "WIN" else "LOSS" if oc == "LOSS" else "NEUTRAL"
            oc_s = "✓WIN" if oc == "WIN" else "✗LOSS" if oc == "LOSS" else "~SCR" if oc == "SCRATCH" else "?N/D"
            print(f"  {_cc('DIM_TEXT')}{ts}{C.RESET}  "
                  f"{_cc(dcol)}{rpad(d,4)}{C.RESET}  "
                  f"{_cc('SPOT_VAL')}{rpad(spt,9)}{C.RESET}  "
                  f"{_cc('DIM_TEXT')}{rpad(zone,28)}{C.RESET}  "
                  f"{_cc('DIM_TEXT')}{rpad(mv_s,7)}{C.RESET}  "
                  f"{_cc(ocol)}{oc_s}{C.RESET}")
        print()

    print(hdiv())
    tpath = os.path.basename(TUNING_PATH)
    foot  = f"Corrections saved → {tpath}   |   MASTER_SIGNAL_BOT picks them up automatically"
    print(_cc("GOLDEN_ZONE") + " " * max(0, (WIDTH - len(foot)) // 2) + foot + C.RESET)
    print(hdiv())


# ══════════════════════════════════════════════════════════════
#  SAVE REPORT
# ══════════════════════════════════════════════════════════════
def save_json_report(metrics: dict, health: List[dict],
                     tuning: dict, changes: List[str],
                     signals: List[dict]) -> str:
    report_dir = os.path.join(PROJECT_ROOT, "logs", "analysis")
    os.makedirs(report_dir, exist_ok=True)
    fname = f"Signal_Analysis_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    fpath = os.path.join(report_dir, fname)
    recent = [s for s in signals[-20:] if s.get("outcome") not in (None,)]
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now().isoformat(),
                   "metrics": metrics, "bot_health": health,
                   "tuning_applied": tuning, "changes": changes,
                   "recent_signals": recent}, f, indent=2, default=str)
    return fpath


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
def run() -> None:
    select_theme()
    print(f"\n{_cc('HEADER')}Scanning logs...{C.RESET}")

    health       = check_bot_health()
    master_sigs  = parse_master_signal_logs(days_back=7)
    fibo_sigs    = parse_fibo_logs(days_back=7)
    pdt_ticks    = parse_pdt_logs(days_back=7)

    # merge: prefer master (richer fields), fill in from fibo where no master record
    master_ts = {s["ts"][:15] for s in master_sigs}
    all_sigs  = master_sigs + [s for s in fibo_sigs if s["ts"][:15] not in master_ts]
    all_sigs.sort(key=lambda r: r["ts"])

    all_sigs = evaluate_outcomes(all_sigs)
    all_sigs = evaluate_premium_correlation(all_sigs, pdt_ticks)

    metrics = aggregate_metrics(all_sigs)
    tuning, changes = compute_tuning(metrics)

    prev_tuning = load_existing_tuning()
    apply_tuning(tuning)

    save_json_report(metrics, health, tuning, changes, all_sigs)
    render(health, metrics, tuning, changes, all_sigs, prev_tuning)


def main() -> None:
    while True:
        try:
            run()
        except KeyboardInterrupt:
            print(f"\n{C.DIM}Exiting Signal Analyzer.{C.RESET}")
            break
        except Exception as e:
            print(f"\n{C.DIM}Error during analysis: {e}{C.RESET}")

        print(f"\n{_cc('DIM_TEXT')}Press Enter to re-run analysis, or Ctrl-C to exit.{C.RESET}", end="")
        try:
            input()
        except KeyboardInterrupt:
            print(f"\n{C.DIM}Exiting.{C.RESET}")
            break


if __name__ == "__main__":
    main()
