"""Orchestrator: the event-driven state machine and composition root. It is the only
module that knows about more than one engine — see docs/DESIGN.md §1, §6, §6b.

Every call to on_snapshot() is one WebSocket-tick-driven cycle. No engine and no method
here ever sleeps waiting for market data; the only timers are the WAIT_MODE/cooldown
durations and the bounded option-chain/candle refresh loops living in the market-data
layer (never here).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable

from .broker.groww_execution_adapter import ORDER_SUCCESS_STATUSES, GrowwExecutionAdapter
from .config.constants import (
    INDEX_EXCHANGE,
    MARKET_CLOSE_TIME,
    Direction,
    Index,
    MarketStructure,
    OrchestratorState,
    TradeAction,
    TradeLifecycleState,
    is_market_open_time,
)
from .config.strategy import StrategyConfig, config_files_mtime
from .engines.breakout_engine import BreakoutEngine
from .engines.decision_engine import DecisionEngine, DecisionInput
from .engines.market_strength_engine import MarketStrengthEngine
from .engines.market_structure_engine import MarketStructureEngine
from .engines.option_selection_engine import OptionSelectionEngine
from .engines.position_sizing_engine import PositionSizingEngine
from .engines.premium_momentum_engine import PremiumMomentumEngine
from .engines.risk_engine import RiskEngine
from .engines.signal_stability_engine import SignalStabilityEngine, SignalStabilityInput, required_confirmation_seconds
from .engines.support_resistance_engine import SupportResistanceEngine
from .engines.trade_manager import TradeManager
from .engines.trading_rules_engine import TradingRulesEngine
from .engines.trend_engine import TrendEngine
from .engines.volatility_engine import VolatilityEngine
from .models.engine_results import (
    BreakoutResult,
    DecisionResult,
    EntryContext,
    MarketStrengthResult,
    MarketStructureResult,
    OptionSelectionResult,
    PremiumMomentumResult,
    RiskResult,
    SignalStabilityResult,
    SupportResistanceResult,
    TradingRulesResult,
    TrendResult,
    VolatilityResult,
)
from .models.market_snapshot import MarketSnapshot, SessionState
from .utils.decision_diagnostics import build_cycle_diagnostics
from .utils.decision_logger import DecisionLogger
from .utils.error_handling import EngineFailureTracker, FatalBrokerError, neutral_engine_result, safe_analyze
from .utils.rolling_history import RollingHistory

logger = logging.getLogger("trading_decision_engine.orchestrator")

# Operational timings (history depth, status-log throttle, exit retry pacing, engine
# failure escalation, config reload cadence) all come from StrategyConfig — see
# config/README.md "Orchestrator operational tuning".


class Orchestrator:
    def __init__(
        self,
        adapter: GrowwExecutionAdapter,
        config: StrategyConfig,
        index: str,
        expiry_date: date,
        lot_size: int,
        log_dir: str,
        mode: str,
        on_decision: Callable[[datetime, str | None, DecisionResult], None] | None = None,
        validate_orders: bool = True,
        config_path: str | None = None,
        profile: str | None = None,
        on_diagnostics: Callable[[dict], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        # Observer for the per-cycle transparency object (console dashboard, custom
        # sinks). Purely one-way: exceptions are swallowed, nothing flows back.
        self._on_diagnostics = on_diagnostics
        # Live-reload bookkeeping: where the config was loaded from (None = default
        # path), which profile the CLI pinned (None = follow the file's active_profile),
        # and the last-seen file mtime. Checked every config_reload_check_seconds.
        self._config_path = config_path
        self._profile = profile
        self._config_mtime = config_files_mtime(config_path)
        self._last_reload_check_ts: datetime | None = None
        self._index = index
        self._expiry_date = expiry_date
        self._lot_size = lot_size
        self._mode = mode
        # Generic observation hook (e.g. for DecisionComparator in replay mode) — the
        # Orchestrator has no awareness of what, if anything, consumes it.
        self._on_decision = on_decision
        # True (default): block on order-status polling and confirm actual executed
        # price/qty before treating a trade as open/closed (PROD10FEB's VALIDATE_ORDERS
        # pattern — its own code recommends True even though it shipped defaulting to
        # False). False: trust the immediate place_order response and an estimated
        # price, same as before this flag existed — lower latency, less certainty.
        self._validate_orders = validate_orders

        self._trend_engine = TrendEngine(config)
        self._structure_engine = MarketStructureEngine(config)
        self._sr_engine = SupportResistanceEngine(config)
        self._momentum_engine = PremiumMomentumEngine(config)
        self._option_selection_engine = OptionSelectionEngine(config)
        self._breakout_engine = BreakoutEngine(config)
        self._strength_engine = MarketStrengthEngine(config)
        self._volatility_engine = VolatilityEngine(config)
        self._rules_engine = TradingRulesEngine(config)
        self._risk_engine = RiskEngine(config)
        self._stability_engine = SignalStabilityEngine(config)
        self._decision_engine = DecisionEngine(config)
        self._sizing_engine = PositionSizingEngine(config)
        self._trade_manager = TradeManager(config)

        self._logger = DecisionLogger(log_dir, mode)
        self._failure_tracker = EngineFailureTracker(escalation_threshold=config.engine_failure_escalation_threshold)

        history_age = config.stability_history_max_age_seconds
        self._trend_history: RollingHistory[TrendResult] = RollingHistory(history_age)
        self._premium_history: RollingHistory[PremiumMomentumResult] = RollingHistory(history_age)
        self._structure_history: RollingHistory[MarketStructureResult] = RollingHistory(history_age)
        self._breakout_history: RollingHistory[BreakoutResult] = RollingHistory(history_age)
        self._sr_history: RollingHistory[SupportResistanceResult] = RollingHistory(history_age)

        self._state = OrchestratorState.MARKET_CLOSED
        self._wait_mode_start: datetime | None = None
        self._cooldown_until: datetime | None = None

        # Session bookkeeping the Orchestrator owns and exposes to the data source via
        # session_state(); mutated only here, never inside an engine.
        self._trades_today = 0
        self._consecutive_losses = 0
        self._daily_pnl = 0.0
        self._current_exposure = 0.0
        self._current_instrument: dict | None = None
        self._current_lots = 0
        self._current_order_id: str | None = None
        self._order_pending = False
        self._exit_retry_count = 0
        self._last_exit_attempt_ts: datetime | None = None
        # Engine names currently past EngineFailureTracker's escalation threshold — set
        # by _run_pure_engines() every cycle, consumed by _run_analysis_cycle() to
        # suppress new entries (never blocks monitoring an already-open trade) and by
        # _log_engine_escalation() to alert exactly once per escalation onset rather
        # than spamming every tick. See docs/DESIGN.md §9.
        self._escalated_engines: set[str] = set()

        # session_state() is called on every tick; a live get_margins() REST call per
        # tick would blow through Groww's rate limit almost immediately, so cache it on
        # the same bounded interval as the option-chain refresh instead of the engine
        # heartbeat.
        self._cached_margin: float = config.max_exposure
        self._margin_cache_ts: datetime | None = None
        self._last_status_log_ts: datetime | None = None

    # ------------------------------------------------------------------ session state
    def _current_margin(self) -> float:
        if self._adapter.dry_run:
            return self._config.max_exposure
        now = datetime.now()
        stale = (
            self._margin_cache_ts is None
            or (now - self._margin_cache_ts).total_seconds() >= self._config.option_chain_refresh_seconds
        )
        if stale:
            try:
                self._cached_margin = self._adapter.get_margins().option_buy_balance_available
                self._margin_cache_ts = now
            except FatalBrokerError:
                raise  # unrecoverable auth failure — must reach on_snapshot's STOPPED handling, not be swallowed
            except Exception:  # noqa: BLE001 - keep the last known-good value on a transient failure
                logger.exception("Failed to refresh margin; using last cached value")
        return self._cached_margin

    def session_state(self) -> SessionState:
        return SessionState(
            already_in_trade=self._trade_manager.state != TradeLifecycleState.IDLE,
            order_pending=self._order_pending,
            margin_available=self._current_margin(),
            broker_connected=self._adapter.is_connected(),
            trades_today=self._trades_today,
            consecutive_losses=self._consecutive_losses,
            daily_pnl=self._daily_pnl,
            current_exposure=self._current_exposure,
            cooldown_until=self._cooldown_until,
        )

    # ------------------------------------------------------------------ market hours
    def _is_near_market_close(self, ts: datetime) -> bool:
        close_dt = datetime.combine(ts.date(), MARKET_CLOSE_TIME)
        return timedelta(0) <= (close_dt - ts) <= timedelta(minutes=self._config.market_close_buffer_minutes)

    # ------------------------------------------------------------------ main entrypoint
    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        if self._state == OrchestratorState.STOPPED:
            return
        self._maybe_reload_config()
        try:
            self._dispatch(snapshot)
        except FatalBrokerError as exc:
            logger.critical("Fatal broker error — halting Orchestrator: %s", exc)
            self._set_state(OrchestratorState.STOPPED, snapshot.timestamp, str(exc))
        except Exception:  # noqa: BLE001 - one bad tick must never kill the pipeline
            logger.exception("Unhandled error processing snapshot at %s", snapshot.timestamp)

    # ------------------------------------------------------------------ live config reload
    def _maybe_reload_config(self) -> None:
        """Cheap mtime poll (wall clock, every config_reload_check_seconds) — a JSON
        edit takes effect on the next tick without restarting. 0 disables the check.
        Runs between decision cycles, never inside one, so a cycle always sees one
        consistent config.
        """
        interval = self._config.config_reload_check_seconds
        if interval <= 0:
            return
        now = datetime.now()
        if self._last_reload_check_ts is not None and (now - self._last_reload_check_ts).total_seconds() < interval:
            return
        self._last_reload_check_ts = now
        current_mtime = config_files_mtime(self._config_path)
        if current_mtime > self._config_mtime:
            self._config_mtime = current_mtime
            self.reload_strategy()

    def reload_strategy(self) -> None:
        """Reload strategy.json (+ active profile) and swap the new config into every
        engine atomically. Open-trade state, rolling histories, and session counters
        are untouched — only thresholds/weights change. Safe to call any time; a broken
        JSON edit is caught and the previous config stays live.
        """
        import dataclasses as _dc

        try:
            new_config = StrategyConfig.load(self._config_path, profile=self._profile)
        except Exception as exc:  # noqa: BLE001 - a bad edit must never kill live trading
            logger.error("Config reload FAILED — keeping previous config: %s", exc)
            return

        changed = {
            f.name: (getattr(self._config, f.name), getattr(new_config, f.name))
            for f in _dc.fields(StrategyConfig)
            if getattr(self._config, f.name) != getattr(new_config, f.name)
        }
        if not changed:
            return
        self._config = new_config
        for engine in (
            self._trend_engine, self._structure_engine, self._sr_engine, self._momentum_engine,
            self._option_selection_engine, self._breakout_engine, self._strength_engine,
            self._volatility_engine, self._rules_engine, self._risk_engine,
            self._stability_engine, self._decision_engine, self._sizing_engine, self._trade_manager,
        ):
            engine._config = new_config  # every engine reads self._config per analyze() call
        logger.info(
            "Strategy config reloaded (profile=%r) — %d parameter(s) changed: %s",
            new_config.active_profile or None, len(changed),
            ", ".join(f"{k}: {old!r} -> {new!r}" for k, (old, new) in sorted(changed.items())),
        )

    def _set_state(self, new_state: OrchestratorState, ts: datetime, note: str = "") -> None:
        if new_state == self._state:
            return
        self._state = new_state
        logger.info("[%s] %s%s", ts.strftime("%H:%M:%S"), new_state.value, f" — {note}" if note else "")

    def _dispatch(self, snapshot: MarketSnapshot) -> None:
        ts = snapshot.timestamp

        if self._state == OrchestratorState.MARKET_CLOSED:
            if is_market_open_time(ts):
                self._wait_mode_start = ts
                self._set_state(OrchestratorState.WAIT_MODE, ts, f"market open, waiting {self._config.wait_after_open_minutes}min before analysis")
            else:
                return

        if self._state == OrchestratorState.WAIT_MODE:
            assert self._wait_mode_start is not None
            if (ts - self._wait_mode_start).total_seconds() >= self._config.wait_after_open_minutes * 60:
                self._set_state(OrchestratorState.ANALYZING, ts, "wait period over, analysis starting")
            else:
                return

        if self._state == OrchestratorState.COOLDOWN:
            assert self._cooldown_until is not None
            if ts < self._cooldown_until:
                return
            self._set_state(OrchestratorState.ANALYZING, ts, "cooldown elapsed, resuming analysis")

        if self._trade_manager.state == TradeLifecycleState.IDLE and self._is_near_market_close(ts):
            self._set_state(OrchestratorState.MARKET_CLOSING, ts, "within close buffer, no new entries")
        if not is_market_open_time(ts) and self._trade_manager.state == TradeLifecycleState.IDLE:
            self._set_state(OrchestratorState.MARKET_CLOSED, ts, "market closed")
            return

        if self._trade_manager.state != TradeLifecycleState.IDLE:
            self._run_trade_monitoring_cycle(snapshot)
        elif self._state == OrchestratorState.MARKET_CLOSING:
            return  # flat and past close-buffer: no new entries, just wait for MARKET_CLOSED
        else:
            self._run_analysis_cycle(snapshot)

    # ------------------------------------------------------------------ engines fan-out
    def _run_pure_engines(self, snapshot: MarketSnapshot) -> dict:
        tracker = self._failure_tracker
        escalated: set[str] = set()

        def _track(name: str, result_and_escalate: tuple) -> object:
            result, escalate = result_and_escalate
            if escalate:
                escalated.add(name)
            return result

        trend = _track("trend", safe_analyze(
            "trend", self._trend_engine.analyze,
            lambda r: neutral_engine_result(TrendResult, r, ehma_value=0.0, ema100_value=0.0, trend_angle=0.0, trend_strength=0.0),
            tracker, snapshot,
        ))
        structure = _track("market_structure", safe_analyze(
            "market_structure", self._structure_engine.analyze,
            lambda r: neutral_engine_result(MarketStructureResult, r, structure=MarketStructure.SIDEWAYS, strength=0.0),
            tracker, snapshot,
        ))
        support_resistance = _track("support_resistance", safe_analyze(
            "support_resistance", self._sr_engine.analyze,
            lambda r: neutral_engine_result(
                SupportResistanceResult, r, levels=(), nearest_support=snapshot.spot, nearest_resistance=snapshot.spot,
                distance_to_support=0.0, distance_to_resistance=0.0, breakout=False, breakdown=False,
            ),
            tracker, snapshot,
        ))
        momentum = _track("premium_momentum", safe_analyze(
            "premium_momentum", self._momentum_engine.analyze,
            lambda r: neutral_engine_result(PremiumMomentumResult, r, velocity=0.0, acceleration=0.0, higher_highs=False, higher_lows=False, consistency=0.0),
            tracker, snapshot,
        ))
        option_selection = _track("option_selection", safe_analyze(
            "option_selection", self._option_selection_engine.analyze,
            lambda r: neutral_engine_result(
                OptionSelectionResult, r, best_ce_symbol=None, best_pe_symbol=None, ce_premium=None, pe_premium=None,
                ce_liquidity_score=0.0, pe_liquidity_score=0.0, ce_spread_score=0.0, pe_spread_score=0.0,
            ),
            tracker, snapshot,
        ))
        breakout = _track("breakout", safe_analyze(
            "breakout", self._breakout_engine.analyze,
            lambda r: neutral_engine_result(BreakoutResult, r, breakout_confirmed=False, breakdown_confirmed=False, confirmation_bars_elapsed=0),
            tracker, snapshot, support_resistance,
        ))
        market_strength = _track("market_strength", safe_analyze(
            "market_strength", self._strength_engine.analyze,
            lambda r: neutral_engine_result(MarketStrengthResult, r, candle_speed=0.0, range_expansion=0.0, consolidation_score=0.0, trend_confidence=0.0),
            tracker, snapshot,
        ))
        volatility = _track("volatility", safe_analyze(
            "volatility", self._volatility_engine.analyze,
            lambda r: neutral_engine_result(VolatilityResult, r, acceptable=False, spread_pct=0.0, spike_score=0.0, gap_detected=False, whipsaw_detected=False),
            tracker, snapshot,
        ))
        trading_rules = _track("trading_rules", safe_analyze(
            "trading_rules", self._rules_engine.analyze,
            lambda r: neutral_engine_result(TradingRulesResult, r, allowed=False, trades_today=self._trades_today, consecutive_losses=self._consecutive_losses, is_expiry_day=False, near_market_close=False),
            tracker, snapshot.session, snapshot.timestamp, self._expiry_date,
        ))
        risk = _track("risk", safe_analyze(
            "risk", self._risk_engine.analyze,
            lambda r: neutral_engine_result(RiskResult, r, safe_to_trade=False, already_in_trade=snapshot.session.already_in_trade, order_pending=snapshot.session.order_pending, broker_connected=snapshot.session.broker_connected),
            tracker, snapshot.session,
        ))

        self._update_engine_escalation(escalated, snapshot.timestamp)

        return {
            "trend": trend, "market_structure": structure, "support_resistance": support_resistance,
            "premium_momentum": momentum, "option_selection": option_selection, "breakout": breakout,
            "market_strength": market_strength, "volatility": volatility, "trading_rules": trading_rules, "risk": risk,
        }

    def _update_engine_escalation(self, escalated: set[str], ts: datetime) -> None:
        newly_escalated = escalated - self._escalated_engines
        recovered = self._escalated_engines - escalated
        if newly_escalated:
            logger.critical(
                "[%s] ALERT: engine(s) %s failing repeatedly — suppressing new entries until recovered (still monitoring any open trade)",
                ts.strftime("%H:%M:%S"), sorted(newly_escalated),
            )
        if recovered:
            logger.info("[%s] Engine(s) %s recovered — new entries no longer suppressed", ts.strftime("%H:%M:%S"), sorted(recovered))
        self._escalated_engines = escalated

    def _update_history(self, snapshot: MarketSnapshot, results: dict) -> None:
        ts = snapshot.timestamp
        self._trend_history.append(ts, results["trend"])
        self._premium_history.append(ts, results["premium_momentum"])
        self._structure_history.append(ts, results["market_structure"])
        self._breakout_history.append(ts, results["breakout"])
        self._sr_history.append(ts, results["support_resistance"])

    def _build_stability_input(self, snapshot: MarketSnapshot, results: dict) -> SignalStabilityInput:
        required = required_confirmation_seconds(results["trend"], results["premium_momentum"], self._config)
        now = snapshot.timestamp
        # Window by the FULL rolling-history depth, not by `required` itself — SignalStabilityEngine
        # needs to see further back than the minimum required window to actually prove
        # elapsed stability has exceeded it, not just barely reach it (windowing to
        # exactly `required` would cap confirmation_seconds_elapsed just under `required`
        # by construction, making `stable=True` nearly unreachable).
        lookback = self._config.stability_history_max_age_seconds
        return SignalStabilityInput(
            trend_history=self._trend_history.window(now, lookback),
            premium_history=self._premium_history.window(now, lookback),
            structure_history=self._structure_history.window(now, lookback),
            breakout_history=self._breakout_history.window(now, lookback),
            support_resistance_history=self._sr_history.window(now, lookback),
            required_seconds=required,
            now=now,
        )

    # ------------------------------------------------------------------ ANALYZING / entry
    def _run_analysis_cycle(self, snapshot: MarketSnapshot) -> None:
        self._set_state(OrchestratorState.ANALYZING, snapshot.timestamp)
        results = self._run_pure_engines(snapshot)
        self._update_history(snapshot, results)
        stability_input = self._build_stability_input(snapshot, results)
        signal_stability, _ = safe_analyze(
            "signal_stability", self._stability_engine.analyze,
            lambda r: neutral_engine_result(
                SignalStabilityResult, r, stable=False, confirmation_seconds_elapsed=0.0,
                required_seconds=stability_input.required_seconds,
            ),
            self._failure_tracker, stability_input,
        )

        decision_input = DecisionInput(
            trend=results["trend"], market_structure=results["market_structure"],
            support_resistance=results["support_resistance"], premium_momentum=results["premium_momentum"],
            option_selection=results["option_selection"], breakout=results["breakout"],
            market_strength=results["market_strength"], volatility=results["volatility"],
            trading_rules=results["trading_rules"], risk=results["risk"], signal_stability=signal_stability,
        )
        decision = self._decision_engine.decide(decision_input)

        diagnostics = (
            build_cycle_diagnostics(snapshot.timestamp, results, signal_stability, decision, self._config)
            if self._config.diagnostics_enabled or self._on_diagnostics is not None
            else None
        )
        if self._on_diagnostics is not None and diagnostics is not None:
            try:
                self._on_diagnostics(diagnostics)
            except Exception:  # noqa: BLE001 - a display bug must never affect trading
                logger.exception("on_diagnostics observer raised")
        latest_tick = snapshot.premium_history[-1] if snapshot.premium_history else None
        self._logger.log_decision(
            timestamp=snapshot.timestamp, spot=snapshot.spot,
            ce_premium=latest_tick.ce_premium if latest_tick else None,
            pe_premium=latest_tick.pe_premium if latest_tick else None,
            trend=results["trend"], structure=results["market_structure"], support_resistance=results["support_resistance"],
            momentum=results["premium_momentum"], stability=signal_stability, option_selection=results["option_selection"],
            breakout=results["breakout"], market_strength=results["market_strength"], volatility=results["volatility"],
            trading_rules=results["trading_rules"], risk=results["risk"], decision=decision,
            diagnostics=diagnostics if self._config.diagnostics_enabled else None,
        )

        if self._on_decision is not None:
            option_selection: OptionSelectionResult = results["option_selection"]
            instrument = (
                option_selection.best_ce_symbol if decision.direction == Direction.BULLISH else option_selection.best_pe_symbol
            )
            self._on_decision(snapshot.timestamp, instrument, decision)

        self._log_decision_to_console(snapshot, decision)

        # CONFIRMING reflects the (possibly multi-tick) period where the trend has
        # cleared its threshold but SignalStabilityEngine hasn't yet proven it held
        # stable for the full adaptive window — observable per docs/DESIGN.md §6,
        # distinct from a plain ANALYZING tick where trend itself hasn't confirmed yet.
        trend_confirmed = results["trend"].direction != Direction.NEUTRAL and results["trend"].score >= self._config.trend_threshold
        if trend_confirmed and not signal_stability.stable and decision.action not in (TradeAction.BUY, TradeAction.SELL):
            self._set_state(OrchestratorState.CONFIRMING, snapshot.timestamp, "trend confirmed, waiting for signal stability")

        if decision.action in (TradeAction.BUY, TradeAction.SELL):
            if self._escalated_engines:
                logger.warning(
                    "[%s] Suppressing %s entry: engine(s) %s repeatedly failing this cycle",
                    snapshot.timestamp.strftime("%H:%M:%S"), decision.action.value, sorted(self._escalated_engines),
                )
                return
            self._set_state(OrchestratorState.SIZING, snapshot.timestamp)
            self._handle_entry(snapshot, decision, results)

    def _log_decision_to_console(self, snapshot: MarketSnapshot, decision) -> None:
        ts = snapshot.timestamp
        if decision.action in (TradeAction.BUY, TradeAction.SELL):
            logger.info(
                "[%s] >>> %s signal — confidence %.0f%%, quality %.0f — %s",
                ts.strftime("%H:%M:%S"), decision.action.value, decision.confidence,
                decision.trade_quality_score, "; ".join(decision.reasons),
            )
            return
        # HOLD/REJECT: a throttled heartbeat so the console isn't silent for minutes,
        # but isn't flooded on every tick either.
        due = (
            self._last_status_log_ts is None
            or (ts - self._last_status_log_ts).total_seconds() >= self._config.status_log_interval_seconds
        )
        if not due:
            return
        self._last_status_log_ts = ts
        top_reason = decision.reasons[0] if decision.reasons else "no reasons reported"
        logger.info(
            "[%s] spot %.2f — %s (buy %.0f / sell %.0f) — %s",
            ts.strftime("%H:%M:%S"), snapshot.spot, decision.action.value,
            decision.buy_score, decision.sell_score, top_reason,
        )

    def _handle_entry(self, snapshot: MarketSnapshot, decision, results: dict) -> None:
        option_selection: OptionSelectionResult = results["option_selection"]
        margin_available = snapshot.session.margin_available
        position_size = self._sizing_engine.size(decision, option_selection, margin_available, self._lot_size)

        if position_size.lots <= 0:
            logger.info("[%s] Entry rejected by sizing: %s", snapshot.timestamp.strftime("%H:%M:%S"), position_size.reasons)
            self._set_state(OrchestratorState.ANALYZING, snapshot.timestamp)
            return

        self._set_state(OrchestratorState.ORDER_PLACING, snapshot.timestamp)
        instrument_symbol = (
            option_selection.best_ce_symbol if decision.direction == Direction.BULLISH else option_selection.best_pe_symbol
        )
        premium_estimate = option_selection.ce_premium if decision.direction == Direction.BULLISH else option_selection.pe_premium
        # "ltp" is DRY_RUN's paper-fill price hint (see GrowwExecutionAdapter.place_order)
        # — the live feed populates the adapter's own last-seen-LTP cache, but replay/
        # shadow-without-a-tick-yet never does, so without this hint a paper fill would
        # settle at price 0.0.
        instrument = {"trading_symbol": instrument_symbol, "exchange": INDEX_EXCHANGE[Index(self._index)], "ltp": premium_estimate}

        self._order_pending = True
        try:
            order = self._adapter.place_order(instrument, position_size.lots * self._lot_size, "BUY")
        finally:
            self._order_pending = False

        ts_str = snapshot.timestamp.strftime("%H:%M:%S")
        filled_lots = position_size.lots

        if self._validate_orders:
            fill_status = self._adapter.wait_for_fill(order.order_id, "BUY")
            if fill_status not in ORDER_SUCCESS_STATUSES:
                logger.warning("[%s] BUY order %s did not execute (status=%s) — aborting entry", ts_str, order.order_id, fill_status)
                self._set_state(OrchestratorState.ANALYZING, snapshot.timestamp)
                return
            entry_price, executed_qty = self._adapter.get_order_executed_price(order.order_id)
            if entry_price <= 0 or executed_qty <= 0:
                logger.warning("[%s] Could not confirm executed price/qty for BUY order %s — aborting entry", ts_str, order.order_id)
                self._set_state(OrchestratorState.ANALYZING, snapshot.timestamp)
                return
            filled_lots = executed_qty // self._lot_size  # handles a partial fill
            if filled_lots <= 0:
                logger.warning("[%s] BUY order %s filled %d units — less than one lot, aborting entry", ts_str, order.order_id, executed_qty)
                self._set_state(OrchestratorState.ANALYZING, snapshot.timestamp)
                return
        else:
            if order.order_status not in ("EXECUTED", "COMPLETED", "PLACED"):
                logger.warning("[%s] Order not placed successfully: %s", ts_str, order)
                self._set_state(OrchestratorState.ANALYZING, snapshot.timestamp)
                return
            entry_price, _ = self._adapter.get_order_executed_price(order.order_id)
            if entry_price <= 0:
                entry_price = option_selection.ce_premium if decision.direction == Direction.BULLISH else option_selection.pe_premium

        entry_context = EntryContext(
            trend=results["trend"], market_structure=results["market_structure"],
            support_resistance=results["support_resistance"], premium_momentum=results["premium_momentum"],
            breakout=results["breakout"], market_strength=results["market_strength"],
            volatility=results["volatility"], decision=decision,
        )
        self._trade_manager.on_trade_opened(
            instrument=instrument_symbol, entry_price=entry_price, lots=filled_lots, lot_size=self._lot_size,
            direction=decision.direction, entry_context=entry_context, now=snapshot.timestamp, spot=snapshot.spot,
        )
        self._current_instrument = instrument
        self._current_lots = filled_lots
        self._current_order_id = order.order_id
        self._trades_today += 1
        self._current_exposure += filled_lots * self._lot_size * entry_price
        self._logger.log_trade_opened(snapshot.timestamp, instrument_symbol, entry_price, filled_lots, entry_context)
        logger.info(
            "[%s] *** BOUGHT %d lot(s) x %d of %s @ %.2f (₹%.2f deployed)%s ***",
            ts_str, filled_lots, self._lot_size, instrument_symbol, entry_price, filled_lots * self._lot_size * entry_price,
            " [validated]" if self._validate_orders else "",
        )
        self._set_state(OrchestratorState.IN_TRADE, snapshot.timestamp)

    # ------------------------------------------------------------------ IN_TRADE / exit
    def _run_trade_monitoring_cycle(self, snapshot: MarketSnapshot) -> None:
        # Per docs/DESIGN.md §6, IN_TRADE --> MARKET_CLOSING once within the close
        # buffer (forcing exit) rather than staying IN_TRADE for the whole buffer
        # window — surfaced here (not just at the top-level dispatch, which only
        # covers the flat/IDLE case) since this is the only place a trade is open.
        force_exit = self._is_near_market_close(snapshot.timestamp)
        if force_exit:
            self._set_state(OrchestratorState.MARKET_CLOSING, snapshot.timestamp, "within close buffer, forcing exit")
        else:
            self._set_state(OrchestratorState.IN_TRADE, snapshot.timestamp)
        results = self._run_pure_engines(snapshot)
        self._update_history(snapshot, results)

        trade_state = self._trade_manager.update(
            snapshot, results["trend"], results["breakout"], results["support_resistance"],
            results["premium_momentum"], results["risk"],
        )

        if self._on_diagnostics is not None and self._current_instrument is not None:
            try:
                self._on_diagnostics({
                    "type": "trade_panel",
                    "timestamp": snapshot.timestamp.isoformat(),
                    "instrument": self._current_instrument["trading_symbol"],
                    "lots": self._current_lots,
                    "entry_price": trade_state.entry_price,
                    "current_price": trade_state.current_price,
                    "pnl": trade_state.current_profit - trade_state.current_loss,
                    "highest_premium": trade_state.highest_premium,
                    "lowest_premium": trade_state.lowest_premium,
                    "time_in_trade_seconds": trade_state.time_in_trade_seconds,
                })
            except Exception:  # noqa: BLE001 - a display bug must never affect trading
                logger.exception("on_diagnostics observer raised")

        if trade_state.state == TradeLifecycleState.EXIT_TRIGGERED or force_exit:
            exit_reason = trade_state.exit_reason or "Market close buffer reached"
            self._execute_exit(snapshot, exit_reason, current_premium=trade_state.current_price)
        else:
            self._log_trade_status_to_console(snapshot, trade_state)

    def _log_trade_status_to_console(self, snapshot: MarketSnapshot, trade_state) -> None:
        ts = snapshot.timestamp
        due = (
            self._last_status_log_ts is None
            or (ts - self._last_status_log_ts).total_seconds() >= self._config.status_log_interval_seconds
        )
        if not due:
            return
        self._last_status_log_ts = ts
        pnl = trade_state.current_profit - trade_state.current_loss
        logger.info(
            "[%s] IN_TRADE %s @ %.2f, now %.2f, P&L ₹%.2f, %.0fs in trade",
            ts.strftime("%H:%M:%S"), self._current_instrument["trading_symbol"], trade_state.entry_price,
            trade_state.current_price, pnl, trade_state.time_in_trade_seconds,
        )

    def _execute_exit(self, snapshot: MarketSnapshot, exit_reason: str, current_premium: float = 0.0) -> None:
        self._set_state(OrchestratorState.EXITING, snapshot.timestamp)
        assert self._current_instrument is not None
        ts_str = snapshot.timestamp.strftime("%H:%M:%S")

        if self._exit_retry_count > 0 and self._last_exit_attempt_ts is not None:
            since_last = (snapshot.timestamp - self._last_exit_attempt_ts).total_seconds()
            if since_last < self._config.exit_retry_min_interval_seconds:
                return  # still backing off — the exit condition remains flagged, we'll try again shortly

        # DRY_RUN paper fills use the instrument's "ltp" hint when no live-feed LTP is
        # cached (always the case offline). Left as-is, the exit would settle at the
        # ENTRY-time hint, silently zeroing every replay trade's P&L — refresh it with
        # the premium the Trade Manager just computed from the current snapshot.
        exit_instrument = self._current_instrument
        if current_premium > 0:
            exit_instrument = {**self._current_instrument, "ltp": current_premium}

        self._last_exit_attempt_ts = snapshot.timestamp
        self._order_pending = True
        try:
            order = self._adapter.place_order(exit_instrument, self._current_lots * self._lot_size, "SELL")
        finally:
            self._order_pending = False

        if self._validate_orders:
            fill_status = self._adapter.wait_for_fill(order.order_id, "SELL")
            if fill_status not in ORDER_SUCCESS_STATUSES:
                # The position is presumably still open — do NOT mark it closed. Stay
                # IN_TRADE; the next eligible tick (after the backoff above) will
                # re-detect the same exit condition and retry, rather than silently
                # losing track of a position that never actually left the account.
                self._exit_retry_count += 1
                escalated = self._exit_retry_count >= self._config.exit_retry_escalation_threshold
                logger.warning(
                    "[%s] SELL order %s did not execute (status=%s), attempt #%d — position considered still OPEN%s",
                    ts_str, order.order_id, fill_status, self._exit_retry_count,
                    " — ESCALATING: repeated exit failures, manual intervention may be required" if escalated else ", will retry",
                )
                self._set_state(OrchestratorState.IN_TRADE, snapshot.timestamp)
                return
            exit_price, _ = self._adapter.get_order_executed_price(order.order_id)
        else:
            exit_price, _ = self._adapter.get_order_executed_price(order.order_id)

        if exit_price <= 0:
            exit_price = snapshot.premium_history[-1].ce_premium if snapshot.premium_history else 0.0

        self._exit_retry_count = 0
        self._last_exit_attempt_ts = None
        final_state = self._trade_manager.on_trade_closed(exit_price=exit_price, now=snapshot.timestamp)
        pnl = final_state.current_profit - final_state.current_loss

        self._daily_pnl += pnl
        self._consecutive_losses = 0 if pnl >= 0 else self._consecutive_losses + 1
        self._current_exposure = max(0.0, self._current_exposure - final_state.entry_price * self._current_lots * self._lot_size)
        self._logger.log_trade_closed(snapshot.timestamp, self._current_instrument["trading_symbol"], exit_price, pnl, exit_reason)
        logger.info(
            "[%s] *** SOLD %s @ %.2f — P&L ₹%.2f — %s — daily P&L ₹%.2f%s ***",
            ts_str, self._current_instrument["trading_symbol"],
            exit_price, pnl, exit_reason, self._daily_pnl,
            " [validated]" if self._validate_orders else "",
        )
        if self._on_diagnostics is not None:
            # Feed session analytics (win rate by entry score, etc.) — the entry-time
            # engine scores come from the EntryContext captured when the trade opened.
            entry = final_state.entry_context
            try:
                self._on_diagnostics({
                    "type": "trade_closed",
                    "timestamp": snapshot.timestamp.isoformat(),
                    "instrument": self._current_instrument["trading_symbol"],
                    "pnl": pnl,
                    "exit_reason": exit_reason,
                    "entry_scores": {
                        "trend": entry.trend.score,
                        "market_structure": entry.market_structure.score,
                        "premium_momentum": entry.premium_momentum.score,
                        "breakout": entry.breakout.score,
                        "market_strength": entry.market_strength.score,
                        "trade_quality": entry.decision.trade_quality_score,
                    } if entry is not None else {},
                })
            except Exception:  # noqa: BLE001 - a display bug must never affect trading
                logger.exception("on_diagnostics observer raised")

        self._current_instrument = None
        self._current_lots = 0
        self._current_order_id = None
        self._cooldown_until = snapshot.timestamp + timedelta(seconds=self._config.cooldown_seconds)
        self._set_state(OrchestratorState.COOLDOWN, snapshot.timestamp, f"next entry allowed after {self._cooldown_until.strftime('%H:%M:%S')}")
