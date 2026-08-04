# Groww Trading Bot — Complete API Reference

> Generated: 2026-06-20 | Scanned: all .py files + groww.in.har

---

## Table of Contents

1. [Authentication](#1-authentication)
2. [Groww Market Data APIs](#2-groww-market-data-apis)
3. [Groww Trading APIs](#3-groww-trading-apis)
4. [NSE Direct APIs](#4-nse-direct-apis)
5. [Groww Web Scraping Endpoints](#5-groww-web-scraping-endpoints)
6. [Telegram Bot API](#6-telegram-bot-api)
7. [Internal Dashboard APIs (LIVE_DASHBOARD.py)](#7-internal-dashboard-apis-live_dashboardpy)
8. [OpenAI API (ANALYZE_BOT.py)](#8-openai-api-analyze_botpy)
9. [Credentials Reference](#9-credentials-reference)
10. [Rate Limits and Notes](#10-rate-limits-and-notes)

---

## 1. Authentication

### How Groww API Auth Works

The bot uses a two-factor authentication flow:

1. **API Key (JWT)** — A long-lived vendor integration token (`api_key`). This is a signed JWT (ES256 algorithm, issued by `apex-auth-prod-app`) that encodes:
   - `userAccountId`: `2ee26222-7c05-4cb0-b03c-703adf5ef7dd`
   - `deviceId`: `6063193d-efd0-59eb-83c4-5d64fd77d747`
   - `vendorIntegrationKey`: `e31ff23b086b406c8874b2f6d8495313`
   - `vendorName`: `growwApi`
   - `role`: `auth-totp`

2. **TOTP** — A 6-digit time-based OTP generated from a secret seed using `pyotp.TOTP(secret).now()`

3. **Access Token** — The `growwapi` SDK exchanges (`api_key` + TOTP) for a short-lived Bearer token via:
   ```python
   access_token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
   ```
   This access token is used in `Authorization: Bearer {access_token}` on all subsequent API calls.

### Standard Headers (all Groww API calls)

```
Accept: application/json
Authorization: Bearer {access_token}
X-API-VERSION: 1.0
```

For POST requests with a JSON body, add:
```
Content-Type: application/json
```

### Standard Response Envelope

All Groww API responses follow this wrapper:
```json
{
  "status": "SUCCESS" | "FAILURE",
  "message": "...",
  "payload": { ... }
}
```

### Token Refresh

- Tokens expire (returns HTTP 401). LIVE_DASHBOARD re-zeroes its token timestamp on 401 to force re-auth on the next call, with a 5-minute token cache (`_ltp_token_ts`).
- PROD10FEB and MOMENTUM_AUTO_BOT authenticate once at startup; no automatic refresh.
- TRENDLINE_SCANNER_BOT re-fetches the token on 401 from the charting endpoint.

---

## 2. Groww Market Data APIs

### 2.1 Live LTP (Last Traded Price)

**Endpoint:** `GET https://api.groww.in/v1/live-data/ltp`

**Used in:** PROD10FEB, MOMENTUM_AUTO_BOT, MASTER_SIGNAL_BOT, FIBONACCI_TREND_ANALYZER, CHART_LEVEL_ANALYZER, PREMIUM_DIRECTION_TRACKER, LIVE_DASHBOARD, WEB_TRADING_SERVER, ENHANCED_ManualBOT, NEWPROD, SCALPING_AUTO, COMMAND_GENERATOR

**Query Parameters:**

| Param | Type | Required | Description |
|---|---|---|---|
| `segment` | string | Yes | `FNO` for options/futures, `CASH` for index spot |
| `exchange_symbols` | string | Yes | Comma-separated or repeated: `{EXCHANGE}_{TRADING_SYMBOL}` |

**Exchange symbol formats:**
- Options: `NSE_NIFTY26JUN23800CE` (NSE), `BSE_SENSEX26JUN83000CE` (BSE)
- Index spot: `NSE_NIFTY`, `NSE_BANKNIFTY`, `BSE_SENSEX`

**Up to 50 symbols per request** (LIVE_DASHBOARD batches in groups of 50).

**Headers:**
```
Accept: application/json
Authorization: Bearer {access_token}
X-API-VERSION: 1.0
```

**Response:**
```json
{
  "status": "SUCCESS",
  "payload": {
    "NSE_NIFTY26JUN23800CE": 150.35,
    "NSE_NIFTY26JUN23750CE": 203.70
  }
}
```

**Response on 429 (rate limit):** Bots sleep 3-5 seconds and retry.

**curl example:**
```bash
curl -X GET \
  "https://api.groww.in/v1/live-data/ltp?segment=FNO&exchange_symbols=NSE_NIFTY26JUN23800CE" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

**Multi-symbol curl:**
```bash
curl -X GET \
  "https://api.groww.in/v1/live-data/ltp?segment=FNO&exchange_symbols=NSE_NIFTY26JUN23800CE&exchange_symbols=NSE_NIFTY26JUN23800PE" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

**Rate limiting:** Groww allows ~300 req/min on Live Data. Bots enforce 4 req/sec (240/min) using a token-bucket `_RateLimiter`.

---

### 2.2 Option Chain

**Endpoint:** `GET https://api.groww.in/v1/option-chain/exchange/{EXCHANGE}/underlying/{INDEX}`

**Used in:** PROD10FEB, ENHANCED_ManualBOT, MASTER_SIGNAL_BOT, FIBONACCI_TREND_ANALYZER, CHART_LEVEL_ANALYZER, PREMIUM_DIRECTION_TRACKER, WEB_TRADING_SERVER, COMMAND_GENERATOR

**Path Parameters:**

| Param | Values | Description |
|---|---|---|
| `{EXCHANGE}` | `NSE` or `BSE` | NSE for NIFTY/BANKNIFTY/FINNIFTY; BSE for SENSEX/BANKEX |
| `{INDEX}` | `NIFTY`, `SENSEX`, `BANKNIFTY`, `FINNIFTY` | Underlying index (uppercase) |

**Query Parameters:**

| Param | Type | Required | Description |
|---|---|---|---|
| `expiry_date` | string | Yes | Format: `YYYY-MM-DD` (e.g. `2026-06-23`) |

**Headers:** Standard Groww headers

**Response:**
```json
{
  "status": "SUCCESS",
  "payload": {
    "underlying_ltp": 24185.50,
    "strikes": {
      "24000": {
        "CE": {
          "trading_symbol": "NIFTY26JUN24000CE",
          "ltp": 230.55,
          "close": 220.00,
          "prev_close": 220.00,
          "open_interest": 1250000,
          "volume": 85000,
          "greeks": {
            "delta": 0.62,
            "theta": -8.5,
            "iv": 11.2,
            "gamma": 0.0015,
            "vega": 12.3,
            "rho": 0.08
          }
        },
        "PE": {
          "trading_symbol": "NIFTY26JUN24000PE",
          "ltp": 45.15,
          "open_interest": 875000,
          "volume": 62000,
          "greeks": { "delta": -0.38, "theta": -6.2, "iv": 12.8 }
        }
      }
    }
  }
}
```

**Key response fields:**
- `payload.underlying_ltp` — live spot price of the index (used as a proxy for spot when NSE data is unavailable)
- `payload.strikes` — dict keyed by strike string, each with `CE` and `PE` sub-objects
- Each option: `ltp`, `close`, `open_interest`, `volume`, `greeks.iv`, `greeks.delta`, `greeks.theta`, `trading_symbol`

**curl example (NIFTY weekly expiry):**
```bash
curl -X GET \
  "https://api.groww.in/v1/option-chain/exchange/NSE/underlying/NIFTY?expiry_date=2026-06-23" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

**curl example (SENSEX BSE):**
```bash
curl -X GET \
  "https://api.groww.in/v1/option-chain/exchange/BSE/underlying/SENSEX?expiry_date=2026-06-25" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

**Caching:** PROD10FEB and ENHANCED_ManualBOT cache option chain responses for 15 seconds with a background prefetcher thread refreshing every 10 seconds.

---

### 2.3 Historical Candles

**Endpoint:** `GET https://api.groww.in/v1/historical/candles`

**Used in:** PROD10FEB, MOMENTUM_AUTO_BOT, MASTER_SIGNAL_BOT, FIBONACCI_TREND_ANALYZER, CHART_LEVEL_ANALYZER, LIVE_DASHBOARD (ATR calc), NEWPROD, SCALPING_AUTO, ENHANCED_ManualBOT

**Note:** Typically called via the `growwapi` SDK (`groww.get_historical_candles(...)`) but LIVE_DASHBOARD also calls it directly via `requests`.

**Query Parameters:**

| Param | Type | Description |
|---|---|---|
| `exchange` | string | `NSE` or `BSE` |
| `segment` | string | `FNO` for options/futures, `CASH` for indices |
| `groww_symbol` | string | Format: `NSE-{TRADING_SYMBOL}` or `NSE-NIFTY 50` (for NIFTY index) |
| `start_time` | string | Format: `YYYY-MM-DD HH:MM:SS` |
| `end_time` | string | Format: `YYYY-MM-DD HH:MM:SS` |
| `candle_interval` | string | `1minute`, `5minute`, `15minute`, `30minute`, `1hour`, `1day` |

**Headers:** Standard Groww headers

**Response:**
```json
{
  "candles": [
    [1749980400000, 23950.0, 23970.5, 23935.0, 23960.2, 12500],
    [1749980460000, 23960.2, 23985.0, 23958.0, 23975.8, 9800]
  ]
}
```

Each candle array: `[timestamp_ms, open, high, low, close, volume]`

**curl example (direct call):**
```bash
curl -X GET \
  "https://api.groww.in/v1/historical/candles?exchange=NSE&segment=FNO&groww_symbol=NSE-NIFTY26JUN23800CE&start_time=2026-06-20%2009:00:00&end_time=2026-06-20%2015:30:00&candle_interval=1minute" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

**Intervals used by bots:**
- `1minute` — technical indicators (RSI, VWAP, ATR) in PROD10FEB and MOMENTUM_AUTO_BOT
- `5minute` — MASTER_SIGNAL_BOT trend analysis, TRENDLINE_SCANNER_BOT
- `15minute` — FIBONACCI_TREND_ANALYZER (15m Fibonacci levels)
- `1hour` — FIBONACCI_TREND_ANALYZER (1h Fibonacci levels), CHART_LEVEL_ANALYZER S/R
- `1day` — CHART_LEVEL_ANALYZER daily levels, PERSONAL_TRADING_AI

**LIVE_DASHBOARD note:** Uses lookback widening — tries 90min, then 300min, then 600min if insufficient candles returned.

**IMPORTANT:** The valid interval string is `"1hour"` not `"60minute"`. Some older bot versions used the wrong string and got empty responses.

---

### 2.4 Live Quote (with prev-close)

**Endpoint:** `GET https://api.groww.in/v1/live-data/quote`

**Used in:** LIVE_DASHBOARD (index prev-close calculation, option chain quotes)

**Query Parameters:**

| Param | Type | Description |
|---|---|---|
| `exchange` | string | `NSE` or `BSE` |
| `segment` | string | `CASH` |
| `trading_symbol` | string | e.g. `NIFTY`, `BANKNIFTY`, `SENSEX` |

**Response:**
```json
{
  "payload": {
    "last_price": 24185.50,
    "ltp": 24185.50,
    "day_change": 125.30,
    "ohlc": {
      "close": 24060.20
    }
  }
}
```

**Usage:** Computing `prev_close = last_price - day_change` to show % change in dashboard indices widget. Also used in `/api/trade/chain_quotes` to fetch prev_close for option chain display.

**curl example:**
```bash
curl -X GET \
  "https://api.groww.in/v1/live-data/quote?exchange=NSE&segment=CASH&trading_symbol=NIFTY" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

---

### 2.5 Order List

**Endpoint:** `GET https://api.groww.in/v1/order/list`

**Used in:** LIVE_DASHBOARD (`read_today_orders()`)

**Query Parameters:**

| Param | Value |
|---|---|
| `segment` | `FNO` |
| `page_size` | `50` |

**Response:**
```json
{
  "payload": {
    "order_list": [
      {
        "trading_symbol": "NIFTY26JUN23800CE",
        "order_status": "EXECUTED",
        "transaction_type": "BUY",
        "quantity": 75,
        "filled_quantity": 75,
        "average_fill_price": 152.40,
        "price": 0,
        "order_type": "MARKET",
        "product": "MIS",
        "created_at": "2026-06-20T10:15:30"
      }
    ]
  }
}
```

**curl example:**
```bash
curl -X GET \
  "https://api.groww.in/v1/order/list?segment=FNO&page_size=50" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

---

### 2.6 Instruments CSV Download

**Endpoint:** `GET https://growwapi-assets.groww.in/instruments/instrument.csv`

**Used in:** MASTER_SIGNAL_BOT, FIBONACCI_TREND_ANALYZER, CHART_LEVEL_ANALYZER, PREMIUM_DIRECTION_TRACKER, COMMAND_GENERATOR, and all trading bots (read from local disk at startup)

**Authentication:** None required (public CDN asset)

**Headers:** None

**Response:** Raw CSV file (no JSON envelope). The local `instrument.csv` is 23MB+ with all active F&O instruments.

**CSV columns used:**

| Column | Example | Description |
|---|---|---|
| `trading_symbol` | `NIFTY26JUN23800CE` | Groww trading symbol |
| `internal_trading_symbol` | `NFO_NSE_NIFTY...` | Internal symbol for `place_order` |
| `underlying_symbol` | `NIFTY` | Underlying index |
| `expiry_date` | `2026-06-23` | Expiry date (YYYY-MM-DD) |
| `strike_price` | `23800` | Strike price |
| `instrument_type` | `CE` or `PE` | Option type |
| `lot_size` | `75` | Lot size |
| `exchange` | `NSE` or `BSE` | Exchange |
| `segment` | `FNO` | Segment |
| `groww_symbol` | `NSE-FNO-NIFTY...` | Symbol for historical candle API |

**Auto-refresh logic:**
- File is re-downloaded if missing or older than 1 day (24 hours)
- FIBONACCI_TREND_ANALYZER additionally caches parsed data in memory for 6 hours

**curl example:**
```bash
curl -O https://growwapi-assets.groww.in/instruments/instrument.csv
```

---

## 3. Groww Trading APIs

### 3.1 Place Order

**Method A — via growwapi SDK (all trading bots):**

SDK call: `groww.place_order(trading_symbol, quantity, validity, exchange, segment, product, order_type, transaction_type, price)`

**Method B — direct REST call (LIVE_DASHBOARD):**

**Endpoint:** `POST https://api.groww.in/v1/order/create`

**Headers:**
```
Accept: application/json
Authorization: Bearer {access_token}
X-API-VERSION: 1.0
Content-Type: application/json
```

**Request Body:**
```json
{
  "trading_symbol": "NIFTY26JUN23800CE",
  "quantity": 75,
  "validity": "DAY",
  "exchange": "NSE",
  "segment": "FNO",
  "product": "MIS",
  "order_type": "MARKET",
  "transaction_type": "BUY",
  "order_reference_id": "DB10153045123"
}
```

**SDK Parameters:**

| Parameter | Values | Description |
|---|---|---|
| `trading_symbol` | e.g. `NIFTY26JUN23800CE` | Use `internal_trading_symbol` from instrument.csv if available |
| `quantity` | e.g. `75`, `150` | Must be multiple of lot size |
| `validity` | `groww.VALIDITY_DAY` | DAY order validity |
| `exchange` | `groww.EXCHANGE_NSE` / `groww.EXCHANGE_BSE` | Exchange |
| `segment` | `groww.SEGMENT_FNO` | Always FNO for options |
| `product` | `groww.PRODUCT_MIS` / `groww.PRODUCT_NRML` | MIS = intraday |
| `order_type` | `groww.ORDER_TYPE_MARKET` / `groww.ORDER_TYPE_LIMIT` | Order type |
| `transaction_type` | `groww.TRANSACTION_TYPE_BUY` / `groww.TRANSACTION_TYPE_SELL` | Direction |
| `price` | `0` for market, float for limit | Limit price (round to 0.05 paise: `round(price * 20) / 20`) |

**Response:**
```json
{
  "status": "SUCCESS",
  "payload": {
    "groww_order_id": "GMKFO2606051444188B8PUKVWV6RO",
    "order_status": "PLACED",
    "remark": ""
  },
  "groww_order_id": "GMKFO2606051444188B8PUKVWV6RO"
}
```

The bots access `resp.get("payload", {}).get("groww_order_id") or resp.get("groww_order_id")` as a fallback chain.

**curl example (market order):**
```bash
curl -X POST \
  "https://api.groww.in/v1/order/create" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0" \
  -d '{
    "trading_symbol": "NIFTY26JUN23800CE",
    "quantity": 75,
    "validity": "DAY",
    "exchange": "NSE",
    "segment": "FNO",
    "product": "MIS",
    "order_type": "MARKET",
    "transaction_type": "BUY",
    "order_reference_id": "DB10153045123"
  }'
```

---

### 3.2 Cancel Order

**Endpoint:** `POST https://api.groww.in/v1/order/cancel`

**Used in:** PROD10FEB, ENHANCED_ManualBOT, TEST_BUGA_cancel_executed_order.py

**Headers:**
```
Content-Type: application/json
Accept: application/json
Authorization: Bearer {access_token}
X-API-VERSION: 1.0
```

**Request Body:**
```json
{
  "segment": "FNO",
  "groww_order_id": "GMKFO2606051444188B8PUKVWV6RO"
}
```

**Response (success):**
```json
{
  "success": true,
  "payload": {
    "order_status": "CANCELLED"
  }
}
```

**Note (BUG-A finding):** Groww API may return HTTP 200 SUCCESS when attempting to cancel an already-EXECUTED order. Investigated in `TEST_BUGA_cancel_executed_order.py`.

**curl example:**
```bash
curl -X POST \
  "https://api.groww.in/v1/order/cancel" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0" \
  -d '{"segment": "FNO", "groww_order_id": "GMKFO2606051444188B8PUKVWV6RO"}'
```

---

### 3.3 Order Status

**Endpoint:** `GET https://api.groww.in/v1/order/status/{order_id}`

**Used in:** PROD10FEB, MOMENTUM_AUTO_BOT, WEB_TRADING_SERVER, LIVE_DASHBOARD, ENHANCED_ManualBOT, NEWPROD, TEST_BUGA, TEST_BUGB

**Path Parameters:** `{order_id}` — Groww order ID (e.g. `GMKFO2606051444188B8PUKVWV6RO`)

**Query Parameters:**

| Param | Value |
|---|---|
| `segment` | `FNO` |

**Headers:** Standard Groww headers

**Response:**
```json
{
  "status": "SUCCESS",
  "payload": {
    "order_status": "EXECUTED",
    "groww_order_id": "GMKFO2606051444188B8PUKVWV6RO",
    "trading_symbol": "NIFTY26JUN23800CE",
    "quantity": 75,
    "average_price": 152.40,
    "avg_price": 152.40,
    "remark": ""
  }
}
```

**Order status values:**

| Status | Meaning |
|---|---|
| `EXECUTED` | Order fully filled |
| `COMPLETED` | Alias for executed |
| `DELIVERY_AWAITED` | Filled, awaiting settlement |
| `FAILED` | Order failed at exchange |
| `REJECTED` | Rejected by exchange |
| `CANCELLED` | Order cancelled |
| `PENDING` | Still in queue |
| `TIMEOUT` | (Internal bot state, not API value) |

**Polling pattern:** Fast poll every 0.2s for BUY orders, 1.0s for SELL orders.

**curl example:**
```bash
curl -X GET \
  "https://api.groww.in/v1/order/status/GMKFO2606051444188B8PUKVWV6RO?segment=FNO" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

---

### 3.4 Order Trades (Executed Price)

**Endpoint:** `GET https://api.groww.in/v1/order/trades/{order_id}`

**Used in:** PROD10FEB, MOMENTUM_AUTO_BOT, WEB_TRADING_SERVER, LIVE_DASHBOARD, ENHANCED_ManualBOT, NEWPROD

**Path Parameters:** `{order_id}` — Groww order ID

**Query Parameters:**

| Param | Type | Description |
|---|---|---|
| `segment` | string | `FNO` |
| `page` | int | `0` (always page 0) |
| `page_size` | int | `50` (max trades per page) |

**Headers:** Standard Groww headers

**Response:**
```json
{
  "status": "SUCCESS",
  "payload": {
    "trade_list": [
      {
        "quantity": 75,
        "price": 152.40,
        "trading_symbol": "NIFTY26JUN23800CE",
        "transaction_type": "BUY"
      }
    ]
  }
}
```

**Average price calculation:**
```python
total_qty   = sum(t["quantity"] for t in trades)
total_value = sum(t["price"] * t["quantity"] for t in trades)
avg_price   = round(total_value / total_qty, 2)
```

**Retry pattern (MOMENTUM_AUTO_BOT):** Retries up to 4 times with 0.5s sleep if `trade_list` is empty (execution lag).

**curl example:**
```bash
curl -X GET \
  "https://api.groww.in/v1/order/trades/GMKFO2606051444188B8PUKVWV6RO?segment=FNO&page=0&page_size=50" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

---

### 3.5 User Positions

**Endpoint:** `GET https://api.groww.in/v1/positions/user`

**Used in:** PROD10FEB, ENHANCED_ManualBOT, LIVE_DASHBOARD

**Query Parameters (optional):**

| Param | Value |
|---|---|
| `segment` | `FNO` |

**Response:**
```json
{
  "status": "SUCCESS",
  "payload": {
    "positions": [
      {
        "trading_symbol": "NIFTY26JUN23800CE",
        "exchange": "NSE",
        "realised_pnl": 1875.00,
        "unrealised_pnl": -230.50,
        "quantity": 0,
        "credit_quantity": 75,
        "debit_quantity": 75,
        "net_price": 152.40
      }
    ]
  }
}
```

**Fields used:**
- `realised_pnl` — closed P&L for the position
- `trading_symbol`, `exchange` — position identification
- `quantity` — net open quantity (0 = fully closed)
- `credit_quantity` — total bought quantity
- `debit_quantity` — total sold quantity
- `net_price` — average entry price

**curl example:**
```bash
curl -X GET \
  "https://api.groww.in/v1/positions/user" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

---

### 3.6 User Margins

**Endpoint:** `GET https://api.groww.in/v1/margins/detail/user`

**Used in:** PROD10FEB, ENHANCED_ManualBOT, LIVE_DASHBOARD (`/api/groww_capital`)

**Headers:** Standard Groww headers

**Response:**
```json
{
  "status": "SUCCESS",
  "payload": {
    "fno_margin_details": {
      "option_buy_balance_available": 48500.00,
      "option_sell_balance_available": 12000.00,
      "net_fno_margin_used": 5000.00,
      "span_margin_used": 3000.00,
      "exposure_margin_used": 2000.00,
      "total_margin": 250000.00
    },
    "clear_cash": 250000.00,
    "net_margin_used": 5000.00
  }
}
```

**Fields used:**
- `fno_margin_details.option_buy_balance_available` — available balance for buying options
- `clear_cash` — total clear cash balance

**curl example:**
```bash
curl -X GET \
  "https://api.groww.in/v1/margins/detail/user" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "X-API-VERSION: 1.0"
```

---

## 4. NSE Direct APIs

NSE requires a session cookie to be primed first. The bots visit `https://www.nseindia.com/` in a `requests.Session` before any API calls to receive the session cookie.

### Standard NSE Headers

```python
{
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}
```

**Cookie priming pattern (required for all NSE calls):**
```python
s = requests.Session()
s.headers.update(NSE_HEADERS)
s.get("https://www.nseindia.com/", timeout=5)   # prime the session cookie
# Now make API calls using s.get(url, ...)
```

On HTTP 403 response or empty body: re-visit homepage to refresh cookie, then retry.

---

### 4.1 All Indices (India VIX + NIFTY)

**Endpoint:** `GET https://www.nseindia.com/api/allIndices`

**Used in:** LIVE_DASHBOARD (VIX poll every 2 minutes), PERSONAL_TRADING_AI

**Headers:** Standard NSE headers with session cookie

**Response:**
```json
{
  "data": [
    {
      "index": "INDIA VIX",
      "last": 14.85,
      "previousClose": 14.20,
      "percentChange": 4.58
    },
    {
      "index": "NIFTY 50",
      "last": 24185.50,
      "previousClose": 24060.20,
      "open": 24090.00,
      "dayHigh": 24230.00,
      "dayLow": 24070.00
    }
  ]
}
```

**Fields used:**
- `index == "INDIA VIX"` -> `last`, `previousClose`, `percentChange`
- `index == "NIFTY 50"` -> `last`, `previousClose`, `open` (for gap % calculation)

**curl example (requires valid NSE session cookie):**
```bash
curl -X GET \
  "https://www.nseindia.com/api/allIndices" \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36" \
  -H "Accept: application/json, text/plain, */*" \
  -H "Referer: https://www.nseindia.com/" \
  -b "nsit=YOUR_COOKIE; nseappid=YOUR_COOKIE"
```

---

### 4.2 Option Chain Indices (PCR)

**Endpoint:** `GET https://www.nseindia.com/api/option-chain-indices`

**Used in:** PERSONAL_TRADING_AI (PCR calculation)

**Query Parameters:**

| Param | Value |
|---|---|
| `symbol` | `NIFTY` |

**Headers:** Standard NSE headers with session cookie

**Response:**
```json
{
  "records": {
    "data": [
      {
        "strikePrice": 24000,
        "CE": {
          "openInterest": 1250000,
          "changeinOpenInterest": -50000,
          "lastPrice": 230.55,
          "totalTradedVolume": 85000,
          "impliedVolatility": 11.2
        },
        "PE": {
          "openInterest": 875000,
          "changeinOpenInterest": 25000,
          "lastPrice": 45.15,
          "totalTradedVolume": 62000,
          "impliedVolatility": 12.8
        }
      }
    ]
  }
}
```

**PCR calculation:**
```python
total_ce_oi = sum(r["CE"]["openInterest"] for r in recs if r.get("CE"))
total_pe_oi = sum(r["PE"]["openInterest"] for r in recs if r.get("PE"))
pcr = round(total_pe_oi / total_ce_oi, 3)
```

**curl example:**
```bash
curl -X GET \
  "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY" \
  -H "User-Agent: Mozilla/5.0 ..." \
  -H "Accept: application/json" \
  -H "Referer: https://www.nseindia.com/" \
  -b "nsit=...; nseappid=..."
```

---

### 4.3 Option Chain v3 (IV, Volume, Max Pain)

**Endpoint:** `GET https://www.nseindia.com/api/option-chain-v3`

**Used in:** calculate_oi_pcr.py

**Query Parameters:**

| Param | Value | Description |
|---|---|---|
| `type` | `Indices` | Data type |
| `symbol` | `NIFTY` | Symbol |
| `expiry` | `27-Jan-2026` | Expiry date in `%-d-%b-%Y` format |

**Additional header required:** `Referer: https://www.nseindia.com/option-chain`

**Response:**
```json
{
  "records": {
    "data": [
      {
        "strikePrice": 24000,
        "CE": {
          "openInterest": 1250000,
          "impliedVolatility": 11.2,
          "lastPrice": 230.55,
          "totalTradedVolume": 85000
        },
        "PE": { ... }
      }
    ]
  }
}
```

**Data extracted:**
- ATM Implied Volatility (CE and PE)
- IV Skew (`atm_ce_iv - atm_pe_iv`)
- Volume PCR (`total_pe_vol / total_ce_vol`)
- Max Pain (strike where total option-writer loss is minimized)

**curl example:**
```bash
curl -X GET \
  "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=27-Jan-2026" \
  -H "User-Agent: Mozilla/5.0 ..." \
  -H "Accept: application/json" \
  -H "Referer: https://www.nseindia.com/option-chain" \
  -b "nsit=...; nseappid=..."
```

---

### 4.4 Heatmap Index (Indices Categories)

**Endpoint:** `GET https://www.nseindia.com/api/heatmap-index`

**Used in:** NSE_INDICES_MONITOR.py

**Query Parameters:**

| Param | Values |
|---|---|
| `type` | `Broad%20Market%20Indices`, `Sectoral%20Indices`, `Thematic%20Indices`, `Strategy%20Indices` |

**Headers (no session cookie needed):**
```
accept: */*
accept-language: en-US,en;q=0.9
user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36
referer: https://www.nseindia.com/market-data/live-market-indices/heatmap
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
```

**Response:**
```json
[
  {"symbol": "NIFTY 50", "pChange": 0.52, "last": 24185.50},
  {"symbol": "NIFTY BANK", "pChange": -0.18, "last": 52340.20}
]
```

**Field used:** `pChange` (percentage change) — averaged across category indices for sentiment score.

**curl example:**
```bash
curl -X GET \
  "https://www.nseindia.com/api/heatmap-index?type=Broad%20Market%20Indices" \
  -H "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36" \
  -H "referer: https://www.nseindia.com/market-data/live-market-indices/heatmap"
```

---

### 4.5 NextApi Option Chain Data

**Endpoint:** `GET https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi`

**Used in:** calculate_oi_pcr.py (primary OI/PCR data source)

**Query Parameters:**

| Param | Value |
|---|---|
| `functionName` | `getOptionChainData` |
| `symbol` | `NIFTY` |
| `params` | `expiryDate=27-Jan-2026` |

**Headers:** Standard NSE headers with session cookie. On 403 or empty body: re-visits NSE homepage, waits 1 second, retries once.

**Response:**
```json
{
  "underlyingValue": 24185.50,
  "data": [
    {
      "strikePrice": 24000,
      "CE": {
        "openInterest": 1250000,
        "changeinOpenInterest": -50000,
        "lastPrice": 230.55,
        "totalTradedVolume": 85000
      },
      "PE": {
        "openInterest": 875000,
        "changeinOpenInterest": 25000,
        "lastPrice": 45.15,
        "totalTradedVolume": 62000
      }
    }
  ]
}
```

**curl example:**
```bash
curl -X GET \
  "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getOptionChainData&symbol=NIFTY&params=expiryDate%3D27-Jan-2026" \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36" \
  -H "Accept: application/json, text/plain, */*" \
  -H "Referer: https://www.nseindia.com/" \
  -b "nsit=...; nseappid=..."
```

---

## 5. Groww Web Scraping Endpoints

These endpoints are from the Groww web app (not the official Groww API). Found in TRENDLINE_SCANNER_BOT.py, PATTERN_ANALYZER.py, and LIVE_PATTERN_SCANNER.py.

### Session Setup (TRENDLINE_SCANNER_BOT)

```python
_sess = requests.Session()
_sess.headers.update({
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-app-id": "growwWeb",
    "x-device-id": "8cea1d25-588a-5eff-9699-5e7fd20a6ca9",
    "x-device-id-v2": "8cea1d25-588a-5eff-9699-5e7fd20a6ca9",
    "x-platform": "web",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
})
```

---

### 5.1 Option Candles (F&O data v1)

**Endpoint:** `GET https://groww.in/v1/api/stocks_fo_data/v1/charting_service/chart/exchange/{EXCHANGE}/segment/FNO/{SYMBOL}/daily`

**Used in:** TRENDLINE_SCANNER_BOT

**Path Parameters:** `{EXCHANGE}` = `NSE` or `BSE`, `{SYMBOL}` = trading symbol

**Query Parameters:** `intervalInMinutes=5`

**Headers:** Session defaults (no Authorization header — public endpoint)

**Response:**
```json
{
  "candles": [
    [1749980400000, 150.0, 155.5, 148.0, 153.2, 5000]
  ]
}
```

**curl example:**
```bash
curl -X GET \
  "https://groww.in/v1/api/stocks_fo_data/v1/charting_service/chart/exchange/NSE/segment/FNO/NIFTY26JUN23800CE/daily?intervalInMinutes=5" \
  -H "x-app-id: growwWeb" \
  -H "x-device-id: 8cea1d25-588a-5eff-9699-5e7fd20a6ca9" \
  -H "x-platform: web" \
  -H "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
```

---

### 5.2 Option LTP (live price, web endpoint)

**Endpoint:** `GET https://groww.in/v1/api/stocks_fo_data/v1/tr_live_prices/exchange/{EXCHANGE}/segment/FNO/{SYMBOL}/latest`

**Used in:** TRENDLINE_SCANNER_BOT

**Response:**
```json
{
  "ltp": 152.35
}
```

**curl example:**
```bash
curl -X GET \
  "https://groww.in/v1/api/stocks_fo_data/v1/tr_live_prices/exchange/NSE/segment/FNO/NIFTY26JUN23800CE/latest" \
  -H "x-app-id: growwWeb" \
  -H "user-agent: Mozilla/5.0"
```

---

### 5.3 Index Spot Price (web endpoint)

**Endpoint:** `GET https://groww.in/v1/api/stocks_data/v1/tr_live_indices/exchange/{EXCHANGE}/segment/CASH/{INDEX}/latest`

**Used in:** TRENDLINE_SCANNER_BOT, PATTERN_ANALYZER, LIVE_PATTERN_SCANNER

**Response:**
```json
{
  "value": 24185.50
}
```

**curl example:**
```bash
curl -X GET \
  "https://groww.in/v1/api/stocks_data/v1/tr_live_indices/exchange/NSE/segment/CASH/NIFTY/latest" \
  -H "x-app-id: growwWeb" \
  -H "user-agent: Mozilla/5.0"
```

---

### 5.4 Index Candles v4 (requires Bearer + device-type)

**Endpoint:** `GET https://groww.in/v1/api/charting_service/v4/chart/exchange/{EXCHANGE}/segment/CASH/{INDEX}`

**Used in:** TRENDLINE_SCANNER_BOT (`fetch_index_candles()`)

**Query Parameters:**

| Param | Description |
|---|---|
| `startTimeInMillis` | Start timestamp in milliseconds |
| `endTimeInMillis` | End timestamp in milliseconds |
| `intervalInMinutes` | Candle interval (e.g. 5) |

**Headers (requires Bearer token from GrowwAPI.get_access_token):**
```
Accept: application/json, text/plain, */*
authorization: Bearer {GROWW_ACCESS_TOKEN}
x-app-id: growwWeb
x-device-id: 8cea1d25-588a-5eff-9699-5e7fd20a6ca9
x-device-type: charts
x-platform: web
User-Agent: Mozilla/5.0 ...
```

**Note:** Credentials read from `ai_config.json` fields `groww_api_key` and `groww_totp_secret`. Re-fetches token on 401.

**Response:**
```json
{
  "candles": [
    [1749980400000, 24150.0, 24230.0, 24120.0, 24185.5, 850000]
  ]
}
```

Or in some cases: `data["data"]["candles"]`

**curl example:**
```bash
curl -X GET \
  "https://groww.in/v1/api/charting_service/v4/chart/exchange/NSE/segment/CASH/NIFTY?startTimeInMillis=1749985200000&endTimeInMillis=1750008600000&intervalInMinutes=5" \
  -H "authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "x-app-id: growwWeb" \
  -H "x-device-id: 8cea1d25-588a-5eff-9699-5e7fd20a6ca9" \
  -H "x-device-type: charts" \
  -H "x-platform: web" \
  -H "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
```

---

### 5.5 Option Candles v4 (PATTERN_ANALYZER, requires web session)

**Endpoint:** `GET https://groww.in/v1/api/stocks_fo_data/v4/charting_service/chart/exchange/{EXCHANGE}/segment/FNO/{SYMBOL}`

**Used in:** PATTERN_ANALYZER.py, LIVE_PATTERN_SCANNER.py

**Query Parameters:**

| Param | Description |
|---|---|
| `startTimeInMillis` | Start timestamp (ms) |
| `endTimeInMillis` | End timestamp (ms) |
| `intervalInMinutes` | Candle interval |

**Headers (web browser session — requires user to be logged in to groww.in):**
```
Accept: application/json, text/plain, */*
authorization: Bearer {WEB_SESSION_BEARER_TOKEN}
x-app-id: growwWeb
x-device-id: 6063193d-efd0-59eb-83c4-5d64fd77d747
x-platform: web
referer: https://groww.in/charts/options/nifty/
user-agent: Mozilla/5.0 ...
Cookie: _gcl_au=...; _ga=...; AUTH_SESSION_ID=...
```

**Note:** The `authorization` Bearer token here is a **web session JWT** (obtained from browser login), not the API access_token.

**Response:**
```json
{
  "candles": [
    [1749980400000, 150.0, 155.5, 148.0, 153.2]
  ]
}
```

Each candle: `[timestamp_ms, open, high, low, close]` (no volume)

**curl example:**
```bash
curl -X GET \
  "https://groww.in/v1/api/stocks_fo_data/v4/charting_service/chart/exchange/NSE/segment/FNO/NIFTY26JUN23800CE?startTimeInMillis=1749985200000&endTimeInMillis=1750008600000&intervalInMinutes=5" \
  -H "authorization: Bearer YOUR_WEB_SESSION_JWT" \
  -H "x-app-id: growwWeb" \
  -H "x-device-id: 6063193d-efd0-59eb-83c4-5d64fd77d747" \
  -H "x-platform: web" \
  -H "user-agent: Mozilla/5.0 ..."
```

---

## 6. Telegram Bot API

**Endpoint:** `POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage`

**Used in:** PROD10FEB, MOMENTUM_AUTO_BOT, FIBONACCI_TREND_ANALYZER, CHART_LEVEL_ANALYZER, PREMIUM_DIRECTION_TRACKER, ENHANCED_ManualBOT, NEWPROD, SCALPING_AUTO

**Authentication:** Bot token embedded in URL path.

**Request Body (form-encoded `data=`):**
```
chat_id=6012308856&text=Your message here
```

**Headers:** None explicitly set (requests default: `Content-Type: application/x-www-form-urlencoded`)

**Timeout:** 3-8 seconds depending on file. All calls are fire-and-forget wrapped in try/except.

**Trigger events:**
- BUY order placed / executed
- SELL order placed / executed
- SL hit / trailing SL activated
- LTP fetch (when `verbose=True`)
- Signal alerts (STRONG CE / STRONG PE)
- Level proximity alerts (Fibonacci near level)
- Error conditions

**curl example:**
```bash
curl -X POST \
  "https://api.telegram.org/bot8666941668:AAEObDodwWqDwdVJVXy8WvFx_lyreq8p7fI/sendMessage" \
  -d "chat_id=6012308856&text=BUY+signal+detected"
```

**Bot tokens found in codebase:**

| Bot Token | Used In |
|---|---|
| `8666941668:AAEObDodwWqDwdVJVXy8WvFx_lyreq8p7fI` | PROD10FEB, MOMENTUM_AUTO_BOT, FIBONACCI, CHART_LEVEL, PREMIUM, ENHANCED |
| `8482701378:AAG7Jtfw0ZW_K9mFiX21LpsyUAV4oOcDiAQ` | NEWPROD, SCALPING_AUTO (older bots) |
| `8226223419:AAGX5fKG21CfceF_0_WjPIrOMx6ON17pZMw` | PROD (oldest version) |

**Chat ID:** `6012308856` (same across all bots)

---

## 7. Internal Dashboard APIs (LIVE_DASHBOARD.py)

The dashboard serves on `http://localhost:8765`. All internal APIs are served by a Python `http.server.BaseHTTPRequestHandler`.

### 7.1 GET Endpoints

| Endpoint | Description | Response Fields |
|---|---|---|
| `GET /api/data` | Main dashboard snapshot (all bot states, indices, OI, VIX) | Large JSON with all bot states |
| `GET /api/indices` | Live index prices (NIFTY, BANKNIFTY, SENSEX) | `{"nifty": {"last": 24185.5, "chg": 125.3, "pct": 0.52}, ...}` |
| `GET /api/trade/status` | Current PROD10 trade state + history | Trade state dict + history list |
| `GET /api/trade/chain?index=NIFTY&expiry=2026-06-23` | Option chain for index/expiry (from Groww API) | `{"strikes": [...], "spot": 24185.5}` |
| `GET /api/trade/expiries?index=NIFTY` | Available expiry dates | `{"expiries": ["2026-06-23", "2026-06-30"]}` |
| `GET /api/trade/chain_quotes?s=NSE_X,NSE_Y` | Previous close prices for option chain symbols | `{"prev_close": {"NSE_X": 150.2, ...}}` |
| `GET /api/trade/ltp_batch?s=NSE_X,NSE_Y` | Live LTP for batch of symbols | `{"ltp": {"NSE_X": 152.4, ...}, "ts": "..."}` |
| `GET /api/lot_size?index=NIFTY&expiry=2026-06-23` | Lot size from instrument.csv | `{"lot_size": 75}` |
| `GET /api/groww_capital` | Available option-buy balance | `{"ok": true, "option_buy_balance": 48500.0, "clear_cash": 250000.0}` |
| `GET /api/performance` | Historical signal accuracy stats | Signal events + outcomes |
| `GET /api/pivots?index=NIFTY` | Daily pivot levels | `{"R3": ..., "R2": ..., "R1": ..., "PP": ..., "S1": ..., "S2": ..., "S3": ...}` |
| `GET /api/personal_ai` | Personal Trading AI analysis cache | AI analysis result dict |
| `GET /api/alerts` | New alerts from all bot logs since last poll | `[{"source": "PROD10", "type": "buy", "msg": "...", "ts": "..."}]` |
| `GET /api/prod10_logs` | Recent PROD10 bot log lines | `{"lines": [...], "file": "...", "offline": false}` |
| `GET /api/momentum_bot_logs` | Recent momentum bot log lines | `{"lines": [...], "offline": false}` |
| `GET /api/oi_verdict_summary` | OI verdict tags from today's trade history | `{"ALIGNED_WIN": 3, "ALIGNED_LOSS": 1, "OPPOSED_WIN": 0, ...}` |
| `GET /api/bot/status` | Status of all registered bots | `{"oi_pcr": {...}, "fibo": {...}, ...}` |
| `GET /api/bot/registry` | List of all registered bots with metadata | `{"bots": [{id, name, script, desc}, ...]}` |
| `GET /api/bot/logs?id=fibo&n=60` | Recent log lines for a specific bot | `{"lines": [...]}` |
| `GET /api/auto_mode_status` | PROD10 auto-mode state | `{"state": "IDLE"}` or `{"state": "ACTIVE", ...}` |
| `GET /api/trendline_config` | Trendline scanner configuration | `{"premium_min": 85, "premium_max": 200, "lots": 18, "expiry_date": ""}` |
| `GET /api/trendline_signals` | Trendline scanner signals from .trendline_signals.json | `{"signals": [...], "active_trade": null, "stats": {}}` |
| `GET /api/trendline_chart` | Trendline chart data from .trendline_chart_data.json | `{"instruments": [...], "spot": 24185.5}` |
| `GET /api/trendline_expiries` | Upcoming NIFTY weekly expiry dates | `{"expiries": ["2026-06-23", "2026-06-30", ...]}` |
| `GET /api/trendline_history?from=2026-06-01&to=2026-06-20&mode=ALL` | Historical trendline trades (JSONL files) | `{"trades": [...]}` |
| `GET /api/trade_history?from=2026-06-01&to=2026-06-20` | General trade history from JSONL logs | `{"trades": [...]}` |
| `GET /api/toggle?f=ai` | Toggle AI/scalp/oi_ai features on/off | Updated `_features` dict |

### 7.2 POST Endpoints

| Endpoint | Request Body | Description |
|---|---|---|
| `POST /api/prod10_buy` | `{"index":"NIFTY","expiry":"2026-06-23","strike":23800,"opt_type":"CE","lots":1,"mode":"manual","paper":false,"atr":false,"mock":false,"quick_pts":1.5,"partial":false,"partial_pct":50}` | Place a trade via PROD10 bridge file |
| `POST /api/prod10_set_target` | `{"quick_pts": 2.0}` | Update quick target points |
| `POST /api/prod10_set_partial` | `{"partial":true,"partial_pct":50}` | Update partial-exit settings |
| `POST /api/start_prod10` | `{}` | Launch PROD10 bot in a new macOS Terminal window |
| `POST /api/prod10_auto` | `{"paper":false}` | Enable PROD10 auto mode |
| `POST /api/momentum/config` | `{"min_premium":85,"max_premium":200,"lots":1,"scan_seconds":30,...}` | Live-update momentum bot config (writes momentum_config_override.json) |
| `POST /api/personal_ai/run` | `{}` | Trigger Personal Trading AI analysis in background thread |
| `POST /api/bot/start` | `{"id":"fibo","config":{}}` | Start a registered bot subprocess |
| `POST /api/bot/stop` | `{"id":"fibo"}` | Stop a registered bot subprocess |
| `POST /api/trendline_config` | `{"premium_min":85,"premium_max":200,"lots":18,"expiry_date":"2026-06-23"}` | Save trendline scanner configuration |
| `POST /api/run_trendline_backtest` | `{"expiry":"2026-06-23","days":31,"premium_min":85,"premium_max":200,"lots":18}` | Run trendline backtest (spawns subprocess, up to 5 min) |

### WEB_TRADING_SERVER Routes (Flask on port 5000)

| Route | Description |
|---|---|
| `GET /` | Main index page with NIFTY/SENSEX selector |
| `GET /chain/{INDEX}` | Interactive option chain (NIFTY or SENSEX) with live LTPs |
| `POST /api/place_order` | Place a market order; body: `{"symbol":"NIFTY26JUN23800CE"}`; response: `{"success":true,"order_id":"...","entry_price":152.4,"quantity":75,"symbol":"..."}` |

---

## 8. OpenAI API (ANALYZE_BOT.py)

**SDK:** `openai` Python SDK (`openai.OpenAI(api_key=...).chat.completions.stream()`)

**Not raw HTTP** — goes through the official OpenAI SDK.

**Configuration from `ai_config.json`:**
```json
{
  "openai_api_key": "sk-...",
  "model": "gpt-4o",
  "enabled": true
}
```

**Parameters:**
- Model: `gpt-4o` (configurable in `ai_config.json`)
- `max_tokens`: 4096
- System message: `_SYSTEM_PROMPT` (detailed trading analyst persona)
- User message: Full markdown trade log summary (log file content)

**Auth:** `OPENAI_API_KEY` environment variable or `openai_api_key` from `ai_config.json`

---

## 9. Credentials Reference

> **WARNING:** These credentials are hardcoded in source files. Rotate them if the repository is shared publicly.

### Groww API Keys

**Active credentials (used by PROD10FEB, MOMENTUM_AUTO_BOT, WEB_TRADING_SERVER, MASTER_SIGNAL_BOT, FIBONACCI, CHART_LEVEL, PREMIUM, ENHANCED, and TEST_ files):**

- `TOTP_SECRET`: `SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4`
- `userAccountId`: `2ee26222-7c05-4cb0-b03c-703adf5ef7dd`
- `deviceId`: `6063193d-efd0-59eb-83c4-5d64fd77d747`
- `vendorIntegrationKey`: `e31ff23b086b406c8874b2f6d8495313`

The full API key JWT is stored in `WEB_TRADING_SERVER.py` line 45 and most bot files.

**Older credentials (NEWPROD, SCALPING_AUTO):**
- `TOTP_SECRET`: `JKE5A5XD75LMF7KV7MKWS3W4YAS3HCT5`

**TRENDLINE_SCANNER_BOT credentials (from `ai_config.json`):**
- Keys: `groww_api_key`, `groww_totp_secret`

### Telegram Bot Tokens

| Token | Chat ID | Used In |
|---|---|---|
| `8666941668:AAEObDodwWqDwdVJVXy8WvFx_lyreq8p7fI` | `6012308856` | PROD10FEB, MOMENTUM, FIBONACCI, CHART_LEVEL, PREMIUM, ENHANCED |
| `8482701378:AAG7Jtfw0ZW_K9mFiX21LpsyUAV4oOcDiAQ` | `6012308856` | NEWPROD, SCALPING (older bots) |
| `8226223419:AAGX5fKG21CfceF_0_WjPIrOMx6ON17pZMw` | `6012308856` | PROD (earliest version) |

---

## 10. Rate Limits and Notes

### Groww API Rate Limits

- **Live Data (`/v1/live-data/ltp`):** ~300 requests/minute enforced by Groww. Bots enforce 4 req/sec (240/min) via token-bucket `_RateLimiter`.
- **HTTP 429 handling:** Sleep 3-5 seconds, then retry.
- **HTTP 401 handling:** Token expired — re-authenticate on next call.
- **Connection reuse:** All bots use `requests.Session()` for TCP connection pooling.
- **IPv4 enforcement:** `WEB_TRADING_SERVER.py` patches `socket.getaddrinfo` to force IPv4 (Groww API rejects IPv6 connections).

### NSE Anti-Scraping Measures

- NSE requires a valid session cookie (obtained by visiting `https://www.nseindia.com/`).
- On HTTP 403 or empty response body: re-visit homepage to refresh cookie, then retry once.
- No auth headers needed — just a primed session with standard browser User-Agent and Referer.

### Data Flow Summary

```
Groww API (api.groww.in)
├── /v1/live-data/ltp          → real-time option/index prices (all bots)
├── /v1/option-chain/...       → full OC with greeks, OI, IV
├── /v1/historical/candles     → OHLCV candles for technical analysis
├── /v1/order/create           → place orders (LIVE_DASHBOARD direct)
├── /v1/order/status/{id}      → order execution status polling
├── /v1/order/trades/{id}      → exact fill price after execution
├── /v1/order/cancel           → cancel pending orders
├── /v1/order/list             → order history (LIVE_DASHBOARD)
├── /v1/positions/user         → open positions + realised P&L
├── /v1/margins/detail/user    → available capital check
└── /v1/live-data/quote        → prev-close + OHLC (LIVE_DASHBOARD)

Groww Assets CDN (growwapi-assets.groww.in)
└── /instruments/instrument.csv → complete F&O instrument list (23MB)

Groww Web App (groww.in) [scraping — not official API]
├── /v1/api/stocks_fo_data/v1/charting_service/chart/.../daily → FO candles (no auth)
├── /v1/api/stocks_fo_data/v1/tr_live_prices/.../latest        → option LTP (no auth)
├── /v1/api/stocks_data/v1/tr_live_indices/.../latest          → index spot (no auth)
├── /v1/api/charting_service/v4/chart/...                      → index candles (Bearer required)
└── /v1/api/stocks_fo_data/v4/charting_service/chart/...       → FO candles v4 (web session)

NSE India (www.nseindia.com)
├── /api/allIndices             → India VIX + all index data
├── /api/option-chain-indices   → NIFTY OC for PCR calculation
├── /api/option-chain-v3        → IV, volume, max pain data
├── /api/heatmap-index          → sector/thematic index heatmap
└── /api/NextApi/...            → legacy OC data (primary OI/PCR source)

Telegram (api.telegram.org)
└── /bot{TOKEN}/sendMessage     → trade alerts and notifications

Yahoo Finance (via yfinance SDK)
└── ^NSEI, ^INDIAVIX            → 3-year historical data (PERSONAL_TRADING_AI)

OpenAI (via openai SDK)
└── chat.completions            → AI trade log analysis (ANALYZE_BOT.py only)

Internal Dashboard (localhost:8765)
└── /api/*                      → 30+ endpoints for UI data and bot control

Internal WEB_TRADING_SERVER (localhost:5000)
└── /api/place_order            → one-click order placement
```

### Known Issues

1. **BUG-A (cancel executed order):** `POST /v1/order/cancel` may return HTTP 200 SUCCESS on an already-EXECUTED order. Investigated in `TEST_BUGA_cancel_executed_order.py`.

2. **BUG-B (CE to PE modify):** Order modification from CE to PE investigated in `TEST_BUGB_ce_to_pe_modify.py`.

3. **`get_available_margin()` undefined:** Referenced in `PROD10FEB_ManualBOT_groww_option_trading_final_bot.py` line 3137 (`_find_option_quiet()`) but not defined in that file — will raise `NameError` at runtime in the auto-mode find-option code path.

4. **`"60minute"` interval invalid:** The Groww historical candles API requires `"1hour"`, not `"60minute"`. Older bot versions used the wrong string and got empty responses.

5. **HAR file note:** The `groww.in.har` file (1.6MB) captured network traffic from the Groww web app on 2026-06-17. The first entry is a Sentry telemetry POST (`o1121657.ingest.sentry.io`). The HAR contains the complete set of web app API calls; run the following to extract all Groww-specific endpoints:
   ```bash
   python3 -c "
   import json, base64
   from urllib.parse import urlparse
   with open('groww.in.har') as f: har = json.load(f)
   seen = {}
   for e in har['log']['entries']:
       url = e['request']['url']
       if 'groww.in' not in url: continue
       key = f\"{e['request']['method']} {urlparse(url).path}\"
       if key in seen: continue
       seen[key] = True
       headers = {h['name'].lower(): h['value'] for h in e['request'].get('headers', [])}
       print(key)
       for h in ['authorization','x-app-id','x-device-id','x-platform']:
           if h in headers: print(f'  {h}: {headers[h]}')
   "
   ```
