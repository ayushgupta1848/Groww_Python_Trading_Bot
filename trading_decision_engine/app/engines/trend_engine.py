"""TrendEngine: EHMA direction vs long-EMA confirmation, ported from the Pine
indicator's hullColor logic (HULL > HULL[2] => bullish). Never uses EMA crossover.
Every numeric parameter (lengths, angle scale, confidence levels, score weights) comes
from StrategyConfig — see config/README.md. See docs/DESIGN.md §3 (row 1).
"""

from __future__ import annotations

import math

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import TrendResult
from ..models.market_snapshot import MarketSnapshot
from ..utils.indicator_math import ehma, ema

HULL_LOOKBACK = 2  # Pine's HULL[2] — part of the ported indicator definition, not a tunable


class TrendEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def _min_closes(self) -> int:
        # Minimum closes before a comparable pair of EHMA values exists.
        length = self._config.trend_ehma_length
        return length + HULL_LOOKBACK + round(math.sqrt(length)) + 1

    def analyze(self, snapshot: MarketSnapshot) -> TrendResult:
        cfg = self._config
        closes = [c.close for c in snapshot.candles]
        min_closes = self._min_closes()

        if len(closes) < min_closes:
            return self._neutral(f"Insufficient candle history: need {min_closes}, have {len(closes)}")

        ehma_series = ehma(closes, cfg.trend_ehma_length)
        ema_long_series = ema(closes, cfg.trend_ema_long_length)

        current_ehma = ehma_series[-1]
        prior_ehma = ehma_series[-1 - HULL_LOOKBACK]
        if current_ehma is None or prior_ehma is None:
            return self._neutral("EHMA not yet confirmable over the available history")

        reasons: list[str] = []
        ehma_rising = current_ehma > prior_ehma
        direction = Direction.BULLISH if ehma_rising else Direction.BEARISH
        reasons.append("EHMA rising" if ehma_rising else "EHMA falling")

        ema_long_value = ema_long_series[-1]
        ema_agrees = None
        if ema_long_value is not None:
            price_above = closes[-1] > ema_long_value
            ema_agrees = price_above == ehma_rising
            reasons.append(
                "EMA%d confirms (price %s)" % (cfg.trend_ema_long_length, "above" if price_above else "below")
                if ema_agrees
                else f"EMA{cfg.trend_ema_long_length} disagrees with EHMA direction"
            )
        else:
            reasons.append(f"EMA{cfg.trend_ema_long_length} unavailable: need {cfg.trend_ema_long_length} closes, have {len(closes)}")

        # Trend angle: slope of EHMA over the lookback, expressed in degrees.
        # trend_angle_scale calibrates how a realistic index move (tens of basis points,
        # not tens of percent) maps to a meaningful angle: atan(0.3% x 300) ~= 42 deg.
        angle_start = max(0, len(ehma_series) - 1 - cfg.trend_angle_lookback_bars)
        angle_start_value = ehma_series[angle_start]
        trend_angle = 0.0
        if angle_start_value not in (None, 0):
            slope_pct = (current_ehma - angle_start_value) / angle_start_value
            trend_angle = math.degrees(math.atan(slope_pct * cfg.trend_angle_scale))

        if cfg.trend_min_angle > 0 and abs(trend_angle) < cfg.trend_min_angle:
            direction = Direction.NEUTRAL
            reasons.append(f"Angle {trend_angle:.1f}° below minimum {cfg.trend_min_angle:.1f}° — treated as flat")

        trend_strength = min(100.0, abs(trend_angle) / 90.0 * 100.0)

        if ema_agrees is None:
            confidence = cfg.trend_confidence_ema_unavailable
        elif ema_agrees:
            confidence = cfg.trend_confidence_ema_agrees
        else:
            confidence = cfg.trend_confidence_ema_disagrees
        score = min(100.0, trend_strength * cfg.trend_score_strength_weight + confidence * cfg.trend_score_confidence_weight)

        return TrendResult(
            direction=direction,
            score=score,
            confidence=confidence,
            reasons=tuple(reasons),
            ehma_value=current_ehma,
            ema100_value=ema_long_value if ema_long_value is not None else 0.0,
            trend_angle=trend_angle,
            trend_strength=trend_strength,
        )

    def _neutral(self, reason: str) -> TrendResult:
        return TrendResult(
            direction=Direction.NEUTRAL,
            score=0.0,
            confidence=0.0,
            reasons=(reason,),
            ehma_value=0.0,
            ema100_value=0.0,
            trend_angle=0.0,
            trend_strength=0.0,
        )
