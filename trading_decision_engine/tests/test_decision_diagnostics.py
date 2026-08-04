"""Tests for the decision-transparency layer: diagnostics builder + console dashboard.
Explainability only — these verify the object faithfully mirrors already-computed
results and that rendering never touches trading state.
"""

from __future__ import annotations

import dataclasses
import io
import json
import unittest
from datetime import datetime

from trading_decision_engine.app.config.constants import Direction, TradeAction
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.engines.decision_engine import DecisionEngine
from trading_decision_engine.app.utils.console_dashboard import ConsoleDashboard, bar, render, render_trade_panel
from trading_decision_engine.app.utils.decision_diagnostics import build_cycle_diagnostics
from trading_decision_engine.tests.fixtures import make_decision_input

TS = datetime(2026, 7, 13, 10, 21, 32)


def _results_dict(inputs) -> dict:
    return {
        "trend": inputs.trend, "market_structure": inputs.market_structure,
        "support_resistance": inputs.support_resistance, "premium_momentum": inputs.premium_momentum,
        "option_selection": inputs.option_selection, "breakout": inputs.breakout,
        "market_strength": inputs.market_strength, "volatility": inputs.volatility,
        "trading_rules": inputs.trading_rules, "risk": inputs.risk,
    }


def _diag(cfg: StrategyConfig, inputs) -> dict:
    decision = DecisionEngine(cfg).decide(inputs)
    return build_cycle_diagnostics(TS, _results_dict(inputs), inputs.signal_stability, decision, cfg), decision


class TestDiagnosticsBuilder(unittest.TestCase):
    def setUp(self):
        # Threshold 10 so the strongly-bullish fixture produces a BUY.
        self.cfg = dataclasses.replace(StrategyConfig(), decision_score_threshold=10.0)

    def test_buy_cycle_has_all_engines_and_confidences_sum_to_100(self):
        diag, decision = _diag(self.cfg, make_decision_input())
        self.assertEqual(decision.action, TradeAction.BUY)
        self.assertEqual(diag["action"], "BUY")
        self.assertEqual(len(diag["engines"]), 11)  # 10 engines + signal_stability
        conf = diag["confidence"]
        self.assertAlmostEqual(conf["buy"] + conf["sell"] + conf["hold"], 100.0, delta=0.3)
        self.assertGreater(conf["buy"], conf["sell"])

    def test_contributions_reconstruct_buy_score_exactly(self):
        diag, decision = _diag(self.cfg, make_decision_input())
        total_contribution = sum(e["contribution"] for e in diag["engines"].values())
        # Option selection contributes to both sides; the winning-side reconstruction
        # is Stage 2's buy_score by definition.
        self.assertAlmostEqual(total_contribution, decision.buy_score, delta=0.05)

    def test_every_engine_exposes_required_fields(self):
        diag, _ = _diag(self.cfg, make_decision_input())
        for name, info in diag["engines"].items():
            for field in ("score", "confidence", "direction", "weight", "contribution", "passed", "explanation"):
                self.assertIn(field, info, f"{name} missing {field}")
            self.assertIsInstance(info["explanation"], list)

    def test_engine_specific_extras_present(self):
        diag, _ = _diag(self.cfg, make_decision_input())
        self.assertIn("velocity", diag["engines"]["premium_momentum"])
        self.assertIn("distance_to_resistance", diag["engines"]["support_resistance"])
        self.assertIn("best_ce", diag["engines"]["option_selection"])
        self.assertIn("elapsed_seconds", diag["engines"]["signal_stability"])
        self.assertIn("structure", diag["engines"]["market_structure"])

    def test_rejection_carries_actual_vs_required_per_failed_gate(self):
        diag, decision = _diag(self.cfg, make_decision_input(trend_score=42.0, trend_direction=Direction.NEUTRAL))
        self.assertEqual(decision.action, TradeAction.REJECT)
        self.assertFalse(diag["stage1"]["passed"])
        self.assertIn("trend", diag["stage1"]["failed_checks"])
        trend_check = diag["stage1"]["checks"]["trend"]
        self.assertFalse(trend_check["passed"])
        self.assertIn("42", trend_check["actual"])           # the actual score
        self.assertIn("60", trend_check["required"])          # cfg.trend_threshold (default 60)
        self.assertIn("NEUTRAL", trend_check["actual"])       # and the actual direction
        # Stage 2 was short-circuited and the object says so.
        self.assertFalse(diag["stage2"]["evaluated"])

    def test_disabled_gate_marked_not_judged(self):
        cfg = dataclasses.replace(self.cfg, require_trend=False)
        diag, _ = _diag(cfg, make_decision_input(trend_direction=Direction.NEUTRAL))
        chk = diag["stage1"]["checks"]["trend"]
        self.assertFalse(chk["enabled"])
        self.assertIsNone(chk["passed"])

    def test_engine_agreement_pct(self):
        diag, _ = _diag(self.cfg, make_decision_input(strength_direction=Direction.NEUTRAL))
        self.assertEqual(diag["stage2"]["engine_agreement"], "3/4")
        self.assertEqual(diag["stage2"]["engine_agreement_pct"], 75.0)

    def test_object_is_json_serializable(self):
        diag, _ = _diag(self.cfg, make_decision_input())
        json.dumps(diag)  # must not raise


class TestConsoleDashboard(unittest.TestCase):
    def setUp(self):
        self.cfg = dataclasses.replace(StrategyConfig(), decision_score_threshold=10.0)

    def test_bar_rendering_bounds(self):
        self.assertEqual(bar(0), "░" * 24)
        self.assertEqual(bar(100), "█" * 24)
        self.assertEqual(bar(150), "█" * 24)   # clamped
        self.assertEqual(bar(-5), "░" * 24)    # clamped
        self.assertEqual(len(bar(37)), 24)

    def test_render_buy_panel_contains_key_sections(self):
        diag, _ = _diag(self.cfg, make_decision_input())
        panel = render(diag)
        for token in ("BUY  confidence", "SELL confidence", "Trend", "Premium Momentum",
                      "STAGE 1 [PASS]", "STAGE 2", "FINAL: BUY", "█"):
            self.assertIn(token, panel)

    def test_render_rejection_panel_shows_actual_vs_required(self):
        diag, _ = _diag(self.cfg, make_decision_input(trend_score=42.0, trend_direction=Direction.NEUTRAL))
        panel = render(diag)
        self.assertIn("STAGE 1 [FAIL]", panel)
        self.assertIn("actual:", panel)
        self.assertIn("required:", panel)
        self.assertIn("not evaluated", panel)

    def test_render_trade_panel(self):
        panel = render_trade_panel({
            "type": "trade_panel", "timestamp": TS.isoformat(), "instrument": "NIFTY2671424200CE",
            "lots": 1, "entry_price": 121.35, "current_price": 123.9, "pnl": 165.75,
            "highest_premium": 124.15, "lowest_premium": 121.0, "time_in_trade_seconds": 42.0,
        })
        self.assertIn("IN TRADE", panel)
        self.assertIn("NIFTY2671424200CE", panel)
        self.assertIn("+165.75", panel)

    def test_dashboard_disabled_on_non_tty_and_never_raises(self):
        stream = io.StringIO()  # not a TTY -> dashboard stays silent
        dash = ConsoleDashboard(refresh_seconds=0.0, stream=stream)
        diag, _ = _diag(self.cfg, make_decision_input())
        dash.update(diag)
        self.assertEqual(stream.getvalue(), "")

    def test_dashboard_writes_on_tty_and_forces_redraw_on_buy(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        stream = FakeTTY()
        dash = ConsoleDashboard(refresh_seconds=3600.0, stream=stream)  # huge throttle
        diag, _ = _diag(self.cfg, make_decision_input())
        self.assertEqual(diag["action"], "BUY")
        dash.update(diag)   # BUY forces through the throttle
        dash.update(diag)   # second draw within throttle also forced (still BUY)
        self.assertIn("FINAL: BUY", stream.getvalue())


class TestObserverIsolation(unittest.TestCase):
    def test_orchestrator_survives_a_crashing_observer(self):
        import tempfile
        from datetime import date

        from trading_decision_engine.app.broker.groww_execution_adapter import GrowwExecutionAdapter
        from trading_decision_engine.app.orchestrator import Orchestrator
        from trading_decision_engine.tests.fixtures import make_candles, make_option_chain, make_premium_history, make_session, make_snapshot

        def exploding_observer(diag):
            raise RuntimeError("display bug")

        cfg = StrategyConfig()
        adapter = GrowwExecutionAdapter(config=cfg, dry_run=True, offline=True)
        with tempfile.TemporaryDirectory() as log_dir:
            orch = Orchestrator(
                adapter=adapter, config=cfg, index="NIFTY", expiry_date=date(2026, 7, 16),
                lot_size=65, log_dir=log_dir, mode="replay", on_diagnostics=exploding_observer,
            )
            snap = make_snapshot(
                candles=make_candles(120), premium_history=make_premium_history(10),
                option_chain=make_option_chain(), session=make_session(),
            )
            orch.on_snapshot(snap)  # must not raise despite the observer exploding
            orch._logger.close()


if __name__ == "__main__":
    unittest.main()
