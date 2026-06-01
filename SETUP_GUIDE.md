# NIFTY Trading System — Complete Setup Guide

> For anyone importing this repo and wanting to start trading from scratch.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.9+ | Use system Python — `python3 --version` |
| Groww Account | Active | With F&O segment enabled |
| Groww API Access | Enabled | Apply at groww.in/trade-api |
| Claude Code CLI | Latest | For AI features (optional) |
| macOS / Linux | Any | Windows not tested |

---

## Step 1 — Clone the Repo

```bash
git clone <your-repo-url>
cd Groww_Python_Trading_Bot-main
```

---

## Step 2 — Install Python Dependencies

```bash
# Install all required packages
pip3 install requests pyotp openpyxl pandas numpy yfinance growwapi

# Optional: for AI features
pip3 install anthropic
```

**Full requirements list:**
```
requests
pyotp
openpyxl
pandas
numpy
yfinance
growwapi
anthropic      # only if using Claude API directly
```

---

## Step 3 — Groww API Setup

### 3.1 Get Your API Key

1. Go to [groww.in/trade-api](https://groww.in/trade-api/api-keys)
2. Create a new API key
3. Note down your **API Key** and **TOTP Secret**

### 3.2 Set Your Credentials

Credentials are stored in `ai_config.json` (this file is gitignored — **never committed to git**).

Edit `ai_config.json` in the project root:

```json
{
  "groww_api_key": "eyJraWQiOi...",
  "groww_totp_secret": "YOUR32CHARBASE32SECRET",
  "anthropic_api_key": "sk-ant-...",
  "openai_api_key": "sk-...",
  "enabled": true
}
```

> `LIVE_DASHBOARD.py` reads `groww_api_key` and `groww_totp_secret` from this file automatically.
> The trading bots (MASTER_SIGNAL_BOT, etc.) each have credentials in their own config section at the top of the file — update those separately.

### 3.3 Download instruments.csv

The instruments CSV contains all tradeable F&O symbols, lot sizes, expiry dates etc.

```bash
# Download the latest instruments file from Groww
curl -o instrument.csv "https://growwapi-assets.groww.in/instruments/instrument.csv"
```

> **Important:** Re-download this file every trading day before market open — Groww updates it daily with new expiry dates and lot sizes.

---

## Step 4 — Personal Trading AI Setup (Optional)

This powers the PnL Status tab's market intelligence features.

### 4.1 Trade History Excel Files

Place your historical trade data Excel files in:
```
ayush_previous_data/
  ├── FY2023-24.xlsx
  ├── FY2024-25.xlsx
  └── FY2025-26.xlsx
```

Each file must have a sheet named `Trade Level` with columns:
- Column A: Scrip Name
- Column B: Quantity  
- Column F: Sell Date (format: `17 Apr 2023`)
- Column I: Realized P&L

### 4.2 Intraday Log (Lakshmi.xlsx)

Place `Lakshmi.xlsx` in the root directory with a sheet named `Lakshmi`:
- Column A: DateTime
- Column B: Symbol
- Column C: Buy Price
- Column D: Sell Price
- Column E: Quantity
- Column F: P&L

> If you don't have this data, the PnL tab's behavioral analysis section will show empty — everything else still works.

---

## Step 5 — Claude Code CLI (Optional, for AI features)

The AI Summary and Scalp Plan in the Live Dashboard, and AI Advisory in PnL tab, require Claude Code CLI.

```bash
# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Login with your claude.ai account
claude login
```

> **Cost:** Claude Code CLI uses your claude.ai subscription (Pro/Teams). No separate API billing. If not installed, AI features show "Claude CLI not found" but everything else works normally.

---

## Step 6 — Start the Bots

Each bot runs in its **own terminal window**. Open multiple terminals:

### Terminal 1 — Required: Master Signal Bot
```bash
cd Groww_Python_Trading_Bot-main
python3 MASTER_SIGNAL_BOT.py
```

### Terminal 2 — Required: Fibonacci Analyzer
```bash
python3 FIBONACCI_TREND_ANALYZER.py
```

### Terminal 3 — Optional: Chart Level Analyzer
```bash
python3 CHART_LEVEL_ANALYZER.py
```

### Terminal 4 — Optional: Premium Direction Tracker
```bash
python3 PREMIUM_DIRECTION_TRACKER.py
```

### Terminal 5 — Optional: Signal Monitor
```bash
python3 SIGNAL_MONITOR.py
```

> **Minimum to start:** Run at least MASTER_SIGNAL_BOT.py + FIBONACCI_TREND_ANALYZER.py. Others add more data to the consensus signal.

---

## Step 7 — Start the Live Dashboard

In a **new terminal**:

```bash
python3 LIVE_DASHBOARD.py
```

Then open in browser:
```
http://localhost:8765
```

> **Port conflict?** If port 8765 is already in use:
> ```bash
> kill $(lsof -ti :8765) && python3 LIVE_DASHBOARD.py
> ```

---

## Step 8 — Using the Dashboard

### 📊 Live Dashboard Tab
- Shows all bot signals in real-time (refreshes every 15s)
- Top box: **Consensus** — combined bull/bear signal from all bots
- **Scalp Plan** (top): one-line AI trade suggestion
- Start bots first, then the dashboard — bots auto-detect

### 💰 PnL Status Tab
- Shows your live Groww account P&L, margin, and orders
- **Daily target alarm**: set your profit target — bell rings when hit
- Market intelligence from PERSONAL_TRADING_AI.py
- Toggle **AI Advisory** to get Claude's trade recommendation

### ⚡ Trade Board Tab
1. Select **Index** (NIFTY/SENSEX/etc.) and **Expiry**
2. Set **Lots**, **Hard SL**, **Trail Start**, **Trail Step**, **Max Time**
3. Toggle **ATR-SL** for dynamic stop loss (adapts to volatility)
4. Toggle **Paper** to test without real orders
5. Select **CE or PE** from the option chain
6. Click **BUY** — trailing SL runs automatically in the background

### 📋 Dashboard Guide Tab
- Full data source map, feature list, known issues, safety assessment

---

## Step 9 — Daily Routine

### Before Market Open (9:00–9:14 AM)
```bash
# 1. Update instruments file
curl -o instrument.csv "https://growwapi-assets.groww.in/instruments/instrument.csv"

# 2. Start bots (each in its own terminal)
python3 MASTER_SIGNAL_BOT.py
python3 FIBONACCI_TREND_ANALYZER.py
python3 CHART_LEVEL_ANALYZER.py    # optional
python3 PREMIUM_DIRECTION_TRACKER.py  # optional

# 3. Start dashboard
python3 LIVE_DASHBOARD.py

# 4. Open browser
open http://localhost:8765
```

### During Market (9:15 AM – 3:30 PM)
- Dashboard auto-refreshes — no action needed
- Watch Consensus signal at top of Live Dashboard
- Use Trade Board for placing trades
- Monitor PnL Status for account health

### After Market
- Dashboard keeps running (shows stale data when bots stop)
- Stop all bots with Ctrl+C in each terminal
- Stop dashboard with Ctrl+C

---

## Configuration Reference

### Changing Default Settings

**Bot refresh rates** — open each bot file and find `REFRESH_SEC` near the top:
```python
"REFRESH_SEC": 60,   # MASTER_SIGNAL_BOT — change to refresh faster/slower
```

**Trade Board defaults** — in `LIVE_DASHBOARD.py`:
```python
# Default lot sizes by index (updates from instruments.csv automatically)
LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20, ...}

# Rate limits (DO NOT exceed Groww limits)
_TB_CHAIN_REFRESH_MS = 500        # chain refresh when no trade active
_TB_CHAIN_REFRESH_ACTIVE_MS = 1000  # chain refresh during active trade
```

**PnL daily target** — set it in the browser UI (PnL Status tab → Daily Target ₹ input). Saved in browser localStorage.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Address already in use` on port 8765 | `kill $(lsof -ti :8765)` then restart |
| Bot shows STALE immediately | Bot crashed — check its terminal for error |
| Option chain empty | Market closed, or `instrument.csv` outdated — re-download |
| `BUY FAILED: Order Reference Id required` | Already fixed in latest code |
| `Choose quantity as multiple of lot size` | instrument.csv has wrong lot size — re-download |
| ATR fetch failed | Market closed — falls back to fixed SL (normal behavior) |
| AI features say "no CLI" | Install Claude Code: `npm i -g @anthropic-ai/claude-code` |
| yfinance error | `pip3 install yfinance --upgrade` |
| Token expired during trade | Auto-renews on next call — brief LTP gap, then recovers |

---

## File Structure

```
Groww_Python_Trading_Bot-main/
├── LIVE_DASHBOARD.py           ← Main dashboard (start this)
├── MASTER_SIGNAL_BOT.py        ← Core signal bot (required)
├── FIBONACCI_TREND_ANALYZER.py ← Fibonacci levels (required)
├── CHART_LEVEL_ANALYZER.py     ← S/R levels (optional)
├── PREMIUM_DIRECTION_TRACKER.py← Options flow (optional)
├── SIGNAL_MONITOR.py           ← Signal aggregator (optional)
├── PERSONAL_TRADING_AI.py      ← Personal trade AI (used by PnL tab)
├── instrument.csv              ← Download daily from Groww
├── Lakshmi.xlsx                ← Your intraday trade log
├── ayush_previous_data/        ← Historical trade Excel files
│   └── *.xlsx
├── logs/                       ← Auto-created by bots
│   ├── master_signal/
│   ├── fibo_analyzer/
│   ├── chart_level/
│   ├── premium_tracker/
│   ├── signal_monitor/
│   └── groww_bot/
└── SETUP_GUIDE.md              ← This file
```

---

## Safety Rules

1. **Always test with Paper mode first** — Trade Board has a Paper toggle
2. **Never restart Python during an active trade** — trailing SL will die
3. **Start with 1 lot** before using full size
4. **Square off before 3:20 PM** — no auto square-off implemented
5. **Monitor from Groww app** if the server crashes during a live trade
6. **ATR is unreliable in first 10 minutes** after market open
7. **Rate limits**: Groww allows 10 Live Data calls/sec, 300/min — dashboard stays within limits

---

## Quick Start (TL;DR)

```bash
# One-time setup
pip3 install requests pyotp openpyxl pandas numpy yfinance growwapi
curl -o instrument.csv "https://growwapi-assets.groww.in/instruments/instrument.csv"
# Edit ai_config.json: set groww_api_key and groww_totp_secret

# Every trading day
curl -o instrument.csv "https://growwapi-assets.groww.in/instruments/instrument.csv"
python3 MASTER_SIGNAL_BOT.py &
python3 FIBONACCI_TREND_ANALYZER.py &
python3 LIVE_DASHBOARD.py
open http://localhost:8765
```

---

*Dashboard version: 1.2.0 | Last updated: June 2026*
