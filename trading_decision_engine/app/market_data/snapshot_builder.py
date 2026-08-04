"""SnapshotBuilder: the single, shared path both the live and replay data sources use to
turn raw ticks/candles/option-chain data into an immutable MarketSnapshot — guaranteeing
identical shape regardless of source. See docs/DESIGN.md §1, §10.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Deque

from ..models.market_snapshot import Candle, MarketSnapshot, OptionChainView, PremiumTick, SessionState

MAX_CANDLES = 300
PREMIUM_HISTORY_SECONDS = 5.0  # comfortably covers the ~3s window PremiumMomentumEngine needs


class SnapshotBuilder:
    def __init__(self) -> None:
        self._candles: Deque[Candle] = deque(maxlen=MAX_CANDLES)
        self._premium_ticks: Deque[PremiumTick] = deque()
        self._option_chain = OptionChainView(underlying_ltp=0.0, strikes={})
        self._spot = 0.0

    def update_candle(self, candle: Candle) -> None:
        self._candles.append(candle)

    def update_premium_tick(self, tick: PremiumTick) -> None:
        self._premium_ticks.append(tick)
        cutoff = tick.ts - timedelta(seconds=PREMIUM_HISTORY_SECONDS)
        while self._premium_ticks and self._premium_ticks[0].ts < cutoff:
            self._premium_ticks.popleft()

    def update_option_chain(self, chain: OptionChainView) -> None:
        self._option_chain = chain

    def update_spot(self, spot: float) -> None:
        self._spot = spot

    def build(self, timestamp: datetime, session: SessionState) -> MarketSnapshot:
        return MarketSnapshot(
            timestamp=timestamp,
            spot=self._spot,
            candles=tuple(self._candles),
            premium_history=tuple(self._premium_ticks),
            option_chain=self._option_chain,
            session=session,
        )
