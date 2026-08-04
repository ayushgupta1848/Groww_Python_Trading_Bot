"""MarketStrengthEngine: momentum, acceleration, candle speed, range expansion,
consolidation, and trend confidence from the recent candle window. See
docs/DESIGN.md §3 (row 7).
"""

from __future__ import annotations

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import MarketStrengthResult
from ..models.market_snapshot import MarketSnapshot

# Convenience default for tests/fixtures — mirrors StrategyConfig's default so fixture
# sizing stays in sync automatically; the engine itself always reads live config below.
MIN_CANDLES = StrategyConfig().market_strength_window * 2


class MarketStrengthEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(self, snapshot: MarketSnapshot) -> MarketStrengthResult:
        window = self._config.market_strength_window
        min_candles = window * 2
        candles = snapshot.candles

        if len(candles) < min_candles:
            return MarketStrengthResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=(f"Insufficient candle history: need {min_candles}, have {len(candles)}",),
                candle_speed=0.0,
                range_expansion=0.0,
                consolidation_score=0.0,
                trend_confidence=0.0,
            )

        closes = [c.close for c in candles]
        recent = candles[-window:]
        prior = candles[-2 * window : -window]

        deltas = [abs(b.close - a.close) for a, b in zip(recent, recent[1:])]
        candle_speed = sum(deltas) / len(deltas) if deltas else 0.0

        recent_avg_range = sum(c.high - c.low for c in recent) / len(recent)
        prior_avg_range = sum(c.high - c.low for c in prior) / len(prior) if prior else 0.0
        range_expansion = (recent_avg_range / prior_avg_range) if prior_avg_range > 0 else 1.0

        consolidation_score = max(0.0, min(100.0, 100.0 - (range_expansion - 0.5) * 100.0))

        net_change = closes[-1] - closes[-window]
        direction = Direction.BULLISH if net_change > 0 else (Direction.BEARISH if net_change < 0 else Direction.NEUTRAL)

        bullish_candles = sum(1 for c in recent if c.close > c.open)
        bearish_candles = sum(1 for c in recent if c.close < c.open)
        agreeing = bullish_candles if direction == Direction.BULLISH else bearish_candles
        trend_confidence = (agreeing / len(recent) * 100.0) if direction != Direction.NEUTRAL else 0.0

        reasons = (
            f"Candle speed {candle_speed:.2f} pts/candle",
            f"Range expansion ratio {range_expansion:.2f}",
            "Consolidating" if consolidation_score >= self._config.market_strength_consolidation_threshold else "Not consolidating",
            f"Trend confidence {trend_confidence:.0f}% of last {window} candles agree",
        )

        return MarketStrengthResult(
            direction=direction,
            score=trend_confidence,
            confidence=trend_confidence,
            reasons=reasons,
            candle_speed=candle_speed,
            range_expansion=range_expansion,
            consolidation_score=consolidation_score,
            trend_confidence=trend_confidence,
        )
