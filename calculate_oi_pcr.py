#!/Users/ayush/Documents/eclipse-workspace/Groww_Python_Trading_Bot-main/.venv/bin/python3
import requests
import time
from datetime import datetime
from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import ColorScaleRule, FormulaRule
from openpyxl.styles import PatternFill
import os

# Set your Anthropic API key as an environment variable before running:
#   export ANTHROPIC_API_KEY="your-key-here"


# ================= CONFIG =================
URL = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
PARAMS = {
    "functionName": "getOptionChainData",
    "symbol": "NIFTY",
    "params": "expiryDate=28-Apr-2026"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/"
}

REFRESH_SECONDS = 60
EXCEL_FILE = "oi_pcr_dashboard.xlsx"
LOT_SIZE = 75  # NIFTY lot size (change to 20 for SENSEX, 35 for BANKNIFTY, etc.)

session = requests.Session()
session.headers.update(HEADERS)

prev_price = None
prev_ce_oi = None
prev_pe_oi = None

# ================= HELPERS =================
def fetch_data():
    r = session.get(URL, params=PARAMS, timeout=10)
    r.raise_for_status()
    return r.json()

def coi_pct(ce, pe):
    total = abs(ce) + abs(pe)
    if total == 0:
        return 0, 0
    return abs(ce) * 100 / total, abs(pe) * 100 / total

def activity(price, prev_price, oi, prev_oi):
    if prev_price is None or prev_oi is None:
        return "N/A"
    if price > prev_price and oi > prev_oi:
        return "LONG BUILDUP"
    if price < prev_price and oi > prev_oi:
        return "SHORT BUILDUP"
    if price > prev_price and oi < prev_oi:
        return "SHORT COVERING"
    if price < prev_price and oi < prev_oi:
        return "LONG UNWINDING"
    return "SIDEWAYS"

def power(price, prev_price, chg):
    if prev_price is None:
        return "N/A"
    if chg > 0 and price < prev_price:
        return "WRITERS"
    if chg > 0 and price > prev_price:
        return "BUYERS"
    return "NEUTRAL"

# ================= RULE-BASED TRADING SUMMARY =================
def generate_trading_summary(m, price_change):
    ce = m["total_oi_ce"]
    pe = m["total_oi_pe"]
    ce_chg = m["total_chg_ce"]
    pe_chg = m["total_chg_pe"]

    # 1. Market bias
    if pe > ce * 1.1:
        bias_line = "Bullish bias — Put OI dominates Call OI."
        bias = "BULLISH"
    elif ce > pe * 1.1:
        bias_line = "Bearish bias — Call OI dominates Put OI."
        bias = "BEARISH"
    else:
        bias_line = "Neutral — Call and Put OI are balanced."
        bias = "NEUTRAL"

    # 2. Writing activity (change OI)
    if pe_chg > 0 and ce_chg <= 0:
        writing_line = "Strong put writing with call unwinding — support forming."
        bias = "BULLISH"
    elif ce_chg > 0 and pe_chg <= 0:
        writing_line = "Strong call writing with put unwinding — resistance forming."
        bias = "BEARISH"
    elif pe_chg > ce_chg and pe_chg > 0:
        writing_line = "Put writing observed — support building up."
    elif ce_chg > pe_chg and ce_chg > 0:
        writing_line = "Call writing observed — resistance building up."
    else:
        writing_line = "No significant writing activity detected."

    # 3. Price-OI confirmation
    if price_change > 0 and pe_chg > 0:
        price_line = "Price up with OI build-up — uptrend confirmed."
    elif price_change < 0 and ce_chg > 0:
        price_line = "Price down with OI build-up — downtrend continuation."
    elif price_change > 0 and ce_chg < 0:
        price_line = "Price up with call OI falling — short covering rally."
    elif price_change < 0 and pe_chg < 0:
        price_line = "Price down with put OI falling — long unwinding."
    else:
        price_line = "Price movement not showing strong OI confirmation."

    # 4. ATM strike diff dominance
    atm_d = m["atm_strikes_oi"].get(m["atm"], {})
    atm_diff = atm_d.get("pe_oi", 0) - atm_d.get("ce_oi", 0)
    if atm_diff > 0:
        final_bias = "Bias: Prefer CALL side — PE dominant at ATM."
    elif atm_diff < 0:
        final_bias = "Bias: Prefer PUT side — CE dominant at ATM."
    else:
        final_bias = f"Bias: Prefer {'CALL' if bias == 'BULLISH' else 'PUT' if bias == 'BEARISH' else 'WAIT'} side."

    return f"{bias_line}\n{writing_line}\n{price_line}\n{final_bias}"


# ================= CONDITIONAL FORMATTING =================
def apply_conditional_formatting(ws):
    ws.conditional_formatting._cf_rules.clear()

    ws.conditional_formatting.add(
        "M2:M10000",
        ColorScaleRule(
            start_type="num", start_value=0.7, start_color="FF6666",
            mid_type="num", mid_value=1.0, mid_color="FFFF99",
            end_type="num", end_value=1.3, end_color="66FF66"
        )
    )

    ws.conditional_formatting.add(
        "N2:N10000",
        ColorScaleRule(
            start_type="num", start_value=0.7, start_color="FF6666",
            mid_type="num", mid_value=1.0, mid_color="FFFF99",
            end_type="num", end_value=1.3, end_color="66FF66"
        )
    )

    ws.conditional_formatting.add(
        "Q2:Q10000",
        ColorScaleRule(
            start_type="num", start_value=40, start_color="66FF66",
            mid_type="num", mid_value=50, mid_color="FFFF99",
            end_type="num", end_value=65, end_color="FF6666"
        )
    )

    ws.conditional_formatting.add(
        "R2:R10000",
        ColorScaleRule(
            start_type="num", start_value=40, start_color="FF6666",
            mid_type="num", mid_value=50, mid_color="FFFF99",
            end_type="num", end_value=65, end_color="66FF66"
        )
    )

    # === SENTIMENT ===
    ws.conditional_formatting.add("Y2:Y10000",
                                  FormulaRule(formula=['$Y2="BULLISH"'],
                                              fill=PatternFill("solid", fgColor="00C853")))

    ws.conditional_formatting.add("Y2:Y10000",
                                  FormulaRule(formula=['$Y2="BEARISH"'],
                                              fill=PatternFill("solid", fgColor="D50000")))

    ws.conditional_formatting.add("Y2:Y10000",
                                  FormulaRule(formula=['$Y2="NEUTRAL"'],
                                              fill=PatternFill("solid", fgColor="FFD54F")))

    ws.conditional_formatting.add(
        "L2:L10000",
        ColorScaleRule(
            start_type="num", start_value=0.5, start_color="FFEB84",
            mid_type="num", mid_value=1.0, mid_color="9BC2E6",
            end_type="num", end_value=2.0, end_color="63BE7B"
        )
    )

    # === TOTAL OI CE vs PE (D & E) ===
    ws.conditional_formatting.add("D2:D10000",
                                  FormulaRule(formula=["$E2>$D2"],
                                              fill=PatternFill("solid", fgColor="F8696B")))

    ws.conditional_formatting.add("E2:E10000",
                                  FormulaRule(formula=["$E2>$D2"],
                                              fill=PatternFill("solid", fgColor="63BE7B")))

    ws.conditional_formatting.add("D2:D10000",
                                  FormulaRule(formula=["$D2>$E2"],
                                              fill=PatternFill("solid", fgColor="63BE7B")))

    ws.conditional_formatting.add("E2:E10000",
                                  FormulaRule(formula=["$D2>$E2"],
                                              fill=PatternFill("solid", fgColor="F8696B")))

    ws.conditional_formatting.add("D2:E10000",
                                  FormulaRule(formula=["ABS($D2-$E2)/MAX($D2,$E2)<0.05"],
                                              fill=PatternFill("solid", fgColor="FFF59D")))

    ws.conditional_formatting.add(
        "M2:O10000",
        ColorScaleRule(
            start_type="num", start_value=30, start_color="63BE7B",
            mid_type="num", mid_value=50, mid_color="FFEB84",
            end_type="num", end_value=70, end_color="F8696B"
        )
    )

    ws.conditional_formatting.add(
        "N2:P10000",
        ColorScaleRule(
            start_type="num", start_value=30, start_color="F8696B",
            mid_type="num", mid_value=50, mid_color="FFEB84",
            end_type="num", end_value=70, end_color="63BE7B"
        )
    )

    ws.conditional_formatting.add(
        "S2:T10000",
        FormulaRule(
            formula=['S2="BUYERS"'],
            fill=PatternFill("solid", fgColor="63BE7B")
        )
    )

    ws.conditional_formatting.add(
        "S2:T10000",
        FormulaRule(
            formula=['S2="WRITERS"'],
            fill=PatternFill("solid", fgColor="F8696B")
        )
    )


# ================= EXCEL INIT =================
def init_excel():
    try:
        wb = load_workbook(EXCEL_FILE)
    except FileNotFoundError:
        wb = Workbook()
        ws = wb.active
        ws.title = "DATA"

        ws.append([
            "Time","Price","ATM",
            "Total OI CE","Total OI PE",
            "Total Chg CE","Total Chg PE",
            "ATM OI CE","ATM OI PE",
            "ATM Chg CE","ATM Chg PE",
            "PCR All","PCR ATM ±3","PCR Chg ATM",
            "COI CE % All","COI PE % All",
            "COI CE % ATM","COI PE % ATM",
            "CE Activity","PE Activity",
            "CE Power","PE Power",
            "Resistance","Support","Sentiment"
        ])

        apply_conditional_formatting(ws)
        wb.save(EXCEL_FILE)

    return wb

# ================= CALCULATION =================
def calculate(j):
    price = j["underlyingValue"]
    data = j["data"]

    strikes = sorted(i["strikePrice"] for i in data)
    atm = min(strikes, key=lambda x: abs(x - price))
    idx = strikes.index(atm)
    atm_range = strikes[max(0, idx-3): idx+4]

    t_oi_ce = t_oi_pe = t_chg_ce = t_chg_pe = 0
    a_oi_ce = a_oi_pe = a_chg_ce = a_chg_pe = 0

    ce_map, pe_map = {}, {}

    for i in data:
        s = i["strikePrice"]
        ce = i.get("CE", {})
        pe = i.get("PE", {})

        ce_oi = ce.get("openInterest", 0) * LOT_SIZE
        pe_oi = pe.get("openInterest", 0) * LOT_SIZE
        ce_chg = ce.get("changeinOpenInterest", 0) * LOT_SIZE
        pe_chg = pe.get("changeinOpenInterest", 0) * LOT_SIZE

        ce_map[s] = ce_oi
        pe_map[s] = pe_oi

        t_oi_ce += ce_oi
        t_oi_pe += pe_oi
        t_chg_ce += ce_chg
        t_chg_pe += pe_chg

        if s in atm_range:
            a_oi_ce += ce_oi
            a_oi_pe += pe_oi
            a_chg_ce += ce_chg
            a_chg_pe += pe_chg

    atm_strikes_oi = {s: {"ce_oi": ce_map.get(s, 0), "pe_oi": pe_map.get(s, 0)} for s in atm_range}

    # Calculate level strength for resistance/support
    resistance_levels = sorted(ce_map, key=ce_map.get, reverse=True)[:3]
    support_levels = sorted(pe_map, key=pe_map.get, reverse=True)[:3]
    resistance_strength = [(level, ce_map[level], ce_map[level] + pe_map.get(level, 0)) for level in resistance_levels]
    support_strength = [(level, pe_map[level], pe_map[level] + ce_map.get(level, 0)) for level in support_levels]

    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "price": price,
        "atm": atm,
        "total_oi_ce": t_oi_ce,
        "total_oi_pe": t_oi_pe,
        "total_chg_ce": t_chg_ce,
        "total_chg_pe": t_chg_pe,
        "atm_oi_ce": a_oi_ce,
        "atm_oi_pe": a_oi_pe,
        "atm_chg_ce": a_chg_ce,
        "atm_chg_pe": a_chg_pe,
        "resistance": resistance_levels,
        "support": support_levels,
        "resistance_strength": resistance_strength,
        "support_strength": support_strength,
        "atm_strikes_oi": atm_strikes_oi,
    }

# ================= MAIN LOOP =================

wb = init_excel()
ws = wb["DATA"]

# Store 15-min candle closes for breakout/breakdown detection
candle_history = {}
candle_interval = 900  # 15 min in seconds
last_candle_close_time = None

atm_strike_history = {}  # {strike: [(time, ce_oi, pe_oi), ...]}

while True:
    try:
        j = fetch_data()
        m = calculate(j)

        pcr_all = m["total_oi_pe"] / m["total_oi_ce"] if m["total_oi_ce"] else 0
        pcr_atm = m["atm_oi_pe"] / m["atm_oi_ce"] if m["atm_oi_ce"] else 0
        pcr_chg = abs(m["atm_chg_pe"]) / abs(m["atm_chg_ce"]) if m["atm_chg_ce"] else 0

        coi_ce_all, coi_pe_all = coi_pct(m["total_chg_ce"], m["total_chg_pe"])
        coi_ce_atm, coi_pe_atm = coi_pct(m["atm_chg_ce"], m["atm_chg_pe"])

        ce_act = activity(m["price"], prev_price, m["atm_oi_ce"], prev_ce_oi)
        pe_act = activity(m["price"], prev_price, m["atm_oi_pe"], prev_pe_oi)

        ce_pow = power(m["price"], prev_price, m["atm_chg_ce"])
        pe_pow = power(m["price"], prev_price, m["atm_chg_pe"])

        sentiment = "BULLISH" if pcr_atm > 1.1 else "BEARISH" if pcr_atm < 0.9 else "NEUTRAL"

        # Store ATM strike history (last 5)
        atm = m["atm"]
        atm_strike_history.setdefault(atm, []).append((m["time"], m["atm_strikes_oi"][atm]["ce_oi"], m["atm_strikes_oi"][atm]["pe_oi"]))
        if len(atm_strike_history[atm]) > 5:
            atm_strike_history[atm].pop(0)

        # ================= PRINT =================
        print("\n" + "="*75)
        print(f"Time                         : {m['time']}")
        print(f"Current Market Price         : {m['price']}")
        print(f"ATM Strike                   : {m['atm']}")

        print("\n--- ALL STRIKES ---")
        print(f"1. Total OI CE                : {m['total_oi_ce']:,}")
        print(f"2. Total OI PE                : {m['total_oi_pe']:,}")
        print(f"3. Total Change OI CE         : {m['total_chg_ce']:,}")
        print(f"4. Total Change OI PE         : {m['total_chg_pe']:,}")

        print("\n--- ATM ±3 STRIKES ---")
        print(f"5. Total OI CE (ATM ±3)       : {m['atm_oi_ce']:,}")
        print(f"6. Total OI PE (ATM ±3)       : {m['atm_oi_pe']:,}")
        print(f"7. Change OI CE (ATM ±3)      : {m['atm_chg_ce']:,}")
        print(f"8. Change OI PE (ATM ±3)      : {m['atm_chg_pe']:,}")

        print("\n  Strike-wise OI (ATM ±3):")
        print(f"  {'Strike':>8} | {'CE OI':>14} | {'PE OI':>14} | {'Diff (PE-CE)':>16}")
        print(f"  {'-'*8}-+-{'-'*14}-+-{'-'*14}-+-{'-'*16}")
        for strike in sorted(m["atm_strikes_oi"]):
            marker = " <-- ATM" if strike == m["atm"] else ""
            d = m["atm_strikes_oi"][strike]
            diff = d["pe_oi"] - d["ce_oi"]
            print(f"  {strike:>8} | {d['ce_oi']:>14,} | {d['pe_oi']:>14,} | {diff:>+16,}{marker}")

        hist = atm_strike_history.get(m["atm"], [])
        if len(hist) > 1:
            print(f"\n  ATM Strike {m['atm']} — Last {len(hist)} readings:")
            print(f"  {'Time':>10} | {'CE OI':>14} | {'PE OI':>14} | {'Diff (PE-CE)':>16}")
            print(f"  {'-'*10}-+-{'-'*14}-+-{'-'*14}-+-{'-'*16}")
            for t, c, p in hist:
                print(f"  {t:>10} | {c:>14,} | {p:>14,} | {(p - c):>+16,}")

        print("\n--- PCR ---")
        print(f"9. PCR (All Strikes)          : {pcr_all:.2f}")
        print(f"10. PCR (ATM ±3)              : {pcr_atm:.2f}")
        print(f"11. PCR Change OI (ATM ±3)    : {pcr_chg:.2f}")

        print("\n--- COI IMBALANCE (%) ---")
        print(f"12. CALL COI % (Overall)      : {coi_ce_all:.2f}%")
        print(f"13. PUT  COI % (Overall)      : {coi_pe_all:.2f}%")
        print(f"14. CALL COI % (ATM ±3)       : {coi_ce_atm:.2f}%")
        print(f"15. PUT  COI % (ATM ±3)       : {coi_pe_atm:.2f}%")

        print("\n--- MARKET SENTIMENT ---")
        print(f"SENTIMENT                    : {sentiment}")

        # === LEVELS & STRENGTH ===
        print("\n--- RESISTANCE LEVELS & STRENGTH ---")
        for level, ce_oi, total_oi in m["resistance_strength"]:
            print(f"Resistance: {level} | CE OI: {ce_oi:,} | Total OI: {total_oi:,}")
        print("\n--- SUPPORT LEVELS & STRENGTH ---")
        for level, pe_oi, total_oi in m["support_strength"]:
            print(f"Support: {level} | PE OI: {pe_oi:,} | Total OI: {total_oi:,}")


        # === Live signal update on each run ===
        breakout_live = None
        breakdown_live = None
        breakout_prob = 0
        breakdown_prob = 0
        signal_strength = {}
        signal_consistency = {}

        # Calculate distance to resistance/support and probability
        for level, ce_oi, total_oi in m["resistance_strength"]:
            dist = m["price"] - level
            strength = ce_oi
            consistency = min(1.0, ce_oi / (total_oi + 1e-6))
            prob = max(0, min(1, 1 - abs(dist) / (level * 0.01) + consistency * 0.5))  # crude estimate
            signal_strength[level] = strength
            signal_consistency[level] = consistency
            if dist > 0:
                breakout_live = (level, dist, strength, consistency, prob)
                breakout_prob = prob
        for level, pe_oi, total_oi in m["support_strength"]:
            dist = m["price"] - level
            strength = pe_oi
            consistency = min(1.0, pe_oi / (total_oi + 1e-6))
            prob = max(0, min(1, 1 - abs(dist) / (level * 0.01) + consistency * 0.5))
            signal_strength[level] = strength
            signal_consistency[level] = consistency
            if dist < 0:
                breakdown_live = (level, dist, strength, consistency, prob)
                breakdown_prob = prob

        # Print live signal info
        print("\n--- LIVE SIGNAL UPDATE ---")
        if breakout_live:
            print(f"Breakout above {breakout_live[0]} | Distance: {breakout_live[1]:.2f} | Strength: {breakout_live[2]:,} | Consistency: {breakout_live[3]*100:.1f}% | Probability: {breakout_live[4]*100:.1f}%")
        else:
            print("No breakout imminent.")
        if breakdown_live:
            print(f"Breakdown below {breakdown_live[0]} | Distance: {breakdown_live[1]:.2f} | Strength: {breakdown_live[2]:,} | Consistency: {breakdown_live[3]*100:.1f}% | Probability: {breakdown_live[4]*100:.1f}%")
        else:
            print("No breakdown imminent.")

        # === 15-min candle close breakout/breakdown detection ===
        now = time.time()
        breakout_signal = None
        breakdown_signal = None
        if last_candle_close_time is None or now - last_candle_close_time >= candle_interval:
            for level in m["resistance"] + m["support"]:
                candle_history.setdefault(level, []).append(m["price"])
            last_candle_close_time = now
            for level in m["resistance"]:
                if candle_history[level][-1] > level:
                    breakout_signal = (level, m["price"])
            for level in m["support"]:
                if candle_history[level][-1] < level:
                    breakdown_signal = (level, m["price"])
            if breakout_signal:
                print(f"\n🚀 BREAKOUT SIGNAL: Price closed above resistance {breakout_signal[0]} | Market Direction: UP | Target: Next resistance")
            if breakdown_signal:
                print(f"\n🔻 BREAKDOWN SIGNAL: Price closed below support {breakdown_signal[0]} | Market Direction: DOWN | Target: Next support")

        # === AI Trading Summary ===
        price_change = (m["price"] - prev_price) if prev_price is not None else 0.0
        print("\n--- AI OI SUMMARY ---")
        print(generate_trading_summary(m, price_change))

        # === Save to Excel ===
        ws.append([
            m["time"], m["price"], m["atm"],
            m["total_oi_ce"], m["total_oi_pe"],
            m["total_chg_ce"], m["total_chg_pe"],
            m["atm_oi_ce"], m["atm_oi_pe"],
            m["atm_chg_ce"], m["atm_chg_pe"],
            pcr_all, pcr_atm, pcr_chg,
            coi_ce_all, coi_pe_all,
            coi_ce_atm, coi_pe_atm,
            ce_act, pe_act, ce_pow, pe_pow,
            str(m["resistance"]), str(m["support"]), sentiment,
            str(m["resistance_strength"]), str(m["support_strength"]),
            "BREAKOUT" if breakout_signal else "", "BREAKDOWN" if breakdown_signal else "",
            breakout_live[0] if breakout_live else "", breakout_live[1] if breakout_live else "", breakout_live[2] if breakout_live else "", breakout_live[3] if breakout_live else "", breakout_live[4] if breakout_live else "",
            breakdown_live[0] if breakdown_live else "", breakdown_live[1] if breakdown_live else "", breakdown_live[2] if breakdown_live else "", breakdown_live[3] if breakdown_live else "", breakdown_live[4] if breakdown_live else ""
        ])

        wb.save(EXCEL_FILE)

        prev_price = m["price"]
        prev_ce_oi = m["atm_oi_ce"]
        prev_pe_oi = m["atm_oi_pe"]

        time.sleep(REFRESH_SECONDS)

    except Exception as e:
        print("Error:", e)
        time.sleep(10)
