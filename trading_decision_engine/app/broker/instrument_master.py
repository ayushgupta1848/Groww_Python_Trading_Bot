"""InstrumentMaster: resolves trading symbols to the {exchange, segment, exchange_token}
dicts GrowwFeed subscriptions require, loaded from the repo-root instrument.csv (the
same instrument master the existing PROD10FEB/QA_PASS bots already download and use).
`refresh_instrument_csv()` keeps that file current automatically (same public-assets
download + 1-day staleness policy as COMMAND_GENERATOR_option_chain.py), so no manual
download is ever needed — run.py calls it on every start. See docs/DESIGN.md §12
(broker adapter is the only module allowed to hold broker/instrument metadata this
module feeds into).
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger("trading_decision_engine.broker")

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INSTRUMENT_CSV = REPO_ROOT / "instrument.csv"

# Groww's public instrument master — no authentication required.
INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
INSTRUMENT_MAX_AGE = timedelta(days=1)


def refresh_instrument_csv(csv_path: Path | str | None = None, max_age: timedelta = INSTRUMENT_MAX_AGE) -> bool:
    """Download the latest instrument.csv from Groww if the local copy is missing or
    older than `max_age` (contracts change daily — new expiries appear, old ones drop).
    A failed download is non-fatal when a previous file exists: the stale copy is kept
    with a warning rather than blocking startup (offline replay must still work with no
    network at all). Returns True if a usable file exists afterwards.
    """
    path = Path(csv_path) if csv_path else DEFAULT_INSTRUMENT_CSV

    stale = True
    if path.exists():
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        stale = age > max_age
        if not stale:
            logger.info("instrument.csv is fresh (%.1fh old) — no download needed", age.total_seconds() / 3600)
            return True

    try:
        logger.info("Downloading latest instrument.csv from Groww (%s)...", INSTRUMENT_CSV_URL)
        resp = requests.get(INSTRUMENT_CSV_URL, timeout=30)
        resp.raise_for_status()
        # Write atomically: never leave a half-written file if the process dies mid-write.
        tmp_path = path.with_suffix(".csv.tmp")
        tmp_path.write_bytes(resp.content)
        tmp_path.replace(path)
        logger.info("instrument.csv updated (%.1f MB)", len(resp.content) / 1e6)
        return True
    except Exception as exc:  # noqa: BLE001 - a stale file beats no startup
        if path.exists():
            logger.warning("instrument.csv download failed (%s) — continuing with the existing (stale) file", exc)
            return True
        logger.error("instrument.csv download failed and no local copy exists: %s", exc)
        return False


@dataclass(frozen=True)
class InstrumentRef:
    exchange: str
    segment: str
    exchange_token: str
    trading_symbol: str
    internal_trading_symbol: str
    lot_size: int

    def feed_dict(self) -> dict:
        """The {exchange, segment, exchange_token} shape GrowwFeed subscriptions need."""
        return {"exchange": self.exchange, "segment": self.segment, "exchange_token": self.exchange_token}


class InstrumentMaster:
    def __init__(self, csv_path: Path | str | None = None) -> None:
        self._by_symbol: dict[str, InstrumentRef] = {}
        self._index_by_name: dict[str, InstrumentRef] = {}
        self._lot_size_by_underlying_expiry: dict[tuple[str, str], int] = {}
        self._load(Path(csv_path) if csv_path else DEFAULT_INSTRUMENT_CSV)

    def _load(self, csv_path: Path) -> None:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                lot_size_raw = row.get("lot_size") or "0"
                lot_size = int(float(lot_size_raw)) if lot_size_raw else 0
                ref = InstrumentRef(
                    exchange=row["exchange"],
                    segment=row["segment"],
                    exchange_token=row["exchange_token"],
                    trading_symbol=row["trading_symbol"],
                    internal_trading_symbol=row.get("internal_trading_symbol") or row["trading_symbol"],
                    lot_size=lot_size,
                )
                self._by_symbol[ref.trading_symbol] = ref
                if ref.segment == "CASH" and row.get("instrument_type") == "IDX":
                    self._index_by_name[ref.trading_symbol] = ref
                underlying = row.get("underlying_symbol")
                expiry = row.get("expiry_date")
                if underlying and expiry and lot_size > 0:
                    self._lot_size_by_underlying_expiry[(underlying, expiry)] = lot_size

    def resolve(self, trading_symbol: str) -> InstrumentRef | None:
        return self._by_symbol.get(trading_symbol)

    def index_instrument(self, index_name: str) -> InstrumentRef:
        ref = self._index_by_name.get(index_name)
        if ref is None:
            raise KeyError(f"No CASH/IDX instrument found for index {index_name!r}")
        return ref

    def lot_size_for(self, underlying: str, expiry_date: str) -> int | None:
        """Exact (underlying_symbol, expiry_date) lookup — used by run.py to
        auto-derive --lot-size when not supplied explicitly.
        """
        return self._lot_size_by_underlying_expiry.get((underlying, expiry_date))

    def expiries_for(self, underlying: str, as_of: date | None = None) -> list[str]:
        """Expiry dates this instrument.csv has for `underlying` that are still live
        (>= as_of, default today) — sorted, nearest first. Already-expired contracts
        are excluded, matching QA_PASS's get_available_expiries filtering, since a
        stale instrument.csv can otherwise offer an expiry that's already passed.
        """
        cutoff = as_of or datetime.now().date()
        all_expiries = {expiry for (u, expiry) in self._lot_size_by_underlying_expiry if u == underlying}
        live = [e for e in all_expiries if datetime.strptime(e, "%Y-%m-%d").date() >= cutoff]
        return sorted(live)
