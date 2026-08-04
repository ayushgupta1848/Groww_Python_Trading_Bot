"""Immutable market-data models. Every engine receives only a MarketSnapshot (or a value
derived from one by the Orchestrator). See docs/DESIGN.md §4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class PremiumTick:
    ts: datetime
    ce_premium: float
    pe_premium: float
    bid: float
    ask: float


@dataclass(frozen=True)
class OptionLeg:
    trading_symbol: str
    ltp: float
    open_interest: int
    volume: int
    bid: float
    ask: float
    iv: float
    delta: float


@dataclass(frozen=True)
class OptionChainView:
    underlying_ltp: float
    # {strike: {"CE": OptionLeg|None, "PE": OptionLeg|None}}
    strikes: dict[float, dict[str, OptionLeg | None]]


@dataclass(frozen=True)
class SessionState:
    already_in_trade: bool
    order_pending: bool
    margin_available: float
    broker_connected: bool
    trades_today: int
    consecutive_losses: int
    daily_pnl: float
    current_exposure: float
    cooldown_until: datetime | None


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp: datetime
    spot: float
    candles: tuple[Candle, ...]
    premium_history: tuple[PremiumTick, ...]
    option_chain: OptionChainView
    session: SessionState
