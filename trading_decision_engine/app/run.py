"""Entrypoint: python -m trading_decision_engine.app.run --mode live|shadow|replay ...
See docs/DESIGN.md §11 (operating modes), §15 (verification).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import interactive_config as interactive
from .broker.groww_execution_adapter import GrowwExecutionAdapter
from .broker.instrument_master import InstrumentMaster, refresh_instrument_csv
from .config.strategy import StrategyConfig
from .market_data.decision_comparator import DecisionComparator
from .market_data.groww_websocket_source import GrowwWebSocketMarketDataSource
from .market_data.historical_replay_builder import HistoricalReplayBuilder
from .market_data.manual_trade_importer import load_manual_trades
from .market_data.replay_source import ReplayMarketDataSource
from .market_data.replay_tick_io import load_replay_ticks, save_replay_ticks
from .orchestrator import Orchestrator

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[1] / "logs"


class _SuppressEmptyNatsErrorFilter(logging.Filter):
    """growwapi's NATS client does `logger.error("Error: %s", e)` on a transient
    reconnect while first establishing the socket; when the underlying exception
    stringifies to empty, that's a benign, self-recovering retry (confirmed
    empirically: always followed by "Socket connection successful" a couple seconds
    later) — suppress only that specific empty-content case. Any error with an actual
    message still gets through, since that could be a real problem.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().strip() != "Error:"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trading Decision Engine")
    parser.add_argument("--mode", choices=["live", "shadow", "replay"], default=None, help="Prompted interactively if omitted")
    parser.add_argument("--index", default=None, choices=list(interactive.INDEX_OPTIONS.values()), help="Prompted interactively if omitted")
    parser.add_argument("--expiry", default=None, help="Option expiry date YYYY-MM-DD — prompted (current/next) if omitted")
    parser.add_argument("--lots", type=int, default=None, help="Number of lots to trade — prompted if omitted")
    parser.add_argument("--premium-min", type=float, default=None, help="Min tradable premium — prompted (with --premium-max) if omitted")
    parser.add_argument("--premium-max", type=float, default=None, help="Max tradable premium — prompted (with --premium-min) if omitted")
    parser.add_argument("--lot-size", type=int, default=None, help="Exchange contract lot size (e.g. 65 for NIFTY) — auto-derived from instrument.csv if omitted; this is NOT the same as --lots")
    parser.add_argument("--instrument-csv", default=None, help="Path to instrument.csv (defaults to the repo-root instrument.csv)")
    parser.add_argument(
        "--validate-orders", action=argparse.BooleanOptionalAction, default=None,
        help="Block on order-status polling and confirm actual executed price/qty before treating a trade as "
             "open/closed (PROD10FEB's VALIDATE_ORDERS pattern). Prompted interactively if omitted (recommended: on). "
             "Use --no-validate-orders to trust the immediate place_order response instead (lower latency, less certainty).",
    )
    parser.add_argument("--config", default=None, help="Path to a strategy.json override")
    parser.add_argument(
        "--dashboard", action=argparse.BooleanOptionalAction, default=None,
        help="Continuously-updating console decision dashboard (confidence bars, per-engine scores/contributions, "
             "gate results). Default: on for live/shadow when stdout is a terminal; off for replay. "
             "Regular log lines are quieted while it is active.",
    )
    parser.add_argument(
        "--profile", default=None,
        help="Strategy profile from config/profiles/ (aggressive | balanced | conservative | scalping | any custom "
             "<name>.json). Overrides the active_profile key in strategy.json.",
    )
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--replay-file", default=None, help="Pre-built JSONL replay tick file (fully offline, no broker connection needed)")
    parser.add_argument("--save-replay-file", default=None, help="Where to save ticks fetched via --replay-start/--replay-end, for later offline reuse")
    parser.add_argument("--replay-start", default=None, help="Replay window start, YYYY-MM-DD HH:MM:SS (ignored if --replay-file is set)")
    parser.add_argument("--replay-end", default=None, help="Replay window end, YYYY-MM-DD HH:MM:SS (ignored if --replay-file is set)")
    parser.add_argument("--replay-speed", type=float, default=0.0, help="0 = as-fast-as-possible, 1.0 = real-time")
    parser.add_argument("--manual-trades", default=None, help="CSV/JSONL of manual trades to compare against (replay only)")
    parser.add_argument("--comparison-tolerance-seconds", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # growwapi's own connection-lifecycle logging ("Connection closed", "Reconnected")
    # is at INFO — quiet it to WARNING+ so only our own trading_decision_engine.* lines
    # show by default. That alone does NOT hide the SDK's benign empty "Error: " line
    # (it's logged at ERROR, which is above WARNING) — that needs its own filter, since
    # WARNING-level suppression can't distinguish it from a real ERROR-level problem.
    logging.getLogger("growwapi").setLevel(logging.WARNING)
    logging.getLogger("growwapi.groww.nats_client").addFilter(_SuppressEmptyNatsErrorFilter())
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    print("\n===== Trading Decision Engine — Configuration =====")
    if args.mode is None:
        args.mode = interactive.prompt_mode()
    if args.profile is None:
        args.profile = interactive.prompt_profile()
    if args.validate_orders is None:
        # Only meaningful when orders (real or simulated) are placed live/shadow;
        # replay always validates against the paper adapter anyway.
        args.validate_orders = interactive.prompt_validate_orders() if args.mode in ("live", "shadow") else True

    config = StrategyConfig.load(args.config, profile=args.profile)
    if config.active_profile:
        logging.info("Strategy profile active: %s", config.active_profile)
    log_dir = args.log_dir or str(DEFAULT_LOG_DIR)

    dry_run = args.mode != "live"
    # Only a fully-offline replay (--replay-file, no network at all) skips real auth.
    # SHADOW still needs a real authenticated session for real market data — it only
    # simulates order placement (dry_run), it does not go offline.
    offline = args.mode == "replay" and bool(args.replay_file)

    # Auto-refresh instrument.csv (download from Groww if missing or >1 day old) so no
    # manual download is ever needed; a failed download falls back to the existing file
    # with a warning, keeping fully-offline replay runs working with no network. Built
    # before the adapter so the adapter can resolve trading_symbol -> exchange_token
    # (needed to look up the live-feed LTP cache correctly in DRY_RUN paper fills).
    if not refresh_instrument_csv(args.instrument_csv):
        raise SystemExit("No instrument.csv available (download failed and no local copy) — cannot start")
    instruments = InstrumentMaster(args.instrument_csv)
    adapter = GrowwExecutionAdapter(config=config, dry_run=dry_run, offline=offline, instrument_master=instruments)

    index = args.index or interactive.prompt_index_selection()
    expiry = args.expiry or interactive.prompt_expiry_selection(instruments, index)
    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()

    lot_size = args.lot_size
    if lot_size is None:
        lot_size = instruments.lot_size_for(index, expiry)
        if lot_size is None:
            available = instruments.expiries_for(index)
            hint = f"available expiries in instrument.csv: {available}" if available else (
                f"no expiries found for {index} at all — instrument.csv may be stale, re-download it"
            )
            raise SystemExit(f"No {index} contract for expiry {expiry} — {hint} (or pass --lot-size explicitly)")
        logging.info("Contract lot size for %s %s: %d units/lot", index, expiry, lot_size)

    lots = args.lots if args.lots is not None else interactive.prompt_lots()
    if args.premium_min is not None and args.premium_max is not None:
        premium_min, premium_max = args.premium_min, args.premium_max
    else:
        premium_min, premium_max = interactive.prompt_premium_range()
    config = dataclasses.replace(config, default_lots=lots, premium_min=premium_min, premium_max=premium_max)
    print(
        f"\n{index} {expiry} — {lots} lot(s) x {lot_size} units/lot, "
        f"premium range ₹{premium_min:.2f}-₹{premium_max:.2f}, "
        f"order validation: {'ON' if args.validate_orders else 'OFF'}\n"
    )

    collected_decisions: list = []

    def _collect(ts, instrument, decision) -> None:
        collected_decisions.append((ts, instrument, decision))

    # Decision-transparency observers: SessionStatistics always aggregates (cheap,
    # pure); the console dashboard additionally renders when enabled. Both consume
    # the same diagnostics stream — trading is identical with them off.
    from .utils.session_statistics import SessionStatistics

    stats = SessionStatistics()
    use_dashboard = args.dashboard if args.dashboard is not None else (args.mode in ("live", "shadow") and sys.stdout.isatty())
    if use_dashboard:
        from .utils.console_dashboard import ConsoleDashboard

        # The dashboard feeds every event to `stats` itself, and appends the session
        # strip under each panel.
        observer = ConsoleDashboard(refresh_seconds=config.dashboard_refresh_seconds, stats=stats).update
        # The dashboard redraws over the whole screen; interleaved INFO logs would be
        # wiped mid-line anyway, so keep only warnings/errors on the console.
        logging.getLogger("trading_decision_engine").setLevel(logging.WARNING)
    else:
        observer = stats.update

    orchestrator = Orchestrator(
        adapter=adapter,
        config=config,
        index=index,
        expiry_date=expiry_date,
        lot_size=lot_size,
        log_dir=log_dir,
        mode=args.mode,
        on_decision=_collect if args.mode == "replay" else None,
        validate_orders=args.validate_orders,
        config_path=args.config,
        profile=args.profile,
        on_diagnostics=observer,
    )

    try:
        if args.mode in ("live", "shadow"):
            return _run_live_or_shadow(args, config, adapter, orchestrator, instruments, index, expiry)
        return _run_replay(args, config, adapter, orchestrator, collected_decisions, index, expiry)
    finally:
        _report_session_stats(stats, log_dir, args.mode)


def _report_session_stats(stats, log_dir: str, mode: str) -> None:
    """End-of-session engine-health report: printed for the operator and saved as JSON
    next to the decision logs for offline calibration.
    """
    from .utils.session_statistics import render_session_stats

    snapshot = stats.to_dict()
    if snapshot["decision_cycles"] == 0 and snapshot["trades"]["closed"] == 0:
        return
    print("\n" + render_session_stats(snapshot))
    out = Path(log_dir) / f"session_stats_{mode}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    stats.save(out)
    print(f"\nSession statistics saved: {out}")


def _run_live_or_shadow(args, config: StrategyConfig, adapter: GrowwExecutionAdapter, orchestrator: Orchestrator, instruments: InstrumentMaster, index: str, expiry: str) -> int:
    source = GrowwWebSocketMarketDataSource(
        adapter=adapter,
        config=config,
        index=index,
        expiry_date=expiry,
        session_provider=orchestrator.session_state,
        instrument_master=instruments,
    )
    stop_event = threading.Event()

    def _handle_sigint(signum, frame) -> None:
        logging.info("Shutting down (%s mode)...", args.mode)
        source.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_sigint)
    source.start(orchestrator.on_snapshot)
    logging.info("%s mode running (dry_run=%s). Press Ctrl+C to stop.", args.mode, adapter.dry_run)
    stop_event.wait()
    return 0


def _run_replay(args, config: StrategyConfig, adapter: GrowwExecutionAdapter, orchestrator: Orchestrator, collected_decisions: list, index: str, expiry: str) -> int:
    # DRY_RUN login is a no-op beyond marking the adapter "connected" (no network call) —
    # always call it so RiskEngine sees broker_connected=True, even in fully-offline
    # --replay-file runs which never otherwise touch the adapter.
    adapter.login()

    if args.replay_file:
        # Fully offline: no broker connection needed at all, per docs/DESIGN.md §15.
        ticks = load_replay_ticks(args.replay_file)
        logging.info("Loaded %d replay ticks from %s", len(ticks), args.replay_file)
    else:
        if not args.replay_start or not args.replay_end:
            raise SystemExit("--replay-file, or --replay-start and --replay-end, are required in replay mode")
        start = datetime.strptime(args.replay_start, "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(args.replay_end, "%Y-%m-%d %H:%M:%S")

        builder = HistoricalReplayBuilder(adapter)
        ticks = builder.build(index, expiry, config.candle_interval, start, end)
        logging.info("Built %d replay ticks from %s to %s", len(ticks), start, end)
        if args.save_replay_file:
            save_replay_ticks(ticks, args.save_replay_file)
            logging.info("Saved replay ticks to %s for future offline reuse", args.save_replay_file)

    source = ReplayMarketDataSource(ticks, session_provider=orchestrator.session_state, speed=args.replay_speed)
    source.start(orchestrator.on_snapshot)
    logging.info("Replay complete: %d decision cycles observed", len(collected_decisions))

    if args.manual_trades:
        manual_trades = load_manual_trades(args.manual_trades)
        report = DecisionComparator.compare(collected_decisions, manual_trades, args.comparison_tolerance_seconds)
        logging.info(
            "Comparison: %d/%d manual trades matched (%.1f%% agreement), %d bot-only, %d manual-only",
            len(report.matched), report.total_manual_trades, report.agreement_pct,
            len(report.bot_only), len(report.manual_only),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
