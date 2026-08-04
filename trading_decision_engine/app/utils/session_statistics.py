"""SessionStatistics: cumulative engine-health analytics for one trading session,
aggregated purely from the decision-transparency stream (decision_cycle / trade_closed
diagnostics events). Answers the calibration questions the per-tick view can't:
which gate rejects most, whether an engine filters too aggressively (pass %, average
score vs its threshold), what premium velocity actually looks like today, and whether
high entry scores correlate with winning trades.

Same architecture rule as the dashboard: a pure observer. Consumes diagnostics dicts,
never touches engines or decisions, and a crash here can never affect trading (the
Orchestrator swallows observer exceptions).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

# Entry-score buckets for the trade-performance correlation table.
SCORE_BUCKETS = ((80.0, ">80"), (60.0, "60-80"), (0.0, "<60"))


def _bucket(score: float) -> str:
    for floor, label in SCORE_BUCKETS:
        if score > floor or floor == 0.0:
            return label
    return "<60"


class _EngineStats:
    __slots__ = ("passes", "fails", "score_sum", "confidence_sum", "samples")

    def __init__(self) -> None:
        self.passes = 0
        self.fails = 0
        self.score_sum = 0.0
        self.confidence_sum = 0.0
        self.samples = 0

    def record(self, info: dict) -> None:
        self.samples += 1
        self.score_sum += info.get("score", 0.0)
        self.confidence_sum += info.get("confidence", 0.0)
        if info.get("passed"):
            self.passes += 1
        else:
            self.fails += 1

    def to_dict(self) -> dict:
        n = max(1, self.samples)
        judged = self.passes + self.fails
        return {
            "samples": self.samples,
            "pass_pct": round(self.passes / judged * 100.0, 1) if judged else 0.0,
            "fail_pct": round(self.fails / judged * 100.0, 1) if judged else 0.0,
            "avg_score": round(self.score_sum / n, 1),
            "avg_confidence": round(self.confidence_sum / n, 1),
        }


class SessionStatistics:
    """update() is an on_diagnostics observer; feed it every diagnostics event."""

    def __init__(self) -> None:
        self.decision_cycles = 0
        self.monitoring_ticks = 0
        self.actions = Counter()                      # BUY / SELL / HOLD / REJECT
        self.rejection_gate_counts = Counter()        # failed gate -> count
        self.engines: dict[str, _EngineStats] = defaultdict(_EngineStats)
        # Premium-momentum session extremes (velocity in pts/sec, absolute for "highest").
        self.velocity_sum = 0.0
        self.velocity_samples = 0
        self.velocity_max: float | None = None
        self.velocity_min: float | None = None
        # Trade performance: per engine, per entry-score bucket -> [wins, losses]
        self.trades_closed = 0
        self.wins = 0
        self.total_pnl = 0.0
        self.win_by_entry_score: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    # ------------------------------------------------------------------ ingest
    def update(self, diag: dict) -> None:
        kind = diag.get("type")
        if kind == "decision_cycle":
            self._record_cycle(diag)
        elif kind == "trade_panel":
            self.monitoring_ticks += 1
        elif kind == "trade_closed":
            self._record_trade(diag)

    def _record_cycle(self, diag: dict) -> None:
        self.decision_cycles += 1
        self.actions[diag.get("action", "?")] += 1
        for gate in diag.get("stage1", {}).get("failed_checks", []):
            self.rejection_gate_counts[gate] += 1
        for name, info in diag.get("engines", {}).items():
            self.engines[name].record(info)
        velocity = diag.get("engines", {}).get("premium_momentum", {}).get("velocity")
        if velocity is not None:
            self.velocity_sum += velocity
            self.velocity_samples += 1
            if self.velocity_max is None or velocity > self.velocity_max:
                self.velocity_max = velocity
            if self.velocity_min is None or velocity < self.velocity_min:
                self.velocity_min = velocity

    def _record_trade(self, diag: dict) -> None:
        self.trades_closed += 1
        pnl = diag.get("pnl", 0.0)
        self.total_pnl += pnl
        won = pnl >= 0
        self.wins += won
        for engine, score in diag.get("entry_scores", {}).items():
            counts = self.win_by_entry_score[engine][_bucket(score)]
            counts[0 if won else 1] += 1

    # ------------------------------------------------------------------ report
    def to_dict(self) -> dict:
        total_rejections = sum(self.rejection_gate_counts.values())
        rejection_pct = {
            gate: round(count / total_rejections * 100.0, 1)
            for gate, count in self.rejection_gate_counts.most_common()
        } if total_rejections else {}

        win_rates = {}
        for engine, buckets in self.win_by_entry_score.items():
            win_rates[engine] = {
                label: {
                    "trades": w + l,
                    "win_rate_pct": round(w / (w + l) * 100.0, 1) if (w + l) else 0.0,
                }
                for label, (w, l) in buckets.items()
            }

        return {
            "type": "session_stats",
            "decision_cycles": self.decision_cycles,
            "monitoring_ticks": self.monitoring_ticks,
            "actions": dict(self.actions),
            "rejection_reasons_pct": rejection_pct,
            "rejection_reasons_count": dict(self.rejection_gate_counts),
            "engines": {name: stats.to_dict() for name, stats in sorted(self.engines.items())},
            "premium_momentum": {
                "avg_velocity": round(self.velocity_sum / self.velocity_samples, 3) if self.velocity_samples else 0.0,
                "max_velocity": round(self.velocity_max, 3) if self.velocity_max is not None else None,
                "min_velocity": round(self.velocity_min, 3) if self.velocity_min is not None else None,
            },
            "trades": {
                "closed": self.trades_closed,
                "wins": self.wins,
                "losses": self.trades_closed - self.wins,
                "win_rate_pct": round(self.wins / self.trades_closed * 100.0, 1) if self.trades_closed else 0.0,
                "total_pnl": round(self.total_pnl, 2),
                "win_rate_by_entry_score": win_rates,
            },
        }

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def render_session_stats(stats: dict) -> str:
    """Full statistics panel (end of session, or on demand)."""
    lines = ["=" * 78, " SESSION STATISTICS", "=" * 78]
    actions = stats["actions"]
    lines.append(
        f"  decision cycles {stats['decision_cycles']:,}   monitoring ticks {stats['monitoring_ticks']:,}"
        f"   BUY {actions.get('BUY', 0)}   SELL {actions.get('SELL', 0)}"
        f"   HOLD {actions.get('HOLD', 0)}   REJECT {actions.get('REJECT', 0):,}"
    )

    if stats["rejection_reasons_pct"]:
        lines.append("-" * 78)
        lines.append("  TOP REJECTION REASONS")
        for gate, pct in stats["rejection_reasons_pct"].items():
            count = stats["rejection_reasons_count"][gate]
            lines.append(f"    {gate.replace('_', ' '):<20} {pct:5.1f}%  ({count:,} cycles)")

    lines.append("-" * 78)
    lines.append(f"  {'ENGINE':<20} {'PASS%':>6} {'FAIL%':>6} {'AVG SCORE':>10} {'AVG CONF':>9}")
    for name, e in stats["engines"].items():
        lines.append(f"  {name.replace('_', ' '):<20} {e['pass_pct']:>5.1f}% {e['fail_pct']:>5.1f}% {e['avg_score']:>10.1f} {e['avg_confidence']:>8.1f}%")

    pm = stats["premium_momentum"]
    lines.append("-" * 78)
    lines.append(
        f"  premium velocity: avg {pm['avg_velocity']:+.3f}/s"
        + (f"   max {pm['max_velocity']:+.3f}" if pm["max_velocity"] is not None else "")
        + (f"   min {pm['min_velocity']:+.3f}" if pm["min_velocity"] is not None else "")
    )

    trades = stats["trades"]
    lines.append("-" * 78)
    lines.append(
        f"  TRADES: {trades['closed']} closed   {trades['wins']}W/{trades['losses']}L"
        f"   win rate {trades['win_rate_pct']:.0f}%   net P&L ₹{trades['total_pnl']:+,.2f}"
    )
    if trades["win_rate_by_entry_score"]:
        lines.append("  WIN RATE BY ENTRY SCORE")
        for engine, buckets in trades["win_rate_by_entry_score"].items():
            parts = [f"{label}: {b['win_rate_pct']:.0f}% ({b['trades']})" for label, b in sorted(buckets.items(), reverse=True)]
            lines.append(f"    {engine.replace('_', ' '):<20} " + "   ".join(parts))
    lines.append("=" * 78)
    return "\n".join(lines)


def render_stats_strip(stats: dict) -> str:
    """One-line summary embedded at the bottom of the live dashboard."""
    actions = stats["actions"]
    top_reject = next(iter(stats["rejection_reasons_pct"].items()), None)
    trades = stats["trades"]
    strip = (
        f"  session: {stats['decision_cycles']:,} cycles   BUY {actions.get('BUY', 0)}  SELL {actions.get('SELL', 0)}"
        f"  REJECT {actions.get('REJECT', 0):,}   trades {trades['closed']} ({trades['win_rate_pct']:.0f}% win, ₹{trades['total_pnl']:+,.0f})"
    )
    if top_reject:
        strip += f"   top block: {top_reject[0].replace('_', ' ')} {top_reject[1]:.0f}%"
    return strip
