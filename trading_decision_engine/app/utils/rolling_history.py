"""Bounded, time-windowed history of arbitrary values, used by the Orchestrator to keep
a rolling window of Trend/Premium/Structure/Breakout/S-R results for the Signal
Stability Engine. See docs/DESIGN.md §3a/§3b.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class TimestampedValue(Generic[T]):
    ts: datetime
    value: T


class RollingHistory(Generic[T]):
    def __init__(self, max_age_seconds: float) -> None:
        self._max_age = timedelta(seconds=max_age_seconds)
        self._items: Deque[TimestampedValue[T]] = deque()

    def append(self, ts: datetime, value: T) -> None:
        self._items.append(TimestampedValue(ts, value))
        self._prune(ts)

    def _prune(self, now: datetime) -> None:
        while self._items and (now - self._items[0].ts) > self._max_age:
            self._items.popleft()

    def window(self, now: datetime, seconds: float) -> tuple[TimestampedValue[T], ...]:
        cutoff = now - timedelta(seconds=seconds)
        return tuple(item for item in self._items if item.ts >= cutoff)

    def __len__(self) -> int:
        return len(self._items)
