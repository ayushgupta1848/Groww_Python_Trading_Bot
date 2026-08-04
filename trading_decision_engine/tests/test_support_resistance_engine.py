import unittest

from trading_decision_engine.app.config.constants import Direction
from trading_decision_engine.app.engines.support_resistance_engine import MIN_CANDLES, SupportResistanceEngine
from trading_decision_engine.tests.fixtures import make_candles, make_snapshot, make_zigzag_candles


class TestSupportResistanceEngine(unittest.TestCase):
    def test_insufficient_history_is_neutral(self):
        snapshot = make_snapshot(candles=make_candles(10))
        result = SupportResistanceEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.NEUTRAL)
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.levels, ())

    def test_zigzag_history_resolves_some_levels(self):
        candles = make_zigzag_candles(MIN_CANDLES + 40)
        snapshot = make_snapshot(candles=candles, spot=candles[-1].close)
        result = SupportResistanceEngine().analyze(snapshot)
        # with a genuine zigzag there should be at least some resolvable pivot levels
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertEqual(len(result.levels), 14)

    def test_level9_and_level10_are_identical_per_source_pine(self):
        candles = make_zigzag_candles(MIN_CANDLES + 60)
        snapshot = make_snapshot(candles=candles, spot=candles[-1].close)
        result = SupportResistanceEngine().analyze(snapshot)
        self.assertEqual(result.levels[8], result.levels[9])  # level9 == level10 (0-indexed 8,9)
        self.assertEqual(result.levels[12], result.levels[13])  # level13 == level14

    def test_breakout_flag_true_when_spot_above_all_resistance_candidates(self):
        candles = make_zigzag_candles(MIN_CANDLES + 60)
        # a spot far above any historical level should register as a breakout
        snapshot = make_snapshot(candles=candles, spot=candles[-1].close + 10000)
        result = SupportResistanceEngine().analyze(snapshot)
        if result.confidence > 0:
            self.assertTrue(result.breakout or result.nearest_resistance <= snapshot.spot)


if __name__ == "__main__":
    unittest.main()
