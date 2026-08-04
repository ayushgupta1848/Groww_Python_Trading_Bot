import unittest

from trading_decision_engine.app.config.constants import Direction
from trading_decision_engine.app.engines.market_strength_engine import MIN_CANDLES, MarketStrengthEngine
from trading_decision_engine.tests.fixtures import make_candles, make_snapshot


class TestMarketStrengthEngine(unittest.TestCase):
    def test_insufficient_history_is_neutral(self):
        snapshot = make_snapshot(candles=make_candles(5))
        result = MarketStrengthEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.NEUTRAL)
        self.assertEqual(result.confidence, 0.0)

    def test_consistent_uptrend_is_bullish_with_high_trend_confidence(self):
        candles = make_candles(MIN_CANDLES + 5, step=3.0)
        snapshot = make_snapshot(candles=candles)
        result = MarketStrengthEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.BULLISH)
        self.assertGreater(result.trend_confidence, 50.0)

    def test_consistent_downtrend_is_bearish(self):
        candles = make_candles(MIN_CANDLES + 5, step=-3.0)
        snapshot = make_snapshot(candles=candles)
        result = MarketStrengthEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.BEARISH)


if __name__ == "__main__":
    unittest.main()
