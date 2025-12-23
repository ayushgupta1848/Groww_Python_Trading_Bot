import os
import threading
import time

import pyotp
import requests
from collections import deque
from datetime import datetime

from growwapi import GrowwAPI
from playsound3 import playsound

########################################
# CONFIG
########################################

api_key = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NTAwNDY3MzksImlhdCI6MTc2MTY0NjczOSwibmJmIjoxNzYxNjQ2NzM5LCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCI2MmEwMTc4YS0wOTk3LTQ0ZDAtOWRiNC0wZDAzOWM5MzY3YmZcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNWQwYzdjODgtMGI1OS01MDU0LTk5ZTYtYWU5MzY5OTc2ZmRiXCIsXCJzZXNzaW9uSWRcIjpcIjY1NzBiNDUwLWE2YzYtNDMyYi1hYTJmLTA4MjExZjk0YzRiOVwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIxNzEuNjAuMTY5LjI1MiwxNzIuNjkuOTUuOTMsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NTAwNDY3Mzk5MTV9IiwiaXNzIjoiYXBleC1hdXRoLXByb2QtYXBwIn0.EKERC7OzG-lblhaOSQPyb44mafdNFpErGbcELiTiLnRk4WEW9p7aBBf6iq-3LGagY4ORdOCnrXbRhyGzbscxSw"
totp_gen = pyotp.TOTP('WI4M7KCAMH5CGN2I6SVB6MN2QDKUXRJF')
SOUND_user_input = "User_input.WAV"


FETCH_INTERVAL = 10        # seconds
WINDOW_SECONDS = 10        # comparison window
UP_THRESHOLD = 12          # weight threshold
DOWN_THRESHOLD = -12

########################################
# HEAVYWEIGHT STOCKS (NIFTY WEIGHT)
########################################

HEAVY_STOCKS = {
    "NSE_RELIANCE": 10.5,
    "NSE_HDFCBANK": 9.5,
    "NSE_ICICIBANK": 7.8,
    "NSE_TCS": 4.5,
    "NSE_INFY": 4.0,
}

########################################
# FETCH LTPs (YOUR CURL)
########################################

def groww_init(api_key):
    """
    Return growwapi client instance (GrowwAPI(access_token))
    This function gets access_token using GrowwAPI.get_access_token if available.
    """
    totp = totp_gen.now()
    try:
        access_token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
        client = GrowwAPI(access_token)
        print(access_token)
        print("✅ Groww API Initialized Successfully")
        return client, access_token
    except Exception as e:
        print(f"❌ Groww login failed: {e}")
        raise

# Init groww client
groww ,access_token = groww_init(api_key)

def play_sound_async(filename):
    try:
        if not os.path.exists(filename):
            print(f"🔇 Sound file not found: {filename}")
            return
        threading.Thread(target=playsound, args=(filename,), daemon=True).start()
    except Exception as e:
        print(f"🔇 Sound error: {e}")

def fetch_heavyweight_ltps():
    url = (
        "https://api.groww.in/v1/live-data/ltp"
        "?segment=CASH"
        "&exchange_symbols="
        "NSE_RELIANCE,NSE_TCS,NSE_HDFCBANK,NSE_ICICIBANK,NSE_INFY"
    )

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0"
    }

    try:
        r = requests.get(url, headers=headers, timeout=5)
        r.raise_for_status()
        return r.json().get("payload", {})
    except Exception as e:
        print("⚠️ API Error:", e)
        return {}

########################################
# FORCE CALCULATION
########################################

def calculate_force(old_prices, new_prices):
    force = 0.0
    details = []

    for sym, weight in HEAVY_STOCKS.items():
        if sym not in old_prices or sym not in new_prices:
            continue

        move_pct = ((new_prices[sym] - old_prices[sym]) / old_prices[sym]) * 100
        move_pct = round(move_pct, 3)

        details.append(f"{sym.replace('NSE_', '')} {move_pct:+.2f}%")

        if move_pct >= 0.15:
            force += weight
        elif move_pct <= -0.15:
            force -= weight

    return round(force, 2), details

########################################
# MAIN INDICATOR LOOP
########################################

def run_indicator():
    print("\n📊 NIFTY HEAVYWEIGHT FORCE INDICATOR STARTED\n")

    history = deque(maxlen=int(WINDOW_SECONDS / FETCH_INTERVAL) + 1)

    while True:
        prices = fetch_heavyweight_ltps()
        if prices:
            history.append((time.time(), prices))

        if len(history) >= 2:
            old_time, old_prices = history[0]
            new_time, new_prices = history[-1]

            force, details = calculate_force(old_prices, new_prices)

            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n{ts} | " + " | ".join(details))
            print(f"➡️ COMBINED FORCE = {force}")

            if force >= UP_THRESHOLD:
                print("🟢 STRONG UP PRESSURE (CE ENVIRONMENT)")
                play_sound_async(SOUND_user_input)
            elif force <= DOWN_THRESHOLD:
                print("🔴 STRONG DOWN PRESSURE (PE ENVIRONMENT)")
                play_sound_async(SOUND_user_input)
            else:
                print("🟡 NO CLEAR DOMINANCE")

        time.sleep(FETCH_INTERVAL)

########################################
# START
########################################

if __name__ == "__main__":
    run_indicator()
