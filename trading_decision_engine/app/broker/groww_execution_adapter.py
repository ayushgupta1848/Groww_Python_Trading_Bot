"""GrowwExecutionAdapter: the only module in this engine allowed to talk to the broker.
Replicates PROD10FEB/QA_PASS's exact SDK/REST call shapes (same auth flow, same
growwapi.place_order(...) constants, same REST endpoints per GROWW_API_REFERENCE.md) as
a fresh, clean, independently-importable module — not an import of those scripts, which
run live auth as an import-time side effect and can't be safely reused as a library.
See docs/DESIGN.md §12.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

import pyotp
import requests

from ..config.strategy import BrokerCredentials, StrategyConfig, load_broker_credentials
from ..models.market_snapshot import Candle, OptionChainView, OptionLeg
from ..utils.error_handling import FatalBrokerError, RetryPolicy
from .instrument_master import InstrumentMaster

logger = logging.getLogger("trading_decision_engine.broker")

BASE_URL = "https://api.groww.in"


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    order_status: str
    raw: dict


@dataclass(frozen=True)
class PositionView:
    trading_symbol: str
    exchange: str
    quantity: int
    net_price: float
    realised_pnl: float
    unrealised_pnl: float


@dataclass(frozen=True)
class MarginView:
    option_buy_balance_available: float
    clear_cash: float


@dataclass(frozen=True)
class TickEvent:
    ts: datetime
    # GrowwFeed.get_ltp()/get_market_depth() key their responses by exchange_token
    # (the feed's "subscription_key"), NOT by the human-readable trading symbol —
    # confirmed against growwapi.groww.constants.FeedConstants._generate_topic_meta.
    # Callers resolve exchange_token -> trading symbol themselves (via InstrumentMaster)
    # since only they know which tokens map to which instruments.
    exchange_token: str
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    spot: float | None = None


# Same terminal-status classification as PROD10FEB_ManualBOT's wait_for_order_status:
# any other status (PENDING, OPEN, etc.) is treated as "still in flight, keep polling".
ORDER_SUCCESS_STATUSES = ("EXECUTED", "COMPLETED", "DELIVERY_AWAITED")
ORDER_FAILURE_STATUSES = ("FAILED", "REJECTED", "CANCELLED")

# PROD10FEB polls indefinitely (no timeout) since a human is watching. This engine runs
# fully unattended on the tick-handling thread, so an unbounded loop here would hang the
# whole pipeline if Groww's API ever got stuck — bounded timeouts are a deliberate safety
# addition, not a faithful copy of the original's "wait forever" behavior.
DEFAULT_BUY_FILL_TIMEOUT_SECONDS = 10.0
DEFAULT_SELL_FILL_TIMEOUT_SECONDS = 15.0


def _is_rate_limited(exc: Exception) -> bool:
    return isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 429


def _is_unauthorized(exc: Exception) -> bool:
    return isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 401


class GrowwExecutionAdapter:
    def __init__(
        self,
        config: StrategyConfig | None = None,
        dry_run: bool = True,
        offline: bool = False,
        instrument_master: InstrumentMaster | None = None,
    ) -> None:
        """
        dry_run: simulates ORDER EXECUTION only (place_order/cancel_order/order status/
            positions/margins never place a real order or touch real capital). SHADOW
            mode sets this True while still wanting real market data.
        offline: skips authentication and any network call entirely (login()/start_feed()
            become no-ops). Only appropriate for fully-offline replay (--replay-file),
            which never needs a live session at all. dry_run alone must NOT skip auth —
            shadow mode needs a real authenticated session to get real ticks/candles/
            option chain even though it never places a real order.
        """
        self._config = config or StrategyConfig()
        self.dry_run = dry_run
        self.offline = offline
        self._instruments = instrument_master
        self._retry = RetryPolicy()
        self._session = requests.Session()
        self._access_token: str | None = None
        self._groww = None  # growwapi.GrowwAPI instance, set in login()
        self._feed = None  # growwapi.GrowwFeed instance, set in start_feed()
        self._connected = False
        # Keyed by exchange_token (GrowwFeed's own subscription key), NOT trading_symbol
        # — see TickEvent's docstring below for why.
        self._last_ltp: dict[str, float] = {}
        self._paper_orders: dict[str, dict] = {}
        self._paper_order_counter = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ auth / feed
    def login(self) -> None:
        if self.offline:
            self._connected = True
            logger.info("Offline mode: skipping live Groww login (no network needed)")
            return

        from growwapi import GrowwAPI  # imported lazily so fully-offline replay never needs the SDK installed

        creds: BrokerCredentials = load_broker_credentials()
        totp = pyotp.TOTP(creds.totp_secret).now()
        access_token = self._retry.run(lambda: GrowwAPI.get_access_token(api_key=creds.api_key, totp=totp))
        self._access_token = access_token
        self._groww = GrowwAPI(access_token)
        self._connected = True
        logger.info("Groww login successful")

    def is_connected(self) -> bool:
        return self._connected

    def mark_feed_disconnected(self) -> None:
        """Called by the market-data layer's stall watchdog when the live feed has gone
        silent — flips SessionState.broker_connected (via is_connected()) to False so
        RiskEngine blocks new entries until the feed is confirmed recovered. See
        docs/DESIGN.md §9.
        """
        self._connected = False

    def mark_feed_connected(self) -> None:
        """Called by the market-data layer once a tick is received again after a stall."""
        self._connected = True

    def _latest_ltp_for(self, trading_symbol: str, fallback: float) -> float:
        """self._last_ltp is keyed by exchange_token (see TickEvent's docstring), not
        trading_symbol — resolve through InstrumentMaster before looking it up. Falls
        back to `fallback` (the pre-decision premium estimate) when no InstrumentMaster
        was supplied, the symbol can't be resolved, or the feed hasn't ticked yet.
        """
        if self._instruments is None:
            return fallback
        ref = self._instruments.resolve(trading_symbol)
        if ref is None:
            return fallback
        with self._lock:
            return self._last_ltp.get(ref.exchange_token, fallback)

    def _make_feed_callback(self, on_tick: Callable[[TickEvent], None], getter_name: str) -> Callable[..., None]:
        # GrowwFeed invokes on_data_received WITH positional argument(s) — confirmed
        # live 2026-07-13 ("_on_ltp_update() takes 0 positional arguments but 1 was
        # given" on every tick). Accept and ignore whatever the SDK passes; we read
        # the full last-known snapshot from the feed's getter instead.
        #
        # getter_name is "get_ltp" (FNO options: values keyed {"ltp": ...}) or
        # "get_index_value" (index spot: values keyed {"value": ...}) — both shapes
        # confirmed against the live feed 2026-07-13:
        #   get_index_value() -> {'NSE': {'CASH': {'NIFTY': {'tsInMillis':..., 'value': 24077.9}}}}
        #   get_ltp()         -> {'NSE': {'FNO': {'51373': {'tsInMillis':..., 'ltp': 93.05, ...}}}}
        def _on_feed_update(*_args, **_kwargs) -> None:
            try:
                feed = self._feed
                if feed is None:  # stop_feed()/restart raced with a late callback
                    return
                data = getattr(feed, getter_name)()
                for exchange_data in (data or {}).values():
                    for segment_data in exchange_data.values():
                        for token, value in segment_data.items():
                            if value is None:
                                continue
                            if isinstance(value, dict):
                                value = value.get("ltp") or value.get("value") or value.get("price")
                                if value is None:
                                    continue
                            ltp = float(value)
                            with self._lock:
                                self._last_ltp[str(token)] = ltp
                            on_tick(TickEvent(ts=datetime.now(), exchange_token=str(token), ltp=ltp))
            except Exception:  # noqa: BLE001
                logger.exception("Error processing feed update (%s)", getter_name)

        return _on_feed_update

    @staticmethod
    def _split_by_segment(instruments: list[dict]) -> tuple[list[dict], list[dict]]:
        """(index_instruments, fno_instruments): indices live on the CASH segment and
        publish ONLY on the index-value topic; subscribe_ltp on an index yields silence
        (confirmed live 2026-07-13 — the root cause of a 'stalled' feed at open).
        """
        index_instr = [i for i in instruments if i.get("segment") == "CASH"]
        fno_instr = [i for i in instruments if i.get("segment") != "CASH"]
        return index_instr, fno_instr

    def start_feed(self, instruments: list[dict], on_tick: Callable[[TickEvent], None]) -> None:
        if self.offline:
            logger.info("Offline mode: no live feed subscription (replay supplies ticks directly)")
            return

        from growwapi import GrowwFeed

        self._feed = GrowwFeed(self._groww)
        index_instr, fno_instr = self._split_by_segment(instruments)
        if index_instr:
            self._feed.subscribe_index_value(index_instr, on_data_received=self._make_feed_callback(on_tick, "get_index_value"))
        if fno_instr:
            self._feed.subscribe_ltp(fno_instr, on_data_received=self._make_feed_callback(on_tick, "get_ltp"))

    def resubscribe(
        self, old_instruments: list[dict], new_instruments: list[dict], on_tick: Callable[[TickEvent], None]
    ) -> None:
        """Swap a subset of subscriptions (e.g. the ATM CE/PE legs when the ATM strike
        drifts) without tearing down the whole feed connection. No-op when offline (no
        feed was ever started) — dry_run alone does not disable this, since shadow mode
        still tracks the real feed. Index (CASH) subscriptions never drift, so only the
        FNO legs are swapped here.
        """
        if self.offline or self._feed is None:
            return
        _, old_fno = self._split_by_segment(old_instruments)
        _, new_fno = self._split_by_segment(new_instruments)
        if old_fno:
            self._feed.unsubscribe_ltp(old_fno)
        if new_fno:
            self._feed.subscribe_ltp(new_fno, on_data_received=self._make_feed_callback(on_tick, "get_ltp"))

    def stop_feed(self) -> None:
        self._feed = None

    # ------------------------------------------------------------------ REST helpers
    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            "X-API-VERSION": "1.0",
        }

    def _run_with_reauth(self, call: Callable[[], dict]) -> dict:
        """Per docs/DESIGN.md §9: HTTP 401 -> one re-login attempt, then raise (fatal,
        not swallowed). RetryPolicy already exhausts its own bounded retries against the
        stale token first (harmless, just a short delay); once those are exhausted we
        attempt exactly one fresh login and retry the call once more. A 401 that
        survives the re-login means the credentials themselves are bad — that is
        unrecoverable and must halt the Orchestrator rather than be treated as one more
        transient engine failure.
        """
        try:
            return self._retry.run(call, is_rate_limited=_is_rate_limited)
        except requests.HTTPError as exc:
            if not _is_unauthorized(exc):
                raise
            logger.warning("Got HTTP 401 — attempting one re-login before failing")
            try:
                self.login()
            except Exception as login_exc:  # noqa: BLE001
                raise FatalBrokerError(f"Re-login after 401 failed: {login_exc}") from login_exc
            try:
                return self._retry.run(call, is_rate_limited=_is_rate_limited)
            except requests.HTTPError as exc2:
                if _is_unauthorized(exc2):
                    raise FatalBrokerError("Still unauthorized after re-login — credentials likely invalid") from exc2
                raise

    def _get(self, path: str, params: dict | None = None) -> dict:
        def _call() -> dict:
            resp = self._session.get(f"{BASE_URL}{path}", headers=self._headers(), params=params, timeout=8)
            resp.raise_for_status()
            return resp.json()

        return self._run_with_reauth(_call)

    def _post(self, path: str, json_body: dict) -> dict:
        def _call() -> dict:
            headers = {**self._headers(), "Content-Type": "application/json"}
            resp = self._session.post(f"{BASE_URL}{path}", headers=headers, json=json_body, timeout=8)
            resp.raise_for_status()
            return resp.json()

        return self._run_with_reauth(_call)

    # ------------------------------------------------------------------ market data
    def get_option_chain(self, index: str, expiry_date: str) -> OptionChainView:
        from ..config.constants import INDEX_EXCHANGE, Index

        exchange = INDEX_EXCHANGE[Index(index)]
        data = self._get(f"/v1/option-chain/exchange/{exchange}/underlying/{index}", params={"expiry_date": expiry_date})
        payload = data.get("payload", {})
        strikes: dict[float, dict[str, OptionLeg | None]] = {}
        for strike_str, legs in payload.get("strikes", {}).items():
            strike = float(strike_str)
            strikes[strike] = {
                "CE": self._parse_leg(legs.get("CE")),
                "PE": self._parse_leg(legs.get("PE")),
            }
        return OptionChainView(underlying_ltp=payload.get("underlying_ltp", 0.0), strikes=strikes)

    @staticmethod
    def _parse_leg(leg: dict | None) -> OptionLeg | None:
        if not leg:
            return None
        greeks = leg.get("greeks", {})
        return OptionLeg(
            trading_symbol=leg.get("trading_symbol", ""),
            ltp=leg.get("ltp", 0.0),
            open_interest=leg.get("open_interest", 0),
            volume=leg.get("volume", 0),
            bid=leg.get("bid", leg.get("ltp", 0.0)),
            ask=leg.get("ask", leg.get("ltp", 0.0)),
            iv=greeks.get("iv", 0.0),
            delta=greeks.get("delta", 0.0),
        )

    def get_historical_candles(self, symbol: str, interval: str, start: datetime, end: datetime) -> tuple[Candle, ...]:
        from ..config.constants import Index

        # The bare index itself (e.g. "NIFTY") lives in the CASH segment; an option's
        # trading_symbol (e.g. "NIFTY26JUN23800CE") is FNO. Confirmed empirically against
        # the live endpoint — GROWW_API_REFERENCE.md's "NSE-NIFTY 50" symbol form for the
        # index returns an empty candle list; plain "NSE-NIFTY" is what actually works.
        is_bare_index = symbol in {i.value for i in Index}
        segment = "CASH" if is_bare_index else "FNO"

        data = self._get(
            "/v1/historical/candles",
            params={
                "exchange": "NSE",
                "segment": segment,
                "groww_symbol": f"NSE-{symbol}",
                "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                "candle_interval": interval,
            },
        )
        candles = []
        # Confirmed empirically against the live endpoint: candles are nested under
        # payload.candles, not top-level, despite GROWW_API_REFERENCE.md showing a bare
        # {"candles": [...]} shape.
        for row in data.get("payload", {}).get("candles", []):
            ts_raw, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
            volume = int(row[5]) if len(row) > 5 and row[5] is not None else 0
            # The live endpoint returns an ISO timestamp string for index candles, but
            # epoch-millisecond integers for option/FNO candles per the documented shape
            # — accept either.
            ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else datetime.fromtimestamp(ts_raw / 1000)
            candles.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=volume))
        return tuple(candles)

    # ------------------------------------------------------------------ orders
    def _next_paper_order_id(self) -> str:
        self._paper_order_counter += 1
        return f"PAPER_{self._paper_order_counter:04d}"

    def place_order(
        self, instrument: dict, quantity: int, side: Literal["BUY", "SELL"], product: str = "MIS"
    ) -> OrderResult:
        trading_symbol = instrument.get("internal_trading_symbol") or instrument["trading_symbol"]

        if self.dry_run:
            price = self._latest_ltp_for(trading_symbol, fallback=instrument.get("ltp", 0.0))
            order_id = self._next_paper_order_id()
            self._paper_orders[order_id] = {"price": price, "qty": quantity, "symbol": trading_symbol, "side": side}
            logger.info("[DRY_RUN] %s %d x %s @ %.2f (order %s)", side, quantity, trading_symbol, price, order_id)
            return OrderResult(order_id=order_id, order_status="EXECUTED", raw={"dry_run": True, "price": price})

        import growwapi

        groww = self._groww
        exchange_str = instrument.get("exchange", "NSE").upper()
        exchange_const = groww.EXCHANGE_BSE if exchange_str == "BSE" else groww.EXCHANGE_NSE
        product_const = getattr(groww, f"PRODUCT_{product}", groww.PRODUCT_MIS)
        transaction_const = getattr(groww, f"TRANSACTION_TYPE_{side}")

        order = self._retry.run(
            lambda: groww.place_order(
                trading_symbol=trading_symbol,
                quantity=quantity,
                validity=groww.VALIDITY_DAY,
                exchange=exchange_const,
                segment=groww.SEGMENT_FNO,
                product=product_const,
                order_type=groww.ORDER_TYPE_MARKET,
                transaction_type=transaction_const,
                price=0,
            )
        )
        payload = order.get("payload", order) if isinstance(order, dict) else {}
        order_id = payload.get("groww_order_id") or order.get("groww_order_id")
        return OrderResult(order_id=order_id, order_status=payload.get("order_status", "PLACED"), raw=order)

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run:
            self._paper_orders.pop(order_id, None)
            return True
        data = self._post("/v1/order/cancel", {"segment": "FNO", "groww_order_id": order_id})
        return bool(data.get("success")) or data.get("payload", {}).get("order_status") == "CANCELLED"

    def get_order_status(self, order_id: str) -> str:
        if self.dry_run:
            return "EXECUTED" if order_id in self._paper_orders or order_id.startswith("PAPER_") else "UNKNOWN"
        data = self._get(f"/v1/order/status/{order_id}", params={"segment": "FNO"})
        return data.get("payload", {}).get("order_status", "UNKNOWN")

    def get_order_executed_price(self, order_id: str) -> tuple[float, int]:
        if self.dry_run:
            paper = self._paper_orders.get(order_id, {})
            return paper.get("price", 0.0), paper.get("qty", 0)
        data = self._get(f"/v1/order/trades/{order_id}", params={"segment": "FNO", "page": 0, "page_size": 50})
        trades = data.get("payload", {}).get("trade_list", [])
        total_qty = sum(t["quantity"] for t in trades)
        total_value = sum(t["price"] * t["quantity"] for t in trades)
        avg_price = round(total_value / total_qty, 2) if total_qty else 0.0
        return avg_price, total_qty

    def wait_for_fill(self, order_id: str, side: Literal["BUY", "SELL"], timeout_seconds: float | None = None) -> str:
        """Poll order status until it reaches a terminal state, or timeout_seconds
        elapses. Same success/failure classification and poll cadence (0.2s BUY, 1.0s
        SELL) as PROD10FEB_ManualBOT's wait_for_order_status — bounded by a timeout
        instead of polling forever, since this runs on the tick-handling thread of an
        unattended system. Returns the final status string, or "TIMEOUT" if the order
        never reached a terminal state in time (caller treats that as a failure).
        """
        if self.dry_run:
            return "EXECUTED"

        poll_interval = 0.2 if side == "BUY" else 1.0
        default_timeout = DEFAULT_BUY_FILL_TIMEOUT_SECONDS if side == "BUY" else DEFAULT_SELL_FILL_TIMEOUT_SECONDS
        deadline = time.monotonic() + (timeout_seconds if timeout_seconds is not None else default_timeout)

        while True:
            status = self.get_order_status(order_id)
            if status in ORDER_SUCCESS_STATUSES or status in ORDER_FAILURE_STATUSES:
                return status
            if time.monotonic() >= deadline:
                logger.warning("Timed out waiting for %s order %s to reach a terminal status (last seen: %s)", side, order_id, status)
                return "TIMEOUT"
            time.sleep(poll_interval)

    # ------------------------------------------------------------------ account
    def get_positions(self) -> tuple[PositionView, ...]:
        if self.dry_run:
            return ()
        data = self._get("/v1/positions/user", params={"segment": "FNO"})
        positions = data.get("payload", {}).get("positions", [])
        return tuple(
            PositionView(
                trading_symbol=p.get("trading_symbol", ""),
                exchange=p.get("exchange", ""),
                quantity=p.get("quantity", 0),
                net_price=p.get("net_price", 0.0),
                realised_pnl=p.get("realised_pnl", 0.0),
                unrealised_pnl=p.get("unrealised_pnl", 0.0),
            )
            for p in positions
        )

    def get_margins(self) -> MarginView:
        if self.dry_run:
            return MarginView(option_buy_balance_available=self._config.max_exposure, clear_cash=self._config.max_exposure)
        data = self._get("/v1/margins/detail/user")
        payload = data.get("payload", {})
        fno = payload.get("fno_margin_details", {})
        return MarginView(
            option_buy_balance_available=fno.get("option_buy_balance_available", 0.0),
            clear_cash=payload.get("clear_cash", 0.0),
        )
