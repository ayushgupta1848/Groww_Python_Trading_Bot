"""DecisionComparator: matches bot decisions against manually-recorded trades within a
time tolerance, to measure agreement before trusting the bot live. Replay-mode-only,
optional, and orthogonal to the rest of the pipeline. See docs/DESIGN.md §11a.

`compare()` takes (timestamp, instrument, DecisionResult) triples rather than bare
DecisionResult objects for matching purposes, since DecisionResult itself carries no
timestamp/instrument — the timestamp and instrument are only used to find matches; the
stored ComparisonReport keeps the plain DecisionResult, per docs/DESIGN.md §4.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from ..config.constants import TradeAction
from ..models.engine_results import ComparisonReport, DecisionResult, ManualTradeRecord

BotDecision = tuple[datetime, str, DecisionResult]


class DecisionComparator:
    @staticmethod
    def compare(
        bot_decisions: Sequence[BotDecision],
        manual_trades: Sequence[ManualTradeRecord],
        tolerance_seconds: float,
    ) -> ComparisonReport:
        bot_trades = [bd for bd in bot_decisions if bd[2].action in (TradeAction.BUY, TradeAction.SELL)]

        matched: list[tuple[DecisionResult, ManualTradeRecord]] = []
        matched_bot_indices: set[int] = set()
        matched_manual_indices: set[int] = set()

        for mi, manual in enumerate(manual_trades):
            best_bi: int | None = None
            best_diff: float | None = None
            for bi, (ts, instrument, decision) in enumerate(bot_trades):
                if bi in matched_bot_indices or instrument != manual.instrument:
                    continue
                diff = abs((ts - manual.timestamp).total_seconds())
                if diff <= tolerance_seconds and (best_diff is None or diff < best_diff):
                    best_bi, best_diff = bi, diff
            if best_bi is not None:
                matched_bot_indices.add(best_bi)
                matched_manual_indices.add(mi)
                matched.append((bot_trades[best_bi][2], manual))

        bot_only = tuple(d for bi, (_, __, d) in enumerate(bot_trades) if bi not in matched_bot_indices)
        manual_only = tuple(m for mi, m in enumerate(manual_trades) if mi not in matched_manual_indices)
        agreement_pct = (len(matched) / len(manual_trades) * 100.0) if manual_trades else 0.0

        return ComparisonReport(
            total_bot_decisions=len(bot_decisions),
            total_manual_trades=len(manual_trades),
            matched=tuple(matched),
            bot_only=bot_only,
            manual_only=manual_only,
            agreement_pct=agreement_pct,
        )
