"""ManualTradeImporter: reads a CSV/JSONL file of manually-executed trades into
ManualTradeRecord tuples, for Replay Mode comparison against bot decisions. Architecture
support only — no import UI. See docs/DESIGN.md §11a.

Expected CSV columns: timestamp,instrument,action,price,lots
Expected JSONL: one {"timestamp":...,"instrument":...,"action":...,"price":...,"lots":...} per line
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from ..config.constants import TradeAction
from ..models.engine_results import ManualTradeRecord


def load_manual_trades(path: Path | str) -> tuple[ManualTradeRecord, ...]:
    file_path = Path(path)
    if file_path.suffix.lower() == ".jsonl":
        return _load_jsonl(file_path)
    return _load_csv(file_path)


def _load_csv(file_path: Path) -> tuple[ManualTradeRecord, ...]:
    records = []
    with open(file_path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            records.append(_to_record(row))
    return tuple(records)


def _load_jsonl(file_path: Path) -> tuple[ManualTradeRecord, ...]:
    records = []
    with open(file_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(_to_record(json.loads(line)))
    return tuple(records)


def _to_record(row: dict) -> ManualTradeRecord:
    return ManualTradeRecord(
        timestamp=datetime.fromisoformat(row["timestamp"]),
        instrument=row["instrument"],
        action=TradeAction(row["action"]),
        price=float(row["price"]),
        lots=int(row["lots"]),
    )
