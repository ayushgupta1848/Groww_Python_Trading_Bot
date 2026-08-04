"""ReplayMarketDataSource: implements the same MarketDataSource protocol as the live
source, replaying a pre-built tick sequence through the identical SnapshotBuilder path
at a configurable speed. Engines and the Orchestrator cannot tell it apart from LIVE.
See docs/DESIGN.md §10.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Sequence

from ..models.market_snapshot import Candle, MarketSnapshot, OptionChainView, PremiumTick, SessionState
from .snapshot_builder import SnapshotBuilder

ReplayKind = Literal["spot", "premium", "candle", "option_chain"]


@dataclass(frozen=True)
class ReplayTick:
    ts: datetime
    kind: ReplayKind
    payload: float | PremiumTick | Candle | OptionChainView


class ReplayMarketDataSource:
    def __init__(
        self,
        ticks: Sequence[ReplayTick],
        session_provider: Callable[[], SessionState],
        speed: float = 0.0,
    ) -> None:
        """speed: 0 = as-fast-as-possible, 1.0 = real-time pacing, values in between scale proportionally."""
        self._ticks = ticks
        self._session_provider = session_provider
        self._speed = speed
        self._builder = SnapshotBuilder()
        self._stop_event = threading.Event()

    def start(self, on_snapshot: Callable[[MarketSnapshot], None]) -> None:
        prev_ts: datetime | None = None
        for tick in self._ticks:
            if self._stop_event.is_set():
                break
            if self._speed > 0 and prev_ts is not None:
                delay = (tick.ts - prev_ts).total_seconds() / self._speed
                if delay > 0:
                    time.sleep(delay)
            prev_ts = tick.ts

            if tick.kind == "spot":
                self._builder.update_spot(tick.payload)
            elif tick.kind == "premium":
                self._builder.update_premium_tick(tick.payload)
            elif tick.kind == "candle":
                self._builder.update_candle(tick.payload)
            elif tick.kind == "option_chain":
                self._builder.update_option_chain(tick.payload)

            on_snapshot(self._builder.build(tick.ts, self._session_provider()))

    def stop(self) -> None:
        self._stop_event.set()
