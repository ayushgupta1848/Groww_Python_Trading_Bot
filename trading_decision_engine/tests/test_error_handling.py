import unittest

from trading_decision_engine.app.config.constants import Direction
from trading_decision_engine.app.models.engine_results import TrendResult
from trading_decision_engine.app.utils.error_handling import EngineFailureTracker, neutral_engine_result, safe_analyze


class TestErrorHandling(unittest.TestCase):
    def test_safe_analyze_catches_exception_and_returns_neutral(self):
        def _raises(snapshot):
            raise ValueError("boom")

        result, escalate = safe_analyze(
            "trend", _raises,
            lambda reason: neutral_engine_result(TrendResult, reason, ehma_value=0.0, ema100_value=0.0, trend_angle=0.0, trend_strength=0.0),
            None, "some snapshot",
        )
        self.assertEqual(result.direction, Direction.NEUTRAL)
        self.assertIn("boom", result.reasons[0])
        self.assertFalse(escalate)

    def test_safe_analyze_passes_through_successful_result(self):
        def _ok(snapshot):
            return TrendResult(direction=Direction.BULLISH, score=80, confidence=80, reasons=("ok",), ehma_value=1, ema100_value=1, trend_angle=1, trend_strength=80)

        result, escalate = safe_analyze(
            "trend", _ok,
            lambda reason: neutral_engine_result(TrendResult, reason, ehma_value=0.0, ema100_value=0.0, trend_angle=0.0, trend_strength=0.0),
            None, "snapshot",
        )
        self.assertEqual(result.direction, Direction.BULLISH)
        self.assertFalse(escalate)

    def test_failure_tracker_escalates_after_threshold(self):
        tracker = EngineFailureTracker(escalation_threshold=3)
        self.assertFalse(tracker.record_failure("trend"))
        self.assertFalse(tracker.record_failure("trend"))
        self.assertTrue(tracker.record_failure("trend"))

    def test_failure_tracker_resets_on_success(self):
        tracker = EngineFailureTracker(escalation_threshold=2)
        tracker.record_failure("trend")
        tracker.record_success("trend")
        self.assertFalse(tracker.record_failure("trend"))

    def test_safe_analyze_escalates_via_tracker(self):
        tracker = EngineFailureTracker(escalation_threshold=1)

        def _raises(snapshot):
            raise RuntimeError("fail")

        _, escalate = safe_analyze(
            "trend", _raises,
            lambda reason: neutral_engine_result(TrendResult, reason, ehma_value=0.0, ema100_value=0.0, trend_angle=0.0, trend_strength=0.0),
            tracker, "snapshot",
        )
        self.assertTrue(escalate)


if __name__ == "__main__":
    unittest.main()
