# Signal & Analyzer Bots (7) — Rebuild Specs

> Part of the end-to-end rebuild documentation. Master document: ../../REBUILD_BLUEPRINT.md
> Generated 2026-08-04 from a full code survey. Treat all constants, filenames,
> JSON keys and printed strings here as EXACT contracts.

---


---

# 1. MASTER_SIGNAL_BOT.py

`/Users/ayush/Documents/eclipse-workspace/Groww_Python_Trading_Bot-main/MASTER_SIGNAL_BOT.py` (896 lines)

### 1. Purpose / order placement
Single-verdict directional signal engine: emits exactly one of `ENTER CE` / `ENTER PE` / `NO TRADE` for NIFTY or SENSEX by fusing 1h + 15m Fibonacci position, 5m candle consensus, and live ATM CE/PE premium flow into a weighted confidence score. **Read-only — never places orders.** Its per-cycle JSONL log is the canonical machine-readable signal feed consumed by `CHART_LEVEL_ANALYZER`, `SIGNAL_ANALYZER`, and `LIVE_DASHBOARD`.

### 2. Loop cadence & market-hours gating
- `REFRESH_SEC = 60`. Each cycle: fetch → compute → render → countdown `max(0, 60 - elapsed)` printing `Next refresh in Ns`.
- Never sleeps out the market: it **always** computes and logs, but forces `direction = "WAIT"` outside the trade window.
- Gates by minutes-since-midnight (`now.hour*60 + now.minute`):
  - `< NO_TRADE_BEFORE_MIN (570 = 09:30)` → `no_trade_reason = "Before 9:30 AM — opening noise, wait"`
  - `>= NO_TRADE_AFTER_MIN (900 = 15:00)` → `"After 3:00 PM — no new positions"`
- On exception: print, `sleep(15)`, continue. On missing spot: `sleep(15)`, continue.

### 3. Data sources
Auth: `groww_token.get_access_token(API_KEY, TOTP_SECRET)` → `GrowwAPI(token)`. Hardcoded `API_KEY` (JWT) + `TOTP_SECRET = "SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4"` at top of file.

| What | Source | Details |
|---|---|---|
| Instruments | `GET https://growwapi-assets.groww.in/instruments/instrument.csv` | Written to `instrument.csv`; re-downloaded if missing or mtime > 1 day |
| Spot | `GET https://api.groww.in/v1/option-chain/exchange/{NSE\|BSE}/underlying/{INDEX}?expiry_date={YYYY-MM-DD}` → `payload.underlying_ltp` | timeout 8s; fallback = `c15m[-1].close` |
| ATM CE+PE LTP | `GET https://api.groww.in/v1/live-data/ltp?segment=FNO&exchange_symbols={EX}_{TS1}&exchange_symbols={EX}_{TS2}` → `payload[sym]` | one call for both legs, timeout 5s |
| Candles | `groww_client.get_historical_candles(groww_symbol, exchange, segment="CASH", start_time, end_time, candle_interval)` | rows `[ts_ms, o, h, l, c]` → dicts `{ts,open,high,low,close}` |

Headers on all raw REST calls: `Accept: application/json`, `Authorization: Bearer {token}`, `X-API-VERSION: 1.0`.

Timeframes fetched per cycle: `1hour` / 48h back, `15minute` / 26h back, `5minute` / 8h back. Symbol candidates tried in order: NIFTY `["NSE-NIFTY 50","NSE-NIFTY"]`, SENSEX `["BSE-SENSEX","BSE-S&P BSE SENSEX"]`. Accepted only if `len(candles) >= 5`.

Expiry: nearest `expiry_date >= today` among rows where `underlying_symbol == INDEX` (also returns next).

### 4. Core algorithm

**RSI (14, simple mean, not Wilder):**
```
gains[i]=max(c[i]-c[i-1],0); losses[i]=max(c[i-1]-c[i],0)
ag=mean(gains[-14:]); al=mean(losses[-14:])
rsi = round(100 - 100/(1+ag/al),1)   # 100.0 if al==0; 50.0 if len<15
```

**Swing detection** `detect_swing(candles, window)` — strict local extremum vs all neighbours in `[i-w, i+w]`, `w=3` for 1h, `w=2` for 15m. Returns `(last swing high, last swing low)`; fallback = max/min of last 20 candles; if `len < 2w+1` → global max/min.

**Fibonacci position score** `fib_score(price, sl, sh)` → integer:
| condition | score |
|---|---|
| `price >= sh` | **+3** (breakout) |
| `pos >= 0.786` | +2 |
| `pos >= 0.618` | +1 |
| `pos >= 0.500` | 0 |
| `pos >= 0.382` | −1 (**GOLDEN ZONE**) |
| `pos >= 0.236` | −2 |
| else / `price <= sl` | **−3** |

where `pos = (price - sl)/(sh - sl)`; returns 0 if `sh <= sl`.

`fib_zone_name()` returns the human string used for tuning exclusions and the log `zone` key: `"BREAKOUT ↑ above swing high"`, `"BREAKDOWN ↓ below swing low"`, `"78.6–100%  shallow retrace"`, `"61.8–78.6%  normal retrace"`, `"50–61.8%  mid range"`, `"38.2–50%  GOLDEN ZONE ★"`, `"23.6–38.2%  deep retrace"`, `"0–23.6%  near swing low"`, `"—"`.

`fib_levels(sl, sh)` → dict keyed `["SL","23.6%","38.2%","50%","61.8%","78.6%","SH"]` at ratios `[0.0,0.236,0.382,0.500,0.618,0.786,1.0]`, `round(sl + rng*r, 1)`.

**RSI dampening** (applied to both s1h and s15m):
```
if rsi >= 75 and s > 0: s = max(s-1, 0)
if rsi <= 25 and s < 0: s = min(s+1, 0)
```

**15m candle pattern** `candle_pattern()` → `(signal ∈ {-1,0,1}, name)`, checked in order: `Bullish Engulfing`(+1), `Bearish Engulfing`(−1), `Hammer`(+1, lower wick > 2×body, upper < body, body/range > 0.1), `Shooting Star`(−1), `Doji`(0, body/range < 0.1), `Strong Bull`(+1, body/range > 0.65), `Strong Bear`(−1), `Normal`(0). Engulf requires `body > prev_body`. Applied as `s15m = clamp(s15m + pat_sig*0.5, -3, 3)` (s15m becomes a float).

**5m consensus** `score_5m()` on last 8 candles (need ≥4): `bulls >= 0.75n → +2`, `>= 0.60n → +1`, `bears >= 0.75n → −2`, `>= 0.60n → −1`, else 0. Note text e.g. `"6/8 bull candles"`, `"mixed  4B / 4A"`.

**Premium flow** `premium_flow(ce_prev, pe_prev, ce_curr, pe_curr)`: `ce_d>0 and ce_d>pe_d → +1`; `pe_d>0 and pe_d>ce_d → −1`; else 0. First cycle → `(0, "— (first poll)")`.

**Weighted decision** `compute_signal(s1h, s15m, s5m, sprem)`:
```
W_1H=0.35  W_15M=0.40  W_5M=0.15  W_PREM=0.10
MAX_TOTAL = 3*0.35 + 3*0.40 + 2*0.15 + 1*0.10 = 2.65

# hard conflict veto FIRST:
if s1h <= -2 and s15m >= 2: return ("WAIT", 0.0, 0.0)
if s1h >=  2 and s15m <= -2: return ("WAIT", 0.0, 0.0)

raw  = s1h*.35 + s15m*.40 + s5m*.15 + sprem*.10
conf = round(abs(raw)/2.65*100, 1)
dir  = "CE" if raw > 0 else "PE"
conf = round(conf * (ce_multiplier if dir=="CE" else pe_multiplier), 1)
if conf < tuning_confidence_threshold(): return ("WAIT", conf, raw)
```
Default `CONFIDENCE_THRESHOLD = 65`.

**Tuning gates** (applied after decision, before time gates): if `zone_name` contains any `excluded_zones` entry (case-insensitive substring) → `WAIT` + reason `"Zone '<zone>' blocked by analyzer"`. Same for `excluded_patterns` vs `pat_name` → `"Pattern '<pat>' blocked by analyzer"`.

**No-trade reasons** (precedence): tuning block → before 09:30 → after 15:00 → `raw == 0.0` → `"1h and 15m in direct conflict — wait for alignment"` → else `"Confidence {c:.0f}% below {thr}% — signals not aligned"`.

**Entry/stop/target** `entry_levels(spot, lvls, direction)`: nearest fib level below spot = support (default `spot-100`), nearest above = resistance (default `spot+100`). CE → `(stop=sup, target=res)`; PE → `(stop=res, target=sup)`. `rr = round(|target-spot| / |spot-stop|, 1)`. When `direction=="WAIT"`, levels are still computed with `eff_dir="CE"`. R:R display flags `>= 1.5` as `"✅ Good R:R"`.

### 5. OUTPUT CONTRACT

**No `.master_signal.json` exists.** The output is a JSONL log file:

`logs/master_signal/Master_Signal_{YYYY-MM-DD_HH-MM-SS}.log` — created at startup (`os.makedirs(exist_ok=True)`, opened `"a", buffering=1`), **one JSON object per line, one line per 60s cycle**, written every cycle including WAIT.

```json
{
  "ts":         "2026-07-31T12:50:38",
  "index":      "NIFTY",
  "spot":       24409.45,
  "direction":  "WAIT",
  "confidence": 0.0,
  "s1h":        -2,
  "s15m":       2.0,
  "s5m":        2,
  "sprem":      -1,
  "rsi1h":      78.6,
  "rsi15m":     85.6,
  "pattern":    "Normal",
  "zone":       "BREAKOUT \u2191 above swing high",
  "stop":       24375.0,
  "target":     24509.5,
  "rr":         2.9,
  "sh15m":      24374.95,
  "sl15m":      24334.15
}
```
Key types/domains: `direction ∈ {"CE","PE","WAIT"}`; `confidence` float 0–100 (1dp); `s1h`/`s5m`/`sprem` ints; `s15m` float (2dp, pattern-adjusted); `rsi*` float 1dp; `pattern` from the 8-name set; `zone` from the 9-string set; `spot`/`stop`/`target`/`sh15m`/`sl15m` rounded to 2dp; `rr` float 1dp.

**Consumers (keys must not change):**
- `CHART_LEVEL_ANALYZER._read_master_signal()` — scans 3 newest `Master_Signal_*.log`, reads lines in reverse, matches `index`, rejects if `ts` older than **300s**, uses `direction` + `confidence`.
- `SIGNAL_ANALYZER.parse_master_signal_logs()` — 7-day window; requires `ts`, `direction`, `spot`; also uses `confidence`, `zone`, `pattern`.

### 6. Config knobs
Top-of-file constants:
```python
REFRESH_SEC          = 60
CONFIDENCE_THRESHOLD = 65
NO_TRADE_BEFORE_MIN  = 9*60+30     # 570
NO_TRADE_AFTER_MIN   = 15*60       # 900
W_1H, W_15M, W_5M, W_PREM = 0.35, 0.40, 0.15, 0.10
MAX_TOTAL = 2.65
INDICES = {"NIFTY":  {"qty":20,"step":50, "exchange":"NSE","sym_candidates":["NSE-NIFTY 50","NSE-NIFTY"]},
           "SENSEX": {"qty":50,"step":100,"exchange":"BSE","sym_candidates":["BSE-SENSEX","BSE-S&P BSE SENSEX"]}}
FIB_RATIOS = [0.0,0.236,0.382,0.500,0.618,0.786,1.0]
FIB_NAMES  = ["SL","23.6%","38.2%","50%","61.8%","78.6%","SH"]
```
Env vars: `BOT_THEME` (`"1".."4"`), `BOT_INDEX` (`NIFTY`/`SENSEX`) — bypass interactive prompts. 4 hex themes (`Groww Classic`, `Amber Night`, `Ocean Blue`, `Minimal`) with keys `BULLISH BEARISH NEUTRAL BORDER HEADER SPOT_VAL DIM_TEXT GOLDEN_ZONE SCORE_HIGH SCORE_MID SCORE_LOW ACTION_CE ACTION_PE ACTION_WAIT FIB_ABOVE FIB_BELOW FIB_SWING SECTION_HDR`; converted via `\033[1;38;2;{r};{g};{b}m`.

**`BOT_TUNING.json`** (written by `SIGNAL_ANALYZER`, hot-reloaded on mtime change every cycle):
```json
{
  "generated_at": "2026-05-22T14:01:12.610998",
  "generated_by": "SIGNAL_ANALYZER",
  "confidence_threshold": 65,
  "excluded_zones": [],
  "excluded_patterns": [],
  "ce_multiplier": 0.65,
  "pe_multiplier": 1.0,
  "notes": ["DAMPEN CE  confidence ×0.65  (CE win-rate 32.1% < 45%, n=28)"]
}
```

### 7. Phantom 09:00 candle
**Not handled.** Raw candles are used as-is, so a phantom 09:00 index bar with a huge wick will corrupt `detect_swing` → `sh/sl` → `fib_score` and the whole confidence chain. Port `KEY_LEVELS_TERMINAL.filter_spikes(candles, mult=8.0)` (drop bars whose `high-low` > `8 × median(high-low)`) into `fetch_candles()` before indicators.

---

# 2. FIBONACCI_TREND_ANALYZER.py

`/Users/ayush/.../FIBONACCI_TREND_ANALYZER.py` (1886 lines)

### 1. Purpose / order placement
Full-screen terminal Fibonacci trend dashboard for NIFTY/SENSEX/BANKNIFTY/FINNIFTY. Builds three fib grids (day-session H→L, current 15m swing pair, 1h swing pair), finds cross-grid confluence zones, scores price position, and prints a plain-English trade setup plus step-by-step TradingView/Kite drawing instructions. **Strictly read-only — no orders** (banner literally says "read-only"); its only side effects are WhatsApp/Telegram alerts and a text log.

### 2. Loop cadence & market-hours gating
- `FIBO_CONFIG["REFRESH_SEC"] = 90`, unconditional `time.sleep(90)` at the end of every iteration.
- **No gating** — it runs and re-renders 24/7. Market state is informational only:
  - `is_market_open()` compares `datetime.now()` to `MARKET_OPEN "09:15"` / `MARKET_CLOSE "15:30"` (same-day replace); header shows `OPEN` / `closed`.
  - **Frozen-price detector** substitutes for a holiday gate: `_recent_spots = deque(maxlen=4)`; `is_price_frozen()` is True when ≥3 samples (`_FROZEN_CYCLES=3`) span ≤ `_FROZEN_TOLERANCE = 5.0` pts. When frozen: header shows `CLOSED/HOLIDAY` and **level alerts are suppressed** (`alerts = []`).
- Exceptions are caught, `traceback.print_exc()`, loop continues.

### 3. Data sources
Auth via `groww_token.get_access_token(API_KEY, TOTP_SECRET)`. Alerts via `whatsapp_gateway.send_whatsapp` (wrapped by `send_telegram()` honouring `TELEGRAM_ALERTS`).

- Instruments: `https://growwapi-assets.groww.in/instruments/instrument.csv` → `instrument.csv`; download if missing or > 1 day old; in-memory cache `_INSTRUMENTS_RELOAD_HOURS = 6`.
- Spot: `GET https://api.groww.in/v1/option-chain/exchange/{NSE|BSE}/underlying/{INDEX}?expiry_date={expiry}` → `payload.underlying_ltp` (timeout 6s). Expiry auto-resolved from CSV each call.
- Candles: `get_historical_candles(segment="CASH", candle_interval=...)`. Two fetches per cycle: `15minute` × `LOOKBACK_15M_HRS=26`, `1hour` × `LOOKBACK_1H_HRS=26`. Accepted at `len >= 5`. Symbol candidates: NIFTY `["NSE-NIFTY 50","NSE-NIFTY","NSE-Nifty 50"]`, SENSEX `["BSE-SENSEX","BSE-S&P BSE SENSEX"]`, BANKNIFTY `["NSE-NIFTY BANK","NSE-BANKNIFTY"]`, FINNIFTY `["NSE-NIFTY FIN SERVICE","NSE-FINNIFTY"]`. **Note:** `'60minute'` is invalid — must use `'1hour'`.
- Fallback: `build_synthetic_candles(ltp_buffer, candle_minutes=15)` aggregates the LTP poll buffer (`deque(maxlen=300)`, 1 sample/cycle) into pseudo-OHLC when the CASH feed returns nothing and ≥10 samples exist.

### 4. Core algorithm

**Fib constants:** retrace `[(0.236,"R23.6%"),(0.382,"R38.2%"),(0.500,"R50.0%"),(0.618,"R61.8%"),(0.786,"R78.6%")]`, extension `[(1.272,"E127.2%"),(1.618,"E161.8%"),(2.618,"E261.8%")]`.

**`calc_fib_levels(swing_low, swing_high, is_bullish_swing)`** → `None` if `high <= low`, else dict with `SWING_HIGH`, `SWING_LOW`, `_range`, `_bullish` + levels:
- bullish: `R* = round(high - rng*r, 2)`, `E* = round(low + rng*r, 2)`
- bearish: `R* = round(low + rng*r, 2)`, `E* = round(high - rng*r, 2)`

**`calc_day_fib(c15m)`** — filters candles to today (epoch-ms `ts/1000`), needs ≥2; `day_high/day_low`; `bullish = (low_idx < high_idx)`; adds `_day_high`, `_day_low`, `_day_bullish`.

**`detect_swings(candles, window)`** — swing HIGH when `c.high == max(high in [i-w, i+w])`; `elif` swing LOW when `c.low == min(low in [i-w, i+w])`; skips zero-range bars; returns strictly **alternating** H/L list, replacing consecutive same-type with the more extreme. `SWING_WINDOW_15M = 2`, `SWING_WINDOW_1H = 3`.

**`find_relevant_swing_pair(swings, spot)`** — Pass 1: newest pair whose `[lo,hi]` contains spot. Pass 2: newest pair with `lo - rng <= spot <= hi + rng`. Fallback: `most_recent_swing_pair`. `second_swing_pair` uses `swings[-3]/[-4]` (needs ≥4) for the secondary grid.

**`find_confluence_zones(fib_dicts, tol_pct=CONFLUENCE_TOL_PCT=0.30)`** — flattens all non-`_`, non-`SWING_*` levels across the 15m / prev-15m / day grids, sorts, greedily clusters everything within `tol_pct%` of the seed price. Emits only clusters of **≥2** with `{price(avg,2dp), count, labels[], min_price, max_price}`, sorted by `count` desc.

**`analyze_position(spot, fib)`** → `(label, score −4..+3, nearest_sup, nearest_res)`. `retrace_pct = (sh-spot)/rng*100` if bullish else `(spot-sl)/rng*100`.
Bullish: `spot>sh → +3` `"ABOVE SWING HIGH"`; `<23.6% → +2`; `<38.2% → +1`; `<50% → 0`; `<61.8% → −1` `"GOLDEN ZONE 50-61.8% ★"`; `<78.6% → −2`; `<=100% → −3`; else `−4` `"BROKEN — BELOW SWING LOW"`.
Bearish (mirrored): `spot<sl → −3`; `<23.6% → −2`; `<38.2% → −1`; `<50% → 0`; `<61.8% → +1` `"GOLDEN BOUNCE ★"`; `<78.6% → +2`; else `+3`.

**RSI(14) — Wilder smoothing** via numpy: seed `mean(gain[:14])`/`mean(loss[:14])`, then `avg = (avg*13 + x)/14`; returns `100.0` if `avg_l==0`, `None` if `len < 15`. Notes: `>=70 "OVERBOUGHT ⚠️"`, `<=30 "OVERSOLD ⚠️"`, `>=55 "bullish zone"`, `<=45 "bearish zone"`, else `"neutral"`.

**`detect_pattern(last 3 candles)`** → one of `"HAMMER 🔨  (bullish reversal)"`, `"SHOOTING STAR ⭐ (bearish reversal)"`, `"BULL ENGULFING 🐂 (strong bullish)"`, `"BEAR ENGULFING 🐻 (strong bearish)"`, `"DOJI ⚖️  (indecision)"`, `"STRONG BULL BAR ↑"`, `"STRONG BEAR BAR ↓"`, `"NONE"`. Hammer needs `wick_l >= 2×body and wick_u <= 0.6×body and c2,c1 both bearish`; strong bars need `body/range > 0.70`; doji `body/range < 0.08`.

**`get_final_bias(score_15m, pattern)`**: `raw = score_15m*0.90 + PATTERN_SCORE[pattern]*0.10`, clamped/rounded to −3..+3. `PATTERN_SCORE`: Hammer +1.0, BullEngulf +1.5, StrongBull +0.5, ShootingStar −1.0, BearEngulf −1.5, StrongBear −0.5, Doji/NONE 0.0.

**`predict_next_move(spot, bias, fib)`** — direction `UP` if `bias>=1`, `DOWN` if `<=-1`, else `RANGE`. Probabilities: recover-to-swing-high 0.60; reach-swing-low 0.58; counter-trend retrace target 0.52; extension continuation 0.48/0.42; range 0.45. Returns `{direction, target, distance, probability, label}`.

**Decision surface** (`_setup_block`, uses `combined = score1h + score15m`):
| combined | signal |
|---|---|
| `>= 4` | `STRONG CE  ✅` |
| `<= -4` | `STRONG PE  ✅` |
| `>= 2` | `CE  (good setup)` |
| `<= -2` | `PE  (good setup)` |
| `== 1` | `LEAN CE — wait for candle confirm` |
| `== -1` | `LEAN PE — wait for candle confirm` |
| `0` | `NO TRADE — timeframes conflict` |

For actionable setups it prints Entry / Target / SL / R:R. Bearish: entry = lowest fib resistance above spot (from `R23.6%,R38.2%,R61.8%,day_high`), target = `day_low` **unless `spot - sl < 20`** in which case target = `E127.2%`, SL = next level above entry. Bullish mirrored (`sh - spot < 20` → extension target). `R:R = |target-ref| / max(|sl-ref|,1)`.

`_hr1_line(sc1h)`: `>=2 "⬆ BULLISH → TRADE CE SIDE"`, `<=-2 "⬇ BEARISH → TRADE PE SIDE"`, `1 "↗ MILD BULLISH → LEAN CE (wait for 15m confirm)"`, `-1 mirrored`, `0 "→ NEUTRAL → BOTH SIDES — wait for clarity"`.

**Alerts** `_check_level_alerts()`: fires when `|spot-price|/price*100 <= NEAR_LEVEL_PCT (0.20)` for any level in the 15m/prev/day grids, per-key cooldown `_ALERT_COOLDOWN_SEC = 300`, key `f"{label}_{price:.0f}"`. Also fires a Telegram message on any change of `bias_score` between cycles.

Display proximity tags: `< 0.10%` → `◄◄ HERE`, `< 0.25%` → `◄ NEAR`; golden star on `R50.0%`/`R61.8%`.

### 5. OUTPUT CONTRACT

**There is no `.fibo_trend.json`.** (Verified: zero `json.dump`/`json.dumps` calls in the file.) The sole persistent artifact:

`logs/fibo_analyzer/Fibo_Analyzer_{YYYY-MM-DD_HH-MM-SS}.log` — plain ANSI-stripped text tee. `setup_logger()` monkey-patches `builtins.print` so every `print()` goes to `sys.__stdout__` with colour **and** to the file with `\033[...]` stripped by `re.compile(r'\033\[[0-9;]*[mKHFABCDEFGJRSTihlnpu]')`. It does **not** replace `sys.stdout` (deliberate — that breaks colours through colorama).

`run_analysis()` returns an in-memory dict (never serialised) that a rebuild should keep identical if a JSON export is added:
```python
{"spot": 24409.45, "index": "NIFTY", "ts": datetime,
 "fib15m": {...}, "fib1h": {...}, "fib_day": {...},
 "pair15m": {"swing_low":.., "swing_high":.., "is_bullish":bool, "description":"↑ 24334 → 24375"},
 "pair1h": {...}, "confluence": [{"price":..,"count":..,"labels":[..],"min_price":..,"max_price":..}],
 "pos15m": "GOLDEN ZONE 50-61.8% (55.2%) ★", "score15m": -1, "score1h": -2,
 "sup15m": ("R61.8%", 24350.1), "res15m": ("R38.2%", 24401.4),
 "pattern": "STRONG BULL BAR ↑", "rsi": 85.6, "rsi_note": "OVERBOUGHT ⚠️",
 "bias_score": -1, "bias_label": "↘  MILD BEARISH", "bias_color": "\033[...",
 "prediction": {"direction":"DOWN","target":24334.15,"distance":75.0,"probability":0.52,"label":"-75"},
 "alerts": ["📍 NIFTY @ 24409  is 0.12% from  R38.2% = 24380  (▲ above)"],
 "frozen": False, "src_15m": "LIVE (104c)"}
```

**Consumer:** `SIGNAL_ANALYZER.parse_fibo_logs()` **regex-scrapes this text log** — so these printed strings are a de-facto contract and must not be reworded:
- `🔄 Analysis cycle #N  [HH:MM:SS]` (block delimiter)
- `FIBONACCI ANALYZER  |  {IDX}  |  {YYYY-MM-DD} ...  |  Spot {NNNN}`
- `15m score: {+N}`
- `Pattern  {NAME}`
- `pos: {ZONE}`
- `→   {LEAN }?{CE|PE|WAIT|CONFLICT|NEUTRAL}`

`ANALYZE_BOT.py` also stores derived fields `fibo_signal`, `fibo_trend_bias`, `fibo_pattern` per trade.

### 6. Config knobs
```python
FIBO_CONFIG = {
  "INDEX": "NIFTY",              # NIFTY | SENSEX | BANKNIFTY | FINNIFTY (expiry auto-detected)
  "SWING_WINDOW_15M": 2, "SWING_WINDOW_1H": 3,
  "LOOKBACK_15M_HRS": 26, "LOOKBACK_1H_HRS": 26,
  "NEAR_LEVEL_PCT": 0.20, "CONFLUENCE_TOL_PCT": 0.30,
  "REFRESH_SEC": 90, "TELEGRAM_ALERTS": True,
  "MARKET_OPEN": "09:15", "MARKET_CLOSE": "15:30",
}
_FROZEN_CYCLES = 3; _FROZEN_TOLERANCE = 5.0; _ALERT_COOLDOWN_SEC = 300
_INSTRUMENTS_RELOAD_HOURS = 6
```
Plus a flat 33-key hex `COLOR_CONFIG` (`BIAS_STRONG_BULL`, `FIB_ABOVE`, `FIB_BELOW`, `FIB_SWING`, `GOLDEN_ZONE`, `SPOT_LINE`, `NEAR_HERE`, `NEAR_CLOSE`, `SETUP_*`, `HR1_*`, `DASH_*`, `MARKET_OPEN/CLOSED`, `SECTION_HEADER`, `SRC_LIVE/STALE`, `CONFLUENCE_ABOVE/BELOW`, `SUMMARY_TEXT`, `API_OK`, `STARTUP_BANNER`, `STATUS_DIM`). No config JSON file, no env vars, no CLI args — edit the dict.

### 7. Phantom 09:00 candle
**Not handled.** `calc_day_fib()` takes a raw `max(high)`/`min(low)` over today's candles, so a phantom 09:00 bar with a 450–770 pt fake wick directly inflates `_day_high`/`_day_low`, which corrupts the day fib grid, every confluence zone, the `_auto_summary` "% into day range" line, and the setup targets. This is the single highest-impact place to insert `filter_spikes(candles, 8.0)` — apply it right after `fetch_candles()` returns, before `detect_swings` and `calc_day_fib`.

---

# 3. CHART_LEVEL_ANALYZER.py

`/Users/ayush/.../CHART_LEVEL_ANALYZER.py` (1816 lines)

### 1. Purpose / order placement
Multi-timeframe support/resistance mapper: enumerates ~50–100 candidate levels (prev day/week OHLC, daily/1H/15M swings, standard + Camarilla pivots, opening range, VWAP, round numbers), scores each 0–10 for strength with an efficiency (hold-rate) %, merges confluence clusters, then emits a traffic-light trade gate (`OK`/`CAUTION`/`WAIT`) and a full CE/PE option suggestion with strike, target, SL, and R:R. **Read-only — never places orders**; it plays a macOS sound alarm, sends Telegram, and writes a signals JSONL.

### 2. Loop cadence & market-hours gating
- `CLA_CONFIG["REFRESH_SEC"] = 30`, `time.sleep(30)` at loop end.
- **No gating** — runs continuously. `is_market_open()` (09:15–15:30 same-day compare) only colours the header `OPEN`/`CLOSED`.
- Tiered candle refresh to save quota: **cycle 1 and every 10th cycle** → refetch all four timeframes; **every 3rd cycle** → refetch only 5m with `hours_back=2`; otherwise reuse cached lists.
- Missing spot → `sleep(10)`, `continue`. Exceptions printed, loop continues.

### 3. Data sources
Auth: `groww_token.init_client(API_KEY, TOTP_SECRET)` → `(client, access_token)`. Telegram direct: `POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage` with `BOT_TOKEN = "8666941668:AAEObDodwWqDwdVJVXy8WvFx_lyreq8p7fI"`, `CHAT_ID = "6012308856"` (timeout 4s).

- Instruments: same CSV URL/staleness logic; 6h memory cache.
- Spot: `GET /v1/option-chain/exchange/{NSE|BSE}/underlying/{IDX}?expiry_date={exp}` → `payload.underlying_ltp`, timeout 6s.
- Candles: `get_historical_candles(segment="CASH")`, accepted at `len >= 3`. Four timeframes:

| interval | `hours_back` | ≈ bars | purpose |
|---|---|---|---|
| `5minute` | `LOOKBACK_5M_HRS = 48` | ~100 | pivots, OR, VWAP, momentum |
| `15minute` | `LOOKBACK_15M_HRS = 120` | ~100 | 15M swings, PD levels, round numbers |
| `1hour` | `LOOKBACK_1H_HRS = 480` | ~140 | 1H swings, PWH/PWL |
| `1day` | `LOOKBACK_1D_HRS = 2160` | ~65 | prev day OHLC, prev week, daily swings |

- Option chain: builds `{strike:{CE:row,PE:row}}` from `instrument.csv` for `|strike - ATM| <= step*7` (`step` = 200 SENSEX / 100 BANKNIFTY / else 50), then batch-fetches in **chunks of 20** via `GET /v1/live-data/ltp?segment=FNO&exchange_symbols={comma-joined}`. Result cached **300 s** in `_opt_chain_cache`.
- **Reads MASTER_SIGNAL_BOT**: `logs/master_signal/Master_Signal_*.log` (3 newest files, lines reversed), matching `index`, rejecting records whose `ts` is > **300 s** old.

### 4. Core algorithm

**`analyze_level_strength(level, candles, tol_pct, tf_base_weight)` → `(strength 0–10, efficiency %, touches)`**
```
tol = level * tol_pct / 100
touch  ⇔ c.low <= level+tol AND c.high >= level-tol
rejection: rej = (close-level)/range if close>=level else (level-close)/range
           strong_rejection if rej > 0.45
hold/break: level acted as support if c.close>=level →
              break if next.close < level-tol, else hold
            (mirrored when close<level)
efficiency = holds/(holds+breaks)*100   # 55.0 default when no data

score  = tf_base_weight
       + (3.0 if touches>=6 else 2.0 if >=4 else 1.0 if >=2 else 0.5 if ==1 else 0)
       + min(2.0, strong_rejections * 0.5)
       + (1.0 if eff>=80 else 0.5 if eff>=65 else 0)
       + (0.5 if touched within last 5 candles)
return min(10.0, round(score,1))
```

**Level catalogue** — `build_all_levels()`, each `_add(price,label,tf,ref_candles,tol_pct,tf_weight)`; any level > `MAX_DIST_PCT = 4.0%` from spot is dropped:

| label | tf | ref candles | tol | base wt |
|---|---|---|---|---|
| `PDH`, `PDL` | D | 15m | `TOL_15M 0.10` | 2.5 |
| `PDO`, `PDC` | D | 15m | 0.10 | 1.5 |
| `PWH`, `PWL` | D | 1h | `TOL_1H 0.15` | 3.0 |
| `D Swing H/L` (last 30) | D | 1d | `TOL_1D 0.25` | 3.0 |
| `Pivot PP` | ID | 5m | `TOL_5M 0.06` | 2.5 |
| `Pivot R1`,`S1` | ID | 5m | 0.06 | 2.0 |
| `Pivot R2`,`S2` | ID | 5m | 0.06 | 1.5 |
| `Pivot R3`,`S3` | ID | 5m | 0.06 | 1.0 |
| `Cam H4`,`Cam L4` | ID | 5m | 0.06 | 2.5 |
| `Cam H3`,`Cam L3` | ID | 5m | 0.06 | 2.0 |
| `OR High`,`OR Low` | ID | 5m | 0.06 | 1.5 |
| `VWAP` | ID | 5m | 0.06 | 1.5 |
| `1H Swing H/L` (last 30) | 1H | 1h | 0.15 | 2.0 |
| `15M Swing H/L` (last 40) | 15M | 15m | 0.10 | 1.5 |
| `Round {n:,.0f}` | ALL | 15m | 0.10 | 2.0 |

Formulas:
```
Standard pivots (prev-day H/L/C):  PP=(H+L+C)/3 ; R1=2PP-L ; R2=PP+(H-L) ; R3=H+2(PP-L)
                                   S1=2PP-H ; S2=PP-(H-L) ; S3=L-2(H-PP)
Camarilla:  H4=C+(H-L)*1.1/2  H3=C+(H-L)*1.1/4  L3=C-(H-L)*1.1/4  L4=C-(H-L)*1.1/2
Opening range: high/low of today's 5m bars where dt.hour==9 and dt.minute<=30
VWAP (range-weighted proxy, volume unavailable):
   Σ((h+l+c)/3 * (h-l)) / Σ(h-l)   over today's candles
Round numbers: base=round(spot/step)*step, i∈[-20,20], keep if within 3.0% of spot
   step: NIFTY 50, SENSEX 200, BANKNIFTY 100, FINNIFTY 50 (skip |rn-spot| < 1)
Prev week: Monday-of-this-week − 7d .. Monday − 3d, max(high)/min(low)
```
`detect_swings(window=3)` for 15M/1H, `window=2` for daily — same alternating-validated algorithm as the Fibo bot.

**Confluence merge** `merge_confluence_levels(levels, spot)` — `tol = spot * CONFLUENCE_TOL_PCT(0.25) / 100`. Levels within `tol` collapse: cluster inherits the **strongest** member's price/label/tf, appends `"{label}[{tf}]"` to `extra_types`, sets `confluent=True`, `strength = min(10, max(a,b) + 1.5)`, `efficiency = max(a,b)`, `touches` summed.

Then `above` = levels > spot sorted ascending, `below` = levels < spot sorted descending.

**`generate_trade_decision(spot, above, below)` → `(signal, text, color)`** with `NEAR_LEVEL_PCT=0.15`, `CAUTION_LEVEL_PCT=0.35`, `STRONG_SCORE=7.0`, `MODERATE_SCORE=4.0`, looking only at `above[:3]`/`below[:3]`:
1. Any level within 0.15% with `strength >= 7` → **`WAIT`** `⛔ Price AT strong level: … → Wait for break/bounce confirmation`
2. Any level within 0.15% → **`CAUTION`** `🟡 Price touching level: …`
3. `gap_above < 0.3% AND gap_below < 0.3%` and either `strength >= 4` → **`CAUTION`** `🟡 Squeezed: …`
4. Strong level (≥7) in the 0.15–0.35% band → **`CAUTION`** `👀 Approaching strong resistance/support …`
5. `gap_above >= 0.4% AND gap_below >= 0.4%` → **`OK`** `✅ Open space: … → Can trade freely`
6. else **`CAUTION`** `🟡 Level picture unclear — monitor closely`

**Option signal vote engine** `analyze_option_signal()` — geometry floors `MIN_TARGET=120` pts, `MIN_SL=25`, `MAX_SL=115`, `MIN_RR=2.0`, vote `THRESHOLD=3`.

`_recent_momentum(candles_5m, n=4)` → `BULLISH` if (`>=3` bull bars and net>0) or (`bull>bear` and net`>8`); `BEARISH` mirrored with net`<-8`; else `NEUTRAL`.
`_strong_bull/_strong_bear`: body/range `> 0.5` on the last 5m bar.

```
bull_votes: momentum BULLISH +2 | NEUTRAL +1
            spot > vwap                    +1
            strong bull last 5m candle     +1
            decision == "OK"               +1
            at_strong_sup AND bull_candle  +2   (confirmed bounce)
            at_strong_res                  -3   (resistance wall)
bear_votes: mirrored (momentum BEARISH +2/NEUTRAL +1, below VWAP, bear candle,
            OK, at_strong_res AND bear_candle +2, at_strong_sup -3)

MASTER_SIGNAL_BOT overlay (< 5 min old, same index):
   direction CE and confidence >= 60 → bull_votes += 3 if conf >= 75 else 1
   direction PE and confidence >= 60 → bear_votes += 3 if conf >= 75 else 1
   direction WAIT                    → bull_votes -= 1 ; bear_votes -= 1

fire CE if bull_votes >= 3 and bull_votes >= bear_votes
fire PE if bear_votes >= 3 and bear_votes >  bull_votes
confidence = "HIGH" if votes >= 5 and master isn't opposing, else "MEDIUM"
entry_type = "NOW" if (decision == "OK" and matching strong candle) else "BREAK"
```
`_resolve_ce()`: target = `above[1]` if `gap_above < 120` else `above[0]`; abort if `target_pts < 120`. SL = distance to `below[0]`; if `< 25` use fixed 25 (`sl_label = "Fixed -25pts"`); if `> 115` try `below[1]` else abort. Abort if `rr < 2.0`. `_resolve_pe()` mirrored.
`at_strong_sup/res` = any of `below[:3]`/`above[:3]` within 0.15% with `strength >= 7`.

`find_best_option(spot, chain, direction, min_prem=90.0, max_prem=160.0)` — candidates with LTP in range; prefer OTM side (CE `strike >= spot`, PE `strike <= spot`); pick `min(|strike-spot|)`.
`_opt_trigger(ltp)` = next 5-multiple strictly above; `_opt_limit(ltp)` = next 5-multiple strictly below.

**Alarms/alerts:** sound alarm `afplay /System/Library/Sounds/Glass.aiff` ×3 with 0.4s gaps in a daemon thread; keyed `alarm_{dir}_{round(spot/50)*50}`, cleared once price moves > `4 × 0.15%` away. Telegram on `confidence == "HIGH"` (key `opt_{dir}_{round(spot/50)*50}`) and on any `above[:3]+below[:3]` level within 0.15% with `strength >= 7` (key `{label}_{round(price)}`), cleared at `> 2 × 0.15%`.

### 5. OUTPUT CONTRACT

**There is no `.chart_levels.json`.** Three artifacts:

**(a) `logs/chart_level/Chart_Level_{YYYY-MM-DD_HH-MM-SS}.log`** — ANSI-stripped `print()` tee (same monkey-patch pattern as the Fibo bot).

**(b) `logs/chart_level/signals_{YYYY-MM-DD}.jsonl`** — one line appended **only when the alarm fires** (new CE/PE signal at a new 50-pt spot bucket):
```json
{"ts": "2026-06-01T14:57:00", "index": "NIFTY", "spot": 23369.35,
 "direction": "PE", "confidence": "HIGH",
 "reason": "bearish momentum | below VWAP | MASTER_BOT PE (79%)",
 "entry_type": "BREAK", "target_pts": 219.0, "sl_pts": 61.0, "rr_ratio": 3.6,
 "spot_target": 23150, "spot_sl": 23430.4,
 "strike": 23450, "option_ltp": 111.85}
```
Domains: `direction ∈ {"CE","PE"}`; `confidence ∈ {"HIGH","MEDIUM"}`; `entry_type ∈ {"NOW","BREAK"}`; `strike`/`option_ltp` are `null` when no option matched the ₹90–160 window; `ts` is `isoformat(timespec="seconds")`.

**(c) `logs/chart_level/live_chain.json`** — overwritten **every cycle**, full ATM±7 chain for the dashboard:
```json
{"ts": "2026-06-01T14:57:00", "spot": 23369.35,
 "chain": {"23400": {"ce_ltp": 128.5, "pe_ltp": 96.2},
           "23450": {"ce_ltp": 101.3, "pe_ltp": 111.85}}}
```
Note `chain` keys are **strings** (`str(float_strike)`), values always contain both `ce_ltp` and `pe_ltp` (0.0 if unfetched); strikes with both zero are pruned.

The in-memory `option_signal` dict (contract for `render_option_section` / any future export):
```python
{"direction":"CE"|"PE"|"NONE", "reason":str, "entry_type":"NOW"|"BREAK",
 "spot_entry":float, "spot_target":float, "spot_sl":float,
 "target_pts":float, "sl_pts":float, "rr_ratio":float,
 "confidence":"HIGH"|"MEDIUM"|"NONE",
 "target_label":str, "sl_label":str, "sources":["chart","MASTER_BOT(PE@79%)"]}
```
And each merged level dict: `{"price":float, "label":str, "tf":"D"|"1H"|"15M"|"ID"|"ALL", "strength":float, "efficiency":float, "touches":int, "confluent":bool, "extra_types":["PDH[D]", ...]}`.

### 6. Config knobs
Single `CLA_CONFIG` dict (no config file, no env vars):
```python
{"INDEX":"NIFTY", "REFRESH_SEC":30, "MARKET_OPEN":"09:15", "MARKET_CLOSE":"15:30",
 "TELEGRAM_ALERTS":True,
 "CONFLUENCE_TOL_PCT":0.25, "NEAR_LEVEL_PCT":0.15, "CAUTION_LEVEL_PCT":0.35,
 "STRONG_SCORE":7.0, "MODERATE_SCORE":4.0,
 "LOOKBACK_5M_HRS":48, "LOOKBACK_15M_HRS":120, "LOOKBACK_1H_HRS":480, "LOOKBACK_1D_HRS":2160,
 "SWING_WIN_15M":3, "SWING_WIN_1H":3, "SWING_WIN_1D":2,
 "TOL_5M":0.06, "TOL_15M":0.10, "TOL_1H":0.15, "TOL_1D":0.25,
 "ROUND_STEP":{"NIFTY":50,"SENSEX":200,"BANKNIFTY":100,"FINNIFTY":50},
 "DISPLAY_ABOVE":5, "DISPLAY_BELOW":5, "MAX_DIST_PCT":4.0}
```
Hardcoded in-function constants worth exposing on rebuild: `MIN_TARGET 120`, `MIN_SL 25`, `MAX_SL 115`, `MIN_RR 2.0`, `THRESHOLD 3`, `find_best_option min_prem 90 / max_prem 160`, option-chain cache 300s, master-signal staleness 300s, chain radius `step*7`, LTP chunk size 20.

### 7. Phantom 09:00 candle
**Only accidentally mitigated, never handled.** `calc_opening_range()` filters `dt.hour == 9 and dt.minute <= 30`, which **includes** a phantom 09:00 bar — so a fake wick sets a bogus `OR High`/`OR Low` (base weight 1.5). Worse, the same raw 5m/15m/1h/1d lists feed `detect_swings` (phantom high/low becomes a swing level) and `calc_vwap` (whose weights are literally `high-low`, so a 700-pt phantom range dominates the entire VWAP). Fix: `filter_spikes(candles, 8.0)` inside `fetch_candles()`, and tighten the OR filter to `dt.hour==9 and 15 <= dt.minute <= 30`.

---

# 4. PREMIUM_DIRECTION_TRACKER.py

`/Users/ayush/.../PREMIUM_DIRECTION_TRACKER.py` (1899 lines)

### 1. Purpose / order placement
Sub-second CE/PE premium direction ticker for one auto-selected near-ATM strike, printing `UP`/`DOWN`/`STABLE` per leg every 2 s with optional distinct WAV beeps. A background thread layers on a "FIB MENTOR" panel: 15m Fibonacci zone classification with CE/PE probability percentages, a composite 1–10 CE score, spot momentum regression, CE/PE-vs-spot divergence detection, day high/low proximity, and an ASCII premium-flow chart. **Read-only — no orders**; startup banner states "Read-only | no orders placed".

### 2. Loop cadence & market-hours gating
Two cadences, both unconditional (**no market-hours gate at all** — `MARKET_OPEN`/`MARKET_CLOSE` are defined in CONFIG but never referenced in the loop):
- **Foreground ticker:** `REFRESH_SEC = 2`. Fetches CE+PE in one batched LTP call, prints one line, sleeps 2 s. Spot is served from `_spot_cache` with `max_age = 3.0 s`.
- **`_fib_worker` daemon thread:** recomputes fibs every `FIB_REFRESH_SEC = 30`, waiting on `threading.Event` so a key-level cross can **force-refresh** immediately. Panel is *reprinted* from cache every `FIB_PRINT_SEC = 10` s (no API cost).
- Substitute for market gating: **frozen-LTP detector** — 20 consecutive identical CE ticks prints `⚠️ LTP unchanged for 20 ticks — market may be closed or API returning stale data.`
- Rate limiting: `_RateLimiter` gates every `/v1/live-data/ltp` call; HTTP 429 → `sleep(5)` and return `None`.

### 3. Data sources
Auth `groww_token.get_access_token`. Telegram `BOT_TOKEN "8666941668:..."` / `CHAT_ID "6012308856"`, always dispatched on daemon threads.

- Instruments: same CSV URL + 1-day staleness.
- Spot: `GET /v1/option-chain/exchange/{NSE|BSE}/underlying/{IDX}?expiry_date={exp}` → `payload.underlying_ltp`, timeout 8s, wrapped by `get_spot_live(max_age=3.0)`.
- CE+PE pair: `GET /v1/live-data/ltp?segment=FNO&exchange_symbols={EX}_{TS_CE}&exchange_symbols={EX}_{TS_PE}` — one call for both, halving quota. Single-leg `get_ltp()` used only during startup strike scanning.
- Fib candles (`_fetch_candles_fib`): `get_historical_candles(segment="CASH", candle_interval="15minute")`, `FIB_LOOKBACK_HOURS = 24` back, accepted at `len >= 5`, mapped to `{high, low, close}` only (no open, no ts). Symbols: NIFTY `["NSE-NIFTY 50","NSE-NIFTY"]`, SENSEX `["BSE-SENSEX","BSE-S&P BSE SENSEX"]`, BANKNIFTY `["NSE-NIFTY BANK"]`.
- Day H/L (`_refresh_day_hl`, piggybacked on the fib cycle for zero extra rate cost): same endpoint with `start_time = today 09:15:00`, `end_time = now`, `15minute`; `_day_hl = {"high": max(highs), "low": min(lows), "updated_at": now}`.

Startup is interactive: `ask_index()` (1=NIFTY/2=SENSEX, env `BOT_INDEX`), `ask_expiry()` (current/next, env `BOT_EXPIRY` = `current`|`next`), `ask_sound_settings()` (env `BOT_SOUND` = `y`|`n`, `BOT_SOUND_TRACK` = `CE`|`PE`).

**Strike selection** `select_strike()`: `step` = 100 SENSEX else 50; offsets `[0,-1,+1,-2,+2,…]` up to `STRIKE_SCAN_RANGE = 8`; picks the first strike where **both** CE and PE LTP ∈ `[MIN_PREMIUM 120, MAX_PREMIUM 380]`; falls back to raw ATM.

### 4. Core algorithm

**Direction** `direction(prev, curr, threshold=DIRECTION_THRESHOLD=0.25)`: `None → "INIT"`; `diff > 0.25 → "UP"`; `diff < -0.25 → "DOWN"`; else `"STABLE"`. Sound plays only on a *change* of direction for the selected track. WAVs are pre-generated at import (44.1 kHz, 16-bit): `UP` = two 1000 Hz 70 ms pips with 50 ms gap; `DOWN` = one 180 Hz 300 ms tone; played via `afplay`.

**Fib math** `_calc_fib(low, high, bullish)` — retrace `[0.236,0.382,0.500,0.618,0.786]` labelled `R23.6%…R78.6%`, extension `[1.272,1.618]` labelled `E127.2%`,`E161.8%`; bullish: `R = high - rng*r`, `E = low + rng*r`; bearish mirrored. Returns `SWING_HIGH`, `SWING_LOW`, `_range`, `_bullish` + levels.

**`_detect_swings(candles, window=FIB_SWING_WINDOW=3)`** — same alternating-validated H/L algorithm (no `idx` field).
**`_swing_pair(swings, spot)`** — 3 passes: (1) newest pair containing spot; (2) newest pair with `lo - 0.5·rng <= spot <= hi + 0.5·rng` (note: **0.5× not 1×**, tighter than the Fibo bot); (3) pair minimising `max(0, lo-spot, spot-hi) / rng` (range-normalised so small near swings beat large distant ones).
**Early-session fallback:** if no pair, retry `_detect_swings` with `window = 2` then `1`.

**`_fib_signal(spot, fib)` — the 16-branch zone classifier.** Returns `{zone, trend, ce_prob, pe_prob, action, target, stop_price, stop_label, mentor[], sup_label, sup_price, res_label, res_price}`.

Bullish swing (`_bullish=True`), evaluated top-down:
| condition | zone | CE/PE | action | target |
|---|---|---|---|---|
| `spot >= SH` | `BREAKOUT — above swing high` | 85/15 | `STRONG CE — ride the breakout` | `E127.2%` (or `SH+0.272·rng`) |
| `>= R23.6%` | `SHALLOW PULLBACK — above R23.6% (…)` | 70/30 | `STAY IN CE — shallow pullback…` | `SH` |
| `>= R38.2%` | `NORMAL PULLBACK — R23.6% to R38.2%` | 60/40 | `LEAN CE — watch R38.2% for bounce` | `SH` |
| `>= R50.0%` | `DEEP PULLBACK — R38.2% to R50.0% (midpoint)` | 52/48 | `WAIT — no edge at midpoint…` | `SH` |
| `>= R61.8%` | `GOLDEN ZONE — R50.0% to R61.8% (critical support)` | 42/58 | `WATCH CE — golden zone bounce possible, high risk` | `R38.2%` |
| `>= R78.6%` | `DANGER ZONE — R61.8% to R78.6% (trend failing)` | 28/72 | `LEAN PE — uptrend likely reversing` | `SL` |
| `> SL` | `NEAR BREAKDOWN — below R78.6% (…)` | 15/85 | `STRONG PE — breakdown very likely` | `SL` |
| `<= SL` | `BREAKDOWN CONFIRMED — below swing low` | 10/90 | `STRONG PE — breakdown confirmed below swing low` | first unmet down-extension |

Bearish swing mirrored (`BREAKOUT CONFIRMED — above swing high` 90/10, `BREAKDOWN — below swing low` 15/85, `SHALLOW BOUNCE` 30/70, `NORMAL BOUNCE` 40/60, `DEEP BOUNCE` 48/52, `GOLDEN ZONE — …(critical resistance)` 55/45, `DANGER ZONE — …(trend reversing)` 72/28, `NEAR BREAKOUT — above R78.6%` 85/15).

Full-negation extension ladders (measured from the opposite extreme, first level price hasn't reached yet): `[1.272, 1.618, 2.618, 4.236, 6.854]` labelled `E127.2%, E161.8%, E261.8%, E423.6%, E685.4%`.

**Momentum** `_calc_momentum()` — least-squares slope over `_spot_history` (`deque(maxlen=40)`, one sample per 30 s fib refresh → ~20 min):
```
pts_per_min = slope_per_sample * (60 / FIB_REFRESH_SEC)
|pts_per_min| < 3.0 → "FLAT"    else "UP"/"DOWN"
```
Needs ≥4 samples.

**Divergence** `_calc_divergence()` over `_tick_history` (`(spot, ce, pe)` triples), comparing the mean of the first third vs last third, thresholds `SPOT_T = 3.0` pts, `PREM_T = 0.60` ₹, needs ≥6 ticks:
- `spot ↑>3` and `pe ↑>0.6` → `BEARISH  "institutional hedging"`
- `spot ↑>3` and `ce ↓<-0.6` → `BEARISH  "smart selling"`
- `spot ↓<-3` and `ce ↑>0.6` → `BULLISH  "smart accumulation"`
- `spot ↓<-3` and `pe ↓<-0.6` → `BULLISH  "PE sellers absorbed"`

**Composite CE score 1–10** `_calc_composite_score()`:
```
fib_score  = (ce_prob - 50)/50 * 3.0
mom_score  = ±min(2.0, |pts_per_min|/5.0)         (0 when FLAT)
flow_score = (mean(last 5 of _prob_history) - 50)/50 * 2.0   (0 if <5 samples)
div_score  = +1.0 BULLISH / -1.0 BEARISH / 0
total    = fib*0.40 + mom*0.30 + flow*0.20 + div*0.10
ce_score = int(round(clamp(5 + total*1.5, 1, 10)))
breakdown = "FIB✅  MOM⚪  FLOW❌  DIV⚪"   # ✅ if fib ce_prob>=60 / mom UP / flow>0.3 / div BULLISH
                                          # ❌ if ce_prob<=40 / mom DOWN / flow<-0.3 / div BEARISH
```
`_prob_history` (`deque(maxlen=40)`) is appended each panel print with `round(ce/(ce+pe)*100)` — the flow chart is a pure premium ratio, independent of Fibonacci.

**Flow chart trend** (`_print_prob_chart`): `recent-older > 3 → ↑ BULLISH`, `< -3 → ↓ BEARISH`, else `→ NEUTRAL`; dot colour green `>55`, red `<45`.

**Alert triggers** (all Telegram, all cooldowns keyed in `_telegram_sent`):
- Zone change in `_refresh_fib` → `📐 PDT ZONE CHANGE` with was/now/spot/action (no cooldown, only on actual change); also triggers macOS `say -r {FIB_VOICE_RATE}` when `FIB_VOICE`.
- Key-level cross of `sup_price`/`res_price` → `⚡ PDT KEY LEVEL: {BREAKOUT|PULLBACK|BREAKDOWN|BOUNCE} at {lvl}`, cooldown **120 s**, and sets the fib force-refresh event.
- Day H/L: crossing → `🚀 PDT DAY HIGH BROKEN` / `🔻 PDT DAY LOW BROKEN` with next target `dh + range*0.618` / `dl - range*0.618`; else proximity within **15 pts** → `⚠️ PDT NEAR DAY RANGE`. Cooldown **180 s**.
- `ce_score <= 3` → `🔴 PDT LOW SCORE`, cooldown **300 s**.

Day H/L display bands: `<15 pts` near (red/green), `15–40` mid (orange/yellow), `>40` far (gray).

### 5. OUTPUT CONTRACT

**There is no `.premium_direction.json`** (zero `json.dump` calls). Sole artifact:

`logs/premium_tracker/Premium_Tracker_{YYYY-MM-DD_HH-MM-SS}.log` — ANSI-stripped `builtins.print` tee (identical monkey-patch to the Fibo/Chart bots; deliberately does *not* replace `sys.stdout`).

**The tick line format IS the contract** — `SIGNAL_ANALYZER.parse_pdt_logs()` scrapes it with
`r"\[(\d{2}:\d{2}:\d{2})\].*?SPOT\s+([\d.]+).*?CE\).*?₹\s*([\d.]+).*?PE\).*?₹\s*([\d.]+)"` (DOTALL) into `{ts, spot, ce_ltp, pe_ltp}`, used for premium-correlation scoring of MASTER signals. Emitted line:
```
[09:32:18]  SPOT 24409.5  (24400 CE) ↑ UP     ₹ 122.25   |   (24400 PE) → STABLE ₹  88.00
```
Do not change: bracketed `HH:MM:SS`, literal `SPOT `, `(strike CE)`/`(strike PE)` parenthesised labels, and `₹` immediately preceding each premium.

In-memory `_fib_state` (contract if a JSON export is added):
```python
{"updated_at": datetime, "spot": 24409.45, "swing_desc": "↑ 24334→24375",
 "zone": "GOLDEN ZONE — R50.0% to R61.8% (critical support)",
 "trend": "⬆ WEAKENING — deep pullback into golden zone",
 "ce_prob": 42, "pe_prob": 58,
 "action": "WATCH CE — golden zone bounce possible, high risk",
 "target": 24360.1, "stop_price": 24340.2, "stop_label": "R78.6% (24340)",
 "mentor": ["⚠️  Deep pullback into GOLDEN ZONE (50–61.8%).", "..."],
 "sup_label": "R61.8%", "sup_price": 24350.1,
 "res_label": "R38.2%", "res_price": 24401.4}
```

### 6. Config knobs
```python
CONFIG = {
  "MIN_PREMIUM": 120, "MAX_PREMIUM": 380,     # both legs must fall in range
  "STRIKE_SCAN_RANGE": 8,
  "DIRECTION_THRESHOLD": 0.25,                # ₹ to register UP/DOWN
  "REFRESH_SEC": 2,                           # keep >= 2 alongside main bot
  "MARKET_OPEN": "09:15", "MARKET_CLOSE": "15:30",   # DEFINED BUT UNUSED
  "FIB_PRINT_SEC": 10,                        # panel reprint (cached, cheap)
  "FIB_REFRESH_SEC": 30,                      # fib recompute (keep >= 60 to be safe)
  "FIB_LOOKBACK_HOURS": 24, "FIB_SWING_WINDOW": 3,
  "FIB_VOICE": False, "FIB_VOICE_RATE": 160,
}
TEST_MODE = False
TEST_INDEX="NIFTY"; TEST_STRIKE=24700; TEST_CE_START=135.0; TEST_PE_START=118.0; TEST_VOLATILITY=0.6
```
`TEST_MODE=True` runs entirely offline: `_MockPrice` random walks (±0.6 ₹/tick), `_MockSpot`, and a pre-seeded `_fib_state` so the panel renders immediately — no API calls at all.
~40-key hex `COLOR_CONFIG` (`UP DOWN STABLE SPOT BULLISH BEARISH NEUTRAL BREAKOUT SUPPORT TARGET DAY_H_NEAR/MID/FAR DAY_L_NEAR/MID/FAR SCORE_HIGH/MID/LOW CE_HIGH/MID/LOW PE_HIGH/MID/LOW ACTION_BULL/BEAR/NEUTRAL MENTOR_NOTES FLOW_BULL/BEAR/NEUTRAL API_OK INSTRUMENTS_OK TRACKING_LABEL SPOT_LABEL FIB_START TRACKER_HEADER TRACKING_LINE STATUS_DIM`). No config JSON. Env: `BOT_INDEX`, `BOT_EXPIRY`, `BOT_SOUND`, `BOT_SOUND_TRACK`.

### 7. Phantom 09:00 candle
**Not handled, and directly damaging.** `_refresh_day_hl` requests candles from exactly `today 09:15:00`, but the Groww index feed still returns the phantom 09:00 bar, whose fake wick becomes `_day_hl["high"]`/`["low"]`. That single value drives: the Day H/L panel line, the ±15 pt proximity Telegram alerts, the day-break alerts, and the `± range*0.618` extension targets — so a phantom bar produces a permanently "unbroken" day high and a nonsensical extension target all session. `_fetch_candles_fib` (24 h of 15m bars) is likewise unfiltered, so the phantom high/low can become the swing pair anchoring every fib level and the whole zone classification. Add `filter_spikes(..., 8.0)` in both fetchers and clamp `_refresh_day_hl` to bars at/after 09:15 IST.

---

# 5. MOMENTUM_AUTO_BOT.py

`/Users/ayush/.../MOMENTUM_AUTO_BOT.py` (1484 lines)

### 1. Purpose / order placement
Short-horizon premium-velocity auto-trader: discovers all ATM±3 CE/PE strikes priced ₹50–200, observes every one for 10 s at 1 Hz, scores each side by `|velocity| × consistency`, then buys the single best strike on the winning side and manages it with a hard SL plus trailing SL. **This bot PLACES REAL ORDERS** — market BUY then market SELL via `groww.place_order` — gated by `CONFIG["trade_mode"]`: `"paper"` (no orders, no Telegram, real LTP), `"mock"` (no orders, Telegram on, fully scripted prices), `"live"` (real orders). Current on-disk override is `{"trade_mode": "paper"}`.

### 2. Loop cadence & market-hours gating
**There is no market-hours gate whatsoever** — `grep` for `market_open|market_close|is_market` in this file returns nothing. It scans continuously whenever running; the operator (or `START_ALL_BOTS.command`) is the gate.

Per-cycle timing: `cooldown_sec = 120` after any trade, `no_signal_wait_sec = 60` after a no-signal scan (counted down 1 s at a time), 5 s retry when no candidate premiums are found. `_reload_override(verbose=False)` runs every cycle so dashboard toggles take effect live.

Two independent pause mechanisms both push `next_scan_at = now + 60`:
- **Consecutive-Hard-SL circuit breaker** (`consec_sl_brake=True`): after `max_consecutive_hard_sl = 2` consecutive Hard SLs, pause `consec_sl_pause_min = 30` min, Telegram `🛑 CIRCUIT BREAKER`. Counter resets on any non-Hard-SL exit.
- **Choppiness pause** (see §4): `choppiness_pause_min = 15` min on `HIGH`.

ATM drift: re-fetch spot each cycle; if `|new_atm - last_atm| >= 2 × step` → reload instruments.

### 3. Data sources
Auth `groww_token.get_access_token` → `GrowwAPI`. Alerts via `whatsapp_gateway.send_whatsapp` aliased `send_telegram`; `start_webhook_server()` called at startup.

| Purpose | Endpoint |
|---|---|
| Option LTP (one per call, `_ltp_lock`-serialised + `sleep(0.05)`) | `GET https://api.groww.in/v1/live-data/ltp?segment=FNO&exchange_symbols=NSE_{trading_symbol}` |
| Index spot | `GET https://api.groww.in/v1/live-data/ltp?segment=CASH&exchange_symbols={NSE_NIFTY\|BSE_SENSEX\|…}` |
| Order create | `groww.place_order(trading_symbol, quantity, validity=VALIDITY_DAY, exchange=EXCHANGE_NSE, segment=SEGMENT_FNO, product=PRODUCT_MIS, order_type=ORDER_TYPE_MARKET, transaction_type=BUY\|SELL, price=0)` |
| Order status | `GET https://api.groww.in/v1/order/status/{order_id}?segment=FNO` → `payload.order_status` |
| Executed price | `GET https://api.groww.in/v1/order/trades/{order_id}?segment=FNO&page=0&page_size=50` → `payload.trade_list`, qty-weighted avg, **retry ×4 at 500 ms** |
| ATR candles | `groww.get_historical_candles(segment=SEGMENT_FNO, candle_interval="1minute")`, last 60 min |

Note the LTP URL hardcodes the `NSE_` prefix — SENSEX options would need `BSE_`. Instruments loaded from local `instrument.csv` only (never downloaded here), filtered to `underlying_symbol == index`, `expiry_date == expiry`, `ATM ± 20×step`. Strike step auto-resolved from `_INDEX_STRIKE_STEP` (NIFTY 50, BANKNIFTY 100, FINNIFTY 50, SENSEX 100, BANKEX 100), overwriting `CONFIG["strike_step"]`.

Reads `oi_snapshot.json` (written by `calculate_oi_pcr.py`) as a soft bias: rejected if `time.time() - snap["timestamp"] > oi_max_age_sec (120)`. `oi_bias()`: if `writer_bias == sentiment` use it; else tiebreak `pcr_atm > 1.1 → BULLISH`, `< 0.9 → BEARISH`, else `writer_bias`. **The bias is logged and recorded for post-hoc verdicts but never blocks a scan** (comment: filtering by bias blocks a whole side and misses in-range ITM options).

Default expiry `_next_expiry(weekday=1)` = next Tuesday.

### 4. Core algorithm

**Phase 1 — Discover** `discover_candidates(spot)`: pairs `(atm ± i·step, CE|PE)` for `i in 0..atm_range(3)`, deduped; parallel `ThreadPoolExecutor(max_workers=min(len,20))`; keep if `min_premium(50) <= ltp <= max_premium(200)`. Returns `(strike, opt_type, instrument, ltp)`.

**Phase 2 — Observe** `run_observation()`: `scan_seconds = 10` ticks at `scan_poll_sec = 1` (all strikes polled in parallel per tick, then sleep the remainder of the second). Prints `Nth second` + `CE = …` / `PE = …` rows. In `mock` mode prices are scripted: `CE += 0.6·tick`, `PE -= 0.2·tick` (so CE always wins).

**Phase 3 — Score** `analyze_momentum()`, per strike with ≥3 ticks:
```
velocity    = (last - first)/first * 100
deltas      = consecutive diffs
direction   = sign(velocity)
consistency = (# deltas matching direction) / len(deltas) * 100
score       = |velocity| * consistency/100
side_net(S) = mean over strikes of side S of (velocity_pct * consistency/100)   # SIGNED
```
Decision chain:
1. `ce_net <= 0 and pe_net <= 0` → no signal (`Both sides showing no upward momentum`).
2. `winning_side = "CE" if ce_net >= pe_net else "PE"`.
3. If `min_score_filter` (default True): require `winning_net >= velocity_pct × consistency_pct/100 = 0.5 × 0.55 = 0.275`, else reject as too weak.
4. If `velocity_filter` (default True): candidates on winning side must have `direction == "UP"` **and** `|velocity_pct| >= velocity_pct (0.5%)`; else (filter off) just `direction == "UP"`.
5. Best = highest `score`. Attach `scan_atr = max(ticks) - min(ticks)`.

Signal dict: `{strike, opt_type, inst, velocity_pct, consistency_pct, score, direction, entry_ltp, scan_atr, oi_bias}`.

**Choppiness tracker `_ChopTracker`** (rolling `choppiness_window = 6` scans of `{dir, spread=|ce_net-pe_net|}`), needs ≥3 scans:
```
flip_rate  = (# adjacent direction changes) / (len(dirs)-1)
avg_spread = mean(spread)
thresholds: flip_thresh = 0.55 ; spread_min = 1.5 ; max_hard_sl = 2
  on expiry day: flip_thresh *= 0.85 (→0.47) ; spread_min *= 1.25 (→1.88)

chop_score: +2 if flip_rate > flip_thresh        | +1 if > 0.75×flip_thresh
            +2 if avg_spread < spread_min        | +1 if < 1.3×spread_min
            +3 if hard_sl_streak >= max_hard_sl
            +1 if expiry day and hour>=13 and minute>=30
level: >=3 HIGH  |  >=1 MEDIUM  |  else LOW
```
`HIGH` → `trigger_pause()` for 15 min, Telegram `⚠️ CHOPPINESS ALERT`, skip the signal, retry in 60 s. `MEDIUM` → warn only.

**Phase 4 — Manage** (`execute_trade`):
```
qty = lots × instrument.lot_size
Hard SL:
  HARD_SL_ATR_BASED=False (default) → HARD_SL_POINTS = 8.0 pts
  True + atr_source="candle" → 14-period EMA-of-TR from 1-min candles × 1.5   (no floor)
                               fetched in a BACKGROUND THREAD started right after
                               BUY submit (overlaps validation), 6 s timeout, 4 s get
  True + atr_source="scan"   → max(3.0, scan_atr × 1.5)
  fallback on failure        → HARD_SL_POINTS
hard_sl = avg_price - hard_sl_pts

ATR maths: TR_i = max(h_i-l_i, |h_i-c_{i-1}|, |l_i-c_{i-1}|)
           EMA(period=14): seed = mean(first 14), k = 2/15, ema = v*k + ema*(1-k)

Trail: step = TRAIL_STEP 0.75 (or scan_atr × TRAIL_SL_ATR_MULTIPLIER if TRAIL_SL_ATR_BASED)
       arms once highest_price >= entry + TRAIL_START_PROFIT (1.0)
       trail_exit = highest_price - step ; exit when ltp <= trail_exit
Exits (checked in order each poll_seconds=1):
  ltp <= hard_sl                                  → 🛑 HARD SL
  elapsed >= max_hold_min(30)*60                  → ⏰ Max hold time
  exit_mode=="quick" and ltp >= entry + 1.0       → 🎯 Quick target hit
  else trail logic
LTP failure backoff: min(30, poll_seconds * 2**min(streak,6))
Heartbeat print every 30 s.
```
Order flow: BUY market → if `validate_orders` poll `/order/status` every 2 s until `EXECUTED|COMPLETED|DELIVERY_AWAITED` (abort on `FAILED|REJECTED|CANCELLED`) → `_get_executed_price` overrides `avg_price`/`qty`. SELL market → if validate, block for confirmed fill price; if **not** validate, a daemon thread waits 2 s and logs actual fill + slippage vs detection LTP.

**OI verdict** `_oi_verdict(oi_bias, opt_type, profit)` → tags `ALIGNED_WIN`, `ALIGNED_LOSS`, `OPPOSED_WIN`, `OPPOSED_LOSS`, `NEUTRAL` (aligned = BULLISH↔CE or BEARISH↔PE). Purely retrospective measurement of whether enabling the filter would have helped.

### 5. OUTPUT CONTRACT

**(a) `logs/momentum_bot/Momentum_Bot_{YYYY-MM-DD_HH-MM-SS}.log`** — created at import time; here `sys.stdout` **and** `sys.stderr` are replaced by a `Tee` class (unlike the analyzer bots' `print` monkey-patch), so ANSI is *not* stripped. `LIVE_DASHBOARD.read_momentum_bot()` regex-scrapes this log for live status (looking for `Signal found`, `CLOSED`, `SELL placed`, `[HH:MM:SS]` timestamps).

**(b) `logs/trade_history/{YYYY-MM-DD}.jsonl`** — one line appended per completed trade (`_log_trade_history`). **Shared file: `bot` field discriminates producers.**
```json
{"date": "2026-08-04", "time_entry": "11:28:03.412", "time_exit": "11:31:47.905",
 "bot": "Auto", "mode": "paper", "index": "NIFTY",
 "symbol": "NIFTY26AUG0424700CE", "expiry": "2026-08-05",
 "buy_price": 132.45, "sell_price": 136.10,
 "qty": 75, "lots": 1, "pnl": 273.75,
 "exit_reason": "🔻 Trail SL hit @ ₹136.10  (peak=₹136.85  exit=₹136.10)  [detected 11:31:47.905]",
 "oi_bias": "BULLISH", "oi_verdict_tag": "ALIGNED_WIN"}
```
`bot` is always the literal `"Auto"`; `mode ∈ {"paper","mock","live"}`; `oi_verdict_tag ∈ {ALIGNED_WIN, ALIGNED_LOSS, OPPOSED_WIN, OPPOSED_LOSS, NEUTRAL, SELL_FAILED}`; `time_entry`/`time_exit` are `HH:MM:SS.mmm` (millisecond precision from `_ts()`), **not** ISO datetimes.

**(c) `Lakshmi.xlsx`** — sheet `Momentum_Trades`, created with header `["DateTime","Symbol","Buy Price","Sell Price","Qty","Profit","Source"]`; each row appends `[now "%Y-%m-%d %H:%M:%S", symbol, buy_px, sell_px, qty, round(profit,2), "MOMENTUM_BOT"]`.

No `.momentum*.json` status file is written — the dashboard reads the log text.

### 6. Config knobs
Full `CONFIG` (all defaults):
```python
{"index":"NIFTY", "expiry":_next_expiry(1), "strike_step":50, "atm_range":3, "lots":1,
 "min_premium":50, "max_premium":200,
 "scan_seconds":10, "scan_poll_sec":1, "poll_seconds":1,
 "velocity_pct":0.5, "consistency_pct":55,
 "HARD_SL_POINTS":8.0, "TRAIL_START_PROFIT":1.0, "TRAIL_STEP":0.75,
 "TRAIL_SL_ATR_BASED":False, "TRAIL_SL_ATR_MULTIPLIER":1.0,
 "QUICK_TRAIL_BUFFER":1.0, "QUICK_TRAIL_GAP":1.5,
 "max_hold_min":30, "cooldown_sec":120, "no_signal_wait_sec":60,
 "max_trades_day":5,            # DEFINED BUT NEVER ENFORCED (banner prints "unlimited")
 "validate_orders":True, "use_oi_filter":True, "oi_max_age_sec":120,
 "trade_mode":"paper",          # paper | mock | live
 "exit_mode":"manual",          # manual | quick
 "consec_sl_brake":True, "consec_sl_pause_min":30,
 "HARD_SL_ATR_BASED":False, "HARD_SL_ATR_MULTIPLIER":1.5, "atr_source":"candle",  # candle | scan
 "min_score_filter":True, "velocity_filter":True,
 "choppiness_enabled":True, "choppiness_window":6, "choppiness_flip_threshold":0.55,
 "choppiness_spread_min":1.5, "choppiness_pause_min":15, "max_consecutive_hard_sl":2}
```

**`momentum_config_override.json`** — written by `LIVE_DASHBOARD` on launch and on every UI toggle; re-read every scan cycle. Only keys present in `_OVERRIDE_CAST` are applied, each coerced through its cast; unknown keys ignored; a bad value is silently skipped. Current file: `{"trade_mode": "paper"}`. Full accepted schema:
```json
{
  "trade_mode": "paper",  "index": "NIFTY",  "expiry": "2026-08-05",  "lots": 1,
  "exit_mode": "manual",  "min_premium": 50.0,  "max_premium": 200.0,  "atm_range": 3,
  "velocity_pct": 0.5,  "consistency_pct": 55.0,  "validate_orders": true,
  "scan_seconds": 10,  "poll_seconds": 1,
  "consec_sl_brake": true,  "consec_sl_pause_min": 30,
  "HARD_SL_ATR_BASED": false,  "HARD_SL_ATR_MULTIPLIER": 1.5,  "atr_source": "candle",
  "min_score_filter": true,  "velocity_filter": true,
  "choppiness_enabled": true,  "choppiness_window": 6,
  "choppiness_flip_threshold": 0.55,  "choppiness_spread_min": 1.5,
  "choppiness_pause_min": 15,  "max_consecutive_hard_sl": 2,
  "_vix_config_note": "VIX 12.8 → LOW vol: velocity_pct 0.4, atm_range 4"
}
```
`_vix_config_note` is a non-config passthrough: printed as `[VIX AUTO CONFIG] {note}` whenever its value changes (deduped via `_last_vix_note`). Casts are `bool(...)` — so **any non-empty JSON string coerces to `True`**; the dashboard must write real JSON booleans.

Careful on rebuild: `_OVERRIDE_CAST` lacks `HARD_SL_POINTS`, `TRAIL_STEP`, `TRAIL_START_PROFIT`, `max_hold_min`, `cooldown_sec`, `use_oi_filter` — those are not remotely tunable today.

### 7. Phantom 09:00 candle
**Not applicable / not present.** This bot never reads index candles. Its only candle usage is 60 min of **1-minute FNO option** candles for the ATR (and only when `HARD_SL_ATR_BASED=True` with `atr_source="candle"`), and the phantom bar is an index-feed artifact. Everything else is live LTP polling. If the phantom-bar defect ever appears on the option feed it would inflate one TR term and thus the hard SL; a `filter_spikes` pass in `_fetch_real_atr` before `_real_atr_from_candles` would be cheap insurance.

---

# 6. TRENDLINE_SCANNER_BOT.py

`/Users/ayush/.../TRENDLINE_SCANNER_BOT.py` (1912 lines)

### 1. Purpose / order placement
Trendline-structure scanner over ~82 option contracts (ATM±20 strikes × CE/PE): builds up to four trendline rails plus a horizontal zone per instrument from 5-min candles, and fires four signal types — `BOUNCE` (near ascending support), `BREAK` (through support → buy the *opposite* side), `BREAKOUT` (through descending resistance), `HORIZ_BOUNCE` — each requiring a live tick-confirmation window. **Simulation by default (`CONFIG["sim"] = True`) — no orders are ever sent to a broker in any code path**; `open_trade`/`close_trade` only mutate in-memory state and write logs, so the `sim` flag currently changes only log wording. Uses **public, unauthenticated** Groww web endpoints for option data.

### 2. Loop cadence & market-hours gating
Three concurrent loops:
- **`monitor_loop`** (main, blocking): `ltp_poll_sec = 15`. Fetches all LTPs in parallel, manages open trades, evaluates signal triggers.
- **`structural_loop`** (daemon): `structural_refresh = 30` s. Re-fetches candles + recomputes trendlines for every instrument **not** in a trade, then `_refresh_spot()`, `_print_status_block()`, `_write_chart_data()`.
- **Per-signal daemon threads** (`_signal_worker`) so the monitor never blocks during confirmation.

**Real market-hours gate:** `is_market_open()` computes IST as `datetime.utcfromtimestamp(time.time() + IST_OFFSET(19800))` and returns `MARKET_OPEN (9,15) <= (hour,minute) < MARKET_CLOSE (15,30)`. Outside hours: log `⏸ Outside market hours — sleeping 60 s`, **still call `_write_signals_file(None)`** to keep the dashboard's TODAY stats fresh, sleep 60, continue.

**Single-trade gate:** `any_busy = any(i.active_trade or i.confirming for i in watch_list)` — at most one trade or confirmation across all 82 instruments at a time.

### 3. Data sources
No auth for option data. Session headers spoof the Groww web app: `x-app-id: growwWeb`, `x-device-id`/`x-device-id-v2` = `CONFIG["device_id"] = "8cea1d25-588a-5eff-9699-5e7fd20a6ca9"` (from a HAR capture), `x-platform: web`, a Chrome UA. Base `_GROWW = "https://groww.in/v1/api"`, `req_timeout = 8`.

| Purpose | Endpoint |
|---|---|
| Option 5-min candles | `GET {_GROWW}/stocks_fo_data/v1/charting_service/chart/exchange/{EXCH}/segment/FNO/{SYMBOL}/daily?intervalInMinutes=5` |
| Option LTP | `GET {_GROWW}/stocks_fo_data/v1/tr_live_prices/exchange/{EXCH}/segment/FNO/{SYMBOL}/latest` → `ltp` |
| Index spot LTP | `GET {_GROWW}/stocks_data/v1/tr_live_indices/exchange/{EXCH}/segment/CASH/{INDEX}/latest` → `value` |
| **Index 5-min candles (auth required)** | `GET https://groww.in/v1/api/charting_service/v4/chart/exchange/{EXCH}/segment/CASH/{INDEX}?startTimeInMillis=&endTimeInMillis=&intervalInMinutes=5` with `authorization: Bearer …`, `x-device-type: charts` |

Index candles are the one authenticated call: token minted by `_auto_bearer_token()` which reads **`ai_config.json`** keys `groww_api_key` + `groww_totp_secret` and calls `groww_token.get_access_token`; cached in `_spot_bearer`, refreshed once on HTTP 401, cleared on any exception. Window: `now - 2 days` → `now` (2 days so pre-market refreshes still have pivots). Response accepts `data["candles"]` or `data["data"]["candles"]`; timestamps `> 1e12` are treated as **milliseconds and divided by 1000**.

Candle dicts use short keys: `{"ts": int_utc_sec, "o":, "h":, "l":, "c":, "v": int (0 if null)}`.

Symbol construction `make_symbol(index, expiry, strike, opt_type)` = `f"{index}{yy}{m}{dd}{strike}{opt_type}"` with `yy = year % 100`, `m` = month **without leading zero**, `dd` zero-padded → e.g. `NIFTY2662324000PE`. `LOT_SIZES = {"NIFTY":65,"BANKNIFTY":15,"FINNIFTY":40,"SENSEX":20,"BANKEX":15}` (default 75).

Watch list built once at startup from `atm_strike(spot, step)` over `offset ∈ [-20, +20]` × `{CE, PE}` = 82 instruments; **never rebuilt** as spot drifts.

### 4. Core algorithm

`filter_today(all_candles)` — keeps only bars whose IST date (`utcfromtimestamp(ts + 19800).date()`) equals the **last** bar's date.

**Pivots** (`pivot_lookback = 3` each side, `min_pivots = 2`):
- `find_swing_lows`: `candles[i].l` strictly less than all `l` in `[i-3, i+3]`.
- `find_swing_highs`: `candles[i].h` strictly greater than all `h` in `[i-3, i+3]`.

**Projection** `project_trendline(pivots, cur_idx)` — line through the **last two** pivots only: `slope = (p2.price - p1.price)/(p2.idx - p1.idx)`; returns `p2.price + slope*(cur_idx - p2.idx)`.

**Five structures**, all requiring `len(today) >= 2·lb + 2 = 8` bars and all storing the projected level in `TrendlineState.support` (misleading name for the resistance variants):

| function | pivots | monotonicity required on last `min_pivots` | `.support` holds | extra |
|---|---|---|---|---|
| `compute_trendline` | lows | strictly rising | ascending support (lower rail) | also sets `last_swing_high = max(all swing highs)` |
| `compute_ascending_resistance` | highs | strictly rising | ascending channel top | `ascending=True` |
| `compute_descending_trendline` | highs | strictly falling | descending resistance | `ascending=False` |
| `compute_descending_support` | lows | strictly falling | descending channel bottom | `ascending=False` |
| `detect_horizontal_zone` | both | — | returns mid-price float or `None` | needs ≥ `2lb+4=10` bars |

`detect_horizontal_zone(tolerance_pct=0.15)`: `mid = mean(last 2 lows + last 2 highs)`; valid only if **both** `|low2-low1|/mid*100 <= 0.15` **and** `|high2-high1|/mid*100 <= 0.15`.

**Signal triggers** (`monitor_loop`, per instrument; requires the structure's toggle on, `premium_min(85) <= ltp <= premium_max(200)`, `not any_busy`):
```
dist = ltp - tl.support
  0.0 <= dist <= proximity_pts (6.0)     → BOUNCE
  dist < -break_pts (3.0)                → BREAK
dist_r = ltp - tl_resist.support
  -6.0 <= dist_r <= 6.0                  → BREAKOUT
dist_h = ltp - horiz_zone
  0.0 <= dist_h <= 6.0                   → HORIZ_BOUNCE
```
Only the first matching signal fires per instrument per poll (`signal_fired` short-circuits).

**Confirmation windows:**
- `confirm_bounce(inst, baseline)` — polls LTP every **3 s** for `bounce_confirm_sec = 25`; confirmed when `ltp - baseline >= _required_confirm_pts()`; **aborts immediately** if `ltp < tl.support - break_pts`. `_required_confirm_pts(ltp)` = `bounce_confirm_pts (2.0)` fixed, or `max(round(ltp * bounce_confirm_pct/100, 2), 1.0)` when `pct_confirm_enabled`.
- `confirm_break_play(inst, baseline, need_pts)` — polls every **2 s** for `break_confirm_sec = 15`, needs `break_confirm_pts = 1.5` (or the passed remainder).

**Optional quality filters** (all default OFF):
- `_spot_confirms(opt_type)` — NIFTY spot structure must agree. Horizontal zone → always allow. CE: if ascending support valid require `spot_close - support >= -10.0`; elif descending resist valid require `spot_close - resist >= 0` (breaking above). PE: if descending resist valid require `<= +10.0`; elif ascending support valid require `< 0` (broke below). No enabled structure or no data → don't block.
- `_volume_confirms(inst)` — needs ≥6 today bars; `cur_vol >= mean(prev 5 non-zero vols) × volume_confirm_mult (1.3)`.

**BOUNCE exits:** target = `tl_asc_top.support - target_buffer(2.0)` if the channel top is valid and above entry, else `tl.last_swing_high - 2.0`, else `None` (trailing only); switched to `None` if target <= entry. `initial_sl = tl.support - trendline_sl_buf(3.0)`; skipped entirely if `entry <= init_sl`. Trail arms at `+bounce_trail_act(5.0)` with `bounce_trail_by(4.0)` distance.

**BREAK play** (`handle_break`) — the most intricate path:
- Per-instrument cooldown: one break signal per instrument per **120 s** (`last_break_ts`).
- Spot filter is queried for the **opposite** side (`opp_type_for_spot`).
- Opposite candidate = nearest strike (`min |i.strike - broken.strike|`) among non-busy instruments of the other type.
- **Cumulative baseline:** `break_ref_ltp` is captured at the *first* break detection and reused, so gains accumulate across repeated detections; **reset after 600 s**. If `cumulative_gain >= break_confirm_pts` → enter immediately without a confirmation window; else confirm for only the `remaining` points. Baseline resets to 0 after a successful entry.
- `initial_sl = entry - break_initial_sl(5.0)`, **no target** (trailing only), trail `+4.0` / `3.0`.

**BREAKOUT exits:** no target; `init_sl = tl_desc_low.support - 3.0` if the descending channel bottom is valid and below the resistance, else `tl_resist.support - 3.0`. **HORIZ_BOUNCE:** no target; `init_sl = zone - 3.0`. Both use bounce trail params.

**`manage_trade`** (every 15 s poll): update `peak`; arm trailing when `ltp - entry >= trail_activate` (SL jumps to `ltp - trail_by`); thereafter `sl = max(sl, peak - trail_by)` (ratchets only up); exit on `ltp >= target` → `🎯 TARGET`, then on `ltp <= sl` → `🔻 TRAIL SL` if armed else `🛑 HARD SL`. `pnl = (exit - entry) × qty`, `qty = lot_size(index) × lots`.

### 5. OUTPUT CONTRACT

**(a) `.trendline_signals.json`** — the dashboard status file. Rewritten (full overwrite, `indent=2`) on: every signal state change (`CONFIRMING`/`FAILED`/`CONFIRMED`), every trade close, every ~30 s from the monitor loop (`int(time.time()) % 30 < ltp_poll_sec`), once at startup, and once per off-hours 60 s sleep. **Stats are recomputed from `logs/trade_history/trendline_{today}.jsonl` on every write.**
```json
{
  "ts": "2026-07-30 12:50:13",
  "active_trade": null,
  "signals": [],
  "stats": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
}
```
- `ts` — `"%Y-%m-%d %H:%M:%S"` (space-separated, not ISO-T).
- `active_trade` — `null`, or from the 30 s writer `{"symbol","type","entry","sl","peak"}`, or from a signal-confirm writer `{"symbol","type","entry","sl"}` (**no `peak`** — consumers must use `.get`). `type` = the `play_type`.
- `signals` — last **30** entries of `_signals_log`, appended per signal attempt. Shapes differ by type:
```json
{"ts":"12:41:07","type":"BOUNCE","status":"CONFIRMED","symbol":"NIFTY2662324000CE",
 "ltp":118.4,"support":114.85,"entry":120.9,"sl":111.85,"target":133.2,"trail_act":5.0}
{"ts":"12:44:22","type":"BREAK","status":"FAILED","broken":"NIFTY2662324000PE",
 "symbol":"NIFTY2662324000CE","ltp":96.3,"direction":"NIFTY bouncing ↑"}
{"ts":"12:51:10","type":"BREAKOUT","status":"CONFIRMED","symbol":"NIFTY2662324050PE",
 "ltp":142.0,"resistance":140.5,"entry":144.5,"sl":137.5,"target":null,"trail_act":5.0}
{"ts":"12:55:03","type":"HORIZ_BOUNCE","status":"CONFIRMING","symbol":"NIFTY2662323950CE",
 "ltp":101.2,"zone":100.4}
```
`ts` here is `HH:MM:SS` only. `type ∈ {BOUNCE, BREAK, BREAKOUT, HORIZ_BOUNCE}`; `status ∈ {CONFIRMING, FAILED, CONFIRMED}`.

Consumed by `LIVE_DASHBOARD.read_trendline_bot()` → `r["signals"]`, `r["active_trade"]`, `r["stats"]`, `r["ts"]`, and served verbatim at HTTP `GET /api/trendline_signals`.

**(b) `.trendline_chart_data.json`** — full chart payload, written by `structural_loop` every 30 s and once at startup. **Skipped entirely when no instrument has candles and spot has no bars**, deliberately preserving the last intraday snapshot for after-hours viewing.
```json
{
  "ts": "2026-07-30 12:50:13",
  "index": "NIFTY",
  "premium_min": 85, "premium_max": 200,
  "status": {
    "tl_active": 0, "total": 82, "in_range": 0,
    "near_signal": [{"symbol":"Y2662324000CE","ltp":118.4,"support":114.9,"dist":3.5,"type":"ASC"}],
    "open_trade": null,
    "spot_bars": 44, "spot_ltp": 24295.8
  },
  "instruments": [
    {"symbol": "NIFTY2662323250CE", "opt_type": "CE", "ltp": 575.15,
     "candles": [{"ts":1785383100,"o":24249.55,"h":24266.65,"l":24190.0,"c":24266.55,"v":0}],
     "trendlines": [
       {"type":"ASC_SUPPORT","color":"#00c853","p1":{"idx":28,"price":24279.45},
        "p2":{"idx":39,"price":24331.05},"projected":24349.81,"slope":4.6909}
     ]}
  ],
  "spot": {"symbol":"NIFTY","ltp":24295.8,"candles":[...],"trendlines":[...]}
}
```
- `status.near_signal` — max 8, sorted by `|dist|`, only instruments in premium range with a valid TL and `|dist| <= proximity_pts × 4`; `symbol` is truncated to the **last 12 chars**; `type ∈ {"ASC","DESC"}`.
- `status.open_trade` — `null` or `{"symbol"(last 12 ch),"type","entry","sl","ltp","peak","trail_active"}`.
- `trendlines[].type` and fixed colours: `ASC_SUPPORT #00c853`, `ASC_RESIST #69f0ae`, `DESC_RESIST #ff5252`, `DESC_SUPPORT #ff8a80`, `HORIZONTAL #ffd740`. The four rail types carry `p1`/`p2`/`projected`/`slope` (anchors are the **last two** pivots, `idx` indexes into that instrument's `candles` array); `HORIZONTAL` carries only `price`.
- Instruments with no candles **and** `ltp <= 0` are omitted.

**(c) `logs/trade_history/trendline_{YYYY-MM-DD}.jsonl`** — one line per closed trade:
```json
{"date":"2026-07-30","time_entry":"12:41:22","time_exit":"12:47:05",
 "bot":"Trendline","mode":"sim","index":"NIFTY",
 "symbol":"NIFTY2662324000CE","option":"24000CE","expiry":"2026-06-23",
 "buy_price":120.9,"sell_price":133.2,"qty":1170,"lots":18,"pnl":14391.0,
 "exit_reason":"🎯 TARGET @ ₹133.20","play_type":"BOUNCE"}
```
`bot` always `"Trendline"`; `mode ∈ {"sim","live"}`; `option` extracted by `re.search(r'(\d{4,6}(?:CE|PE))$', symbol)` falling back to the last 6 chars. Note `print_daily_summary` reads `entry_price`/`exit_price` keys that `_write_trade_log` never writes (`buy_price`/`sell_price`) — a latent bug: the summary prints ₹0.00→₹0.00.

**(d) `logs/trendline_bot/TrendlineBot_{YYYY-MM-DD_HH-MM-SS}.log`** — `logging` module, `format="%(asctime)s  %(message)s"`, `datefmt="%H:%M:%S"`, `FileHandler` + `StreamHandler(stdout)`; `urllib3` silenced to ERROR. `LIVE_DASHBOARD` scrapes a `[HH:MM:SS` timestamp from the last 20 lines for freshness.

### 6. Config knobs
Full `CONFIG` dict (defaults as shipped):
```python
{"index":"NIFTY", "exchange":"NSE", "expiry_date":"2026-06-23", "strike_step":50,
 "scan_range":20, "premium_min":85.0, "premium_max":200.0,
 "candle_interval":5, "structural_refresh":30, "ltp_poll_sec":15,
 "pivot_lookback":3, "min_pivots":2,
 "proximity_pts":6.0, "break_pts":3.0,
 "bounce_confirm_pts":2.0, "bounce_confirm_sec":25,
 "break_confirm_pts":1.5,  "break_confirm_sec":15,
 "target_buffer":2.0, "trendline_sl_buf":3.0,
 "bounce_trail_act":5.0, "bounce_trail_by":4.0,
 "break_initial_sl":5.0, "break_trail_act":4.0, "break_trail_by":3.0,
 "lots":18, "sim":True,
 "tl_ascending_enabled":True, "tl_descending_enabled":False, "tl_horizontal_enabled":False,
 "spot_confirm_enabled":False,
 "volume_confirm_enabled":False, "volume_confirm_mult":1.3,
 "pct_confirm_enabled":False, "bounce_confirm_pct":0.8,
 "device_id":"8cea1d25-588a-5eff-9699-5e7fd20a6ca9", "req_timeout":8}
LOT_SIZES = {"NIFTY":65,"BANKNIFTY":15,"FINNIFTY":40,"SENSEX":20,"BANKEX":15}
MARKET_OPEN=(9,15); MARKET_CLOSE=(15,30); IST_OFFSET=19800
```

**`trendline_config.json`** — dashboard-written override, read **once at import** (`_load_external_config()`), *not* hot-reloaded. Exactly 12 whitelisted keys are copied with **no type coercion** (values land as-is from JSON):
```json
{
  "expiry_date": "2026-06-23",
  "premium_min": 85,
  "premium_max": 200,
  "lots": 18,
  "tl_ascending_enabled": true,
  "tl_descending_enabled": true,
  "tl_horizontal_enabled": false,
  "spot_confirm_enabled": false,
  "volume_confirm_enabled": false,
  "volume_confirm_mult": 1.3,
  "pct_confirm_enabled": false,
  "bounce_confirm_pct": 1
}
```
Not overridable: `index`, `exchange`, `strike_step`, `scan_range`, `sim`, all pivot/proximity/confirm-window/trail parameters, `ltp_poll_sec`, `structural_refresh`. Also requires `ai_config.json` with `groww_api_key` + `groww_totp_secret` for index candles (without it `_refresh_spot` silently has no spot candles and `spot_confirm_enabled` degrades to always-allow).

### 7. Phantom 09:00 candle
**Not handled, and it lands squarely in the pivot engine.** `fetch_index_candles` → `filter_today` → `find_swing_lows/highs` runs on raw bars, so a phantom 09:00 index bar with a 450–770 pt wick becomes the day's extreme low *and* high. Consequences: it is picked as a pivot, `project_trendline` draws through it and produces a wildly wrong `slope`/`projected` level, the resulting `_spot_state` structure poisons `_spot_confirms` (the ±10 pt tolerances are meaningless against a 700 pt error), and the bogus anchor is exported to `.trendline_chart_data.json` and drawn on the dashboard chart. Note the phantom bar sits *before* 09:15 but `filter_today` keys only on **date**, not time, so it is not excluded. Fix: run `filter_spikes(candles, 8.0)` inside `fetch_index_candles` and additionally drop bars whose IST time is before 09:15. Option candles (`fetch_candles`, FNO endpoint) do not exhibit the artifact.

---

# 7. CONVERGENCE_SIGNAL_BOT.py

`/Users/ayush/.../CONVERGENCE_SIGNAL_BOT.py` (767 lines)

### 1. Purpose / order placement
Multi-strike convergence detector built to front-run `MOMENTUM_AUTO_BOT`: instead of watching one strike for 20 s, it batch-polls **all** ATM±6 CE and PE strikes over a 5-second window and fires when 3+ strikes on the *same* side simultaneously exceed a velocity threshold — the premise being that institutional flow hits many strikes at once, 15–30 s before the index price catches up. **Read-only — places no orders**; it only prints, WhatsApps/Telegrams, and writes `.convergence_signals.json`.

### 2. Loop cadence & market-hours gating
Documented as a 6-phase cycle: Snapshot → Observe → Score → Accel → Signal → Cooldown.
- **Hard market-hours gate** at the top of every iteration: `_in_market_hours()` compares `datetime.now()` to `market_open "09:15"` / `market_close "15:25"` (note **15:25**, 5 min earlier than the other bots). Outside → print and `sleep(60)`, `continue`.
- `_reload_override(verbose=False)` every iteration.
- Scan window `scan_seconds = 5` with `poll_sec = 1` → `n_ticks = max(5//1, 2) = 5` polls after the baseline.
- After a fired signal: `sleep(cooldown_sec = 30)`. After no signal or a throttled signal: `sleep(no_signal_wait = 2)` — so it scans roughly every 7 s.
- Spot + ATM re-fetched on `scan_count % 5 == 1` (every 5th scan). If the symbol map shrinks below 4 entries, instruments are reloaded and the map rebuilt.

### 3. Data sources
Auth `groww_token.get_access_token(API_KEY, TOTP_SECRET)` → `GrowwAPI`. Alerts via `whatsapp_gateway.send_whatsapp` aliased `send_telegram`.

- **Batch LTP (the core efficiency win):** `GET https://api.groww.in/v1/live-data/ltp?segment=FNO&exchange_symbols=S1&exchange_symbols=S2&…` — repeated `exchange_symbols` params, **chunked at 50 symbols per call**, timeout 8 s. Handles `401` → `groww_init()` re-mint + retry once; `429` → `sleep(3)` and skip the chunk. Returns `{exchange_symbol: float}` from `payload`.
- Spot: `GET https://api.groww.in/v1/live-data/ltp?segment=CASH&exchange_symbols={NSE_NIFTY|BSE_SENSEX|…}` → `payload[sym]`, 0.0 on failure.
- Instruments: local `instrument.csv` only (never downloaded). Filtered to `underlying_symbol == index`, `expiry_date == expiry`, and strike within **2×** `atm_range × step` of ATM (double buffer so spot can drift without a reload).
- `oi_snapshot.json` (from `calculate_oi_pcr.py`) — rejected if `time.time() - snap["timestamp"] > oi_max_age_sec (180)`.

Index maps: `_INDEX_SPOT_SYMBOL` (NIFTY→`NSE_NIFTY`, BANKNIFTY→`NSE_BANKNIFTY`, FINNIFTY→`NSE_FINNIFTY`, SENSEX→`BSE_SENSEX`, BANKEX→`BSE_BANKEX`), `_INDEX_EXCHANGE` (NSE/NSE/NSE/BSE/BSE), `_INDEX_STRIKE_STEP` (50/100/50/100/100).

Symbol map (`_build_symbol_map`) → `{f"{exch}_{ts}": {"strike","opt_type","ts"}}` for `|strike - atm| <= atm_range × step`, using `internal_trading_symbol` or `trading_symbol`. ATM±6 → up to 26 symbols (13 CE + 13 PE); docstring says 24.

Default expiry `_next_expiry(weekday=1)` = next Tuesday.

### 4. Core algorithm

**Per-symbol velocity** over the window (`tick_data[sym] = deque(maxlen=n_ticks+2)` of `(t, ltp)`):
```
baseline snapshot at t0, then n_ticks polls at 1 s
vel = (last_ltp - first_ltp) / first_ltp * 100          # % over ~5 s
premium filter: skip unless min_premium(30) <= first_ltp <= max_premium(500)
hit if vel >= velocity_pct (0.8)
```
Live per-tick console counters use the same `vel >= thresh` rule but *without* the premium filter.

**Acceleration** (`acceleration_mode = True`, needs ≥4 ticks): split the tick list at `mid = len//2`;
```
v1 = (h1[-1] - h1[0]) / h1[0] * 100
v2 = (h2[-1] - h2[0]) / h2[0] * 100
accelerating = (v1 > 0 and v2 >= v1 * accel_ratio (1.3))
```

**OI bias** `oi_bias(snap)`: if `writer_bias == sentiment` use it; else `pcr_atm > 1.1 → BULLISH`, `< 0.9 → BEARISH`, else `writer_bias`. `NEUTRAL` when the snapshot is missing/stale.

**Convergence voting:**
```
ce_count = len(ce_hits) ; pe_count = len(pe_hits)          # hits sorted by vel desc

# OI bias lowers the bar by 1 for the aligned side, floored at 2
ce_min = max(min_convergence(3) - (1 if bias=="BULLISH" else 0), 2)
pe_min = max(min_convergence(3) - (1 if bias=="BEARISH" else 0), 2)

# acceleration bonus: 2+ accelerating strikes on a side counts as one extra strike
ce_eff = ce_count + (1 if ce_accel_cnt >= 2 else 0)
pe_eff = pe_count + (1 if pe_accel_cnt >= 2 else 0)

side = "CE"   if ce_eff >= ce_min and ce_eff >  pe_eff
     = "PE"   if pe_eff >= pe_min and pe_eff >  ce_eff
     = "BOTH" if ce_eff >= ce_min and pe_eff >= pe_min      # tie, both qualify
     = None   otherwise → no signal

strength = "STRONG" if (len(hits) >= min_convergence + 1 or accel_total >= 2) else "MODERATE"
avg_vel_pct = mean(vel_pct of the signal's hits)
top_strike  = hits[0].strike            # highest velocity
```
For `BOTH`, `signal_hits = ce_hits + pe_hits` (so `conv_count` and `avg_vel_pct` span both sides).

**Throttle** `_last_signal_ts = {"CE":0,"PE":0,"BOTH":0}` — a side is suppressed if its last signal was within `min_signal_interval_sec = 60`. A throttled signal is **not** written to the JSON and **not** counted in `signal_count`.

**Alert text** `_fmt_signal()`: icon `🔴⚡` for CE, `🟢⚡` for PE, `⚡⚡` for BOTH (note the inverted colour convention — CE is red here); header `{icon} [{strength}] CONVERGENCE — {side}`; lines for converged count + avg velocity, an `Accelerating : N strikes 🚀` line only when `accel_count >= 2`, top strike + spot, OI bias, then up to 6 active strikes formatted `{strike}{opt_type} {vel:+.1f}%{🚀 if accelerating}`, then time.

### 5. OUTPUT CONTRACT

**(a) `.convergence_signals.json`** — the dashboard file. Rewritten in full (`indent=2`) **only when a non-throttled signal fires**; `_signal_history` is in-memory and grows for the process lifetime, so a restart resets `total` and truncates history.
```json
{
  "updated": "2026-06-25 11:36:40",
  "total": 41,
  "signals": [ /* last 100 */ ]
}
```
`updated` = `"%Y-%m-%d %H:%M:%S"`. `total` = **lifetime count since process start**, not `len(signals)` (which is capped at 100 by `_signal_history[-100:]`). Each signal object (real sample from disk):
```json
{
  "time":        "2026-06-25 11:36:40",
  "ts_ms":       1782367600057,
  "side":        "PE",
  "strength":    "STRONG",
  "conv_count":  4,
  "accel_count": 2,
  "avg_vel_pct": 0.954,
  "top_strike":  24150.0,
  "spot":        24189.0,
  "oi_bias":     "NEUTRAL",
  "vel_thresh":  0.8,
  "scan_secs":   5,
  "hits": [
    {"sym":"NSE_NIFTY26JUN24150PE","strike":24150.0,"opt_type":"PE",
     "vel_pct":1.1,"ltp_start":86.35,"ltp_end":87.3,"accelerating":false},
    {"sym":"NSE_NIFTY26JUN24250PE","strike":24250.0,"opt_type":"PE",
     "vel_pct":1.01,"ltp_start":133.6,"ltp_end":134.95,"accelerating":false},
    {"sym":"NSE_NIFTY26JUN24100PE","strike":24100.0,"opt_type":"PE",
     "vel_pct":0.873,"ltp_start":68.7,"ltp_end":69.3,"accelerating":true},
    {"sym":"NSE_NIFTY26JUN24200PE","strike":24200.0,"opt_type":"PE",
     "vel_pct":0.834,"ltp_start":107.95,"ltp_end":108.85,"accelerating":true}
  ]
}
```
Types/domains: `side ∈ {"CE","PE","BOTH"}`; `strength ∈ {"STRONG","MODERATE"}`; `oi_bias ∈ {"BULLISH","BEARISH","NEUTRAL"}`; `ts_ms = int(time.time()*1000)`; `strike`/`top_strike` are **floats**; `vel_pct` 3dp, `ltp_start`/`ltp_end` 2dp, `avg_vel_pct` 3dp; `hits` truncated to the **top 8** by velocity; `vel_thresh` and `scan_secs` echo the config in force at scan time (so historical signals are self-describing after a config change).

Consumed by `LIVE_DASHBOARD._read_conv_signals()` → `data.get("signals", [])[-5:]`, fed into the AI-brain prompt.

**(b) `logs/convergence_bot/Convergence_Bot_{YYYY-MM-DD_HH-MM-SS}.log`** — `sys.stdout` **and** `sys.stderr` replaced by a `Tee` (ANSI preserved). Console timestamps are millisecond-precision `HH:MM:SS.mmm` via `_ts()`.

### 6. Config knobs
```python
CONFIG = {
  "index": "NIFTY", "expiry": _next_expiry(weekday=1), "strike_step": 50,
  "atm_range": 6,               # ATM ± 6 strikes → up to 26 symbols
  "scan_seconds": 5, "poll_sec": 1,
  "velocity_pct": 0.8,          # min % move per strike to count as active
  "min_convergence": 3,         # strikes needed on the same side
  "min_premium": 30, "max_premium": 500,
  "acceleration_mode": True, "accel_ratio": 1.3,
  "use_oi_filter": True, "oi_max_age_sec": 180,
  "market_open": "09:15", "market_close": "15:25",
  "cooldown_sec": 30, "no_signal_wait": 2,
  "min_signal_interval_sec": 60,
}
```
**`convergence_config_override.json`** — same pattern as the momentum bot, re-read **every loop iteration** (hot). Only `_OVERRIDE_CAST` keys are applied, each coerced; failures skipped silently. File does not currently exist on disk. Full schema:
```json
{
  "index": "NIFTY",              "expiry": "2026-08-05",
  "atm_range": 6,                "scan_seconds": 5,
  "poll_sec": 1,                 "velocity_pct": 0.8,
  "min_convergence": 3,          "min_premium": 30.0,
  "max_premium": 500.0,          "acceleration_mode": true,
  "accel_ratio": 1.3,            "use_oi_filter": true,
  "oi_max_age_sec": 180,         "cooldown_sec": 30,
  "min_signal_interval_sec": 60
}
```
Casts: `index`/`expiry` `str`; `atm_range`/`scan_seconds`/`poll_sec`/`min_convergence`/`oi_max_age_sec`/`cooldown_sec`/`min_signal_interval_sec` `int`; `velocity_pct`/`min_premium`/`max_premium`/`accel_ratio` `float`; `acceleration_mode`/`use_oi_filter` `bool` (same caveat: any non-empty string → `True`). **Not** overridable: `strike_step` (auto-derived from index anyway), `market_open`, `market_close`, `no_signal_wait`.

### 7. Phantom 09:00 candle
**Not applicable.** This bot fetches **no candles at all** — it is purely LTP-tick based, so the index-feed phantom bar cannot reach it. The one indirect exposure is `oi_snapshot.json`, produced by `calculate_oi_pcr.py`; if that upstream file were candle-derived the bias could be skewed, but it is OI/PCR-based.

---

# Briefly: SIGNAL_ANALYZER.py

`/Users/ayush/.../SIGNAL_ANALYZER.py` (800 lines)

**Purpose.** Offline, zero-API self-review and **auto-tuning** engine. It scrapes the last 7 days of bot logs, retroactively scores whether each CE/PE signal actually moved spot the right way, aggregates win-rates by direction / zone / pattern / confidence band / hour, then computes corrective parameters and writes them where `MASTER_SIGNAL_BOT` will pick them up on its next cycle. Interactive: renders a report, then waits on `input()` to re-run.

**Inputs (all read-only, log parsing only):**
- `logs/master_signal/Master_Signal_*.log` — JSONL, `days_back=7`; requires `ts` + `direction` + `spot`; richest source, preferred in the merge.
- `logs/fibo_analyzer/Fibo_Analyzer_*.log` — text, regex-scraped per `🔄 Analysis cycle` block for `Spot`, `15m score:`, `Pattern`, `pos:`, `→ CE|PE|WAIT`.
- `logs/premium_tracker/Premium_Tracker_*.log` — text, regex-scraped for `(ts, spot, ce_ltp, pe_ltp)` tick tuples, used for premium-correlation scoring.
- `BOT_TUNING.json` — previous tuning, loaded to render a before/after diff.

Merge rule: master records win; fibo records are added only when no master record shares the same `ts[:15]` (minute-level key).

**Outputs:**
- **`BOT_TUNING.json`** (overwritten; the live control surface for `MASTER_SIGNAL_BOT`) — schema exactly as shown in §6 of spec 1: `generated_at`, `generated_by: "SIGNAL_ANALYZER"`, `confidence_threshold`, `excluded_zones[]`, `excluded_patterns[]`, `ce_multiplier`, `pe_multiplier`, `notes[]`.
- **`logs/analysis/Signal_Analysis_{YYYY-MM-DD_HH-MM-SS}.json`** — `{generated_at, metrics, bot_health, tuning_applied, changes[], recent_signals[last 20 with outcomes]}`.

**Tuning thresholds** (worth preserving): `OUTCOME_WIN_MIN=10`/`MAX=25`, `SPOT_THRESH_PCT=0.05`; `MIN_ZONE_SAMPLES=MIN_PATTERN_SAMPLES=4`; `BAD_ZONE_WR=BAD_PATTERN_WR=35`; `CE_PENALTY_BELOW=PE_PENALTY_BELOW=45`; `GOOD_CONF_THRESHOLD=65`, `HIGH_CONF_THRESHOLD=72`, `MAX_THRESHOLD=80`. Rules: `<5` signals → keep defaults; 65–74% band WR `<45` with `n>=3` → raise threshold to 75; overall WR `>=65` with `n>=10` → allow 65; block any zone/pattern with `n>=4` and WR `<=35` (patterns `NONE`/`Normal`/`Doji` exempt); direction multiplier `round(max(0.65, wr/65), 2)` when `n>=5` and WR `<45`.

# Briefly: PERSONAL_TRADING_AI.py

`/Users/ayush/.../PERSONAL_TRADING_AI.py` (1211 lines)

**Purpose.** A "should I trade today?" advisor, orthogonal to the signal bots. It parses 3+ years of the user's own F&O trade history, scores today's live market conditions, finds historically similar days and how the user actually performed on them, runs a behavioural risk analysis (revenge trading, expiry-day behaviour, streaks), and outputs a single trading-permission score plus verdict — then has Claude write the narrative.

**Inputs:**
- `ayush_previous_data/` — historical broker/P&L exports (`parse_excel_history` → daily P&L, trade counts, expiry days).
- `Lakshmi.xlsx` — intraday-detailed trade log (`parse_lakshmi_intraday`).
- `^NSEI` + `^INDIAVIX` daily history from **yfinance** (`start="2023-04-01"`); optional import, degrades gracefully.
- **NSE** live endpoints via `fetch_live_market()` → VIX, NIFTY, PCR.
- `ai_config.json` — `ANTHROPIC_API_KEY` per the docstring; the actual implementation shells out to the **`claude` CLI** (`shutil.which("claude")` → `[claude_bin, "-p", prompt]`), falling back to `"(claude CLI not found — install Claude Code to enable AI narrative)"`.
- CLI flag `--refresh` forces a market-DB rebuild.

**Outputs:**
- **`.trading_ai_cache.json`** — the only file written: `{"built_at": <epoch float>, "records": [{"date":"2023-04-03","nifty_open":..,"nifty_close":..,"nifty_prev":..,"gap_pct":..,"trend_5d":..,"dow":0..4,"vix":..}, ...]}`. TTL **43200 s (12 h)**; reused only if `built_at` is fresh *and* `records` is non-empty.
- Everything else is terminal-only (`display_header`, `display_live_market`, `display_personal_stats`, `display_similar_days`, `display_behavioral`, `display_permission_score`, `display_ai_narrative`, `display_footer`). No JSON contract is exposed to `LIVE_DASHBOARD` or any other bot.

---

## Cross-cutting notes for the rebuild

**Files the dashboard actually reads — there is no `.master_signal.json`, `.fibo_trend.json`, `.chart_levels.json`, or `.premium_direction.json`.** Only two of these seven bots publish a dot-JSON status file (`.trendline_signals.json` + `.trendline_chart_data.json`, and `.convergence_signals.json`). The other five publish **log files whose text or JSONL format is the contract**, which is the fragile part of the current design:

| Producer | Artifact | Format | Consumer |
|---|---|---|---|
| MASTER_SIGNAL_BOT | `logs/master_signal/Master_Signal_*.log` | JSONL, 1/60 s | CHART_LEVEL_ANALYZER (≤300 s), SIGNAL_ANALYZER |
| FIBONACCI_TREND_ANALYZER | `logs/fibo_analyzer/Fibo_Analyzer_*.log` | ANSI-stripped text | SIGNAL_ANALYZER (**regex**) |
| CHART_LEVEL_ANALYZER | `logs/chart_level/signals_*.jsonl`, `live_chain.json` | JSONL + JSON | dashboard |
| PREMIUM_DIRECTION_TRACKER | `logs/premium_tracker/Premium_Tracker_*.log` | ANSI-stripped text | SIGNAL_ANALYZER (**regex**) |
| MOMENTUM_AUTO_BOT | `logs/momentum_bot/*.log`, `logs/trade_history/{date}.jsonl`, `Lakshmi.xlsx` | text + JSONL + xlsx | dashboard (log regex) |
| TRENDLINE_SCANNER_BOT | `.trendline_signals.json`, `.trendline_chart_data.json`, `logs/trade_history/trendline_{date}.jsonl` | JSON | dashboard `/api/trendline_signals` |
| CONVERGENCE_SIGNAL_BOT | `.convergence_signals.json` | JSON | dashboard AI brain |
| SIGNAL_ANALYZER | `BOT_TUNING.json`, `logs/analysis/*.json` | JSON | MASTER_SIGNAL_BOT (hot) |

`logs/trade_history/` is shared: `{date}.jsonl` (`bot:"Auto"`, momentum) vs `trendline_{date}.jsonl` (`bot:"Trendline"`) — different filenames *and* different key sets (`exit_reason`/`oi_verdict_tag` vs `play_type`/`option`).

**Config-override hot-reload differs per bot** — momentum and convergence re-read every cycle; `MASTER_SIGNAL_BOT` re-reads `BOT_TUNING.json` on mtime change; `TRENDLINE_SCANNER_BOT` reads `trendline_config.json` **once at import** (a UI change requires a restart); the three analyzer bots have no config file at all.

**Two candle API families.** Six bots use the authenticated `growwapi` library (`get_historical_candles(segment="CASH"|SEGMENT_FNO)`); `TRENDLINE_SCANNER_BOT` alone uses the public `groww.in/v1/api/...charting_service` web endpoints for options plus an authenticated `charting_service/v4` call for index candles with a token from `ai_config.json`.

**Phantom 09:00 candle: handled in zero of the seven.** The only working implementation in the repo is `KEY_LEVELS_TERMINAL.filter_spikes(candles, mult=8.0)` — drop bars whose `high-low` exceeds `8 ×` the median bar range (guarded by `len >= 5` and `median > 0`), reporting the dropped count. Insertion points, in impact order: `FIBONACCI_TREND_ANALYZER.fetch_candles` (poisons the day-fib grid, all confluence zones, and every setup target), `PREMIUM_DIRECTION_TRACKER._refresh_day_hl` + `_fetch_candles_fib` (poisons `_day_hl` and therefore the day-break Telegram alerts and 61.8% extension targets all session), `TRENDLINE_SCANNER_BOT.fetch_index_candles` (phantom becomes a pivot → garbage slope/projection → `_spot_confirms` and the dashboard chart), `CHART_LEVEL_ANALYZER.fetch_candles` (phantom becomes a swing level and, because `calc_vwap` weights by `high-low`, dominates VWAP), `MASTER_SIGNAL_BOT.fetch_candles` (corrupts `detect_swing` → `fib_score` → confidence). `CHART_LEVEL_ANALYZER.calc_opening_range` additionally needs its filter tightened from `hour==9 and minute<=30` to `hour==9 and 15<=minute<=30`, and `TRENDLINE_SCANNER_BOT.filter_today` keys on date only, so a pre-09:15 bar is not excluded there either. `MOMENTUM_AUTO_BOT` and `CONVERGENCE_SIGNAL_BOT` are unaffected (no index candles).

Secondary issues worth fixing during a rebuild: credentials (JWT `API_KEY`, `TOTP_SECRET`, Telegram `BOT_TOKEN`/`CHAT_ID`) are hardcoded in all seven files and should move to gitignored `ai_config.json`; `MOMENTUM_AUTO_BOT` has **no market-hours gate** and its `max_trades_day: 5` is never enforced; `MOMENTUM_AUTO_BOT._get_ltp` hardcodes the `NSE_` exchange prefix so SENSEX options break; `TRENDLINE_SCANNER_BOT.print_daily_summary` reads `entry_price`/`exit_price` keys that `_write_trade_log` writes as `buy_price`/`sell_price`; `TRENDLINE_SCANNER_BOT` builds its 82-instrument watch list once and never rebuilds it as spot drifts; and both override loaders cast with `bool(...)`, so any non-empty JSON string becomes `True`.
