import unittest

from trading_decision_engine.app.config.constants import Direction, MarketStructure
from trading_decision_engine.app.engines.market_structure_engine import MarketStructureEngine
from trading_decision_engine.tests.fixtures import make_candles, make_snapshot, make_zigzag_candles


class TestMarketStructureEngine(unittest.TestCase):
    def test_insufficient_history_is_sideways(self):
        snapshot = make_snapshot(candles=make_candles(5))
        result = MarketStructureEngine().analyze(snapshot)
        self.assertEqual(result.structure, MarketStructure.SIDEWAYS)
        self.assertEqual(result.confidence, 0.0)

    def test_zigzag_produces_a_classified_structure(self):
        candles = make_zigzag_candles(80)
        snapshot = make_snapshot(candles=candles, spot=candles[-1].close)
        result = MarketStructureEngine().analyze(snapshot)
        self.assertIsInstance(result.structure, MarketStructure)
        self.assertGreaterEqual(result.strength, 0.0)

    def test_monotonic_uptrend_has_no_clear_swing_alternation(self):
        candles = make_candles(80, step=5.0)
        snapshot = make_snapshot(candles=candles, spot=candles[-1].close)
        result = MarketStructureEngine().analyze(snapshot)
        # a smooth monotonic ramp has essentially no local swing highs/lows to classify
        self.assertIn(result.structure, (MarketStructure.SIDEWAYS, MarketStructure.HH_HL))


if __name__ == "__main__":
    unittest.main()
