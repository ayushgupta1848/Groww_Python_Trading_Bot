# Support Infrastructure, Configs & Data Files — Rebuild Spec

> Part of the end-to-end rebuild documentation. Master document: ../../REBUILD_BLUEPRINT.md
> Generated 2026-08-04 from a full code survey. Treat all constants, filenames,
> JSON keys and printed strings here as EXACT contracts.

---


---

# REBUILD SPECS — Support Infrastructure
Repo root: `/Users/ayush/Documents/eclipse-workspace/Groww_Python_Trading_Bot-main/`

---

## 1. `groww_token.py` — shared cross-process Groww access-token cache

**Why:** `POST /v1/token/api/access` is rate-limited (`growwapi.groww.exceptions.GrowwAPIRateLimitException`). One token is minted per machine and shared by all bots.

**Module constants**
```python
_DIR              = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH        = os.path.join(_DIR, ".groww_token.json")
_LOCK_PATH        = CACHE_PATH + ".lock"          # ".groww_token.json.lock"
SAFETY_MARGIN_SEC = 15 * 60      # 900  — refresh this early
FALLBACK_TTL_SEC  = 6 * 3600     # 21600 — used when JWT has no readable exp
_BACKOFF          = (20, 45, 90, 180)   # seconds, rate-limit retry schedule
_LOCK_STALE_SEC   = 120          # lock older than this is deleted as dead
```
Temp file during write: `CACHE_PATH + ".tmp"` → `.groww_token.json.tmp`

**Cache file schema — `.groww_token.json`** (3 keys, mode `0o600`)
```json
{"token": "<JWT str>", "expiry": 1785889800.0, "written_at": 1785822837.8606951}
```
- `expiry` = `_jwt_expiry(token)` or `time.time() + FALLBACK_TTL_SEC`
- written via `json.dump` to `.tmp`, then `os.replace(tmp, CACHE_PATH)` (atomic), then `os.chmod(CACHE_PATH, 0o600)`. All exceptions swallowed.

**`_jwt_expiry(token) -> float`** — unverified JWT parse, `0.0` on any failure:
```python
payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)        # restore base64 padding
exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
return float(exp) if exp else 0.0
```

**`_read_cache() -> str | None`** — returns token only if `token and time.time() < expiry - SAFETY_MARGIN_SEC`; any exception → `None`.

**Lock protocol**
- `_acquire_lock() -> int | None`: if `time.time() - os.path.getmtime(_LOCK_PATH) > _LOCK_STALE_SEC` → `os.unlink`. Then `os.open(_LOCK_PATH, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)`; `FileExistsError`/`OSError` → `None`.
- `_release_lock(fd)`: `os.close(fd)` + `os.unlink(_LOCK_PATH)`.

**Public API**
```python
def get_access_token(api_key: str, totp_secret: str,
                     force_refresh: bool = False,
                     verbose: bool = True) -> str
def init_client(api_key, totp_secret, force_refresh=False, verbose=True) -> tuple  # (GrowwAPI(token), token)
def clear_cache() -> None       # unlinks CACHE_PATH and _LOCK_PATH
```
`get_access_token` algorithm:
1. Unless `force_refresh`: `_read_cache()` → return (prints `🔑 Groww token: reusing cached token (valid ~N min)`).
2. `_acquire_lock()`. If `None` (another process minting): loop 30 times — `time.sleep(2)`, `_read_cache()` → return if found (`🔑 Groww token: picked up token minted by another bot`); `break` if `_LOCK_PATH` disappeared. Then `_acquire_lock()` again to take over.
3. `for attempt, wait in enumerate((0,) + _BACKOFF):` → i.e. 5 attempts, waits 0/20/45/90/180. If `wait`: print `⏳ Groww token rate-limited — retrying in {wait}s (attempt {n}/5)`, sleep.
   - `totp = pyotp.TOTP(totp_secret).now()`; `token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)`; `_write_cache(token)`; return (`🔑 Groww token: minted a fresh token and cached it`).
   - On exception: if `"RateLimit" not in type(exc).__name__` → **re-raise immediately**. Else `_read_cache()` (a sibling may have won) → return if present.
4. After loop: `raise last_err`. `finally: _release_lock(lock_fd)`.

**CLI (`__main__`)**
- `--clear` → `clear_cache()`, print `🗑️  Token cache cleared.`, exit 0
- `--refresh` → `import CHART_LEVEL_ANALYZER as _cla; get_access_token(_cla.API_KEY, _cla.TOTP_SECRET, force_refresh=True)`, exit 0
- no args → `✅ Cached token valid for ~N more min  (<CACHE_PATH>)` or `❌ No valid cached token — the next bot you start will mint one.`

**Callers:** `KEY_LEVELS_TERMINAL.init_groww`, `LIVE_DASHBOARD._get_ltp_token`, `TRADE_CONTROL_PANEL.get_token`, `CHART_LEVEL_ANALYZER`, others. `.groww_token.json`, `.groww_token.json.lock`, `.groww_token.json.tmp` are all gitignored.

---

## 2. `whatsapp_gateway.py`

**Purpose:** drop-in replacement for the legacy `send_telegram()` pattern — outbound alerts via Twilio WhatsApp + an inbound 2-way command channel. Imported as `from whatsapp_gateway import send_whatsapp as send_telegram, start_webhook_server` by `PROD10FEB_ManualBOT…py:112`, `MOMENTUM_AUTO_BOT.py:98`, `CONVERGENCE_SIGNAL_BOT.py:93`, and `from whatsapp_gateway import send_whatsapp, start_webhook_server` by `FIBONACCI_TREND_ANALYZER.py:202`.

**Config — environment variables only (no ai_config.json)**
```
TWILIO_ACCOUNT_SID   default ""
TWILIO_AUTH_TOKEN    default ""
TWILIO_WA_FROM       default "whatsapp:+14155238886"   (Twilio sandbox)
WHATSAPP_TO          default "whatsapp:+916012308856"
```
`_BASE_DIR = dirname(abspath(__file__))`; `_CONTROL_FILE = _BASE_DIR/".wa_control.json"`; `_server_lock = threading.Lock()`; `_server_started = False`.

**Outbound — `send_whatsapp(message: str) -> None`**
Spawns `threading.Thread(target=_send, daemon=True)` (fire-and-forget, never blocks trading). If SID or token empty → prints `⚠️ WhatsApp not configured — message dropped: {message[:60]}`. Otherwise `requests.post("https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json", auth=(SID, TOKEN), data={"From":TWILIO_WA_FROM,"To":WHATSAPP_TO,"Body":message}, timeout=10)`.

**IPC control file `.wa_control.json`** (written by webhook, polled by bots)
```json
{"command": "PAUSE", "timestamp": "2026-08-04 13:32:17", "processed": false}
```
- `get_pending_command() -> Optional[str]` — returns `data["command"].upper()` only when `data.get("processed", True)` is falsy, else `None`.
- `mark_command_processed(ack: str = "")` — sets `processed = True`, rewrites with `indent=2`, optionally `send_whatsapp(ack)`.
- `_write_command(cmd)` — timestamp format `"%Y-%m-%d %H:%M:%S"`, `processed: False`, `indent=2`.

**Endpoints (Flask app `_flask_app = Flask(__name__)`)**
| Method | Path | Behaviour |
|---|---|---|
| POST | `/whatsapp` | Twilio webhook. Reads `request.form["Body"]`, `.strip().upper()`, returns TwiML `Response(xml, mimetype="application/xml")` |
| GET | `/health` | `({"status": "ok", "service": "whatsapp-gateway"}, 200)` |

**Command handling `_handle_command(cmd)`** — returns
`'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe_body}</Message></Response>'`
with `&`,`<`,`>` escaped.
- `HELP` → help text listing STATUS / PAUSE / RESUME / STOP / HELP
- `STATUS | PAUSE | RESUME | STOP` → `_write_command(cmd)`, body `✅ Command *{cmd}* queued — bot will acknowledge shortly.`
- anything else → `Unknown command '{cmd}'. Send HELP to see options.`

**`start_webhook_server(port: int = 5055) -> None`** — idempotent under `_server_lock` (only first call starts); background daemon thread named `wa-webhook`; sets `logging.getLogger("werkzeug")` to `ERROR`; `_flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)`. Prints ngrok/Twilio setup hints.

**Setup chain:** Twilio sandbox → `ngrok http 5055` → Twilio Sandbox Settings webhook `https://<ngrok-id>.ngrok.io/whatsapp`.

---

## 3. `KEY_LEVELS_TERMINAL.py` + `pine_script/*.pine`

### `KEY_LEVELS_TERMINAL.py` (487 lines)
**Purpose:** terminal-only companion to `pine_script/key_levels.pine`. Prints for one index: (a) previous-day H/L/C/O (the "orange lines"), (b) multi-touch S/R levels ranked by touch count (the "green lines"), (c) moving indicators mirroring `pine_script/indicator.pine`.

**Credentials:** `API_KEY` (hardcoded multi-line JWT) and `TOTP_SECRET = "SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4"` at module top; `_session = requests.Session()`. Auth: `init_groww()` → `get_cached_access_token(API_KEY, TOTP_SECRET)` then `GrowwAPI(access_token)`.

**Index symbol map `_index_symbols(groww, index_name)`**
| Index | Exchange | Candidate `groww_symbol`s (tried in order) |
|---|---|---|
| NIFTY | `EXCHANGE_NSE` | `NSE-NIFTY 50`, `NSE-NIFTY` |
| SENSEX | `EXCHANGE_BSE` | `BSE-SENSEX`, `BSE-S&P BSE SENSEX` |
| BANKNIFTY | `EXCHANGE_NSE` | `NSE-NIFTY BANK`, `NSE-BANKNIFTY` |
| FINNIFTY | `EXCHANGE_NSE` | `NSE-NIFTY FIN SERVICE` |
| other | `EXCHANGE_NSE` | `NSE-{IDX}` |

**Data**
- `fetch_candles(groww, index, interval, days_back)` → `groww.get_historical_candles(groww_symbol, exchange, segment="CASH", start_time/end_time "%Y-%m-%d %H:%M:%S", candle_interval=interval)`; accepts result only if `>= 3` candles; maps `[ts, o, h, l, c, v?]` → `{"ts","open","high","low","close","volume"}`.
- `get_spot(index, access_token, fallback)` → `GET https://api.groww.in/v1/live-data/ltp?segment=CASH&exchange_symbols={BSE|NSE}_{IDX}`, headers `Accept: application/json`, `Authorization: Bearer <tok>`, `X-API-VERSION: 1.0`, `timeout=6`; takes first value of `payload`; falls back to last candle close.
- `_ts_to_dt(ts)` handles both epoch-ms ints (intraday) and ISO strings (daily).

**Algorithm**
1. `intraday = fetch_candles(index, args.interval, args.days)`; `daily = fetch_candles(index, "1day", max(days,7))`.
2. `filter_spikes(candles, mult)` — computes median bar range; drops candles with `high-low > median*mult`. Removes Groww's phantom 09:00 index candle (450–770pt fake wick vs ~17pt median). Returns `(clean, dropped_count)`.
3. `pdo = prev_day_from_intraday(intraday) or prev_day_ohlc(daily)`. Intraday aggregation is preferred because the daily feed's OPEN is unreliable (Groww reports prior close as open): `open = bars[0].open`, `high = max(high)`, `low = min(low)`, `close = bars[-1].close` for `prev_date = max(dates < today)`.
4. `tol = ref*tol_pct/100` (`tol_mode="pct"`) or `tol_pts`; `ref = spot or last_close or 0.0`.
5. `find_pivots(candles, left, right)` — a bar is a pivot high if all bars in `[i-left, i+right]` have `high <= hi`; pivot low symmetric on `low`. Both appended to one flat price list.
6. `cluster_levels(pivots, tol)` — nearest-band merge; `price` becomes a running average `(price*touches + p)/(touches+1)`, `touches += 1`; else new `{"price","touches"}`.
7. Indicators (unless `--no-indicators`): `ema100`, `hull_ma`, `vwap_session`.

**Indicator math (mirrors `indicator.pine`)**
- `_ema_series(vals, length)`: `k = 2/(length+1)`, seed `vals[0]`.
- `ema100(candles)` → `_ema_series(closes, 100)[-1]`; needs ≥20 closes. ("N-Line", purple)
- `hull_ma(candles)`: `length = 16`; `e_half = ema(closes, 8)`, `e_full = ema(closes, 16)`, `raw = 2*e_half - e_full`, `hull = ema(raw, round(16**0.5)) = ema(raw, 4)`. Returns `{"mhull": hull[-1], "shull": hull[-3], "up": mhull > shull}`.
- `vwap_session(intraday)`: last day only; `tp = (h+l+c)/3`. If all volumes present and non-zero → volume-weighted VWAP + weighted variance; else unweighted mean (flagged `weighted=False`, labelled "no volume in index feed — typical-price avg"). Returns `{"vwap","upper"(+1σ),"lower"(-1σ),"weighted","date"}`.

**Output (`render`)** — ANSI-coloured terminal only, no files written:
- Header `══════════ KEY LEVELS · {INDEX} · {interval} ══════════`, timestamp + Spot
- `▶ MOVING INDICATORS` — N-Line/EMA100, Hull MHULL, Hull SHULL (+trend UP▲/DOWN▼), VWAP session, VWAP upper 1σ, VWAP lower 1σ, each with `▲/▼ ±diff (±pct%)` distance from spot
- `▶ PREVIOUS DAY LEVELS` — Prev Day HIGH / LOW / CLOSE / OPEN (orange, `\033[38;5;208m`) + date
- `▶ MULTI-TOUCH S/R  (>= N touches)` — table `LEVEL | ROLE | TOUCHES | DISTANCE`, sorted by price descending; ROLE = RESISTANCE if `price > spot` else SUPPORT; stars `"★" * min(touches, 10)`; colour GREEN if `touches >= 4`, YELLOW if `== 3`, GREY otherwise.

**CLI flags / defaults**
| Flag | Type | Default |
|---|---|---|
`--index` | str | `NIFTY` |
`--interval` | str | `5minute` (`1minute\|5minute\|15minute\|1hour`) |
`--days` | int | `7` |
`--left` | int | `10` |
`--right` | int | `10` |
`--min-touches` | int | `2` |
`--tol-mode` | `pct\|pts` | `pct` |
`--tol-pct` | float | `0.15` |
`--tol-pts` | float | `20.0` |
`--spike-mult` | float | `8.0` |
`--no-spike-filter` | flag | off |
`--no-indicators` | flag | off |
`--watch` | int | `0` (0 = run once, else refresh every N sec, `KeyboardInterrupt` → `stopped.`) |

### `pine_script/indicator.pine` (106 lines, `//@version=4`, `study("TW All in One", overlay=true)`)
Purpose: the master TradingView chart indicator the Python bots are ported from.
- Hull band: `modeSwitch="Ehma"`, `length=16`, `EHMA(src,L)=ema(2*ema(src,L)-ema(src,L), round(sqrt(L)))`; `MHULL=HULL[0]`, `SHULL=HULL[2]`; colour blue `rgb(0,24,243)` when `HULL > HULL[2]` else red `rgb(255,75,75)`; filled band between Band 1/Band 2. Buy/Sell triangles on `crossunder/crossover(SHULL, MHULL)`.
- `ema100 = ema(close, 100)` plotted as "N-Line", purple, linewidth 4.
- "Target & Stop Loss": `left=33`, `right=21`, `quick_right=3`, `src_auto_sr="Close"`; `pivothigh/pivotlow` + `valuewhen(..., occurrence 0..5)` produce `level1…level14`, each plotted green when `close >= level` else red ("Target Stoploss Line 1..14").
- **Identical copy** at `trading_decision_engine/reference/tw_all_in_one_indicator.pine` (whitespace-only diff) — the authoritative source for `SupportResistanceEngine`.

### `pine_script/key_levels.pine` (158 lines, `//@version=5`)
`indicator("Key Levels — PDH/PDL + Multi-Touch S/R", overlay=true, max_lines_count=500, max_labels_count=500)`. The Pine twin of `KEY_LEVELS_TERMINAL.py`.
- Inputs group `"Previous Day Levels"`: `showPDH=true`, `showPDC=true`, `showPDO=false`, `pdColHL=orange`, `pdColC=gray(20)`, `pdWidth=2`. Levels via `request.security(syminfo.tickerid, "D", high[1]/low[1]/close[1]/open[1], lookahead=barmerge.lookahead_on)`, `plot.style_linebr`, plus right-edge `PDH`/`PDL` labels on `barstate.islast`.
- Inputs group `"Multi-Touch S/R"`: `showSR=true`, `pivLeft=10`, `pivRight=10`, `minTouches=2`, `tolMode="Percent"|"Points"`, `tolPct=0.15`, `tolPts=20.0`, `maxLevels=12`, `srColor=green`, `showTouchLbl=true`, `extendRight=true`.
- State arrays `lvlPrice/lvlTouches/lvlBarSeen/lvlLine/lvlLabel`; `f_tol(p)`; `f_register(price)` nearest-band merge with running average (same formula as Python); `ta.pivothigh(high,pivLeft,pivRight)` / `ta.pivotlow(low,…)`; `f_prune()` drops weakest-by-touches (tie-break oldest) beyond `maxLevels`; thicker/more-opaque lines for more touches.

---

## 4. `TRADE_CONTROL_PANEL.py` (brief)

- Port/host: `HOST = "127.0.0.1"` (local-only — page embeds live tokens), `PORT = int(os.environ.get("TCP_PORT", "8790"))`, `API = "https://api.groww.in"`, `ThreadingHTTPServer`, stdlib only + `requests`. Auto-started by `LIVE_DASHBOARD._ensure_control_panel()` (socket probe on 8790, else `subprocess.Popen`, stdout → `logs/control_panel.log`) and embedded in the 🛡 Control tab via iframe.
- Token: `_load_creds()` from `ai_config.json` (`groww_api_key`, `groww_totp_secret`) + `_read_cache_file()` on `.groww_token.json`, guarded by `_token_lock` / `_token_cache = {"token","ts"}`.
- `GET /` and `/index.html` → single embedded `PAGE` HTML.
- `GET /api/state` → `{token_ok, token_exp, positions:[{trading_symbol,exchange,product,net_qty,avg_price,ltp,…}], orders:[…]}` (LTP filled per position).
- `GET /api/token` → `{token}`.
- `POST /api/exit` `{trading_symbol, exchange="NSE", product="MIS", net_qty}`; `POST /api/exit_all` → `{results:[…{symbol}]}`.
- `POST /api/order` — body filtered to whitelist `{trading_symbol, quantity, validity, exchange, segment, product, order_type, transaction_type, price, order_reference_id}`.
- `POST /api/cancel` `{groww_order_id}` → `/v1/order/cancel` with `segment:"FNO"`.
- Features: live FNO positions w/ P&L auto-refresh, one-click EXIT (market, opposite side, matching product/exchange), EXIT ALL panic button (double confirm), today's orders + cancel, manual BUY/SELL market/limit, 📋 curl-copy icons that embed the latest token for positions/orders/ltp/status/trades/buy/sell/exit/cancel.
- Designed to survive bot crashes (2026-08-04 incident: BUY executed, avg-price fetch failed, bot abandoned position).

---

## 5. `trading_decision_engine/`

**Standalone AND integrated.** Standalone: `python3 -m trading_decision_engine.app.run --mode live|shadow|replay …`. Integrated: `LIVE_DASHBOARD.py` has a process manager (`_DE_PROC_MARK = "trading_decision_engine.app.run"`, `_engine_start`/`_engine_stop`, `_DE_DIR`), a dedicated "Decision Engine" tab, and launches it headless:
```
[_PY_BIN, "-m", "trading_decision_engine.app.run", "--mode", mode, "--index", idx,
 "--expiry", exp, "--lots", n, "--premium-min", x, "--premium-max", y,
 "--validate-orders"|"--no-validate-orders", "--no-dashboard"(, "--profile", p)]
```
Stop = `SIGINT` first (so `run.py` saves session stats). Dashboard tails `trading_decision_engine/logs/events_*.jsonl`.

**Directory structure**
```
trading_decision_engine/
  __init__.py
  app/
    run.py                      # ENTRY POINT (argparse + interactive prompts + wiring)
    orchestrator.py             # event-driven state machine, composition root
    interactive_config.py       # prompt_mode/prompt_profile/prompt_index_selection/
                                #   prompt_expiry_selection/prompt_lots/prompt_premium_range/
                                #   prompt_validate_orders ; INDEX_OPTIONS
    config/  constants.py (INDEX_EXCHANGE, MARKET_CLOSE_TIME, Direction, Index,
             MarketStructure, OrchestratorState, TradeAction, TradeLifecycleState,
             is_market_open_time)  |  strategy.py (StrategyConfig.load, config_files_mtime,
             creds from env → ai_config.json)
    broker/  groww_execution_adapter.py (GrowwExecutionAdapter, ORDER_SUCCESS_STATUSES)
             instrument_master.py (InstrumentMaster, refresh_instrument_csv)
    market_data/  market_data_source.py, groww_websocket_source.py, replay_source.py,
             replay_tick_io.py, historical_replay_builder.py, candle_file_replay_builder.py,
             snapshot_builder.py, manual_trade_importer.py, decision_comparator.py
    models/  market_snapshot.py (MarketSnapshot, SessionState), engine_results.py (all *Result)
    engines/ (see table)
    utils/   console_dashboard.py, decision_logger.py, decision_diagnostics.py,
             session_statistics.py, error_handling.py, indicator_math.py,
             structure_math.py, rolling_history.py
  config/  strategy.json, README.md, profiles/{aggressive,balanced,conservative,scalping}.json
  docs/DESIGN.md
  reference/tw_all_in_one_indicator.pine
  logs/                      # gitignored
  tests/  fixtures.py + 24 test_*.py
```

**Entry point `app/run.py`** — `main(argv)`:
sets `logging.basicConfig(INFO)`, quiets `growwapi` to WARNING, attaches `_SuppressEmptyNatsErrorFilter` to `growwapi.groww.nats_client` (drops only the benign empty `"Error:"` line) → interactive prompts for any missing `--mode/--profile/--validate-orders/--index/--expiry/--lots/--premium-*` → `StrategyConfig.load(args.config, profile=args.profile)` → `refresh_instrument_csv(args.instrument_csv)` (fatal only if no file at all) → `InstrumentMaster` → `GrowwExecutionAdapter(config, dry_run=(mode!="live"), offline=(mode=="replay" and bool(replay_file)), instrument_master=…)` → `lot_size = instruments.lot_size_for(index, expiry)` if not given → `dataclasses.replace(config, default_lots=lots, premium_min=…, premium_max=…)` → `SessionStatistics` (+`ConsoleDashboard` observer if `--dashboard`/tty) → `Orchestrator(...)` → `_run_live_or_shadow` or `_run_replay` → `finally: _report_session_stats(...)`.

CLI args: `--mode {live,shadow,replay}`, `--index`, `--expiry YYYY-MM-DD`, `--lots`, `--premium-min`, `--premium-max`, `--lot-size`, `--instrument-csv`, `--validate-orders/--no-validate-orders`, `--config`, `--dashboard/--no-dashboard`, `--profile`, `--log-dir` (default `trading_decision_engine/logs`), `--replay-file`, `--save-replay-file`, `--replay-start`, `--replay-end`, `--replay-speed` (0 = max speed, 1.0 = real time), `--manual-trades`, `--comparison-tolerance-seconds` (120.0).

**Orchestrator role:** "the event-driven state machine and composition root. It is the only module that knows about more than one engine." One `on_snapshot()` call = one WebSocket-tick-driven cycle. No engine or orchestrator method ever sleeps waiting for market data; the only timers are WAIT_MODE/cooldown durations and the bounded option-chain/candle refresh loops in the market-data layer. Owns `SessionState`, `expiry_date`, `lot_size`, order validation, live config reload (`config_files_mtime` / `reload_strategy()`), `DecisionLogger`, and the diagnostics observer.

**Engines (`app/engines/`)**
| File | Role |
|---|---|
`base.py` | `Engine(Protocol)` — shared shape; engines never call each other or the broker |
`trend_engine.py` | EHMA direction vs long-EMA confirmation (`HULL > HULL[2]` ⇒ bullish); never EMA crossover |
`market_structure_engine.py` | HH/HL/LH/LL, double top/bottom, sideways, compression/expansion, exhaustion |
`support_resistance_engine.py` | math-only port of the Pine "Target & Stop Loss" section (left=33, right=21, quick_right=3, Close) → Level1–14 → nearest S/R |
`premium_momentum_engine.py` | velocity/acceleration/HH-HL/consistency of the ATM CE−PE spread over ~3s of ticks (also directional) |
`option_selection_engine.py` | best CE/PE strike by premium-range fit + liquidity (OI+volume) + spread |
`breakout_engine.py` | near-level → confirmed breakout/breakdown held for `breakout_confirmation_bars` closed candles; takes `SupportResistanceResult` as input |
`market_strength_engine.py` | momentum, acceleration, candle speed, range expansion, consolidation, trend confidence |
`volatility_engine.py` | gate: spread too high, abnormal vol, spikes, gaps, whipsaws (direction always NEUTRAL) |
`trading_rules_engine.py` | discipline only — SessionState + timestamp + config (+ optional `expiry_date`) |
`risk_engine.py` | operational safety only: in-trade, order-pending, margin, broker-connected |
`signal_stability_engine.py` | requires Trend/Momentum/Structure/Breakout/S-R to hold across a confirmation window; fails safe (`stable=False`) |
`decision_engine.py` | two-stage BUY/SELL/HOLD/REJECT: mandatory eligibility gates, then weighted trade-quality scoring; reasons carry actual-vs-required |
`position_sizing_engine.py` | how much: lots, capital, margin |
`trade_manager.py` | the one stateful component: open-trade lifecycle + six exit conditions (reversal, momentum loss, failed breakout, support failure, resistance rejection, forced) |

**Config schema** — precedence `built-in defaults < config/strategy.json < config/profiles/<active_profile>.json`; `--profile` overrides `active_profile`. Keys starting `_` are docs; unknown keys log a warning. Live reload within `config_reload_check_seconds` (5s default); bad JSON rejected, previous config stays live. Full reference: `trading_decision_engine/config/README.md`.

`config/strategy.json` keys (all, in file order): `_comment`, `active_profile` (`""`), `option_chain_refresh_seconds` 3.0, `candle_interval` "1minute", `trend_threshold` 60.0, `trend_ehma_length` 16, `trend_ema_long_length` 100, `trend_angle_lookback_bars` 5, `trend_angle_scale` 300.0, `trend_min_angle` 0.0, `trend_confidence_ema_agrees` 85.0, `trend_confidence_ema_disagrees` 40.0, `trend_confidence_ema_unavailable` 55.0, `trend_score_strength_weight` 0.7, `trend_score_confidence_weight` 0.3, `structure_swing_left` 3, `structure_swing_right` 3, `structure_min_candles` 30, `structure_exhaustion_threshold` 40.0, `structure_double_tolerance_pct` 0.15, `structure_compression_lookback` 20, `structure_compression_ratio` 0.6, `structure_expansion_ratio` 1.6, `structure_min_strength` 0.0, `sr_pivot_left` 33, `sr_pivot_right` 21, `sr_quick_pivot_right` 3, `min_resistance_distance` 15.0, `sr_breakout_buffer_points` 0.0, `premium_momentum_min_samples` 6, `premium_velocity_scale` 40.0, `momentum_threshold` 0.05, `momentum_min_acceleration` 0.0, `momentum_min_consistency` 0.0, `premium_min` 60.0, `premium_max` 250.0, `liquidity_min_oi` 50000, `liquidity_min_volume` 10000, `max_spread_pct` 2.0, `option_min_liquidity_score` 0.0, `option_min_spread_score` 0.0, `option_liquidity_weight` 0.5, `breakout_confirmation_bars` 2, `breakout_buffer_points` 0.0, `market_strength_window` 10, `market_strength_consolidation_threshold` 60.0, `volatility_min_candles` 20, `volatility_range_lookback` 15, `volatility_spike_multiplier` 2.5, `volatility_gap_multiplier` 1.5, `volatility_abnormal_multiplier` 2.0, `volatility_whipsaw_window` 6, `volatility_whipsaw_min_reversals` 4, `volatility_violation_penalty` 25.0, `max_trades_per_day` 6, `cooldown_seconds` 20, `consecutive_loss_limit` 3, `daily_loss_limit` 5000.0, `daily_profit_lock` 10000.0, `max_exposure` 100000.0, `expiry_day_cutoff_hour` 14, `market_close_buffer_minutes` 15, `wait_after_open_minutes` 1, `risk_min_margin_available` 0.0, `signal_stability_base_seconds` 3.0, `signal_stability_min_seconds` 1.5, `signal_stability_max_seconds` 6.0, `signal_stability_strong_threshold` 75.0, `signal_stability_weak_threshold` 35.0, `stability_history_max_age_seconds` 30.0, `require_trend` false, `require_signal_stability` false, `require_trading_rules` true, `require_risk` true, `require_support_resistance` false, `require_volatility` false, `require_market_structure` false, `require_breakout` false, `require_market_strength` false, `require_option_selection` false, `decision_score_threshold` 85.0, `min_buy_score` null, `min_sell_score` null, `min_confidence` 0.0, `min_trade_quality` 0.0, `min_score_difference` 0.0, `min_engine_agreement` 0, `quality_stability_bonus_cap` 10.0, `quality_liquidity_bonus_scale` 0.1, `quality_spread_bonus_scale` 0.1, `default_lots` 1, `exit_retry_min_interval_seconds` 2.0, `exit_retry_escalation_threshold` 5, `engine_failure_escalation_threshold` 3, `status_log_interval_seconds` 15.0, `config_reload_check_seconds` 5.0, `diagnostics_enabled` true, `dashboard_refresh_seconds` 1.0, and
`weights`: `trend` 0.15, `market_structure` 0.15, `support_resistance` 0.15, `premium_momentum` 0.15, `option_selection` 0.05, `breakout` 0.15, `market_strength` 0.10, `volatility` 0.05, `trading_rules` 0.025, `risk` 0.025.

Profiles (overlay only the keys they change):
| Key | aggressive | balanced | conservative | scalping |
|---|---|---|---|---|
`trend_threshold` | 30.0 | 35.0 | 45.0 | 25.0 |
`decision_score_threshold` | 40.0 | 75.0 | 90.0 | 60.0 |
`min_confidence` | 60.0 | 75.0 | 90.0 | 55.0 |
`min_trade_quality` | 60.0 | 75.0 | 90.0 | 55.0 |
`min_score_difference` | — | — | — | 10.0 |
`min_engine_agreement` | — | — | 3 | — |
`signal_stability_min/max/base` | 1.0/2.0/1.5 | 2.0/4.0/3.0 | 3.0/5.0/5.0 | 0.8/1.5/1.0 |
`min_resistance_distance` | 8.0 | 10.0 | 15.0 | 5.0 |
`momentum_threshold` | 0.02 | 0.02 | 0.03 | 0.02 |
`momentum_min_consistency` | — | — | — | 55.0 |
`premium_velocity_scale` | 0.5 | 0.5 | 0.5 | 0.4 |
`structure_double_tolerance_pct` | 0.03 | 0.03 | 0.03 | 0.03 |
`require_breakout` / `require_market_strength` | — | — | true/true | — |
`max_trades_per_day` | 15 | 10 | 5 | 20 |
`cooldown_seconds` | 15 | 20 | 30 | 10 |
`market_close_buffer_minutes` | — | — | — | 10 |

**Outputs** (`trading_decision_engine/logs/`, gitignored, `--log-dir` overridable; files roll per calendar date)
- `decisions_YYYY-MM-DD.csv` — `DecisionLogger.CSV_HEADER` (exact order):
  `timestamp, mode, spot, ce_premium, pe_premium, trend_score, structure_score, sr_score, momentum_score, stability_stable, stability_required_seconds, option_ce_symbol, option_pe_symbol, breakout_confirmed, market_strength_score, volatility_acceptable, rules_allowed, risk_safe, eligibility_passed, action, buy_score, sell_score, exit_score, confidence, trade_quality_score, reasons, exit_reason`
- `events_YYYY-MM-DD.jsonl` — full per-cycle event stream (read by the dashboard tab)
- `session_stats_{mode}_{YYYY-MM-DD_HHMMSS}.json` — written by `_report_session_stats` only if `decision_cycles > 0` or `trades.closed > 0`. Schema: `{"type":"session_stats","decision_cycles":int,"monitoring_ticks":int,"actions":{ACTION:count},"rejection_reasons_pct":{name:pct},"rejection_reasons_count":{name:count},"engines":{name:{"samples","pass_pct","fail_pct","avg_score","avg_confidence"}},"trades":{"closed":…}}`

Also touches `instrument.csv` at repo root (auto-download, see §6).

---

## 6. `instrument.csv` → `instrument.json`

**Source:** `https://growwapi-assets.groww.in/instruments/instrument.csv` (~19.1 MB, 132 525 data rows). Downloaded by `trading_decision_engine/app/broker/instrument_master.refresh_instrument_csv()` (`INSTRUMENT_MAX_AGE = timedelta(days=1)`; atomic write to `.csv.tmp` then `.replace()`; stale copy kept with a warning on failure), and independently by `CHART_LEVEL_ANALYZER.py:257`, `FIBONACCI_TREND_ANALYZER.py:308`, `COMMAND_GENERATOR_option_chain.py:52`, `CONVERGENCE_SIGNAL_BOT.py`.

**Exact 21 columns (header order):**
```
exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,
underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,
freeze_quantity,is_reserved,buy_allowed,sell_allowed,internal_trading_symbol,is_intraday
```
**Example row (first data row):**
```
NSE,66825,360ONE26AUG1080PE,NSE-360ONE-25Aug26-1080-PE,,PE,FNO,,,360ONE,13061,2026-08-25,1080,500,0.05,20001,0,1,1,360ONE26AUG1080PE,0
```
Notes: `name`, `series`, `isin` empty for FNO rows; `instrument_type ∈ {CE, PE, FUT, …}`; `expiry_date` is `YYYY-MM-DD`; FUT rows carry `strike_price = -0.01`; `groww_symbol` format `{EXCH}-{UNDERLYING}-{DDMonYY}-{STRIKE}-{CE|PE}` (or `-FUT`); booleans are `"0"/"1"`.

**`instrument.json` derivation** — `csv_to_json(csv_file_path, json_file_path=None)` in `PROD10FEB_ManualBOT_groww_option_trading_final_bot.py:244` (same helper duplicated in the other PROD/NEWPROD bots):
- default output `os.path.splitext(csv_file_path)[0] + ".json"` → `instrument.json`
- **skips conversion** if the JSON exists and `getmtime(json) >= getmtime(csv)` (prints `⚡ Using existing JSON (up-to-date)`) and returns the parsed JSON
- otherwise `csv.DictReader` → `list[dict]` → `json.dump(data, f, indent=4, ensure_ascii=False)` (prints `✅ Converted 'instrument.csv' → 'instrument.json'`)
- Result: a JSON **array** of 132 525 objects, keys = the 21 CSV column names, **all values are strings** (including `strike_price`, `lot_size`, `tick_size`, booleans). 95.4 MB. Gitignored.

---

## 7. Excel files

### `Lakshmi.xlsx` — trade Excel logger (100 KB, 3 sheets)
Writer: `log_trade_to_excel(symbol, buy_price, sell_price, quantity, profit)` — `PROD10FEB_ManualBOT…py:193` (identical in `MASTER_NEWPROD…:170`, `NEWPROD…:171`, `QA_PASS…:239`, `ENHANCED_ManualBOT…:169`, `PROD_fully_working…:161`, `PROD_working_trailling…:161`, `QA_env_test_groww_direct_mode…:159`, `DEV_NEWPROD…:170`); `MOMENTUM_AUTO_BOT.py:106` uses `EXCEL_FILE = "Lakshmi.xlsx"`.

| Sheet | Columns |
|---|---|
`Lakshmi` (active, A1:I1627) | `DateTime, Symbol, Buy Price, Sell Price, Quantity, Profit ₹, Capital Used, Result, Mode` |
`Sheet1` | scratch/empty (`C5:D18`) |
`NSE_Indices_Monitor` (A1:H613) | `Timestamp, Broad Market %, Sectoral %, Thematic %, Strategy %, Overall %, Signal, Reason` — written by `NSE_INDICES_MONITOR.py` (`EXCEL_FILE="Lakshmi.xlsx"`, `SHEET_NAME`) |

Writer behaviour (sheet `Lakshmi`):
- creates workbook if missing with `ws.title = "Lakshmi"` and the 9-column header row
- `mode = "PAPER" if CONFIG["PAPER_TRADING"] else ("MOCK" if CONFIG["MOCK_LTP_RUN"] else "LIVE")`
- also emits a machine-parseable stdout line for `ANALYZE_BOT`:
  `[TRADE_RECORD] {"ts":"%Y-%m-%dT%H:%M:%S","symbol":…,"buy_px":…,"sell_px":…,"qty":…,"pnl":…,"mode":…}`
- writes to `ws.active`, `DateTime` = `"%Y-%m-%d %H:%M:%S"`, `Capital Used` = `round(buy_price*quantity, 2)`, `Result` = `"PROFIT" if profit >= 0 else "LOSS"`
- ghost-row guard: `next_row = ws.max_row + 1`, then decrement while `cell(next_row-1, 1).value is None`
- historical rows also carry Excel formulas: `Capital Used` `=(C{r}*E{r})`, `Result` `=IF(F{r}<0,"LOSS","PROFIT")`
- example row: `2025-12-15 09:33:19 | NIFTY25D1625900CE | 96.35 | 98 | 1050 | 1732.5 | =(C2*E2) | =IF(F2<0,…) `
- Read by `PERSONAL_TRADING_AI.py:39` (`LAKSHMI = ROOT / "Lakshmi.xlsx"`) for intraday behavioural patterns.

Related legacy: `Lakshmi1.xlsx` sheet `Trades` = `DateTime, Symbol, Buy Price, Sell Price, Quantity, Profit, Volume, oi`. `technical_logs.xlsx` sheet `Technicals` = `Time, Symbol, LTP, EMA_9, SMA_20, RSI, ADX, VWAP`.

### `oi_pcr_dashboard.xlsx` (344 KB) — written by `calculate_oi_pcr.py` (`EXCEL_FILE = "oi_pcr_dashboard.xlsx"`, `REFRESH_SECONDS = 60`)
Sheets: `DATA` (A1:AM1405 = 39 columns) and `CHART` (A1:A1, holds the chart object).
`init_excel()` writes only these **25 header names**:
```
Time, Price, ATM, Total OI CE, Total OI PE, Total Chg CE, Total Chg PE,
ATM OI CE, ATM OI PE, ATM Chg CE, ATM Chg PE,
PCR All, PCR ATM ±3, PCR Chg ATM,
COI CE % All, COI PE % All, COI CE % ATM, COI PE % ATM,
CE Activity, PE Activity, CE Power, PE Power,
Resistance, Support, Sentiment
```
but each `ws.append(...)` row writes **39 values** — columns 26–39 are unheadered:
`str(resistance_strength)`, `str(support_strength)`, `"BREAKOUT"|""`, `"BREAKDOWN"|""`, `breakout_live[0..4]`, `breakdown_live[0..4]`.
`Resistance`/`Support` are `str(list)` e.g. `"[26000, 26200, 27000]"`. Conditional formatting via `apply_conditional_formatting(ws)` (e.g. `S2:T10000` fill `F8696B` when `S2="WRITERS"`).
Example row: `10:53:04 | 25947.3 | 25950 | 826153 | 631258 | 152168 | 165281 | 193241 | 203341 | 59994 | 63396 | 0.7640933338 | 1.0522663409 | 1.0567056705 | 47.9346 | 52.0653 | 48.6214 | 51.3785 | N/A | N/A | N/A | N/A | [26000, 26200, 27000] | [26000, 25900, 25500] | NEUTRAL`

---

## 8. `requirements.txt` (full contents, 10 lines)
```
flask>=3.0.0
growwapi>=1.5.0
openai>=1.0.0
openpyxl>=3.1.0
pandas>=2.0.0
playwright>=1.50.0
pyperclip>=1.8.0
pyotp>=2.9.0
requests>=2.32.0
twilio>=9.0.0
```
Not listed but used at runtime: `yfinance` (PERSONAL_TRADING_AI, optional via `YF_OK`), `playsound` (sound alerts), `flask-cors`. Venv is Python 3.9 (`.venv/lib/python3.9`); `SETUP.md` also requires `.venv/bin/playwright install chromium`.

---

## 9. `ai_config.json` (gitignored) — expected keys

Single JSON object at repo root. Union of keys read by all consumers:
```json
{
  "openai_api_key":     "sk-proj-…",
  "model":              "gpt-4o",
  "anthropic_api_key":  "sk-ant-…",
  "enabled":            true,
  "groww_api_key":      "<Groww API JWT>",
  "groww_totp_secret":  "<base32 TOTP secret>"
}
```
| Key | Read by | Notes |
|---|---|---|
`groww_api_key`, `groww_totp_secret` | `LIVE_DASHBOARD._load_groww_creds()` (line 1961) → `_GROWW_API_KEY, _GROWW_TOTP_SECRET`; `TRADE_CONTROL_PANEL._load_creds()` (line 52); `TRENDLINE_SCANNER_BOT.py:533`; `TRENDLINE_BACKTEST.py:139`; `trading_decision_engine/app/config/strategy.py:249-262` | All use `cfg.get(k, "")`; missing → `("", "")` and token fetch silently fails. `strategy.py` order is **env vars first, then ai_config.json, never hardcoded**; raises if both missing ("…or populate groww_api_key/groww_totp_secret in ai_config.json") |
`openai_api_key` | `ANALYZE_BOT.py:51-77` — resolution order `os.environ["OPENAI_API_KEY"]` → `ai_config.json` → interactive prompt (then **writes** the key back and `cfg.setdefault("model","gpt-4o")`); on invalid key it `cfg.pop("openai_api_key")` and prints "Edit ai_config.json and correct it." |
`model` | `ANALYZE_BOT.py:1546` — `cfg.get("model", "gpt-4o")` |
`enabled` | `ANALYZE_BOT.py:1529` — AI narrative skipped when `cfg.get("enabled") is False` |
`anthropic_api_key` | `PERSONAL_TRADING_AI.py:38` (`AI_CONFIG = ROOT/"ai_config.json"`) — "Add ANTHROPIC_API_KEY to ai_config.json for AI narrative" |

`SETUP.md` minimal bootstrap only writes `openai_api_key`, `model`, `enabled`. Note: `PROD10FEB…py` (~line 100) and `KEY_LEVELS_TERMINAL.py`/`COMMAND_GENERATOR_option_chain.py` still hardcode `api_key`/`TOTP_SECRET` — these should migrate here.

---

## 10. Dotfile state/cache files in repo root

| File | Writer | Readers | Gitignored |
|---|---|---|---|
`.auto_mode_status.json` | `PROD10FEB…py:3300 _AUTO_STATUS_FILE` / `_write_auto_status()` | `LIVE_DASHBOARD._read_auto_mode_status()` (2568), `/api/auto_mode_status` (14021), `_build_mb_ai_prompt` | no |
`.convergence_signals.json` | `CONVERGENCE_SIGNAL_BOT.SIGNALS_PATH` (101) / `_write_signal()` (384) | `LIVE_DASHBOARD._read_conv_signals()` (2560) | no |
`.trading_ai_cache.json` | `PERSONAL_TRADING_AI.CACHE_FILE` (37) / `build_market_db()` | `PERSONAL_TRADING_AI.build_market_db()` only | no |
`.trendline_chart_data.json` | `TRENDLINE_SCANNER_BOT._CHART_DATA_FILE` (126), written at line 319 | `LIVE_DASHBOARD` `/api/trendline_signals` (13857) | no |
`.trendline_signals.json` | `TRENDLINE_SCANNER_BOT._SIGNALS_FILE` (152) / `_write_signals_file()` (156) | `LIVE_DASHBOARD` (567, 13849) | no |
`.vix_cache.json` | `LIVE_DASHBOARD._VIX_CACHE_FILE` (647) / `_update_vix_history()` | `LIVE_DASHBOARD._load_vix_cache()` (673, called at 14209) | no |
`oi_snapshot.json` | `calculate_oi_pcr.write_oi_snapshot()` (859, saved 915, `indent=2`, every 60s) | `MOMENTUM_AUTO_BOT.OI_SNAPSHOT_PATH` (105, loader 610), `CONVERGENCE_SIGNAL_BOT` (100, 155), `LIVE_DASHBOARD` (725) | no |
`.prod10_bridge_cmd.json` | `LIVE_DASHBOARD.PROD10_BRIDGE_FILE` (20) — written at 13966/13978/13991/14015, deleted at 14003 | `PROD10FEB…py:3678 _BRIDGE_FILE` / `_dashboard_bridge_watcher()` | yes (`.claimed.*` pattern) |
`.groww_token.json` (+ `.lock`, `.tmp`) | `groww_token._write_cache` | all bots | yes |
`.prod10_bridge.lock` | `PROD10FEB…py:3684 _BRIDGE_OWNER_LOCK` — `fcntl.flock(LOCK_EX\|LOCK_NB)`, content = PID (e.g. `46132`) | ownership guard only | yes |
`.wa_control.json` | `whatsapp_gateway._write_command` | `whatsapp_gateway.get_pending_command` | no (not currently present) |

**Schemas**

`.auto_mode_status.json` — `{"state": str, "ts": float}` always, plus arbitrary merged kwargs (`mode_label` "PAPER"/"LIVE", `index`, `direction`, `confidence`, `votes_ce`, `votes_pe`, `instrument_symbol`, `ltp`, `total_pnl`, `trade_count`, `detail_line`, …). Live sample:
```json
{"state": "STARTING", "ts": 1781458419.778692, "mode_label": "LIVE", "index": "NIFTY"}
```

`.convergence_signals.json` — `indent=2`, last 100 signals:
```json
{"updated": "2026-06-25 11:36:40", "total": 41,
 "signals": [{"time":"2026-06-25 10:37:21","ts_ms":1782364041227,"side":"PE",
   "strength":"STRONG","conv_count":3,"accel_count":3,"avg_vel_pct":1.201,
   "top_strike":24150.0,"spot":24178.15,"oi_bias":"NEUTRAL","vel_thresh":0.8,
   "scan_secs":5,
   "hits":[{"sym":"NSE_NIFTY26JUN24150PE","strike":24150.0,"opt_type":"PE",
            "vel_pct":1.474,"ltp_start":91.6,"ltp_end":92.95,"accelerating":true}]}]}
```

`.trading_ai_cache.json` — refreshed if `time.time() - built_at >= 43200` (12 h):
```json
{"built_at": 1785822841.123405,
 "records": [{"date":"2023-04-03","nifty_open":17427.949,"nifty_close":17398.051,
              "nifty_prev":NaN,"gap_pct":NaN,"trend_5d":NaN,"dow":0,"vix":12.59}]}
```
(821 records; `dow` 0=Mon…4=Fri; source `yf.download("^NSEI")` + `^INDIAVIX` from 2023-04-01.)

`.trendline_chart_data.json`
```json
{"ts":"2026-07-30 12:50:49","index":"NIFTY","premium_min":85,"premium_max":200,
 "status":{"tl_active":0,"total":82,"in_range":0,"near_signal":[],"open_trade":null,
           "spot_bars":44,"spot_ltp":24295.8},
 "instruments":[{"symbol":"NIFTY2662323250CE","opt_type":"CE","ltp":575.15,
                 "candles":[],"trendlines":[]}],
 "spot":{"symbol":"NIFTY","ltp":24295.8,
         "candles":[{"ts":1785383100,"o":24249.55,"h":24266.65,"l":24190.0,"c":24266.55,"v":0}],
         "trendlines":[{"type":"ASC_RESIST","color":"#69f0ae",
                        "p1":{"idx":28,"price":24279.45},"p2":{"idx":39,"price":24331.05},
                        "projected":24349.81,"slope":4.6909}]}}
```

`.trendline_signals.json` — `indent=2`, last 30 signals; `stats` recomputed each write by re-reading `logs/trade_history/trendline_{YYYY-MM-DD}.jsonl`:
```json
{"ts":"2026-07-30 12:50:13","active_trade":null,"signals":[],
 "stats":{"trades":0,"wins":0,"losses":0,"pnl":0.0}}
```

`.vix_cache.json` — discarded on load if `date != today`:
```json
{"date":"2026-08-04","session_open":12.0,"history":[{"t":"11:23","v":12.0}]}
```
(`t` = `HH:MM`, `v` = `round(vix,2)`; duplicates by `t` skipped; capped at `_VIX_HISTORY_MAX`.)

`oi_snapshot.json` — `indent=2`, 44 keys:
```
time (HH:MM:SS), timestamp (epoch float), price, atm, sentiment,
pcr_all, pcr_atm, total_oi_ce, total_oi_pe, total_chg_ce, total_chg_pe,
resistance [3 strikes], support [3 strikes],
resistance_strength [{strike, ce_oi, total_oi}], support_strength [{strike, pe_oi, total_oi}],
atm_strikes_oi {"<strike>": {ce_oi, pe_oi}},
writer_bias, bullish_score, bearish_score,
ce_writing_strikes [3], pe_writing_strikes [3], max_pain,
vol_pcr, total_ce_vol, total_pe_vol, atm_ce_iv, atm_pe_iv, iv_skew,
atm_extras {"<strike>": {ce_iv, pe_iv, ce_ltp, pe_ltp, ce_vol, pe_vol}},
smart_money_ce [{strike, oi_change, ltp, vol}], smart_money_pe [same],
market_signal, bull_score_v2, bear_score_v2, momentum_score, signal_list,
call_writing, put_writing, strike_buildups, iv_changes,
pcr_change, atm_momentum, resistance_breakout, support_breakdown
```

`.prod10_bridge_cmd.json` — one-shot command from dashboard to PROD10 (deleted/renamed after consumption). Written by `/api/prod10_buy` (13966):
```json
{"command":"17 NIFTY04AUG202624800PE","mode":"manual|quick|auto","paper":false,
 "atr":true,"atr_source":"candle|scan","mock":false,"quick_pts":1.5,
 "partial":false,"partial_pct":50,"ltp":96.35,"validate_orders":true}
```
`command` = `f"{lots} {index}{DD}{MON}{YYYY}{strike}{CE|PE}"`. `validate_orders` omitted → keep bot CONFIG default. Two other shapes:
```json
{"command":"set_quick_pts","quick_pts": 2.0}
{"command":"set_partial","partial":true,"partial_pct":50}
{"command":"__AUTO__","mode":"auto","paper":false}
```
Reader defaults (`_dashboard_bridge_watcher`): `mode="manual"`, `paper=None` (keep CONFIG), `mock=False`, `validate_orders=None`, `atr=True`, `atr_source="candle"`, `quick_pts=1.5`, `partial=False`, `partial_pct=50`, `ltp=0`. Consumption is **atomic**: `os.rename(_BRIDGE_FILE, f"{_BRIDGE_FILE}.claimed.{os.getpid()}")` (exactly one process wins), read, then `os.remove`. Poll interval `time.sleep(0.01)`; heartbeat print every 60s. `_bridge_lock.acquire(blocking=False)` rejects a second concurrent command ("⚠️ [DASHBOARD] Ignored — bot is already executing an order."). Only the process holding the `fcntl` flock on `.prod10_bridge.lock` starts the watcher.

---

## 11. `logs/` layout

```
logs/
  analysis/          pattern_analysis_YYYYmmdd_HHMMSS.json      (PATTERN_ANALYZER.py:973)
  chart_level/       Chart_Level_YYYY-MM-DD_HH-MM-SS.log        (CHART_LEVEL_ANALYZER.py:1552)
                     signals_YYYY-MM-DD.jsonl                   (CHART_LEVEL_ANALYZER.py:1574)
                     live_chain.json                            (CHART_LEVEL_ANALYZER.py:1696)   [dir gitignored]
  convergence_bot/   Convergence_Bot_YYYY-MM-DD_HH-MM-SS.log    (CONVERGENCE_SIGNAL_BOT.py:66)
  fibo_analyzer/     Fibo_Analyzer_YYYY-MM-DD_HH-MM-SS.log      (FIBONACCI_TREND_ANALYZER.py:1796)
  groww_bot/         Groww_Bot_YYYY-MM-DD_HH-MM-SS.log          (PROD10FEB / all PROD variants)
  master_signal/     Master_Signal_YYYY-MM-DD_HH-MM-SS.log      (MASTER_SIGNAL_BOT.py)
  momentum_bot/      Momentum_Bot_YYYY-MM-DD_HH-MM-SS.log       (MOMENTUM_AUTO_BOT.py:71)
  premium_tracker/   Premium_Tracker_YYYY-MM-DD_HH-MM-SS.log    (PREMIUM_DIRECTION_TRACKER.py)
  qa_pass_bot/       Groww_Bot_YYYY-MM-DD_HH-MM-SS.log          (QA_PASS_…py:36)
  replay/            trendline_replay_YYYY-MM-DD.log  +  .json  (TRENDLINE_REPLAY.py:608-610)
  signal_monitor/    Signal_Monitor_YYYY-MM-DD_HH-MM-SS.log     (SIGNAL_MONITOR.py)
  trade_history/     YYYY-MM-DD.jsonl, trendline_YYYY-MM-DD.jsonl,
                     trendline_backtest_YYYYmmdd.jsonl, live_scanner_YYYY-MM-DD.json
  trendline_bot/     TrendlineBot_YYYY-MM-DD_HH-MM-SS.log       (TRENDLINE_SCANNER_BOT.py:107)
  control_panel.log  (append-only, TRADE_CONTROL_PANEL stdout via LIVE_DASHBOARD)
  o1_c3_diff_log.csv (QA_PASS_…py:265 log_o1_c3_diff)
```
Timestamp token is always `datetime.now().strftime('%Y-%m-%d_%H-%M-%S')`, one new file per process start. No rotation/deletion (a documented limitation). Dashboard reads the newest file per prefix via `_latest(dir, prefix)`; the map is `LIVE_DASHBOARD.py:200-202` — `trendline_scanner → ("logs/trendline_bot","TrendlineBot_")`, `momentum → ("logs/momentum_bot","Momentum_Bot_")`, `trade_bot → ("logs/groww_bot","Groww_Bot_")`. `*.log` is gitignored; `logs/chart_level/` fully gitignored.

**`logs/trade_history/YYYY-MM-DD.jsonl`** — one JSON object per line, appended. Two writers with slightly different fields:

PROD10 via dashboard (`LIVE_DASHBOARD.py:1334-1358`):
```json
{"date":"2026-06-23","time_entry":"12:27:16.360","time_exit":"12:27:19.775",
 "bot":"PROD10","mode":"paper|live","index":"NIFTY","symbol":"NIFTY2662323900CE",
 "option":"323900CE","expiry":"2026-06-23","buy_price":76.6,"sell_price":75.85,
 "qty":1170,"lots":1,"pnl":-877.5,"exit_reason":""}
```
`MOMENTUM_AUTO_BOT._log_trade_history` (988-1016) adds `"bot":"Auto"`, real `exit_reason` text, `oi_bias`, `oi_verdict_tag`:
```json
{"date":"2026-06-23","time_entry":"12:27:16.360","time_exit":"12:27:19.775","bot":"Auto",
 "mode":"paper","index":"NIFTY","symbol":"NIFTY2662323900CE","expiry":"2026-06-23",
 "buy_price":76.6,"sell_price":75.85,"qty":1170,"lots":18,"pnl":-877.5,
 "exit_reason":"🔻 Trail SL hit @ ₹75.85  (peak=₹77.75  exit=₹77.00)  [detected 12:27:19.775]",
 "oi_bias":"BEARISH","oi_verdict_tag":"OPPOSED_LOSS"}
```
`trendline_YYYY-MM-DD.jsonl` (TRENDLINE_SCANNER_BOT): same base + `"bot":"Trendline"`, `"mode":"sim"`, `"option":"323950CE"`, `"play_type":"BOUNCE"`.

**`logs/analysis/pattern_analysis_*.json`** — JSON **array** of trade objects:
```json
{"date":"2026-06-08","symbol":"NIFTY2662324050CE","pattern":"COMPRESSION|MULTI_BOUNCE|…",
 "entry_time":"10:35","entry_price":67.75,"exit_time":"12:50","exit_price":71.55,
 "exit_reason":"TARGET|SL|TRAIL_SL","pts":3.8,"pnl":5130.0,"qty":1350,
 "touches":0,"premium_at_entry":65.1}
```

**`logs/chart_level/signals_YYYY-MM-DD.jsonl`**
```json
{"ts":"2026-07-31T12:22:38","index":"NIFTY","spot":24377.0,"direction":"CE",
 "confidence":"HIGH","reason":"Bounce off 1H Swing H | bullish momentum | above VWAP",
 "entry_type":"BREAK","target_pts":228.0,"sl_pts":25.0,"rr_ratio":9.1,
 "spot_target":24604.95,"spot_sl":24352.0,"strike":24350,"option_ltp":103.7}
```
**`logs/chart_level/live_chain.json`** — `{"ts": ISO seconds, "spot": float, "chain": {"<strike>": {...}}}` (5-min cached full chain for the dashboard).

**`logs/replay/trendline_replay_YYYY-MM-DD.json`** — JSON array:
```json
{"date":"2026-06-17","symbol":"NIFTY2662323850CE","strike":23850,"opt_type":"CE",
 "signal":"BOUNCE","entry_time":"11:40","entry_price":286.4,"exit_time":"11:45",
 "exit_price":290.7,"exit_reason":"TRAIL_SL","pts":4.3,"pnl_1lot":322.5,
 "pnl_18lots":5805.0,"qty_1lot":75,"qty_18lots":1350,
 "chart_verify":{"symbol":…,"signal_candle":"11:35","signal_candle_close":286.2,
   "trendline_pivot1":{"time":"10:05","price":272.15},
   "trendline_pivot2":{"time":"11:10","price":281.2},
   "trendline_support":284.68,"how_to_verify":"1. Open … on Groww chart (5-min candles)…"}}
```
Paired with a human-readable `trendline_replay_YYYY-MM-DD.log`.

**`logs/o1_c3_diff_log.csv`** — header + appended rows (`QA_PASS_…py:265`, called at 1280):
```
DateTime,Symbol,O1_Open,C3_Close,Diff,Diff_%,Direction
2026-07-14 11:20:37,NSE-NIFTY-21Jul26-24200-CE,130.35,130.35,0.0,0.0,PE
```

---

## 12. `commands_*.html`

**Generated by** `COMMAND_GENERATOR_option_chain.py` → `save_all_commands_to_file(index_name, expiry_label, options_data, quantity, spot_price)` (line 251):
`html_filename = PROJECT_ROOT/f"commands_{index_name}_{expiry_label.replace(' ','_')}.html"`.

**`main()` behaviour (hardcoded, no user input):**
1. Deletes every existing `commands_*.html` via `glob.glob(PROJECT_ROOT/"commands_*.html")` + `os.remove` (regenerated fresh each run).
2. `instruments = load_instruments_from_csv()` (repo-root `instrument.csv`, downloaded from `https://growwapi-assets.groww.in/instruments/instrument.csv` if needed).
3. `indices = ["NIFTY", "SENSEX"]`; `strike_range = 20` (20 strikes each side of ATM); `quantities = {"NIFTY": 20, "SENSEX": 50}`; strike step `100` for SENSEX else `50`.
4. For each index: spot via `get_spot_price`, `get_expiry_dates` → `(current_expiry, next_expiry)`, then one file per expiry with `expiry_label = f"Current_{current_expiry}"` / `f"Next_{next_expiry}"`.

**Resulting four files (current on disk):**
`commands_NIFTY_Current_2026-07-21.html`, `commands_NIFTY_Next_2026-07-28.html`, `commands_SENSEX_Current_2026-07-16.html`, `commands_SENSEX_Next_2026-07-23.html`.

**Role:** a click-to-copy option-chain cheat sheet for the manual bot's stdin. Dark theme (`#1e1e1e`/`#d4d4d4`, Courier New), classic chain layout `CE ── strike ── PE` (`.row`/`.ce-group`/`.strike-col`/`.pe-group`, `.ce` green `#0dc710`, `.pe` red `#e51717`), strikes ascending. Header `<h2>{INDEX} Option Commands — {expiry_label}</h2>` + `<h3 style="color:#ffd700">Spot: 24182.15</h3>`. Each cell holds the exact PROD10 command string plus a 📋 button running `cp(text, btn)` → `navigator.clipboard.writeText` with a ✓ flash for 1500 ms.

**Command string format** (`build_command`, line ~245): `f"{quantity} {index}{exp_formatted}{strike_str}{option_type}"` where `exp_formatted` is `DDMONYYYY` — e.g. `20 NIFTY21JUL202623200CE`. Same format the PROD10 bot accepts on stdin and that `LIVE_DASHBOARD`'s bridge synthesizes (`17 NIFTY04AUG202624800PE`).

**Launch:** started in the background (no terminal window) as the first line of `START_ALL_BOTS.command`.

---

## 13. Day-to-day startup order

### Automated — `START_ALL_BOTS.command` (zsh + AppleScript, chmod +x, double-clickable)
```
DIR = /Users/ayush/Documents/eclipse-workspace/Groww_Python_Trading_Bot-main
PY  = /Library/Developer/CommandLineTools/usr/bin/python3
```
Background (no window, `> /dev/null 2>&1 &`), in order:
1. `COMMAND_GENERATOR_option_chain.py` — regenerates the 4 `commands_*.html`
2. `calculate_oi_pcr.py` — writes `oi_snapshot.json` every 60 s
3. `LIVE_DASHBOARD.py` — serves `http://localhost:8765` (also auto-starts `TRADE_CONTROL_PANEL.py` on `127.0.0.1:8790`)

Then AppleScript minimises the launcher and opens 8 Terminal windows (profile "Pro"), `delay 1.2` between each:
| Win | Bot | Bounds |
|---|---|---|
0 | `PERSONAL_TRADING_AI.py` (pre-market check, read first; `delay 4` before the rest) | 245,112,1225,762 |
1 | `PREMIUM_DIRECTION_TRACKER.py` | 0,25,490,400 |
2 | `FIBONACCI_TREND_ANALYZER.py` | 490,25,980,400 |
3 | `MASTER_SIGNAL_BOT.py` | 980,25,1470,400 |
4 | `CHART_LEVEL_ANALYZER.py` | 0,400,368,874 |
5 | `SIGNAL_MONITOR.py` | 368,400,736,874 |
6 | `PROD10FEB_ManualBOT_groww_option_trading_final_bot.py` | 736,400,1104,874 |
7 | `TRENDLINE_SCANNER_BOT.py` | 1104,400,1470,874 |

Then `delay 1.5`, re-applies all bounds, raises window 0, `delay 2`, `open http://localhost:8765`.

### Manual — `SETUP_GUIDE.md` §Terminals
```
Terminal 1  python3 MASTER_SIGNAL_BOT.py           [required]
Terminal 2  python3 FIBONACCI_TREND_ANALYZER.py    [required]
Terminal 3  python3 CHART_LEVEL_ANALYZER.py        [optional]
Terminal 4  python3 PREMIUM_DIRECTION_TRACKER.py   [optional]
Terminal 5  python3 SIGNAL_MONITOR.py              [optional]
Terminal 6  python3 LIVE_DASHBOARD.py              (last — it reads the others' logs)
restart:    kill $(lsof -ti :8765) && python3 LIVE_DASHBOARD.py
```
`LIVE_DASHBOARD.main()` prints the same order on startup (lines 14197-14201: MASTER_SIGNAL required, FIBONACCI required, CHART_LEVEL optional, PREMIUM_DIRECTION optional), then starts `_loop`, `_ltp_fetcher_loop`, `_run_ptai_analysis`, `_idx_refresh_loop`, `_load_vix_cache()`, `_vix_fetch_loop`, `_ensure_control_panel()`, and binds `('0.0.0.0', 8765)`.

### `SYSTEM_GUIDE.html` §08 Quick-Start Matrix (● required, ○ optional)
| Scenario | oi_pcr | Premium | Fibo | ChartLvl | Trendline | Converge | Master | PROD10 | Momentum | Dashboard |
|---|---|---|---|---|---|---|---|---|---|---|
Full manual analysis | ● | ● | ● | ● | ○ | ○ | ● | ● | — | ● |
Quick Trade Mode only | ○ | ○ | — | — | — | — | — | ● | — | ● |
Momentum auto-trade | ● | — | — | — | — | ○ | — | — | ● | ● |
Pre-market preparation | — | — | — | — | — | — | — | — | — | ● |
Post-session review | — | — | — | — | — | — | — | — | — | — (run `ANALYZE_BOT.py` → `SIGNAL_ANALYZER.py`) |
Full system | ● | ● | ● | ● | ● | ● | ● | ● | ○ | ● |

**Operating rules:** never run `MOMENTUM_AUTO_BOT` and `PROD10` on the same index simultaneously (no cross-bot position coordination); investigate any bot showing >5 min stale in the dashboard BOT STATUS bar before trading; run `SIGNAL_ANALYZER` after losing sessions or the `BOT_TUNING.json` correction loop stays open. Ports: dashboard `0.0.0.0:8765`, control panel `127.0.0.1:8790`, WhatsApp webhook `0.0.0.0:5055`.
