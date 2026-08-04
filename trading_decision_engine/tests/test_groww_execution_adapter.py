import unittest
from unittest.mock import MagicMock, patch

import requests

from trading_decision_engine.app.broker.groww_execution_adapter import GrowwExecutionAdapter
from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.utils.error_handling import FatalBrokerError


def _http_error(status_code: int) -> requests.HTTPError:
    resp = MagicMock()
    resp.status_code = status_code
    return requests.HTTPError(response=resp)


class TestWaitForFill(unittest.TestCase):
    def test_dry_run_returns_executed_immediately(self):
        adapter = GrowwExecutionAdapter(config=StrategyConfig(), dry_run=True)
        self.assertEqual(adapter.wait_for_fill("PAPER_0001", "BUY"), "EXECUTED")

    def test_polls_until_terminal_success_status(self):
        adapter = GrowwExecutionAdapter(config=StrategyConfig(), dry_run=False)
        statuses = iter(["PENDING", "PENDING", "EXECUTED"])
        with patch.object(adapter, "get_order_status", side_effect=lambda oid: next(statuses)), \
             patch("time.sleep", return_value=None):
            result = adapter.wait_for_fill("REAL_ORDER_1", "BUY", timeout_seconds=5.0)
        self.assertEqual(result, "EXECUTED")

    def test_returns_immediately_on_terminal_failure_status(self):
        adapter = GrowwExecutionAdapter(config=StrategyConfig(), dry_run=False)
        with patch.object(adapter, "get_order_status", return_value="REJECTED"), \
             patch("time.sleep", return_value=None):
            result = adapter.wait_for_fill("REAL_ORDER_2", "BUY", timeout_seconds=5.0)
        self.assertEqual(result, "REJECTED")

    def test_times_out_if_never_terminal(self):
        adapter = GrowwExecutionAdapter(config=StrategyConfig(), dry_run=False)
        call_count = {"n": 0}
        fake_time = {"t": 0.0}

        def fake_monotonic():
            return fake_time["t"]

        def fake_sleep(seconds):
            fake_time["t"] += seconds

        with patch.object(adapter, "get_order_status", return_value="PENDING"), \
             patch("time.sleep", side_effect=fake_sleep), \
             patch("time.monotonic", side_effect=fake_monotonic):
            result = adapter.wait_for_fill("REAL_ORDER_3", "BUY", timeout_seconds=1.0)
        self.assertEqual(result, "TIMEOUT")

    def test_sell_uses_slower_poll_interval(self):
        adapter = GrowwExecutionAdapter(config=StrategyConfig(), dry_run=False)
        sleep_calls = []
        with patch.object(adapter, "get_order_status", side_effect=["PENDING", "EXECUTED"]), \
             patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            adapter.wait_for_fill("REAL_ORDER_4", "SELL", timeout_seconds=5.0)
        self.assertEqual(sleep_calls, [1.0])


class TestReauthOn401(unittest.TestCase):
    def test_401_triggers_one_relogin_then_succeeds(self):
        adapter = GrowwExecutionAdapter(config=StrategyConfig(), dry_run=False)
        adapter._access_token = "stale"
        call_count = {"n": 0}
        relogin_calls = {"n": 0}

        def fake_get(*args, **kwargs):
            call_count["n"] += 1
            resp = MagicMock()
            if call_count["n"] <= adapter._retry.max_attempts:
                resp.raise_for_status.side_effect = _http_error(401)
            else:
                resp.raise_for_status.side_effect = None
                resp.json.return_value = {"payload": {"ok": True}}
            return resp

        def fake_login():
            relogin_calls["n"] += 1
            adapter._access_token = "fresh"

        with patch.object(adapter._session, "get", side_effect=fake_get), \
             patch.object(adapter, "login", side_effect=fake_login), \
             patch("time.sleep", return_value=None):
            result = adapter._get("/v1/some/path")

        self.assertEqual(result, {"payload": {"ok": True}})
        self.assertEqual(relogin_calls["n"], 1)

    def test_401_persisting_after_relogin_raises_fatal(self):
        adapter = GrowwExecutionAdapter(config=StrategyConfig(), dry_run=False)
        adapter._access_token = "stale"

        def fake_get(*args, **kwargs):
            resp = MagicMock()
            resp.raise_for_status.side_effect = _http_error(401)
            return resp

        with patch.object(adapter._session, "get", side_effect=fake_get), \
             patch.object(adapter, "login", return_value=None), \
             patch("time.sleep", return_value=None):
            with self.assertRaises(FatalBrokerError):
                adapter._get("/v1/some/path")

    def test_non_401_http_error_propagates_without_relogin(self):
        adapter = GrowwExecutionAdapter(config=StrategyConfig(), dry_run=False)
        relogin_calls = {"n": 0}

        def fake_get(*args, **kwargs):
            resp = MagicMock()
            resp.raise_for_status.side_effect = _http_error(500)
            return resp

        def fake_login():
            relogin_calls["n"] += 1

        with patch.object(adapter._session, "get", side_effect=fake_get), \
             patch.object(adapter, "login", side_effect=fake_login), \
             patch("time.sleep", return_value=None):
            with self.assertRaises(requests.HTTPError):
                adapter._get("/v1/some/path")
        self.assertEqual(relogin_calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
