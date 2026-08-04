# PROD10FEB Manual Trading Bot — Rebuild Spec

> Part of the end-to-end rebuild documentation. Master document: ../../REBUILD_BLUEPRINT.md
> Generated 2026-08-04 from a full code survey. Treat all constants, filenames,
> JSON keys and printed strings here as EXACT contracts.

---


---

# REBUILD SPEC — `PROD10FEB_ManualBOT_groww_option_trading_final_bot.py`

Source: `/Users/ayush/Documents/eclipse-workspace/Groww_Python_Trading_Bot-main/PROD10FEB_ManualBOT_groww_option_trading_final_bot.py` (3832 lines, single-file script, no classes except `Tee` and `_RateLimiter`).

## 0. Module-level bootstrap order (import-time side effects — order matters)

1. Imports: `os, re, json, threading, concurrent.futures(ThreadPoolExecutor, as_completed), pyotp, openpyxl(Workbook, load_workbook), playsound3(playsound), datetime(datetime,timedelta), threading.Lock, requests, sys, time, numpy as np`.
2. `session = requests.Session()` — global shared session for all GET (live-data, positions, margins, option-chain).
3. `MOMENTUM_SAMPLES = 3`, `MOMENTUM_DELAY = 0.5`.
4. `LOG_FILE_PATH = setup_persistent_logger()` — creates `<script_dir>/logs/groww_bot/`, file `Groww_Bot_%Y-%m-%d_%H-%M-%S.log`, opened `"a", buffering=1, encoding="utf-8"`. A `Tee` class wraps `sys.stdout`/`sys.stderr`, prefixing every new line with `[HH:MM:SS.mmm] ` (`"%H:%M:%S.%f"[:-3]`). Prints `📝 Logging started. Log file: {log_path}`.
5. Credentials (module globals, see §2).
6. `PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))`; `csv_path = PROJECT_ROOT/instrument.csv` (printed); `convert_csv_to_json = "yes"`.
7. `from whatsapp_gateway import send_whatsapp as send_telegram, start_webhook_server` — **all "telegram" calls are actually Twilio WhatsApp**.
8. Sound constants; `_QUICK_RUNTIME_TARGET=[None]`, `_QUICK_RUNTIME_PARTIAL=[None]`.
9. `DEFAULT_PRODUCT="MIS"`, `ORDER_PRODUCT_MAP={"MIS":"MIS","NRML":"NRML"}`.
10. `from growwapi import GrowwAPI` (wrapped in try/except printing `❗ growwapi module not found.`); `from groww_token import get_access_token as get_cached_access_token`.
11. `groww, access_token = groww_init(api_key)`.
12. TCP keep-alive patch (§7).
13. `ltp_lock = threading.Lock()`; `_live_data_limiter = _RateLimiter(rate=4.0)`.
14. `CONFIG = {...}` then `_instruments_cache = {...}` then `CONFIG["spot"] = get_index_spot_price(CONFIG["index"], access_token)`; prints `🎯 Using {index} with spot price: {spot}`.
15. `instruments_data = load_instruments_from_json()`; then build `symbol_index` dict.
16. `start_option_chain_prefetcher()` daemon thread (prints `🔁 Option-chain prefetcher started.`).
17. `if __name__ == "__main__":` banner → `display_account_summary` → bridge thread → menu REPL.

---

## 1. CONFIG dict — every key, exact default, meaning

```python
CONFIG = {
    "index":                     "NIFTY",       # NIFTY | SENSEX | BANKNIFTY | FINNIFTY
    "expiry":                    "2026-04-28",  # must equal instrument JSON expiry_date (YYYY-MM-DD)
    "min_premium":               90,            # auto/premium-scan lower bound ₹
    "max_premium":               230,           # auto/premium-scan upper bound ₹
    "lots":                      16,            # lots for directional + auto mode
    "book_profit":               1050,          # declared, NEVER read anywhere (dead key)
    "target_pnl":                6000,          # auto mode: cumulative ₹ P&L stop-for-day
    "spot":                      0,             # overwritten at import by get_index_spot_price()
    "TRAIL_START_PROFIT":        1,             # pts of profit (high - entry) before trailing arms
    "TRAIL_STEP":                0.75,          # fixed trail distance in pts (used when TRAIL_SL_ATR_BASED False)
    "TRAIL_SL_ATR_BASED":        False,         # True → trail step = ATR × TRAIL_SL_ATR_MULTIPLIER
    "TRAIL_SL_ATR_MULTIPLIER":   1.0,           # 0.5 tight … 1.5 loose
    "POLL_INTERVAL":             0.50,          # seconds between LTP polls in manual/directional/auto monitor
    "MAX_TRAIL_TIME":            3600,          # seconds max in a manual/directional trade
    "HARD_SL_POINTS":            6.0,           # fixed SL pts below entry; ALSO the floor for HIST-ATR SL
    "HARD_SL_ATR_MULTIPLIER":    1.5,           # 5-min ATR × this = raw SL pts (quick mode)
    "VALIDATE_ORDERS":           False,         # True → poll order status + fetch executed price
    "PAPER_TRADING":             True,          # True → simulate all orders (no exchange calls)
    "QUICK_TRAIL_BUFFER":        1.0,           # DEPRECATED / unused
    "QUICK_TRAIL_GAP":           1.5,           # DEPRECATED / unused
    "user_confirmation_needed":  False,         # auto mode: input("Proceed BUY ...? (y/n)") gate
    "ENABLE_EMA_CHECK":          False,         # declared, unused
    "ENABLE_ADX_CHECK":          False,         # declared, unused
    "ENABLE_RSI_CHECK":          False,         # declared, unused
    "ENABLE_VWAP_CHECK":         False,         # declared, unused
    "ENABLE_LOGICAL_CONDITIONS_CHECK": False,   # declared, unused
    "DIRECTIONAL_MODE": {"prefer_mid_premium": True},  # legacy, read into dir_cfg but never used
}
```

Runtime-only key **not** in the literal: `CONFIG["MOCK_LTP_RUN"]` (bool) — injected by the dashboard bridge; read via `.get()` in `log_trade_to_excel` and the manual monitor loop.

Other module constants: `CACHE_EXPIRY_SECONDS = 15` (option-chain cache TTL), `AUTO_V2_CONFIG` (§5), `MONTHS` map (JAN/JANUARY→'01' … DEC/DECEMBER→'12'), `SOUND_PROFIT="coin.mp3"`, `SOUND_SL="SL_HIT.mp3"`, `SOUND_user_input="User_input.WAV"` (declared, unused).

---

## 2. Credentials / auth

- `api_key = "<REDACTED>"` — a **hardcoded Groww JWT string literal on line 99** of the bot file (not env, not file). It's the vendor API key passed to token minting.
- `totp_gen = pyotp.TOTP('<REDACTED_32CHAR_BASE32_SECRET>')` — line 100; module-level TOTP object that is **never used** (dead). The same literal secret is passed *again inline* as the 2nd arg in `groww_init`.
- `groww_init(api_key)`:
  ```python
  access_token = get_cached_access_token(api_key, '<REDACTED_TOTP_SECRET>')  # groww_token.get_access_token
  client = GrowwAPI(access_token)
  print(access_token)                       # token echoed to stdout AND the log file
  print("✅ Groww API Initialized Successfully")
  return client, access_token               # on failure: print "❌ Groww login failed: {e}"; raise
  ```
- `groww_token.py` (`get_access_token(api_key, totp_secret, force_refresh=False, verbose=True)`):
  - Cache file `.groww_token.json` next to script, schema `{"token": str, "expiry": <epoch float>, ...}`; `CACHE_PATH+".lock"` = `.groww_token.json.lock`.
  - `SAFETY_MARGIN_SEC = 15*60` (refresh 15 min early), `FALLBACK_TTL_SEC = 6*3600` when JWT has no `exp`, `_LOCK_STALE_SEC = 120`, mint backoff `_BACKOFF = (20, 45, 90, 180)` seconds on rate-limit.
  - Reuse path prints `🔑 Groww token: reusing cached token (valid ~N min)`; expiry read by base64-decoding the JWT payload's `exp`.
  - If lock held by a sibling: poll `_read_cache()` 30× at 2 s intervals, print `🔑 Groww token: picked up token minted by another bot`.
  - Mint: `totp = pyotp.TOTP(secret).now()` then `GrowwAPI.get_access_token(api_key=api_key, totp=totp)` → `_write_cache`. Non-RateLimit exceptions re-raise immediately.
- Global `access_token` string is threaded explicitly into every raw-REST helper.

---

## 3. Instrument loading

### 3.1 `csv_to_json(csv_file_path, json_file_path=None)`
- Default JSON path = `os.path.splitext(csv_path)[0] + ".json"` → `instrument.json`.
- If JSON exists and `getmtime(json) >= getmtime(csv)`: print `⚡ Using existing JSON (up-to-date): '{path}'` and `json.load` it (≈95 MB file).
- Else: `csv.DictReader` → list of dicts → `json.dump(..., indent=4, ensure_ascii=False)`; print `✅ Converted '{csv}' → '{json}'`.
- Source CSV downloadable from `https://growwapi-assets.groww.in/instruments/instrument.csv`.

### 3.2 CSV/JSON row schema (exact columns, all values are **strings**)
`exchange, exchange_token, trading_symbol, groww_symbol, name, instrument_type, segment, series, isin, underlying_symbol, underlying_exchange_token, expiry_date, strike_price, lot_size, tick_size, freeze_quantity, is_reserved, buy_allowed, sell_allowed, internal_trading_symbol, is_intraday`

Example row: `NSE,66825,360ONE26AUG1080PE,NSE-360ONE-25Aug26-1080-PE,,PE,FNO,,,360ONE,13061,2026-08-25,1080,500,0.05,20001,0,1,1,360ONE26AUG1080PE,0`

Symbol-format semantics:
- `trading_symbol` — exchange compact symbol, e.g. `NIFTY25N0425950CE`, `SENSEX2621283500CE`. **Used for LTP** (`{exchange}_{trading_symbol}`).
- `internal_trading_symbol` — Groww's internal symbol; **preferred for order placement** (`internal_trading_symbol or trading_symbol`) and for Excel logging.
- `groww_symbol` — dashed form `NSE-360ONE-25Aug26-1080-PE`; **used for historical candles / technicals / ATR**.
- `expiry_date` — `YYYY-MM-DD`. `instrument_type` ∈ `CE|PE|FUT`. `segment` = `FNO`/`CASH`.
- Lot size resolution everywhere: `int(item.get("lot_size") or item.get("lotsize") or 1)`; directional mode uses `int(selected_option.get("lot_size", 25))`; quick-mode partial math re-reads `int(instrument.get("lot_size", 1))`. `quantity = lots * lot_size`.

### 3.3 `load_instruments_from_json(json_path=None, force_reload=False)`
- Reads `CONFIG["index"].upper()`, `CONFIG["expiry"].strip()`, `CONFIG["spot"]`.
- Strike step: `100` if `"BANK" in INDEX`, `100` if `"SENSEX" in INDEX`, `50` if `"FINNIFTY" in INDEX`, else `50`.
- `nearest_strike = round(spot/step)*step`. **Cache-key range uses ±10 strikes** (`spot_range = (nearest-10*step, nearest+10*step)`) but the **actual filter uses ±20 strikes** (recomputed lower/upper bounds) — an intentional-looking inconsistency to preserve.
- Cache hit condition: `not force_reload and _instruments_cache["data"] is not None and index==, expiry==, spot_range==` → print `⚡ Using cached {INDEX} instruments ({n} loaded)`.
- Miss: print `💾 Loading instruments from file...`, load via `csv_to_json` (since `convert_csv_to_json=="yes"`), print `🎯 Filtering {INDEX} {EXPIRY} instruments between {lower}–{upper} (Spot={spot})`.
- Filter per item: `underlying_symbol.upper() == INDEX` **AND** `lower_bound <= float(strike_price or 0) <= upper_bound`. **Expiry filter is commented out** — all expiries for the index in the strike band are retained.
- Print `✅ Loaded {len(filtered)} filtered instruments (out of {len(instruments)})`; store into `_instruments_cache` = `{"data","index","expiry","spot_range"}`; return list.
- `_instruments_cache` initial value: `{"data":None,"index":None,"expiry":None,"spot_range":None}`.

### 3.4 `symbol_index`
Dict built once after load: for each item, `symbol_index[(internal_trading_symbol or trading_symbol).upper()] = item` and `symbol_index[groww_symbol.upper()] = item`. **Never read afterwards** (dead index).

### 3.5 Spot price — `get_index_spot_price(index_name, access_token=None, json_path=None)`
- Uses `_instruments_cache["data"]` if its `index` matches or is empty, else loads full instrument file into global `instruments1`.
- `index_mappings = {"NIFTY":["NIFTY","NSE-NIFTY","NIFTY 50"], "SENSEX":["SENSEX","BSE-SENSEX","SENSEX","BSE_SENSEX"], "BANKNIFTY":["BANKNIFTY","NIFTY BANK","NSE-BANKNIFTY"], "FINNIFTY":["FINNIFTY","NIFTY FIN SERVICE","NSE-FINNIFTY"]}`; fallback `[index_name]`.
- **SENSEX special path**: pick first instrument with `underlying_symbol=="SENSEX" and segment=="FNO"`, take its `expiry_date`, GET the BSE option chain, return `payload.underlying_ltp` (print `📊 Live {index} Spot (from option chain): {ltp}`).
- Generic path: find first instrument whose `trading_symbol.upper()` ∈ search_terms, or `groww_symbol.upper()` ∈ `["NSE-"+t]`/`["BSE-"+t]`, or `name.upper()` ∈ search_terms → `get_ltp_for_instrument(..., segment="CASH", verbose=False)`. Returns `0` on any failure.
- `get_nifty_spot_price(access_token=None, json_path=None)` = thin wrapper for `"NIFTY"`.

### 3.6 Command parsing
- `parse_cp_command(cmd)`: regex `^\s*(\d+)\s+([A-Z0-9]+)\s*$` → `{"lots": int, "trading_symbol_str": UPPER}`. Anything else → `None` (caller prints `❌ Invalid command format. Expected: <lots> <TRADING_SYMBOL>`).
- `parse_trading_symbol_string(s)`: regex `([A-Z]+)(\d{1,2}[A-Z]+\d{2,4})(\d+)(CE|PE)` → `{"underlying","expiry_token","expiry_date","strike","opt_type"}`. Examples accepted: `NIFTY30DEC2525950CE`, `NIFTY02MARCH202625300PE`, `SENSEX12MAR202674600CE`.
- `cmd_expiry_to_date(token)`: regex `(\d{1,2})([A-Z]+)(\d{2,4})`; day zero-padded; 2-digit year → `"20"+yy`; month via `MONTHS`; returns **`"YYYY-MM-DD"`** (docstring incorrectly says DD/MM/YYYY).
- `find_instrument_by_details(underlying, expiry_date, strike, opt_type, instruments)`: linear scan matching `underlying_symbol.upper()==`, `expiry_date==`, `str(strike_price)==strike` (string compare!), `instrument_type.upper()==opt_type`. Prints `🔍 Searching for: …`, `📦 Searching in N loaded instruments...`, `✅ Found: {trading_symbol} ({groww_symbol})`, or diagnostics + `❌ Instrument not found: …`.

---

## 4. The four modes

Shared primitives first.

### 4.0.1 `get_ltp_for_instrument(instrument, access_token, verbose=True, segment="FNO", delay=0.05, max_retries=2)`
- `exchange_symbol = f"{instrument.get('exchange','NSE').upper()}_{instrument['trading_symbol']}"`.
- `GET https://api.groww.in/v1/live-data/ltp?segment={segment}&exchange_symbols={exchange_symbol}`, headers `Accept: application/json`, `Authorization: Bearer {token}`, `X-API-VERSION: 1.0`; `session.get(..., timeout=5)`.
- Order of guards: `_live_data_limiter.acquire()` → `with ltp_lock:` → GET → `time.sleep(delay)` if `delay>0` (inside the lock).
- HTTP 429 → print `⚠️ HTTP 429 error fetching LTP: {text}`, `time.sleep(3)`, return `None`. Other non-200 → print + `None`.
- Parse `resp.json()["payload"][exchange_symbol]` → `float`. Missing → print `⚠️ No LTP found for … in payload: {payload}`, `None`.
- If `verbose`: print **and send_telegram** `💰 LTP for {exchange_symbol}: ₹{ltp} ====== [{YYYY-mm-dd HH:MM:SS}]`.
- `max_retries` param is accepted but never used.

### 4.0.2 Order placement
- `place_market_order_groww(instrument, quantity, transaction_type="BUY", product="MIS")`
  - `trading_symbol = internal_trading_symbol or trading_symbol`.
  - PAPER: fetch LTP (`verbose=False, delay=0`) or `0.0`; `fake_id = f"PAPER_{counter:04d}"` (counter starts at 0, pre-incremented); store `_paper_orders[fake_id] = {"price": float(ltp), "qty": quantity, "symbol":…, "type":…}`; print `📋 [PAPER] MARKET {side} {qty} × {sym} @ ₹{ltp:.2f} | ID: {fake_id}`; send_telegram; **return `{"payload": {"groww_order_id": fake_id}}`**.
  - LIVE: `groww.place_order(trading_symbol=…, quantity=…, validity=groww.VALIDITY_DAY, exchange=groww.EXCHANGE_BSE if exchange=="BSE" else groww.EXCHANGE_NSE, segment=groww.SEGMENT_FNO, product=getattr(groww,f"PRODUCT_{product}", groww.PRODUCT_MIS), order_type=groww.ORDER_TYPE_MARKET, transaction_type=getattr(groww,f"TRANSACTION_TYPE_{transaction_type}"), price=0)`.
- `place_limit_order_groww(instrument, quantity, price, transaction_type="SELL", product="MIS")` — identical but `ORDER_TYPE_LIMIT`, `price=price`; PAPER stores the limit price and prints `📋 [PAPER] LIMIT …` (no telegram).
- Order-id extraction pattern used everywhere: `resp.get("payload",{}).get("groww_order_id") or resp.get("groww_order_id")`.
- `cancel_order_groww(order_id, access_token)`: PAPER pops from `_paper_orders`, prints `📋 [PAPER] Order {id} cancelled`, returns True. LIVE `POST https://api.groww.in/v1/order/cancel` with `requests.post(..., timeout=8)`, headers incl. `Content-Type: application/json`, body `{"segment":"FNO","groww_order_id": order_id}`; prints `🔄 Cancel order response: {data}`; returns `True` if `data.get("success")` or `payload.order_status == "CANCELLED"`.
- `round_to_nearest_5_paise(price) = round(round(price*20)/20, 2)`.

### 4.0.3 `wait_for_order_status(order_id, access_token, order_type="BUY")`
- PAPER: print `📋 [PAPER] {type} order {id} → EXECUTED (simulated)`, return `"EXECUTED"`.
- Prints `🔎 Waiting for {type} order ({id}) to finish...`; **infinite loop** (no timeout): `get_order_status()` → print `🕒 {type} status: {status}`.
  - Success set: `["EXECUTED","COMPLETED","DELIVERY_AWAITED"]` → print+telegram `✅ {type} order executed successfully.` return status.
  - Failure set: `["FAILED","REJECTED","CANCELLED"]` → print+telegram `❌ {type} order failed …` return status.
  - Otherwise `time.sleep(0.2 if order_type=="BUY" else 1.0)`.
- `get_order_status(order_id, access_token)`: `GET https://api.groww.in/v1/order/status/{order_id}?segment=FNO` via bare `requests.get(timeout=8)` + `raise_for_status()`; prints `🔍 Order status response: {data}`; returns `payload.order_status` (or `None` on JSONDecodeError/exception).

### 4.0.4 `get_order_executed_price(order_id, access_token, segment="FNO")` → `(avg_price, total_qty)`
- PAPER: return `(_paper_orders[id]["price"], ["qty"])`, print `📋 [PAPER] Executed: ₹{p:.2f} × {q} (order {id})`.
- `GET https://api.groww.in/v1/order/trades/{order_id}?segment={segment}&page=0&page_size=50` (bare `requests.get`, `timeout=5`).
- **Retry schedule `_backoff = (0, 0.25, 0.5, 1.0, 1.25)`** — 5 attempts; `time.sleep(delay)` *before* attempts 2-5 (first fires immediately). Retry on exception, on `data["status"] != "SUCCESS"`, or on empty `payload.trade_list`.
- Success: `total_qty = Σ t["quantity"]`; `avg_price = round(Σ(t["price"]*t["quantity"]) / total_qty, 2)`; prints `✅ {transaction_type} {trading_symbol} | Total Qty={q} | Avg Price=₹{p}`.
- **Fallback** after all 5 attempts: `GET /v1/order/status/{id}?segment={segment}` → `payload.average_price or payload.avg_price`, `payload.filled_quantity or payload.quantity`; if both truthy, return `(round(float(p),2), int(q))` and print `✅ Fallback via order-status: …`. Else print `⚠️ Order-status fallback had no average_price: {payload}` and **return `(None, None)`**.

### 4.0.5 ATR helpers
- `_resolve_trail_step(atr_value)`: if `TRAIL_SL_ATR_BASED` and `atr_value>0` → `round(atr*TRAIL_SL_ATR_MULTIPLIER,2)` and print `📐 ATR trail step: ₹{step:.2f}  (ATR={atr:.2f} × {mult})`; else return `CONFIG["TRAIL_STEP"]`.
- `_fetch_atr_sync(instrument, timeout=3)`: runs `get_technicals(instrument["groww_symbol"], groww, segment="FNO", instrument=instrument)` in a daemon thread, `t.join(timeout)`, returns `techs["atr"]` from a `queue.Queue` or `None`.

### 4.0.6 Excel logging + TRADE_RECORD — `log_trade_to_excel(symbol, buy_price, sell_price, quantity, profit)`
- `mode = "PAPER" if PAPER_TRADING else ("MOCK" if MOCK_LTP_RUN else "LIVE")`.
- First emits one stdout line, exactly:
  `[TRADE_RECORD] {"ts": "...", "symbol": "...", "buy_px": ..., "sell_px": ..., "qty": ..., "pnl": ..., "mode": "..."}` via `json.dumps` of
  ```json
  {"ts":"%Y-%m-%dT%H:%M:%S","symbol":"<str>","buy_px":<round2|null>,"sell_px":<round2|null>,"qty":<int|null>,"pnl":<round2|null>,"mode":"PAPER|MOCK|LIVE"}
  ```
- Workbook `"Lakshmi.xlsx"` (relative to CWD), active sheet titled `"Lakshmi"`. Created on first use with header row:
  `["DateTime","Symbol","Buy Price","Sell Price","Quantity","Profit ₹","Capital Used","Result","Mode"]`
- Row values, columns 1..9: `now("%Y-%m-%d %H:%M:%S")`, `symbol`, `buy_price`, `sell_price`, `quantity`, `round(profit,2)`, `capital_used = round(buy_price*quantity,2)` (None if either falsy), `result_label = "PROFIT" if profit>=0 else "LOSS"`, `mode`.
- Ghost-row guard: `next_row = ws.max_row+1`; `while next_row>2 and ws.cell(next_row-1,1).value is None: next_row -= 1`.
- Prints `📊 Excel logged: {symbol}  {result}  ₹{profit:.0f}  row {next_row}` or `⚠️  Excel log failed: {exc}`.

### 4.0.7 `display_account_summary(access_token)`
Prints a 60-char `=` banner + `📊 ACCOUNT SUMMARY AFTER TRADE`; iterates `get_user_positions()["positions"]`, summing `realised_pnl`, printing per-symbol `🟢 PROFIT`/`🔴 LOSS` lines; then total; then `get_user_margins()` → `fno_margin_details.option_buy_balance_available` (`💵 Option Buy Balance Available: ₹…`) and `clear_cash` (`💸 Clear Cash: ₹…`). Sends a multi-line WhatsApp summary (wrapped in bare `except`, so an unbound `option_buy_balance` when margins fail is silently swallowed).

---

### 4.1 QUICK MODE — `place_quick_order(command, atr_based=True, quick_pts=1.5, atr_source="candle", partial=False, partial_pct=50, ltp_hint=0)`

Algorithm, in order:

1. `parse_cp_command` → lots + symbol string; `parse_trading_symbol_string` → details. Bail on failure.
2. **Index hot-swap**: if `underlying != CONFIG["index"].upper()` → print `🔄 Detected index change: A → B` / `📦 Reloading instruments for B...`, set `CONFIG["index"]=B`, `CONFIG["expiry"]=parsed expiry_date`, `CONFIG["spot"]=get_index_spot_price(B)`, `instruments_data = load_instruments_from_json()`, print `✅ Switched to B | Spot: …`.
3. `find_instrument_by_details`; `lot_size`; `quantity = lots*lot_size`.
4. `_atr_src_label = ('HIST ATR' if atr_source=='candle' else 'TICK RNG') if atr_based else 'OFF'`.
5. **No pre-order LTP fetch.** `_ref_ltp = round(float(ltp_hint),2) if ltp_hint>0 else None`. Print either `⚡ QUICK MODE: Ref LTP=₹X (from dashboard) | +{quick_pts}pt target  ATR-SL={label}` or `⚡ QUICK MODE: Placing MARKET order immediately | …`.
6. **MARKET BUY** immediately → `order_id`. Print `✅ Buy Order placed: {resp} {timestamp-set}`. On exception: print+telegram `❌ Buy order failed: {e}` and return.
7. Entry price resolution:
   - `VALIDATE_ORDERS == True`: require `order_id` (else `❌ No BUY order ID received.` return) → `wait_for_order_status(...,"BUY")`; non-success → print+telegram `⚠️ BUY failed: {status}` return. → `get_order_executed_price`. If it yields nothing: **never abandon** — `_est = _ref_ltp or get_ltp_for_instrument(verbose=True, delay=0)`; if still nothing print+telegram `🚨 BUY {id} EXECUTED but no price available — POSITION OPEN & UNMANAGED. Exit manually!` and return; else `avg_price = round(_est,2)`, `executed_qty = quantity`, warn `⚠️ Avg price unavailable … using LTP estimate ₹X; managing position.` Then `quantity = executed_qty`.
   - `VALIDATE_ORDERS == False`: `avg_price = _ref_ltp` (print `⚡ No-validate: using dashboard ref LTP …`) else one `get_ltp_for_instrument(delay=0)` rounded, else `0`.
   - **Target**: `target_price = round_to_nearest_5_paise(avg_price + quick_pts)`; print `🎯 BUY EXECUTED @ ₹{avg} | Target: ₹{tgt} (+{quick_pts}pt)`.
8. Telegram `⚡ QUICK BUY PLACED: fill≈₹{avg} | Target: ₹{tgt} | {internal_trading_symbol} | qty={quantity}`.
9. **SL computation** (`atr_val=0.0`, `hard_sl_pts=CONFIG["HARD_SL_POINTS"]` default 6.0, `mult=CONFIG["HARD_SL_ATR_MULTIPLIER"]` 1.5):
   - `atr_based and atr_source=="scan"` → **TICK RNG**: print `📡 TICK RNG: sampling LTP for ~8 seconds…`; background thread takes **8 LTP samples with `time.sleep(1)` between** (`delay=0, verbose=False`), pushes list to queue; main waits `queue.get(timeout=10)`. If ≥2 ticks: `atr_val = round(max-min, 2)`, print `✅ Tick range sampled: … (hi= lo= n=)`. Then `raw = round(atr_val*mult,2)`, `sl_pts = max(3.0, raw)` (**hard 3.0-pt floor**), `sl_price = round(avg_price - sl_pts, 2)`, print `🛡️ TICK RNG Hard SL: ₹X (range=… × 1.5=…, floor=3.0)`. If `atr_val==0`: `sl_price = avg - hard_sl_pts`, print `🛡️ Tick range unavailable → Fixed Hard SL: …`.
   - `atr_based and atr_source=="candle"` → **HIST ATR**: `get_technicals(groww_symbol, groww, segment="FNO", instrument=…, interval="5minute", lookback_minutes=150)` in a thread with `join(timeout=5)`; if `techs["atr"]` → `atr_val`, print `✅ Hist ATR fetched (5-min): {atr:.2f}`. Then `raw_sl = mult*atr_val`, `sl_pts = max(hard_sl_pts, raw_sl)` (**floor = HARD_SL_POINTS**), `sl_price = round(avg - sl_pts,2)`, print `🛡️ HIST ATR Hard SL: ₹X (1.5 × ATR … = …, floor=6.0)`. Unavailable → fixed.
   - `atr_based False` → `sl_price = round(avg - hard_sl_pts, 2)`, print `🛡️ Fixed Hard SL: ₹X (6.0pts, ATR-SL OFF)`.
10. **LIMIT SELL** at `target_price` for full `quantity` → `sell_order_id`; print `✅ LIMIT SELL placed @ ₹X: {resp}`; telegram `🎯 LIMIT SELL @ ₹X | Order ID: …`. If `VALIDATE_ORDERS`: one `get_order_status` check; if in `FAILED/REJECTED/CANCELLED` → print+telegram and **return** (leaves position open). On exception → print+telegram `❌ Limit SELL order failed` and return.
11. `trail_gap = _resolve_trail_step(atr_val)`; print `📐 Partial reversal step: ₹X  (ATR×1.0 | fixed CONFIG[TRAIL_STEP])`. Note: quick mode is a **hard target** — no trailing beyond target; `trail_gap` is used only for the partial-reversal rule.
12. Partial state: `remaining_qty=quantity`, `partial_booked=False`, `partial_sub_peak=0.0`, `partial_trigger_pts = round(quick_pts*0.60, 2)`, `partial_trigger_lvl = round(avg_price + partial_trigger_pts, 2)`, `partial_qty_raw = max(lot_size, round(quantity*(partial_pct/100)/lot_size)*lot_size)`, `partial_qty = partial_qty_raw if partial_qty_raw < quantity else 0` (0 disables). If `partial`: print `📊 PARTIAL mode ON | trigger≥₹X (+Ypt) | sell P% = Q qty | drop = Zpt`.
13. Print `⏳ Monitoring price... Target: ₹T | SL: ₹S`; `start_time=time.time()`, `max_monitor_time = 3600` (hard-coded, not `MAX_TRAIL_TIME`), `limit_sell_alive=True`.
14. **Monitor loop** (`while True`, `time.sleep(1)` per iteration at the bottom; `time.sleep(1)` and `continue` if LTP is None; `except → print ⚠️ Monitoring error, sleep(2)`):
    - Timeout: `elapsed >= 3600` → print `⏰ Max monitoring time reached (1 hour)` and break (position left open!).
    - `ltp = get_ltp_for_instrument(verbose=False, delay=0)`.
    - **Runtime partial update**: if `_QUICK_RUNTIME_PARTIAL[0] is not None and not partial_booked` → consume, recompute `partial/partial_pct/partial_qty`, print `📊 [RUNTIME] Partial updated → ON/OFF P% = Q qty` (+ trigger/drop line).
    - **Partial exit** (only if `partial and not partial_booked and limit_sell_alive and partial_qty>0`): while `ltp >= partial_trigger_lvl`, track `partial_sub_peak = max(...)`; if `partial_sub_peak>0 and ltp <= partial_sub_peak - trail_gap` →
      1. If `VALIDATE_ORDERS`, check existing sell status; if already filled → `partial_booked=True; break`.
      2. `cancel_order_groww(sell_order_id)`, `limit_sell_alive=False`.
      3. `place_market_order_groww(instrument, partial_qty, "SELL")`; if validating → `wait_for_order_status` + `get_order_executed_price` to refine `_partial_sell_price` (default = current ltp).
      4. `_partial_pnl = (_partial_sell_price - avg_price) * partial_qty`; print `💰 PARTIAL PROFIT: ₹…`; telegram; `log_trade_to_excel(...)` **for the partial leg**.
      5. `remaining_qty = quantity - partial_qty`; `sl_price = max(sl_price, partial_trigger_lvl)` (raise SL floor, print `🔼 SL raised to ₹X …`); place new LIMIT SELL for `remaining_qty` at same `target_price`; `limit_sell_alive=True`; `quantity = remaining_qty`; `partial_booked=True`; print `✅ New limit sell (Q qty) @ ₹T placed`.
    - **Runtime target update**: if `limit_sell_alive and _QUICK_RUNTIME_TARGET[0] is not None` → consume pts, `_new_target = round_to_nearest_5_paise(avg_price + pts)`; if different: (if validating) check old sell status — if filled, print `⚠️ Limit sell already filled at old target …` and `continue`; else cancel, set `target_price=_new_target`, place fresh LIMIT SELL, telegram `🔄 Target updated → ₹X (+Ypt)`.
    - **SL check** `ltp <= sl_price`: print+telegram `🛑 SL HIT @ ₹X`, `play_sound_async(SOUND_SL)`; cancel the live limit sell (log success/failure); `place_market_order_groww(quantity,"SELL")`; if validating → `wait_for_order_status` → `get_order_executed_price` → `loss = (sell_price-avg_price)*sold_qty`, print `💸 LOSS: ₹…`, telegram, Excel log, `break`. Fall-through path (no validation or no exec price): `loss = (ltp-avg_price)*quantity`, print `💸 Estimated LOSS`, Excel log with `ltp`. `break`.
    - **Target check** `ltp >= target_price`: print+telegram `🎯 TARGET HIT @ ₹X`, `play_sound_async(SOUND_PROFIT)`; if validating + `sell_order_id`: `wait_for_order_status(...,"SELL")` → executed price → `profit = (sell_price-avg)*sold_qty`, print `💰 PROFIT: ₹…`, telegram, Excel; else `profit = (target_price-avg)*quantity` → `💰 Estimated PROFIT`, Excel. `break`.
15. Print `✅ Quick order complete. Ready for next command.` (Quick mode does **not** call `display_account_summary`.)

### 4.2 MANUAL MODE — `place_cp_order(command, is_auto=False)`

1. `command_start_time = now()`; print `[HH:MM:SS.mmm] ⏱️  Command entered: {command}`. `is_auto=True` just prints `Auto mode not supported in this bot, only manual mode`.
2. Parse command + symbol; index hot-swap (same as quick, step 2).
3. `find_instrument_by_details`; `lot_size`; `quantity = lots*lot_size`.
4. **LTP: option-chain cache first.** Look up `_option_chain_cache[(underlying, expiry_date)]`; if age `< 10 s`, read `payload["strikes"][str(int(float(strike)))][opt_type]["ltp"]`; if `>0` → `💰 LTP from cache: ₹X`. Else print `💰 Fetching LTP (cache miss)...` and `get_ltp_for_instrument(verbose=True, delay=0)`; `None` → `❌ Could not fetch LTP before placing order.` return.
5. `entry_price = round(float(ltp_before),2)`; print `💵 Entry price: ₹X`.
6. **MARKET BUY** immediately; print DEBUG keys, `✅ BUY Order placed: {id} (took {order_duration:.2f}s)`; telegram `entry price: {entry_price} | {internal_trading_symbol} | qty={quantity}`. Exception → print+telegram, return.
7. `atr = CONFIG["HARD_SL_POINTS"]` as default; kick off **non-blocking** `get_technicals(groww_symbol, groww, segment="FNO", instrument=…)` (defaults: `interval="1minute"`, `lookback_minutes=60`) in a daemon thread writing to `result_queue`.
8. If `VALIDATE_ORDERS`: `wait_for_order_status(...,"BUY")` (non-success → print + telegram `⚠️ BUY failed`, return); no id → `❌ No BUY order ID received. Aborting trade.` return. Then `get_order_executed_price`; if it fails, **fall back to `avg_price = entry_price`, `executed_qty = quantity`** with the warning `⚠️ Avg price unavailable for BUY {id} — using pre-order LTP ₹X; managing position.` + telegram. Prints `🎯 Executed avg price: ₹X, Qty: N` and `⏱️  Total time: T (Order: …, Validation: …)`; telegram `🎯 BUY EXECUTED @ ₹X | Qty=N`. Else (`VALIDATE_ORDERS False`) `avg_price = entry_price`, print `⚠️ Testing mode: Using entry price estimate ₹X`.
9. If the background technicals arrived (`not result_queue.empty()`) → `atr = techs["atr"]`, print `✅ ATR fetched: {atr:.2f}`.
10. Monitor params: `highest_price = avg_price`; `trail_start = TRAIL_START_PROFIT` (1); `trail_step = _resolve_trail_step(atr)`; `poll = POLL_INTERVAL` (0.50); `max_time = MAX_TRAIL_TIME` (3600); **`hard_sl = entry_price - (1.5 * atr)`** (fixed 1.5 multiplier, based on `entry_price` not `avg_price`, and `atr` defaults to `HARD_SL_POINTS`=6 → SL 9 pts below entry when ATR unavailable). Print + telegram `📈 Trailing started... Dynamic SL: {hard_sl:.2f} (based on ATR: {atr:.2f})`.
11. **Mock LTP generator** (used when `CONFIG["MOCK_LTP_RUN"]`): closure `_next_mock_ltp()` returning `round(avg_price + offset, 2)` with offsets by tick index: `t<3 → 0.0`, `t<7 → 5.0`, `t<9 → 8.0`, else `3.0`; prints `🎭 MOCK LTP tick={t}: ₹{v}` and `time.sleep(1.0)` per tick (so the trail arms at +5, ratchets to +8, then trips).
12. **Trailing loop** (`while True`, bottom `time.sleep(poll)`):
    - Heartbeat every ≥30 s: `💓 Monitoring... LTP last seen: ₹X`.
    - LTP: mock or `get_ltp_for_instrument(verbose=False, delay=0)`. On exception or `None`: `_ltp_fail_streak += 1`; `_backoff = min(30, poll * 2**min(streak,6))`; log only when `streak<=3 or streak%10==0`; sleep backoff; continue. Success resets streak to 0.
    - Exit conditions, in priority order: `ltp <= hard_sl` → `🛑 DYNAMIC SL HIT @ {ltp}`; elif `elapsed >= max_time` → `⏰ Max trail time reached — exiting`; else new-high tracking (print + telegram `🔼 New High: ₹X`) and, once `highest_price >= avg_price + trail_start`, `trail_exit = round_to_nearest_5_paise(highest_price - trail_step)`; print/telegram `📉 Trail Active | LTP=… | High=… | Exit=…` only when `trail_exit` changed; if `ltp <= trail_exit` → `🔻 Trailing HIT @ ₹{ltp}  (trail_exit=₹… high=₹…)`.
    - On exit: sound (`SOUND_SL` for SL/default, `SOUND_PROFIT` for trailing/profit) → **MARKET SELL** full `quantity` (print `🔄 Placing SELL order…` / `✅ SELL Order placed: {id} (took {s:.3f}s)`) → telegram `{sell_reason}\n✅ SELL Order: {id}` → if `VALIDATE_ORDERS` & id: `wait_for_order_status(...,"SELL")`; on success `get_order_executed_price` → `profit = (sell_price-avg_price)*sold_qty`, print `💰 SELL EXECUTED @ ₹X | P&L: ₹Y (Buy ₹A → Sell ₹B)`, telegram `💰 PROFIT: ₹Y`, sound by sign, `log_trade_to_excel(instrument["internal_trading_symbol"], avg, sell, qty, profit)`, `display_account_summary()`. If exec price missing → log with `ltp`. If status failed → print+telegram `⚠️ SELL failed: {status}` + SL sound (**no Excel row**). If `VALIDATE_ORDERS False` → `sell_price=ltp`, estimated profit, sound, Excel, account summary. Print `✅ Trade cycle completed. Ready for next trade.` and `break`.

### 4.3 DIRECTIONAL MODE — `directional_mode()`

Interactive sub-REPL. Banner prints index/expiry/lots and usage. Input format `"<premium> <c|p>"` (lowercased); `back` returns to main menu; validation errors: `⚠️ Invalid format. Use: <premium> <c/p>`, `⚠️ Invalid premium value…`, `⚠️ Invalid direction. Use 'c' for Call or 'p' for Put`.

1. `option_type = "CE" if 'c' else "PE"`; `direction_name = "CALL (Bullish)" | "PUT (Bearish)"`.
2. `spot_price = CONFIG["spot"]`, refetch via `get_index_spot_price` if ≤0.
3. Collect `matching_options` from `instruments_data` filtered on `underlying_symbol==index`, `expiry_date == CONFIG["expiry"]` (exact), `instrument_type==option_type`.
4. `step = 100 if "SENSEX" in index else 50`; `atm_strike = round(spot/step)*step`; sort by `abs(strike - atm_strike)` (ATM-outward scan).
5. Parallel LTP scan: `max_checks = 30`, `batch_size = 5`, `ThreadPoolExecutor(max_workers=5)` per batch, `get_ltp_for_instrument(delay=0)`. Track `best_diff = abs(ltp - target_premium)`. **Early exit when `diff <= 3.0`** (prints `✅ Found close match after checking N options (diff: ₹X)`). No valid prices → `❌ Could not find any options with valid prices. Try again.`
6. Report block: `symbol = internal_trading_symbol or trading_symbol`, `lot_size = int(get("lot_size",25))`, `quantity = CONFIG["lots"]*lot_size`, `total_value = ltp*quantity`, ITM/OTM/ATM classification (CE: ITM if `strike<atm`, OTM if `>`, else ATM; PE inverted), `SL: ₹{ltp - HARD_SL_POINTS}`, `Trail: Activates at ₹{ltp + TRAIL_START_PROFIT}`.
7. **Auto-executes without confirmation**: MARKET BUY `quantity`; no order id → `continue`. Telegram `✅ BUY {symbol} @ ₹{ltp:.2f} | Qty: {quantity}`.
8. If `VALIDATE_ORDERS`: `wait_for_order_status` (non-success → `continue`, position possibly open) → `get_order_executed_price`; fallback `avg_price = ltp`, `bought_qty = quantity`. Else `avg_price = ltp`.
9. `_dir_atr = _fetch_atr_sync(selected_option, timeout=3)`; **`hard_sl = round_to_nearest_5_paise(avg_price - HARD_SL_POINTS)`** (fixed pts, ATR only affects trail step here); `trail_step = _resolve_trail_step(_dir_atr)`; `poll=POLL_INTERVAL`; `max_time=MAX_TRAIL_TIME`. Telegram `📈 Trailing started | Entry: ₹… | SL: ₹…`.
10. Trailing loop identical in structure to manual (heartbeat 30 s, `time.sleep(poll)`, `New High`, `trail_exit = round_to_nearest_5_paise(high - trail_step)` armed at `high >= avg+trail_start`), exit reasons `🛑 DYNAMIC SL HIT @ {ltp}` / `⏰ Max trail time reached` / `🔻 Trailing HIT @ {ltp}`. On exit: sound, MARKET SELL `bought_qty`, telegram, and if validating+executed → `profit = (sell_price-avg_price)*sold_qty`, prints, telegram `💰 PROFIT`, `log_trade_to_excel(symbol, …)`, `display_account_summary()`. Then `break`. (Note: Excel logging happens **only** on the validated-and-priced path here; also `complete_trade_duration = (sell_executed - start_time)` mixes `datetime` with a float `time.time()` → raises `TypeError` in that print, caught by the outer `except Exception as sell_error`.)

### 4.4 AUTO MODE — `auto_mode_runner()` → see §5.

---

## 5. AUTO MODE v2 — consensus algorithm

```python
AUTO_V2_CONFIG = {
    "MIN_MASTER_CONFIDENCE":   65,      # % — MASTER_SIGNAL_BOT min to count as a vote
    "MIN_VOTES":                4,      # votes needed (max attainable ~8)
    "MASTER_SIGNAL_MAX_AGE_S": 90,      # ignore stale MASTER entries
    "SCAN_WAIT_SEC":           15,      # sleep between no-trade scan cycles
    "SCAN_TIMEOUT_MIN":        25,      # after X min of no signal → pause
    "SL_PCT_HIGH":            0.12,     # 12% SL for HIGH confidence
    "SL_PCT_MEDIUM":          0.09,     # 9% SL for MEDIUM (also used for LOW)
    "TARGET_MULTIPLIER":       2.0,     # target = SL distance × 2  (R:R ≥ 2)
    "MAX_TRADE_MIN":           75,      # hard max minutes per trade
    "SIGNAL_RECHECK_SEC":      30,      # MASTER re-check cadence during hold
    "NO_TRADE_BEFORE": (9, 30),         # (h, m) tuple compare
    "NO_TRADE_AFTER":  (15, 0),
}
```

### 5.1 Signal sources & vote weights (`_collect_signals_auto(index, expiry, lots, min_p, max_p)`)

**1) MASTER_SIGNAL_BOT** — `_read_master_signal_latest(index)`: newest-mtime file in `<script_dir>/logs/master_signal/` whose name ends `.log` and contains `"Master_Signal"`; read all non-empty lines, iterate **reversed**, `json.loads` each (skip malformed), require `rec["index"].upper() == index.upper()`, then compute `age = now - strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S")`; if `age > 90` → return `None`; else return the record. Record fields consumed: `index, ts, direction ("CE"|"PE"|"WAIT"), confidence (0-100), s1h, s15m, s5m, rr, stop, target`.
  - `direction=="CE"` and `conf >= 65` → `votes_ce += 3 if conf>=75 else 2`.
  - `direction=="PE"` and `conf >= 65` → `votes_pe += 3 if conf>=75 else 2`.
  - `direction=="WAIT"` and `conf >= 50` → **both sides −1**, log `⚠️  MASTER WAIT (conf=X% ≥ 50) → both sides −1`.
  - else → `ℹ️  MASTER WAIT at low conf (X%) — ignored`.
  - Missing file → `📡 MASTER: no recent signal (start MASTER_SIGNAL_BOT)`.

**2) Fibonacci, two timeframes** — spot from `_get_spot_via_chain(index, expiry)`; loop `[("1h","1hour",48), ("15m","15minute",26)]` → `_fetch_index_candles_auto(index, interval, hours_back)`.
  - `_fib_score_auto(spot, candles)`: needs ≥10 candles; `n = min(20, len)`; `sh=max high`, `sl=min low` over last n; `rng<1 → 0`; `pos=(spot-sl)/rng`; thresholds `>=0.786→+3`, `>=0.618→+2`, `>=0.500→+1`, `>=0.382→−1`, `>=0.236→−2`, else `−3`.
  - Votes: `sc>=2 → +2 CE`; `sc==1 → +1 CE`; `sc<=-2 → +2 PE`; `sc==-1 → +1 PE`. (`sc==0` and `sc==3`/`-3` all fold into these branches; `+3` hits the `>=2` branch.)
  - `_rsi_auto(candles, period=14)` (Wilder, from closes): `rsi >= 72 → votes_ce -= 1`; `rsi <= 28 → votes_pe -= 1`.

**3) Premium momentum** — `_find_option_quiet("CE"/"PE", index, expiry, min_p, max_p, lots)` picks the instrument whose LTP is closest to `(min_p+max_p)/2` among instruments matching underlying/type/exact-expiry, within premium range, and affordable (`lots*lot_size*ltp <= get_available_margin(access_token)*0.9`). **⚠️ `get_available_margin` is never defined anywhere in the repo → `NameError` on the first candidate; the exception is not caught inside `_collect_signals_auto` or `auto_mode_runner`, so AUTO mode crashes at this step. A reimplementation should use `get_user_margins()['fno_margin_details']['option_buy_balance_available']`.**
  - `_quick_ltp_direction(instrument)`: two LTP samples **1.5 s apart**, `chg = (ltp2-ltp1)/ltp1`; `>= 0.002 → "UP"`, `<= -0.002 → "DOWN"`, else `"FLAT"`.
  - `ce_dir=="UP" and pe_dir!="UP"` → `votes_ce += 1`, select CE instrument; symmetric for PE; else no vote and the instrument is picked from whichever side currently leads (`votes_ce >= votes_pe → CE`).
  - `_premium_momentum_dir(instrument, samples=4, delay=0.35)` exists (avg change > 0.1 with ≥60% positive ticks → UP; < −0.1 with ≤40% → DOWN) but is **not called** by `_collect_signals_auto`.

**4) Direction resolution**: `votes_ce >= 4 and votes_ce >= votes_pe → "CE"`; `votes_pe >= 4 and votes_pe > votes_ce → "PE"`; else `"WAIT"` (instrument None, ltp 0.0, lot_size 1). If the momentum-selected instrument's `instrument_type` disagrees with the resolved direction, it is swapped to the matching side.

**5) Confidence**: `winning = votes for direction`, `losing = other`, `margin = winning - max(losing, 0)`.
  - `HIGH` if `winning >= 7 or margin >= 4`; `MEDIUM` if `winning >= 5 or margin >= 2`; else `LOW`; `NONE` when WAIT.
  - `sl_pct = 0.12 if HIGH else 0.09`.
- Returns `{"direction","confidence","votes_ce","votes_pe","instrument","ltp","lot_size","detail","sl_pct","master"}` where `detail` is the newline-joined human log lines (final line `  🗳  VOTES  CE=x  PE=y  →  DIR  [CONF]`).

`_fetch_index_candles_auto(index, interval, hours_back)`: symbol candidates `NIFTY→["NSE-NIFTY 50","NSE-NIFTY"]`, `SENSEX→["BSE-SENSEX"]`, `BANKNIFTY→["NSE-BANKNIFTY","NSE-NIFTY BANK"]`, `FINNIFTY→["NSE-NIFTY FIN SERVICE","NSE-FINNIFTY"]`, default `["NSE-"+idx]`; exchange BSE for SENSEX else NSE; `segment="CASH"`; times `"%Y-%m-%d %H:%M:%S"`; requires ≥5 candles; maps to `[{"ts","open","high","low","close"}]`.

`_get_nearest_expiry_auto(index)`: min of `expiry_date` values (parsed `%Y-%m-%d`) `>= today` among instruments for that index; fallback `CONFIG["expiry"]`.

### 5.2 Runner flow

1. Read `PAPER_TRADING → mode_lbl "PAPER"|"LIVE"`, index/min_p/max_p/lots/target_pnl/poll from CONFIG. `expiry = _get_nearest_expiry_auto(index)`; if it differs, print `⚠️  CONFIG expiry X is stale — using nearest: Y` and mutate `CONFIG["expiry"]`.
2. `_write_auto_status("STARTING", mode_label, index)`; print 68-char banner with all thresholds; `start_webhook_server()` (Flask on port 5055); telegram `🤖 AUTO v2 [{mode}] started  |  {index}`.
3. `total_pnl=0.0`, `trade_count=0`, `scan_start=time.time()`. Outer `while True`:
   - **P&L gate**: `total_pnl >= target_pnl` → print/telegram, `_write_auto_status("STOPPED", …, stop_reason="target_reached")`, break.
   - **Hours gate**: `_within_trading_hours_auto()` = `(9,30) <= (hour,minute) < (15,0)`; false → print `⏳ Outside trading window. Waiting for 09:30...`, `time.sleep(60)`, continue.
   - Print `🔍 Trade #N | Scanning signals  (P&L: ₹x / ₹y)`; `signals = _collect_signals_auto(...)`; print `signals["detail"]`; `_write_auto_status("SCANNING", …)`.
   - If `direction=="WAIT"` or `confidence in ("LOW","NONE")` → `time.sleep(15)`; if `elapsed_min >= 25` → print `⚠️  No signal for 25 min — pausing 5 min`, `time.sleep(300)`, reset `scan_start`; continue.
   - Instrument/ltp missing → `⚠️  No valid {dir} instrument in range …`, sleep 15, continue.
   - `symbol = internal_trading_symbol or trading_symbol`; `quantity = lots*lot_size`; print signal-confirmed block incl. MASTER `conf/stop/target/rr`.
   - `user_confirmation_needed` → `input("\nProceed BUY {dir} {symbol}? (y/n): ")`, non-`y` → sleep 5, continue.
   - MARKET BUY → on exception print `❌ BUY failed: {e}. Retrying in 10s…`, sleep 10, continue. Telegram `🤖 AUTO v2 BUY [{mode}]: {symbol} @ ₹X  Qty:N  [{dir}|{conf}]`.
   - If `VALIDATE_ORDERS` → `wait_for_order_status`; non-success → `⚠️  BUY not executed (…). Skipping trade.` continue; `get_order_executed_price`, fallback `(ltp, quantity)`. Else `(ltp, quantity)`.
   - `_write_auto_status("IN_TRADE", …)`.
   - **SL/target**: `sl_abs = avg_price * sl_pct`; `hard_sl = round_to_nearest_5_paise(avg - sl_abs)`; `target_p = round_to_nearest_5_paise(avg + sl_abs*2.0)`; then `cfg_sl = round_to_nearest_5_paise(avg - HARD_SL_POINTS)` and **`hard_sl = max(hard_sl, cfg_sl)`** (tighter of the two wins). `trail_start_p = TRAIL_START_PROFIT`, `trail_step = CONFIG["TRAIL_STEP"]` (raw, **not** `_resolve_trail_step`), `max_trade_sec = 75*60 = 4500`.
   - **Monitor loop** (`time.sleep(poll)` = 0.5 s; heartbeat every >30 s `  💓 Monitoring {dir} @ ₹…  SL:…  Tgt:…  High:…`), exit priority:
     1. `cur_ltp <= hard_sl` → `🛑 Hard SL hit @ ₹X`
     2. `cur_ltp >= target_p` → `🎯 Target hit @ ₹X`
     3. `elapsed >= 4500` → `⏰ Max trade time (75min) @ ₹X`
     4. `not _within_trading_hours_auto()` → `🔔 Market closing — squaring off @ ₹X`
     5. every ≥30 s: re-read MASTER; if `new_dir not in (entry_dir,"WAIT") and new_conf >= 65` → `🔄 MASTER flipped to {new_dir} ({conf}%) — exiting {entry_dir}`
     6. else trailing: track `highest_price`; once `highest_price >= avg + trail_start_p`, `trail_exit = round_to_nearest_5_paise(high - trail_step)`, print on change `  📉 Trail | LTP=…  High=…  Exit=…`; `cur_ltp <= trail_exit` → `🔻 Trail stop hit @ ₹X`.
   - Exit: sound by `sell_price > avg_price`; MARKET SELL `bought_qty`; if validating+executed refine `(sell_price, bought_qty)`; `profit = (sell_price-avg_price)*bought_qty`; `total_pnl += profit`; `trade_count += 1`; print `{🟢|🔴} Trade #N [{mode}]  Entry ₹…  Exit ₹…  P&L ₹…  |  Total ₹…`; telegram; `log_trade_to_excel(symbol, avg, sell, qty, profit)`; `_write_auto_status("TRADE_CLOSED", …)`; break inner loop.
   - Print `⏳ Cooldown 15s before next signal scan…`; `time.sleep(15)`.

### 5.3 `.auto_mode_status.json` schema — `_write_auto_status(state, **kwargs)`

Whole-file overwrite (`open(path,"w")` + `json.dump`, exceptions swallowed). Always: `state` (`"STARTING"|"SCANNING"|"IN_TRADE"|"TRADE_CLOSED"|"STOPPED"`), `ts` (epoch float). Merged kwargs by state:

| state | fields written |
|---|---|
| STARTING | `mode_label`, `index` |
| SCANNING | `mode_label, index, expiry, direction, confidence, votes_ce, votes_pe, total_pnl, trade_count, last_scan_detail` |
| IN_TRADE | `mode_label, index, expiry, direction, confidence, symbol, entry_price, quantity, total_pnl, trade_count(+1)` |
| TRADE_CLOSED | `mode_label, index, expiry, direction, symbol, entry_price, exit_price, trade_pnl, total_pnl, trade_count, exit_reason` |
| STOPPED | `mode_label, index, total_pnl, trade_count, stop_reason:"target_reached"` |

Live example on disk: `{"state": "STARTING", "ts": 1781458419.778692, "mode_label": "LIVE", "index": "NIFTY"}`

---

## 6. Dashboard bridge protocol

Files (all in script dir): command file `.prod10_bridge_cmd.json`; owner lock `.prod10_bridge.lock`; transient claim `\.prod10_bridge_cmd.json.claimed.<pid>`.

**Single-instance guard** `_claim_bridge_ownership()`: `import fcntl`; `fd = open(".prod10_bridge.lock","w")`; `fcntl.flock(fd, LOCK_EX|LOCK_NB)`; write `str(os.getpid())`, flush, **keep fd open for process lifetime** (kernel releases on exit/crash). Returns `fd` or `None`. If `None`: read the file for the owner pid and print `🚫 Dashboard bridge DISABLED in this instance — another PROD10 (PID X) already owns it.` + `   Dashboard clicks go to that instance only. Close it and restart this one to take over.` + telegram `⚠️ PROD10 started with bridge DISABLED — another instance (PID X) owns the dashboard bridge.` If owned: start daemon thread `DashboardBridgeWatcher` and print `🌐 Live Dashboard bridge active — select a strike in the Dashboard and click → PROD10`.

**Watcher loop** (`_dashboard_bridge_watcher`, `time.sleep(0.01)` per iteration, whole body wrapped in bare `except Exception: pass`):
- Heartbeat every ≥60 s: `[{HH:MM:SS}] 🌐 [DASHBOARD] Bridge alive — idle, ready for commands`.
- **Atomic claim**: if command file exists, `os.rename(_BRIDGE_FILE, f"{_BRIDGE_FILE}.claimed.{os.getpid()}")`; on `OSError` → `time.sleep(0.01); continue` (another instance won). Then `json.load` the claimed file and `os.remove` it.
- Field extraction with defaults:

```json
{
  "command":         "<str>",     // "" ok for auto; "<lots> <SYMBOL>"; or "set_quick_pts" / "set_partial" / "__AUTO__"
  "mode":            "manual",    // "manual" | "quick" | "auto" | anything-else→manual
  "paper":           null,        // null = keep CONFIG["PAPER_TRADING"]; else bool override
  "mock":            false,       // → CONFIG["MOCK_LTP_RUN"] (always set, even when absent)
  "validate_orders": null,        // null = keep CONFIG["VALIDATE_ORDERS"]; else bool override
  "atr":             true,        // quick mode atr_based
  "atr_source":      "candle",    // "candle" (HIST ATR 5-min) | "scan" (TICK RNG 8s)
  "quick_pts":       1.5,         // float target points
  "partial":         false,        // bool
  "partial_pct":     50,           // int 10–90 (validated dashboard-side)
  "ltp":             0             // float chain-LTP hint → place_quick_order(ltp_hint=)
}
```

- **Runtime commands (consumed before any trade dispatch, then `continue`)**:
  - `command == "set_quick_pts"`: `_new_tgt = float(data["quick_pts"])`; if `>0` set `_QUICK_RUNTIME_TARGET[0] = _new_tgt`, print `🎯 [DASHBOARD] Runtime target update → +Xpt (will apply on next LTP tick)`.
  - `command == "set_partial"`: `_QUICK_RUNTIME_PARTIAL[0] = {"partial": bool(data.get("partial",False)), "partial_pct": int(data.get("partial_pct",50))}`, print `📊 [DASHBOARD] Runtime partial update → ON/OFF P%`.
- **Actionability**: `_actionable = _cmd or _mode == "auto"` (auto needs no command string).
- **Concurrency**: `_bridge_lock = threading.Lock()`; `acquire(blocking=False)` — failure prints `⚠️  [DASHBOARD] Ignored — bot is already executing an order.` Success prints `🌐 [DASHBOARD] Command received: {cmd or '(auto)'}  (mode={mode}[ PAPER][ MOCK-RUN][ VALIDATE| NO-VALIDATE])` and launches daemon thread named `DashBridge` running `_run(...)`:
  - saves `_orig = CONFIG["PAPER_TRADING"]`, `_orig_mock = CONFIG.get("MOCK_LTP_RUN", False)`, `_orig_val = CONFIG.get("VALIDATE_ORDERS", False)`;
  - applies overrides (paper/validate only if not None; mock always);
  - dispatch: `auto` → `auto_mode_runner()`; `quick` → `place_quick_order(cmd, atr_based=atr, quick_pts=quick_pts, atr_source=atr_source, partial=partial, partial_pct=partial_pct, ltp_hint=ltp)`; else → `place_cp_order(cmd)`;
  - on exception print `❌ [DASHBOARD] {mode} mode crashed: {exc}` + traceback;
  - `finally` restores all three CONFIG values and `_bridge_lock.release()`.

**Producer side (`LIVE_DASHBOARD.py`)** — for reference on symbol construction: `expiry_token = f"{day:02d}{%b.upper()}{year}"` (e.g. `17MAR2026`), `prod10_sym = f"{index}{expiry_token}{strike}{opt_type}"`, `command = f"{lots} {prod10_sym}"`. Endpoints: `POST /api/prod10_*` writes the full bridge dict; `/api/prod10_set_target` writes `{"command":"set_quick_pts","quick_pts":X}`; `/api/prod10_set_partial` writes `{"command":"set_partial","partial":b,"partial_pct":n}` (rejects `partial_pct` outside 10–90); `/api/prod10_auto` writes `{"command":"__AUTO__","mode":"auto","paper":b}`; `/api/start_prod10` deletes any stale bridge file then launches the bot in Terminal via osascript.

---

## 7. Rate limiting / session / throttles

- `session = requests.Session()` — all GETs (LTP, positions, margins, option chain, `_get_spot_via_chain`). `requests.get/post` used bare for order status, trades, cancel.
- `_order_session = requests.Session()` with header `{"Connection": "keep-alive"}`; `_fast_request_post(url, json=None, headers=None, timeout=None, **kwargs)` posts through it and re-raises `growwapi.groww.exceptions.GrowwAPITimeoutException` on `requests.exceptions.Timeout` (falls through to original on ImportError). Installed as `groww._request_post = _fast_request_post`; prints `⚡ Order session patched — persistent TCP connection enabled`. Rationale in comments: growwapi's bare `requests.post` costs a fresh TCP+TLS handshake (~2-3 s) per order; patched latency <1 s.
- `class _RateLimiter(rate)` — token bucket: starts full, refills `(now-last)*rate` capped at `rate`, consumes 1.0 token, else sleeps `(1.0 - tokens)/rate` **outside** the lock then decrements. `_live_data_limiter = _RateLimiter(rate=4.0)` → 4 req/s ≈ 240/min. Documented Groww budget: 10 req/s burst, 300 req/min (5/s avg); capped at 4/s to share with sibling bots.
- `ltp_lock = threading.Lock()` — serialises all LTP HTTP calls (one at a time process-wide); `delay` sleep happens inside it.
- Timeouts: LTP 5 s; positions/margins 10 s; option chain 8 s; SENSEX chain 8 s; order status 8 s; cancel 8 s; trades 5 s.
- Option-chain cache: `_option_chain_cache[(underlying, expiry)] = (payload, ts)`, TTL `CACHE_EXPIRY_SECONDS = 15`, double-checked under `_option_chain_cache_lock`; inter-call spacing enforced under `_api_call_lock` — minimum **0.2 s** between chain calls (`_last_api_call_time`).
- Prefetcher: daemon thread `OptionChainPrefetcher` refreshing `(CONFIG["index"], CONFIG["expiry"])` every **10 s**, `time.sleep(30)` after an error (prints `⚠️ Prefetcher error: {e}`).
- 429 handling in LTP: `time.sleep(3)` and return None. Manual monitor LTP failure backoff: `min(30, poll * 2**min(streak,6))`.

---

## 8. Notifications (Telegram/WhatsApp)

- `from whatsapp_gateway import send_whatsapp as send_telegram, start_webhook_server` — every `send_telegram(...)` in the bot is a **Twilio WhatsApp** send. Most call sites are wrapped in bare `try/except` (or `except: pass`) so notification failures never block trading.
- `send_whatsapp(message)`: fire-and-forget daemon thread → `POST https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json`, HTTP basic auth `(SID, AUTH_TOKEN)`, form data `{"From": TWILIO_WA_FROM, "To": WHATSAPP_TO, "Body": message}`, `timeout=10`. If SID/token empty → prints `⚠️ WhatsApp not configured — message dropped: {message[:60]}`.
- Config via env vars: `TWILIO_ACCOUNT_SID` (default `""`), `TWILIO_AUTH_TOKEN` (default `""`), `TWILIO_WA_FROM` (default `whatsapp:+14155238886` — Twilio sandbox), `WHATSAPP_TO` (default `whatsapp:+91<REDACTED>`).
- Inbound: `start_webhook_server(port=5055)` — Flask app with `/whatsapp` webhook and `/health`; commands are written to `.wa_control.json` (`{"processed": bool, ...}`) and polled via `get_pending_command()` / `mark_command_processed(ack)`. **Only auto mode calls `start_webhook_server()`**; the bot never calls `get_pending_command()`.
- Notified events: LTP verbose fetches, buy/sell placed & executed, entry price, target/SL/trail updates, new highs, PROFIT/LOSS, partial exits, account summary, order failures, bridge-disabled warning, auto-mode lifecycle.
- Sounds: `play_sound_async(filename)` → daemon thread `playsound3.playsound`; missing file prints `🔇 Sound file not found: {filename}`; errors `🔇 Sound error: {e}`.

---

## 9. ATR logic

- **Candle fetch** — `get_technicals(symbol, groww_client, interval="1minute", segment="FNO", timeout=5, instrument=None, lookback_minutes=60)`:
  - `exchange_const = groww.EXCHANGE_BSE if instrument["exchange"]=="BSE" else groww.EXCHANGE_NSE`.
  - Window: `end = now()`, `start = end - timedelta(minutes=lookback_minutes)`; both formatted `"%Y-%m-%d %H:%M:%S"`.
  - Prints `🔄 Fetching historical candles for {symbol}...`; guards with `signal.SIGALRM` alarm of `timeout` seconds (`TimeoutError("Historical candles fetch timed out")`; wrapped in try/except for Windows) and cancels via `signal.alarm(0)`.
  - `groww_client.get_historical_candles(groww_symbol=symbol, exchange=…, segment=…, start_time=…, end_time=…, candle_interval=interval)`.
  - Requires `len(candles) >= 20`, else prints `⚠️ Insufficient candles: N` and returns None. Candle tuple layout: `[timestamp, open, high, low, close, volume]`.
  - Returns `{"sma_20","ema_9","rsi","adx","vwap","ltp","atr"}` where `ltp = close[-1]`, VWAP computed only for `segment=="FNO"` (falls back to `close[-1]`).
- **ATR-14** — `calculate_atr(high, low, close, period=14)`: returns 0 if `len(high) < period`; `tr1 = high-low`, `tr2 = |high - roll(close,1)|`, `tr3 = |low - roll(close,1)|`, `tr = amax((tr1,tr2,tr3), axis=0)`; smoothed with **EMA** via `calculate_ema(tr, 14)` (`multiplier = 2/(period+1)`, seeded with `mean(tr[:14])`); `np.roll` wraps the last close into index 0 (accepted artifact).
- **Quick-mode SL formula (HIST ATR)**: candles = `5minute`, `lookback_minutes=150` (≈30 candles), thread `join(timeout=5)`; `sl_pts = max(CONFIG["HARD_SL_POINTS"]=6.0, CONFIG["HARD_SL_ATR_MULTIPLIER"]=1.5 × ATR)`; `sl_price = round(avg_price - sl_pts, 2)`.
- **Quick-mode SL formula (TICK RNG)**: 8 LTP samples 1 s apart (≤10 s queue timeout); `range = max-min`; `sl_pts = max(3.0, round(range*1.5,2))`.
- **Manual mode SL**: `hard_sl = entry_price - 1.5*atr` with `atr` from a 1-minute/60-min `get_technicals` (default `atr = HARD_SL_POINTS = 6.0` if unavailable → 9-pt SL).
- **Directional mode SL**: fixed `avg_price - HARD_SL_POINTS` (ATR used only for trail step via `_fetch_atr_sync`, 3 s timeout).
- **Auto mode SL**: `max(avg*(1 - sl_pct), avg - HARD_SL_POINTS)`; no ATR.
- **Trail step**: `_resolve_trail_step(atr)` → `ATR × TRAIL_SL_ATR_MULTIPLIER` when `TRAIL_SL_ATR_BASED` else `TRAIL_STEP` (0.75). Auto mode bypasses this and uses raw `CONFIG["TRAIL_STEP"]`.
- Other indicators available: `calculate_sma`, `calculate_ema`, `calculate_rsi(period=14, Wilder, returns 50 if insufficient / 100 if avg_loss==0)`, `calculate_adx(period=14, returns 25 if len<2*period, 1e-9 div-guards)`, `calculate_vwap` (cumsum p*v / cumsum v).

---

## 10. Groww API surface used

Base `https://api.groww.in`. Common headers: `Accept: application/json`, `Authorization: Bearer {access_token}`, `X-API-VERSION: 1.0` (+ `Content-Type: application/json` for cancel).

| # | Method | Path + params | Purpose | Response read |
|---|---|---|---|---|
| 1 | GET | `/v1/live-data/ltp?segment={FNO\|CASH}&exchange_symbols={EXCH}_{trading_symbol}` | LTP (timeout 5 s, throttled 4/s, `ltp_lock`) | `payload[exchange_symbol]` |
| 2 | GET | `/v1/positions/user` | positions (timeout 10 s) | `status=="SUCCESS"` → `payload.positions[].{trading_symbol, realised_pnl}` |
| 3 | GET | `/v1/margins/detail/user` | margins (timeout 10 s) | `payload.fno_margin_details.option_buy_balance_available`, `payload.clear_cash` |
| 4 | GET | `/v1/option-chain/exchange/NSE/underlying/{underlying}?expiry_date={YYYY-MM-DD}` | cached chain (timeout 8 s, 15 s TTL, ≥0.2 s spacing) | `payload.strikes[strike][CE\|PE].{ltp, open_interest, volume, trading_symbol, greeks{delta,theta,iv,gamma,vega,rho}}`, `payload.underlying_ltp` |
| 5 | GET | `/v1/option-chain/exchange/BSE/underlying/SENSEX?expiry_date={…}` | SENSEX spot (timeout 8 s) | `payload.underlying_ltp` |
| 6 | GET | `/v1/option-chain/exchange/{NSE\|BSE}/underlying/{index}?expiry_date={…}` | `_get_spot_via_chain` (timeout 8 s) | `payload.underlying_ltp` |
| 7 | GET | `/v1/order/status/{order_id}?segment=FNO` | order status poll (timeout 8 s) | `payload.order_status`; fallback also reads `payload.average_price\|avg_price`, `payload.filled_quantity\|quantity` |
| 8 | GET | `/v1/order/trades/{order_id}?segment=FNO&page=0&page_size=50` | fills (timeout 5 s, 5 attempts) | `payload.trade_list[].{price, quantity, trading_symbol, transaction_type}` |
| 9 | POST | `/v1/order/cancel` body `{"segment":"FNO","groww_order_id":"<id>"}` | cancel resting limit sell (timeout 8 s) | `success` or `payload.order_status=="CANCELLED"` |
| 10 | SDK | `GrowwAPI.get_access_token(api_key=…, totp=…)` | token mint (via `groww_token`) | JWT string |
| 11 | SDK | `groww.place_order(trading_symbol, quantity, validity=VALIDITY_DAY, exchange=EXCHANGE_NSE\|BSE, segment=SEGMENT_FNO, product=PRODUCT_MIS, order_type=ORDER_TYPE_MARKET\|LIMIT, transaction_type=TRANSACTION_TYPE_BUY\|SELL, price)` | orders (POST patched onto keep-alive session) | `payload.groww_order_id` |
| 12 | SDK | `groww.get_historical_candles(groww_symbol, exchange, segment, start_time, end_time, candle_interval)` intervals used: `1minute`, `5minute`, `15minute`, `1hour` | candles for ATR/RSI/Fib | `candles[[ts,o,h,l,c,v]]` |
| 13 | SDK | `groww.get_margins()` (via `getattr(groww,"get_margins", lambda: {"availablecash":130000})`) | funds check in `find_option_by_premium_parallel`; fallback cash `130000`, buffer `0.9` | `availablecash` |
| 14 | Static | `https://growwapi-assets.groww.in/instruments/instrument.csv` | instrument master download (manual) | CSV |
| 15 | POST | `https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json` | WhatsApp notify (not Groww) | ignored |

---

## 11. Main menu / REPL

Banner: `✨ Groww Multi-Index Options Trading Bot Ready`, `📊 Index: … | Expiry: …`, `💰 Lots: … | Poll: …s`, then mode banner — PAPER (`📋 PAPER TRADING MODE: All orders are SIMULATED — no real trades`), LIVE-validated (`✅ LIVE TRADING MODE: Order validation ENABLED`), or unvalidated (`⚠️  TESTING MODE: Order validation DISABLED`). Then `display_account_summary(access_token)`, `Supported: NIFTY (NSE) | SENSEX (BSE) | BANKNIFTY | FINNIFTY`, examples `20 NIFTY17MAR202623150CE`, `50 SENSEX12MAR202674600CE`.

Loop prompt: `Choose mode: (m)anual / (q)uick / (d)irectional / (a)uto / (e)xit: ` (lowercased, stripped).
- `e|exit|quit` → print `Exiting.` break.
- `a|auto` → `auto_mode_runner()`.
- `d|directional|dir` → `directional_mode()`.
- `q|quick` → prompt `⚡ QUICK MODE - Enter command (buy + instant 1.5pt target): `, print `⏱️  Command entered at: {%Y-%m-%d %H:%M:%S.mmm}`, `back`/empty → continue, else `place_quick_order(user_input)` (defaults: `atr_based=True, quick_pts=1.5, atr_source="candle", partial=False, partial_pct=50, ltp_hint=0`).
- `m|manual` → prompt `Enter command (or press Enter for status, type 'back' to menu): `, empty → `Status check not implemented for Groww PnL in this script.`, else `place_cp_order(user_input)`.

## 12. Known defects to preserve or fix deliberately

1. `get_available_margin` (auto mode, `_find_option_quiet`) is undefined → `NameError` kills auto mode. Replace with `get_user_margins()`-derived balance.
2. `directional_mode` computes `complete_trade_duration = (sell_executed - start_time)` where `start_time` was rebound to `time.time()` → `TypeError` swallowed by the enclosing `except`, skipping the `✅ Trade cycle completed.` print.
3. `load_instruments_from_json` cache key uses ±10 strikes while the filter uses ±20; expiry filter is commented out.
4. Quick mode's 1-hour monitor timeout breaks the loop leaving the resting limit sell and position untouched.
5. `wait_for_order_status` has no timeout — a stuck order blocks the trade thread (and thus the bridge lock) forever.
6. Manual-mode SL uses `entry_price` (pre-order LTP) rather than `avg_price`, and `atr` defaults to `HARD_SL_POINTS` so "1.5×ATR" silently becomes 9 pts.
7. `display_account_summary`'s telegram block references `option_buy_balance`/`clear_cash` that are unbound when the margins call fails (masked by bare `except`).
8. `api_key` and the TOTP secret are hardcoded literals and the access token is printed into the log file.
