"""TradeManager: the one deliberately stateful component. Tracks an open trade's
lifecycle (entry price, current price, highest/lowest premium and spot, running P&L,
time in trade) and detects the six exit conditions from docs/DESIGN.md §6b: reversal,
momentum loss, failed breakout, support failure, resistance rejection, and a forced
exit (risk/market-close). See docs/DESIGN.md §3 (row 14), §6b.

`update()` takes the specific engine results it needs to evaluate the exit conditions
(trend, breakout, support_resistance, premium_momentum, risk) directly — the same
narrow "one more concrete input" extension already used for BreakoutEngine and
PositionSizingEngine, since a bare DecisionResult (which is entry-eligibility-shaped and
always REJECTs while already_in_trade) does not carry the per-engine detail these
specific, named exit checks require.
"""

from __future__ import annotations

from datetime import datetime

from ..config.constants import Direction, TradeLifecycleState
from ..config.strategy import StrategyConfig
from ..models.engine_results import (
    BreakoutResult,
    EntryContext,
    PremiumMomentumResult,
    RiskResult,
    SupportResistanceResult,
    TradeState,
    TrendResult,
)
from ..models.market_snapshot import MarketSnapshot


class TradeManager:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()
        self._state = TradeLifecycleState.IDLE
        self._instrument: str | None = None
        self._direction: Direction = Direction.NEUTRAL
        self._lots = 0
        self._lot_size = 0
        self._entry_price = 0.0
        self._entry_time: datetime | None = None
        self._highest_premium = 0.0
        self._lowest_premium = 0.0
        self._highest_spot = 0.0
        self._lowest_spot = 0.0
        self._entry_context: EntryContext | None = None
        self._exit_reason: str | None = None

    @property
    def state(self) -> TradeLifecycleState:
        return self._state

    def on_trade_opened(
        self,
        instrument: str,
        entry_price: float,
        lots: int,
        lot_size: int,
        direction: Direction,
        entry_context: EntryContext,
        now: datetime,
        spot: float,
    ) -> None:
        self._state = TradeLifecycleState.OPEN
        self._instrument = instrument
        self._direction = direction
        self._lots = lots
        self._lot_size = lot_size
        self._entry_price = entry_price
        self._entry_time = now
        self._highest_premium = entry_price
        self._lowest_premium = entry_price
        self._highest_spot = spot
        self._lowest_spot = spot
        self._entry_context = entry_context
        self._exit_reason = None

    def update(
        self,
        snapshot: MarketSnapshot,
        trend: TrendResult,
        breakout: BreakoutResult,
        support_resistance: SupportResistanceResult,
        premium_momentum: PremiumMomentumResult,
        risk: RiskResult,
    ) -> TradeState:
        if self._state == TradeLifecycleState.IDLE or self._entry_time is None:
            return self._trade_state(current_price=0.0, time_in_trade=0.0)

        if self._state == TradeLifecycleState.OPEN:
            self._state = TradeLifecycleState.MONITORING

        current_price = self._current_price(snapshot)
        self._highest_premium = max(self._highest_premium, current_price)
        self._lowest_premium = min(self._lowest_premium, current_price)
        self._highest_spot = max(self._highest_spot, snapshot.spot)
        self._lowest_spot = min(self._lowest_spot, snapshot.spot)
        time_in_trade = (snapshot.timestamp - self._entry_time).total_seconds()

        if self._state == TradeLifecycleState.MONITORING:
            exit_reason = self._detect_exit(trend, breakout, support_resistance, premium_momentum, risk, snapshot.session.daily_pnl)
            if exit_reason is not None:
                self._state = TradeLifecycleState.EXIT_TRIGGERED
                self._exit_reason = exit_reason

        return self._trade_state(current_price=current_price, time_in_trade=time_in_trade)

    def on_trade_closed(self, exit_price: float, now: datetime) -> TradeState:
        time_in_trade = (now - self._entry_time).total_seconds() if self._entry_time else 0.0
        state = self._trade_state(current_price=exit_price, time_in_trade=time_in_trade)
        final = TradeState(
            state=TradeLifecycleState.CLOSED,
            entry_price=state.entry_price,
            current_price=state.current_price,
            highest_premium=state.highest_premium,
            lowest_premium=state.lowest_premium,
            current_profit=state.current_profit,
            current_loss=state.current_loss,
            time_in_trade_seconds=state.time_in_trade_seconds,
            highest_spot=state.highest_spot,
            lowest_spot=state.lowest_spot,
            exit_reason=self._exit_reason,
            entry_context=state.entry_context,
        )
        self._reset()
        return final

    def _reset(self) -> None:
        self._state = TradeLifecycleState.IDLE
        self._instrument = None
        self._direction = Direction.NEUTRAL
        self._lots = 0
        self._lot_size = 0
        self._entry_price = 0.0
        self._entry_time = None
        self._highest_premium = 0.0
        self._lowest_premium = 0.0
        self._highest_spot = 0.0
        self._lowest_spot = 0.0
        self._entry_context = None
        self._exit_reason = None

    def _current_price(self, snapshot: MarketSnapshot) -> float:
        if not snapshot.premium_history:
            return self._entry_price
        latest = snapshot.premium_history[-1]
        return latest.ce_premium if self._direction == Direction.BULLISH else latest.pe_premium

    def _pnl_per_unit(self, current_price: float) -> float:
        # Both directions are LONG premium (bullish buys a CE, bearish buys a PE — see
        # the Orchestrator's entry flow, which always places a BUY), so P&L is always
        # current - entry on the tracked leg; `entry - current` would be short-selling
        # semantics this engine never uses.
        return current_price - self._entry_price

    def _trade_state(self, current_price: float, time_in_trade: float) -> TradeState:
        pnl_per_unit = self._pnl_per_unit(current_price) if self._entry_time else 0.0
        total_pnl = pnl_per_unit * self._lots * self._lot_size
        return TradeState(
            state=self._state,
            entry_price=self._entry_price,
            current_price=current_price,
            highest_premium=self._highest_premium,
            lowest_premium=self._lowest_premium,
            current_profit=max(0.0, total_pnl),
            current_loss=max(0.0, -total_pnl),
            time_in_trade_seconds=time_in_trade,
            highest_spot=self._highest_spot,
            lowest_spot=self._lowest_spot,
            exit_reason=self._exit_reason,
            entry_context=self._entry_context,
        )

    def _detect_exit(
        self,
        trend: TrendResult,
        breakout: BreakoutResult,
        support_resistance: SupportResistanceResult,
        premium_momentum: PremiumMomentumResult,
        risk: RiskResult,
        daily_pnl: float,
    ) -> str | None:
        opposing = Direction.BEARISH if self._direction == Direction.BULLISH else Direction.BULLISH

        if trend.direction == opposing:
            return f"Reversal: trend turned {trend.direction.value} against the {self._direction.value} position"

        if premium_momentum.direction == opposing:
            return f"Momentum loss: premium momentum turned {premium_momentum.direction.value}"

        against_breakout = breakout.breakdown_confirmed if self._direction == Direction.BULLISH else breakout.breakout_confirmed
        if against_breakout:
            return "Failed breakout: price fell back through the confirmed level"

        # A BULLISH (long CE) position is hurt by the underlying breaking DOWN through
        # support; a BEARISH (long PE) position is hurt by the underlying breaking UP
        # through resistance — that is an actual breakout/failure of the level, not a
        # "rejection" (a rejection is price bouncing away from a level, which would favor
        # this position, not hurt it), so the two sides get distinct, accurate labels.
        against_level = support_resistance.breakdown if self._direction == Direction.BULLISH else support_resistance.breakout
        if against_level:
            return "Support failure: price broke below the confirmed support level" if self._direction == Direction.BULLISH else "Resistance breakout: price broke above the confirmed resistance level"

        against_direction = support_resistance.direction == opposing
        if against_direction:
            return "Resistance rejection: price pressure shifted toward resistance" if self._direction == Direction.BULLISH else "Support rejection: price pressure shifted toward support"

        if not risk.broker_connected:
            return "Risk Engine forces exit: broker disconnected"

        if daily_pnl <= -self._config.daily_loss_limit:
            return f"Risk Engine forces exit: daily loss limit reached (₹{daily_pnl:.2f})"

        return None
