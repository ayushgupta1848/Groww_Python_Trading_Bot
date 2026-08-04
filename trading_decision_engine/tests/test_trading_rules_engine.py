import unittest
from datetime import date, datetime, timedelta

from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.engines.trading_rules_engine import TradingRulesEngine
from trading_decision_engine.tests.fixtures import make_session

MID_SESSION = datetime(2026, 7, 13, 11, 0, 0)


class TestTradingRulesEngine(unittest.TestCase):
    def test_all_clear_allows_trading(self):
        session = make_session()
        result = TradingRulesEngine(StrategyConfig()).analyze(session, MID_SESSION)
        self.assertTrue(result.allowed)

    def test_max_trades_per_day_blocks(self):
        config = StrategyConfig()
        session = make_session(trades_today=config.max_trades_per_day)
        result = TradingRulesEngine(config).analyze(session, MID_SESSION)
        self.assertFalse(result.allowed)

    def test_active_cooldown_blocks(self):
        config = StrategyConfig()
        session = make_session(cooldown_until=MID_SESSION + timedelta(seconds=30))
        result = TradingRulesEngine(config).analyze(session, MID_SESSION)
        self.assertFalse(result.allowed)

    def test_consecutive_loss_limit_blocks(self):
        config = StrategyConfig()
        session = make_session(consecutive_losses=config.consecutive_loss_limit)
        result = TradingRulesEngine(config).analyze(session, MID_SESSION)
        self.assertFalse(result.allowed)

    def test_daily_loss_limit_blocks(self):
        config = StrategyConfig()
        session = make_session(daily_pnl=-config.daily_loss_limit)
        result = TradingRulesEngine(config).analyze(session, MID_SESSION)
        self.assertFalse(result.allowed)

    def test_daily_profit_lock_blocks(self):
        config = StrategyConfig()
        session = make_session(daily_pnl=config.daily_profit_lock)
        result = TradingRulesEngine(config).analyze(session, MID_SESSION)
        self.assertFalse(result.allowed)

    def test_near_market_close_blocks(self):
        config = StrategyConfig()
        session = make_session()
        near_close = datetime(2026, 7, 13, 15, 20, 0)
        result = TradingRulesEngine(config).analyze(session, near_close)
        self.assertFalse(result.allowed)
        self.assertTrue(result.near_market_close)

    def test_expiry_day_cutoff_blocks(self):
        config = StrategyConfig()
        session = make_session()
        expiry_afternoon = datetime(2026, 7, 13, 14, 30, 0)
        result = TradingRulesEngine(config).analyze(session, expiry_afternoon, expiry_date=date(2026, 7, 13))
        self.assertFalse(result.allowed)
        self.assertTrue(result.is_expiry_day)


if __name__ == "__main__":
    unittest.main()
