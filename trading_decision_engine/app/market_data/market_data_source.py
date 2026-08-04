"""MarketDataSource: the shared protocol for LIVE/SHADOW (GrowwWebSocketMarketDataSource)
and REPLAY (ReplayMarketDataSource) — the Orchestrator and every engine are unaware of
which one is active. See docs/DESIGN.md §1, §10, §11.
"""

from __future__ import annotations

from typing import Callable, Protocol

from ..models.market_snapshot import MarketSnapshot


class MarketDataSource(Protocol):
    def start(self, on_snapshot: Callable[[MarketSnapshot], None]) -> None: ...

    def stop(self) -> None: ...
