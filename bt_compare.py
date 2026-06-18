#!/usr/bin/env python3
"""
bt_compare.py  —  Run all experiment combinations across multiple day ranges.

Usage:
    python3 bt_compare.py --expiry 2026-06-23 --days 30 50 100 150 180

Each range × 16 combos (no_break × max_entry_risk × max_daily_sl × spot_confirm).
Cross-range summary shows which config wins most often.
Note: Groww API only has ~6-8 weeks of data for a given weekly expiry contract.
      Longer ranges will plateau at the same trade count — that is expected.
"""
import subprocess, sys, json, os, argparse, itertools, tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
BT   = os.path.join(BASE, "TRENDLINE_BACKTEST.py")

# ── Experiment dimensions ─────────────────────────────────────────────────────
EXPERIMENTS = {
    "no_break":       [False, True],
    "max_entry_risk": [0.0,   7.0],
    "max_daily_sl":   [0,     2],
    "spot_confirm":   [False, True],
}

COMBO_KEYS  = list(EXPERIMENTS.keys())
ALL_COMBOS  = list(itertools.product(*[EXPERIMENTS[k] for k in COMBO_KEYS]))


def combo_label(vals) -> str:
    nb  = "Y" if vals[0] else "N"
    rc  = str(vals[1]) if vals[1] else "N"
    ds  = str(vals[2]) if vals[2] else "N"
    sc  = "Y" if vals[3] else "N"
    return f"nb={nb} rc={rc} ds={ds} sc={sc}"


def run_one(expiry, days, premium_min, premium_max, lots, flags: dict) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        out = tf.name
    cmd = [
        sys.executable, BT,
        "--expiry",      expiry,
        "--days",        str(days),
        "--premium_min", str(premium_min),
        "--premium_max", str(premium_max),
        "--lots",        str(lots),
        "--out",         out,
    ]
    if flags.get("no_break"):
        cmd.append("--no_break")
    if flags.get("max_entry_risk", 0) > 0:
        cmd += ["--max_entry_risk", str(flags["max_entry_risk"])]
    if flags.get("max_daily_sl", 0) > 0:
        cmd += ["--max_daily_sl", str(flags["max_daily_sl"])]
    if flags.get("spot_confirm"):
        cmd.append("--spot_confirm")
    try:
        subprocess.run(cmd, cwd=BASE, timeout=360, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "pnl": -999999}
    except Exception as e:
        return {"error": str(e), "pnl": -999999}
    trades = []
    if os.path.exists(out):
        with open(out) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
        os.unlink(out)
    if not trades:
        return {"trades": 0, "wins": 0, "losses": 0, "pnl": 0, "wr": 0,
                "avg_win": 0, "avg_loss": 0, "sl_hits": 0}
    pnl    = sum(t["pnl"] for t in trades)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    sl_h   = [t for t in trades if t.get("exit_reason", "") == "SL"]
    wr     = round(100 * len(wins) / len(trades))
    avgw   = round(sum(t["pnl"] for t in wins)   / len(wins),   0) if wins   else 0
    avgl   = round(sum(t["pnl"] for t in losses) / len(losses), 0) if losses else 0
    return {"trades": len(trades), "wins": len(wins), "losses": len(losses),
            "pnl": round(pnl, 0), "wr": wr, "avg_win": avgw, "avg_loss": avgl,
            "sl_hits": len(sl_h)}


def print_range_table(days, results_for_range):
    """Print sorted table for one day range."""
    ranked = sorted(results_for_range, key=lambda x: x["pnl"], reverse=True)
    base   = next((r for r in results_for_range
                   if not r["no_break"] and r["max_entry_risk"] == 0
                   and r["max_daily_sl"] == 0), None)
    base_pnl = base["pnl"] if base else 0

    print(f"\n  {'─'*95}")
    print(f"  DAY RANGE: last {days} days   (baseline = Rs{base_pnl:+,.0f})")
    print(f"  {'─'*95}")
    hdr = f"  {'Rank':<5} {'nb':<4} {'rc':<6} {'ds':<4} {'sc':<4} {'Trades':<8} {'W/L':<9} "
    hdr += f"{'WR%':<6} {'P&L':>12} {'vs base':>10} {'SL_hits':<8}"
    print(hdr)
    print(f"  {'-'*100}")
    for rank, r in enumerate(ranked, 1):
        delta   = r["pnl"] - base_pnl
        delta_s = ("+" if delta >= 0 else "") + f"{delta:,.0f}"
        pnl_s   = ("+" if r["pnl"] >= 0 else "") + f"{r['pnl']:,.0f}"
        wl_s    = str(r["wins"]) + "W/" + str(r["losses"]) + "L"
        star    = " ★" if rank == 1 else "  "
        print(f"  #{rank:<4} {('Y' if r['no_break'] else 'N'):<4} "
              f"{(str(r['max_entry_risk']) if r['max_entry_risk'] else 'N'):<6} "
              f"{(str(r['max_daily_sl']) if r['max_daily_sl'] else 'N'):<4} "
              f"{('Y' if r.get('spot_confirm') else 'N'):<4} "
              f"{r['trades']:<8} {wl_s:<9} {r['wr']:<6} "
              f"{'Rs'+pnl_s:>12} {'Rs'+delta_s:>10} {r['sl_hits']:<8}{star}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expiry",      default="2026-06-23")
    ap.add_argument("--days",        nargs="+", type=int, default=[30, 50, 100, 150, 180])
    ap.add_argument("--premium_min", type=float, default=85)
    ap.add_argument("--premium_max", type=float, default=200)
    ap.add_argument("--lots",        type=int,   default=18)
    args = ap.parse_args()

    total_runs = len(args.days) * len(ALL_COMBOS)
    print(f"\n{'='*95}")
    print(f"  MULTI-RANGE BACKTEST EXPERIMENT")
    print(f"  Expiry: {args.expiry}  Lots: {args.lots}  "
          f"Premium: Rs{args.premium_min}–Rs{args.premium_max}")
    print(f"  Day ranges: {args.days}")
    print(f"  {total_runs} total runs  ({datetime.now().strftime('%H:%M:%S')})")
    print(f"{'='*95}")

    # ── Run all combinations for all ranges ──────────────────────────────────
    all_results = {}   # days -> list of result dicts
    run_num = 0
    for days in args.days:
        all_results[days] = []
        for vals in ALL_COMBOS:
            run_num += 1
            flags = dict(zip(COMBO_KEYS, vals))
            label = combo_label(vals)
            print(f"  [{run_num:02d}/{total_runs}] days={days:<4} {label}  ...", end="", flush=True)
            r = run_one(args.expiry, days, args.premium_min, args.premium_max,
                        args.lots, flags)
            r.update(flags)   # store flags in result for cross-range analysis
            all_results[days].append(r)
            if "error" in r:
                print(f"  ERROR: {r['error']}")
            else:
                pnl_s = ("+" if r["pnl"] >= 0 else "") + f"{r['pnl']:,.0f}"
                print(f"  Rs{pnl_s}  WR {r['wr']}%  ({r['trades']} trades)")

    # ── Per-range sorted tables ───────────────────────────────────────────────
    print(f"\n\n{'='*95}")
    print(f"  PER-RANGE RESULTS  (nb=no_break  rc=risk_cap_pts  ds=daily_sl_limit  sc=spot_confirm)")
    for days in args.days:
        print_range_table(days, all_results[days])

    # ── Cross-range summary: winner config per range ──────────────────────────
    print(f"\n\n{'='*95}")
    print(f"  CROSS-RANGE SUMMARY  — winner per day range")
    print(f"  {'─'*93}")
    hdr = f"  {'Days':<8} {'Winner config':<28} {'Trades':<8} {'WR%':<6} "
    hdr += f"{'P&L':>12} {'vs base':>10} {'SL_hits':<8}"
    print(hdr)
    print(f"  {'-'*95}")
    for days in args.days:
        ranked = sorted(all_results[days], key=lambda x: x["pnl"], reverse=True)
        base   = next((r for r in all_results[days]
                       if not r["no_break"] and r["max_entry_risk"] == 0
                       and r["max_daily_sl"] == 0 and not r.get("spot_confirm")), None)
        base_pnl = base["pnl"] if base else 0
        w = ranked[0]
        label = combo_label((w["no_break"], w["max_entry_risk"],
                             w["max_daily_sl"], w.get("spot_confirm", False)))
        delta  = w["pnl"] - base_pnl
        pnl_s  = ("+" if w["pnl"] >= 0 else "") + f"{w['pnl']:,.0f}"
        dlt_s  = ("+" if delta >= 0 else "") + f"{delta:,.0f}"
        star   = "  ← consistent" if (w["no_break"] and w["max_entry_risk"] > 0) else ""
        print(f"  {days:<8} {label:<28} {w['trades']:<8} {w['wr']:<6} "
              f"{'Rs'+pnl_s:>12} {'Rs'+dlt_s:>10} {w['sl_hits']:<8}{star}")

    # ── Consistency check: how does the previously-best config (nb=Y rc=7.0 ds=N sc=N) hold?
    print(f"\n\n  CONFIG CONSISTENCY CHECK  — nb=Y rc=7.0 ds=N sc=N  (old best config)")
    print(f"  {'─'*70}")
    hdr2 = f"  {'Days':<8} {'Trades':<8} {'W/L':<9} {'WR%':<6} {'P&L':>12} {'SL_hits':<8}"
    print(hdr2)
    print(f"  {'-'*68}")
    for days in args.days:
        r = next((x for x in all_results[days]
                  if x["no_break"] and x["max_entry_risk"] == 7.0
                  and x["max_daily_sl"] == 0 and not x.get("spot_confirm")), None)
        if r:
            wl_s  = str(r["wins"]) + "W/" + str(r["losses"]) + "L"
            pnl_s = ("+" if r["pnl"] >= 0 else "") + f"{r['pnl']:,.0f}"
            print(f"  {days:<8} {r['trades']:<8} {wl_s:<9} {r['wr']:<6} "
                  f"{'Rs'+pnl_s:>12} {r['sl_hits']:<8}")

    # ── Show top-3 configs by P&L (across all day ranges combined) ───────────
    from collections import defaultdict
    config_totals: dict = defaultdict(lambda: {"pnl": 0, "count": 0, "trades": 0, "wins": 0})
    for days in args.days:
        for r in all_results[days]:
            key = combo_label((r["no_break"], r["max_entry_risk"],
                               r["max_daily_sl"], r.get("spot_confirm", False)))
            config_totals[key]["pnl"]    += r["pnl"]
            config_totals[key]["count"]  += 1
            config_totals[key]["trades"] += r["trades"]
            config_totals[key]["wins"]   += r["wins"]
    ranked_global = sorted(config_totals.items(), key=lambda x: x[1]["pnl"], reverse=True)
    print(f"\n\n  TOP-5 CONFIGS  — summed P&L across all day ranges tested")
    print(f"  {'─'*80}")
    print(f"  {'Config':<28} {'Total P&L':>14} {'Avg/range':>12} {'Trades':>8} {'WR%':>6}")
    print(f"  {'-'*78}")
    for cfg_label, v in ranked_global[:5]:
        avg = v["pnl"] / v["count"] if v["count"] else 0
        wr  = round(100 * v["wins"] / v["trades"]) if v["trades"] else 0
        pnl_s = ("+" if v["pnl"] >= 0 else "") + f"{v['pnl']:,.0f}"
        avg_s = ("+" if avg >= 0 else "") + f"{avg:,.0f}"
        print(f"  {cfg_label:<28} {'Rs'+pnl_s:>14} {'Rs'+avg_s:>12} {v['trades']:>8} {wr:>6}%")

    print(f"\n{'='*95}")
    print(f"  Done.  {total_runs} runs completed.  ({datetime.now().strftime('%H:%M:%S')})")
    print()


if __name__ == "__main__":
    main()
