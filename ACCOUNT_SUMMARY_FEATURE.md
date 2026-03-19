# Account Summary Feature - Implementation Guide

## Overview
Added automatic account summary display after every trade execution (profit booking or SL hit) in the manual trading bot.

## New Functions Added

### 1. `get_user_positions(access_token)`
**Purpose**: Fetches user's current positions from Groww API  
**API Endpoint**: `GET https://api.groww.in/v1/positions/user`  
**Returns**: Dictionary with positions data including `realised_pnl` for each position

**Response Structure**:
```json
{
  "status": "SUCCESS",
  "payload": {
    "positions": [
      {
        "trading_symbol": "NIFTY25MAR2623500CE",
        "realised_pnl": 500,
        ...
      }
    ]
  }
}
```

### 2. `get_user_margins(access_token)`
**Purpose**: Fetches user's margin details from Groww API  
**API Endpoint**: `GET https://api.groww.in/v1/margins/detail/user`  
**Returns**: Dictionary with margin data including FNO balances

**Response Structure**:
```json
{
  "status": "SUCCESS",
  "payload": {
    "clear_cash": 36372.14,
    "fno_margin_details": {
      "option_buy_balance_available": 36372.14,
      ...
    }
  }
}
```

### 3. `display_account_summary(access_token)`
**Purpose**: Fetches and displays comprehensive account summary after trade execution  
**Displays**:
- ✅ Total Realised P&L (from all positions)
- ✅ Option Buy Balance Available
- ✅ Clear Cash
- ✅ Individual position P&L

**Output Format**:
```
============================================================
📊 ACCOUNT SUMMARY AFTER TRADE
============================================================
  📈 NIFTY25MAR2623500CE: Realised P&L = ₹500

💰 Total Realised P&L: ₹500.00
💵 Option Buy Balance Available: ₹36372.14
💸 Clear Cash: ₹36372.14
============================================================
```

## Integration Points

The `display_account_summary()` function is automatically called after:

1. **✅ Successful SELL order execution** (when order status is EXECUTED/COMPLETED)
2. **✅ LTP-based profit logging** (when executed price cannot be fetched)
3. **✅ Testing mode profit logging** (when VALIDATE_ORDERS is False)

## Telegram Notifications

Account summary is also sent to Telegram with the same information:
```
📊 ACCOUNT SUMMARY
━━━━━━━━━━━━━━━━
💰 Total Realised P&L: ₹500.00
💵 Option Buy Balance: ₹36372.14
💸 Clear Cash: ₹36372.14
```

## Usage

No manual intervention needed. The bot will automatically:
1. Execute trade (BUY → monitor → SELL)
2. Log profit to Excel
3. **Fetch and display account summary**
4. Send summary to Telegram
5. Ready for next trade

## Benefits

- 📊 **Real-time P&L tracking**: See cumulative profits after each trade
- 💰 **Balance monitoring**: Know available funds for next trade
- 🔔 **Instant notifications**: Get summary on Telegram immediately
- 📝 **Audit trail**: Console logs + Telegram history for review

## Error Handling

- If API calls fail, appropriate warnings are logged
- Bot continues normal operation even if summary fetch fails
- No impact on trade execution flow

## Notes

- API calls are made using persistent session for better performance
- 10-second timeout for each API call to prevent hanging
- Thread-safe implementation with existing session object
