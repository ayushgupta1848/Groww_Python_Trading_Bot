import unittest
from datetime import timedelta

from trading_decision_engine.app.engines.volatility_engine import MIN_CANDLES, VolatilityEngine
from trading_decision_engine.app.models.market_snapshot import Candle
from trading_decision_engine.tests.fixtures import BASE_TS, make_candles, make_premium_history, make_snapshot


class TestVolatilityEngine(unittest.TestCase):
    def test_insufficient_history_rejects(self):
        snapshot = make_snapshot(candles=make_candles(5))
        result = VolatilityEngine().analyze(snapshot)
        self.assertFalse(result.acceptable)

    def test_calm_market_is_acceptable(self):
        candles = make_candles(MIN_CANDLES + 10, step=0.5)
        history = make_premium_history(6, ce_step=0.0, pe_step=0.0)
        snapshot = make_snapshot(candles=candles, premium_history=history)
        result = VolatilityEngine().analyze(snapshot)
        self.assertTrue(result.acceptable)
        self.assertFalse(result.gap_detected)
        self.assertFalse(result.whipsaw_detected)

    def test_price_spike_detected(self):
        candles = list(make_candles(MIN_CANDLES + 10, step=0.5))
        last = candles[-1]
        candles[-1] = Candle(ts=last.ts, open=last.open, high=last.open + 500, low=last.open - 500, close=last.close, volume=1000)
        snapshot = make_snapshot(candles=tuple(candles))
        result = VolatilityEngine().analyze(snapshot)
        self.assertFalse(result.acceptable)

    def test_wide_spread_rejects(self):
        candles = make_candles(MIN_CANDLES + 10, step=0.5)
        history = make_premium_history(6, ce_step=0.0, pe_step=0.0)
        wide_tick = history[-1]
        history = history[:-1] + (
            wide_tick.__class__(ts=wide_tick.ts, ce_premium=wide_tick.ce_premium, pe_premium=wide_tick.pe_premium, bid=50.0, ask=150.0),
        )
        snapshot = make_snapshot(candles=candles, premium_history=history)
        result = VolatilityEngine().analyze(snapshot)
        self.assertFalse(result.acceptable)
        self.assertGreater(result.spread_pct, 2.0)


if __name__ == "__main__":
    unittest.main()
