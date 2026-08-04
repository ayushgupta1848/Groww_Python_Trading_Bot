"""RiskEngine: operational safety only — already-in-trade, order-pending,
margin-available, broker-connected. Cooldown/daily-loss/exposure discipline lives in
TradingRulesEngine, kept deliberately separate. See docs/DESIGN.md §3 (row 10).
"""

from __future__ import annotations

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import RiskResult
from ..models.market_snapshot import SessionState


class RiskEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(self, session: SessionState) -> RiskResult:
        reasons: list[str] = []
        # margin must exceed the configured safety floor, not just be non-zero —
        # risk_min_margin_available = 0 preserves the original "any margin" behaviour.
        min_margin = self._config.risk_min_margin_available

        if session.already_in_trade:
            reasons.append("Already in an open trade")
        if session.order_pending:
            reasons.append("An order is already pending")
        if not session.broker_connected:
            reasons.append("Broker not connected")
        if session.margin_available <= min_margin:
            reasons.append(f"Margin ₹{session.margin_available:.2f} at or below required floor ₹{min_margin:.2f}")

        safe_to_trade = (
            not session.already_in_trade
            and not session.order_pending
            and session.broker_connected
            and session.margin_available > min_margin
        )
        if safe_to_trade:
            reasons.append("Operationally safe to trade")

        return RiskResult(
            direction=Direction.NEUTRAL,
            score=100.0 if safe_to_trade else 0.0,
            confidence=100.0,
            reasons=tuple(reasons),
            safe_to_trade=safe_to_trade,
            already_in_trade=session.already_in_trade,
            order_pending=session.order_pending,
            broker_connected=session.broker_connected,
        )
