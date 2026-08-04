"""Build a replay tick JSONL from user-supplied 1-minute candle JSON files (index spot +
one real option leg), for fully-offline backtesting via `run.py --mode replay
--replay-file F`. Complements HistoricalReplayBuilder, which needs a live broker
connection — this builder needs only two local files in Groww's candle-JSON shape:

    {"candles": [[epoch_seconds, open, high, low, close, volume], ...]}

The index file drives spot + the candle window every analysis engine reads; the option
file drives the real premium stream for that leg. The opposite leg has no data, so it is
synthesized from spot moves at a nominal delta (same documented simplification, and same
constants, as HistoricalReplayBuilder) — P&L on trades in the synthetic leg's direction
is therefore indicative only, while trades in the real leg's direction settle against
actual traded premiums.

Spot and premium sub-ticks are interpolated at PREMIUM_SUBTICK_SECONDS granularity
WITHIN each candle — SnapshotBuilder only retains PREMIUM_HISTORY_SECONDS (5s) of
premium ticks and PremiumMomentumEngine needs premium_momentum_min_samples (6) of them,
so sub-ticks must arrive at least every ~0.8s for momentum to ever resolve. Candles are
never interpolated ACROSS days (overnight gaps stay gaps).

Usage:
    python -m trading_decision_engine.app.market_data.candle_file_replay_builder \
        --index-file <spot.json> --option-file <option.json> --option-type CE \
        --out <replay_ticks.jsonl>
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config.constants import STRIKE_STEP, Index
from ..models.market_snapshot import Candle, OptionChainView, OptionLeg, PremiumTick
from .replay_source import ReplayTick
from .replay_tick_io import save_replay_ticks

PREMIUM_SUBTICK_SECONDS = 1.0
NOMINAL_DELTA = 0.5          # synthetic-leg premium change per point of spot move
NOMINAL_BASE_PREMIUM = 140.0  # synthetic leg's premium at each day's first spot print
NOMINAL_SPREAD = 0.5          # half-spread applied around every premium print
CHAIN_STRIKE_WIDTH = 10       # strikes on each side of ATM in the synthetic chain
CHAIN_PREMIUM_STEP = 8.0      # per-strike premium decay away from ATM
CHAIN_OI = 100_000            # comfortably above liquidity_min_oi
CHAIN_VOLUME = 20_000         # comfortably above liquidity_min_volume


def load_candles(path: Path | str) -> list[Candle]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data["candles"] if isinstance(data, dict) else data
    candles = []
    for row in rows:
        ts_raw, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        volume = int(row[5]) if len(row) > 5 and row[5] is not None else 0
        ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else datetime.fromtimestamp(ts_raw)
        candles.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=volume))
    return sorted(candles, key=lambda cd: cd.ts)


def _atm_strike(spot: float, index: Index) -> float:
    step = STRIKE_STEP[index]
    return round(spot / step) * step


def _make_leg(symbol: str, ltp: float) -> OptionLeg:
    ltp = max(0.05, ltp)
    return OptionLeg(
        trading_symbol=symbol, ltp=ltp, open_interest=CHAIN_OI, volume=CHAIN_VOLUME,
        bid=max(0.05, ltp - NOMINAL_SPREAD), ask=ltp + NOMINAL_SPREAD, iv=12.0, delta=0.5,
    )


def _build_chain(index: Index, spot: float, real_premium: float, synthetic_premium: float, option_type: str) -> OptionChainView:
    """Synthetic chain centered on ATM: the real leg's ATM price comes from the actual
    candle data (so OptionSelection picks realistically-priced strikes and paper fills
    settle near real prices); everything else decays linearly away from ATM.
    """
    step = STRIKE_STEP[index]
    atm = _atm_strike(spot, index)
    ce_atm = real_premium if option_type == "CE" else synthetic_premium
    pe_atm = real_premium if option_type == "PE" else synthetic_premium
    strikes: dict[float, dict[str, OptionLeg | None]] = {}
    for k in range(-CHAIN_STRIKE_WIDTH, CHAIN_STRIKE_WIDTH + 1):
        strike = atm + k * step
        # CE premium falls as strike rises; PE premium falls as strike falls.
        strikes[strike] = {
            "CE": _make_leg(f"{index.value}{int(strike)}CE", ce_atm - k * CHAIN_PREMIUM_STEP),
            "PE": _make_leg(f"{index.value}{int(strike)}PE", pe_atm + k * CHAIN_PREMIUM_STEP),
        }
    return OptionChainView(underlying_ltp=spot, strikes=strikes)


def build_ticks(
    index_candles: list[Candle], option_candles: list[Candle], index: Index, option_type: str
) -> list[ReplayTick]:
    option_by_ts = {c.ts: c for c in option_candles}
    ticks: list[ReplayTick] = []

    by_day: dict[date, list[Candle]] = {}
    for candle in index_candles:
        by_day.setdefault(candle.ts.date(), []).append(candle)

    for day_candles in by_day.values():
        day_spot0 = day_candles[0].open
        synthetic_premium = NOMINAL_BASE_PREMIUM

        for prev, cur in zip([None] + day_candles[:-1], day_candles):
            ticks.append(ReplayTick(ts=cur.ts, kind="candle", payload=cur))

            option_candle = option_by_ts.get(cur.ts)
            if prev is None or option_candle is None:
                # First candle of the day (nothing to interpolate from), or a minute the
                # option file is missing: emit one spot/premium print at the candle close.
                spot = cur.close
                real_prem = option_candle.close if option_candle is not None else None
                synthetic_premium = max(0.05, NOMINAL_BASE_PREMIUM - (spot - day_spot0) * NOMINAL_DELTA * (1 if option_type == "CE" else -1))
                ticks.append(ReplayTick(ts=cur.ts, kind="spot", payload=spot))
                if real_prem is not None:
                    ce, pe = (real_prem, synthetic_premium) if option_type == "CE" else (synthetic_premium, real_prem)
                    ticks.append(ReplayTick(ts=cur.ts, kind="premium", payload=PremiumTick(
                        ts=cur.ts, ce_premium=ce, pe_premium=pe,
                        bid=max(0.05, ce - NOMINAL_SPREAD), ask=ce + NOMINAL_SPREAD,
                    )))
                    ticks.append(ReplayTick(ts=cur.ts, kind="option_chain", payload=_build_chain(index, spot, real_prem, synthetic_premium, option_type)))
                continue

            prev_option = option_by_ts.get(prev.ts, option_candle)
            span = max(1.0, (cur.ts - prev.ts).total_seconds())
            subticks = max(1, int(span // PREMIUM_SUBTICK_SECONDS))
            for i in range(1, subticks + 1):
                sub_ts = prev.ts + timedelta(seconds=i * PREMIUM_SUBTICK_SECONDS)
                progress = i / subticks
                spot = prev.close + (cur.close - prev.close) * progress
                real_prem = max(0.05, prev_option.close + (option_candle.close - prev_option.close) * progress)
                synthetic_premium = max(0.05, NOMINAL_BASE_PREMIUM - (spot - day_spot0) * NOMINAL_DELTA * (1 if option_type == "CE" else -1))
                ce, pe = (real_prem, synthetic_premium) if option_type == "CE" else (synthetic_premium, real_prem)

                ticks.append(ReplayTick(ts=sub_ts, kind="spot", payload=spot))
                ticks.append(ReplayTick(ts=sub_ts, kind="premium", payload=PremiumTick(
                    ts=sub_ts, ce_premium=ce, pe_premium=pe,
                    bid=max(0.05, ce - NOMINAL_SPREAD), ask=ce + NOMINAL_SPREAD,
                )))

            # One chain refresh per minute keeps OptionSelection's view current without
            # bloating the tick file (21 strikes/chain x 1 chain/candle).
            ticks.append(ReplayTick(ts=cur.ts, kind="option_chain", payload=_build_chain(index, cur.close, option_candle.close, synthetic_premium, option_type)))

    ticks.sort(key=lambda t: t.ts)
    return ticks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a replay tick JSONL from candle JSON files")
    parser.add_argument("--index-file", required=True, help="1-min index/spot candle JSON")
    parser.add_argument("--option-file", required=True, help="1-min option premium candle JSON (one leg)")
    parser.add_argument("--option-type", required=True, choices=["CE", "PE"], help="Which leg the option file is")
    parser.add_argument("--index", default="NIFTY", choices=[i.value for i in Index])
    parser.add_argument("--out", required=True, help="Output replay tick JSONL path")
    args = parser.parse_args(argv)

    index_candles = load_candles(args.index_file)
    option_candles = load_candles(args.option_file)
    ticks = build_ticks(index_candles, option_candles, Index(args.index), args.option_type)
    save_replay_ticks(ticks, args.out)
    days = sorted({c.ts.date() for c in index_candles})
    print(f"Wrote {len(ticks)} replay ticks ({len(index_candles)} candles across {len(days)} day(s): {[str(d) for d in days]}) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
