"""Serialize/deserialize a ReplayTick sequence to/from a JSON file, so a replay run can
be driven entirely offline via `run.py --replay-file F` with no live broker connection —
`HistoricalReplayBuilder` (network-backed) is one way to produce such a file; this
module is the other half, matching docs/DESIGN.md §15's `--replay-file` verification
command.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..models.market_snapshot import Candle, OptionChainView, OptionLeg, PremiumTick
from .replay_source import ReplayTick


def save_replay_ticks(ticks: list[ReplayTick], path: Path | str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for tick in ticks:
            fh.write(json.dumps({"ts": tick.ts.isoformat(), "kind": tick.kind, "payload": _serialize_payload(tick)}) + "\n")


def load_replay_ticks(path: Path | str) -> list[ReplayTick]:
    ticks = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ticks.append(ReplayTick(ts=datetime.fromisoformat(row["ts"]), kind=row["kind"], payload=_deserialize_payload(row["kind"], row["payload"])))
    return ticks


def _serialize_payload(tick: ReplayTick):
    if tick.kind == "spot":
        return tick.payload
    if tick.kind == "candle":
        c: Candle = tick.payload
        return {"ts": c.ts.isoformat(), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
    if tick.kind == "premium":
        p: PremiumTick = tick.payload
        return {"ts": p.ts.isoformat(), "ce_premium": p.ce_premium, "pe_premium": p.pe_premium, "bid": p.bid, "ask": p.ask}
    if tick.kind == "option_chain":
        chain: OptionChainView = tick.payload
        return {
            "underlying_ltp": chain.underlying_ltp,
            "strikes": {
                str(strike): {
                    side: (None if leg is None else {"trading_symbol": leg.trading_symbol, "ltp": leg.ltp, "open_interest": leg.open_interest, "volume": leg.volume, "bid": leg.bid, "ask": leg.ask, "iv": leg.iv, "delta": leg.delta})
                    for side, leg in legs.items()
                }
                for strike, legs in chain.strikes.items()
            },
        }
    raise ValueError(f"Unknown replay tick kind: {tick.kind}")


def _deserialize_payload(kind: str, payload):
    if kind == "spot":
        return payload
    if kind == "candle":
        return Candle(ts=datetime.fromisoformat(payload["ts"]), open=payload["open"], high=payload["high"], low=payload["low"], close=payload["close"], volume=payload["volume"])
    if kind == "premium":
        return PremiumTick(ts=datetime.fromisoformat(payload["ts"]), ce_premium=payload["ce_premium"], pe_premium=payload["pe_premium"], bid=payload["bid"], ask=payload["ask"])
    if kind == "option_chain":
        strikes = {
            float(strike): {
                side: (None if leg is None else OptionLeg(**leg))
                for side, leg in legs.items()
            }
            for strike, legs in payload["strikes"].items()
        }
        return OptionChainView(underlying_ltp=payload["underlying_ltp"], strikes=strikes)
    raise ValueError(f"Unknown replay tick kind: {kind}")
