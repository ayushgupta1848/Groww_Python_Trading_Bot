"""Decision transparency: turns one cycle's already-computed EngineResults +
DecisionResult into a complete, self-explanatory diagnostics object — per-engine raw
score, confidence, weight, weighted contribution, gate threshold, actual vs required,
pass/fail, and human-readable explanations, plus overall BUY/SELL/HOLD/EXIT confidence
and full Stage-1/Stage-2 detail.

Pure explainability layer (docs: config/README.md "Diagnostics & tuning workflow"):
consumes results, never influences them — no engine imports this module, nothing here
feeds back into scoring. The same object drives the console dashboard
(utils/console_dashboard.py) and the JSONL event log, so a replayed decision can be
reconstructed exactly.
"""

from __future__ import annotations

from datetime import datetime

from ..config.constants import Direction
from ..config.strategy import StrategyConfig
from ..models.engine_results import DecisionResult, SignalStabilityResult

# The Stage-2 scoring dimensions, in the same order DecisionEngine uses.
_QUALITY_KEYS = ("market_structure", "premium_momentum", "breakout", "market_strength")


def _confidences(decision: DecisionResult) -> dict:
    """Overall BUY/SELL/HOLD/EXIT confidence as percentages that sum to ~100.
    HOLD is the head-room neither side claimed; when buy+sell exceed 100 the trio is
    normalized. EXIT mirrors the decision's exit_score (0 outside an open trade).
    """
    buy, sell = decision.buy_score, decision.sell_score
    hold = max(0.0, 100.0 - buy - sell)
    total = buy + sell + hold
    if total <= 0:
        return {"buy": 0.0, "sell": 0.0, "hold": 100.0, "exit": round(decision.exit_score, 1)}
    return {
        "buy": round(buy / total * 100.0, 1),
        "sell": round(sell / total * 100.0, 1),
        "hold": round(hold / total * 100.0, 1),
        "exit": round(decision.exit_score, 1),
    }


def _engine_extras(key: str, result) -> dict:
    """Engine-specific human-relevant numbers, straight off the typed result."""
    if key == "trend":
        return {"ehma": round(result.ehma_value, 2), "ema_long": round(result.ema100_value, 2),
                "angle_deg": round(result.trend_angle, 1), "strength": round(result.trend_strength, 1)}
    if key == "market_structure":
        return {"structure": result.structure.value, "strength": round(result.strength, 1)}
    if key == "support_resistance":
        return {"nearest_support": round(result.nearest_support, 2),
                "nearest_resistance": round(result.nearest_resistance, 2),
                "distance_to_support": round(result.distance_to_support, 2) if result.distance_to_support != float("inf") else "unbounded",
                "distance_to_resistance": round(result.distance_to_resistance, 2) if result.distance_to_resistance != float("inf") else "unbounded",
                "breakout": result.breakout, "breakdown": result.breakdown}
    if key == "premium_momentum":
        return {"velocity": round(result.velocity, 3), "acceleration": round(result.acceleration, 3),
                "consistency_pct": round(result.consistency, 1),
                "higher_highs": result.higher_highs, "higher_lows": result.higher_lows}
    if key == "option_selection":
        return {"best_ce": result.best_ce_symbol, "best_pe": result.best_pe_symbol,
                "ce_premium": result.ce_premium, "pe_premium": result.pe_premium,
                "ce_liquidity": round(result.ce_liquidity_score, 1), "pe_liquidity": round(result.pe_liquidity_score, 1),
                "ce_spread": round(result.ce_spread_score, 1), "pe_spread": round(result.pe_spread_score, 1)}
    if key == "breakout":
        return {"breakout_confirmed": result.breakout_confirmed, "breakdown_confirmed": result.breakdown_confirmed,
                "bars_elapsed": result.confirmation_bars_elapsed}
    if key == "market_strength":
        return {"candle_speed": round(result.candle_speed, 2), "range_expansion": round(result.range_expansion, 2),
                "consolidation": round(result.consolidation_score, 1), "trend_confidence": round(result.trend_confidence, 1)}
    if key == "volatility":
        return {"acceptable": result.acceptable, "spread_pct": round(result.spread_pct, 2),
                "spike_score": round(result.spike_score, 1), "gap": result.gap_detected, "whipsaw": result.whipsaw_detected}
    if key == "trading_rules":
        return {"allowed": result.allowed, "trades_today": result.trades_today,
                "consecutive_losses": result.consecutive_losses, "is_expiry_day": result.is_expiry_day,
                "near_market_close": result.near_market_close}
    if key == "risk":
        return {"safe": result.safe_to_trade, "already_in_trade": result.already_in_trade,
                "order_pending": result.order_pending, "broker_connected": result.broker_connected}
    return {}


def _stage1_checks(results: dict, stability: SignalStabilityResult, decision: DecisionResult, cfg: StrategyConfig) -> dict:
    """Per-gate detail: enabled?, passed?, and the actual-vs-required pair the gate
    compared — exactly what a rejection needs to be actionable.
    """
    failed = set(decision.eligibility.failed_checks)
    trend = results["trend"]
    sr = results["support_resistance"]
    trend_dir = trend.direction

    if trend_dir == Direction.BULLISH:
        room_actual, room_label = sr.distance_to_resistance, "distance to resistance"
    elif trend_dir == Direction.BEARISH:
        room_actual, room_label = sr.distance_to_support, "distance to support"
    else:
        room_actual, room_label = None, "room (no trend direction)"

    def check(name: str, enabled: bool, actual, required) -> dict:
        return {
            "enabled": enabled,
            "passed": (name not in failed) if enabled else None,  # None = gate disabled, not judged
            "actual": actual,
            "required": required,
        }

    return {
        "trend": check("trend", cfg.require_trend,
                       f"score {trend.score:.0f}, direction {trend_dir.value}",
                       f"score >= {cfg.trend_threshold:.0f} and direction != NEUTRAL"),
        "signal_stability": check("signal_stability", cfg.require_signal_stability,
                                  f"stable {stability.confirmation_seconds_elapsed:.1f}s",
                                  f"{stability.required_seconds:.1f}s clean"),
        "trading_rules": check("trading_rules", cfg.require_trading_rules,
                               "allowed" if results["trading_rules"].allowed else "blocked", "all rules clear"),
        "risk": check("risk", cfg.require_risk,
                      "safe" if results["risk"].safe_to_trade else "unsafe", "operationally safe"),
        "support_resistance": check("support_resistance", cfg.require_support_resistance,
                                    f"{room_label} = " + ("n/a" if room_actual is None else ("unbounded" if room_actual == float("inf") else f"{room_actual:.1f}")),
                                    f">= {cfg.min_resistance_distance:.1f} pts"),
        "volatility": check("volatility", cfg.require_volatility,
                            "acceptable" if results["volatility"].acceptable else f"{len([r for r in results['volatility'].reasons if 'No volatility' not in r])} violation(s)",
                            "zero violations"),
        "market_structure": check("market_structure", cfg.require_market_structure,
                                  results["market_structure"].direction.value, "agrees with trend"),
        "breakout": check("breakout", cfg.require_breakout,
                          f"{results['breakout'].confirmation_bars_elapsed} bars", f"confirmed ({cfg.breakout_confirmation_bars} bars) in trend direction"),
        "market_strength": check("market_strength", cfg.require_market_strength,
                                 results["market_strength"].direction.value, "agrees with trend"),
        "option_selection": check("option_selection", cfg.require_option_selection,
                                  results["option_selection"].best_ce_symbol or results["option_selection"].best_pe_symbol or "none",
                                  "tradable strike exists"),
    }


def build_cycle_diagnostics(
    timestamp: datetime,
    results: dict,
    stability: SignalStabilityResult,
    decision: DecisionResult,
    cfg: StrategyConfig,
) -> dict:
    """The complete transparency object for one decision cycle. Serializable as-is into
    the JSONL event log; also the console dashboard's input.
    """
    total_weight = sum(cfg.weights[k] for k in _QUALITY_KEYS) + cfg.weights["option_selection"]
    winning = decision.direction
    failed = set(decision.eligibility.failed_checks)

    def contribution(key: str, result) -> float:
        # The exact number this engine added to the winning side's Stage-2 score:
        # normalized weight x score, counted only when the engine backed that side
        # (option selection is direction-neutral and always counts).
        if key == "option_selection":
            return round(cfg.weights[key] / total_weight * result.score, 2)
        if key in _QUALITY_KEYS and winning != Direction.NEUTRAL and result.direction == winning:
            return round(cfg.weights[key] / total_weight * result.score, 2)
        return 0.0

    engines: dict = {}
    for key, result in results.items():
        scoring = key in _QUALITY_KEYS or key == "option_selection"
        engines[key] = {
            "score": round(result.score, 2),
            "confidence": round(result.confidence, 2),
            "direction": result.direction.value,
            "weight": cfg.weights.get(key, 0.0),
            "weight_normalized": round(cfg.weights[key] / total_weight, 4) if scoring else 0.0,
            "contribution": contribution(key, result),
            "passed": key not in failed,
            "explanation": list(result.reasons),
            **_engine_extras(key, result),
        }
    engines["signal_stability"] = {
        "score": round(stability.score, 2),
        "confidence": round(stability.confidence, 2),
        "direction": stability.direction.value,
        "weight": 0.0,
        "weight_normalized": 0.0,
        "contribution": 0.0,
        "passed": "signal_stability" not in failed,
        "explanation": list(stability.reasons),
        "elapsed_seconds": round(stability.confirmation_seconds_elapsed, 2),
        "required_seconds": round(stability.required_seconds, 2),
        "stable": stability.stable,
    }

    min_buy = cfg.min_buy_score if cfg.min_buy_score is not None else cfg.decision_score_threshold
    min_sell = cfg.min_sell_score if cfg.min_sell_score is not None else cfg.decision_score_threshold
    agreeing = sum(
        1 for k in _QUALITY_KEYS
        if winning != Direction.NEUTRAL and results[k].direction == winning
    )

    return {
        "type": "decision_cycle",
        "timestamp": timestamp.isoformat(),
        "profile": cfg.active_profile or None,
        "confidence": _confidences(decision),
        "action": decision.action.value,
        "engines": engines,
        "stage1": {
            "passed": decision.eligibility.passed,
            "failed_checks": sorted(failed),
            "checks": _stage1_checks(results, stability, decision, cfg),
        },
        "stage2": {
            "evaluated": decision.eligibility.passed,  # Stage 2 never runs on a Stage-1 reject
            "buy_score": round(decision.buy_score, 2), "required_buy": min_buy,
            "sell_score": round(decision.sell_score, 2), "required_sell": min_sell,
            "confidence": round(decision.confidence, 2), "required_confidence": cfg.min_confidence,
            "trade_quality": round(decision.trade_quality_score, 2), "required_quality": cfg.min_trade_quality,
            "engine_agreement": f"{agreeing}/{len(_QUALITY_KEYS)}",
            "engine_agreement_pct": round(agreeing / len(_QUALITY_KEYS) * 100.0, 0),
            "min_score_difference": cfg.min_score_difference,
            "min_engine_agreement": cfg.min_engine_agreement,
        },
        "final": {
            "action": decision.action.value,
            "reasons": list(decision.reasons),
        },
    }
