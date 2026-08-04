# LIVE_DASHBOARD Server — Rebuild Spec

> Part of the end-to-end rebuild documentation. Master document: ../../REBUILD_BLUEPRINT.md
> Generated 2026-08-04 from a full code survey. Treat all constants, filenames,
> JSON keys and printed strings here as EXACT contracts.

---

# REBUILD SPEC — `LIVE_DASHBOARD.py`

Single-file, stdlib-HTTP trading dashboard (14,229 lines) at `/Users/ayush/Documents/eclipse-workspace/Groww_Python_Trading_Bot-main/LIVE_DASHBOARD.py`. It is a **read-only aggregator over other bots' log/JSON files** plus a thin Groww REST client, plus a **file-based command bridge** to the PROD10 trading bot. It does not itself place orders from the UI (see §7 note).

---

## 1. Server topology

```python
BASE        = os.path.dirname(os.path.abspath(__file__))
PORT        = 8765
REFRESH_SEC = 15          # snapshot rebuild cadence
STALE_SECS  = 300         # default "_live" staleness cutoff
PROD10_BRIDGE_FILE = os.path.join(BASE, ".prod10_bridge_cmd.json")
```

Imports: `os, json, time, re as _re, threading, csv, sys, requests as _req, datetime, http.server.BaseHTTPRequestHandler/HTTPServer, socketserver.ThreadingMixIn, subprocess as _bsp, signal as _signal`.

Server class (defined inside `main()`):
```python
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def handle_error(...)   # swallows BrokenPipeError / ConnectionResetError
server = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
```
`Handler.log_message` is a no-op (silent access log). `Handler._json(body, code=200)` writes `json.dumps(body, default=str)` with `Content-Type: application/json` + `Access-Control-Allow-Origin: *`, swallowing broken pipes. `do_OPTIONS` → 204 + CORS (GET,POST,OPTIONS; Content-Type).

`main()` startup order:
1. Print banner + "start these bots first" list (MASTER_SIGNAL_BOT, FIBONACCI_TREND_ANALYZER required; CHART_LEVEL_ANALYZER, PREMIUM_DIRECTION_TRACKER optional).
2. `_refresh()` synchronously (blocking first snapshot).
3. daemon threads: `_loop`, `_ltp_fetcher_loop`, `_run_ptai_analysis` (one-shot), `_idx_refresh_loop`.
4. `_load_vix_cache()` then thread `_vix_fetch_loop`.
5. `_ensure_control_panel()` (§11).
6. `serve_forever()`; KeyboardInterrupt → "Stopped."

### Background threads and cadences

| Thread / function | Cadence | Work |
|---|---|---|
| `_loop()` | `time.sleep(REFRESH_SEC)` = 15 s | calls `_refresh()`, then conditionally spawns AI jobs from a `_last` dict of last-run epochs |
| `_refresh()` | per loop | runs **all** readers, builds `_snapshot` under `_lock` |
| `_ltp_fetcher_loop()` | `sleep(30)` | reads `_snapshot.bots.chart_signal.strike/direction` → `_fetch_live_option_ltp()` → `_ltp_result` |
| `_idx_refresh_loop()` | `sleep(3)`; OHLC sub-refresh every 60 s | one batched Groww LTP call for `NSE_NIFTY,NSE_BANKNIFTY,BSE_SENSEX`; spawns `_fetch_idx_quote()` thread at start and every 60 s |
| `_vix_fetch_loop()` | `sleep(120)` | NSE `allIndices` → INDIA VIX → `_update_vix_history` |
| `_run_ptai_analysis()` | `PTAI_ANALYSIS_REFRESH = 300` | PERSONAL_TRADING_AI module analysis → `_ptai_analysis` |
| `_run_ptai_ai()` | `PTAI_AI_REFRESH = 1800`, gated on `_features["ptai_ai"]` | Claude CLI |
| `generate_ai_summary()` | `AI_REFRESH_SECS = 180`, gated `_features["ai"]` | Claude CLI |
| `generate_scalp_plan()` | `SCALP_REFRESH_SECS = 60`, gated `_features["scalp"]` | Claude CLI |
| `generate_oi_summary()` | `OI_AI_REFRESH_SECS = 120`, gated `_features["oi_ai"]` | Claude CLI |
| `generate_mb_ai()` | `MB_AI_REFRESH_SECS = 300`, gated `_features["mb_ai"]` | Claude CLI |
| `generate_qs_ai()` | `QS_AI_REFRESH_SECS = 300`, gated `_features["qs_ai"]`; first run seeded to fire ~10 s after start (`_last["qs_ai"] = now - QS_AI_REFRESH_SECS + 10`); skipped if status=="running" | Claude CLI |
| `_bot_log_reader(bot_id, proc)` | per spawned non-terminal bot | drains `proc.stdout` into `_bot_logs[bot_id]`, capped `_BOT_MAX_LOG = 200` |
| `_start_trail_ltp_feed(esym)` | no sleep (HTTP RTT ≈250 ms is the throttle) | polls Groww LTP into `_trail_ltp` while trade status in ACTIVE/BUYING/EXITING |
| `_trail_loop` | `sleep(0.015)` (~65 Hz), heartbeat log every 30 s | reads cached LTP, trails |
| `_run_pai_bg()` | on-demand (`POST /api/personal_ai/run`) | `subprocess.run([sys.executable, PERSONAL_TRADING_AI.py], timeout=180)` |

Locks: `_lock` (snapshot), `_bot_lock`, `_trade_lock`, `_trail_ltp_lock`, `_idx_lock`, `_ltp_result_lock`, `_oi_history_lock`, `_vix_history_lock`, `_ptai_analysis_lock`, `_ptai_ai_lock`, `_ai_lock`, `_scalp_lock`, `_oi_ai_lock`, `_mb_ai_lock`, `_qs_lock`, `_pai_lock`, `_proc_lock`, `_ai_session_lock`.

Other module caches/constants: `_PNL_CACHE_SECS=30`, `_PERF_CACHE_SECS=30.0`, `_PIVOT_CACHE_SECS=60.0`, `_MARGIN_CACHE_SECS=60`, `_ORDERS_CACHE_SECS=30`, `_CHAIN_CACHE_TTL=5`, `_OI_HISTORY_MAX=200`, `_VIX_HISTORY_MAX=210`, `_de_expiry_cache` TTL 600 s, LTP token TTL 7200 s.

---

## 2. Complete HTTP API surface

All routes on port 8765. `do_GET` dispatches on `urlparse(self.path).path` (two use `startswith`: `/api/toggle`, `/api/data`). Unmatched GET → serves the `HTML` constant (`text/html; charset=utf-8`). Unmatched POST → `{"error":"not found"}` 404.

### GET

| Path | Params | Response |
|---|---|---|
| `/api/toggle` (startswith) | `f` ∈ `ai, scalp, ptai_ai, oi_ai, mb_ai, qs_ai` | flips `_features[f]`; if turned OFF, terminates `_running_procs[f]`. Returns full `_features` dict |
| `/api/data` (startswith) | — | the whole `_snapshot` (§ shape below) with `features` re-injected |
| `/api/prod10_logs` | — | `{"lines":[str],"file":str,"offline":bool,"error":str}`. Newest `logs/groww_bot/Groww_Bot_*.log`; if mtime age > **90 s** → `offline:true`. Else last 300 lines → keep lines containing any of `DASHBOARD, BUY, Trailing, Monitoring, Trail, SELL, Exit, SL HIT, profit, PROFIT, loss, LOSS, ✅, ❌, 🌐, 📈, 💓, Placed, Order placed, Command, ACTIVE, Entry price, LTP for`; drop noise `Error fetching LTP, LTP is None, retrying, streak=`; collapse consecutive duplicates (compare after first `]`); return last 60 |
| `/api/groww_capital` | — | `{"ok":true,"option_buy_balance":float,"clear_cash":float}` from `GET https://api.groww.in/v1/margins/detail/user`; else `{"ok":false,"error":...}` |
| `/api/lot_size` | `index` (def NIFTY), `expiry` | `{"lot_size":int}` via `_lot_size_from_csv` |
| `/api/momentum_bot_logs` | — | same shape as prod10_logs; dir `logs/momentum_bot/Momentum_Bot_*.log`; offline if age > **120 s**; last 500 lines; keep-list `ENTRY,BUY,SELL,Trail,Signal,Momentum,MOCK TRAIL,SIM,Quick target,HARD SL,Max hold,P&L,CLOSED,CONFIG,starting,Ready,✅,❌,⚠,📈,💰,🎯,🔻,🛑,🎭,second,CE =,PE =,vel=,score,Scanning,Override,Groww API,🔎,OI VERDICT`; skip `Monitoring |`, `💓`; last 80 |
| `/api/oi_verdict_summary` | — | `{"ALIGNED_WIN":n,"ALIGNED_LOSS":n,"OPPOSED_WIN":n,"OPPOSED_LOSS":n,"NEUTRAL":n}` tallied from `logs/trade_history/<today>.jsonl` key `oi_verdict_tag` |
| `/api/alerts` | — | `{"alerts":[{"source","type","msg","ts"}]}` — see §Alerts engine |
| `/api/trade/status` | — | copy of `_trade_state` + `"history": _trade_history` |
| `/api/trade/chain` | `index`, `expiry` (required else 400 `{"error":"expiry required"}`) | `fetch_option_chain()` → `{"strikes":[…],"spot":float,"lot_size":int,"error":str}` |
| `/api/trade/expiries` | `index` | `{"expiries":[YYYY-MM-DD]}` (≤12, from instrument.csv, excludes today after 15:30) |
| `/api/indices` | — | `read_market_indices()` → `{"nifty":{last,chg,pct},"banknifty":…,"sensex":…,"_ohlc":{"nifty":{high,low,open},…}}` |
| `/api/trade/chain_quotes` | `s` = comma list of `EXCH_TRADINGSYMBOL` (max 48) | `{"prev_close":{esym:float}}`; per-symbol Groww `/v1/live-data/quote` in `ThreadPoolExecutor(max_workers=8)`; prev = `last_price - day_change` |
| `/api/trade/ltp_batch` | `s` = comma list | `{"ltp":{esym:float},"ts":iso}`; batches of 50 per Groww call. *(No front-end caller — available for external use.)* |
| `/api/mb_ai` | — | copy of `_mb_ai_cache` + `"qs": _qs_cache` + `"qs_ai_enabled": bool` |
| `/api/personal_ai` | — | copy of `_pai_cache` `{output,score,verdict,ts,running,error}` |
| `/api/performance` | — | `_parse_perf_data()` → `{"ts":"HH:MM:SS","sr_events":[…],"signal_events":[…]}` |
| `/api/pivots` | `index` (def NIFTY) | `_read_pivots()` → `{PP,R1..R4,S1..S4,_prev_h,_prev_l,_prev_c,_source,ts,index,error?}` |
| `/api/bot/status` | — | `{bot_id: "running"|"stopped"}` for all registry entries |
| `/api/bot/registry` | — | `{"bots": _BOT_REGISTRY}` |
| `/api/bot/logs` | `id`, `n` (def 60) | `{"lines":[str]}` |
| `/api/engine/expiries` | `index` | `{"index":str,"expiries":[≤6 dates]}` |
| `/api/engine/console` | `n` (def 40) | `{"lines":[…], "running":bool, "pid":int|null}` |
| `/api/trendline_config` | — | contents of `trendline_config.json`, else default `{"premium_min":85.0,"premium_max":200.0,"lots":18,"expiry_date":""}` |
| `/api/trendline_signals` | — | contents of `.trendline_signals.json`, else `{"signals":[],"active_trade":null,"stats":{}}` |
| `/api/trendline_chart` | — | contents of `.trendline_chart_data.json`, else `{"instruments":[],"spot":null}` |
| `/api/trendline_expiries` | — | `{"expiries":[≤12]}` NIFTY expiry_date ≥ today from instrument.csv |
| `/api/trendline_history` | `from`, `to` (def today), `mode` (def `ALL`) | `{"trades":[rec]}` from `logs/trade_history/trendline_<DATE>.jsonl` (skips filenames containing `backtest`); injects `date` if absent |
| `/api/trade_history` | `from`, `to` | `{"trades": read_trade_history(from,to)}` |
| anything else | — | full HTML page |

### POST (body = JSON; `Content-Length` parsed, empty → `{}`)

| Path | Request body | Effect / response |
|---|---|---|
| `/api/prod10_buy` | `index, expiry (YYYY-MM-DD), strike:int, opt_type:"CE"/"PE", lots:int, mode:"manual"/"quick", paper:bool, atr:bool, atr_source:str, mock:bool, quick_pts:float(def 1.5), partial:bool, partial_pct:int(def 50), ltp:float, validate_orders:bool|null` | Validates `expiry && strike>0 && lots>0` else 400. Builds `expiry_token = f"{d.day:02d}{MON_UPPER}{year}"`, `prod10_sym = f"{index}{expiry_token}{strike}{opt_type}"`, `command = f"{lots} {prod10_sym}"`; **overwrites** `.prod10_bridge_cmd.json`. Returns `{"ok":true,"command":…,"mode":…}` |
| `/api/prod10_set_target` | `quick_pts:float>0` | writes bridge `{"command":"set_quick_pts","quick_pts":…}` → `{"ok":true,"quick_pts":…}` |
| `/api/prod10_set_partial` | `partial:bool, partial_pct:int` (must be 10–90) | writes bridge `{"command":"set_partial","partial":…,"partial_pct":…}` |
| `/api/prod10_auto` | `paper:bool` | writes bridge `{"command":"__AUTO__","mode":"auto","paper":…}` |
| `/api/start_prod10` | `{}` | deletes stale bridge file, then `osascript -e 'tell application "Terminal" to do script "cd <BASE> && python3 \"<BASE>/PROD10FEB_ManualBOT_groww_option_trading_final_bot.py\""'` → `{"ok":true}` |
| `/api/auto_mode_status` | `{}` | reads `.auto_mode_status.json` → its JSON, else `{"state":"IDLE"}`. **Known bug: the UI calls this with `fetch()` (GET), which falls through to the HTML page and the JSON parse fails silently — reimplement as a GET route (or both).** |
| `/api/trendline_config` | arbitrary config object | `json.dump(body, indent=2)` to `trendline_config.json` → `{"ok":true}` |
| `/api/run_trendline_backtest` | `expiry(def "2026-06-23"), days(31), premium_min(85), premium_max(200), lots(18)` | runs `TRENDLINE_BACKTEST.py --expiry --days --premium_min --premium_max --lots --out logs/trade_history/trendline_backtest_<YYYYMMDD>.jsonl`, `timeout=300`; returns `{"trades":[…],"out":path}` or `{"error":…}` |
| `/api/bot/start` | `{"id":bot_id,"config":{…}}` | `_bot_start` → `{"ok":bool,"error"?}` |
| `/api/bot/stop` | `{"id":bot_id}` | `_bot_stop` |
| `/api/engine/start` | `{mode:"shadow"|"live", profile, index, expiry, lots, premium_min, premium_max, validate_orders:bool, confirm_live:"YES"}` | `_engine_start` → `{"ok":true,"pid":int,"cmd":str}` or `{"ok":false,"error":…}` |
| `/api/engine/stop` | `{}` | `_engine_stop` → `{"ok":true}` |
| `/api/momentum/config` | any subset of the `_live_cast` map | merge-writes `momentum_config_override.json`; casts: `validate_orders,choppiness_enabled,consec_sl_brake,HARD_SL_ATR_BASED,min_score_filter,velocity_filter`→bool; `atr_source,_vix_config_note`→str; `min_premium,max_premium,velocity_pct,consistency_pct`→float; `lots,atm_range,scan_seconds,poll_seconds`→int. → `{"ok":true}` |
| `/api/personal_ai/run` | `{}` | starts `_run_pai_bg` thread → `{"status":"started"}` or `{"status":"already_running"}` |
| `/api/qs_ai/refresh` | `{}` | `{"ok":false,"status":"disabled"}` if `qs_ai` off; `{"ok":false,"status":"already_running"}`; else spawns `generate_qs_ai` → `{"ok":true,"status":"started"}` |
| `/api/mb_ai/refresh` | `{}` | `{"ok":false,"error":"AI Brain is OFF — toggle it on first"}` / `already_running` / `{"ok":true,"status":"started"}` |

**There is no `/api/place_order`, no `/api/trade/start`, no `/api/trade/exit`.** All real order flow from the UI goes through the PROD10 bridge file.

### `/api/data` snapshot shape (built in `_refresh`)
```
ts, index, spot,
bots: { master, fibo, chart_signal, chart_decision, premium, trade,
        momentum_bot, trendline_bot, signal_monitor },
live_chain, live_option_ltp, consensus,
ai_summary{text,ts,status,error,source}, scalp_plan{...}, oi_ai{...},
features{ai,scalp,ptai_ai,oi_ai,mb_ai,qs_ai}, mins_to_close,
pnl_today, margin, orders, mkt_idx,
pnl_analysis (ptai), pnl_ai, ptai_ok,
oi_snapshot, oi_history[], vix_history[], vix_session_open,
mb_ai, decision_engine
```
`spot` chosen by `_best_spot()`: live Groww index LTP for the current index → else first `_live` bot source among (chart_decision, chart_signal, master, fibo) → else any non-zero → 0.

### Alerts engine (`/api/alerts`)
Stateful, incremental. Module state: `_alert_state{log_path: byte_offset}`, `_alert_bot_idx{bot_id:int}`, `_alert_dedup{(src,type,msg[:60]): epoch}` purged after 300 s, `_oi_snap_last`, `_consensus_last`.
Sources `_SRCS = [('PROD10', logs/groww_bot, 'Groww_Bot_', False), ('MOMENTUM', logs/momentum_bot,'Momentum_Bot_',False), ('MASTER', logs/master_signal,'',True), ('OI·FIBO', logs/signal_monitor,'Signal_Monitor_',False), ('PREMIUM', logs/premium_tracker,'Premium_Tracker_',False)]`. First poll records file size as offset (emits nothing).
Text pattern table `_PAT` (first match wins) → types `buy, sell, sl, target, profit, loss, error, signal_buy, signal_sell` (regexes cover PROD10 + MOMENTUM phrasings; loss detection keys on `💸`). JSON source (MASTER): emits when `direction ∈ (BUY,SELL)` and `confidence ≥ 65`.
Also: in-memory `oi_pcr` stdout patterns (`BUY CE NOW|BREAKOUT SIGNAL`, etc.); OI snapshot transitions (market_signal → STRONG BULLISH/BEARISH or BULLISH/BEARISH; PCR crossings at 1.5 / 1.2 / 0.8 / 0.6; writer_bias flip off NEUTRAL); consensus signal change; VIX spike ±3 % vs ~10 min ago (index `len-6`) and threshold crossings at 25 DANGER / 20 HIGH / 18 ELEVATED / 15 CAUTION.

---

## 3. External files READ (inter-bot contracts)

Helper `_latest(subdir, prefix, ext=".log")` = reverse-sorted newest filename match under `BASE/subdir`. `_tail(path, max_bytes)` reads only the tail. `_tag(d, stale_secs)` adds `_age` ("Ns/Nm/Nh ago") and `_live` (bool) from `d["ts"]` via `_parse_ts` (accepts `%Y-%m-%dT%H:%M:%S`, `%Y-%m-%d %H:%M:%S`, `HH:MM:SS`; future timestamps roll back one day).

| Reader | File(s) | Format & expected keys |
|---|---|---|
| `read_master()` | `logs/master_signal/Master_Signal_*.log` (newest) | JSONL, last parseable line with `ts`. Keys used: `ts, index, spot, direction("CE"/"PE"/"WAIT"), confidence(float %), s1h, s15m, s5m, sprem, rsi1h, rsi15m, pattern, zone, stop, target, rr, sh15m, sl15m`. `_tag(..., 150)` |
| `read_fibo()` | `logs/fibo_analyzer/Fibo_Analyzer_*.log` (tail 120 KB) | **Text regex**: header `FIBONACCI ANALYZER | <IDX> | <ts> | Spot <n>`; `DAY FIB H <h> L <l> (<n> pts <dir> day)`; level lines `^\s+<price>\s+<label>\s+<±pts> pts` (labels containing `SWING_LOW`/`SWING_HIGH` populate `swing_low_15m`/`swing_high_15m`); confluence `(\*+)\s+<price>\s+<±pts> pts \[tags\]`; `1-HR → <x> → <y>`; `PE trigger: … | CE trigger: …`; `--- SUMMARY ---` block; `TRADE SETUP ─── ` block. Output keys: `ts,index,spot,fib_levels[],confluence[{stars,price,dist_pts,tags}],day_high,day_low,day_dir,zone_1h,pe_trigger,ce_trigger,summary,trade_setup`. `_tag(...,200)` |
| `read_chart_signal()` | `logs/chart_level/signals_<YYYY-MM-DD>.jsonl` | last line. Real keys: `ts,index,spot,direction("CE"/"PE"),confidence("HIGH"/"MEDIUM"),reason,entry_type,target_pts,sl_pts,rr_ratio,spot_target,spot_sl,strike,option_ltp`. `_tag(...,90)` |
| `read_chart_decision()` | `logs/chart_level/Chart_Level_*.log` (tail 300 KB) | regex `TRADE DECISION │ …`, `OPTION SUGGESTION │ …`, `SPOT:  <n>`, `BUY <IDX> <strike> (CE|PE) … LTP ₹<n>` → `{ts,spot,decision,option_text,current_ltp,current_strike,current_dir,ltp_by_key{"<strike>_<CE|PE>":ltp}}`, `_tag(...,90)` |
| `read_premium()` | `logs/premium_tracker/Premium_Tracker_*.log` | regex `[HH:MM:SS] SPOT <n> <rest>`, then `(<strike> CE) → <flow> ₹<ltp>` and same for PE → `{ts,spot,raw,ce_strike,ce_flow,ce_ltp,pe_strike,pe_flow,pe_ltp}`, `_tag(...,90)` |
| `read_trade_bot()` | `logs/groww_bot/Groww_Bot_*.log` (last 120 lines) | phrase scan: `Trade cycle completed`/`Ready for next trade`→idle; `Monitoring`+`LTP last seen`→`last_ltp`, active; `Trailing started` + `Dynamic SL: <n>`→`trailing_sl`; `Entry price` ₹n→`entry_price`; `Parsing symbol: <sym>`→`symbol`. Output `{active,ts,status,last_ltp,trailing_sl,entry_price,symbol}` |
| `read_momentum_bot()` | `logs/momentum_bot/Momentum_Bot_*.log` (last 80) | statuses from `Cooldown`/`No momentum signal`/`session complete`→Idle; `MOMENTUM ENTRY`→In trade; `Trail active`/`Trail |`→Trailing; `SIGNAL:`+`vel=`→Signal found; `CLOSED`/`SELL placed`→Trade closed |
| `read_trendline_bot()` | `logs/trendline_bot/TrendlineBot_*.log` (last 20, ts only) + `.trendline_signals.json` | JSON keys: `ts, signals[], active_trade, stats{trades,wins,losses,pnl}` |
| `read_signal_monitor()` | `logs/signal_monitor/Signal_Monitor_*.log` (tail 60 KB) | regex `STRONG CE|STRONG PE|✅ \S+ CE|✅ \S+ PE`, `PDT signal (\w+)`, `FIBO signal (\w+)` → `{ts,combined,pdt,fibo}`, `_tag(...,150)` |
| `read_live_chain()` | `logs/chart_level/live_chain.json` | `{ts, spot, chain}` verbatim, `_tag(...,90)` |
| `read_oi_snapshot()` | `oi_snapshot.json` (written by `calculate_oi_pcr.py` ~60 s) | epoch `timestamp` + `time`; adds `_age_sec`, `_stale` (>300 s), `_ts_disp`. **Full key contract**: `time,timestamp,price,atm,sentiment,pcr_all,pcr_atm,total_oi_ce,total_oi_pe,total_chg_ce,total_chg_pe,resistance[],support[],resistance_strength,support_strength,atm_strikes_oi{strike:{ce_oi,pe_oi}},writer_bias,bullish_score,bearish_score,ce_writing_strikes[],pe_writing_strikes[],max_pain,vol_pcr,total_ce_vol,total_pe_vol,atm_ce_iv,atm_pe_iv,iv_skew,atm_extras,smart_money_ce[{strike,oi_change,…}],smart_money_pe[],market_signal,bull_score_v2,bear_score_v2,momentum_score,signal_list[],call_writing,put_writing,strike_buildups,iv_changes`; UI also uses `atm_momentum{ce_ltp,pe_ltp,ce_ltp_chg,pe_ltp_chg}`. Every read appends to `_oi_history` via `_update_oi_history` (dedup by `time`) |
| `_read_conv_signals()` | `.convergence_signals.json` | `{updated,total,signals:[{time,ts_ms,side,strength,conv_count,accel_count,avg_vel_pct,top_strike,spot,oi_bias,vel_thresh,scan_secs,hits}]}`; last 5 used |
| `_read_auto_mode_status()` | `.auto_mode_status.json` | `{state,ts,mode_label,index}` (+`enabled` referenced by the AI prompt). States: `STARTING, SCANNING, IN_TRADE, TRADE_CLOSED, STOPPED, IDLE` |
| `read_decision_engine()` | `trading_decision_engine/config/strategy.json`, `…/config/profiles/*.json`, `…/logs/events_*.jsonl` | config keys surfaced: `active_profile, trend_threshold, decision_score_threshold, min_resistance_distance, momentum_threshold, premium_velocity_scale, signal_stability_min_seconds, signal_stability_max_seconds, max_trades_per_day, cooldown_seconds, daily_loss_limit, daily_profit_lock`. Events JSONL: `event ∈ {decision, rejected, trade_opened, trade_closed}`, `mode`, `action`, `diagnostics{stage1{failed_checks[]}, engines{name:{passed,score}}}`. Incremental tail via `_de_state{file,offset,cycles,actions,gates,eng,latest,trades,mode,partial}`; if starting mid-session on a file > 30 MB, seek to `size-10MB` and set `partial:true`; trades list capped 30 |
| `_parse_perf_data()` | `logs/fibo_analyzer/*.log` (last 2) + `logs/chart_level/signals_*.jsonl` (last 3) | S/R: split on `🔄 Analysis cycle`, `Spot <n>`, `◄ NEAR` level lines; outcome vs next cycle spot: resistance & move<−8 or support & move>+8 → `RESPECTED`; `abs(move)>5` → `BROKE`; else `WATCHING`. Signals: forward-scan up to 25 later ticks; `fav ≥ target_pts` → WIN, `fav ≤ -sl_pts` → LOSS, else PENDING. Returns last 50 sr / 40 signal events reversed |
| `_read_pivots()` | `logs/chart_level/Chart_Level_*.log`; **yfinance fallback** | regex `<price> [▲▼]<±>pts … Pivot (PP|R1..3|S1..3)`; needs ≥5 keys, `_source="chart-level log"`. Fallback `yfinance.download("^NSEI"/"^BSESN", period=7d, interval=1d)` using the **second-to-last** completed day: `PP=(H+L+C)/3`, `R1=2PP−L, R2=PP+range, R3=H+2(PP−L), R4=PP+3·range`, `S1=2PP−H, S2=PP−range, S3=L−2(H−PP), S4=PP−3·range`, `_source="yfinance"` |
| `read_trade_history()` | `logs/trade_history/*.jsonl` + `logs/groww_bot/Groww_Bot_*.log` + `logs/momentum_bot/Momentum_Bot_*.log` | bot logs scanned for `[TRADE_RECORD] {json}` with keys `ts, symbol, mode, buy_px, sell_px, qty, pnl, exit_reason`; bot label `PROD10` / `Auto`; dedup key `(bot,date,time_exit,symbol,pnl)`; sorted desc by (date,time_exit) |
| `_bot_get_logs()` fallback | `_BOT_LOG_DIRS = {"trendline_scanner":("logs/trendline_bot","TrendlineBot_"), "momentum":("logs/momentum_bot","Momentum_Bot_"), "trade_bot":("logs/groww_bot","Groww_Bot_")}` | last N lines |
| credentials | `ai_config.json` | `groww_api_key`, `groww_totp_secret` |
| instruments | `instrument.csv` (~19 MB, loaded once into `_instruments_for_ltp` as `list(csv.DictReader)`) | columns used: `trading_symbol, groww_symbol, underlying_symbol, expiry_date, instrument_type, strike_price, lot_size` |
| config reads | `trendline_config.json`, `momentum_config_override.json`, `.trendline_chart_data.json` (`{ts,index,premium_min,premium_max,status,instruments:[{symbol,opt_type,ltp,candles,trendlines}],spot}`), `.vix_cache.json` | |
| PTAI module | imports `PERSONAL_TRADING_AI.py` via `importlib.util.spec_from_file_location` and calls `parse_excel_history()`, `parse_lakshmi_intraday()`, `overall_stats()`, `build_market_db()`, `fetch_live_market()`, `market_condition_score()`, `find_similar_days()`, `behavioral_analysis()`, `trading_permission_score()` — which in turn read `Lakshmi.xlsx` / `ayush_previous_data/*.xlsx` |

---

## 4. Files WRITTEN

| File | Written by | Schema |
|---|---|---|
| `.prod10_bridge_cmd.json` | `/api/prod10_buy` | `{"command":"<lots> <INDEX><DDMONYYYY><strike><CE|PE>", "mode":"manual"|"quick", "paper":bool, "atr":bool, "atr_source":str, "mock":bool, "quick_pts":float, "partial":bool, "partial_pct":int, "ltp":float, ["validate_orders":bool]}` |
| " | `/api/prod10_set_target` | `{"command":"set_quick_pts","quick_pts":float}` |
| " | `/api/prod10_set_partial` | `{"command":"set_partial","partial":bool,"partial_pct":int}` |
| " | `/api/prod10_auto` | `{"command":"__AUTO__","mode":"auto","paper":bool}` |
| " | `/api/start_prod10` | **deletes** the file before launching PROD10 |
| `momentum_config_override.json` | `_bot_start("momentum", config)` (whitelist) and `/api/momentum/config` (merge) | whitelist in `_bot_start`: `trade_mode,index,expiry,lots,exit_mode,min_premium,max_premium,atm_range,validate_orders,scan_seconds,poll_seconds,choppiness_enabled,consec_sl_brake,consec_sl_pause_min,HARD_SL_ATR_BASED,HARD_SL_ATR_MULTIPLIER,atr_source,min_score_filter,velocity_filter` |
| `.vix_cache.json` | `_update_vix_history` on every new tick | `{"date":"YYYY-MM-DD","session_open":float,"history":[{"t":"HH:MM","v":float}]}` (≤210) |
| `logs/trade_history/<YYYY-MM-DD>.jsonl` | `_write_trade_jsonl` (appends after each inline-engine exit) | `{date,time_entry,time_exit,bot:"PROD10",mode:"paper"|"live",index,symbol,option,expiry,buy_price,sell_price,qty,lots:1,pnl,exit_reason:""}` — `index/option/expiry` derived by `_parse_fno_sym` |
| `logs/trade_history/trendline_backtest_<YYYYMMDD>.jsonl` | written by the spawned `TRENDLINE_BACKTEST.py --out` | backtest trade records |
| `trendline_config.json` | `POST /api/trendline_config` | full body, `indent=2` |
| `logs/control_panel.log` | `_ensure_control_panel` | appended stdout+stderr of TRADE_CONTROL_PANEL.py |

No OI cache file is written (OI history is in-memory only; `oi_snapshot.json` is read-only from this process).

---

## 5. Bot Control

`_BOT_REGISTRY` (order matters — it drives the UI grid): each entry `{"id","name","script","desc","terminal":bool,"bounds":"x1, y1, x2, y2"}`.

| id | name | script | terminal | bounds |
|---|---|---|---|---|
| `oi_pcr` | OI PCR Analyzer | `calculate_oi_pcr.py` | True | "" |
| `premium` | Premium Direction Tracker | `PREMIUM_DIRECTION_TRACKER.py` | True | `0, 25, 490, 400` |
| `fibo` | Fibonacci Analyzer | `FIBONACCI_TREND_ANALYZER.py` | True | `490, 25, 980, 400` |
| `master_signal` | Master Signal Bot | `MASTER_SIGNAL_BOT.py` | True | `980, 25, 1470, 400` |
| `chart_level` | Chart Level Analyzer | `CHART_LEVEL_ANALYZER.py` | True | `0, 400, 490, 874` |
| `signal_monitor` | Signal Monitor | `SIGNAL_MONITOR.py` | True | `490, 400, 980, 874` |
| `trade_bot` | Trade Bot (PROD10FEB) | `PROD10FEB_ManualBOT_groww_option_trading_final_bot.py` | True | `980, 400, 1470, 874` |
| `momentum` | Momentum Auto Bot | `MOMENTUM_AUTO_BOT.py` | True | "" |
| `trendline_scanner` | Trendline Scanner Bot | `TRENDLINE_SCANNER_BOT.py` | True | "" |

`_PY_BIN = sys.executable` (used for all launches so packages match).

**`_bot_start(bot_id, config=None)`**
1. Look up registry; error `Unknown bot: <id>`; verify script exists (`Script not found: <script>`).
2. If `bot_id=="momentum"` and config given → filter to whitelist and write `momentum_config_override.json` (error `Could not write config override: …`).
3. `terminal == False` → `Popen([_PY_BIN, script], stdout=PIPE, stderr=STDOUT, text=True, bufsize=1, cwd=BASE)`, store in `_bot_procs`, reset `_bot_logs[bot_id]`, start `_bot_log_reader` thread. Refuses if existing proc `poll() is None` → `"Already running"`.
4. `terminal == True` → `pgrep -f <script>`; rc==0 → `"Already running"`. Else `osascript -e` of:
```
tell application "Terminal"
    activate
    do script "cd '<BASE>' && clear && echo '  <name>' && '<_PY_BIN>' '<script>'"
    delay 0.6
    set w to front window
    try
        set current settings of w to settings set "Pro"
    end try
    [set bounds of w to {<bounds>}]
end tell
```
Returns `{"ok":true}` / `{"ok":false,"error":…}`.

**`_bot_stop`**: non-terminal → `terminate()`, `wait(3)`, else `kill()`, set `_bot_procs[id]=None`. Terminal → `pkill -f <script>`.

**`_bot_status_all`**: non-terminal via `proc.poll()`; terminal via `pgrep -f <script>` returncode.

**`_bot_get_logs(bot_id, n=60)`**: in-memory `_bot_logs` tail first; else newest file from `_BOT_LOG_DIRS`; else `[]`.

### Decision engine process manager
`_DE_PROC_MARK = "trading_decision_engine.app.run"`.
- `_engine_running()` → `{"running":bool,"pid":int|None}` via `pgrep -f`.
- `_engine_expiries(index)` → from `instrument.csv` where `underlying_symbol==index and expiry_date >= today`, sorted, first 6, cached 600 s in `_de_expiry_cache`.
- `_engine_start(cfg)` validation: mode ∈ live/shadow; **live requires `confirm_live.strip().upper()=="YES"`**; expiry required; `lots>=1`; `premium_min < premium_max`. Command:
```
[_PY_BIN, "-m", "trading_decision_engine.app.run",
 "--mode", mode, "--index", INDEX, "--expiry", expiry,
 "--lots", n, "--premium-min", pmin, "--premium-max", pmax,
 "--validate-orders" | "--no-validate-orders", "--no-dashboard",
 ("--profile", profile)?]
```
Popen with piped output into `_bot_logs["decision_engine"]` (viewable via `/api/engine/console`).
- `_engine_stop()`: SIGINT → `wait(8)` → terminate → `wait(3)` → kill; if not owned, `pkill -INT -f <mark>`.

---

## 6. Trade Board internals

**Lot size / exchange maps**
```python
LOT_SIZES = {"NIFTY":75,"BANKNIFTY":35,"SENSEX":20,"FINNIFTY":65,"MIDCPNIFTY":75,"BANKEX":15}
EXCH_MAP  = {"NIFTY":"NSE","BANKNIFTY":"NSE","FINNIFTY":"NSE","MIDCPNIFTY":"NSE","SENSEX":"BSE","BANKEX":"BSE"}
```
`_lot_size_from_csv(index, expiry)` scans `instrument.csv` for matching `underlying_symbol` + exact `expiry_date`, returns `int(float(lot_size))`; fallback dict `{"NIFTY":75,"BANKNIFTY":35,"SENSEX":20,"FINNIFTY":65,"MIDCPNIFTY":75}` default 75.

**`fetch_expiries(index)`** — distinct `expiry_date` from instrument.csv ≥ today, but if local time ≥ 15:30 the minimum becomes tomorrow; sorted, first 12.

**`fetch_option_chain(index, expiry)`** — cache `_chain_cache[(INDEX,expiry)] = (ts,result)`, TTL `_CHAIN_CACHE_TTL = 5 s`. Calls `GET /v1/option-chain/exchange/{EXCH}/underlying/{INDEX}?expiry_date=…`. Maps payload `underlying_ltp` → `spot`; per strike from `payload.strikes{strike:{CE:{...},PE:{...}}}` emit
`{strike, ce_sym, pe_sym, ce_ltp, pe_ltp, ce_prev, pe_prev, ce_oi, pe_oi, ce_vol, pe_vol, ce_iv, pe_iv}` (prev from `close`/`prev_close`/`previous_close`; IV from `greeks.iv`, 1 dp). Failure → `{"strikes":[],"spot":0,"lot_size":…,"error":"fetch failed"}`.

**Chain UI/flow (front-end)**
- Toolbar (PROD10 row): `tb-index`, `tb-expiry`, `tb-lots` (+lock `tbToggleLots`), `tb-p10-mode` (manual/quick), quick-target group (`tb-quick-pts`, mode toggle PTS↔₹ profit via `tbQuickTargetPoints()`, SET → `/api/prod10_set_target`), partial group (`tb-partial-btn`, `tb-partial-pct`, SET → `/api/prod10_set_partial`), toggles `PAPER`, `ATR-SL`, `ATR SRC` (HIST ATR ↔ alt), `MOCK`, `VALIDATE`, `⚡ Quick Trade` (`tbToggleQuickTrade`), `▶ Start PROD10` (`/api/start_prod10`).
- Quick Trade row: capital source `api|manual`; FETCH CAPITAL reads `/api/data` → `margin.opt_buy_avail`; `tbComputeMaxPremium()` = `floor(capital / (lots × lot_size))`; strikes above max premium are visually excluded/flagged. While Quick Trade is ON, `_tbQuickRefreshStart()` re-fetches the chain every **5000 ms** and recomputes max premium; clicking a CE/PE cell fires `tbSendToProd10()` **immediately without confirmation**.
- `tbSendToProd10()` reads every control fresh from the DOM, records `_tbClickTs`, appends a `🖱️ Dashboard → PROD10 …` line to the session log, POSTs `/api/prod10_buy` with `{index,expiry,strike,opt_type,lots,mode,paper,atr,atr_source,mock,validate_orders,quick_pts,partial,partial_pct,ltp:_tbSelectedLtp}`, then shows `✅ Sent: <command>` for 3 s.
- `⚡ AUTO` sub-panel (Momentum bot launcher): `mb-index, mb-expiry, mb-lots, mb-exit-mode, TRADE MODE (PAPER|MOCK|LIVE), mb-validate-btn, mb-prem-min/max, mb-strikes, mb-scan-sec, mb-poll-sec, CHOP, CONS SL, ATR SL, ATR SRC, MIN SCORE, VEL FILTER, VEL %, CONS %, VIX AUTO`, `🚀 Auto Bot` → `POST /api/bot/start {id:"momentum", config:{trade_mode,index,expiry,lots,exit_mode,min_premium,max_premium,atm_range,validate_orders,scan_seconds,poll_seconds,choppiness_enabled,consec_sl_brake,HARD_SL_ATR_BASED,atr_source,min_score_filter,velocity_filter}}`; LIVE mode requires a JS `confirm()`. Capital calculator uses `/api/groww_capital` + `/api/lot_size`.
- Prev-close colouring uses `/api/trade/chain_quotes?s=…` for up to 48 visible symbols.

**PROD10 status parsing (front-end, from `/api/prod10_logs`)** — `tbPollProd10Logs()` polls every **500 ms** together with `/api/trade/status`. If `offline`, renders "PROD10 STATUS ● OFFLINE" + the server's error string. Otherwise it selects the **last** line matching each stage and extracts `[HH:MM:SS.mmm]`:

| Row | Match phrases |
|---|---|
| 🖱 Dashboard click | client-side `_tbClickTs` |
| 📥 PROD10 received | `Command entered` \| `[DASHBOARD]` |
| 💰 LTP fetched | `Entry price` \| `LTP for` \| `LTP from cache` (falls back to `₹<n>`) |
| ✅ BUY placed | `BUY Order placed` \| `BUY placed` (fallback `took <n>s`) |
| 📈 Trail started | `Trail started` \| `Trailing started` |
| 🔻 Trail/SL hit | `Trailing HIT` \| `DYNAMIC SL HIT` \| `Max trail time` |
| 🔄 SELL started | `Placing SELL` |
| ✅ SELL placed | `SELL Order placed` \| `SELL EXECUTED` |
| 💓 Heartbeat | `Monitoring` \| `💓` |
| ❌ Last error | `FAIL` \| `❌` (last 60 chars) |

`ACTIVE` badge = trail line present and no hit line. Log lines are colour-coded by substring (`SELL EXECUTED/PROFIT/Trailing started/BUY Order placed` = bull; `Trailing HIT/DYNAMIC SL/SL HIT` = orange; `SELL Order placed/Placing SELL` = warn; `❌/FAIL/LOSS` = bear; `🎭/MOCK` = violet; `🌐/DASHBOARD` = info).

---

## 7. Trailing / inline trade engine

State: `_trade_state` (fields `status, symbol, exchange, order_id, avg_price, qty, entry_ts, buy_exec_ms, ltp, highest, hard_sl, trail_exit, trail_active, unrealised, exit_reason, exit_price, exit_exec_ms, total_ms, pnl, log[], paper, error, atr_val, atr_based`), status machine `IDLE → BUYING → ACTIVE → EXITING → DONE`. `_trade_history` list of `{entry_ts,exit_ts,symbol,direction,buy,sell,qty,pnl,paper}`. `_tlog(msg)` prepends `HH:MM:SS.mmm  msg`, cap 300 entries, also prints `[TRADE] …`.

**`trade_start(sym,exch,qty,paper,hard_sl_pts,trail_start,trail_step,max_sec,atr_based=False,atr_multiplier=1.0)`** — refuses unless status in `("IDLE","DONE")`; resets state; spawns `_buy_and_trail` daemon thread.

**`_buy_and_trail`**
- Paper: `GET /v1/live-data/ltp {segment:FNO, exchange_symbols:[f"{exch}_{sym}"]}` (default 50.0), `bms = 50`.
- Live: `POST /v1/order/create {trading_symbol, quantity, validity:"DAY", exchange, segment:"FNO", product:"MIS", order_type:"MARKET", transaction_type:"BUY", order_reference_id:"DB"+HHMMSSmmm}` (reference id is mandatory in practice). Error paths: `_err`, top-level `status=="FAILURE"`, missing `payload.groww_order_id`, `order_status ∈ (REJECTED,FAILED,CANCELLED)` → status back to IDLE with `error`. Then `_wait_fill(oid)`; if avg 0, fall back to LTP.
- ATR: `hard_sl = round_to_0.05(avg - hard_sl_pts)` is **always set first** so the trail is never unprotected. If `atr_based`, fetch ATR in a background thread and `join(timeout=2.0)`; on success `hard_sl = round_to_0.05(avg - 1.5*atr_val)` and `trail_step = round(atr_val*atr_multiplier,2)`.
- Sets `status=ACTIVE, avg_price, highest=avg, ltp=avg, hard_sl, entry_ts=iso(seconds)`, then `_trail_loop`.

**`_trail_loop`** — starts `_start_trail_ltp_feed(esym)` (dedicated REST-poll thread writing `_trail_ltp`), then loops at `sleep(0.015)`:
1. exit loop if status ≠ ACTIVE;
2. read cached LTP (only if `_trail_ltp["esym"]==esym`);
3. heartbeat `_tlog` every 30 s with elapsed/LTP/high/unrealised;
4. new high → `_tlog("New high …")`; update `ltp,highest,unrealised=(ltp-avg)*qty`;
5. `time-t0 >= max_sec` → `_do_sell(..., "MAX TIME REACHED")`;
6. `0 < ltp <= hard_sl` → `_do_sell(..., f"HARD SL @ ₹{ltp}")`;
7. once `highest >= avg + trail_start`: `trail_exit = round_to_0.05(highest - trail_step)`, set `trail_active=True`, log only on change; `ltp <= trail_exit` → `_do_sell(..., f"TRAIL HIT @ ₹{ltp}")`.

**`_do_sell(sym,exch,qty,avg_price,paper,reason,t0_epoch)`** — guard status=="ACTIVE" → `EXITING`. Paper: exit = cached LTP (or avg), `exec_ms=50`. Live: MARKET SELL with `order_reference_id="DS"+HHMMSSmmm`; on failure revert to ACTIVE and set `error`; else `_wait_fill(sid, max_sec=5.0)`, fallback to cached LTP. Then `pnl=(exit-avg)*qty`, `status=DONE`, record `exit_exec_ms`/`total_ms`, append to `_trade_history` (direction inferred from CE/PE suffix), call `_write_trade_jsonl`, and log `DONE | Sell ₹… | P&L … | Exit exec …ms | Total Ns Nms`.

**`trade_force_exit()`** — only when status=="ACTIVE"; recovers `t0` from `entry_ts` (fallback `now-60`); spawns `_do_sell(..., "MANUAL EXIT")`.

**`_wait_fill(order_id, max_sec=8.0)`** — polls `GET /v1/order/status/{id}?segment=FNO` every 50 ms; success statuses `COMPLETE, EXECUTED, DELIVERY_AWAITED`; terminal failures `REJECTED, FAILED, CANCELLED` → `(0.0, status, remark)`; no status → `(0.0,"TIMEOUT","")`. Then up to 3 attempts (0.5 s apart) at `GET /v1/order/trades/{id}?segment=FNO` computing VWAP over `trade_list[{price,quantity}]`; final fallback `average_price`/`avg_price` from order status.

**ATR helpers** — `_ema(data, period)` (SMA seed then k=2/(p+1)); `_atr(high,low,close,period=14)` using numpy true range then EMA; `fetch_atr(trading_symbol, exchange, period=14)` resolves `groww_symbol` from instrument.csv and tries lookback windows **[90, 300, 600] minutes** against `GET /v1/historical/candles?exchange&segment=FNO&groww_symbol&start_time&end_time&candle_interval=1minute` (candle tuple indices 2=high, 3=low, 4=close), requires `len ≥ period+2`.

> **Important**: this whole engine is reachable only through `GET /api/trade/status` (read) — there is **no HTTP route that calls `trade_start` or `trade_force_exit`** in the current file. Reimplementations should either wire routes (`POST /api/trade/start`, `/api/trade/exit`) or keep it as an internal/legacy path, since the live UI delegates execution to PROD10 through the bridge file.

---

## 8. PnL / today's orders / margin readers

Shared client: `_groww_get(path, params)` → `GET https://api.groww.in{path}` with headers `Accept: application/json`, `Authorization: Bearer <token>`, `X-API-VERSION: 1.0`, `timeout=8`, returns `r.json()["payload"]` on 200; a 401 zeroes `_ltp_token_ts` to force re-auth. `_groww_post(path, body)` → same headers + `Content-Type`, `timeout=10`, returns raw JSON; treats 200/400/422 as parsable, other codes → `{"_err":"http_<code>","_body":…}`; 401 → `{"_err":"auth_401"}`; exceptions → `{"_err":str(e)}`.

| Reader | Endpoint(s) | TTL | Output |
|---|---|---|---|
| `read_today_pnl()` | `GET /v1/positions/user?segment=FNO`, then one batched `GET /v1/live-data/ltp?segment=FNO&exchange_symbols=[…]` for open legs | `_PNL_CACHE_SECS = 30` | `{ts, total_pnl, unrealised, total_with_open, trades:[{sym,exchange,realised,net_qty,buy_qty,sell_qty,is_open,avg_price,ltp,unrealised}], count, wins, losses, open, error}`. Position fields consumed: `trading_symbol, exchange, realised_pnl, quantity, credit_quantity, debit_quantity, net_price`. No token → `error:"no_token"` |
| `read_margin()` | `GET /v1/margins/detail/user` | `_MARGIN_CACHE_SECS = 60` | `{ts, clear_cash, margin_used (net_margin_used), brokerage (brokerage_and_charges), opt_buy_avail (fno.option_buy_balance_available), opt_sell_avail, fno_margin_used (net_fno_margin_used), span_used, exposure_used}` |
| `read_today_orders()` | `GET /v1/order/list?segment=FNO&page_size=50` | `_ORDERS_CACHE_SECS = 30` | `{ts, orders:[{sym,status,type,qty,filled,avg_fill,price,order_type,product,created}]}` from `order_list[*]{trading_symbol,order_status,transaction_type,quantity,filled_quantity,average_fill_price,price,order_type,product,created_at}` |
| `/api/groww_capital` | direct `_req.get` (not `_groww_get`) to `/v1/margins/detail/user`; requires `data["status"]=="SUCCESS"` | none | `{ok, option_buy_balance, clear_cash}` |

**Auth**: `_get_ltp_token()` caches an access token for **7200 s**; obtains it by `sys.path.insert(0, BASE)`, importing `growwapi.GrowwAPI`, `pyotp`, and `groww_token.get_access_token(_GROWW_API_KEY, _GROWW_TOTP_SECRET)`; creds from `ai_config.json`. Single shared `requests.Session()` `_ltp_session`.

`_fetch_live_option_ltp(strike, direction, index)` — nearest expiry ≥ today from instrument.csv, exchange `BSE` for SENSEX else `NSE`, matches `instrument_type == CE|PE` and `int(float(strike_price)) == strike`, then `GET /v1/live-data/ltp?segment=FNO&exchange_symbols={EXCH}_{trading_symbol}`.

---

## 9. Index quotes + VIX

**Index state**: `_idx_state{nifty,banknifty,sensex}`, `_idx_prev{sym: prev_close}`, `_idx_ohlc{label:{high,low,open}}`; `_idx_entry(ltp, prev)` → `{"last":round2, "chg":ltp-prev, "pct":chg/prev*100}`.

- `_fetch_idx_quote()` (alias `_fetch_idx_prev_close()`): for `("NIFTY","NSE","CASH","nifty"), ("BANKNIFTY","NSE","CASH","banknifty"), ("SENSEX","BSE","CASH","sensex")` → `GET /v1/live-data/quote?exchange&segment&trading_symbol`; `prev = last_price - day_change`; OHLC from `ohlc{high,low,open}` with `high/low/day_high/day_low/open` fallbacks; prints `[idx ohlc] …`.
- `_idx_refresh_loop()`: single call `GET /v1/live-data/ltp?segment=CASH&exchange_symbols=["NSE_NIFTY","NSE_BANKNIFTY","BSE_SENSEX"]` every **3 s** (~20 calls/min); OHLC/quote refresh spawned every **60 s**.
- `read_market_indices()` returns `_idx_state` + `"_ohlc"`.

**VIX** — `_vix_fetch_loop()` uses its own `requests.Session` with headers:
```
User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36
Accept: application/json, text/plain, */*
Referer: https://www.nseindia.com/
```
Cookie priming: `GET https://www.nseindia.com/` (timeout 5) before the loop **and again on every error**. Then `GET https://www.nseindia.com/api/allIndices` (timeout 7), find `data[*].index == "INDIA VIX"` → `last`. Sleep **120 s**.
`_update_vix_history(vix, "HH:MM")` dedups by `t`, records `_vix_session_open[0]` on first tick, caps at `_VIX_HISTORY_MAX = 210`, and rewrites `.vix_cache.json` (`{date, session_open, history:[{t,v}]}`) each tick. `_load_vix_cache()` at startup discards the file if `date != today`, else restores history + session_open and prints `[VIX] Loaded N cached ticks…`.

---

## 10. AI integrations

**`_try_claude_cli(prompt, timeout=45, feature_key="")`** — `shutil.which("claude")`; returns `""` if absent. Command `[claude, "-p", prompt, "--output-format", "json"]`, plus `["--resume", _ai_session_id]` when a session id exists (single persistent session shared by every feature, visible in the VSCode session switcher). Registers the Popen in `_running_procs[feature_key]`; polls every 0.5 s and **terminates early if `_features[feature_key]` is switched off**; on timeout terminate→kill and return "". On rc==0 parses JSON, persists `session_id`, extracts `result` (or `content[0].text`), falls back to raw stdout on parse failure.

Feature flags: `_features = {"ai":False,"scalp":False,"ptai_ai":False,"oi_ai":False,"mb_ai":False,"qs_ai":False}` — all default OFF, toggled via `GET /api/toggle?f=…`.

| Job | Prompt builder | Inputs | Output store | Timeout |
|---|---|---|---|---|
| `generate_ai_summary` | `_build_prompt(snap)` | master, fibo (confluence top4, triggers), chart_signal, consensus, spot/index, day range position | `_ai_summary{text,ts,status,error,source:"Claude Code CLI"}`; statuses `init/ok/no_data/no_subscription` | 45 |
| `generate_scalp_plan` | `_build_scalp_prompt(snap)` | mins-to-close (`_mins_to_close()` vs 15:30), nearest fib level above/below, consensus, master scores | `_scalp_plan` (keeps only first non-empty line) | 25 |
| `generate_oi_summary` | `_build_oi_prompt(snap)` | returns `""` (skips) if `oi_snapshot` missing or `_stale`; uses pcr_all/pcr_atm/atm/sentiment/writer_bias/scores/ce_writing/pe_writing/market_signal/bull_score_v2/bear_score_v2/momentum_score/signal_list/max_pain/vol_pcr/iv_skew/smart_money_*/resistance/support/total & change OI/atm_strikes_oi | `_oi_ai` | — |
| `generate_mb_ai` | `_build_mb_ai_prompt(snap) -> (prompt, n_lines)` | VIX history + zones, OI block, Fibonacci, Master, Consensus, Chart decision, Premium flow, Momentum bot, `.convergence_signals.json` (last 3), trendline signals (last 3), `.auto_mode_status.json` | `_mb_ai_cache{intraday,longterm,key_levels,risks,bottom_line,ts,status,error,context_lines}` parsed by `_extract()` against labels `📊 INTRADAY VIEW (Next 1-2 hours):`, `📈 LONG-TERM VIEW (Positional/Swing — 2-5 days):`, `⚡ KEY LEVELS TO WATCH:`, `⚠️ RISKS:`, `💡 BOTTOM LINE:`; if neither intraday nor longterm parsed, whole response → `intraday` | 60 |
| `generate_qs_ai` | `_build_qs_prompt(snap)` | short prompt: spot (falls back to `oi_snapshot.price`), OI levels, consensus, master, premium, VIX | `_qs_cache{text,ts,status,error}`; statuses `idle/running/ok/no_data/no_cli/error`; `no_cli` message tells the user to `npm install -g @anthropic-ai/claude-code` | 60 |
| `_run_ptai_ai` | `_build_ptai_ai_prompt(analysis, today_pnl)` | PTAI analysis + today's Groww PnL; asks for ≤180 words: recommendation / direction bias / position size / behavioral warning | `_ptai_ai{text,ts,status}` (`init/loading/ok/no_data/no_cli`) | 60 |

**PTAI (non-LLM) pipeline** — `_ptai_load_history()` (once) → `{daily_pnl, daily_trades, expiry_days, intraday, stats}` + `_ptai_mktdb`. `_run_ptai_analysis()` → `fetch_live_market`, `market_condition_score`, `find_similar_days`, `behavioral_analysis`, `trading_permission_score` → `_serialize_ptai(...)` producing `{ts, live, mkt_score, mkt_bkdwn{k:{pts,max,val,meaning}}, sim{…,best_pnl,best_date,worst_pnl,worst_date,top5[{date,vix,gap,dow,pnl,sim}]}, behav{risks[{type,detail,weight}],insights[],risk_score,recent_wr,recent_avg}, perm_score, verdict, perm_bkdwn, stats{total_days,win_days,loss_days,win_rate,total_pnl,avg_win,avg_loss,best_*,worst_*,yearly{}}}`.

**`_run_pai_bg()`** (separate, subprocess-based) — runs `PERSONAL_TRADING_AI.py`, strips ANSI (`\x1b\[[0-9;]*[a-zA-Z]`), regexes `(?:Permission Score|PERMISSION SCORE)[^\d]*(\d+)` and `\b(NO_TRADE|CAUTION|NORMAL|HIGH_CONFIDENCE)\b` → `_pai_cache{output,score,verdict,ts:"HH:MM",running,error}`.

**`build_consensus(master, fibo, csig, sigmon)`** — additive bull/bear scoring:
- master (`_live`): CE/PE with confidence ≥60 → weight 3 if ≥75 else 2; pattern keywords (`HAMMER/BULL/MORNING` +1 bull; `SHOOTING/BEAR/EVENING` +1 bear); `s5m` and `sprem` add `max(0,s)` bull / `max(0,-s)` bear.
- fibo (`_live`): `trade_setup` containing CE/PE (excluding `NO TRADE`, `CONFLICT`) → ±2.
- chart signal: counted only if **≤120 s fresh**; weight 3 if `confidence=="HIGH"` else 2.
- signal monitor (`_live`): `STRONG CE`→+2 bull, `CE`→+1, `STRONG PE`→+2 bear, `PE`→+1.
Classification: `bull≥6 & bull>bear` → `"STRONG CE ▲▲"/strong-bull`; `bull≥3` → `"CE ▲"/bull`; `bear≥6` → `"STRONG PE ▼▼"/strong-bear`; `bear≥3` → `"PE ▼"/bear`; else `"WAIT ─"/neutral`. Returns `{signal, cls, summary, bull, bear, sources[]}`.

---

## 11. 🛡 Control tab + TRADE_CONTROL_PANEL auto-start (port 8790)

`_ensure_control_panel()` runs during `main()` before `serve_forever()`:
```python
socket.create_connection(("127.0.0.1", 8790), timeout=0.5)  # already running → return
log = open(os.path.join(BASE, "logs", "control_panel.log"), "a")
subprocess.Popen([sys.executable, os.path.join(BASE, "TRADE_CONTROL_PANEL.py")],
                 stdout=log, stderr=subprocess.STDOUT, cwd=BASE)
```
Prints `🛡  Trade Control Panel auto-started → http://127.0.0.1:8790` or a `⚠️ Could not auto-start…` warning.

Front-end `initControlTab()`: probes `http://<location.hostname||127.0.0.1>:8790/` with `fetch(url,{mode:'no-cors'})`; on success shows `#controlFrame` (iframe, src set once) and hides `#controlHint`; on failure hides the iframe and shows the hint telling the user to run `python3 TRADE_CONTROL_PANEL.py`.

---

## UI tabs (functional)

Header (always visible): `#htitle`, `#mkt-ticker` (NIFTY/BANKNIFTY/SENSEX cards, click-to-pin primary, order persisted in `localStorage['idx_order']`, polled `/api/indices` every **1 s**), `#htime` live clock (1 s), `#mtc-badge` market countdown to 15:30 / "Opens Xh Ym" (weekday + 9:15–15:30 aware), `Refresh #countdown` (`const R=15`, `load()` hits `/api/data` when it reaches 0), theme colour picker (`#picker-btn`, BUY CE/PE colour + glow persisted in localStorage), notification bell `#notif-badge`/`#notif-panel`/`#notif-mute-btn` (polls `/api/alerts` every **5 s**, WebAudio beeps per alert type, ≤60 items, unread counter), `#bbar` bot status strip.

Tabs (`switchTab(id, btn)`):
1. **📡 Live Dashboard** — consensus box (`#csig/#csmry/#csrc/#cbull/#cbear`), Scalp Plan card with ON/OFF toggle (`scalp`), Key Levels table (`#lvlbody`, `#swing-danger`), Master Signal card, Pivot card (`/api/pivots` every **60 s**, source badge), Fibonacci card, Option Signal card, Premium tracker card, Trade Bot card, Signal Monitor card, AI Summary card with toggle (`ai`) + source badge.
2. **🔬 OI Intelligence** — everything from `oi_snapshot` + `oi_history`: PCR all / PCR ATM±3, sentiment, writer bias, total CE/PE OI + change, max pain (+distance), vol PCR, ATM IV, resistance wall / support floor cards, support↔resistance range bar with spot marker + verdict, top CE/PE OI strike lists, 10-factor market-direction banner (bull/bear/momentum scores + component list), smart-money CE/PE tables, call/put writing rows, ATM momentum card (action, reason, CE/PE score, target/stop), strike-buildup table, IV-change spikes, PCR change card, writer activity rows, key levels. Re-renders every **20 s**; `oiChartOpen()` opens an intraday canvas chart over `oi_history` with series toggles `{ce, pe, pcr, spot, delta}`; `toggleOIAI` flips `oi_ai`.
3. **🚀 Trade Board** — see §6: PROD10 toolbar, ⚡ AUTO panel, option chain (spot, refresh badge, quick-trade badge, click CE/PE to select), selected-symbol display + `BUY <dir> <strike> → PROD10` button, draggable divider, AUTO MODE v2 status panel (`/api/auto_mode_status`, 5 s), PROD10 STATUS timing breakdown, log viewer with PROD10 / ⚡ AUTO BOT tabs + Clear, OI filter tally row (`/api/oi_verdict_summary`), session trade-history table (from `/api/trade/status.history`).
4. **💹 PnL Status** — today's P&L card (big number, count/wins/losses/open, unrealised, total incl. open), daily-target progress bar with editable target (`localStorage['nifty_pnl_target']`) and alarm toggle, TRADING PERMISSION card (verdict, permission score bar, market score bar, recent win rate, direction bias, position size, breakdown), LIVE MARKET card (NIFTY/VIX/Gap/PCR/market open, market-condition score + breakdown), 3-year stats tiles, trade-history table with from/to date filters (`/api/trade_history`), Personal AI runner (`/api/personal_ai`, `/api/personal_ai/run`, polled while running).
5. **📈 Performance** — two tables from `/api/performance` (30 s): S/R level events (Time, Level, Label, Type, Near at, Result) and option-signal events (Time, Spot, Reason, Max Move, Result).
6. **🤖 Bot Control** — cards generated from `/api/bot/registry`; per-card status badge, Start/Stop (`/api/bot/start|stop`), inline log pane for non-terminal bots (`/api/bot/logs?id&n=30`), momentum radio group `momentum-mode` → `config.trade_mode`; Start All / Stop All; status refresh every **4 s**.
7. **🔭 Scanner** (Trendline) — status badge, start/stop `trendline_scanner`, config form (expiry from `/api/trendline_expiries`, premium min/max, lots, toggles: Ascending/Descending/Horizontal TL, Spot Confirm, Volume Surge + multiplier, % Confirm + value) saved via `POST /api/trendline_config`, "Apply Ideal" preset, today's stats (trades/wins/losses/PnL), active trade panel, signals list (`/api/trendline_signals`), log viewer (`/api/bot/logs?id=trendline_scanner&n=80`), live status panel (spot, bars, TL count, in-range, watching), NIFTY + option chart from `/api/trendline_chart`, demo-guide modal, backtest section (from/to/mode → `/api/trendline_history`; fresh run → `/api/run_trendline_backtest`) with summary + trades table. Polls every **8 s**.
8. **🧠 AI Brain** — Quick Summary block (`#qs-text`, toggle `qs_ai`, manual `POST /api/qs_ai/refresh`, poll `/api/mb_ai` every **2.5 s** while running, auto every 5 min) and the dual AI Brain report (`intraday`, `longterm`, `key_levels`, `risks`, `bottom_line`, ts, `context_lines`), toggle `mb_ai`, manual `POST /api/mb_ai/refresh`, auto refresh timer 300000 ms; OFF state instructs `claude login`.
9. **🌡 VIX** — Market Regime cards: NIFTY DAY RANGE (fibo day_high/low, fallback `mkt_idx._ohlc.nifty`; labels WIDE ≥1.5 %, MODERATE ≥0.75 %, else NARROW), ATM STRADDLE (`oi_snapshot.atm_momentum.ce_ltp+pe_ltp`; HIGH IV >200, NORMAL >120, else LOW), PREMIUM ACTIVITY (`ce_ltp_chg`/`pe_ltp_chg`; HIGH ≥8, MODERATE ≥3, else STAGNANT; bias inferred from signs), REGIME SCORE 0–6 = `rangeScore(0-2) + premScore(0-2) + vixScore(0-2)` where `vixScore = 2 if vix≥15 or sessΔ≥2% ; 1 if vix≥12 or sessΔ≥0.8%`, plus `bothSideWhipsaw` (range ≥0.8 % with premium move <3 and a falling side), `premCrush` (both deltas <0), IV-squeeze proxy (`avgATMIV < VIX/√252 × 0.8`). Verdict labels: `⚡ THETA DECAY`, `↔ BOTH-SIDE CHOP`, `— SIDEWAYS` (≤1), `〜 MIXED` (≤3), `↗ TRENDING` (≤5), `🔥 STRONG TREND`. VIX panel: current + regime (`ULTRA CALM <12, CALM <15, MODERATE <18, ELEVATED <22, DANGER ≥22`), session Δ%, 10-min Δ% (index `len-6`), session high/low/open/range, 30-min velocity, tick count, timestamp, hover sparkline over `vix_history`; empty-state messages differ for closed market vs NSE rate-limiting.
10. **⚡ Decision Engine** — header badges (mode, running, profile, events file); LAUNCH CONTROL form (`dec-mode` shadow/live, `dec-profile`, `dec-index`, `dec-expiry` from `/api/engine/expiries`, `dec-lots`, `dec-pmin`, `dec-pmax`, `dec-validate`, `dec-live-confirm` "Type YES to arm LIVE" shown only for live) → `/api/engine/start`; Stop button with a `confirm()` warning → `/api/engine/stop`; console pane polling `/api/engine/console?n=40` every **3 s**; cards for latest decision, rejection gates (count + %), per-engine pass %/avg score in fixed order `trend, market_structure, support_resistance, premium_momentum, breakout, market_strength, option_selection, volatility, trading_rules, risk, signal_stability`, aggregate stats, last 10 trades, strategy config; offline message pointing at `trading_decision_engine/logs/events_*.jsonl`.
11. **🛡 Control** — iframe of the port-8790 panel (§11) with fallback hint.
12. **🗺️ Guide** — static documentation of the bot ecosystem, data sources (`ayush_previous_data/*.xlsx`, `Lakshmi.xlsx`, `instrument.csv`), and workflows.
