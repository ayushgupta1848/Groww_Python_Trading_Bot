"""Shared test fixtures: builders for MarketSnapshot and its parts, with sensible
defaults so each test only overrides what it cares about.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from trading_decision_engine.app.models.market_snapshot import (
    Candle,
    MarketSnapshot,
    OptionChainView,
    OptionLeg,
    PremiumTick,
    SessionState,
)

BASE_TS = datetime(2026, 7, 13, 10, 0, 0)  # a Monday


def make_candles(count: int, start_price: float = 24000.0, step: float = 1.0, start_ts: datetime = BASE_TS) -> tuple[Candle, ...]:
    """A smooth uptrend of `count` 1-minute candles (step > 0) or downtrend (step < 0)."""
    candles = []
    price = start_price
    for i in range(count):
        o = price
        price += step
        c = price
        candles.append(
            Candle(
                ts=start_ts + timedelta(minutes=i),
                open=o,
                high=max(o, c) + abs(step) * 0.2,
                low=min(o, c) - abs(step) * 0.2,
                close=c,
                volume=1000,
            )
        )
    return tuple(candles)


def make_zigzag_candles(count: int, start_price: float = 24000.0, start_ts: datetime = BASE_TS) -> tuple[Candle, ...]:
    """Alternates up-legs and down-legs of 10 bars each, producing genuine swing points."""
    candles = []
    price = start_price
    leg_len = 10
    for i in range(count):
        direction = 1 if (i // leg_len) % 2 == 0 else -1
        o = price
        price += direction * 3.0
        c = price
        candles.append(
            Candle(ts=start_ts + timedelta(minutes=i), open=o, high=max(o, c) + 1, low=min(o, c) - 1, close=c, volume=1000)
        )
    return tuple(candles)


def make_premium_history(
    count: int = 12, start_ce: float = 100.0, start_pe: float = 90.0, ce_step: float = 1.0, pe_step: float = -1.0,
    interval_seconds: float = 0.25, start_ts: datetime = BASE_TS,
) -> tuple[PremiumTick, ...]:
    ticks = []
    ce, pe = start_ce, start_pe
    for i in range(count):
        ce += ce_step
        pe += pe_step
        ts = start_ts + timedelta(seconds=i * interval_seconds)
        ticks.append(PremiumTick(ts=ts, ce_premium=max(0.05, ce), pe_premium=max(0.05, pe), bid=ce - 0.5, ask=ce + 0.5))
    return tuple(ticks)


def make_option_leg(symbol: str = "NIFTYTESTCE", ltp: float = 120.0, oi: int = 100_000, volume: int = 20_000, bid: float | None = None, ask: float | None = None) -> OptionLeg:
    return OptionLeg(
        trading_symbol=symbol, ltp=ltp, open_interest=oi, volume=volume,
        bid=bid if bid is not None else ltp - 0.5, ask=ask if ask is not None else ltp + 0.5, iv=12.0, delta=0.5,
    )


def make_option_chain(spot: float = 24000.0, strike_step: float = 50.0, width: int = 10) -> OptionChainView:
    strikes = {}
    base_strike = round(spot / strike_step) * strike_step
    for k in range(-width, width + 1):
        strike = base_strike + k * strike_step
        strikes[strike] = {
            "CE": make_option_leg(f"NIFTY{int(strike)}CE", ltp=max(1.0, 150 - abs(k) * 8)),
            "PE": make_option_leg(f"NIFTY{int(strike)}PE", ltp=max(1.0, 140 - abs(k) * 8)),
        }
    return OptionChainView(underlying_ltp=spot, strikes=strikes)


def make_session(
    already_in_trade: bool = False, order_pending: bool = False, margin_available: float = 100_000.0,
    broker_connected: bool = True, trades_today: int = 0, consecutive_losses: int = 0, daily_pnl: float = 0.0,
    current_exposure: float = 0.0, cooldown_until: datetime | None = None,
) -> SessionState:
    return SessionState(
        already_in_trade=already_in_trade, order_pending=order_pending, margin_available=margin_available,
        broker_connected=broker_connected, trades_today=trades_today, consecutive_losses=consecutive_losses,
        daily_pnl=daily_pnl, current_exposure=current_exposure, cooldown_until=cooldown_until,
    )


def make_snapshot(
    timestamp: datetime = BASE_TS,
    spot: float = 24000.0,
    candles: tuple[Candle, ...] = (),
    premium_history: tuple[PremiumTick, ...] = (),
    option_chain: OptionChainView | None = None,
    session: SessionState | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp=timestamp,
        spot=spot,
        candles=candles,
        premium_history=premium_history,
        option_chain=option_chain if option_chain is not None else make_option_chain(spot),
        session=session if session is not None else make_session(),
    )


def make_decision_input(
    trend_direction=None,
    trend_score: float = 80.0,
    breakout_confirmed: bool = True,
    strength_direction=None,
):
    """A strongly-bullish DecisionInput that passes every default Stage-1 gate, with
    convenience knobs for the dimensions config tests care about. Imported lazily so
    fixtures stays importable without the engine-results dependency chain at
    module-import time.
    """
    from trading_decision_engine.app.config.constants import Direction, MarketStructure
    from trading_decision_engine.app.engines.decision_engine import DecisionInput
    from trading_decision_engine.app.models.engine_results import (
        BreakoutResult,
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

    trend_direction = trend_direction if trend_direction is not None else Direction.BULLISH
    strength_direction = strength_direction if strength_direction is not None else Direction.BULLISH
    return DecisionInput(
        trend=TrendResult(direction=trend_direction, score=trend_score, confidence=85, reasons=("EHMA rising",), ehma_value=100, ema100_value=95, trend_angle=20, trend_strength=80),
        market_structure=MarketStructureResult(direction=Direction.BULLISH, score=75, confidence=75, reasons=(), structure=MarketStructure.HH_HL, strength=75),
        support_resistance=SupportResistanceResult(direction=Direction.BULLISH, score=70, confidence=90, reasons=(), levels=(), nearest_support=23900, nearest_resistance=24100, distance_to_support=50, distance_to_resistance=50, breakout=False, breakdown=False),
        premium_momentum=PremiumMomentumResult(direction=Direction.BULLISH, score=80, confidence=80, reasons=(), velocity=5, acceleration=1, higher_highs=True, higher_lows=True, consistency=80),
        option_selection=OptionSelectionResult(direction=Direction.NEUTRAL, score=90, confidence=90, reasons=(), best_ce_symbol="NIFTYCE", best_pe_symbol="NIFTYPE", ce_premium=120.0, pe_premium=90.0, ce_liquidity_score=95, pe_liquidity_score=90, ce_spread_score=95, pe_spread_score=90),
        breakout=BreakoutResult(direction=Direction.BULLISH if breakout_confirmed else Direction.NEUTRAL, score=95 if breakout_confirmed else 40, confidence=95, reasons=(), breakout_confirmed=breakout_confirmed, breakdown_confirmed=False, confirmation_bars_elapsed=3 if breakout_confirmed else 1),
        market_strength=MarketStrengthResult(direction=strength_direction, score=90, confidence=90, reasons=(), candle_speed=2, range_expansion=1.2, consolidation_score=20, trend_confidence=90),
        volatility=VolatilityResult(direction=Direction.NEUTRAL, score=100, confidence=100, reasons=(), acceptable=True, spread_pct=0.5, spike_score=0, gap_detected=False, whipsaw_detected=False),
        trading_rules=TradingRulesResult(direction=Direction.NEUTRAL, score=100, confidence=100, reasons=("clear",), allowed=True, trades_today=0, consecutive_losses=0, is_expiry_day=False, near_market_close=False),
        risk=RiskResult(direction=Direction.NEUTRAL, score=100, confidence=100, reasons=("safe",), safe_to_trade=True, already_in_trade=False, order_pending=False, broker_connected=True),
        signal_stability=SignalStabilityResult(direction=Direction.BULLISH, score=100, confidence=100, reasons=("stable",), stable=True, confirmation_seconds_elapsed=4.0, required_seconds=2.0),
    )
