"""CSV + JSON (JSONL) decision/trade logger, mode-tagged (live/shadow/replay). See
docs/DESIGN.md §8.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from ..models.engine_results import (
    BreakoutResult,
    DecisionResult,
    EntryContext,
    MarketStrengthResult,
    MarketStructureResult,
    OptionSelectionResult,
    PremiumMomentumResult,
    RiskResult,
    SignalStabilityResult,
    SupportResistanceResult,
    TradingRulesResult,
    TrendResult,
    VolatilityResult,
)

CSV_HEADER = [
    "timestamp", "mode", "spot", "ce_premium", "pe_premium", "trend_score", "structure_score",
    "sr_score", "momentum_score", "stability_stable", "stability_required_seconds",
    "option_ce_symbol", "option_pe_symbol", "breakout_confirmed", "market_strength_score",
    "volatility_acceptable", "rules_allowed", "risk_safe", "eligibility_passed", "action",
    "buy_score", "sell_score", "exit_score", "confidence", "trade_quality_score", "reasons", "exit_reason",
]


class DecisionLogger:
    def __init__(self, log_dir: Path | str, mode: str) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._mode = mode
        self._current_date: str | None = None
        self._csv_file = None
        self._csv_writer = None
        self._jsonl_path: Path | None = None

    def _ensure_files(self, timestamp: datetime) -> None:
        date_str = timestamp.strftime("%Y-%m-%d")
        if date_str == self._current_date:
            return
        if self._csv_file is not None:
            self._csv_file.close()
        self._current_date = date_str
        csv_path = self._log_dir / f"decisions_{date_str}.csv"
        is_new = not csv_path.exists()
        self._csv_file = open(csv_path, "a", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        if is_new:
            self._csv_writer.writerow(CSV_HEADER)
        self._jsonl_path = self._log_dir / f"events_{date_str}.jsonl"

    def _write_jsonl(self, event: dict) -> None:
        with open(self._jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def log_decision(
        self,
        timestamp: datetime,
        spot: float,
        ce_premium: float | None,
        pe_premium: float | None,
        trend: TrendResult,
        structure: MarketStructureResult,
        support_resistance: SupportResistanceResult,
        momentum: PremiumMomentumResult,
        stability: SignalStabilityResult,
        option_selection: OptionSelectionResult,
        breakout: BreakoutResult,
        market_strength: MarketStrengthResult,
        volatility: VolatilityResult,
        trading_rules: TradingRulesResult,
        risk: RiskResult,
        decision: DecisionResult,
        diagnostics: dict | None = None,
    ) -> None:
        self._ensure_files(timestamp)
        self._csv_writer.writerow(
            [
                timestamp.isoformat(), self._mode, spot, ce_premium, pe_premium,
                round(trend.score, 2), round(structure.score, 2), round(support_resistance.score, 2),
                round(momentum.score, 2), stability.stable, round(stability.required_seconds, 2),
                option_selection.best_ce_symbol, option_selection.best_pe_symbol, breakout.breakout_confirmed,
                round(market_strength.score, 2), volatility.acceptable, trading_rules.allowed, risk.safe_to_trade,
                decision.eligibility.passed, decision.action.value, round(decision.buy_score, 2),
                round(decision.sell_score, 2), round(decision.exit_score, 2), round(decision.confidence, 2),
                round(decision.trade_quality_score, 2), "; ".join(decision.reasons), "",
            ]
        )
        self._csv_file.flush()
        event = {
            "event": "decision" if decision.action != decision.action.REJECT else "rejected",
            "mode": self._mode,
            "timestamp": timestamp.isoformat(),
            "action": decision.action.value,
            "confidence": round(decision.confidence, 2),
            "trade_quality_score": round(decision.trade_quality_score, 2),
            "reasons": list(decision.reasons),
        }
        # Per-engine score / threshold / pass-fail / weight / contribution — makes any
        # rejected cycle directly explainable for tuning. See config/README.md.
        if diagnostics is not None:
            event["diagnostics"] = diagnostics
        self._write_jsonl(event)

    def log_trade_opened(
        self, timestamp: datetime, instrument: str, entry_price: float, lots: int, entry_context: EntryContext
    ) -> None:
        self._ensure_files(timestamp)
        self._write_jsonl(
            {
                "event": "trade_opened",
                "mode": self._mode,
                "timestamp": timestamp.isoformat(),
                "instrument": instrument,
                "entry_price": entry_price,
                "lots": lots,
                "entry_context": {
                    "trend_score": round(entry_context.trend.score, 2),
                    "structure": entry_context.market_structure.structure.value,
                    "sr_distance_to_resistance": round(entry_context.support_resistance.distance_to_resistance, 2),
                    "premium_momentum_velocity": round(entry_context.premium_momentum.velocity, 2),
                    "breakout_confirmed": entry_context.breakout.breakout_confirmed,
                    "market_strength_score": round(entry_context.market_strength.score, 2),
                    "volatility_acceptable": entry_context.volatility.acceptable,
                    "decision_confidence": round(entry_context.decision.confidence, 2),
                    "trade_quality_score": round(entry_context.decision.trade_quality_score, 2),
                    "reasons": list(entry_context.decision.reasons),
                },
            }
        )

    def log_trade_closed(
        self, timestamp: datetime, instrument: str, exit_price: float, pnl: float, exit_reason: str | None
    ) -> None:
        self._ensure_files(timestamp)
        self._write_jsonl(
            {
                "event": "trade_closed",
                "mode": self._mode,
                "timestamp": timestamp.isoformat(),
                "instrument": instrument,
                "exit_price": exit_price,
                "pnl": pnl,
                "exit_reason": exit_reason,
            }
        )

    def close(self) -> None:
        if self._csv_file is not None:
            self._csv_file.close()
