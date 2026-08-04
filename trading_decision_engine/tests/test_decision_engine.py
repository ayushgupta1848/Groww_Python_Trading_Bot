import unittest

from trading_decision_engine.app.config.constants import Direction, MarketStructure, TradeAction
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.engines.decision_engine import DecisionEngine, DecisionInput
from trading_decision_engine.app.models.engine_results import (
    BreakoutResult,
    MarketStrengthResult,
    MarketStructureResult,
    OptionSelectionResult,
    PremiumMomentumResult,
    RiskResult,
    SignalStabilityResult,
    SupportResistanceResult,
    TradingRulesResult,
    TrendResult,
    VolatilityResult,
)


def _base_inputs(**overrides) -> DecisionInput:
    defaults = dict(
        trend=TrendResult(direction=Direction.BULLISH, score=80, confidence=85, reasons=("EHMA rising",), ehma_value=100, ema100_value=95, trend_angle=20, trend_strength=80),
        market_structure=MarketStructureResult(direction=Direction.BULLISH, score=75, confidence=75, reasons=(), structure=MarketStructure.HH_HL, strength=75),
        support_resistance=SupportResistanceResult(direction=Direction.BULLISH, score=70, confidence=90, reasons=(), levels=(), nearest_support=23900, nearest_resistance=24100, distance_to_support=50, distance_to_resistance=50, breakout=False, breakdown=False),
        premium_momentum=PremiumMomentumResult(direction=Direction.BULLISH, score=80, confidence=80, reasons=(), velocity=5, acceleration=1, higher_highs=True, higher_lows=True, consistency=80),
        option_selection=OptionSelectionResult(direction=Direction.NEUTRAL, score=90, confidence=90, reasons=(), best_ce_symbol="NIFTYCE", best_pe_symbol="NIFTYPE", ce_premium=120.0, pe_premium=90.0, ce_liquidity_score=95, pe_liquidity_score=90, ce_spread_score=95, pe_spread_score=90),
        breakout=BreakoutResult(direction=Direction.BULLISH, score=95, confidence=95, reasons=(), breakout_confirmed=True, breakdown_confirmed=False, confirmation_bars_elapsed=3),
        market_strength=MarketStrengthResult(direction=Direction.BULLISH, score=90, confidence=90, reasons=(), candle_speed=2, range_expansion=1.2, consolidation_score=20, trend_confidence=90),
        volatility=VolatilityResult(direction=Direction.NEUTRAL, score=100, confidence=100, reasons=(), acceptable=True, spread_pct=0.5, spike_score=0, gap_detected=False, whipsaw_detected=False),
        trading_rules=TradingRulesResult(direction=Direction.NEUTRAL, score=100, confidence=100, reasons=("clear",), allowed=True, trades_today=0, consecutive_losses=0, is_expiry_day=False, near_market_close=False),
        risk=RiskResult(direction=Direction.NEUTRAL, score=100, confidence=100, reasons=("safe",), safe_to_trade=True, already_in_trade=False, order_pending=False, broker_connected=True),
        signal_stability=SignalStabilityResult(direction=Direction.BULLISH, score=100, confidence=100, reasons=("stable",), stable=True, confirmation_seconds_elapsed=4.0, required_seconds=2.0),
    )
    defaults.update(overrides)
    return DecisionInput(**defaults)


class TestDecisionEngine(unittest.TestCase):
    def test_strong_setup_produces_buy(self):
        engine = DecisionEngine(StrategyConfig())
        decision = engine.decide(_base_inputs())
        self.assertEqual(decision.action, TradeAction.BUY)
        self.assertTrue(decision.eligibility.passed)
        self.assertGreater(decision.trade_quality_score, 0.0)

    def test_stage1_rejects_on_unconfirmed_trend_without_running_stage2(self):
        engine = DecisionEngine(StrategyConfig())
        weak_trend = TrendResult(direction=Direction.NEUTRAL, score=10, confidence=10, reasons=(), ehma_value=1, ema100_value=1, trend_angle=1, trend_strength=10)
        decision = engine.decide(_base_inputs(trend=weak_trend))
        self.assertEqual(decision.action, TradeAction.REJECT)
        self.assertFalse(decision.eligibility.passed)
        self.assertIn("trend", decision.eligibility.failed_checks)
        self.assertEqual(decision.trade_quality_score, 0.0)

    def test_stage1_rejects_on_unstable_signal(self):
        engine = DecisionEngine(StrategyConfig())
        unstable = SignalStabilityResult(direction=Direction.NEUTRAL, score=0, confidence=0, reasons=("not stable",), stable=False, confirmation_seconds_elapsed=0.5, required_seconds=3.0)
        decision = engine.decide(_base_inputs(signal_stability=unstable))
        self.assertEqual(decision.action, TradeAction.REJECT)
        self.assertIn("signal_stability", decision.eligibility.failed_checks)

    def test_stage1_rejects_on_trading_rules_blocked(self):
        engine = DecisionEngine(StrategyConfig())
        blocked = TradingRulesResult(direction=Direction.NEUTRAL, score=0, confidence=100, reasons=("blocked",), allowed=False, trades_today=6, consecutive_losses=0, is_expiry_day=False, near_market_close=False)
        decision = engine.decide(_base_inputs(trading_rules=blocked))
        self.assertEqual(decision.action, TradeAction.REJECT)
        self.assertIn("trading_rules", decision.eligibility.failed_checks)

    def test_stage1_rejects_on_risk_unsafe(self):
        engine = DecisionEngine(StrategyConfig())
        unsafe = RiskResult(direction=Direction.NEUTRAL, score=0, confidence=100, reasons=("unsafe",), safe_to_trade=False, already_in_trade=True, order_pending=False, broker_connected=True)
        decision = engine.decide(_base_inputs(risk=unsafe))
        self.assertEqual(decision.action, TradeAction.REJECT)
        self.assertIn("risk", decision.eligibility.failed_checks)

    def test_stage1_rejects_on_insufficient_resistance_room(self):
        engine = DecisionEngine(StrategyConfig())
        tight_sr = SupportResistanceResult(direction=Direction.BULLISH, score=10, confidence=90, reasons=(), levels=(), nearest_support=23990, nearest_resistance=24005, distance_to_support=10, distance_to_resistance=5, breakout=False, breakdown=False)
        decision = engine.decide(_base_inputs(support_resistance=tight_sr))
        self.assertEqual(decision.action, TradeAction.REJECT)
        self.assertIn("support_resistance", decision.eligibility.failed_checks)

    def test_stage1_rejects_on_unacceptable_volatility(self):
        engine = DecisionEngine(StrategyConfig())
        bad_vol = VolatilityResult(direction=Direction.NEUTRAL, score=0, confidence=0, reasons=("spike",), acceptable=False, spread_pct=5, spike_score=90, gap_detected=True, whipsaw_detected=False)
        decision = engine.decide(_base_inputs(volatility=bad_vol))
        self.assertEqual(decision.action, TradeAction.REJECT)
        self.assertIn("volatility", decision.eligibility.failed_checks)

    def test_weak_quality_dimensions_produce_hold_not_buy(self):
        engine = DecisionEngine(StrategyConfig())
        weak_structure = MarketStructureResult(direction=Direction.NEUTRAL, score=10, confidence=10, reasons=(), structure=MarketStructure.SIDEWAYS, strength=10)
        weak_momentum = PremiumMomentumResult(direction=Direction.NEUTRAL, score=10, confidence=10, reasons=(), velocity=0, acceleration=0, higher_highs=False, higher_lows=False, consistency=10)
        weak_breakout = BreakoutResult(direction=Direction.NEUTRAL, score=0, confidence=0, reasons=(), breakout_confirmed=False, breakdown_confirmed=False, confirmation_bars_elapsed=0)
        weak_strength = MarketStrengthResult(direction=Direction.NEUTRAL, score=10, confidence=10, reasons=(), candle_speed=0, range_expansion=1, consolidation_score=80, trend_confidence=10)
        decision = engine.decide(_base_inputs(
            market_structure=weak_structure, premium_momentum=weak_momentum, breakout=weak_breakout, market_strength=weak_strength,
        ))
        self.assertIn(decision.action, (TradeAction.HOLD,))
        self.assertTrue(decision.eligibility.passed)  # eligibility still passes, just quality is too low

    def test_trade_quality_score_independent_of_action(self):
        engine = DecisionEngine(StrategyConfig())
        decision = engine.decide(_base_inputs())
        # trade_quality_score reflects setup quality, not a re-derivation of the action
        self.assertGreaterEqual(decision.trade_quality_score, decision.confidence)


if __name__ == "__main__":
    unittest.main()
