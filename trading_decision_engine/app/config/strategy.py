"""StrategyConfig: every tunable threshold/weight/interval, loaded from config/strategy.json
with optional profile overlays from config/profiles/<name>.json. No threshold is hardcoded in
engine code — see config/README.md for the full documented parameter reference and
docs/DESIGN.md §7. Secrets (Groww API key/TOTP secret) are loaded separately by
load_broker_credentials(), never from strategy.json.

Load precedence (later wins):
    dataclass defaults  <-  config/strategy.json  <-  config/profiles/<active_profile>.json

`active_profile` is itself a strategy.json key (or the --profile CLI flag), so switching
between e.g. "aggressive" and "conservative" is a one-line JSON edit — never a code change.

Live reload: StrategyConfig instances are immutable (frozen dataclass). The Orchestrator
polls the config file's mtime and, when it changes, loads a fresh instance and swaps it
into every engine atomically between decision cycles — see Orchestrator.reload_strategy().
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

logger = logging.getLogger("trading_decision_engine.config")

REPO_ROOT = Path(__file__).resolve().parents[3]
ENGINE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STRATEGY_PATH = ENGINE_ROOT / "config" / "strategy.json"
PROFILES_DIR = ENGINE_ROOT / "config" / "profiles"


@dataclass(frozen=True)
class StrategyConfig:
    # ------------------------------------------------------------------ data cadence
    option_chain_refresh_seconds: float = 3.0   # bounded option-chain pull interval (no push feed exists)
    candle_interval: str = "1minute"            # candle interval backing Trend/S-R/Structure engines

    # ------------------------------------------------------------------ profile
    active_profile: str = ""                    # "" = no profile; else config/profiles/<name>.json overlays this file

    # ------------------------------------------------------------------ Trend Engine
    trend_threshold: float = 60.0               # Stage-1 gate: min trend score to consider bullish/bearish
    trend_ehma_length: int = 16                 # EHMA period (Pine indicator's `length`)
    trend_ema_long_length: int = 100            # confirmation EMA period (Pine's ema100)
    trend_angle_lookback_bars: int = 5          # bars over which the EHMA slope/angle is measured
    trend_angle_scale: float = 300.0            # slope-% multiplier before atan() — higher = steeper angles from small moves
    trend_min_angle: float = 0.0                # direction forced NEUTRAL when |angle| below this (0 = disabled)
    trend_confidence_ema_agrees: float = 85.0   # confidence when price vs EMA-long agrees with EHMA direction
    trend_confidence_ema_disagrees: float = 40.0
    trend_confidence_ema_unavailable: float = 55.0
    trend_score_strength_weight: float = 0.7    # score = strength*this + confidence*(1-this... see next field)
    trend_score_confidence_weight: float = 0.3

    # ------------------------------------------------------------------ Market Structure Engine
    structure_swing_left: int = 3               # pivot lookback (swing detection sensitivity)
    structure_swing_right: int = 3              # pivot lookahead (confirmation delay in bars)
    structure_min_candles: int = 30
    structure_exhaustion_threshold: float = 40.0   # min divergence strength to flag EXHAUSTION
    structure_double_tolerance_pct: float = 0.15   # swing-height tolerance (%) for double top/bottom
    structure_compression_lookback: int = 20
    structure_compression_ratio: float = 0.6       # recent/prior range ratio at or below = COMPRESSION
    structure_expansion_ratio: float = 1.6         # recent/prior range ratio at or above = EXPANSION
    structure_min_strength: float = 0.0            # direction forced NEUTRAL when strength below this (0 = disabled)

    # ------------------------------------------------------------------ Support / Resistance Engine
    sr_pivot_left: int = 33                     # Pine `left`
    sr_pivot_right: int = 21                    # Pine `right`
    sr_quick_pivot_right: int = 3               # Pine `quick_right`
    min_resistance_distance: float = 15.0       # Stage-1 gate: min points of room toward the trade side's level
    sr_breakout_buffer_points: float = 0.0      # spot must clear the level by this many extra points to flag breakout/breakdown

    # ------------------------------------------------------------------ Premium Momentum Engine
    premium_momentum_min_samples: int = 6       # min premium ticks in the rolling window before reporting
    premium_velocity_scale: float = 40.0        # CE-PE spread velocity (pts/sec) that maps to score 100
    momentum_threshold: float = 0.05            # min |velocity| (pts/sec) to call direction BULLISH/BEARISH vs NEUTRAL
    momentum_min_acceleration: float = 0.0      # direction forced NEUTRAL when acceleration opposes it beyond this (0 = disabled)
    momentum_min_consistency: float = 0.0       # direction forced NEUTRAL when consistency %% below this (0 = disabled)

    # ------------------------------------------------------------------ Option Selection Engine
    premium_min: float = 60.0                   # tradable premium range lower bound
    premium_max: float = 250.0                  # tradable premium range upper bound
    liquidity_min_oi: int = 50_000              # min open interest for a strike to be selectable
    liquidity_min_volume: int = 10_000          # min traded volume for a strike to be selectable
    max_spread_pct: float = 2.0                 # max bid/ask spread (% of premium) considered liquid
    option_min_liquidity_score: float = 0.0     # candidates below this liquidity score are dropped (0 = disabled)
    option_min_spread_score: float = 0.0        # candidates below this spread score are dropped (0 = disabled)
    option_liquidity_weight: float = 0.5        # candidate ranking = liquidity*this + spread*(1-this)

    # ------------------------------------------------------------------ Breakout Engine
    breakout_confirmation_bars: int = 2         # consecutive closes beyond the level to confirm
    breakout_buffer_points: float = 0.0         # closes must clear the level by this many extra points

    # ------------------------------------------------------------------ Market Strength Engine
    market_strength_window: int = 10            # candle window compared against the prior window of same size
    market_strength_consolidation_threshold: float = 60.0  # consolidation_score at or above = "consolidating" label

    # ------------------------------------------------------------------ Volatility Engine
    volatility_min_candles: int = 20
    volatility_range_lookback: int = 15
    volatility_spike_multiplier: float = 2.5    # last range > this x avg range = spike violation
    volatility_gap_multiplier: float = 1.5      # open gap > this x avg range = gap violation
    volatility_abnormal_multiplier: float = 2.0 # recent avg range > this x longer avg = abnormal-vol violation
    volatility_whipsaw_window: int = 6
    volatility_whipsaw_min_reversals: int = 4
    volatility_violation_penalty: float = 25.0  # score = 100 - penalty x violations

    # ------------------------------------------------------------------ Trading Rules Engine
    max_trades_per_day: int = 6
    cooldown_seconds: int = 20                  # signals ignored for this long after every exit
    consecutive_loss_limit: int = 3
    daily_loss_limit: float = 5_000.0           # blocks new entries AND force-exits an open trade
    daily_profit_lock: float = 10_000.0
    max_exposure: float = 100_000.0
    expiry_day_cutoff_hour: int = 14            # hour (24h) after which expiry-day entries stop
    market_close_buffer_minutes: int = 15       # no entries / force exit inside this window before close
    wait_after_open_minutes: int = 5            # WAIT_MODE duration after 09:15

    # ------------------------------------------------------------------ Risk Engine
    risk_min_margin_available: float = 0.0      # margin must exceed this (₹) for safe_to_trade

    # ------------------------------------------------------------------ Signal Stability Engine (adaptive window, §3b)
    signal_stability_base_seconds: float = 3.0  # documented default; effective window is adaptive below
    signal_stability_min_seconds: float = 1.5   # window when combined strength >= strong threshold
    signal_stability_max_seconds: float = 6.0   # window when combined strength <= weak threshold
    signal_stability_strong_threshold: float = 75.0
    signal_stability_weak_threshold: float = 35.0
    stability_history_max_age_seconds: float = 30.0  # rolling per-engine result history depth the window is proven against

    # ------------------------------------------------------------------ Decision Engine — Stage-1 gate toggles
    # Disable a mandatory gate by flipping it to false — no code change needed. The four
    # extra gates below (structure/breakout/strength/option) are OFF by default, exactly
    # matching the original behaviour where they contribute to scoring but never veto.
    require_trend: bool = True                  # trend direction != NEUTRAL and score >= trend_threshold
    require_signal_stability: bool = True       # stability.stable must be true
    require_trading_rules: bool = True          # rules.allowed must be true
    require_risk: bool = True                   # risk.safe_to_trade must be true
    require_support_resistance: bool = True     # room to level >= min_resistance_distance
    require_volatility: bool = True             # volatility.acceptable must be true
    require_market_structure: bool = False      # structure direction must agree with trend direction
    require_breakout: bool = False              # breakout/breakdown must be confirmed in trend direction
    require_market_strength: bool = False       # strength direction must agree with trend direction
    require_option_selection: bool = False      # a tradable strike must exist on the trade side

    # ------------------------------------------------------------------ Decision Engine — Stage-2 thresholds
    decision_score_threshold: float = 85.0      # min buy/sell score to act (fallback when the two below are unset)
    min_buy_score: float | None = None          # overrides decision_score_threshold for BUY when set
    min_sell_score: float | None = None         # overrides decision_score_threshold for SELL when set
    min_confidence: float = 0.0                 # final confidence must reach this or action -> HOLD (0 = disabled)
    min_trade_quality: float = 0.0              # trade_quality_score must reach this or action -> HOLD (0 = disabled)
    min_score_difference: float = 0.0           # winning score must beat the other side by this margin (0 = disabled)
    min_engine_agreement: int = 0               # min directional engines agreeing with the action (0 = disabled, max 4)

    # ------------------------------------------------------------------ Decision Engine — trade quality (analytics)
    quality_stability_bonus_cap: float = 10.0   # max bonus for confirming faster than required
    quality_liquidity_bonus_scale: float = 0.1  # avg liquidity score x this added to quality
    quality_spread_bonus_scale: float = 0.1     # avg spread score x this added to quality

    # ------------------------------------------------------------------ Position sizing
    default_lots: int = 1

    # ------------------------------------------------------------------ Orchestrator operational tuning
    exit_retry_min_interval_seconds: float = 2.0   # min gap between retries of a failing exit order
    exit_retry_escalation_threshold: int = 5       # exit failures before escalating to a critical alert
    engine_failure_escalation_threshold: int = 3   # consecutive engine crashes before suppressing new entries
    status_log_interval_seconds: float = 15.0      # console heartbeat cadence while HOLD/REJECT-ing
    config_reload_check_seconds: float = 5.0       # how often the config file's mtime is checked (0 = disable live reload)
    diagnostics_enabled: bool = True               # per-cycle engine diagnostics in the events JSONL
    dashboard_refresh_seconds: float = 1.0         # console dashboard redraw throttle (--dashboard)

    # ------------------------------------------------------------------ Stage-2 weights (normalized internally)
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "trend": 0.15,
            "market_structure": 0.15,
            "support_resistance": 0.15,
            "premium_momentum": 0.15,
            "option_selection": 0.05,
            "breakout": 0.15,
            "market_strength": 0.10,
            "volatility": 0.05,
            "trading_rules": 0.025,
            "risk": 0.025,
        }
    )

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path | str | None = None, profile: str | None = None) -> "StrategyConfig":
        """Load defaults <- strategy.json <- profiles/<active_profile>.json.

        `profile` (e.g. from the --profile CLI flag) overrides the file's
        `active_profile` key. Unknown keys in either file are ignored with a warning so
        a typo'd parameter never silently configures nothing.
        """
        config_path = Path(path) if path else DEFAULT_STRATEGY_PATH
        merged: dict = {}
        if config_path.exists():
            merged.update(_read_config_file(config_path))

        profile_name = profile if profile is not None else merged.get("active_profile", "")
        if profile_name:
            profile_path = PROFILES_DIR / f"{profile_name}.json"
            if profile_path.exists():
                merged.update(_read_config_file(profile_path))
                merged["active_profile"] = profile_name
                logger.info("Applied strategy profile '%s' (%s)", profile_name, profile_path)
            else:
                available = sorted(p.stem for p in PROFILES_DIR.glob("*.json")) if PROFILES_DIR.exists() else []
                raise FileNotFoundError(
                    f"Strategy profile '{profile_name}' not found at {profile_path} — available profiles: {available}"
                )

        known_fields = {f.name for f in fields(cls)}
        # keys starting with "_" are documentation (e.g. "_comment"), never parameters
        unknown = sorted(k for k in merged if k not in known_fields and not k.startswith("_"))
        if unknown:
            logger.warning("Ignoring unknown config keys (typo?): %s", unknown)
        filtered = {k: v for k, v in merged.items() if k in known_fields}
        return cls(**filtered)


def _read_config_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object at the top level")
    return data


def config_files_mtime(path: Path | str | None = None) -> float:
    """Latest modification time across strategy.json and every profile file — the
    Orchestrator compares this between cycles to detect edits for live reload.
    """
    paths = [Path(path) if path else DEFAULT_STRATEGY_PATH]
    if PROFILES_DIR.exists():
        paths.extend(PROFILES_DIR.glob("*.json"))
    return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)


@dataclass(frozen=True)
class BrokerCredentials:
    api_key: str
    totp_secret: str


def load_broker_credentials() -> BrokerCredentials:
    """Env vars first, then repo-root ai_config.json — never hardcoded. See docs/DESIGN.md §12."""
    api_key = os.environ.get("GROWW_API_KEY")
    totp_secret = os.environ.get("GROWW_TOTP_SECRET")
    if not api_key or not totp_secret:
        ai_config_path = REPO_ROOT / "ai_config.json"
        if ai_config_path.exists():
            with open(ai_config_path, "r", encoding="utf-8") as fh:
                ai_config = json.load(fh)
            api_key = api_key or ai_config.get("groww_api_key")
            totp_secret = totp_secret or ai_config.get("groww_totp_secret")
    if not api_key or not totp_secret:
        raise RuntimeError(
            "Groww credentials not found: set GROWW_API_KEY/GROWW_TOTP_SECRET env vars "
            "or populate groww_api_key/groww_totp_secret in ai_config.json"
        )
    return BrokerCredentials(api_key=api_key, totp_secret=totp_secret)
