import unittest

from trading_decision_engine.app.config.constants import Direction, TradeAction
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.engines.position_sizing_engine import (
    ConfidenceBasedSizingStrategy,
    FixedLotSizingStrategy,
    PositionSizingEngine,
)
from trading_decision_engine.app.models.engine_results import DecisionResult, EligibilityResult, OptionSelectionResult

PASSING_ELIGIBILITY = EligibilityResult(passed=True, reasons=("ok",), failed_checks=())


def _decision(action=TradeAction.BUY, direction=Direction.BULLISH, quality=90.0):
    return DecisionResult(
        direction=direction, score=90, confidence=90, reasons=(), action=action,
        buy_score=90, sell_score=0, exit_score=0, eligibility=PASSING_ELIGIBILITY, trade_quality_score=quality,
    )


def _option_selection(ce_premium=100.0, pe_premium=90.0):
    return OptionSelectionResult(
        direction=Direction.NEUTRAL, score=90, confidence=90, reasons=(), best_ce_symbol="CE", best_pe_symbol="PE",
        ce_premium=ce_premium, pe_premium=pe_premium, ce_liquidity_score=90, pe_liquidity_score=90, ce_spread_score=90, pe_spread_score=90,
    )


class TestPositionSizingEngine(unittest.TestCase):
    def test_hold_decision_sizes_zero(self):
        engine = PositionSizingEngine(StrategyConfig())
        result = engine.size(_decision(action=TradeAction.HOLD), _option_selection(), margin_available=100000, lot_size=75)
        self.assertEqual(result.lots, 0)

    def test_fixed_lot_sizing_respects_default(self):
        config = StrategyConfig()
        engine = PositionSizingEngine(config, sizing_strategy=FixedLotSizingStrategy())
        result = engine.size(_decision(), _option_selection(ce_premium=100.0), margin_available=1_000_000, lot_size=75)
        self.assertEqual(result.lots, config.default_lots)
        self.assertAlmostEqual(result.capital_allocated, config.default_lots * 75 * 100.0)

    def test_margin_cap_reduces_lots(self):
        config = StrategyConfig(default_lots=10)
        engine = PositionSizingEngine(config)
        # 10 lots * 75 * 100 = 75000, but margin only allows 1 lot (7500)
        result = engine.size(_decision(), _option_selection(ce_premium=100.0), margin_available=8000, lot_size=75)
        self.assertEqual(result.lots, 1)
        self.assertIn("Capped", " ".join(result.reasons))

    def test_exposure_cap_reduces_lots(self):
        config = StrategyConfig(default_lots=10, max_exposure=7500.0)
        engine = PositionSizingEngine(config)
        result = engine.size(_decision(), _option_selection(ce_premium=100.0), margin_available=1_000_000, lot_size=75)
        self.assertEqual(result.lots, 1)

    def test_no_margin_gives_zero_lots(self):
        config = StrategyConfig()
        engine = PositionSizingEngine(config)
        result = engine.size(_decision(), _option_selection(ce_premium=100.0), margin_available=0, lot_size=75)
        self.assertEqual(result.lots, 0)

    def test_sizing_strategy_is_swappable(self):
        config = StrategyConfig(default_lots=1)
        engine = PositionSizingEngine(config, sizing_strategy=ConfidenceBasedSizingStrategy())
        result = engine.size(_decision(quality=95.0), _option_selection(ce_premium=50.0), margin_available=1_000_000, lot_size=75)
        # quality 95 -> multiplier round(95/50)=2 -> 2 lots
        self.assertEqual(result.lots, 2)


if __name__ == "__main__":
    unittest.main()
