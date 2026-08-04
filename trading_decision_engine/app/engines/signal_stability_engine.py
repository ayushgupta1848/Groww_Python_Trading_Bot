"""SignalStabilityEngine: confirms that Trend/Premium Momentum/Market Structure/
Breakout/Support-Resistance have held consistently across a confirmation window before
allowing any BUY/SELL — reproducing the habit of watching a setup hold for a few
seconds before entering. Fails safe (stable=False) on disagreement or incomplete
history. See docs/DESIGN.md §3 (row 11), §3b.

Unlike the other engines, this one does not take a MarketSnapshot: it takes a rolling
history of other engines' results, built and windowed only by the Orchestrator (the one
component allowed to know about more than one engine) — see docs/DESIGN.md §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import (
    BreakoutResult,
    MarketStructureResult,
    PremiumMomentumResult,
    SignalStabilityResult,
    SupportResistanceResult,
    TrendResult,
)
from ..utils.rolling_history import TimestampedValue


@dataclass(frozen=True)
class SignalStabilityInput:
    trend_history: tuple[TimestampedValue[TrendResult], ...]
    premium_history: tuple[TimestampedValue[PremiumMomentumResult], ...]
    structure_history: tuple[TimestampedValue[MarketStructureResult], ...]
    breakout_history: tuple[TimestampedValue[BreakoutResult], ...]
    support_resistance_history: tuple[TimestampedValue[SupportResistanceResult], ...]
    required_seconds: float
    now: datetime


def required_confirmation_seconds(
    trend: TrendResult, momentum: PremiumMomentumResult, config: StrategyConfig
) -> float:
    """Adaptive confirmation window (docs/DESIGN.md §3b): strong trend + strong
    momentum confirms fast; sideways/slow market waits longer. Computed by the
    Orchestrator each cycle and passed into SignalStabilityInput.required_seconds.
    """
    strength = (trend.trend_strength + momentum.consistency) / 2.0
    if strength >= config.signal_stability_strong_threshold:
        return config.signal_stability_min_seconds
    if strength <= config.signal_stability_weak_threshold:
        return config.signal_stability_max_seconds
    span = config.signal_stability_weak_threshold - config.signal_stability_strong_threshold
    t = (strength - config.signal_stability_strong_threshold) / span
    return config.signal_stability_min_seconds + t * (
        config.signal_stability_max_seconds - config.signal_stability_min_seconds
    )


class SignalStabilityEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def analyze(self, stability_input: SignalStabilityInput) -> SignalStabilityResult:
        required = stability_input.required_seconds

        if not stability_input.trend_history or not stability_input.premium_history:
            return SignalStabilityResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=("No trend/premium history yet",),
                stable=False,
                confirmation_seconds_elapsed=0.0,
                required_seconds=required,
            )

        target_direction = stability_input.trend_history[-1].value.direction
        if target_direction == Direction.NEUTRAL:
            return SignalStabilityResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=("No clear trend direction to confirm",),
                stable=False,
                confirmation_seconds_elapsed=0.0,
                required_seconds=required,
            )

        now = stability_input.now
        opposite_direction = Direction.BEARISH if target_direction == Direction.BULLISH else Direction.BULLISH

        # For each tracked series, find the most recent moment (if any) that disagreed
        # with target_direction, scanning the FULL recorded history (not just the
        # window) — this is the true "confirmation start" point, not just the oldest
        # sample that happens to fall inside the window.
        last_disagreement_ts: float | None = None
        earliest_recorded_ts: list[float] = []
        insufficient: list[str] = []

        for label, history, strict in (
            ("Trend", stability_input.trend_history, True),
            ("Premium Momentum", stability_input.premium_history, True),
            ("Market Structure", stability_input.structure_history, False),
            ("Breakout", stability_input.breakout_history, False),
            ("Support/Resistance", stability_input.support_resistance_history, False),
        ):
            if not history:
                insufficient.append(f"{label}: no history recorded yet")
                continue
            earliest_recorded_ts.append(history[0].ts.timestamp())
            for item in history:
                disagreed = (
                    item.value.direction != target_direction
                    if strict
                    else item.value.direction == opposite_direction
                )
                if disagreed:
                    ts = item.ts.timestamp()
                    if last_disagreement_ts is None or ts > last_disagreement_ts:
                        last_disagreement_ts = ts

        if insufficient:
            return SignalStabilityResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=tuple(insufficient),
                stable=False,
                confirmation_seconds_elapsed=0.0,
                required_seconds=required,
            )

        # The earliest point we can vouch for ALL FIVE series simultaneously is bounded
        # by whichever series has the SHORTEST recorded history (the weakest link) —
        # i.e. the latest of each series' own earliest timestamp, not the earliest.
        all_series_start = max(earliest_recorded_ts)
        confirmation_start = max(last_disagreement_ts, all_series_start) if last_disagreement_ts is not None else all_series_start
        confirmation_seconds_elapsed = now.timestamp() - confirmation_start
        window_fully_recorded = all_series_start <= now.timestamp() - required
        stable = confirmation_seconds_elapsed >= required and window_fully_recorded

        if stable:
            reasons = (
                f"Stable {target_direction.value} setup confirmed for {confirmation_seconds_elapsed:.1f}s/{required:.1f}s required",
            )
        elif last_disagreement_ts is not None:
            reasons = (f"Signal disagreed {confirmation_seconds_elapsed:.1f}s ago — needs {required:.1f}s clean",)
        else:
            reasons = (f"Only {confirmation_seconds_elapsed:.1f}s of history recorded, {required:.1f}s required",)

        return SignalStabilityResult(
            direction=target_direction if stable else Direction.NEUTRAL,
            score=min(100.0, (confirmation_seconds_elapsed / required) * 100.0) if required else 0.0,
            confidence=100.0 if stable else 0.0,
            reasons=reasons,
            stable=stable,
            confirmation_seconds_elapsed=confirmation_seconds_elapsed,
            required_seconds=required,
        )
