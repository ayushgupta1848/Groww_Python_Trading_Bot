import unittest

from trading_decision_engine.app.config.constants import Direction
from trading_decision_engine.app.engines.premium_momentum_engine import PremiumMomentumEngine
from trading_decision_engine.tests.fixtures import make_premium_history, make_snapshot


class TestPremiumMomentumEngine(unittest.TestCase):
    def test_insufficient_samples_is_neutral(self):
        snapshot = make_snapshot(premium_history=make_premium_history(3))
        result = PremiumMomentumEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.NEUTRAL)
        self.assertEqual(result.confidence, 0.0)

    def test_rising_ce_falling_pe_is_bullish_with_higher_highs_lows(self):
        history = make_premium_history(12, ce_step=2.0, pe_step=-2.0)
        snapshot = make_snapshot(premium_history=history)
        result = PremiumMomentumEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.BULLISH)
        self.assertTrue(result.higher_highs)
        self.assertTrue(result.higher_lows)
        self.assertGreater(result.velocity, 0.0)

    def test_falling_ce_rising_pe_is_bearish(self):
        history = make_premium_history(12, ce_step=-2.0, pe_step=2.0)
        snapshot = make_snapshot(premium_history=history)
        result = PremiumMomentumEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.BEARISH)
        self.assertLess(result.velocity, 0.0)

    def test_flat_premiums_are_neutral_with_low_score(self):
        history = make_premium_history(12, ce_step=0.0, pe_step=0.0)
        snapshot = make_snapshot(premium_history=history)
        result = PremiumMomentumEngine().analyze(snapshot)
        self.assertEqual(result.direction, Direction.NEUTRAL)
        self.assertAlmostEqual(result.score, 0.0, places=3)


if __name__ == "__main__":
    unittest.main()
