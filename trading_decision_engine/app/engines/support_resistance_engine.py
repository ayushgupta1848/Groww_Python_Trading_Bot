"""SupportResistanceEngine: a faithful, math-only port of
reference/tw_all_in_one_indicator.pine's pivot/valuewhen "Target & Stop Loss" section
(left=33, right=21, quick_right=3, src="Close"). Plotting/coloring/shape logic is not
ported — only the computation of Level1-Level14 and the resulting nearest support and
resistance. See docs/DESIGN.md §3 (row 3).

Level9/Level10 and Level13/Level14 are intentionally identical formulas (both derived
from `pivot_lows`) because that is exactly what the source .pine script computes —
preserved here rather than "fixed", per the instruction to translate the indicator's
mathematical logic exactly.
"""

from __future__ import annotations

from typing import Sequence

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import SupportResistanceResult
from ..models.market_snapshot import MarketSnapshot
from ..utils.indicator_math import pivot_high_flags, pivot_low_flags, shifted, value_when

# Defaults mirroring the source .pine (left=33, right=21, quick_right=3) — kept for
# tests/fixtures; the engine reads live values from StrategyConfig (sr_pivot_left,
# sr_pivot_right, sr_quick_pivot_right).
LEFT = StrategyConfig().sr_pivot_left
RIGHT = StrategyConfig().sr_pivot_right
QUICK_RIGHT = StrategyConfig().sr_quick_pivot_right
MIN_CANDLES = LEFT + RIGHT + 1


def _confirmed_flags(base_flags: Sequence[bool], offset: int) -> list[bool]:
    """Pine's pivothigh/pivotlow series is non-na `offset` bars AFTER the pivot bar
    itself (once the `right`-bar lookahead is satisfied) — shift the pivot's own index
    forward by `offset` so it aligns with valuewhen's search-from-the-current-bar view.
    """
    n = len(base_flags)
    out = [False] * n
    for i in range(n):
        if i - offset >= 0:
            out[i] = base_flags[i - offset]
    return out


class SupportResistanceEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(self, snapshot: MarketSnapshot) -> SupportResistanceResult:
        cfg = self._config
        left, right, quick_right = cfg.sr_pivot_left, cfg.sr_pivot_right, cfg.sr_quick_pivot_right
        min_candles = left + right + 1
        closes = [c.close for c in snapshot.candles]
        spot = snapshot.spot

        if len(closes) < min_candles:
            return SupportResistanceResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=(f"Insufficient candle history: need {min_candles}, have {len(closes)}",),
                levels=(),
                nearest_support=0.0,
                nearest_resistance=0.0,
                distance_to_support=0.0,
                distance_to_resistance=0.0,
                breakout=False,
                breakdown=False,
            )

        pivot_high_confirmed = _confirmed_flags(pivot_high_flags(closes, left, right), right)
        pivot_low_confirmed = _confirmed_flags(pivot_low_flags(closes, left, right), right)
        quick_pivot_high_confirmed = _confirmed_flags(pivot_high_flags(closes, left, quick_right), quick_right)
        quick_pivot_low_confirmed = _confirmed_flags(pivot_low_flags(closes, left, quick_right), quick_right)

        close_shift_right = shifted(closes, right)
        close_shift_quick = shifted(closes, quick_right)

        level1 = value_when(quick_pivot_high_confirmed, close_shift_quick, 0)
        level2 = value_when(quick_pivot_low_confirmed, close_shift_quick, 0)
        level3 = value_when(pivot_high_confirmed, close_shift_right, 0)
        level4 = value_when(pivot_low_confirmed, close_shift_right, 0)
        level5 = value_when(pivot_high_confirmed, close_shift_right, 1)
        level6 = value_when(pivot_low_confirmed, close_shift_right, 1)
        level7 = value_when(pivot_high_confirmed, close_shift_right, 2)
        level8 = value_when(pivot_low_confirmed, close_shift_right, 2)
        level9 = value_when(pivot_low_confirmed, close_shift_right, 3)
        level10 = value_when(pivot_low_confirmed, close_shift_right, 3)  # duplicate of level9, per source
        level11 = value_when(pivot_high_confirmed, close_shift_right, 4)
        level12 = value_when(pivot_low_confirmed, close_shift_right, 4)
        level13 = value_when(pivot_low_confirmed, close_shift_right, 5)
        level14 = value_when(pivot_low_confirmed, close_shift_right, 5)  # duplicate of level13, per source

        all_levels = (
            level1, level2, level3, level4, level5, level6, level7,
            level8, level9, level10, level11, level12, level13, level14,
        )
        resolved_levels = [lvl for lvl in all_levels if lvl is not None]

        resistance_candidates = [lvl for lvl in resolved_levels if lvl > spot]
        support_candidates = [lvl for lvl in resolved_levels if lvl < spot]

        # No level on a given side means UNBOUNDED room on that side (price above every
        # known resistance / below every known support — e.g. a fresh breakout), not
        # zero room: nearest stays at spot for reporting, but the distance must read as
        # wide-open or the Decision Engine's min-room eligibility check would block
        # entries at exactly the moments the strategy is designed to catch. Only when
        # NO levels are resolved at all (insufficient pivot history) do both distances
        # stay 0.0 — "unknown", which correctly fails the room check until pivots form.
        nearest_resistance = min(resistance_candidates) if resistance_candidates else spot
        nearest_support = max(support_candidates) if support_candidates else spot

        if resolved_levels:
            distance_to_resistance = (nearest_resistance - spot) if resistance_candidates else float("inf")
            distance_to_support = (spot - nearest_support) if support_candidates else float("inf")
        else:
            distance_to_resistance = 0.0
            distance_to_support = 0.0

        reasons = [
            f"{len(resolved_levels)}/14 pivot levels resolved",
            f"Nearest support {nearest_support:.2f} ({distance_to_support:.2f} away)"
            if support_candidates
            else "No support level below spot yet",
            f"Nearest resistance {nearest_resistance:.2f} ({distance_to_resistance:.2f} away)"
            if resistance_candidates
            else "No resistance level above spot yet",
        ]

        # Breakout/breakdown per the spec's "if resistance breaks -> Breakout=True":
        # compared against the MOST RECENTLY formed levels (level1 = latest quick pivot
        # high, level2 = latest quick pivot low), not against nearest_resistance —
        # which is by definition still above spot and therefore can never be "broken".
        # A buffer > 0 demands spot clears the level by that many extra points, filtering
        # marginal one-tick pokes through it.
        buffer = cfg.sr_breakout_buffer_points
        breakout = level1 is not None and spot > level1 + buffer
        breakdown = level2 is not None and spot < level2 - buffer

        if not resistance_candidates or not support_candidates:
            direction = Direction.NEUTRAL
        elif distance_to_resistance > distance_to_support:
            direction = Direction.BULLISH
        elif distance_to_support > distance_to_resistance:
            direction = Direction.BEARISH
        else:
            direction = Direction.NEUTRAL

        total_room = distance_to_resistance + distance_to_support
        if total_room > 0 and resistance_candidates and support_candidates:
            score = (
                distance_to_resistance / total_room * 100.0
                if direction == Direction.BULLISH
                else distance_to_support / total_room * 100.0
            )
        else:
            score = 0.0
        confidence = min(100.0, len(resolved_levels) / 14 * 100.0)

        return SupportResistanceResult(
            direction=direction,
            score=score,
            confidence=confidence,
            reasons=tuple(reasons),
            levels=all_levels,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            distance_to_support=distance_to_support,
            distance_to_resistance=distance_to_resistance,
            breakout=breakout,
            breakdown=breakdown,
        )
