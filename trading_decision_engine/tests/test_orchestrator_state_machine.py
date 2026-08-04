import dataclasses
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from trading_decision_engine.app.broker.groww_execution_adapter import GrowwExecutionAdapter
from trading_decision_engine.app.config.constants import Direction, OrchestratorState, TradeAction, TradeLifecycleState
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.market_data.replay_source import ReplayMarketDataSource, ReplayTick
from trading_decision_engine.app.models.engine_results import DecisionResult, EligibilityResult
from trading_decision_engine.app.models.market_snapshot import Candle, OptionChainView, PremiumTick
from trading_decision_engine.app.orchestrator import Orchestrator
from trading_decision_engine.app.utils.error_handling import FatalBrokerError
from trading_decision_engine.tests.fixtures import make_candles, make_option_chain, make_premium_history, make_snapshot

EXPIRY = date(2026, 7, 16)


def _make_orchestrator(config: StrategyConfig, log_dir: str) -> Orchestrator:
    adapter = GrowwExecutionAdapter(config=config, dry_run=True, offline=True)
    adapter.login()
    return Orchestrator(adapter=adapter, config=config, index="NIFTY", expiry_date=EXPIRY, lot_size=75, log_dir=log_dir, mode="replay")


def _build_trending_ticks(start: datetime, minutes: int, step: float, chain: OptionChainView) -> list[ReplayTick]:
    ticks = [ReplayTick(ts=start, kind="option_chain", payload=chain)]
    prev_close = chain.underlying_ltp
    t = start
    for _ in range(minutes):
        ts_candle = t + timedelta(seconds=60)
        o = prev_close
        c = prev_close + step
        candle = Candle(ts=ts_candle, open=o, high=max(o, c) + 1, low=min(o, c) - 1, close=c, volume=1000)
        ticks.append(ReplayTick(ts=ts_candle, kind="candle", payload=candle))
        for s in range(1, 61):
            sub_ts = t + timedelta(seconds=s)
            progress = s / 60
            sub_price = o + (c - o) * progress
            ticks.append(ReplayTick(ts=sub_ts, kind="spot", payload=sub_price))
            ce = 150 + (sub_price - chain.underlying_ltp) * 0.5
            pe = max(5.0, 140 - (sub_price - chain.underlying_ltp) * 0.5)
            ticks.append(ReplayTick(ts=sub_ts, kind="premium", payload=PremiumTick(ts=sub_ts, ce_premium=ce, pe_premium=pe, bid=ce - 1, ask=ce + 1)))
        prev_close = c
        t = ts_candle
    ticks.sort(key=lambda tk: tk.ts)
    return ticks


class TestOrchestratorStateMachine(unittest.TestCase):
    def setUp(self):
        self.log_dir = tempfile.mkdtemp(prefix="tde_test_")
        self._orchestrators: list[Orchestrator] = []

    def tearDown(self):
        for orch in self._orchestrators:
            orch._logger.close()
        shutil.rmtree(self.log_dir, ignore_errors=True)

    def _tracked_orchestrator(self, config: StrategyConfig) -> Orchestrator:
        orch = _make_orchestrator(config, self.log_dir)
        self._orchestrators.append(orch)
        return orch

    def test_market_closed_outside_hours_stays_closed(self):
        config = StrategyConfig()
        orch = self._tracked_orchestrator(config)
        chain = make_option_chain(24000.0)
        outside_hours = datetime(2026, 7, 13, 20, 0, 0)  # 8pm, well after close
        ticks = [ReplayTick(ts=outside_hours, kind="option_chain", payload=chain), ReplayTick(ts=outside_hours, kind="spot", payload=24000.0)]
        source = ReplayMarketDataSource(ticks, session_provider=orch.session_state, speed=0)
        source.start(orch.on_snapshot)
        self.assertEqual(orch._state, OrchestratorState.MARKET_CLOSED)

    def test_wait_mode_then_analyzing(self):
        config = StrategyConfig()
        orch = self._tracked_orchestrator(config)
        chain = make_option_chain(24000.0)
        market_open = datetime(2026, 7, 13, 9, 15, 0)
        ticks = [ReplayTick(ts=market_open, kind="option_chain", payload=chain)]
        # still inside the wait window (< wait_after_open_minutes)
        early = market_open + timedelta(minutes=2)
        ticks.append(ReplayTick(ts=early, kind="spot", payload=24000.0))
        source = ReplayMarketDataSource(ticks, session_provider=orch.session_state, speed=0)
        source.start(orch.on_snapshot)
        self.assertEqual(orch._state, OrchestratorState.WAIT_MODE)

        # now past the wait window
        later = market_open + timedelta(minutes=config.wait_after_open_minutes + 1)
        source2 = ReplayMarketDataSource([ReplayTick(ts=later, kind="spot", payload=24000.0)], session_provider=orch.session_state, speed=0)
        source2.start(orch.on_snapshot)
        self.assertEqual(orch._state, OrchestratorState.ANALYZING)

    def test_full_cycle_buy_then_reversal_exit_then_cooldown(self):
        config = dataclasses.replace(
            StrategyConfig(),
            trend_threshold=5.0, min_resistance_distance=0.0, decision_score_threshold=10.0,
            signal_stability_min_seconds=0.5, signal_stability_max_seconds=2.0, signal_stability_base_seconds=1.0,
            wait_after_open_minutes=0, cooldown_seconds=5,
        )
        orch = self._tracked_orchestrator(config)
        chain = make_option_chain(24000.0)
        start = datetime(2026, 7, 13, 9, 20, 0)

        uptrend_ticks = _build_trending_ticks(start, minutes=40, step=8.0, chain=chain)
        source = ReplayMarketDataSource(uptrend_ticks, session_provider=orch.session_state, speed=0)
        source.start(orch.on_snapshot)

        self.assertEqual(orch._trades_today, 1)
        self.assertEqual(orch._trade_manager.state, TradeLifecycleState.MONITORING)
        self.assertEqual(orch._state, OrchestratorState.IN_TRADE)

        # now reverse hard to trigger an exit
        last_ts = uptrend_ticks[-1].ts
        downtrend_ticks = _build_trending_ticks(last_ts, minutes=10, step=-8.0, chain=chain)
        source2 = ReplayMarketDataSource(downtrend_ticks, session_provider=orch.session_state, speed=0)
        source2.start(orch.on_snapshot)

        self.assertEqual(orch._trade_manager.state, TradeLifecycleState.IDLE)
        self.assertIn(orch._state, (OrchestratorState.COOLDOWN, OrchestratorState.ANALYZING))

    def test_fatal_broker_error_during_order_placement_halts_to_stopped(self):
        config = dataclasses.replace(
            StrategyConfig(),
            trend_threshold=5.0, min_resistance_distance=0.0, decision_score_threshold=10.0,
            signal_stability_min_seconds=0.5, signal_stability_max_seconds=2.0, signal_stability_base_seconds=1.0,
            wait_after_open_minutes=0, cooldown_seconds=5,
        )
        orch = self._tracked_orchestrator(config)
        chain = make_option_chain(24000.0)
        start = datetime(2026, 7, 13, 9, 20, 0)
        uptrend_ticks = _build_trending_ticks(start, minutes=40, step=8.0, chain=chain)

        with patch.object(orch._adapter, "place_order", side_effect=FatalBrokerError("simulated 401 after re-login")):
            source = ReplayMarketDataSource(uptrend_ticks, session_provider=orch.session_state, speed=0)
            source.start(orch.on_snapshot)

        self.assertEqual(orch._state, OrchestratorState.STOPPED)
        self.assertEqual(orch._trades_today, 0)

        # STOPPED must be sticky — further ticks are ignored entirely, not re-dispatched.
        more_ticks = _build_trending_ticks(uptrend_ticks[-1].ts, minutes=2, step=8.0, chain=chain)
        source2 = ReplayMarketDataSource(more_ticks, session_provider=orch.session_state, speed=0)
        source2.start(orch.on_snapshot)
        self.assertEqual(orch._state, OrchestratorState.STOPPED)

    def test_repeated_engine_failure_escalates_then_clears_on_recovery(self):
        # Direct unit test of the escalation-tracking mechanism itself (not a full
        # multi-thousand-tick replay, which is slow and indirect for this purpose).
        orch = self._tracked_orchestrator(StrategyConfig())
        snapshot = make_snapshot(candles=make_candles(60), premium_history=make_premium_history(10))

        with patch.object(orch._structure_engine, "analyze", side_effect=RuntimeError("boom")):
            for _ in range(3):  # EngineFailureTracker's default escalation_threshold
                orch._run_pure_engines(snapshot)
        self.assertIn("market_structure", orch._escalated_engines)

        orch._run_pure_engines(snapshot)  # engine healthy again on this call
        self.assertNotIn("market_structure", orch._escalated_engines)

    def test_escalated_engine_suppresses_new_entry_without_blocking_monitoring(self):
        # Isolates _run_analysis_cycle's suppression branch from decision-scoring
        # realism by stubbing DecisionEngine.decide() to return a fixed BUY. The
        # escalation must come from real consecutive failures (_run_pure_engines
        # recomputes self._escalated_engines every call, so presetting it directly
        # wouldn't survive into _run_analysis_cycle's own engine pass).
        orch = self._tracked_orchestrator(StrategyConfig())
        buy_decision = DecisionResult(
            direction=Direction.BULLISH, score=90.0, confidence=90.0, reasons=("stub",), action=TradeAction.BUY,
            buy_score=90.0, sell_score=0.0, exit_score=0.0,
            eligibility=EligibilityResult(passed=True, reasons=(), failed_checks=()), trade_quality_score=90.0,
        )
        snapshot = make_snapshot(candles=make_candles(60), premium_history=make_premium_history(10))

        with patch.object(orch._structure_engine, "analyze", side_effect=RuntimeError("boom")), \
             patch.object(orch._decision_engine, "decide", return_value=buy_decision), \
             patch.object(orch, "_handle_entry") as mock_handle_entry:
            for _ in range(3):  # cross EngineFailureTracker's default escalation_threshold
                orch._run_pure_engines(snapshot)
            orch._run_analysis_cycle(snapshot)

        mock_handle_entry.assert_not_called()
        self.assertEqual(orch._trades_today, 0)


if __name__ == "__main__":
    unittest.main()
