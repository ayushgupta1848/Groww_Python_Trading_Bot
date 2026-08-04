import unittest
from datetime import datetime, timedelta

from trading_decision_engine.app.config.constants import Direction, MarketStructure, TradeAction, TradeLifecycleState
from trading_decision_engine.app.engines.trade_manager import TradeManager
from trading_decision_engine.app.models.engine_results import (
    BreakoutResult,
    DecisionResult,
    EligibilityResult,
    EntryContext,
    MarketStrengthResult,
    MarketStructureResult,
    PremiumMomentumResult,
    RiskResult,
    SupportResistanceResult,
    TrendResult,
    VolatilityResult,
)
from trading_decision_engine.tests.fixtures import make_session, make_snapshot

NOW = datetime(2026, 7, 13, 11, 0, 0)


def _trend(direction=Direction.BULLISH):
    return TrendResult(direction=direction, score=80, confidence=80, reasons=(), ehma_value=1, ema100_value=1, trend_angle=10, trend_strength=80)


def _breakout(direction=Direction.NEUTRAL, breakout_confirmed=False, breakdown_confirmed=False):
    return BreakoutResult(direction=direction, score=0, confidence=0, reasons=(), breakout_confirmed=breakout_confirmed, breakdown_confirmed=breakdown_confirmed, confirmation_bars_elapsed=0)


def _sr(direction=Direction.BULLISH, breakout=False, breakdown=False):
    return SupportResistanceResult(direction=direction, score=60, confidence=60, reasons=(), levels=(), nearest_support=100, nearest_resistance=200, distance_to_support=10, distance_to_resistance=20, breakout=breakout, breakdown=breakdown)


def _momentum(direction=Direction.BULLISH):
    return PremiumMomentumResult(direction=direction, score=80, confidence=80, reasons=(), velocity=5, acceleration=1, higher_highs=True, higher_lows=True, consistency=80)


def _risk(broker_connected=True):
    return RiskResult(direction=Direction.NEUTRAL, score=100, confidence=100, reasons=(), safe_to_trade=broker_connected, already_in_trade=True, order_pending=False, broker_connected=broker_connected)


def _entry_context():
    decision = DecisionResult(
        direction=Direction.BULLISH, score=90, confidence=90, reasons=("Strong Trend",), action=TradeAction.BUY,
        buy_score=90, sell_score=0, exit_score=0,
        eligibility=EligibilityResult(passed=True, reasons=("ok",), failed_checks=()), trade_quality_score=92.0,
    )
    return EntryContext(
        trend=_trend(), market_structure=MarketStructureResult(direction=Direction.BULLISH, score=70, confidence=70, reasons=(), structure=MarketStructure.HH_HL, strength=70),
        support_resistance=_sr(), premium_momentum=_momentum(),
        breakout=_breakout(breakout_confirmed=True), market_strength=MarketStrengthResult(direction=Direction.BULLISH, score=80, confidence=80, reasons=(), candle_speed=1, range_expansion=1, consolidation_score=10, trend_confidence=80),
        volatility=VolatilityResult(direction=Direction.NEUTRAL, score=100, confidence=100, reasons=(), acceptable=True, spread_pct=0.5, spike_score=0, gap_detected=False, whipsaw_detected=False),
        decision=decision,
    )


class TestTradeManager(unittest.TestCase):
    def setUp(self):
        self.tm = TradeManager()
        self.tm.on_trade_opened(
            instrument="NIFTYTESTCE", entry_price=100.0, lots=1, lot_size=75, direction=Direction.BULLISH,
            entry_context=_entry_context(), now=NOW, spot=24000.0,
        )

    def test_open_then_first_update_moves_to_monitoring(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1), spot=24010)
        state = self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        self.assertEqual(state.state, TradeLifecycleState.MONITORING)
        self.assertIsNotNone(state.entry_context)

    def test_no_exit_condition_stays_monitoring(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1), spot=24010)
        self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        state = self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        self.assertEqual(state.state, TradeLifecycleState.MONITORING)

    def test_reversal_triggers_exit(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1), spot=23990)
        self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        state = self.tm.update(snapshot, _trend(direction=Direction.BEARISH), _breakout(), _sr(), _momentum(), _risk())
        self.assertEqual(state.state, TradeLifecycleState.EXIT_TRIGGERED)
        self.assertIn("Reversal", state.exit_reason)

    def test_momentum_loss_triggers_exit(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1))
        self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        state = self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(direction=Direction.BEARISH), _risk())
        self.assertEqual(state.state, TradeLifecycleState.EXIT_TRIGGERED)
        self.assertIn("Momentum loss", state.exit_reason)

    def test_failed_breakout_triggers_exit(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1))
        self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        state = self.tm.update(snapshot, _trend(), _breakout(breakdown_confirmed=True), _sr(), _momentum(), _risk())
        self.assertEqual(state.state, TradeLifecycleState.EXIT_TRIGGERED)
        self.assertIn("Failed breakout", state.exit_reason)

    def test_support_failure_triggers_exit(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1))
        self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        state = self.tm.update(snapshot, _trend(), _breakout(), _sr(breakdown=True), _momentum(), _risk())
        self.assertEqual(state.state, TradeLifecycleState.EXIT_TRIGGERED)
        self.assertIn("Support failure", state.exit_reason)

    def test_bearish_position_resistance_breakout_labeled_as_breakout_not_rejection(self):
        # A short (PE) position hurt by price breaking UP through resistance is an
        # actual level failure, not a "rejection" (which would mean price bounced away
        # from resistance — favorable for the short, not adverse).
        tm = TradeManager()
        tm.on_trade_opened(
            instrument="NIFTYTESTPE", entry_price=100.0, lots=1, lot_size=75, direction=Direction.BEARISH,
            entry_context=_entry_context(), now=NOW, spot=24000.0,
        )
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1))
        tm.update(snapshot, _trend(direction=Direction.BEARISH), _breakout(), _sr(direction=Direction.BEARISH), _momentum(direction=Direction.BEARISH), _risk())
        state = tm.update(snapshot, _trend(direction=Direction.BEARISH), _breakout(), _sr(direction=Direction.BEARISH, breakout=True), _momentum(direction=Direction.BEARISH), _risk())
        self.assertEqual(state.state, TradeLifecycleState.EXIT_TRIGGERED)
        self.assertIn("Resistance breakout", state.exit_reason)
        self.assertNotIn("rejection", state.exit_reason.lower())

    def test_broker_disconnect_forces_exit(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1))
        self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        state = self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk(broker_connected=False))
        self.assertEqual(state.state, TradeLifecycleState.EXIT_TRIGGERED)

    def test_daily_loss_limit_breach_forces_exit_mid_trade(self):
        # Default StrategyConfig.daily_loss_limit is 5000.0 — a session already past
        # that loss (e.g. from earlier closed trades today) must force this open trade
        # closed too, not just block new entries (see docs/DESIGN.md §6b).
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1), session=make_session(daily_pnl=-6000.0))
        state = self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        self.assertEqual(state.state, TradeLifecycleState.EXIT_TRIGGERED)
        self.assertIn("daily loss limit", state.exit_reason)

    def test_daily_pnl_within_limit_does_not_force_exit(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1), session=make_session(daily_pnl=-1000.0))
        state = self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        self.assertEqual(state.state, TradeLifecycleState.MONITORING)

    def test_bearish_pe_position_gaining_premium_is_a_profit(self):
        # Bearish exposure is a LONG PE (the bot always buys premium) — a PE that rises
        # from 100 to 150 is a WIN, not a short-side loss.
        tm = TradeManager()
        tm.on_trade_opened(
            instrument="NIFTYTESTPE", entry_price=100.0, lots=1, lot_size=75, direction=Direction.BEARISH,
            entry_context=_entry_context(), now=NOW, spot=24000.0,
        )
        final = tm.on_trade_closed(exit_price=150.0, now=NOW + timedelta(seconds=30))
        self.assertEqual(final.current_profit, 50.0 * 75)
        self.assertEqual(final.current_loss, 0.0)

    def test_on_trade_closed_resets_to_idle_and_preserves_entry_context(self):
        snapshot = make_snapshot(timestamp=NOW + timedelta(seconds=1))
        self.tm.update(snapshot, _trend(), _breakout(), _sr(), _momentum(), _risk())
        self.tm.update(snapshot, _trend(direction=Direction.BEARISH), _breakout(), _sr(), _momentum(), _risk())
        final = self.tm.on_trade_closed(exit_price=110.0, now=NOW + timedelta(seconds=2))
        self.assertEqual(final.state, TradeLifecycleState.CLOSED)
        self.assertIsNotNone(final.entry_context)
        self.assertEqual(self.tm.state, TradeLifecycleState.IDLE)
        self.assertGreater(final.current_profit, 0.0)  # bought at 100, exited at 110


if __name__ == "__main__":
    unittest.main()
