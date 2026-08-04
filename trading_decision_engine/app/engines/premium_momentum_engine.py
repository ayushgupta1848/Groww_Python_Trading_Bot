"""PremiumMomentumEngine: velocity/acceleration/higher-highs-lows/consistency of the
ATM CE-PE premium spread over the last ~3s of rolling ticks. A rising CE-PE spread means
calls are gaining relative to puts (bullish spot pressure) and vice versa, so the spread
doubles as both a momentum measure and a directional signal. See docs/DESIGN.md §3 (row 4).
"""

from __future__ import annotations

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import PremiumMomentumResult
from ..models.market_snapshot import MarketSnapshot


class PremiumMomentumEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(self, snapshot: MarketSnapshot) -> PremiumMomentumResult:
        cfg = self._config
        ticks = snapshot.premium_history
        min_samples = cfg.premium_momentum_min_samples

        if len(ticks) < min_samples:
            return PremiumMomentumResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=(f"Insufficient premium ticks: need {min_samples}, have {len(ticks)}",),
                velocity=0.0,
                acceleration=0.0,
                higher_highs=False,
                higher_lows=False,
                consistency=0.0,
            )

        spread = [t.ce_premium - t.pe_premium for t in ticks]
        elapsed = (ticks[-1].ts - ticks[0].ts).total_seconds()
        if elapsed <= 0:
            elapsed = 1e-6

        velocity = (spread[-1] - spread[0]) / elapsed

        mid = len(spread) // 2
        t1 = (ticks[mid].ts - ticks[0].ts).total_seconds() or 1e-6
        t2 = (ticks[-1].ts - ticks[mid].ts).total_seconds() or 1e-6
        velocity_first_half = (spread[mid] - spread[0]) / t1
        velocity_second_half = (spread[-1] - spread[mid]) / t2
        acceleration = velocity_second_half - velocity_first_half

        higher_highs = max(spread[mid:]) > max(spread[:mid])
        higher_lows = min(spread[mid:]) > min(spread[:mid])

        step_signs = [1 if b > a else (-1 if b < a else 0) for a, b in zip(spread, spread[1:])]
        trend_sign = 1 if velocity > 0 else (-1 if velocity < 0 else 0)
        agreeing_steps = sum(1 for s in step_signs if s == trend_sign and s != 0)
        consistency = (agreeing_steps / len(step_signs) * 100.0) if step_signs and trend_sign != 0 else 0.0

        # momentum_threshold gates whether the velocity is strong enough to call the
        # premium genuinely "rising"/"falling" rather than just noise around zero — see
        # docs/DESIGN.md §7 ("Minimum premium momentum score to consider 'rising'").
        direction = (
            Direction.BULLISH if velocity > cfg.momentum_threshold
            else (Direction.BEARISH if velocity < -cfg.momentum_threshold else Direction.NEUTRAL)
        )

        # Optional stricter gates (both disabled at 0, preserving original behaviour):
        # a real move should not be decelerating hard against its own direction, and
        # should have a minimum share of ticks agreeing with it.
        if direction != Direction.NEUTRAL and cfg.momentum_min_acceleration > 0:
            decelerating = (direction == Direction.BULLISH and acceleration < -cfg.momentum_min_acceleration) or (
                direction == Direction.BEARISH and acceleration > cfg.momentum_min_acceleration
            )
            if decelerating:
                direction = Direction.NEUTRAL
        if direction != Direction.NEUTRAL and cfg.momentum_min_consistency > 0 and consistency < cfg.momentum_min_consistency:
            direction = Direction.NEUTRAL

        reasons = [
            f"CE-PE spread velocity {velocity:+.2f}/s",
            f"Acceleration {acceleration:+.2f}",
            "Higher highs" if higher_highs else "No higher highs",
            "Higher lows" if higher_lows else "No higher lows",
            f"Trend consistency {consistency:.0f}%",
        ]

        score = min(100.0, abs(velocity) / cfg.premium_velocity_scale * 100.0)
        confidence = consistency

        return PremiumMomentumResult(
            direction=direction,
            score=score,
            confidence=confidence,
            reasons=tuple(reasons),
            velocity=velocity,
            acceleration=acceleration,
            higher_highs=higher_highs,
            higher_lows=higher_lows,
            consistency=consistency,
        )
