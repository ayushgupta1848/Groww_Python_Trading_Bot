"""VolatilityEngine: rejects trades on spread too high, abnormal volatility, price
spikes, gaps, and whipsaws. A gate/filter engine — direction is always NEUTRAL, it only
reports acceptable/not. See docs/DESIGN.md §3 (row 8).
"""

from __future__ import annotations

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import VolatilityResult
from ..models.market_snapshot import MarketSnapshot

# Convenience default for tests/fixtures — mirrors StrategyConfig's default so fixture
# sizing stays in sync automatically; the engine itself always reads live config below.
MIN_CANDLES = StrategyConfig().volatility_min_candles


class VolatilityEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(self, snapshot: MarketSnapshot) -> VolatilityResult:
        cfg = self._config
        candles = snapshot.candles
        min_candles = cfg.volatility_min_candles

        if len(candles) < min_candles:
            return VolatilityResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=(f"Insufficient candle history: need {min_candles}, have {len(candles)}",),
                acceptable=False,
                spread_pct=0.0,
                spike_score=0.0,
                gap_detected=False,
                whipsaw_detected=False,
            )

        reasons: list[str] = []
        violations = 0

        range_lookback = cfg.volatility_range_lookback
        recent_ranges = [c.high - c.low for c in candles[-range_lookback:]]
        avg_range = sum(recent_ranges[:-1]) / len(recent_ranges[:-1]) if len(recent_ranges) > 1 else 0.0
        latest_range = recent_ranges[-1]

        spike_multiplier = cfg.volatility_spike_multiplier
        spike_score = (latest_range / avg_range * 100.0 / spike_multiplier) if avg_range > 0 else 0.0
        spike_detected = avg_range > 0 and latest_range > avg_range * spike_multiplier
        if spike_detected:
            violations += 1
            reasons.append(f"Price spike: last candle range {latest_range:.2f} vs avg {avg_range:.2f}")

        gap = abs(candles[-1].open - candles[-2].close)
        gap_detected = avg_range > 0 and gap > avg_range * cfg.volatility_gap_multiplier
        if gap_detected:
            violations += 1
            reasons.append(f"Gap detected: {gap:.2f} vs avg range {avg_range:.2f}")

        longer_ranges = [c.high - c.low for c in candles[-2 * range_lookback : -range_lookback]]
        longer_avg_range = sum(longer_ranges) / len(longer_ranges) if longer_ranges else 0.0
        abnormal_volatility = longer_avg_range > 0 and avg_range > longer_avg_range * cfg.volatility_abnormal_multiplier
        if abnormal_volatility:
            violations += 1
            reasons.append(f"Abnormal volatility: recent avg range {avg_range:.2f} vs longer avg {longer_avg_range:.2f}")

        whipsaw_window = cfg.volatility_whipsaw_window
        recent_closes = [c.close for c in candles[-whipsaw_window - 1 :]]
        deltas = [b - a for a, b in zip(recent_closes, recent_closes[1:])]
        reversals = sum(1 for a, b in zip(deltas, deltas[1:]) if a * b < 0)
        whipsaw_detected = reversals >= cfg.volatility_whipsaw_min_reversals
        if whipsaw_detected:
            violations += 1
            reasons.append(f"Whipsaw: {reversals} directional reversals in last {whipsaw_window} candles")

        spread_pct = 0.0
        if snapshot.premium_history:
            latest_tick = snapshot.premium_history[-1]
            mid = (latest_tick.ask + latest_tick.bid) / 2.0
            if mid > 0:
                spread_pct = (latest_tick.ask - latest_tick.bid) / mid * 100.0
        spread_too_high = spread_pct > self._config.max_spread_pct
        if spread_too_high:
            violations += 1
            reasons.append(f"Spread too high: {spread_pct:.2f}% > {self._config.max_spread_pct:.2f}%")

        acceptable = violations == 0
        if acceptable:
            reasons.append("No volatility red flags")

        score = max(0.0, 100.0 - violations * cfg.volatility_violation_penalty)
        confidence = score

        return VolatilityResult(
            direction=Direction.NEUTRAL,
            score=score,
            confidence=confidence,
            reasons=tuple(reasons),
            acceptable=acceptable,
            spread_pct=spread_pct,
            spike_score=min(100.0, spike_score),
            gap_detected=gap_detected,
            whipsaw_detected=whipsaw_detected,
        )
