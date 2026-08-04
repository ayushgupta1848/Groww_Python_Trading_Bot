"""DecisionEngine: combines every other engine's result into a BUY/SELL/HOLD/REJECT
decision via a two-stage process (mandatory eligibility, then trade-quality scoring),
plus an analytics-only trade_quality_score. Every gate is individually switchable and
every threshold configurable — see config/README.md. Rejection reasons always carry
"actual vs required" so tuning needs only the logs. See docs/DESIGN.md §3 (row 12),
§3c, §3d.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.constants import Direction, TradeAction
from ..config.strategy import StrategyConfig
from ..models.engine_results import (
    BreakoutResult,
    DecisionResult,
    EligibilityResult,
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

# Stage 2 uses exactly these "quality" dimensions, per docs/DESIGN.md §3c.
_QUALITY_DIRECTIONAL_KEYS = ("market_structure", "premium_momentum", "breakout", "market_strength")


@dataclass(frozen=True)
class DecisionInput:
    trend: TrendResult
    market_structure: MarketStructureResult
    support_resistance: SupportResistanceResult
    premium_momentum: PremiumMomentumResult
    option_selection: OptionSelectionResult
    breakout: BreakoutResult
    market_strength: MarketStrengthResult
    volatility: VolatilityResult
    trading_rules: TradingRulesResult
    risk: RiskResult
    signal_stability: SignalStabilityResult


class DecisionEngine:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or StrategyConfig()

    @property
    def config(self) -> StrategyConfig:
        return self._config

    def set_config(self, config: StrategyConfig) -> None:
        """Live-reload hook: swap thresholds/weights atomically between cycles."""
        self._config = config

    def decide(self, inputs: DecisionInput) -> DecisionResult:
        eligibility = self._check_eligibility(inputs)
        if not eligibility.passed:
            return DecisionResult(
                direction=Direction.NEUTRAL,
                score=0.0,
                confidence=0.0,
                reasons=eligibility.reasons,
                action=TradeAction.REJECT,
                buy_score=0.0,
                sell_score=0.0,
                exit_score=0.0,
                eligibility=eligibility,
                trade_quality_score=0.0,
            )
        return self._score_quality(inputs, eligibility)

    # ------------------------------------------------------------------ Stage 1
    def _check_eligibility(self, inputs: DecisionInput) -> EligibilityResult:
        """Every gate is a require_* config toggle. A disabled gate is skipped entirely
        (its engine still contributes to Stage-2 scoring). Reasons always state actual
        vs required so a rejection log is directly actionable for tuning.
        """
        cfg = self._config
        reasons: list[str] = []
        failed: list[str] = []
        trend_direction = inputs.trend.direction

        if cfg.require_trend:
            if trend_direction == Direction.NEUTRAL or inputs.trend.score < cfg.trend_threshold:
                failed.append("trend")
                reasons.append(
                    f"Trend not confirmed: score {inputs.trend.score:.0f} vs required {cfg.trend_threshold:.0f}, direction {trend_direction.value}"
                )

        if cfg.require_signal_stability and not inputs.signal_stability.stable:
            failed.append("signal_stability")
            reasons.extend(f"Signal Stability: {r}" for r in inputs.signal_stability.reasons)

        if cfg.require_trading_rules and not inputs.trading_rules.allowed:
            failed.append("trading_rules")
            reasons.extend(f"Trading Rules: {r}" for r in inputs.trading_rules.reasons)

        if cfg.require_risk and not inputs.risk.safe_to_trade:
            failed.append("risk")
            reasons.extend(f"Risk: {r}" for r in inputs.risk.reasons)

        if cfg.require_support_resistance:
            if trend_direction == Direction.BULLISH:
                room = inputs.support_resistance.distance_to_resistance
                if room < cfg.min_resistance_distance:
                    failed.append("support_resistance")
                    reasons.append(f"Room to resistance {room:.1f} vs required {cfg.min_resistance_distance:.1f}")
            elif trend_direction == Direction.BEARISH:
                room = inputs.support_resistance.distance_to_support
                if room < cfg.min_resistance_distance:
                    failed.append("support_resistance")
                    reasons.append(f"Room to support {room:.1f} vs required {cfg.min_resistance_distance:.1f}")

        if cfg.require_volatility and not inputs.volatility.acceptable:
            failed.append("volatility")
            reasons.extend(f"Volatility: {r}" for r in inputs.volatility.reasons)

        # Optional extra gates — OFF by default (original behaviour: these engines only
        # contribute to Stage-2 scoring, they never veto).
        if cfg.require_market_structure and trend_direction != Direction.NEUTRAL:
            if inputs.market_structure.direction != trend_direction:
                failed.append("market_structure")
                reasons.append(
                    f"Structure {inputs.market_structure.direction.value} does not confirm trend {trend_direction.value} (required by require_market_structure)"
                )

        if cfg.require_breakout and trend_direction != Direction.NEUTRAL:
            confirmed = inputs.breakout.breakout_confirmed if trend_direction == Direction.BULLISH else inputs.breakout.breakdown_confirmed
            if not confirmed:
                failed.append("breakout")
                reasons.append(
                    f"Breakout not confirmed in {trend_direction.value} direction: {inputs.breakout.confirmation_bars_elapsed}/{cfg.breakout_confirmation_bars} bars (required by require_breakout)"
                )

        if cfg.require_market_strength and trend_direction != Direction.NEUTRAL:
            if inputs.market_strength.direction != trend_direction:
                failed.append("market_strength")
                reasons.append(
                    f"Market strength {inputs.market_strength.direction.value} does not confirm trend {trend_direction.value} (required by require_market_strength)"
                )

        if cfg.require_option_selection and trend_direction != Direction.NEUTRAL:
            symbol = inputs.option_selection.best_ce_symbol if trend_direction == Direction.BULLISH else inputs.option_selection.best_pe_symbol
            if symbol is None:
                failed.append("option_selection")
                reasons.append(f"No tradable {'CE' if trend_direction == Direction.BULLISH else 'PE'} strike available (required by require_option_selection)")

        passed = not failed
        if passed:
            reasons = ["All mandatory eligibility checks passed"]
        return EligibilityResult(passed=passed, reasons=tuple(reasons), failed_checks=tuple(failed))

    # ------------------------------------------------------------------ Stage 2
    def _score_quality(self, inputs: DecisionInput, eligibility: EligibilityResult) -> DecisionResult:
        cfg = self._config
        total_weight = sum(cfg.weights[k] for k in _QUALITY_DIRECTIONAL_KEYS) + cfg.weights["option_selection"]
        normalized = {k: cfg.weights[k] / total_weight for k in _QUALITY_DIRECTIONAL_KEYS}
        option_weight = cfg.weights["option_selection"] / total_weight

        directional_results = {
            "market_structure": inputs.market_structure,
            "premium_momentum": inputs.premium_momentum,
            "breakout": inputs.breakout,
            "market_strength": inputs.market_strength,
        }

        buy_score = 0.0
        sell_score = 0.0
        bullish_agreeing = 0
        bearish_agreeing = 0
        reasons: list[str] = []
        for key, result in directional_results.items():
            label = key.replace("_", " ").title()
            weight = normalized[key]
            if result.direction == Direction.BULLISH:
                buy_score += weight * result.score
                bullish_agreeing += 1
                reasons.append(f"✓ {label} bullish ({result.score:.0f})")
            elif result.direction == Direction.BEARISH:
                sell_score += weight * result.score
                bearish_agreeing += 1
                reasons.append(f"✓ {label} bearish ({result.score:.0f})")
            else:
                reasons.append(f"✗ {label} neutral")

        # Option selection has no direction of its own — it's a baseline tradability
        # quality factor that benefits whichever side the other dimensions favor.
        buy_score += option_weight * inputs.option_selection.score
        sell_score += option_weight * inputs.option_selection.score
        if inputs.option_selection.score > 0:
            reasons.append(f"✓ Option selection quality ({inputs.option_selection.score:.0f})")

        min_buy = cfg.min_buy_score if cfg.min_buy_score is not None else cfg.decision_score_threshold
        min_sell = cfg.min_sell_score if cfg.min_sell_score is not None else cfg.decision_score_threshold

        action, direction, confidence = TradeAction.HOLD, Direction.NEUTRAL, max(buy_score, sell_score)
        agreeing = 0
        if buy_score >= min_buy and buy_score > sell_score:
            action, direction, confidence, agreeing = TradeAction.BUY, Direction.BULLISH, buy_score, bullish_agreeing
        elif sell_score >= min_sell and sell_score > buy_score:
            action, direction, confidence, agreeing = TradeAction.SELL, Direction.BEARISH, sell_score, bearish_agreeing
        else:
            reasons.append(
                f"Buy {buy_score:.0f} vs required {min_buy:.0f} · sell {sell_score:.0f} vs required {min_sell:.0f} — neither cleared"
            )

        trade_quality_score = self._trade_quality_score(inputs, buy_score, sell_score)

        # Post-score quality filters — each disabled at its default, each demotes an
        # otherwise-actionable signal to HOLD with an actual-vs-required reason.
        if action in (TradeAction.BUY, TradeAction.SELL):
            score_gap = abs(buy_score - sell_score)
            if cfg.min_score_difference > 0 and score_gap < cfg.min_score_difference:
                reasons.append(f"Score gap {score_gap:.0f} vs required {cfg.min_score_difference:.0f} — sides too close, HOLD")
                action, direction = TradeAction.HOLD, Direction.NEUTRAL
            elif cfg.min_engine_agreement > 0 and agreeing < cfg.min_engine_agreement:
                reasons.append(f"Engine agreement {agreeing}/{len(directional_results)} vs required {cfg.min_engine_agreement} — HOLD")
                action, direction = TradeAction.HOLD, Direction.NEUTRAL
            elif cfg.min_confidence > 0 and confidence < cfg.min_confidence:
                reasons.append(f"Confidence {confidence:.0f} vs required {cfg.min_confidence:.0f} — HOLD")
                action, direction = TradeAction.HOLD, Direction.NEUTRAL
            elif cfg.min_trade_quality > 0 and trade_quality_score < cfg.min_trade_quality:
                reasons.append(f"Trade quality {trade_quality_score:.0f} vs required {cfg.min_trade_quality:.0f} — HOLD")
                action, direction = TradeAction.HOLD, Direction.NEUTRAL

        return DecisionResult(
            direction=direction,
            score=max(buy_score, sell_score),
            confidence=confidence,
            reasons=tuple(reasons),
            action=action,
            buy_score=buy_score,
            sell_score=sell_score,
            exit_score=0.0,
            eligibility=eligibility,
            trade_quality_score=trade_quality_score,
        )

    def _trade_quality_score(self, inputs: DecisionInput, buy_score: float, sell_score: float) -> float:
        """Analytics-first composite — see docs/DESIGN.md §3d. Gates execution only when
        min_trade_quality is explicitly set above 0."""
        cfg = self._config
        base = max(buy_score, sell_score)

        stability_bonus = 0.0
        if inputs.signal_stability.required_seconds > 0:
            ratio = inputs.signal_stability.confirmation_seconds_elapsed / inputs.signal_stability.required_seconds
            stability_bonus = min(cfg.quality_stability_bonus_cap, max(0.0, (ratio - 1.0) * cfg.quality_stability_bonus_cap))

        liquidity_bonus = (
            (inputs.option_selection.ce_liquidity_score + inputs.option_selection.pe_liquidity_score) / 2.0
            * cfg.quality_liquidity_bonus_scale
        )
        spread_bonus = (
            (inputs.option_selection.ce_spread_score + inputs.option_selection.pe_spread_score) / 2.0
            * cfg.quality_spread_bonus_scale
        )

        return min(100.0, base + stability_bonus + liquidity_bonus + spread_bonus)
