import unittest

from trading_decision_engine.app.config.constants import Direction
from trading_decision_engine.app.engines.trend_engine import TrendEngine
from trading_decision_engine.tests.fixtures import make_candles, make_snapshot


class TestTrendEngine(unittest.TestCase):
    def test_insufficient_history_is_neutral(self):
        snapshot = make_snapshot(candles=make_candles(5))
        result = TrendEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.NEUTRAL)
        self.assertEqual(result.confidence, 0.0)

    def test_strong_uptrend_is_bullish(self):
        candles = make_candles(150, start_price=24000.0, step=6.0)
        snapshot = make_snapshot(candles=candles, spot=candles[-1].close)
        result = TrendEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.BULLISH)
        self.assertGreater(result.trend_strength, 0.0)
        self.assertIn("EHMA rising", result.reasons)

    def test_strong_downtrend_is_bearish(self):
        candles = make_candles(150, start_price=24000.0, step=-6.0)
        snapshot = make_snapshot(candles=candles, spot=candles[-1].close)
        result = TrendEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.BEARISH)

    def test_ema100_agreement_boosts_confidence(self):
        candles = make_candles(150, start_price=24000.0, step=6.0)
        snapshot = make_snapshot(candles=candles, spot=candles[-1].close)
        result = TrendEngine().analyze(snapshot)
        # a sustained uptrend should have price above EMA100, agreeing with bullish EHMA
        self.assertGreater(result.confidence, 50.0)


if __name__ == "__main__":
    unittest.main()
