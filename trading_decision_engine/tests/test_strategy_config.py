"""Tests for the configuration system: profile overlays, unknown-key tolerance,
live-reload plumbing, Stage-1 gate toggles, and the new Stage-2 quality thresholds.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from trading_decision_engine.app.config.constants import Direction, TradeAction
from trading_decision_engine.app.config.strategy import PROFILES_DIR, StrategyConfig, config_files_mtime
from trading_decision_engine.app.engines.decision_engine import DecisionEngine
from trading_decision_engine.tests.fixtures import make_decision_input


class TestStrategyConfigLoading(unittest.TestCase):
    def test_defaults_match_original_behaviour(self):
        cfg = StrategyConfig()
        # The gate set that existed before configurability: six on, four extras off.
        self.assertTrue(cfg.require_trend)
        self.assertTrue(cfg.require_signal_stability)
        self.assertTrue(cfg.require_trading_rules)
        self.assertTrue(cfg.require_risk)
        self.assertTrue(cfg.require_support_resistance)
        self.assertTrue(cfg.require_volatility)
        self.assertFalse(cfg.require_market_structure)
        self.assertFalse(cfg.require_breakout)
        self.assertFalse(cfg.require_market_strength)
        self.assertFalse(cfg.require_option_selection)
        # New Stage-2 thresholds all default to disabled.
        self.assertIsNone(cfg.min_buy_score)
        self.assertIsNone(cfg.min_sell_score)
        self.assertEqual(cfg.min_confidence, 0.0)
        self.assertEqual(cfg.min_trade_quality, 0.0)
        self.assertEqual(cfg.min_score_difference, 0.0)
        self.assertEqual(cfg.min_engine_agreement, 0)

    def test_shipped_strategy_json_loads_cleanly(self):
        cfg = StrategyConfig.load()
        self.assertEqual(cfg.decision_score_threshold, 85.0)
        self.assertEqual(cfg.active_profile, "")

    def test_all_shipped_profiles_load(self):
        for profile_path in sorted(PROFILES_DIR.glob("*.json")):
            cfg = StrategyConfig.load(profile=profile_path.stem)
            self.assertEqual(cfg.active_profile, profile_path.stem)

    def test_profile_overrides_base_file(self):
        aggressive = StrategyConfig.load(profile="aggressive")
        base = StrategyConfig.load()
        self.assertLess(aggressive.decision_score_threshold, base.decision_score_threshold)
        self.assertLess(aggressive.trend_threshold, base.trend_threshold)

    def test_unknown_profile_raises_with_available_list(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            StrategyConfig.load(profile="does_not_exist")
        self.assertIn("aggressive", str(ctx.exception))

    def test_unknown_keys_ignored_and_comment_keys_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy.json"
            path.write_text(json.dumps({"_comment": "doc", "definitely_a_typo": 1, "trend_threshold": 42.0}))
            cfg = StrategyConfig.load(path)
        self.assertEqual(cfg.trend_threshold, 42.0)

    def test_config_files_mtime_tracks_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy.json"
            path.write_text("{}")
            first = config_files_mtime(path)
            import os
            os.utime(path, (first + 10, first + 10))
            self.assertGreater(config_files_mtime(path), first)


class TestGateToggles(unittest.TestCase):
    def test_disabling_trend_gate_skips_trend_rejection(self):
        # A NEUTRAL trend rejects by default...
        inputs = make_decision_input(trend_direction=Direction.NEUTRAL)
        default_decision = DecisionEngine(StrategyConfig()).decide(inputs)
        self.assertEqual(default_decision.action, TradeAction.REJECT)
        self.assertIn("trend", default_decision.eligibility.failed_checks)
        # ...but with require_trend=false the same input reaches Stage 2.
        cfg = dataclasses.replace(StrategyConfig(), require_trend=False)
        decision = DecisionEngine(cfg).decide(inputs)
        self.assertNotIn("trend", decision.eligibility.failed_checks)

    def test_enabling_breakout_gate_vetoes_unconfirmed_breakout(self):
        inputs = make_decision_input(breakout_confirmed=False)  # bullish everything, no breakout
        base = DecisionEngine(StrategyConfig()).decide(inputs)
        self.assertTrue(base.eligibility.passed)  # extra gate off by default
        cfg = dataclasses.replace(StrategyConfig(), require_breakout=True)
        gated = DecisionEngine(cfg).decide(inputs)
        self.assertEqual(gated.action, TradeAction.REJECT)
        self.assertIn("breakout", gated.eligibility.failed_checks)

    def test_rejection_reasons_state_actual_vs_required(self):
        inputs = make_decision_input(trend_score=40.0)
        decision = DecisionEngine(StrategyConfig()).decide(inputs)  # threshold 60
        self.assertEqual(decision.action, TradeAction.REJECT)
        self.assertTrue(any("40" in r and "60" in r for r in decision.reasons), decision.reasons)


class TestStage2Thresholds(unittest.TestCase):
    def _buyable_inputs(self):
        return make_decision_input()  # fixture default: strongly bullish, passes stage 1

    def test_min_buy_score_overrides_shared_threshold(self):
        cfg = dataclasses.replace(StrategyConfig(), decision_score_threshold=999.0, min_buy_score=10.0)
        decision = DecisionEngine(cfg).decide(self._buyable_inputs())
        self.assertEqual(decision.action, TradeAction.BUY)

    def test_min_confidence_demotes_to_hold(self):
        base = DecisionEngine(dataclasses.replace(StrategyConfig(), decision_score_threshold=10.0)).decide(self._buyable_inputs())
        self.assertEqual(base.action, TradeAction.BUY)
        cfg = dataclasses.replace(StrategyConfig(), decision_score_threshold=10.0, min_confidence=99.9)
        decision = DecisionEngine(cfg).decide(self._buyable_inputs())
        self.assertEqual(decision.action, TradeAction.HOLD)
        self.assertTrue(any("Confidence" in r and "vs required" in r for r in decision.reasons), decision.reasons)

    def test_min_engine_agreement_demotes_to_hold(self):
        cfg = dataclasses.replace(StrategyConfig(), decision_score_threshold=10.0, min_engine_agreement=4)
        inputs = make_decision_input(strength_direction=Direction.NEUTRAL)  # only 3 of 4 agree
        decision = DecisionEngine(cfg).decide(inputs)
        self.assertEqual(decision.action, TradeAction.HOLD)
        self.assertTrue(any("agreement" in r.lower() for r in decision.reasons), decision.reasons)

    def test_min_trade_quality_demotes_to_hold(self):
        # Zero the quality bonuses so trade_quality == raw buy score (~86), clearly
        # below the 99.9 floor — otherwise bonuses cap quality at 100 and it passes.
        cfg = dataclasses.replace(
            StrategyConfig(), decision_score_threshold=10.0, min_trade_quality=99.9,
            quality_stability_bonus_cap=0.0, quality_liquidity_bonus_scale=0.0, quality_spread_bonus_scale=0.0,
        )
        decision = DecisionEngine(cfg).decide(self._buyable_inputs())
        self.assertEqual(decision.action, TradeAction.HOLD)
        self.assertTrue(any("Trade quality" in r for r in decision.reasons), decision.reasons)


class TestLiveReload(unittest.TestCase):
    def _make_orchestrator(self, config_path: Path):
        from datetime import date

        from trading_decision_engine.app.broker.groww_execution_adapter import GrowwExecutionAdapter
        from trading_decision_engine.app.orchestrator import Orchestrator

        self._tmp_logs = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_logs.cleanup)
        adapter = GrowwExecutionAdapter(config=StrategyConfig.load(config_path), dry_run=True, offline=True)
        return Orchestrator(
            adapter=adapter, config=StrategyConfig.load(config_path), index="NIFTY",
            expiry_date=date(2026, 7, 16), lot_size=75, log_dir=self._tmp_logs.name,
            mode="replay", config_path=str(config_path),
        )

    def test_reload_strategy_swaps_config_into_every_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy.json"
            path.write_text(json.dumps({"trend_threshold": 60.0}))
            orch = self._make_orchestrator(path)
            self.assertEqual(orch._decision_engine._config.trend_threshold, 60.0)

            path.write_text(json.dumps({"trend_threshold": 33.0, "cooldown_seconds": 55}))
            orch.reload_strategy()
            self.assertEqual(orch._config.trend_threshold, 33.0)
            self.assertEqual(orch._decision_engine._config.trend_threshold, 33.0)
            self.assertEqual(orch._trend_engine._config.trend_threshold, 33.0)
            self.assertEqual(orch._rules_engine._config.cooldown_seconds, 55)
            self.assertEqual(orch._trade_manager._config.cooldown_seconds, 55)

    def test_broken_json_edit_keeps_previous_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy.json"
            path.write_text(json.dumps({"trend_threshold": 60.0}))
            orch = self._make_orchestrator(path)
            path.write_text("{ this is not valid json")
            orch.reload_strategy()  # must not raise
            self.assertEqual(orch._config.trend_threshold, 60.0)


if __name__ == "__main__":
    unittest.main()
