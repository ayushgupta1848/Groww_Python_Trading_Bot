"""BreakoutEngine: near-resistance/near-support observation -> confirmed
breakout/breakdown once price clears the level and HOLDS for
`breakout_confirmation_bars` consecutive closed candles. Takes the
SupportResistanceResult as an explicit second input (per the original architecture
spec) rather than recomputing levels itself. See docs/DESIGN.md §3 (row 6).

Confirmation is derived purely from the candle history already in the snapshot (a
consecutive-closes-beyond-the-level count) so the engine stays a pure function of its
inputs with no engine-held state.
"""

from __future__ import annotations

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import BreakoutResult, SupportResistanceResult
from ..models.market_snapshot import MarketSnapshot


class BreakoutEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(self, snapshot: MarketSnapshot, support_resistance: SupportResistanceResult) -> BreakoutResult:
        cfg = self._config
        closes = [c.close for c in snapshot.candles]

        if not closes or support_resistance.confidence == 0.0:
            return BreakoutResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=("Support/Resistance levels not yet resolved",),
                breakout_confirmed=False,
                breakdown_confirmed=False,
                confirmation_bars_elapsed=0,
            )

        # The level being broken is the most recently FORMED one (level1 = latest quick
        # pivot high, level2 = latest quick pivot low — first two entries of the Pine
        # port's levels tuple), not nearest_resistance/support, which sit beyond spot
        # by construction and therefore can never have consecutive closes past them.
        levels = support_resistance.levels
        resistance_level = levels[0] if levels and levels[0] is not None else support_resistance.nearest_resistance
        support_level = levels[1] if len(levels) > 1 and levels[1] is not None else support_resistance.nearest_support
        buffer = cfg.breakout_buffer_points

        count_above = 0
        for close in reversed(closes):
            if close > resistance_level + buffer:
                count_above += 1
            else:
                break

        count_below = 0
        for close in reversed(closes):
            if close < support_level - buffer:
                count_below += 1
            else:
                break

        required = cfg.breakout_confirmation_bars
        breakout_confirmed = count_above >= required
        breakdown_confirmed = count_below >= required

        if breakout_confirmed:
            direction = Direction.BULLISH
            bars_elapsed = count_above
            reasons = (f"Breakout confirmed: {count_above} closes above resistance {resistance_level:.2f}",)
        elif breakdown_confirmed:
            direction = Direction.BEARISH
            bars_elapsed = count_below
            reasons = (f"Breakdown confirmed: {count_below} closes below support {support_level:.2f}",)
        elif count_above > 0:
            direction = Direction.NEUTRAL
            bars_elapsed = count_above
            reasons = (f"Near resistance, {count_above}/{required} closes above — awaiting confirmation",)
        elif count_below > 0:
            direction = Direction.NEUTRAL
            bars_elapsed = count_below
            reasons = (f"Near support, {count_below}/{required} closes below — awaiting confirmation",)
        else:
            direction = Direction.NEUTRAL
            bars_elapsed = 0
            reasons = ("Price is between support and resistance, no breakout observed",)

        score = min(100.0, (bars_elapsed / required) * 100.0) if required else 0.0
        confidence = score if (breakout_confirmed or breakdown_confirmed) else score * 0.5

        return BreakoutResult(
            direction=direction,
            score=score,
            confidence=confidence,
            reasons=reasons,
            breakout_confirmed=breakout_confirmed,
            breakdown_confirmed=breakdown_confirmed,
            confirmation_bars_elapsed=bars_elapsed,
        )
