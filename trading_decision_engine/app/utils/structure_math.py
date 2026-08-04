"""Swing-point / market-structure math backing MarketStructureEngine: HH/HL/LH/LL
classification, double top/bottom, compression/expansion, and trend exhaustion.
See docs/DESIGN.md §3 (row 2) and §14 (Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .indicator_math import pivot_high_flags, pivot_low_flags


@dataclass(frozen=True)
class StructureAnalysis:
    structure: str  # one of the MarketStructure enum values, kept as str to avoid a config import here
    strength: float  # 0-100
    reasons: tuple[str, ...]


def swing_points(values: Sequence[float], left: int, right: int, kind: str) -> list[tuple[int, float]]:
    """Confirmed swing highs/lows as (index, value) pairs, oldest first."""
    flags = pivot_high_flags(values, left, right) if kind == "high" else pivot_low_flags(values, left, right)
    return [(i, v) for i, (v, flag) in enumerate(zip(values, flags)) if flag]


def _range_avg(highs: Sequence[float], lows: Sequence[float], start: int, end: int) -> float:
    window = [h - l for h, l in zip(highs[start:end], lows[start:end])]
    return sum(window) / len(window) if window else 0.0


def classify_market_structure(
    highs: Sequence[float],
    lows: Sequence[float],
    left: int = 3,
    right: int = 3,
    double_tolerance_pct: float = 0.15,
    compression_lookback: int = 20,
    compression_ratio: float = 0.6,
    expansion_ratio: float = 1.6,
) -> StructureAnalysis:
    reasons: list[str] = []
    swing_highs = swing_points(highs, left, right, "high")
    swing_lows = swing_points(lows, left, right, "low")

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return StructureAnalysis("SIDEWAYS", 20.0, ("Insufficient swing history to classify structure",))

    (_, sh_prev), (_, sh_last) = swing_highs[-2], swing_highs[-1]
    (_, sl_prev), (_, sl_last) = swing_lows[-2], swing_lows[-1]

    higher_high = sh_last > sh_prev
    higher_low = sl_last > sl_prev
    lower_high = sh_last < sh_prev
    lower_low = sl_last < sl_prev

    # Double top/bottom: two comparable-height swing highs/lows within tolerance.
    top_spread_pct = abs(sh_last - sh_prev) / sh_prev * 100 if sh_prev else 100.0
    bottom_spread_pct = abs(sl_last - sl_prev) / sl_prev * 100 if sl_prev else 100.0

    if top_spread_pct <= double_tolerance_pct * 100 and not higher_low:
        strength = max(0.0, 100.0 - top_spread_pct * 10)
        reasons.append(f"Double top: swing highs {sh_prev:.2f} and {sh_last:.2f} within tolerance")
        return StructureAnalysis("DOUBLE_TOP", strength, tuple(reasons))

    if bottom_spread_pct <= double_tolerance_pct * 100 and not lower_high:
        strength = max(0.0, 100.0 - bottom_spread_pct * 10)
        reasons.append(f"Double bottom: swing lows {sl_prev:.2f} and {sl_last:.2f} within tolerance")
        return StructureAnalysis("DOUBLE_BOTTOM", strength, tuple(reasons))

    if higher_high and higher_low:
        move_pct = (sh_last - sh_prev) / sh_prev * 100 if sh_prev else 0.0
        strength = min(100.0, 50.0 + move_pct * 5)
        reasons.append(f"Higher high {sh_last:.2f} > {sh_prev:.2f}, higher low {sl_last:.2f} > {sl_prev:.2f}")
        return StructureAnalysis("HH_HL", strength, tuple(reasons))

    if lower_high and lower_low:
        move_pct = (sh_prev - sh_last) / sh_prev * 100 if sh_prev else 0.0
        strength = min(100.0, 50.0 + move_pct * 5)
        reasons.append(f"Lower high {sh_last:.2f} < {sh_prev:.2f}, lower low {sl_last:.2f} < {sl_prev:.2f}")
        return StructureAnalysis("LH_LL", strength, tuple(reasons))

    # Mixed swings — fall back to a volatility-regime read (compression/expansion) if
    # there's enough candle history, else plain sideways.
    if len(highs) >= compression_lookback * 2:
        recent_avg = _range_avg(highs, lows, len(highs) - compression_lookback, len(highs))
        prior_avg = _range_avg(highs, lows, len(highs) - 2 * compression_lookback, len(highs) - compression_lookback)
        if prior_avg > 0:
            ratio = recent_avg / prior_avg
            if ratio <= compression_ratio:
                reasons.append(f"Range compressing: recent/prior range ratio {ratio:.2f}")
                return StructureAnalysis("COMPRESSION", min(100.0, (1 - ratio) * 100), tuple(reasons))
            if ratio >= expansion_ratio:
                reasons.append(f"Range expanding: recent/prior range ratio {ratio:.2f}")
                return StructureAnalysis("EXPANSION", min(100.0, (ratio - 1) * 60), tuple(reasons))

    reasons.append("Mixed swing structure, no clear trend or volatility regime")
    return StructureAnalysis("SIDEWAYS", 30.0, tuple(reasons))


def detect_exhaustion(
    values: Sequence[float], left: int, right: int, kind: str, lookback_bars: int = 5
) -> tuple[bool, float]:
    """Momentum divergence at swing extremes: the latest swing makes a new price extreme
    but the approach to it was slower (smaller per-bar move) than the prior swing's
    approach — a classic exhaustion signal. Returns (exhausted, strength 0-100).
    """
    points = swing_points(values, left, right, kind)
    if len(points) < 2:
        return False, 0.0
    (idx_prev, val_prev), (idx_last, val_last) = points[-2], points[-1]

    made_new_extreme = val_last > val_prev if kind == "high" else val_last < val_prev
    if not made_new_extreme:
        return False, 0.0

    start_prev = max(0, idx_prev - lookback_bars)
    start_last = max(0, idx_last - lookback_bars)
    momentum_prev = (val_prev - values[start_prev]) / max(1, idx_prev - start_prev)
    momentum_last = (val_last - values[start_last]) / max(1, idx_last - start_last)

    if kind == "high":
        weaker = momentum_last < momentum_prev
    else:
        weaker = momentum_last > momentum_prev  # for lows, "weaker" means less negative

    if not weaker or momentum_prev == 0:
        return False, 0.0
    strength = min(100.0, abs(1 - momentum_last / momentum_prev) * 100)
    return True, strength
