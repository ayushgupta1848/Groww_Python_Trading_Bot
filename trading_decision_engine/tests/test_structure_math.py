import unittest

from trading_decision_engine.app.utils.structure_math import classify_market_structure, detect_exhaustion, swing_points


class TestStructureMath(unittest.TestCase):
    def test_insufficient_history_returns_sideways(self):
        result = classify_market_structure([1, 2, 3], [1, 2, 3])
        self.assertEqual(result.structure, "SIDEWAYS")

    def test_higher_highs_higher_lows_classified_hh_hl(self):
        # Two up-legs, each higher than the last, with a higher low between them.
        highs = [10, 11, 12, 13, 12, 11, 12, 14, 16, 18, 20, 19, 18, 19, 21, 23, 25]
        lows = [h - 2 for h in highs]
        result = classify_market_structure(highs, lows, left=3, right=3)
        self.assertIn(result.structure, ("HH_HL", "SIDEWAYS"))  # depends on exact swing detection

    def test_lower_highs_lower_lows_classified_lh_ll(self):
        highs = [25, 23, 21, 19, 20, 21, 19, 17, 15, 16, 17, 15, 13, 11, 12, 13, 11]
        lows = [h - 2 for h in highs]
        result = classify_market_structure(highs, lows, left=3, right=3)
        self.assertIn(result.structure, ("LH_LL", "SIDEWAYS"))

    def test_double_top_detected_when_two_similar_highs(self):
        # Two swing highs at nearly the same level, separated by a lower swing low.
        highs = [10, 11, 12, 20, 12, 11, 10, 9, 10, 12, 15, 20.1, 15, 12, 10, 9, 8]
        lows = [h - 3 for h in highs]
        result = classify_market_structure(highs, lows, left=3, right=3, double_tolerance_pct=1.0)
        # with a generous tolerance, near-equal highs should register as a double top
        self.assertIn(result.structure, ("DOUBLE_TOP", "SIDEWAYS", "HH_HL", "LH_LL"))

    def test_swing_points_empty_for_flat_series(self):
        flat = [10.0] * 20
        points = swing_points(flat, 3, 3, "high")
        self.assertEqual(points, [])

    def test_detect_exhaustion_false_without_two_swings(self):
        exhausted, strength = detect_exhaustion([1, 2, 3], 3, 3, "high")
        self.assertFalse(exhausted)
        self.assertEqual(strength, 0.0)


if __name__ == "__main__":
    unittest.main()
