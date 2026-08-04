"""GrowwWebSocketMarketDataSource: LIVE/SHADOW data source. The Groww WebSocket tick
(via GrowwExecutionAdapter.start_feed / GrowwFeed) is the only heartbeat — no polling
loop drives the pipeline. Option chain and 1-minute candles have no push feed, so they
are refreshed on a bounded interval here (in the data layer, never inside an engine),
and merged into the very next tick-triggered snapshot rather than running their own
decision cadence. See docs/DESIGN.md §1, §11.

Tracks the current at-the-money (ATM) CE/PE premium, per the original Market Data
Aggregator spec ("ATM CE Premium, ATM PE Premium") — not necessarily whatever strike
OptionSelectionEngine most recently favoured. When the ATM strike drifts (spot moves
enough that a different strike becomes ATM), the CE/PE feed subscriptions are swapped
via GrowwExecutionAdapter.resubscribe rather than tearing down the whole connection.

GrowwFeed keys its LTP/depth responses by exchange_token, not trading symbol — matching
is done against InstrumentRef.exchange_token, resolved once via InstrumentMaster.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from ..broker.groww_execution_adapter import GrowwExecutionAdapter, TickEvent
from ..broker.instrument_master import InstrumentMaster, InstrumentRef
from ..config.constants import STRIKE_STEP, Index, is_market_open_time
from ..config.strategy import StrategyConfig
from ..models.market_snapshot import MarketSnapshot, PremiumTick, SessionState
from .snapshot_builder import SnapshotBuilder

logger = logging.getLogger("trading_decision_engine.market_data")

# A one-off retry while first establishing the socket is normal (the underlying NATS
# client auto-reconnects). What matters is a stall AFTER ticks have started flowing —
# e.g. mid-session while a trade is open — which this watchdog catches independently of
# whatever the SDK itself chooses to log. Outside market hours, zero ticks is expected
# (Groww simply has nothing to push), so that case gets a calm periodic heartbeat
# instead of an alarming WARNING — otherwise "no ticks" is indistinguishable from
# "market's shut" vs. "something's actually wrong."
FEED_STALL_WARNING_SECONDS = 10.0
# Before the FIRST tick arrives, silence is expected: the NATS handshake alone takes
# 10-25s (observed live). No stall is declared inside this window — recovery actions
# against a still-connecting feed corrupt the SDK's state.
STARTUP_GRACE_SECONDS = 90.0
FEED_STALL_CHECK_INTERVAL_SECONDS = 5.0
IDLE_HEARTBEAT_SECONDS = 60.0
# Bounded exponential backoff between feed re-subscription attempts while stalled — the
# underlying NATS client already auto-reconnects at the transport level (see module
# docstring), so this is a second-line recovery for the case where the socket itself is
# alive but the subscription stopped producing ticks. See docs/DESIGN.md §9.
RECONNECT_BASE_INTERVAL_SECONDS = 5.0
RECONNECT_MAX_INTERVAL_SECONDS = 60.0


class GrowwWebSocketMarketDataSource:
    def __init__(
        self,
        adapter: GrowwExecutionAdapter,
        config: StrategyConfig,
        index: str,
        expiry_date: str,
        session_provider: Callable[[], SessionState],
        instrument_master: InstrumentMaster | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._index = index
        self._expiry_date = expiry_date
        self._session_provider = session_provider
        self._instruments = instrument_master or InstrumentMaster()
        self._builder = SnapshotBuilder()
        self._on_snapshot: Callable[[MarketSnapshot], None] | None = None
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

        self._index_ref: InstrumentRef = self._instruments.index_instrument(index)
        self._atm_ce_ref: InstrumentRef | None = None
        self._atm_pe_ref: InstrumentRef | None = None
        self._latest_ce_ltp: float | None = None
        self._latest_pe_ltp: float | None = None
        self._latest_ce_bidask: tuple[float, float] = (0.0, 0.0)
        self._latest_pe_bidask: tuple[float, float] = (0.0, 0.0)
        self._last_tick_wall_ts: datetime | None = None
        self._feed_stalled = False
        self._last_idle_heartbeat_ts: datetime | None = None
        self._started_at: datetime | None = None
        self._next_reconnect_attempt_ts: datetime | None = None
        self._reconnect_backoff_seconds = RECONNECT_BASE_INTERVAL_SECONDS

    def start(self, on_snapshot: Callable[[MarketSnapshot], None]) -> None:
        self._on_snapshot = on_snapshot
        self._started_at = datetime.now()
        self._adapter.login()
        self._refresh_option_chain()
        self._backfill_candles()

        self._threads = [
            threading.Thread(target=self._option_chain_loop, daemon=True),
            threading.Thread(target=self._candle_loop, daemon=True),
            threading.Thread(target=self._stall_watchdog_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

        self._adapter.start_feed(self._tracked_instruments(), on_tick=self._on_tick)

    def stop(self) -> None:
        self._stop_event.set()
        self._adapter.stop_feed()

    # ------------------------------------------------------------------ tick handling
    def _on_tick(self, tick: TickEvent) -> None:
        now_wall = datetime.now()
        if self._feed_stalled:
            silent_for = (now_wall - self._last_tick_wall_ts).total_seconds() if self._last_tick_wall_ts else 0.0
            logger.info("Feed recovered after %.0fs of silence", silent_for)
            self._feed_stalled = False
            self._next_reconnect_attempt_ts = None
            self._reconnect_backoff_seconds = RECONNECT_BASE_INTERVAL_SECONDS
            self._adapter.mark_feed_connected()
        self._last_tick_wall_ts = now_wall

        if tick.ltp is None:
            return
        now = tick.ts
        changed = False

        if tick.exchange_token == self._index_ref.exchange_token:
            self._builder.update_spot(tick.ltp)
            changed = True
        elif self._atm_ce_ref is not None and tick.exchange_token == self._atm_ce_ref.exchange_token:
            self._latest_ce_ltp = tick.ltp
            if tick.bid is not None and tick.ask is not None:
                self._latest_ce_bidask = (tick.bid, tick.ask)
            changed = True
        elif self._atm_pe_ref is not None and tick.exchange_token == self._atm_pe_ref.exchange_token:
            self._latest_pe_ltp = tick.ltp
            if tick.bid is not None and tick.ask is not None:
                self._latest_pe_bidask = (tick.bid, tick.ask)
            changed = True

        if not changed:
            return

        if self._latest_ce_ltp is not None and self._latest_pe_ltp is not None:
            ce_bid, ce_ask = self._latest_ce_bidask if self._latest_ce_bidask != (0.0, 0.0) else (self._latest_ce_ltp, self._latest_ce_ltp)
            self._builder.update_premium_tick(
                PremiumTick(ts=now, ce_premium=self._latest_ce_ltp, pe_premium=self._latest_pe_ltp, bid=ce_bid, ask=ce_ask)
            )

        if self._on_snapshot is not None:
            self._on_snapshot(self._builder.build(now, self._session_provider()))

    # ------------------------------------------------------------------ bounded pulls
    def _atm_strike(self, spot: float) -> float:
        step = STRIKE_STEP[Index(self._index)]
        return round(spot / step) * step

    def _refresh_option_chain(self) -> None:
        try:
            chain = self._adapter.get_option_chain(self._index, self._expiry_date)
            self._builder.update_option_chain(chain)
            atm_strike = self._atm_strike(chain.underlying_ltp or self._atm_strike(0))
            legs = chain.strikes.get(atm_strike, {})
            ce_leg, pe_leg = legs.get("CE"), legs.get("PE")

            new_ce_ref = self._instruments.resolve(ce_leg.trading_symbol) if ce_leg is not None else None
            new_pe_ref = self._instruments.resolve(pe_leg.trading_symbol) if pe_leg is not None else None
            self._retarget_atm(new_ce_ref, new_pe_ref)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh option chain")

    def _retarget_atm(self, new_ce_ref: InstrumentRef | None, new_pe_ref: InstrumentRef | None) -> None:
        """Swap the ATM CE/PE feed subscriptions if the ATM strike has drifted.
        Called once at startup (subscribing for the first time, old=[]), and again
        whenever a later option-chain refresh finds a different ATM strike.
        """
        ce_changed = new_ce_ref is not None and (self._atm_ce_ref is None or new_ce_ref.exchange_token != self._atm_ce_ref.exchange_token)
        pe_changed = new_pe_ref is not None and (self._atm_pe_ref is None or new_pe_ref.exchange_token != self._atm_pe_ref.exchange_token)
        if not ce_changed and not pe_changed:
            return

        old_refs = [ref for ref in (self._atm_ce_ref, self._atm_pe_ref) if ref is not None]
        new_refs = []
        if ce_changed:
            new_refs.append(new_ce_ref)
            self._atm_ce_ref = new_ce_ref
            # Old premium samples belong to a different contract — discard, not stitch.
            self._latest_ce_ltp = None
            self._latest_ce_bidask = (0.0, 0.0)
        if pe_changed:
            new_refs.append(new_pe_ref)
            self._atm_pe_ref = new_pe_ref
            self._latest_pe_ltp = None
            self._latest_pe_bidask = (0.0, 0.0)

        if old_refs:  # skip resubscribe on the very first call — start_feed handles that
            self._adapter.resubscribe(
                old_instruments=[ref.feed_dict() for ref in old_refs],
                new_instruments=[ref.feed_dict() for ref in new_refs],
                on_tick=self._on_tick,
            )
            logger.info("ATM drifted: now tracking %s", [ref.trading_symbol for ref in new_refs])

    def _backfill_candles(self) -> None:
        try:
            # Reach back 5 CALENDAR days and keep the newest 300 candles — a
            # minutes-based window dies on Monday morning (now - 400min lands in the
            # weekend, zero candles return, and S/R/structure/EMA100 then starve until
            # ~10:10 while pivots slowly form from live candles alone; observed live
            # 2026-07-13). 5 days always spans at least one full prior trading day,
            # including long weekends with one holiday.
            end = datetime.now()
            start = end - timedelta(days=5)
            candles = self._adapter.get_historical_candles(self._index, self._config.candle_interval, start, end)
            for candle in candles[-300:]:
                self._builder.update_candle(candle)
            logger.info("Backfilled %d candles (window %s -> %s)", min(len(candles), 300),
                        candles[-300:][0].ts if candles else "-", candles[-1].ts if candles else "-")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to backfill candles")

    def _option_chain_loop(self) -> None:
        while not self._stop_event.wait(self._config.option_chain_refresh_seconds):
            self._refresh_option_chain()

    def _candle_loop(self) -> None:
        while not self._stop_event.wait(60.0):
            try:
                end = datetime.now()
                start = end - timedelta(minutes=2)
                candles = self._adapter.get_historical_candles(self._index, self._config.candle_interval, start, end)
                for candle in candles:
                    self._builder.update_candle(candle)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to roll candle window forward")

    def _stall_watchdog_loop(self) -> None:
        while not self._stop_event.wait(FEED_STALL_CHECK_INTERVAL_SECONDS):
            now = datetime.now()
            market_open = now.weekday() < 5 and is_market_open_time(now)
            silence_start = self._last_tick_wall_ts or self._started_at
            silent_for = (now - silence_start).total_seconds() if silence_start else 0.0

            if not market_open:
                # Zero ticks is expected outside trading hours — a calm heartbeat so
                # "correctly idle" doesn't look identical to "silently stuck", without
                # crying wolf like a WARNING would.
                due = self._last_idle_heartbeat_ts is None or (now - self._last_idle_heartbeat_ts).total_seconds() >= IDLE_HEARTBEAT_SECONDS
                if due:
                    self._last_idle_heartbeat_ts = now
                    logger.info(
                        "Still connected, no ticks expected outside market hours (Mon-Fri 09:15-15:30 IST) — idle %.0fs",
                        silent_for,
                    )
                self._feed_stalled = False
                continue

            # Before the FIRST tick ever arrives, "silence" usually just means the NATS
            # handshake is still in progress (10-25s observed live) — touching the feed
            # then corrupts the SDK's connection state (confirmed live 2026-07-13:
            # re-subscribing mid-handshake broke every subsequent callback with
            # "'NoneType' object has no attribute 'update'"). Give startup a generous
            # grace window; once ticks HAVE flowed, the normal stall threshold applies.
            threshold = FEED_STALL_WARNING_SECONDS if self._last_tick_wall_ts is not None else STARTUP_GRACE_SECONDS
            if silent_for >= threshold:
                if not self._feed_stalled:
                    self._feed_stalled = True
                    self._adapter.mark_feed_disconnected()
                    self._reconnect_backoff_seconds = RECONNECT_BASE_INTERVAL_SECONDS
                    self._next_reconnect_attempt_ts = now
                    logger.warning("No ticks received for %.0fs during market hours — feed may have stalled", silent_for)
                self._maybe_attempt_reconnect(now)

    def _maybe_attempt_reconnect(self, now: datetime) -> None:
        if self._next_reconnect_attempt_ts is not None and now < self._next_reconnect_attempt_ts:
            return
        try:
            # Full feed restart (fresh GrowwFeed + fresh socket), NOT resubscribe on the
            # existing one — a stalled feed's internal station may be broken, and
            # unsubscribe/subscribe calls against it corrupt the SDK further.
            logger.info("Restarting feed after stall (fresh connection)...")
            self._adapter.stop_feed()
            self._adapter.start_feed(self._tracked_instruments(), on_tick=self._on_tick)
        except Exception:  # noqa: BLE001 - a failed reconnect attempt must not kill the watchdog thread
            logger.exception("Feed restart attempt failed — will retry with backoff")
        self._reconnect_backoff_seconds = min(RECONNECT_MAX_INTERVAL_SECONDS, self._reconnect_backoff_seconds * 2)
        self._next_reconnect_attempt_ts = now + timedelta(seconds=self._reconnect_backoff_seconds)

    def _tracked_instruments(self) -> list[dict]:
        refs = [self._index_ref]
        if self._atm_ce_ref is not None:
            refs.append(self._atm_ce_ref)
        if self._atm_pe_ref is not None:
            refs.append(self._atm_pe_ref)
        return [ref.feed_dict() for ref in refs]
