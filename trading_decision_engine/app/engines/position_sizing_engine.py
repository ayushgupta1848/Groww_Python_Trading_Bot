"""PositionSizingEngine: decides HOW MUCH to trade (lots, capital, margin), separate
from DecisionEngine's WHETHER-to-trade call. Built around a pluggable SizingStrategy so
dynamic/confidence-based sizing is a config change later, not a rewrite. See
docs/DESIGN.md §3 (row 13).

margin_available and lot_size are passed as plain values (sourced by the Orchestrator
from SessionState / the instrument master) rather than threaded through RiskResult,
mirroring the same narrow "engine needs one more concrete input" pattern already used
for BreakoutEngine's support_resistance parameter.
"""

from __future__ import annotations

from typing import Protocol

from ..config.constants import Direction, TradeAction
from ..config.strategy import StrategyConfig
from ..models.engine_results import DecisionResult, OptionSelectionResult, PositionSizeResult


class SizingStrategy(Protocol):
    def size(
        self, decision: DecisionResult, option_selection: OptionSelectionResult, config: StrategyConfig
    ) -> tuple[int, str]: ...


class FixedLotSizingStrategy:
    """v1 default: always trade config.default_lots."""

    def size(
        self, decision: DecisionResult, option_selection: OptionSelectionResult, config: StrategyConfig
    ) -> tuple[int, str]:
        return config.default_lots, f"Fixed sizing: {config.default_lots} lot(s)"


class ConfidenceBasedSizingStrategy:
    """Documented future extension (not selected by default): scales lots with
    trade_quality_score. Wire in via PositionSizingEngine(sizing_strategy=...) once the
    scaling curve has been tuned against replay data.
    """

    def size(
        self, decision: DecisionResult, option_selection: OptionSelectionResult, config: StrategyConfig
    ) -> tuple[int, str]:
        multiplier = max(1, round(decision.trade_quality_score / 50.0))
        lots = config.default_lots * multiplier
        return lots, f"Confidence-based sizing: quality {decision.trade_quality_score:.0f} -> {lots} lot(s)"


class PositionSizingEngine:
    def __init__(self, config: StrategyConfig | None = None, sizing_strategy: SizingStrategy | None = None) -> None:
        self._config = config or StrategyConfig()
        self._sizing_strategy = sizing_strategy or FixedLotSizingStrategy()

    def size(
        self,
        decision: DecisionResult,
        option_selection: OptionSelectionResult,
        margin_available: float,
        lot_size: int,
    ) -> PositionSizeResult:
        cfg = self._config

        if decision.action not in (TradeAction.BUY, TradeAction.SELL):
            return PositionSizeResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=("No entry action to size",),
                lots=0,
                capital_allocated=0.0,
                margin_required=0.0,
                risk_percentage_used=0.0,
            )

        premium = option_selection.ce_premium if decision.direction == Direction.BULLISH else option_selection.pe_premium
        if premium is None or premium <= 0:
            return PositionSizeResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=("No instrument selected to size",),
                lots=0,
                capital_allocated=0.0,
                margin_required=0.0,
                risk_percentage_used=0.0,
            )

        requested_lots, reason = self._sizing_strategy.size(decision, option_selection, cfg)
        cost_per_lot = premium * lot_size

        max_lots_by_margin = int(margin_available // cost_per_lot) if cost_per_lot > 0 else 0
        max_lots_by_exposure = int(cfg.max_exposure // cost_per_lot) if cost_per_lot > 0 else 0
        final_lots = max(0, min(requested_lots, max_lots_by_margin, max_lots_by_exposure))

        capital_allocated = final_lots * cost_per_lot
        margin_required = capital_allocated
        risk_percentage_used = (capital_allocated / cfg.max_exposure * 100.0) if cfg.max_exposure > 0 else 0.0

        reasons = [reason]
        if final_lots < requested_lots:
            reasons.append(
                f"Capped from {requested_lots} to {final_lots} lot(s) by margin/exposure headroom"
            )
        if final_lots == 0:
            reasons.append("Insufficient margin/exposure headroom for even 1 lot")

        return PositionSizeResult(
            direction=decision.direction,
            score=100.0 if final_lots > 0 else 0.0,
            confidence=100.0 if final_lots == requested_lots else 50.0,
            reasons=tuple(reasons),
            lots=final_lots,
            capital_allocated=capital_allocated,
            margin_required=margin_required,
            risk_percentage_used=risk_percentage_used,
        )
