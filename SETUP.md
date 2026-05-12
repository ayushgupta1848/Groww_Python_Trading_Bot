# Setup Guide — Fresh Machine

## 1. Clone the repo
```bash
git clone https://github.com/ayushgupta1848/Groww_Python_Trading_Bot.git
cd Groww_Python_Trading_Bot
```

## 2. Create virtual environment
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## 3. Create ai_config.json (get key from your password manager / Apple Notes)
```bash
cat > ai_config.json << 'EOF'
{
  "openai_api_key": "sk-proj-YOUR_KEY_HERE",
  "model": "gpt-4o",
  "enabled": true
}
EOF
```

## 4. Add your Groww API key
Open `PROD10FEB_ManualBOT_groww_option_trading_final_bot.py` and set your `api_key`.

## 5. Run
```bash
.venv/bin/python3 PROD10FEB_ManualBOT_groww_option_trading_final_bot.py   # trading bot
.venv/bin/python3 FIBONACCI_TREND_ANALYZER.py                              # fibo analyzer
.venv/bin/python3 PREMIUM_DIRECTION_TRACKER.py                             # premium tracker
.venv/bin/python3 ANALYZE_BOT.py                                           # post-session analysis
```

## Files NOT in GitHub (keep these safe separately)
| File | What it is | How to recover |
|------|-----------|----------------|
| `ai_config.json` | OpenAI API key | Store key in Apple Notes / password manager |
| `instrument.json` | NSE instrument list | Auto-downloaded on first bot run |
| `.venv/` | Python packages | Recreate with step 2 above |
| `logs/` | Trading logs | Back up to iCloud / external drive periodically |
