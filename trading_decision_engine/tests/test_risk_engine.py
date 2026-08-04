import unittest

from trading_decision_engine.app.engines.risk_engine import RiskEngine
from trading_decision_engine.tests.fixtures import make_session


class TestRiskEngine(unittest.TestCase):
    def test_all_clear_is_safe(self):
        result = RiskEngine().analyze(make_session())
        self.assertTrue(result.safe_to_trade)

    def test_already_in_trade_is_unsafe(self):
        result = RiskEngine().analyze(make_session(already_in_trade=True))
        self.assertFalse(result.safe_to_trade)
        self.assertTrue(result.already_in_trade)

    def test_order_pending_is_unsafe(self):
        result = RiskEngine().analyze(make_session(order_pending=True))
        self.assertFalse(result.safe_to_trade)

    def test_broker_disconnected_is_unsafe(self):
        result = RiskEngine().analyze(make_session(broker_connected=False))
        self.assertFalse(result.safe_to_trade)
        self.assertFalse(result.broker_connected)

    def test_no_margin_is_unsafe(self):
        result = RiskEngine().analyze(make_session(margin_available=0.0))
        self.assertFalse(result.safe_to_trade)


if __name__ == "__main__":
    unittest.main()
