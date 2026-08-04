"""Verifies the --validate-orders behavior at the Orchestrator level: when enabled, a
failed/timed-out fill must abort an entry (no trade opened) and must NOT mark an open
position closed on a failed exit (stays IN_TRADE so the next tick retries) — mirroring
PROD10FEB's VALIDATE_ORDERS pattern (docs discussion: no retry of the order itself, but
never silently treat an unconfirmed fill as done).
"""

import dataclasses
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta

from trading_decision_engine.app.broker.groww_execution_adapter import MarginView, OrderResult
from trading_decision_engine.app.config.constants import TradeLifecycleState
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.market_data.replay_source import ReplayMarketDataSource, ReplayTick
from trading_decision_engine.app.models.market_snapshot import Candle, PremiumTick
from trading_decision_engine.app.orchestrator import Orchestrator
from trading_decision_engine.tests.fixtures import make_option_chain

EXPIRY = date(2026, 7, 16)


class _FakeAdapter:
    """Duck-typed stand-in for GrowwExecutionAdapter — deterministic, no network,
    lets us script exactly what a real fill/failed-fill would look like.
    """

    def __init__(self, dry_run=False, fill_status="EXECUTED", executed_price=150.0, executed_qty=65):
        self.dry_run = dry_run
        self.fill_status = fill_status
        self.executed_price = executed_price
        self.executed_qty = executed_qty
        self.placed_orders = []
        self._order_counter = 0

    def login(self):
        pass

    def is_connected(self):
        return True

    def get_margins(self):
        return MarginView(option_buy_balance_available=1_000_000.0, clear_cash=1_000_000.0)

    def place_order(self, instrument, quantity, side, product="MIS"):
        self._order_counter += 1
        order_id = f"FAKE_{self._order_counter}"
        self.placed_orders.append((instrument, quantity, side))
        return OrderResult(order_id=order_id, order_status="PLACED", raw={})

    def wait_for_fill(self, order_id, side, timeout_seconds=None):
        return self.fill_status

    def get_order_executed_price(self, order_id):
        return self.executed_price, self.executed_qty


def _build_trending_ticks(start, minutes, step, chain):
    ticks = [ReplayTick(ts=start, kind="option_chain", payload=chain)]
    prev_close = chain.underlying_ltp
    t = start
    for _ in range(minutes):
        ts_candle = t + timedelta(seconds=60)
        o, c = prev_close, prev_close + step
        ticks.append(ReplayTick(ts=ts_candle, kind="candle", payload=Candle(ts=ts_candle, open=o, high=max(o, c) + 1, low=min(o, c) - 1, close=c, volume=1000)))
        for s in range(1, 61):
            sub_ts = t + timedelta(seconds=s)
            sub_price = o + (c - o) * (s / 60)
            ticks.append(ReplayTick(ts=sub_ts, kind="spot", payload=sub_price))
            ce = 150 + (sub_price - chain.underlying_ltp) * 0.5
            pe = max(5.0, 140 - (sub_price - chain.underlying_ltp) * 0.5)
            ticks.append(ReplayTick(ts=sub_ts, kind="premium", payload=PremiumTick(ts=sub_ts, ce_premium=ce, pe_premium=pe, bid=ce - 1, ask=ce + 1)))
        prev_close, t = c, ts_candle
    ticks.sort(key=lambda tk: tk.ts)
    return ticks


class TestValidateOrders(unittest.TestCase):
    def setUp(self):
        self.log_dir = tempfile.mkdtemp(prefix="tde_validate_test_")
        self.config = dataclasses.replace(
            StrategyConfig(), trend_threshold=5.0, min_resistance_distance=0.0, decision_score_threshold=10.0,
            signal_stability_min_seconds=0.5, signal_stability_max_seconds=2.0, signal_stability_base_seconds=1.0,
            wait_after_open_minutes=0, cooldown_seconds=5,
        )

    def tearDown(self):
        shutil.rmtree(self.log_dir, ignore_errors=True)

    def _make_orchestrator(self, adapter, validate_orders):
        return Orchestrator(
            adapter=adapter, config=self.config, index="NIFTY", expiry_date=EXPIRY, lot_size=65,
            log_dir=self.log_dir, mode="shadow", validate_orders=validate_orders,
        )

    def test_validated_entry_succeeds_and_uses_real_executed_fields(self):
        adapter = _FakeAdapter(fill_status="EXECUTED", executed_price=155.0, executed_qty=65)
        orch = self._make_orchestrator(adapter, validate_orders=True)
        chain = make_option_chain(24200.0)
        ticks = _build_trending_ticks(datetime(2026, 7, 13, 9, 20, 0), 30, 8.0, chain)
        ReplayMarketDataSource(ticks, session_provider=orch.session_state, speed=0).start(orch.on_snapshot)

        self.assertEqual(orch._trade_manager.state, TradeLifecycleState.MONITORING)
        self.assertEqual(orch._current_lots, 1)  # 65 executed / 65 lot_size

    def test_validated_entry_aborts_on_rejected_fill(self):
        adapter = _FakeAdapter(fill_status="REJECTED")
        orch = self._make_orchestrator(adapter, validate_orders=True)
        chain = make_option_chain(24200.0)
        ticks = _build_trending_ticks(datetime(2026, 7, 13, 9, 20, 0), 30, 8.0, chain)
        ReplayMarketDataSource(ticks, session_provider=orch.session_state, speed=0).start(orch.on_snapshot)

        self.assertEqual(orch._trade_manager.state, TradeLifecycleState.IDLE)
        self.assertEqual(orch._trades_today, 0)

    def test_validated_entry_aborts_on_zero_executed_qty(self):
        adapter = _FakeAdapter(fill_status="EXECUTED", executed_price=0.0, executed_qty=0)
        orch = self._make_orchestrator(adapter, validate_orders=True)
        chain = make_option_chain(24200.0)
        ticks = _build_trending_ticks(datetime(2026, 7, 13, 9, 20, 0), 30, 8.0, chain)
        ReplayMarketDataSource(ticks, session_provider=orch.session_state, speed=0).start(orch.on_snapshot)

        self.assertEqual(orch._trade_manager.state, TradeLifecycleState.IDLE)

    def test_unvalidated_entry_trusts_immediate_response(self):
        adapter = _FakeAdapter(fill_status="REJECTED")  # would abort if validated — but it isn't
        orch = self._make_orchestrator(adapter, validate_orders=False)
        chain = make_option_chain(24200.0)
        ticks = _build_trending_ticks(datetime(2026, 7, 13, 9, 20, 0), 30, 8.0, chain)
        ReplayMarketDataSource(ticks, session_provider=orch.session_state, speed=0).start(orch.on_snapshot)

        # place_order() itself still returned order_status="PLACED", which the
        # unvalidated path accepts as good enough — validate_orders=False never calls
        # wait_for_fill at all, so the scripted "REJECTED" fill_status is never consulted.
        self.assertEqual(orch._trade_manager.state, TradeLifecycleState.MONITORING)

    def test_validated_exit_stays_in_trade_on_failed_sell(self):
        adapter = _FakeAdapter(fill_status="EXECUTED", executed_price=150.0, executed_qty=65)
        orch = self._make_orchestrator(adapter, validate_orders=True)
        chain = make_option_chain(24200.0)
        uptrend = _build_trending_ticks(datetime(2026, 7, 13, 9, 20, 0), 30, 8.0, chain)
        ReplayMarketDataSource(uptrend, session_provider=orch.session_state, speed=0).start(orch.on_snapshot)
        self.assertEqual(orch._trade_manager.state, TradeLifecycleState.MONITORING)

        # Now make the SELL fail to confirm, and feed a reversal to trigger an exit attempt.
        adapter.fill_status = "FAILED"
        last_ts = uptrend[-1].ts
        downtrend = _build_trending_ticks(last_ts, 10, -8.0, chain)
        ReplayMarketDataSource(downtrend, session_provider=orch.session_state, speed=0).start(orch.on_snapshot)

        # The exit condition is (correctly) still flagged — TradeManager.state stays
        # EXIT_TRIGGERED, meaning the Orchestrator keeps retrying the sell every tick
        # (visible as repeated "did not execute" log lines with new order IDs) — but
        # crucially it never calls on_trade_closed(), so the position is never silently
        # dropped just because Groww hasn't confirmed the fill yet.
        self.assertEqual(orch._trade_manager.state, TradeLifecycleState.EXIT_TRIGGERED)
        self.assertIsNotNone(orch._current_instrument)
        self.assertEqual(orch._trades_today, 1)  # no phantom second trade opened
        self.assertEqual(orch._daily_pnl, 0.0)  # nothing realized yet — exit never confirmed


if __name__ == "__main__":
    unittest.main()
