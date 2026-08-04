# Strategy Configuration Reference

Every strategy tunable in the Trading Decision Engine lives in JSON — **no threshold, weight,
or limit requires a Python change**. This document is the complete parameter reference.

## How loading works

```
built-in defaults  <-  config/strategy.json  <-  config/profiles/<active_profile>.json
```

Later sources win. A profile only needs the keys it changes; everything else falls through.

**Activating a profile** (either way, one line, no code):
- set `"active_profile": "balanced"` in `strategy.json`, or
- pass `--profile balanced` on the command line (overrides the file).

Shipped profiles: `aggressive`, `balanced`, `conservative`, `scalping` — drop any custom
`<name>.json` into `config/profiles/` and it's immediately usable.

**Live reload**: while the bot is running (live, shadow, or replay), edits to
`strategy.json` or any profile file are detected within `config_reload_check_seconds`
(default 5s) and applied on the next tick — open trades, session counters, and rolling
histories are untouched; only thresholds/weights swap. A broken JSON edit is rejected
with an error log and the previous config stays live. Manual trigger:
`orchestrator.reload_strategy()`.

Keys starting with `_` (e.g. `_comment`) are documentation and ignored. Unknown keys log
a warning so a typo never silently configures nothing.

---

## Parameters

### Data cadence
| Key | Default | Meaning |
|---|---|---|
| `option_chain_refresh_seconds` | 3.0 | Bounded option-chain pull interval (no push feed exists) |
| `candle_interval` | "1minute" | Candle interval backing Trend/S-R/Structure engines |

### Trend Engine
| Key | Default | Meaning |
|---|---|---|
| `trend_threshold` | 60.0 | **Stage-1 gate**: min trend score to consider bullish/bearish |
| `trend_ehma_length` | 16 | EHMA period (the Pine indicator's `length`) |
| `trend_ema_long_length` | 100 | Confirmation EMA period |
| `trend_angle_lookback_bars` | 5 | Bars over which EHMA slope/angle is measured |
| `trend_angle_scale` | 300.0 | Slope-% multiplier before atan(); higher = small moves read steeper |
| `trend_min_angle` | 0.0 | Direction forced NEUTRAL when \|angle\| below this (0 = off) |
| `trend_confidence_ema_agrees` | 85.0 | Confidence when price-vs-EMA agrees with EHMA direction |
| `trend_confidence_ema_disagrees` | 40.0 | Confidence when it disagrees |
| `trend_confidence_ema_unavailable` | 55.0 | Confidence when too little history for the long EMA |
| `trend_score_strength_weight` | 0.7 | score = strength×this + confidence×(next) |
| `trend_score_confidence_weight` | 0.3 | (the two weights should sum to 1.0) |

> Calibration note: with the default `trend_angle_scale`/`trend_threshold`, score 60 needs
> a very fast index move. On 1-min NIFTY data, real scores top out near 50 — the shipped
> profiles use `trend_threshold` 25–45.

### Market Structure Engine
| Key | Default | Meaning |
|---|---|---|
| `structure_swing_left` / `structure_swing_right` | 3 / 3 | Swing-pivot sensitivity (left lookback / right confirmation delay) |
| `structure_min_candles` | 30 | History required before classifying |
| `structure_exhaustion_threshold` | 40.0 | Min divergence strength to flag EXHAUSTION |
| `structure_double_tolerance_pct` | 0.15 | Swing-height tolerance (%) for double top/bottom. **0.15% of NIFTY ≈ 36 pts — too wide for 1-min charts; profiles use 0.03** |
| `structure_compression_lookback` | 20 | Candles per window in the range-regime comparison |
| `structure_compression_ratio` | 0.6 | recent/prior range at or below = COMPRESSION |
| `structure_expansion_ratio` | 1.6 | recent/prior range at or above = EXPANSION |
| `structure_min_strength` | 0.0 | Weak structures give no directional call below this (0 = off) |

### Support / Resistance Engine
| Key | Default | Meaning |
|---|---|---|
| `sr_pivot_left` / `sr_pivot_right` | 33 / 21 | Pine `left`/`right` pivot windows |
| `sr_quick_pivot_right` | 3 | Pine `quick_right` |
| `min_resistance_distance` | 15.0 | **Stage-1 gate**: min points of room toward the level ahead (used for support room too, on bearish setups) |
| `sr_breakout_buffer_points` | 0.0 | Spot must clear the level by this much extra before breakout/breakdown flags fire |

The 14-level `valuewhen` scheme is fixed — it is the ported Pine indicator's definition,
not a tunable.

### Premium Momentum Engine
| Key | Default | Meaning |
|---|---|---|
| `premium_momentum_min_samples` | 6 | Min premium ticks in the rolling window before reporting |
| `premium_velocity_scale` | 40.0 | Velocity (pts/sec) that maps to score 100. **40 is unreachable on real tape; profiles use 0.4–0.5** |
| `momentum_threshold` | 0.05 | Min \|velocity\| to call a direction vs NEUTRAL |
| `momentum_min_acceleration` | 0.0 | Direction dropped if decelerating harder than this against itself (0 = off) |
| `momentum_min_consistency` | 0.0 | Direction dropped below this consistency % (0 = off) |

### Option Selection Engine
| Key | Default | Meaning |
|---|---|---|
| `premium_min` / `premium_max` | 60 / 250 | Tradable premium range (also settable via CLI/interactive prompt) |
| `liquidity_min_oi` / `liquidity_min_volume` | 50000 / 10000 | Liquidity floors per strike |
| `max_spread_pct` | 2.0 | Max bid/ask spread (% of premium) considered liquid |
| `option_min_liquidity_score` | 0.0 | Hard floor on candidate liquidity score (0 = off) |
| `option_min_spread_score` | 0.0 | Hard floor on candidate spread score (0 = off) |
| `option_liquidity_weight` | 0.5 | Candidate ranking = liquidity×this + spread×(1−this) |

### Breakout Engine
| Key | Default | Meaning |
|---|---|---|
| `breakout_confirmation_bars` | 2 | Consecutive closes beyond the level to confirm |
| `breakout_buffer_points` | 0.0 | Closes must clear the level by this much extra |

### Market Strength Engine
| Key | Default | Meaning |
|---|---|---|
| `market_strength_window` | 10 | Candle window vs the prior window of the same size |
| `market_strength_consolidation_threshold` | 60.0 | Consolidation score at or above = "consolidating" |

### Volatility Engine
| Key | Default | Meaning |
|---|---|---|
| `volatility_min_candles` | 20 | History required before judging |
| `volatility_range_lookback` | 15 | Candles in the average-range window |
| `volatility_spike_multiplier` | 2.5 | Last range > this × avg = spike violation |
| `volatility_gap_multiplier` | 1.5 | Open gap > this × avg range = gap violation |
| `volatility_abnormal_multiplier` | 2.0 | Recent avg range > this × longer avg = violation |
| `volatility_whipsaw_window` | 6 | Candles scanned for direction reversals |
| `volatility_whipsaw_min_reversals` | 4 | Reversals in that window = whipsaw violation |
| `volatility_violation_penalty` | 25.0 | score = 100 − penalty × violations |

`acceptable` (the **Stage-1 gate**) is true only at zero violations; `max_spread_pct`
above is the fifth check.

### Trading Rules Engine (session discipline)
| Key | Default | Meaning |
|---|---|---|
| `max_trades_per_day` | 6 | Hard daily trade cap |
| `cooldown_seconds` | 20 | Signals ignored after every exit |
| `consecutive_loss_limit` | 3 | Stop after N losses in a row |
| `daily_loss_limit` | 5000.0 | ₹ — blocks new entries AND force-exits an open trade |
| `daily_profit_lock` | 10000.0 | ₹ — stop trading once daily profit reaches this |
| `max_exposure` | 100000.0 | ₹ — max capital deployed at once |
| `expiry_day_cutoff_hour` | 14 | No expiry-day entries after this hour |
| `market_close_buffer_minutes` | 15 | No entries / force exit inside this window before 15:30 |
| `wait_after_open_minutes` | 5 | WAIT_MODE duration after 09:15 |

### Risk Engine (operational safety)
| Key | Default | Meaning |
|---|---|---|
| `risk_min_margin_available` | 0.0 | Margin must exceed this ₹ floor for safe_to_trade |

### Signal Stability Engine (adaptive window)
| Key | Default | Meaning |
|---|---|---|
| `signal_stability_min_seconds` | 1.5 | Window when combined trend+momentum strength ≥ strong threshold |
| `signal_stability_max_seconds` | 6.0 | Window when strength ≤ weak threshold |
| `signal_stability_base_seconds` | 3.0 | Documented reference point (effective window is adaptive) |
| `signal_stability_strong_threshold` | 75.0 | Strength band selecting the min window |
| `signal_stability_weak_threshold` | 35.0 | Strength band selecting the max window |
| `stability_history_max_age_seconds` | 30.0 | Rolling engine-result history depth the window is proven against |

### Decision Engine — Stage-1 gate toggles
Each mandatory check can be switched off (or the four optional ones on) independently:

| Key | Default | Gate |
|---|---|---|
| `require_trend` | true | Trend direction ≠ NEUTRAL and score ≥ `trend_threshold` |
| `require_signal_stability` | true | Stability held for the full adaptive window |
| `require_trading_rules` | true | All session-discipline rules clear |
| `require_risk` | true | Operationally safe |
| `require_support_resistance` | true | Room ≥ `min_resistance_distance` |
| `require_volatility` | true | Zero volatility violations |
| `require_market_structure` | **false** | Structure direction must agree with trend |
| `require_breakout` | **false** | Breakout/breakdown confirmed in trend direction |
| `require_market_strength` | **false** | Strength direction must agree with trend |
| `require_option_selection` | **false** | A tradable strike must exist on the trade side |

Defaults exactly reproduce the original behaviour (six gates on, four extras scoring-only).

### Decision Engine — Stage-2 thresholds
| Key | Default | Meaning |
|---|---|---|
| `decision_score_threshold` | 85.0 | Min buy/sell score to act (shared fallback) |
| `min_buy_score` | null | Overrides the shared threshold for BUY when set |
| `min_sell_score` | null | Overrides the shared threshold for SELL when set |
| `min_confidence` | 0.0 | Final confidence below this → HOLD (0 = off) |
| `min_trade_quality` | 0.0 | trade_quality_score below this → HOLD (0 = off) |
| `min_score_difference` | 0.0 | Winning side must beat the other by this margin (0 = off) |
| `min_engine_agreement` | 0 | Min directional engines (of 4) agreeing with the action (0 = off) |
| `quality_stability_bonus_cap` | 10.0 | Max quality bonus for faster-than-required confirmation |
| `quality_liquidity_bonus_scale` | 0.1 | Liquidity contribution to quality |
| `quality_spread_bonus_scale` | 0.1 | Spread contribution to quality |

### Stage-2 weights
`weights` — per-engine contribution to buy/sell scoring, normalized internally over the
five scoring dimensions (market_structure, premium_momentum, breakout, market_strength,
option_selection). The trend/S-R/volatility/rules/risk entries are reserved for future
scoring use; changing any weight is config-only.

### Position sizing
| Key | Default | Meaning |
|---|---|---|
| `default_lots` | 1 | FixedLotSizingStrategy output (also settable via CLI/interactive prompt) |

### Orchestrator operational tuning
| Key | Default | Meaning |
|---|---|---|
| `exit_retry_min_interval_seconds` | 2.0 | Min gap between retries of a failing exit order |
| `exit_retry_escalation_threshold` | 5 | Exit failures before a critical alert |
| `engine_failure_escalation_threshold` | 3 | Consecutive engine crashes before entries are suppressed |
| `status_log_interval_seconds` | 15.0 | Console heartbeat cadence while holding/rejecting |
| `config_reload_check_seconds` | 5.0 | Config mtime poll cadence (0 = disable live reload) |
| `diagnostics_enabled` | true | Per-cycle engine diagnostics in the events JSONL |
| `dashboard_refresh_seconds` | 1.0 | Console dashboard redraw throttle (`--dashboard`, default on for live/shadow TTY) |

---

## Diagnostics & tuning workflow

With `diagnostics_enabled`, every decision/rejected event in `logs/events_YYYY-MM-DD.jsonl`
carries a `diagnostics` object: per engine — `score`, `requirement`, `gate_enabled`,
`passed`, `weight`, `contribution` — plus Stage-2 actual-vs-required for buy/sell score,
confidence, and trade quality. Rejection `reasons` always state **actual vs required**
(e.g. `"Trend not confirmed: score 40 vs required 60"`).

Tuning loop (no code, ever):
1. Run replay or shadow mode with a profile.
2. Aggregate the JSONL: count rejections per failed gate, distribution of each engine's
   scores, trades/day, win rate from `trade_closed` events.
3. Edit the profile JSON (thresholds live-reload even mid-session).
4. Repeat until trade frequency/quality is where you want it (~10–15/day target →
   start from the `scalping` or `aggressive` profile and tighten).
