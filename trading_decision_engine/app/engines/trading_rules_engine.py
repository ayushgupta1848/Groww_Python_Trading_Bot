"""TradingRulesEngine: trading discipline, entirely independent of market analysis —
only reads SessionState, the current timestamp, and StrategyConfig. See
docs/DESIGN.md §3 (row 9).

`expiry_date` is accepted as an optional calendar fact (not price/candle data) so
expiry-day rules can be evaluated; it is sourced by the Orchestrator from the currently
traded instrument's expiry, not computed here.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import TradingRulesResult
from ..models.market_snapshot import SessionState

MARKET_CLOSE_TIME = time(15, 30)  # NSE/BSE regular close


class TradingRulesEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(
        self,
        session: SessionState,
        timestamp: datetime,
        expiry_date: date | None = None,
    ) -> TradingRulesResult:
        cfg = self._config
        reasons: list[str] = []
        blocked = False

        if session.trades_today >= cfg.max_trades_per_day:
            blocked = True
            reasons.append(f"Max trades/day reached ({session.trades_today}/{cfg.max_trades_per_day})")

        if session.cooldown_until is not None and timestamp < session.cooldown_until:
            blocked = True
            remaining = (session.cooldown_until - timestamp).total_seconds()
            reasons.append(f"Cooldown active for {remaining:.0f}s more")

        if session.consecutive_losses >= cfg.consecutive_loss_limit:
            blocked = True
            reasons.append(f"Consecutive loss limit reached ({session.consecutive_losses}/{cfg.consecutive_loss_limit})")

        if session.daily_pnl <= -cfg.daily_loss_limit:
            blocked = True
            reasons.append(f"Daily loss limit reached (₹{session.daily_pnl:.2f})")

        if session.daily_pnl >= cfg.daily_profit_lock:
            blocked = True
            reasons.append(f"Daily profit lock reached (₹{session.daily_pnl:.2f})")

        if session.current_exposure >= cfg.max_exposure:
            blocked = True
            reasons.append(f"Max exposure reached (₹{session.current_exposure:.2f}/{cfg.max_exposure:.2f})")

        is_expiry_day = expiry_date is not None and expiry_date == timestamp.date()
        if is_expiry_day and timestamp.hour >= cfg.expiry_day_cutoff_hour:
            blocked = True
            reasons.append(f"Expiry-day cutoff hour reached ({timestamp.hour}:00 >= {cfg.expiry_day_cutoff_hour}:00)")

        market_close_dt = datetime.combine(timestamp.date(), MARKET_CLOSE_TIME)
        near_market_close = (market_close_dt - timestamp) <= timedelta(minutes=cfg.market_close_buffer_minutes)
        if near_market_close:
            blocked = True
            reasons.append(f"Within {cfg.market_close_buffer_minutes}min of market close — no new entries")

        if not blocked:
            reasons.append("All trading-discipline rules clear")

        return TradingRulesResult(
            direction=Direction.NEUTRAL,
            score=0.0 if blocked else 100.0,
            confidence=100.0,
            reasons=tuple(reasons),
            allowed=not blocked,
            trades_today=session.trades_today,
            consecutive_losses=session.consecutive_losses,
            is_expiry_day=is_expiry_day,
            near_market_close=near_market_close,
        )
