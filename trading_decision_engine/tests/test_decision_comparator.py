import unittest
from datetime import datetime, timedelta

from trading_decision_engine.app.config.constants import Direction, TradeAction
from trading_decision_engine.app.market_data.decision_comparator import DecisionComparator
from trading_decision_engine.app.models.engine_results import DecisionResult, EligibilityResult, ManualTradeRecord

NOW = datetime(2026, 7, 13, 10, 0, 0)
PASS = EligibilityResult(passed=True, reasons=("ok",), failed_checks=())


def _decision(action=TradeAction.BUY):
    return DecisionResult(direction=Direction.BULLISH, score=90, confidence=90, reasons=(), action=action, buy_score=90, sell_score=0, exit_score=0, eligibility=PASS, trade_quality_score=90)


class TestDecisionComparator(unittest.TestCase):
    def test_matches_within_tolerance(self):
        bot_decisions = [(NOW, "NIFTYCE", _decision())]
        manual_trades = [ManualTradeRecord(timestamp=NOW + timedelta(seconds=30), instrument="NIFTYCE", action=TradeAction.BUY, price=100.0, lots=1)]
        report = DecisionComparator.compare(bot_decisions, manual_trades, tolerance_seconds=60.0)
        self.assertEqual(len(report.matched), 1)
        self.assertEqual(report.agreement_pct, 100.0)
        self.assertEqual(len(report.bot_only), 0)
        self.assertEqual(len(report.manual_only), 0)

    def test_outside_tolerance_is_unmatched(self):
        bot_decisions = [(NOW, "NIFTYCE", _decision())]
        manual_trades = [ManualTradeRecord(timestamp=NOW + timedelta(seconds=300), instrument="NIFTYCE", action=TradeAction.BUY, price=100.0, lots=1)]
        report = DecisionComparator.compare(bot_decisions, manual_trades, tolerance_seconds=60.0)
        self.assertEqual(len(report.matched), 0)
        self.assertEqual(len(report.bot_only), 1)
        self.assertEqual(len(report.manual_only), 1)
        self.assertEqual(report.agreement_pct, 0.0)

    def test_different_instrument_is_unmatched(self):
        bot_decisions = [(NOW, "NIFTYCE", _decision())]
        manual_trades = [ManualTradeRecord(timestamp=NOW, instrument="NIFTYPE", action=TradeAction.SELL, price=90.0, lots=1)]
        report = DecisionComparator.compare(bot_decisions, manual_trades, tolerance_seconds=60.0)
        self.assertEqual(len(report.matched), 0)

    def test_hold_and_reject_decisions_excluded_from_bot_trades(self):
        bot_decisions = [(NOW, "NIFTYCE", _decision(action=TradeAction.HOLD)), (NOW, "NIFTYCE", _decision(action=TradeAction.REJECT))]
        manual_trades = [ManualTradeRecord(timestamp=NOW, instrument="NIFTYCE", action=TradeAction.BUY, price=100.0, lots=1)]
        report = DecisionComparator.compare(bot_decisions, manual_trades, tolerance_seconds=60.0)
        self.assertEqual(len(report.matched), 0)
        self.assertEqual(len(report.manual_only), 1)


if __name__ == "__main__":
    unittest.main()
