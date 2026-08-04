import unittest

from trading_decision_engine.app.config.strategy import StrategyConfig
from trading_decision_engine.app.engines.option_selection_engine import OptionSelectionEngine
from trading_decision_engine.app.models.market_snapshot import OptionChainView
from trading_decision_engine.tests.fixtures import make_option_leg, make_snapshot


class TestOptionSelectionEngine(unittest.TestCase):
    def test_no_legs_in_range_returns_none(self):
        config = StrategyConfig()
        chain = OptionChainView(underlying_ltp=24000, strikes={24000.0: {"CE": make_option_leg(ltp=5.0), "PE": make_option_leg(ltp=5.0)}})
        snapshot = make_snapshot(option_chain=chain)
        result = OptionSelectionEngine(config).analyze(snapshot)
        self.assertIsNone(result.best_ce_symbol)
        self.assertIsNone(result.best_pe_symbol)

    def test_picks_highest_liquidity_within_premium_range(self):
        config = StrategyConfig()
        chain = OptionChainView(
            underlying_ltp=24000,
            strikes={
                23950.0: {"CE": make_option_leg("LOW_LIQ_CE", ltp=100.0, oi=1000, volume=100), "PE": None},
                24000.0: {"CE": make_option_leg("HIGH_LIQ_CE", ltp=110.0, oi=200_000, volume=50_000), "PE": None},
            },
        )
        snapshot = make_snapshot(option_chain=chain)
        result = OptionSelectionEngine(config).analyze(snapshot)
        self.assertEqual(result.best_ce_symbol, "HIGH_LIQ_CE")

    def test_wide_spread_lowers_spread_score(self):
        config = StrategyConfig()
        tight = make_option_leg("TIGHT_CE", ltp=100.0, bid=99.9, ask=100.1)
        wide = make_option_leg("WIDE_CE", ltp=100.0, bid=90.0, ask=110.0)
        chain = OptionChainView(underlying_ltp=24000, strikes={24000.0: {"CE": tight, "PE": None}, 24050.0: {"CE": wide, "PE": None}})
        snapshot = make_snapshot(option_chain=chain)
        result = OptionSelectionEngine(config).analyze(snapshot)
        self.assertEqual(result.best_ce_symbol, "TIGHT_CE")


if __name__ == "__main__":
    unittest.main()
