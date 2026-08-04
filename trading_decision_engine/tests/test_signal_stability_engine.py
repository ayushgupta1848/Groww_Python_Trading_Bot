import unittest
from datetime import datetime, timedelta

from trading_decision_engine.app.config.constants import Direction, MarketStructure
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.engines.signal_stability_engine import (
    SignalStabilityEngine,
    SignalStabilityInput,
    required_confirmation_seconds,
)
from trading_decision_engine.app.models.engine_results import (
    BreakoutResult,
    MarketStructureResult,
    PremiumMomentumResult,
    SupportResistanceResult,
    TrendResult,
)
from trading_decision_engine.app.utils.rolling_history import TimestampedValue

NOW = datetime(2026, 7, 13, 11, 0, 0)


def _trend(direction=Direction.BULLISH, strength=80.0):
    return TrendResult(direction=direction, score=strength, confidence=strength, reasons=(), ehma_value=1, ema100_value=1, trend_angle=10, trend_strength=strength)


def _momentum(direction=Direction.BULLISH, consistency=80.0):
    return PremiumMomentumResult(direction=direction, score=80, confidence=80, reasons=(), velocity=5, acceleration=1, higher_highs=True, higher_lows=True, consistency=consistency)


def _structure(direction=Direction.BULLISH):
    return MarketStructureResult(direction=direction, score=70, confidence=70, reasons=(), structure=MarketStructure.HH_HL, strength=70)


def _breakout(direction=Direction.NEUTRAL):
    return BreakoutResult(direction=direction, score=0, confidence=0, reasons=(), breakout_confirmed=False, breakdown_confirmed=False, confirmation_bars_elapsed=0)


def _sr(direction=Direction.BULLISH):
    return SupportResistanceResult(direction=direction, score=60, confidence=60, reasons=(), levels=(), nearest_support=100, nearest_resistance=200, distance_to_support=10, distance_to_resistance=20, breakout=False, breakdown=False)


def _series(value, seconds_ago_list):
    return tuple(TimestampedValue(NOW - timedelta(seconds=s), value) for s in seconds_ago_list)


class TestSignalStabilityEngine(unittest.TestCase):
    def test_fully_stable_window_is_stable(self):
        si = SignalStabilityInput(
            trend_history=_series(_trend(), [3, 2, 1, 0]),
            premium_history=_series(_momentum(), [3, 2, 1, 0]),
            structure_history=_series(_structure(), [3, 2, 1, 0]),
            breakout_history=_series(_breakout(), [3, 2, 1, 0]),
            support_resistance_history=_series(_sr(), [3, 2, 1, 0]),
            required_seconds=1.5,
            now=NOW,
        )
        result = SignalStabilityEngine().analyze(si)
        self.assertTrue(result.stable)
        self.assertGreaterEqual(result.confirmation_seconds_elapsed, 1.5)

    def test_disagreement_mid_window_breaks_stability(self):
        bad_momentum = _series(_momentum(direction=Direction.BEARISH), [1])
        good_momentum = _series(_momentum(), [3, 2, 0])
        si = SignalStabilityInput(
            trend_history=_series(_trend(), [3, 2, 1, 0]),
            premium_history=tuple(sorted(bad_momentum + good_momentum, key=lambda t: t.ts)),
            structure_history=_series(_structure(), [3, 2, 1, 0]),
            breakout_history=_series(_breakout(), [3, 2, 1, 0]),
            support_resistance_history=_series(_sr(), [3, 2, 1, 0]),
            required_seconds=1.5,
            now=NOW,
        )
        result = SignalStabilityEngine().analyze(si)
        self.assertFalse(result.stable)

    def test_insufficient_history_is_not_stable(self):
        si = SignalStabilityInput(
            trend_history=_series(_trend(), [1, 0.5, 0]),
            premium_history=_series(_momentum(), [1, 0.5, 0]),
            structure_history=_series(_structure(), [3, 2, 1, 0]),
            breakout_history=_series(_breakout(), [3, 2, 1, 0]),
            support_resistance_history=_series(_sr(), [3, 2, 1, 0]),
            required_seconds=1.5,
            now=NOW,
        )
        result = SignalStabilityEngine().analyze(si)
        self.assertFalse(result.stable)

    def test_neutral_trend_is_never_stable(self):
        si = SignalStabilityInput(
            trend_history=_series(_trend(direction=Direction.NEUTRAL), [3, 2, 1, 0]),
            premium_history=_series(_momentum(), [3, 2, 1, 0]),
            structure_history=_series(_structure(), [3, 2, 1, 0]),
            breakout_history=_series(_breakout(), [3, 2, 1, 0]),
            support_resistance_history=_series(_sr(), [3, 2, 1, 0]),
            required_seconds=1.5,
            now=NOW,
        )
        result = SignalStabilityEngine().analyze(si)
        self.assertFalse(result.stable)

    def test_required_confirmation_seconds_fast_for_strong_conditions(self):
        config = StrategyConfig()
        seconds = required_confirmation_seconds(_trend(strength=90), _momentum(consistency=90), config)
        self.assertEqual(seconds, config.signal_stability_min_seconds)

    def test_required_confirmation_seconds_slow_for_weak_conditions(self):
        config = StrategyConfig()
        seconds = required_confirmation_seconds(_trend(strength=10), _momentum(consistency=10), config)
        self.assertEqual(seconds, config.signal_stability_max_seconds)

    def test_required_confirmation_seconds_interpolates_mid_band(self):
        config = StrategyConfig()
        seconds = required_confirmation_seconds(_trend(strength=55), _momentum(consistency=55), config)
        self.assertTrue(config.signal_stability_min_seconds < seconds < config.signal_stability_max_seconds)


if __name__ == "__main__":
    unittest.main()
