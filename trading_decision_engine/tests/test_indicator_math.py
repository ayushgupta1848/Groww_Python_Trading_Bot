import unittest

from trading_decision_engine.app.utils.indicator_math import (
    ehma,
    ema,
    pivot_high_flags,
    pivot_low_flags,
    shifted,
    sma,
    value_when,
    wma,
)


class TestIndicatorMath(unittest.TestCase):
    def test_sma_basic(self):
        values = [1, 2, 3, 4, 5]
        result = sma(values, 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertAlmostEqual(result[2], 2.0)
        self.assertAlmostEqual(result[3], 3.0)
        self.assertAlmostEqual(result[4], 4.0)

    def test_wma_weights_recent_bar_more(self):
        values = [1, 1, 10]  # a big jump on the most recent bar
        result = wma(values, 3)
        # weights 1,2,3 normalized by 6: (1*1 + 1*2 + 10*3)/6 = 33/6 = 5.5
        self.assertAlmostEqual(result[2], 5.5)

    def test_ema_seeds_with_sma_then_recurses(self):
        values = [1, 2, 3, 4, 5, 6]
        result = ema(values, 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertAlmostEqual(result[2], 2.0)  # seed = sma([1,2,3])
        alpha = 2 / 4
        expected_3 = alpha * 4 + (1 - alpha) * 2.0
        self.assertAlmostEqual(result[3], expected_3)

    def test_ehma_produces_values_once_enough_history(self):
        values = [100 + i * 0.5 for i in range(40)]
        result = ehma(values, 16)
        self.assertTrue(any(v is not None for v in result))
        self.assertIsNotNone(result[-1])

    def test_pivot_high_flags_detects_local_peak(self):
        values = [1, 2, 3, 5, 3, 2, 1]
        flags = pivot_high_flags(values, left=3, right=3)
        self.assertTrue(flags[3])
        self.assertFalse(any(f for i, f in enumerate(flags) if i != 3))

    def test_pivot_low_flags_detects_local_trough(self):
        values = [9, 8, 7, 1, 7, 8, 9]
        flags = pivot_low_flags(values, left=3, right=3)
        self.assertTrue(flags[3])

    def test_pivot_not_confirmed_without_enough_right_bars(self):
        values = [1, 2, 3, 5, 3]  # only 1 bar after the peak, right=3 needs 3
        flags = pivot_high_flags(values, left=3, right=3)
        self.assertFalse(any(flags))

    def test_pivot_high_plateau_confirms_exactly_one_pivot(self):
        # Two adjacent bars tied at the peak (102, 102): a naive strict (>) comparison
        # on both sides confirms NEITHER (each tied bar disqualifies the other), while
        # tolerating ties on both sides confirms BOTH (double-counting one peak as two
        # swing points — downstream structure analysis then sees a phantom double-top).
        # The correct Pine-style asymmetric rule confirms exactly one: ties tolerated
        # against earlier bars, strictly-lower required of later bars.
        values = [98, 100, 102, 102, 101, 100, 99, 98]
        flags = pivot_high_flags(values, left=2, right=2)
        self.assertEqual(sum(flags), 1)
        self.assertTrue(flags[3])

    def test_pivot_low_plateau_confirms_exactly_one_pivot(self):
        values = [102, 100, 98, 98, 99, 100, 101, 102]
        flags = pivot_low_flags(values, left=2, right=2)
        self.assertEqual(sum(flags), 1)
        self.assertTrue(flags[3])

    def test_flat_series_has_no_pivots(self):
        values = [10.0] * 20
        self.assertFalse(any(pivot_high_flags(values, 3, 3)))
        self.assertFalse(any(pivot_low_flags(values, 3, 3)))

    def test_value_when_returns_most_recent_match(self):
        condition = [False, True, False, True, False]
        source = [10, 20, 30, 40, 50]
        self.assertEqual(value_when(condition, source, 0), 40)
        self.assertEqual(value_when(condition, source, 1), 20)
        self.assertIsNone(value_when(condition, source, 2))

    def test_shifted_offsets_correctly(self):
        values = [10, 20, 30, 40]
        result = shifted(values, 2)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertEqual(result[2], 10)
        self.assertEqual(result[3], 20)


if __name__ == "__main__":
    unittest.main()
