import csv
import os
import tempfile
import unittest
from datetime import date

from trading_decision_engine.app.broker.instrument_master import InstrumentMaster

HEADER = [
    "exchange", "exchange_token", "trading_symbol", "groww_symbol", "name", "instrument_type",
    "segment", "series", "isin", "underlying_symbol", "underlying_exchange_token", "expiry_date",
    "strike_price", "lot_size", "tick_size", "freeze_quantity", "is_reserved", "buy_allowed",
    "sell_allowed", "internal_trading_symbol", "is_intraday",
]

ROWS = [
    # NIFTY index (CASH/IDX)
    ["NSE", "NIFTY", "NIFTY", "NSE-NIFTY", "NIFTY 50", "IDX", "CASH", "", "NIFTY", "", "", "", "", "", "", "", "0", "0", "", "0"],
    # a NIFTY option, already-expired relative to the as_of dates used below
    ["NSE", "42499", "NIFTY2670630CE", "NSE-NIFTY-30Jun26-17850-CE", "", "CE", "FNO", "", "", "NIFTY", "26000", "2026-06-30", "17850", "65", "0.05", "1801", "1", "1", "1", "NIFTY2670630CE", "0"],
    # two still-live NIFTY expiries
    ["NSE", "42501", "NIFTY2670707CE", "NSE-NIFTY-07Jul26-17900-CE", "", "CE", "FNO", "", "", "NIFTY", "26000", "2026-07-07", "17900", "65", "0.05", "1801", "1", "1", "1", "NIFTY2670707CE", "0"],
    ["NSE", "42502", "NIFTY2670714CE", "NSE-NIFTY-14Jul26-17900-CE", "", "CE", "FNO", "", "", "NIFTY", "26000", "2026-07-14", "17900", "65", "0.05", "1801", "1", "1", "1", "NIFTY2670714CE", "0"],
]


class TestInstrumentMaster(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerows(ROWS)
        self.master = InstrumentMaster(self.path)

    def tearDown(self):
        os.remove(self.path)

    def test_resolve_option_by_trading_symbol(self):
        ref = self.master.resolve("NIFTY2670707CE")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.exchange_token, "42501")
        self.assertEqual(ref.exchange, "NSE")
        self.assertEqual(ref.segment, "FNO")
        self.assertEqual(ref.lot_size, 65)

    def test_resolve_unknown_symbol_returns_none(self):
        self.assertIsNone(self.master.resolve("DOES_NOT_EXIST"))

    def test_index_instrument_resolves(self):
        ref = self.master.index_instrument("NIFTY")
        self.assertEqual(ref.segment, "CASH")
        self.assertEqual(ref.exchange_token, "NIFTY")

    def test_index_instrument_missing_raises(self):
        with self.assertRaises(KeyError):
            self.master.index_instrument("BANKNIFTY")

    def test_feed_dict_shape(self):
        ref = self.master.resolve("NIFTY2670707CE")
        self.assertEqual(ref.feed_dict(), {"exchange": "NSE", "segment": "FNO", "exchange_token": "42501"})

    def test_lot_size_for_exact_underlying_expiry(self):
        self.assertEqual(self.master.lot_size_for("NIFTY", "2026-07-07"), 65)

    def test_lot_size_for_unknown_returns_none(self):
        self.assertIsNone(self.master.lot_size_for("BANKNIFTY", "2026-07-07"))

    def test_expiries_for_excludes_already_expired(self):
        # as_of 2026-07-01: 2026-06-30 has already expired, 07-07 and 07-14 haven't.
        result = self.master.expiries_for("NIFTY", as_of=date(2026, 7, 1))
        self.assertEqual(result, ["2026-07-07", "2026-07-14"])

    def test_expiries_for_includes_expiry_on_as_of_date_itself(self):
        result = self.master.expiries_for("NIFTY", as_of=date(2026, 7, 7))
        self.assertIn("2026-07-07", result)

    def test_expiries_for_sorted_nearest_first(self):
        result = self.master.expiries_for("NIFTY", as_of=date(2026, 7, 1))
        self.assertEqual(result, sorted(result))

    def test_expiries_for_unknown_underlying_is_empty(self):
        self.assertEqual(self.master.expiries_for("BANKNIFTY", as_of=date(2026, 7, 1)), [])


if __name__ == "__main__":
    unittest.main()
