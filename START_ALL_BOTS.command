#!/bin/zsh
DIR="/Users/ayush/Documents/eclipse-workspace/Groww_Python_Trading_Bot-main"
PY="/Library/Developer/CommandLineTools/usr/bin/python3"

# ── Background processes (no Terminal window) ──────────────────────────────
# Command Generator: generates option chain HTML files
"$PY" "$DIR/COMMAND_GENERATOR_option_chain.py" > /dev/null 2>&1 &

# OI PCR Analyzer: writes oi_snapshot.json every 60s
"$PY" "$DIR/calculate_oi_pcr.py" > /dev/null 2>&1 &

# Live Dashboard: serves HTML at http://localhost:8765
"$PY" "$DIR/LIVE_DASHBOARD.py" > /dev/null 2>&1 &

osascript << EOF
tell application "Terminal"
    activate
    delay 0.4

    -- Minimise the launcher window (this .command file's own window)
    set miniaturized of front window to true

    -- ════════════════════════════════════════════════════════
    --  WINDOW 0 : PERSONAL TRADING AI  (centred, reads first)
    -- ════════════════════════════════════════════════════════
    do script "cd '$DIR' && clear && echo '🧠  PERSONAL TRADING AI — Pre-Market Check' && echo '' && '$PY' PERSONAL_TRADING_AI.py; echo ''; echo '═══════════════════════════════════════════════════════════════'; echo '  ✅  Report complete. Review your score above before trading.'; echo '  Press Ctrl+C or close this window when done reading.'; echo '═══════════════════════════════════════════════════════════════'; cat"
    delay 1.2
    set w0 to front window
    try
        set current settings of w0 to settings set "Pro"
    end try
    set bounds of w0 to {245, 112, 1225, 762}

    -- Wait for PersonalAI to finish loading before opening trading bots
    delay 4

    -- ════════════════════════════════════════════════════════
    --  TOP ROW  (3 analysis bots)  y: 25 → 400
    -- ════════════════════════════════════════════════════════

    -- ── Window 1 : PREMIUM DIRECTION TRACKER  (top-left) ──
    do script "cd '$DIR' && clear && echo '🟢  PREMIUM DIRECTION TRACKER' && '$PY' PREMIUM_DIRECTION_TRACKER.py"
    delay 1.2
    set w1 to front window
    try
        set current settings of w1 to settings set "Pro"
    end try
    set bounds of w1 to {0, 25, 490, 400}

    -- ── Window 2 : FIBONACCI TREND ANALYZER  (top-centre) ──
    do script "cd '$DIR' && clear && echo '🟡  FIBONACCI TREND ANALYZER' && '$PY' FIBONACCI_TREND_ANALYZER.py"
    delay 1.2
    set w2 to front window
    try
        set current settings of w2 to settings set "Pro"
    end try
    set bounds of w2 to {490, 25, 980, 400}

    -- ── Window 3 : MASTER SIGNAL BOT  (top-right) ──────────
    do script "cd '$DIR' && clear && echo '🎯  MASTER SIGNAL BOT' && '$PY' MASTER_SIGNAL_BOT.py"
    delay 1.2
    set w3 to front window
    try
        set current settings of w3 to settings set "Pro"
    end try
    set bounds of w3 to {980, 25, 1470, 400}

    -- ════════════════════════════════════════════════════════
    --  BOTTOM ROW  (3 more bots)  y: 400 → 874
    -- ════════════════════════════════════════════════════════

    -- ── Window 4 : CHART LEVEL ANALYZER  (bottom-left) ─────
    do script "cd '$DIR' && clear && echo '📊  CHART LEVEL ANALYZER' && '$PY' CHART_LEVEL_ANALYZER.py"
    delay 1.2
    set w4 to front window
    try
        set current settings of w4 to settings set "Pro"
    end try
    set bounds of w4 to {0, 400, 368, 874}

    -- ── Window 5 : SIGNAL MONITOR  (bottom-2nd) ──────────
    do script "cd '$DIR' && clear && echo '🔍  SIGNAL MONITOR' && '$PY' SIGNAL_MONITOR.py"
    delay 1.2
    set w5 to front window
    try
        set current settings of w5 to settings set "Pro"
    end try
    set bounds of w5 to {368, 400, 736, 874}

    -- ── Window 6 : PROD10FEB MANUAL BOT  (bottom-right, 3rd col) ──
    do script "cd '$DIR' && clear && echo '🔵  PROD10FEB MANUAL BOT' && '$PY' PROD10FEB_ManualBOT_groww_option_trading_final_bot.py"
    delay 1.2
    set w6 to front window
    try
        set current settings of w6 to settings set "Pro"
    end try
    set bounds of w6 to {736, 400, 1104, 874}

    -- ── Window 7 : TRENDLINE SCANNER BOT  (bottom-rightmost, 4th col) ──
    do script "cd '$DIR' && clear && echo '🔭  TRENDLINE SCANNER BOT' && '$PY' TRENDLINE_SCANNER_BOT.py"
    delay 1.2
    set w7 to front window
    try
        set current settings of w7 to settings set "Pro"
    end try
    set bounds of w7 to {1104, 400, 1470, 874}

    -- Re-apply bounds after settle (Terminal sometimes shifts them)
    delay 1.5
    set bounds of w1 to {0,   25,  490, 400}
    set bounds of w2 to {490,  25,  980, 400}
    set bounds of w3 to {980,  25, 1470, 400}
    set bounds of w4 to {0,   400,  368, 874}
    set bounds of w5 to {368, 400,  736, 874}
    set bounds of w6 to {736, 400, 1104, 874}
    set bounds of w7 to {1104, 400, 1470, 874}

    -- Bring PersonalAI window back to front so user reads verdict first
    set frontmost of w0 to true
    set bounds of w0 to {245, 112, 1225, 762}

    -- Open Live Dashboard in default browser
    delay 2
    do script "open http://localhost:8765"

end tell
EOF
