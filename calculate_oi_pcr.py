import requests
import time
from datetime import datetime
from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import ColorScaleRule, FormulaRule
from openpyxl.styles import PatternFill

# ================= CONFIG =================
URL = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
PARAMS = {
    "functionName": "getOptionChainData",
    "symbol": "NIFTY",
    "params": "expiryDate=23-Dec-2025"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/"
}

REFRESH_SECONDS = 120
EXCEL_FILE = "oi_pcr_dashboard.xlsx"

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

        ce_oi = ce.get("openInterest", 0)
        pe_oi = pe.get("openInterest", 0)
        ce_chg = ce.get("changeinOpenInterest", 0)
        pe_chg = pe.get("changeinOpenInterest", 0)

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
        "resistance": sorted(ce_map, key=ce_map.get, reverse=True)[:3],
        "support": sorted(pe_map, key=pe_map.get, reverse=True)[:3],
    }

# ================= MAIN LOOP =================
wb = init_excel()
ws = wb["DATA"]

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
            str(m["resistance"]), str(m["support"]), sentiment
        ])

        wb.save(EXCEL_FILE)

        prev_price = m["price"]
        prev_ce_oi = m["atm_oi_ce"]
        prev_pe_oi = m["atm_oi_pe"]

        time.sleep(REFRESH_SECONDS)

    except Exception as e:
        print("Error:", e)
        time.sleep(10)
