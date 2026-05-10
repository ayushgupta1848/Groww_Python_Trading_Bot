#!/bin/zsh
DIR="/Users/ayush/Documents/eclipse-workspace/Groww_Python_Trading_Bot-main"
PY="/Library/Developer/CommandLineTools/usr/bin/python3"

osascript << EOF
tell application "Terminal"
    activate
    delay 0.4

    -- Minimise the launcher window (this .command file's own window)
    set miniaturized of front window to true

    -- ── Window 1 : PREMIUM DIRECTION TRACKER  (top-left) ──────────
    do script "cd '$DIR' && clear && echo '🟢  PREMIUM DIRECTION TRACKER' && '$PY' PREMIUM_DIRECTION_TRACKER.py"
    delay 1.2
    set w1 to front window
    try
        set current settings of w1 to settings set "Pro"
    end try
    set bounds of w1 to {0, 25, 735, 490}

    -- ── Window 2 : FIBONACCI TREND ANALYZER  (top-right) ──────────
    do script "cd '$DIR' && clear && echo '🟡  FIBONACCI TREND ANALYZER' && '$PY' FIBONACCI_TREND_ANALYZER.py"
    delay 1.2
    set w2 to front window
    try
        set current settings of w2 to settings set "Pro"
    end try
    set bounds of w2 to {735, 25, 1470, 490}

    -- ── Window 3 : PROD10FEB MANUAL BOT  (bottom, full width) ─────
    do script "cd '$DIR' && clear && echo '🔵  PROD10FEB MANUAL BOT' && '$PY' PROD10FEB_ManualBOT_groww_option_trading_final_bot.py"
    delay 1.2
    set w3 to front window
    try
        set current settings of w3 to settings set "Pro"
    end try
    set bounds of w3 to {0, 490, 1470, 874}

    -- Re-apply bounds after settle (Terminal sometimes needs this)
    delay 1
    set bounds of w1 to {0, 25, 735, 490}
    set bounds of w2 to {735, 25, 1470, 490}
    set bounds of w3 to {0, 490, 1470, 874}

end tell
EOF
