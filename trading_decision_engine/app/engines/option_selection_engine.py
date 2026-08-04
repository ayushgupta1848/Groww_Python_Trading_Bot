"""OptionSelectionEngine: picks the best tradable CE and PE strike by premium-range fit,
liquidity (OI + volume), and spread — replacing a hardcoded ATM lookup. See
docs/DESIGN.md §3 (row 5).
"""

from __future__ import annotations

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import OptionSelectionResult
from ..models.market_snapshot import MarketSnapshot, OptionLeg


def _liquidity_score(leg: OptionLeg, min_oi: int, min_volume: int) -> float:
    oi_component = min(50.0, (leg.open_interest / min_oi) * 50.0) if min_oi else 0.0
    vol_component = min(50.0, (leg.volume / min_volume) * 50.0) if min_volume else 0.0
    return min(100.0, oi_component + vol_component)


def _spread_score(leg: OptionLeg, max_spread_pct: float) -> float:
    if leg.ltp <= 0:
        return 0.0
    spread_pct = (leg.ask - leg.bid) / leg.ltp * 100.0
    if spread_pct <= 0:
        return 100.0
    return max(0.0, 100.0 - (spread_pct / max_spread_pct) * 100.0)


class OptionSelectionEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    def _best_leg(self, legs: list[tuple[float, OptionLeg]]) -> tuple[float | None, OptionLeg | None, float, float]:
        cfg = self._config
        candidates = []
        for strike, leg in legs:
            if leg is None:
                continue
            if not (cfg.premium_min <= leg.ltp <= cfg.premium_max):
                continue
            liquidity = _liquidity_score(leg, cfg.liquidity_min_oi, cfg.liquidity_min_volume)
            spread = _spread_score(leg, cfg.max_spread_pct)
            # Optional hard floors (0 = disabled): drop candidates whose liquidity or
            # spread quality is below the configured minimum score.
            if cfg.option_min_liquidity_score > 0 and liquidity < cfg.option_min_liquidity_score:
                continue
            if cfg.option_min_spread_score > 0 and spread < cfg.option_min_spread_score:
                continue
            w = cfg.option_liquidity_weight
            combined = liquidity * w + spread * (1.0 - w)
            candidates.append((combined, strike, leg, liquidity, spread))
        if not candidates:
            return None, None, 0.0, 0.0
        candidates.sort(key=lambda c: c[0], reverse=True)
        _, strike, leg, liquidity, spread = candidates[0]
        return strike, leg, liquidity, spread

    def analyze(self, snapshot: MarketSnapshot) -> OptionSelectionResult:
        chain = snapshot.option_chain
        ce_legs = [(strike, legs.get("CE")) for strike, legs in chain.strikes.items()]
        pe_legs = [(strike, legs.get("PE")) for strike, legs in chain.strikes.items()]

        ce_strike, ce_leg, ce_liquidity, ce_spread = self._best_leg(ce_legs)
        pe_strike, pe_leg, pe_liquidity, pe_spread = self._best_leg(pe_legs)

        reasons: list[str] = []
        if ce_leg is not None:
            reasons.append(
                f"Best CE {ce_leg.trading_symbol} @ {ce_leg.ltp:.2f} (liquidity {ce_liquidity:.0f}, spread {ce_spread:.0f})"
            )
        else:
            reasons.append("No CE strike within tradable premium range/liquidity")
        if pe_leg is not None:
            reasons.append(
                f"Best PE {pe_leg.trading_symbol} @ {pe_leg.ltp:.2f} (liquidity {pe_liquidity:.0f}, spread {pe_spread:.0f})"
            )
        else:
            reasons.append("No PE strike within tradable premium range/liquidity")

        found_scores = [s for s in (ce_liquidity, pe_liquidity) if s]
        combined_spread_scores = [s for s in (ce_spread, pe_spread) if s]
        score = sum(found_scores) / len(found_scores) if found_scores else 0.0
        confidence = sum(combined_spread_scores) / len(combined_spread_scores) if combined_spread_scores else 0.0

        return OptionSelectionResult(
            direction=Direction.NEUTRAL,
            score=score,
            confidence=confidence,
            reasons=tuple(reasons),
            best_ce_symbol=ce_leg.trading_symbol if ce_leg else None,
            best_pe_symbol=pe_leg.trading_symbol if pe_leg else None,
            ce_premium=ce_leg.ltp if ce_leg else None,
            pe_premium=pe_leg.ltp if pe_leg else None,
            ce_liquidity_score=ce_liquidity,
            pe_liquidity_score=pe_liquidity,
            ce_spread_score=ce_spread,
            pe_spread_score=pe_spread,
        )
