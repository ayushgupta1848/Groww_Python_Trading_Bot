"""Result dataclasses returned by every engine. See docs/DESIGN.md §3-4.

Every engine result extends EngineResult(direction, score, confidence, reasons). Two
composite dataclasses (EntryContext, TradeState) aggregate several engine results together
and are built only by the Orchestrator / TradeManager, never by an individual engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config.constants import Direction, MarketStructure, TradeAction, TradeLifecycleState


@dataclass(frozen=True)
class EngineResult:
    direction: Direction
    score: float
    confidence: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TrendResult(EngineResult):
    ehma_value: float
    ema100_value: float
    trend_angle: float
    trend_strength: float


@dataclass(frozen=True)
class MarketStructureResult(EngineResult):
    structure: MarketStructure
    strength: float


@dataclass(frozen=True)
class SupportResistanceResult(EngineResult):
    levels: tuple[float, ...]
    nearest_support: float
    nearest_resistance: float
    distance_to_support: float
    distance_to_resistance: float
    breakout: bool
    breakdown: bool


@dataclass(frozen=True)
class PremiumMomentumResult(EngineResult):
    velocity: float
    acceleration: float
    higher_highs: bool
    higher_lows: bool
    consistency: float


@dataclass(frozen=True)
class OptionSelectionResult(EngineResult):
    best_ce_symbol: str | None
    best_pe_symbol: str | None
    ce_premium: float | None
    pe_premium: float | None
    ce_liquidity_score: float
    pe_liquidity_score: float
    ce_spread_score: float
    pe_spread_score: float


@dataclass(frozen=True)
class BreakoutResult(EngineResult):
    breakout_confirmed: bool
    breakdown_confirmed: bool
    confirmation_bars_elapsed: int


@dataclass(frozen=True)
class MarketStrengthResult(EngineResult):
    candle_speed: float
    range_expansion: float
    consolidation_score: float
    trend_confidence: float


@dataclass(frozen=True)
class VolatilityResult(EngineResult):
    acceptable: bool
    spread_pct: float
    spike_score: float
    gap_detected: bool
    whipsaw_detected: bool


@dataclass(frozen=True)
class TradingRulesResult(EngineResult):
    allowed: bool
    trades_today: int
    consecutive_losses: int
    is_expiry_day: bool
    near_market_close: bool


@dataclass(frozen=True)
class RiskResult(EngineResult):
    safe_to_trade: bool
    already_in_trade: bool
    order_pending: bool
    broker_connected: bool


@dataclass(frozen=True)
class SignalStabilityResult(EngineResult):
    stable: bool
    confirmation_seconds_elapsed: float
    required_seconds: float  # adaptive, see docs/DESIGN.md §3b


@dataclass(frozen=True)
class EligibilityResult:
    passed: bool
    reasons: tuple[str, ...]
    failed_checks: tuple[str, ...]


@dataclass(frozen=True)
class DecisionResult(EngineResult):
    action: TradeAction
    buy_score: float
    sell_score: float
    exit_score: float
    eligibility: EligibilityResult
    trade_quality_score: float  # 0-100, analytics-only, never gates execution — see docs/DESIGN.md §3d


@dataclass(frozen=True)
class PositionSizeResult(EngineResult):
    lots: int
    capital_allocated: float
    margin_required: float
    risk_percentage_used: float


@dataclass(frozen=True)
class EntryContext:
    """Full engine snapshot captured at trade-open time, for replay analysis. See docs/DESIGN.md §3a."""

    trend: TrendResult
    market_structure: MarketStructureResult
    support_resistance: SupportResistanceResult
    premium_momentum: PremiumMomentumResult
    breakout: BreakoutResult
    market_strength: MarketStrengthResult
    volatility: VolatilityResult
    decision: DecisionResult


@dataclass(frozen=True)
class TradeState:
    state: TradeLifecycleState
    entry_price: float
    current_price: float
    highest_premium: float
    lowest_premium: float
    current_profit: float
    current_loss: float
    time_in_trade_seconds: float
    highest_spot: float
    lowest_spot: float
    exit_reason: str | None
    entry_context: EntryContext | None


@dataclass(frozen=True)
class ManualTradeRecord:
    """One manually-executed trade, imported for replay comparison. See docs/DESIGN.md §11a."""

    timestamp: datetime
    instrument: str
    action: TradeAction
    price: float
    lots: int


@dataclass(frozen=True)
class ComparisonReport:
    """Bot-decision vs. manual-trade agreement over a replay run. See docs/DESIGN.md §11a."""

    total_bot_decisions: int
    total_manual_trades: int
    matched: tuple[tuple[DecisionResult, ManualTradeRecord], ...]
    bot_only: tuple[DecisionResult, ...]
    manual_only: tuple[ManualTradeRecord, ...]
    agreement_pct: float
