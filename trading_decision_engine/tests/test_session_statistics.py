"""Tests for SessionStatistics: cumulative engine-health analytics aggregated from the
diagnostics stream. Pure observer — verified against synthetic diagnostics events and
against real builder output.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from trading_decision_engine.app.config.constants import Direction
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.engines.decision_engine import DecisionEngine
from trading_decision_engine.app.utils.decision_diagnostics import build_cycle_diagnostics
from trading_decision_engine.app.utils.session_statistics import SessionStatistics, render_session_stats, render_stats_strip
from trading_decision_engine.tests.fixtures import make_decision_input

TS = datetime(2026, 7, 13, 10, 0, 0)


def _cycle_diag(cfg: StrategyConfig, **input_overrides) -> dict:
    inputs = make_decision_input(**input_overrides)
    decision = DecisionEngine(cfg).decide(inputs)
    results = {k: getattr(inputs, k) for k in (
        "trend", "market_structure", "support_resistance", "premium_momentum",
        "option_selection", "breakout", "market_strength", "volatility", "trading_rules", "risk",
    )}
    return build_cycle_diagnostics(TS, results, inputs.signal_stability, decision, cfg)


class TestSessionStatistics(unittest.TestCase):
    def setUp(self):
        self.buy_cfg = dataclasses.replace(StrategyConfig(), decision_score_threshold=10.0)
        self.stats = SessionStatistics()

    def test_counts_actions_and_cycles(self):
        self.stats.update(_cycle_diag(self.buy_cfg))                                     # BUY
        self.stats.update(_cycle_diag(self.buy_cfg, trend_direction=Direction.NEUTRAL))  # REJECT
        self.stats.update(_cycle_diag(self.buy_cfg, trend_direction=Direction.NEUTRAL))  # REJECT
        snap = self.stats.to_dict()
        self.assertEqual(snap["decision_cycles"], 3)
        self.assertEqual(snap["actions"]["BUY"], 1)
        self.assertEqual(snap["actions"]["REJECT"], 2)

    def test_rejection_reason_percentages(self):
        for _ in range(3):
            self.stats.update(_cycle_diag(self.buy_cfg, trend_direction=Direction.NEUTRAL))
        snap = self.stats.to_dict()
        # NEUTRAL trend also fails signal stability (its own history sees no direction);
        # trend must be among the top reasons and percentages must total 100.
        self.assertIn("trend", snap["rejection_reasons_pct"])
        self.assertAlmostEqual(sum(snap["rejection_reasons_pct"].values()), 100.0, delta=0.5)

    def test_per_engine_pass_rate_and_averages(self):
        self.stats.update(_cycle_diag(self.buy_cfg))                                     # trend passes, score 80
        self.stats.update(_cycle_diag(self.buy_cfg, trend_score=40.0, trend_direction=Direction.NEUTRAL))  # trend fails, score 40
        trend = self.stats.to_dict()["engines"]["trend"]
        self.assertEqual(trend["samples"], 2)
        self.assertEqual(trend["pass_pct"], 50.0)
        self.assertEqual(trend["fail_pct"], 50.0)
        self.assertEqual(trend["avg_score"], 60.0)  # (80 + 40) / 2

    def test_velocity_extremes_tracked(self):
        self.stats.update(_cycle_diag(self.buy_cfg))  # fixture velocity = 5
        snap = self.stats.to_dict()["premium_momentum"]
        self.assertEqual(snap["avg_velocity"], 5.0)
        self.assertEqual(snap["max_velocity"], 5.0)
        self.assertEqual(snap["min_velocity"], 5.0)

    def test_win_rate_by_entry_score_buckets(self):
        self.stats.update({"type": "trade_closed", "pnl": 500.0, "entry_scores": {"trend": 85.0}})
        self.stats.update({"type": "trade_closed", "pnl": 200.0, "entry_scores": {"trend": 90.0}})
        self.stats.update({"type": "trade_closed", "pnl": -300.0, "entry_scores": {"trend": 45.0}})
        trades = self.stats.to_dict()["trades"]
        self.assertEqual(trades["closed"], 3)
        self.assertEqual(trades["wins"], 2)
        self.assertEqual(trades["win_rate_pct"], 66.7)
        self.assertEqual(trades["total_pnl"], 400.0)
        by_trend = trades["win_rate_by_entry_score"]["trend"]
        self.assertEqual(by_trend[">80"], {"trades": 2, "win_rate_pct": 100.0})
        self.assertEqual(by_trend["<60"], {"trades": 1, "win_rate_pct": 0.0})

    def test_monitoring_ticks_counted_separately(self):
        self.stats.update({"type": "trade_panel", "timestamp": TS.isoformat()})
        self.stats.update({"type": "trade_panel", "timestamp": TS.isoformat()})
        snap = self.stats.to_dict()
        self.assertEqual(snap["monitoring_ticks"], 2)
        self.assertEqual(snap["decision_cycles"], 0)

    def test_render_full_panel_and_strip(self):
        self.stats.update(_cycle_diag(self.buy_cfg))
        self.stats.update(_cycle_diag(self.buy_cfg, trend_direction=Direction.NEUTRAL))
        self.stats.update({"type": "trade_closed", "pnl": 500.0, "entry_scores": {"trend": 85.0}})
        snap = self.stats.to_dict()
        panel = render_session_stats(snap)
        for token in ("SESSION STATISTICS", "TOP REJECTION REASONS", "ENGINE", "PASS%",
                      "premium velocity", "TRADES", "WIN RATE BY ENTRY SCORE"):
            self.assertIn(token, panel)
        strip = render_stats_strip(snap)
        self.assertIn("BUY 1", strip)
        self.assertIn("top block:", strip)

    def test_snapshot_json_serializable_and_saveable(self):
        self.stats.update(_cycle_diag(self.buy_cfg))
        json.dumps(self.stats.to_dict())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "stats.json"
            self.stats.save(out)
            self.assertEqual(json.loads(out.read_text())["type"], "session_stats")

    def test_empty_session_renders_without_crashing(self):
        render_session_stats(self.stats.to_dict())
        render_stats_strip(self.stats.to_dict())


if __name__ == "__main__":
    unittest.main()
