import unittest

from trading_decision_engine.app.config.constants import Direction
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.engines.breakout_engine import BreakoutEngine
from trading_decision_engine.app.models.engine_results import SupportResistanceResult
from trading_decision_engine.tests.fixtures import make_candles, make_snapshot


def _sr_result(nearest_support=23900.0, nearest_resistance=24100.0, confidence=90.0):
    return SupportResistanceResult(
        direction=Direction.NEUTRAL, score=50.0, confidence=confidence, reasons=(),
        levels=(), nearest_support=nearest_support, nearest_resistance=nearest_resistance,
        distance_to_support=100.0, distance_to_resistance=100.0, breakout=False, breakdown=False,
    )


class TestBreakoutEngine(unittest.TestCase):
    def test_unresolved_sr_gives_neutral(self):
        snapshot = make_snapshot(candles=make_candles(5))
        result = BreakoutEngine().analyze(snapshot, _sr_result(confidence=0.0))
        self.assertEqual(result.direction, Direction.NEUTRAL)
        self.assertFalse(result.breakout_confirmed)

    def test_breakout_confirmed_after_enough_closes_above_resistance(self):
        config = StrategyConfig()
        # 5 candles all closing well above the resistance level
        candles = make_candles(10, start_price=24150.0, step=5.0)
        snapshot = make_snapshot(candles=candles)
        result = BreakoutEngine(config).analyze(snapshot, _sr_result(nearest_resistance=24100.0))
        self.assertTrue(result.breakout_confirmed)
        self.assertEqual(result.direction, Direction.BULLISH)

    def test_breakdown_confirmed_after_enough_closes_below_support(self):
        config = StrategyConfig()
        candles = make_candles(10, start_price=23800.0, step=-5.0)
        snapshot = make_snapshot(candles=candles)
        result = BreakoutEngine(config).analyze(snapshot, _sr_result(nearest_support=23900.0))
        self.assertTrue(result.breakdown_confirmed)
        self.assertEqual(result.direction, Direction.BEARISH)

    def test_no_breakout_when_price_between_levels(self):
        candles = make_candles(10, start_price=24000.0, step=0.5)
        snapshot = make_snapshot(candles=candles)
        result = BreakoutEngine().analyze(snapshot, _sr_result(nearest_support=23900.0, nearest_resistance=24100.0))
        self.assertFalse(result.breakout_confirmed)
        self.assertFalse(result.breakdown_confirmed)


if __name__ == "__main__":
    unittest.main()
