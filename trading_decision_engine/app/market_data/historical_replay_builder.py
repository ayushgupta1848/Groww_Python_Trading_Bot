"""HistoricalReplayBuilder: reconstructs a ReplayTick sequence from historical candles
(and, where available, option-chain snapshots) for backtesting. Real tick-by-tick
historical option premiums aren't available from the Groww historical-candles API, so
premium ticks are synthesized from candle-close moves scaled by a nominal delta — a
documented simplification for offline replay/backtesting, never used in LIVE/SHADOW.
See docs/DESIGN.md §10.

Premium sub-ticks are synthesized at PREMIUM_SUBTICK_SECONDS granularity WITHIN each
candle (not once per candle) — PremiumMomentumEngine needs several samples inside a
rolling few-second window, which a single tick per 1-minute candle could never satisfy.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..broker.groww_execution_adapter import GrowwExecutionAdapter
from ..config.constants import STRIKE_STEP, Index
from ..models.market_snapshot import OptionChainView, PremiumTick
from .replay_source import ReplayTick

NOMINAL_DELTA = 0.5
NOMINAL_BASE_PREMIUM = 100.0
NOMINAL_SPREAD = 0.5
PREMIUM_SUBTICK_SECONDS = 1.0


class HistoricalReplayBuilder:
    def __init__(self, adapter: GrowwExecutionAdapter) -> None:
        self._adapter = adapter

    def build(
        self, index: str, expiry_date: str, candle_interval: str, start: datetime, end: datetime
    ) -> list[ReplayTick]:
        candles = self._adapter.get_historical_candles(index, candle_interval, start, end)
        if not candles:
            return []

        ticks: list[ReplayTick] = []
        try:
            option_chain = self._adapter.get_option_chain(index, expiry_date)
        except Exception:  # noqa: BLE001
            option_chain = OptionChainView(underlying_ltp=candles[0].close, strikes={})
        ticks.append(ReplayTick(ts=candles[0].ts, kind="option_chain", payload=option_chain))

        atm_strike = round(candles[0].close / STRIKE_STEP[Index(index)]) * STRIKE_STEP[Index(index)]
        ce_premium = NOMINAL_BASE_PREMIUM + max(0.0, (candles[0].close - atm_strike)) * NOMINAL_DELTA
        pe_premium = NOMINAL_BASE_PREMIUM + max(0.0, (atm_strike - candles[0].close)) * NOMINAL_DELTA

        for prev_candle, candle in zip(candles, candles[1:]):
            ticks.append(ReplayTick(ts=candle.ts, kind="candle", payload=candle))

            span_seconds = max(1.0, (candle.ts - prev_candle.ts).total_seconds())
            subtick_count = max(1, int(span_seconds // PREMIUM_SUBTICK_SECONDS))
            for i in range(1, subtick_count + 1):
                sub_ts = prev_candle.ts + timedelta(seconds=i * PREMIUM_SUBTICK_SECONDS)
                progress = i / subtick_count
                sub_price = prev_candle.close + (candle.close - prev_candle.close) * progress
                ticks.append(ReplayTick(ts=sub_ts, kind="spot", payload=sub_price))

                price_change = sub_price - prev_candle.close
                sub_ce_premium = max(0.05, ce_premium + price_change * NOMINAL_DELTA)
                sub_pe_premium = max(0.05, pe_premium - price_change * NOMINAL_DELTA)
                ticks.append(
                    ReplayTick(
                        ts=sub_ts,
                        kind="premium",
                        payload=PremiumTick(
                            ts=sub_ts,
                            ce_premium=sub_ce_premium,
                            pe_premium=sub_pe_premium,
                            bid=sub_ce_premium - NOMINAL_SPREAD,
                            ask=sub_ce_premium + NOMINAL_SPREAD,
                        ),
                    )
                )
            ce_premium, pe_premium = sub_ce_premium, sub_pe_premium

        # Candle ticks and their sub-ticks are appended in construction order, not
        # strict timestamp order (a candle's own ts can tie with or precede its last
        # sub-tick) — ReplayMarketDataSource/SnapshotBuilder require monotonic-by-ts
        # delivery, so enforce it here once rather than relying on caller ordering.
        ticks.sort(key=lambda t: t.ts)
        return ticks
