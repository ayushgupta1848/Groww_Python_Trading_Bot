"""MarketStructureEngine: HH/HL/LH/LL, double top/bottom, sideways range,
compression/expansion, and trend exhaustion from swing points. See docs/DESIGN.md §3
(row 2).
"""

from __future__ import annotations

from ..config.constants import Direction, MarketStructure
from ..config.strategy import StrategyConfig
from ..models.engine_results import MarketStructureResult
from ..models.market_snapshot import MarketSnapshot
from ..utils.structure_math import classify_market_structure, detect_exhaustion

_STRUCTURE_DIRECTION = {
    "HH_HL": Direction.BULLISH,
    "LH_LL": Direction.BEARISH,
    "DOUBLE_TOP": Direction.BEARISH,
    "DOUBLE_BOTTOM": Direction.BULLISH,
    "SIDEWAYS": Direction.NEUTRAL,
    "COMPRESSION": Direction.NEUTRAL,
    "EXPANSION": Direction.NEUTRAL,
}


class MarketStructureEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(self, snapshot: MarketSnapshot) -> MarketStructureResult:
        cfg = self._config
        swing_left, swing_right = cfg.structure_swing_left, cfg.structure_swing_right
        min_candles = cfg.structure_min_candles
        highs = [c.high for c in snapshot.candles]
        lows = [c.low for c in snapshot.candles]

        if len(highs) < min_candles:
            return MarketStructureResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=(f"Insufficient candle history: need {min_candles}, have {len(highs)}",),
                structure=MarketStructure.SIDEWAYS,
                strength=0.0,
            )

        analysis = classify_market_structure(
            highs, lows, left=swing_left, right=swing_right,
            double_tolerance_pct=cfg.structure_double_tolerance_pct,
            compression_lookback=cfg.structure_compression_lookback,
            compression_ratio=cfg.structure_compression_ratio,
            expansion_ratio=cfg.structure_expansion_ratio,
        )
        structure_name = analysis.structure
        reasons = list(analysis.reasons)

        if structure_name == "HH_HL":
            exhausted, exhaustion_strength = detect_exhaustion(highs, swing_left, swing_right, "high")
            if exhausted and exhaustion_strength >= cfg.structure_exhaustion_threshold:
                reasons.append(f"Bullish exhaustion detected (strength {exhaustion_strength:.0f})")
                structure_name = "EXHAUSTION"
                analysis_strength = exhaustion_strength
            else:
                analysis_strength = analysis.strength
        elif structure_name == "LH_LL":
            exhausted, exhaustion_strength = detect_exhaustion(lows, swing_left, swing_right, "low")
            if exhausted and exhaustion_strength >= cfg.structure_exhaustion_threshold:
                reasons.append(f"Bearish exhaustion detected (strength {exhaustion_strength:.0f})")
                structure_name = "EXHAUSTION"
                analysis_strength = exhaustion_strength
            else:
                analysis_strength = analysis.strength
        else:
            analysis_strength = analysis.strength

        direction = (
            Direction.NEUTRAL if structure_name == "EXHAUSTION" else _STRUCTURE_DIRECTION[structure_name]
        )
        # Optional floor (0 = disabled): a weakly-formed structure gives no directional
        # opinion rather than a low-conviction one.
        if cfg.structure_min_strength > 0 and analysis_strength < cfg.structure_min_strength:
            direction = Direction.NEUTRAL
            reasons.append(f"Structure strength {analysis_strength:.0f} below minimum {cfg.structure_min_strength:.0f} — no directional call")
        structure_enum = MarketStructure(structure_name)
        confidence = analysis_strength
        score = analysis_strength

        return MarketStructureResult(
            direction=direction,
            score=score,
            confidence=confidence,
            reasons=tuple(reasons),
            structure=structure_enum,
            strength=analysis_strength,
        )
