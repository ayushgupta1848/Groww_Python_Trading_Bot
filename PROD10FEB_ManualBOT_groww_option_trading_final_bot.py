# Groww_CP_Bot.py


############### MUST READ #####################
#• To start Bot we just need to assure that config data is proper,
# Need to remove comments from "place_cp_order" method to validate the order status under comment starts from "#STATUS VALIDATION",
# And in last we need to validate that funds are matching with BOT and Groww Account unnder comment "Funds check"

#DOWNLOAD GROWW INSTRUMENTS:- https://growwapi-assets.groww.in/instruments/instrument.csv

import os
import re
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import pyotp
from openpyxl import Workbook, load_workbook
from playsound3 import playsound
from datetime import datetime, timedelta
from threading import Lock
import requests
import sys
from datetime import datetime
import time
import os
import sys
from datetime import datetime
import numpy as np

# ENHANCEMENT: Use a session for persistent HTTP connections (faster polling)
session = requests.Session()

MOMENTUM_SAMPLES = 3  # Reduced from 5 for faster execution
MOMENTUM_DELAY = 0.5  # Reduced from 1 second

def setup_persistent_logger():
    """Creates a local 'logs' folder beside the script and logs all console output there."""
    # Create /logs folder in the same directory as the script
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    # Create a timestamped log file
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(log_dir, f"Groww_Bot_{timestamp}.log")

    # Define a Tee class to write to both console and log file
    class Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, data):
            for s in self.streams:
                try:
                    s.write(data)
                    s.flush()
                except Exception:
                    pass  # Ignore on shutdown

        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    # Open log file (unbuffered, UTF-8)
    logfile = open(log_path, "a", buffering=1, encoding="utf-8")

    # Redirect both stdout and stderr
    sys.stdout = Tee(sys.stdout, logfile)
    sys.stderr = Tee(sys.stderr, logfile)

    print(f"📝 Logging started. Log file: {log_path}")

    return log_path


# --- Initialize persistent logging ---
LOG_FILE_PATH = setup_persistent_logger()

# Replace with your Groww API key (or leave and use TOTP to fetch access_token)
api_key = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NTAwNDY3MzksImlhdCI6MTc2MTY0NjczOSwibmJmIjoxNzYxNjQ2NzM5LCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCI2MmEwMTc4YS0wOTk3LTQ0ZDAtOWRiNC0wZDAzOWM5MzY3YmZcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNWQwYzdjODgtMGI1OS01MDU0LTk5ZTYtYWU5MzY5OTc2ZmRiXCIsXCJzZXNzaW9uSWRcIjpcIjY1NzBiNDUwLWE2YzYtNDMyYi1hYTJmLTA4MjExZjk0YzRiOVwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIxNzEuNjAuMTY5LjI1MiwxNzIuNjkuOTUuOTMsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NTAwNDY3Mzk5MTV9IiwiaXNzIjoiYXBleC1hdXRoLXByb2QtYXBwIn0.EKERC7OzG-lblhaOSQPyb44mafdNFpErGbcELiTiLnRk4WEW9p7aBBf6iq-3LGagY4ORdOCnrXbRhyGzbscxSw"
totp_gen = pyotp.TOTP('WI4M7KCAMH5CGN2I6SVB6MN2QDKUXRJF')

# Get project root directory (folder where your script is running)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
# Build CSV path dynamically
csv_path = os.path.join(PROJECT_ROOT, "instrument.csv")
print(csv_path)

# Instruments CSV/JSON path (script will convert CSV -> JSON if convert_csv_to_json = yes)
# csv_path = r"C:\Users\HITS\Downloads\instrument (6).csv"
convert_csv_to_json = "yes"

# Telegram placeholders (you will replace later)
TELEGRAM_BOT_TOKEN = "PUT_YOUR_TOKEN_HERE"
TELEGRAM_CHAT_ID = "PUT_YOUR_CHAT_ID_HERE"

# Sound files (ensure these exist in script folder or provide full path)
SOUND_PROFIT = "coin.mp3"
SOUND_SL = "SL_HIT.mp3"
SOUND_user_input = "User_input.WAV"

# Trade defaults for Groww
DEFAULT_PRODUCT = "MIS"   # intraday; change to "NRML" if you want positional
ORDER_PRODUCT_MAP = {
    "MIS": "MIS",
    "NRML": "NRML"
}
# NOTE: the growwapi wrapper constants are used from the growwapi instance below

# ----------------- import growwapi late (after auth) -----------------
try:
    from growwapi import GrowwAPI
except Exception:
    # If local module not available, user must install or place it in PYTHONPATH
    print("❗ growwapi module not found. Make sure it's installed and importable.")
    # continue; import errors will show when script runs further

# ----------------- Groww auth & wrapper -----------------
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


# ----------------- Utilities: Telegram, Sound, Excel Logging -----------------

# === TELEGRAM CONFIG ===
BOT_TOKEN = "8226223419:AAGX5fKG21CfceF_0_WjPIrOMx6ON17pZMw"
CHAT_ID = "6012308856"

def send_telegram(message: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message}
        requests.post(url, data=payload, timeout=3)  # 3-second timeout
    except Exception as e:
        pass  # Silently ignore Telegram errors to avoid spam

def play_sound_async(filename):
    try:
        if not os.path.exists(filename):
            print(f"🔇 Sound file not found: {filename}")
            return
        threading.Thread(target=playsound, args=(filename,), daemon=True).start()
    except Exception as e:
        print(f"🔇 Sound error: {e}")

def log_trade_to_excel(symbol, buy_price, sell_price, quantity, profit):
    file_name = "Lakshmi.xlsx"
    if not os.path.exists(file_name):
        wb = Workbook()
        ws = wb.active
        ws.title = "Trades"
        ws.append(["DateTime", "Symbol", "Buy Price", "Sell Price", "Quantity", "Profit"])
        wb.save(file_name)

    # Load existing workbook
    wb = load_workbook(file_name)
    ws = wb.active

    # Find the next empty row
    next_row = ws.max_row + 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ws.cell(row=next_row, column=1).value = now
    ws.cell(row=next_row, column=2).value = symbol
    ws.cell(row=next_row, column=3).value = buy_price
    ws.cell(row=next_row, column=4).value = sell_price
    ws.cell(row=next_row, column=5).value = quantity
    ws.cell(row=next_row, column=6).value = round(profit, 2)
    wb.save(file_name)


# ----------------- CSV -> JSON loader -----------------
def csv_to_json(csv_file_path, json_file_path=None):
    """
    Converts CSV to JSON only if JSON doesn't exist or CSV is newer.
    🚀 PERFORMANCE: Skips conversion if JSON is up-to-date
    """
    import csv
    if json_file_path is None:
        json_file_path = os.path.splitext(csv_file_path)[0] + ".json"
    
    # 🚀 Skip conversion if JSON exists and is newer than CSV
    if os.path.exists(json_file_path) and os.path.exists(csv_file_path):
        json_time = os.path.getmtime(json_file_path)
        csv_time = os.path.getmtime(csv_file_path)
        if json_time >= csv_time:
            print(f"⚡ Using existing JSON (up-to-date): '{json_file_path}'")
            with open(json_file_path, 'r', encoding='utf-8') as jf:
                return json.load(jf)
    
    # Convert CSV to JSON
    data = []
    with open(csv_file_path, encoding='utf-8') as csv_file:
        csv_reader = csv.DictReader(csv_file)
        for row in csv_reader:
            data.append(row)
    with open(json_file_path, 'w', encoding='utf-8') as json_file:
        json.dump(data, json_file, indent=4, ensure_ascii=False)
    print(f"✅ Converted '{csv_file_path}' → '{json_file_path}'")
    return data

ltp_lock = threading.Lock()

def get_ltp_for_instrument(instrument, access_token, verbose=True, segment="FNO", delay=0.05, max_retries=2):
    """
    Fetches the latest traded price (LTP) for a given F&O instrument using Groww's authenticated API.
    Automatically detects exchange (NSE/BSE) from instrument data.
    Thread-safe with a global lock to prevent too-frequent API calls.
    """

    try:
        trading_symbol = instrument.get("trading_symbol")  # e.g. NIFTY25N0425950CE or SENSEX2621283500CE
        if not trading_symbol:
            print("⚠️ Missing trading_symbol in instrument.")
            return None

        # Detect exchange from instrument data
        exchange = instrument.get("exchange", "NSE").upper()  # NSE or BSE
        exchange_symbol = f"{exchange}_{trading_symbol}"
        
        url = f"https://api.groww.in/v1/live-data/ltp?segment={segment}&exchange_symbols={exchange_symbol}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }

        # 🔒 Lock ensures one API call at a time
        with ltp_lock:
            resp = session.get(url, headers=headers, timeout=5)  # Use session + reduced timeout
            if delay > 0:
                time.sleep(delay)  # ⏳ short delay to respect Groww API rate limits

        if resp.status_code != 200:
            print(f"⚠️ HTTP {resp.status_code} error fetching LTP: {resp.text}")
            return None

        data = resp.json()
        payload = data.get("payload", {})
        ltp = payload.get(exchange_symbol)

        if ltp is None:
            print(f"⚠️ No LTP found for {exchange_symbol} in payload: {payload}")
            return None
        if verbose:
            print(f"💰 LTP for {exchange_symbol}: ₹{ltp} ====== [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
            try:
                send_telegram(f"💰 LTP for {exchange_symbol}: ₹{ltp} ====== [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
            except:
                pass
        return float(ltp)

    except Exception as e:
        print(f"⚠️ Error fetching LTP for {instrument.get('trading_symbol')}: {e}")
        return None

def get_user_positions(access_token):
    """
    Fetches user's current positions from Groww API.
    Returns: dict with positions data or None
    """
    try:
        url = "https://api.groww.in/v1/positions/user"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }
        
        resp = session.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ HTTP {resp.status_code} error fetching positions: {resp.text}")
            return None
        
        data = resp.json()
        if data.get("status") == "SUCCESS":
            return data.get("payload", {})
        else:
            print(f"⚠️ Failed to fetch positions: {data}")
            return None
            
    except Exception as e:
        print(f"⚠️ Error fetching positions: {e}")
        return None

def get_user_margins(access_token):
    """
    Fetches user's margin details from Groww API.
    Returns: dict with margin data or None
    """
    try:
        url = "https://api.groww.in/v1/margins/detail/user"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }
        
        resp = session.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ HTTP {resp.status_code} error fetching margins: {resp.text}")
            return None
        
        data = resp.json()
        if data.get("status") == "SUCCESS":
            return data.get("payload", {})
        else:
            print(f"⚠️ Failed to fetch margins: {data}")
            return None
            
    except Exception as e:
        print(f"⚠️ Error fetching margins: {e}")
        return None

def display_account_summary(access_token):
    """
    Fetches and displays total realised P&L and available balance after trade execution.
    Uses clear visual indicators: 🟢 for profit, 🔴 for loss.
    """
    print("\n" + "="*60)
    print("📊 ACCOUNT SUMMARY AFTER TRADE")
    print("="*60)
    
    # Fetch positions for realised P&L
    positions_data = get_user_positions(access_token)
    total_realised_pnl = 0.0
    
    if positions_data and "positions" in positions_data:
        for pos in positions_data["positions"]:
            realised_pnl = pos.get("realised_pnl", 0)
            if realised_pnl:
                total_realised_pnl += float(realised_pnl)
                trading_symbol = pos.get("trading_symbol", "N/A")
                
                # Use clear visual indicators
                if float(realised_pnl) < 0:
                    print(f"  📈 {trading_symbol}: 🔴 LOSS: ₹{realised_pnl} ❌")
                else:
                    print(f"  📈 {trading_symbol}: 🟢 PROFIT: ₹{realised_pnl} ✅")
        
        # Total P&L with clear indicators
        print("\n" + "-"*60)
        if total_realised_pnl < 0:
            print(f"💰 Total Realised P&L: 🔴 LOSS: ₹{total_realised_pnl:.2f} ❌")
        else:
            print(f"💰 Total Realised P&L: 🟢 PROFIT: ₹{total_realised_pnl:.2f} ✅")
        print("-"*60)
    else:
        print("⚠️ Could not fetch positions data")
    
    # Fetch margins for available balance
    margins_data = get_user_margins(access_token)
    
    if margins_data:
        fno_details = margins_data.get("fno_margin_details", {})
        option_buy_balance = fno_details.get("option_buy_balance_available", 0)
        
        print(f"💵 Option Buy Balance Available: ₹{option_buy_balance:.2f}")
        
        # Also show clear cash
        clear_cash = margins_data.get("clear_cash", 0)
        print(f"💸 Clear Cash: ₹{clear_cash:.2f}")
    else:
        print("⚠️ Could not fetch margins data")
    
    print("="*60 + "\n")
    
    # Send to Telegram
    try:
        summary_msg = f"""
📊 ACCOUNT SUMMARY
━━━━━━━━━━━━━━━━
💰 Total Realised P&L: ₹{total_realised_pnl:.2f}
💵 Option Buy Balance: ₹{option_buy_balance:.2f}
💸 Clear Cash: ₹{clear_cash:.2f}
        """
        send_telegram(summary_msg)
    except:
        pass

def get_index_spot_price(index_name, access_token=None, json_path=None):
    """
    Fetches live spot price for any index (NIFTY, SENSEX, BANKNIFTY, etc.) using Groww instrument data.
    For SENSEX, fetches from option chain if spot instrument not available.
    🚀 OPTIMIZED: Uses cached instruments if available
    
    Args:
        index_name: 'NIFTY', 'SENSEX', 'BANKNIFTY', etc.
        access_token: Groww API access token
        json_path: Path to instruments JSON
    
    Returns:
        float: Spot price or 0 if failed
    """
    global instruments1, _instruments_cache
    index_name = index_name.upper()

    # 🚀 Try to use cached instruments first (much faster)
    if _instruments_cache.get("data"):
        # Check if we have instruments for this index in cache
        cached_underlying = _instruments_cache.get("index", "").upper()
        if cached_underlying == index_name or not cached_underlying:
            instruments1 = _instruments_cache["data"]
            # print(f"⚡ Using cached instruments for {index_name} spot lookup")
        else:
            # Need to load fresh for different index
            instruments1 = None
    else:
        instruments1 = None

    # Load from file if not cached
    if not instruments1:
        if json_path is None:
            json_path = os.path.splitext(csv_path)[0] + ".json"

        # 🔄 Load or convert JSON
        if convert_csv_to_json.lower() == "yes":
            instruments1 = csv_to_json(csv_path, json_path)
        else:
            if not os.path.exists(json_path):
                raise FileNotFoundError(f"JSON not found: {json_path}")
            with open(json_path, "r", encoding="utf-8") as jf:
                instruments1 = json.load(jf)
            # print(f"ℹ️ Loaded instruments for spot lookup")

    # Map index names to their various identifiers
    index_mappings = {
        "NIFTY": ["NIFTY", "NSE-NIFTY", "NIFTY 50"],
        "SENSEX": ["SENSEX", "BSE-SENSEX", "SENSEX", "BSE_SENSEX"],
        "BANKNIFTY": ["BANKNIFTY", "NIFTY BANK", "NSE-BANKNIFTY"],
        "FINNIFTY": ["FINNIFTY", "NIFTY FIN SERVICE", "NSE-FINNIFTY"]
    }
    
    search_terms = index_mappings.get(index_name, [index_name])

    try:
        # Special handling for SENSEX - use option chain to get underlying price
        if index_name == "SENSEX":
            print(f"📊 Fetching {index_name} spot from option chain...")
            try:
                # Get current/near expiry from instruments
                sensex_options = [item for item in instruments1 
                                 if item.get("underlying_symbol", "").upper() == "SENSEX" 
                                 and item.get("segment") == "FNO"]
                
                if sensex_options:
                    # Get first available expiry
                    expiry = sensex_options[0].get("expiry_date")
                    url = f"https://api.groww.in/v1/option-chain/exchange/BSE/underlying/SENSEX?expiry_date={expiry}"
                    headers = {
                        "Accept": "application/json",
                        "Authorization": f"Bearer {access_token}",
                        "X-API-VERSION": "1.0"
                    }
                    resp = session.get(url, headers=headers, timeout=8)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "SUCCESS":
                            underlying_ltp = data.get("payload", {}).get("underlying_ltp")
                            if underlying_ltp:
                                print(f"📊 Live {index_name} Spot (from option chain): {underlying_ltp}")
                                return float(underlying_ltp)
            except Exception as e:
                print(f"⚠️ Could not fetch {index_name} from option chain: {e}")

        # Try standard spot instrument lookup for NSE indices
        spot_instrument = next(
            (item for item in instruments1
             if item.get("trading_symbol", "").upper() in search_terms
             or item.get("groww_symbol", "").upper() in [f"NSE-{t}" for t in search_terms]
             or item.get("groww_symbol", "").upper() in [f"BSE-{t}" for t in search_terms]
             or item.get("name", "").upper() in search_terms),
            None
        )

        if not spot_instrument:
            print(f"⚠️ {index_name} spot instrument not found in instruments")
            return 0

        # SENSEX is on BSE, others typically on NSE
        segment = "CASH"
        spot = get_ltp_for_instrument(spot_instrument, access_token, verbose=False, segment=segment)
        
        if spot:
            print(f"📊 Live {index_name} Spot: {spot}")
            return float(spot)
        else:
            print(f"⚠️ Failed to fetch LTP for {index_name} spot")
            return 0
    except Exception as e:
        print(f"⚠️ Error fetching {index_name} spot: {e}")
        return 0

# Backward compatibility wrapper
def get_nifty_spot_price(access_token=None, json_path=None):
    return get_index_spot_price("NIFTY", access_token, json_path)


CONFIG = {
    "index": "NIFTY",  # Change to "SENSEX", "BANKNIFTY", "FINNIFTY" as needed
    "expiry": "2026-03-24",  #this needs to be same as expiry_date in json file of instruments # format DD/MM/YYYY to match instruments JSON (example)
    "min_premium": 90,
    "max_premium": 230,
    "lots": 16,
    "book_profit": 1050,
    "target_pnl": 6000,
    "spot": 0,  # Will be fetched dynamically below
    "TRAIL_START_PROFIT": 1,  # Start trailing after this profit per unit (in points)
    "TRAIL_STEP": .75,  # Trailing step (in points)
    "POLL_INTERVAL": 0.15,  # Poll interval in seconds (Optimized for speed)
    "MAX_TRAIL_TIME": 3600,  # Max trailing time in seconds (1 hour)
    "HARD_SL_POINTS": 6.0,  # Hard stop loss points below entry
    "VALIDATE_ORDERS": True,  # ✅ LIVE TRADING: Set True to validate BUY/SELL execution (RECOMMENDED)
    "user_confirmation_needed": False,   # or False
    "ENABLE_EMA_CHECK": False,
    "ENABLE_ADX_CHECK": False,
    "ENABLE_RSI_CHECK": False,
    "ENABLE_VWAP_CHECK": False,
    "ENABLE_LOGICAL_CONDITIONS_CHECK": False,
    # Directional Mode Settings
    "DIRECTIONAL_MODE": {
        "prefer_mid_premium": True,  # Pick option closest to mid-range of min/max premium (legacy, not used in new format)
    }
}

# 🚀 PERFORMANCE: Global cache to avoid reloading instruments (must be defined before usage)
_instruments_cache = {
    "data": None,
    "index": None,
    "expiry": None,
    "spot_range": None
}

# Dynamically fetch spot price based on selected index
CONFIG["spot"] = get_index_spot_price(CONFIG["index"], access_token)
print(f"🎯 Using {CONFIG['index']} with spot price: {CONFIG['spot']}")

# Load instruments_data
def load_instruments_from_json(json_path=None, force_reload=False):
    """
    Loads instruments from JSON (or CSV → JSON if convert_csv_to_json = 'yes'),
    but only keeps instruments:
      - matching expiry from CONFIG
      - within ±10 strikes of current index spot price
    
    🚀 CACHED: Returns cached data if index/expiry/spot haven't changed
    """
    global instruments, _instruments_cache
    config = CONFIG
    INDEX = config["index"].upper()
    EXPIRY = config["expiry"].strip()
    spot = config["spot"]
    
    # Determine strike step based on index
    if "BANK" in INDEX:
        step = 100
    elif "SENSEX" in INDEX:
        step = 100
    elif "FINNIFTY" in INDEX:
        step = 50
    else:  # NIFTY default
        step = 50
    
    nearest_strike = round(spot / step) * step
    lower_bound = nearest_strike - (10 * step)
    upper_bound = nearest_strike + (10 * step)
    spot_range = (lower_bound, upper_bound)
    
    # 🚀 Check cache first
    if (not force_reload and 
        _instruments_cache["data"] is not None and
        _instruments_cache["index"] == INDEX and
        _instruments_cache["expiry"] == EXPIRY and
        _instruments_cache["spot_range"] == spot_range):
        print(f"⚡ Using cached {INDEX} instruments ({len(_instruments_cache['data'])} loaded)")
        return _instruments_cache["data"]
    
    print(f"💾 Loading instruments from file...")

    if json_path is None:
        json_path = os.path.splitext(csv_path)[0] + ".json"

    # 🔄 Step 1: Load or convert JSON
    if convert_csv_to_json.lower() == "yes":
        instruments = csv_to_json(csv_path, json_path)
    else:
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"JSON not found: {json_path}")
        with open(json_path, "r", encoding="utf-8") as jf:
            instruments = json.load(jf)
        print(f"ℹ️ Loaded instruments from existing JSON: {json_path}")

    # 🧩 Step 2: Get live index spot
    spot = config["spot"]

    # Determine strike step based on index
    # NIFTY = 50, BANKNIFTY = 100, SENSEX = 100, FINNIFTY = 50
    if "BANK" in INDEX:
        step = 100
    elif "SENSEX" in INDEX:
        step = 100
    elif "FINNIFTY" in INDEX:
        step = 50
    else:  # NIFTY default
        step = 50

    # Define strike range (±20 strikes for wider OTM coverage)
    nearest_strike = round(spot / step) * step
    lower_bound = nearest_strike - (20 * step)
    upper_bound = nearest_strike + (20 * step)

    print(f"🎯 Filtering {INDEX} {EXPIRY} instruments between {lower_bound}–{upper_bound} (Spot={spot})")

    # 🧹 Step 3: Filter instruments by expiry and strike range
    filtered = []
    for item in instruments:
        try:
            if item.get("underlying_symbol", "").upper() != INDEX:
                continue
            # if item.get("expiry_date", "").strip() != EXPIRY:
            #     continue
            strike = float(item.get("strike_price") or 0)
            if lower_bound <= strike <= upper_bound:
                filtered.append(item)
        except Exception:
            continue

    print(f"✅ Loaded {len(filtered)} filtered instruments (out of {len(instruments)})")
    instruments = filtered
    
    # 🚀 Save to cache
    _instruments_cache["data"] = instruments
    _instruments_cache["index"] = INDEX
    _instruments_cache["expiry"] = EXPIRY
    _instruments_cache["spot_range"] = spot_range
    
    return instruments


# initialize
instruments_data = load_instruments_from_json()


# Pre-index instruments for quick lookup by internal_trading_symbol or groww_symbol or custom compact symbol
symbol_index = {}
for it in instruments_data:
    # keys: internal_trading_symbol, groww_symbol, compact like NIFTY04NOV2525950CE approximate
    try:
        k1 = it.get("internal_trading_symbol", "") or it.get("trading_symbol", "")
        k2 = it.get("groww_symbol", "")
        if k1:
            symbol_index[k1.upper()] = it
        if k2:
            symbol_index[k2.upper()] = it
    except Exception:
        pass

# ----------------- Helpers: date/expiry normalization -----------------
MONTHS = {
    'JAN': '01', 'JANUARY': '01',
    'FEB': '02', 'FEBRUARY': '02',
    'MAR': '03', 'MARCH': '03',
    'APR': '04', 'APRIL': '04',
    'MAY': '05',
    'JUN': '06', 'JUNE': '06',
    'JUL': '07', 'JULY': '07',
    'AUG': '08', 'AUGUST': '08',
    'SEP': '09', 'SEPTEMBER': '09',
    'OCT': '10', 'OCTOBER': '10',
    'NOV': '11', 'NOVEMBER': '11',
    'DEC': '12', 'DECEMBER': '12'
}

def cmd_expiry_to_date(expiry_token):
    """
    expiry_token example: 04NOV25 or 04NOV2025 or 02MARCH2026 or 28AUG25 or 28AUG2025
    Return string 'DD/MM/YYYY'
    """
    m = re.match(r'(\d{1,2})([A-Z]+)(\d{2,4})', expiry_token.upper())
    if not m:
        return None
    dd = m.group(1).zfill(2)
    mon_abbr = m.group(2)
    yy = m.group(3)
    if len(yy) == 2:
        yyyy = "20" + yy
    else:
        yyyy = yy
    mm = MONTHS.get(mon_abbr, None)
    if not mm:
        return None
    return f"{yyyy}-{mm}-{dd}"


# ----------------- Command parser -----------------
def parse_cp_command(command):
    """
    Parse strings like:
      14 NIFTY30DEC2525950CE
    Returns dict or None
    """
    # Pattern to match: <lots> <TRADING_SYMBOL>
    pattern = r'^\s*(\d+)\s+([A-Z0-9]+)\s*$'
    m = re.match(pattern, command.strip())
    if not m:
        return None

    lots = int(m.group(1))
    trading_symbol_str = m.group(2).upper()

    return {
        "lots": lots,
        "trading_symbol_str": trading_symbol_str,
    }

def parse_trading_symbol_string(trading_symbol_str: str):
    """
    Parses a trading symbol string like 'NIFTY30DEC2525950CE' or 'NIFTY02MARCH202625300PE'
    into its components.
    Returns dict or None.
    """
    # Pattern: UNDERLYING(NIFTY) DAY(30) MONTH(DEC or MARCH) YEAR(25) STRIKE(25950) TYPE(CE)
    pattern = r'([A-Z]+)(\d{1,2}[A-Z]+\d{2,4})(\d+)(CE|PE)'
    m = re.match(pattern, trading_symbol_str)
    if not m:
        print(f"❌ Could not parse trading symbol string: {trading_symbol_str}")
        return None

    underlying = m.group(1).upper()
    expiry_token = m.group(2).upper()
    strike = m.group(3)
    opt_type = m.group(4).upper()
    expiry_date = cmd_expiry_to_date(expiry_token)

    if not expiry_date:
        print(f"❌ Could not derive expiry date from token: {expiry_token}")
        return None

    return {
        "underlying": underlying,
        "expiry_token": expiry_token,
        "expiry_date": expiry_date,
        "strike": strike,
        "opt_type": opt_type
    }

def find_instrument_by_details(underlying, expiry_date, strike, opt_type, instruments: list):
    """
    Finds an instrument in the master list based on parsed details.
    """
    print(f"🔍 Searching for: {underlying} | Expiry: {expiry_date} | Strike: {strike} | Type: {opt_type}")
    print(f"📦 Searching in {len(instruments)} loaded instruments...")
    
    for inst in instruments:
        if (
            inst["underlying_symbol"].upper() == underlying
            and inst["expiry_date"] == expiry_date
            and str(inst["strike_price"]) == strike
            and inst["instrument_type"].upper() == opt_type
        ):
            print(f"✅ Found: {inst.get('trading_symbol')} ({inst.get('groww_symbol')})")
            return inst
    
    # Debug: Show what we have for this underlying
    matching_underlying = [i for i in instruments if i["underlying_symbol"].upper() == underlying]
    if matching_underlying:
        print(f"⚠️ Found {len(matching_underlying)} {underlying} instruments, but none match the criteria")
        # Show a sample
        sample = matching_underlying[0]
        print(f"📋 Sample instrument format: Expiry={sample.get('expiry_date')}, Strike={sample.get('strike_price')}, Type={sample.get('instrument_type')}")
    else:
        print(f"⚠️ No {underlying} instruments found in loaded data")
    
    print(f"❌ Instrument not found: {underlying} {expiry_date} {strike} {opt_type}")
    return None


def calculate_sma(prices, period):
    if len(prices) < period:
        return None
    return np.mean(prices[-period:])


def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    # Initial SMA
    ema = np.mean(prices[:period])
    multiplier = 2 / (period + 1)
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calculate_rsi(prices, period=14):
    prices = np.array(prices)
    if len(prices) < period + 1:
        return 50  # Default neutral

    deltas = np.diff(prices)
    gains = np.maximum(deltas, 0)
    losses = -np.minimum(deltas, 0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # Wilder's Smoothing
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

    return rsi


def calculate_adx(highs, lows, closes, period=14):
    if len(highs) < period * 2:
        return 25  # Default

    highs = np.array(highs)
    lows = np.array(lows)
    closes = np.array(closes)

    tr = np.zeros(len(highs))
    plus_dm = np.zeros(len(highs))
    minus_dm = np.zeros(len(highs))

    for i in range(1, len(highs)):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        else:
            plus_dm[i] = 0

        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
        else:
            minus_dm[i] = 0

    # Smoothing
    def smooth(data, period):
        smoothed = np.zeros_like(data)
        if len(data) > period:
            smoothed[period] = np.mean(data[1:period + 1])  # Initial SMA
            for i in range(period + 1, len(data)):
                smoothed[i] = (smoothed[i - 1] * (period - 1) + data[i]) / period
        return smoothed

    atr = smooth(tr, period)
    plus_di = 100 * smooth(plus_dm, period) / (atr + 1e-9)  # Avoid div by zero
    minus_di = 100 * smooth(minus_dm, period) / (atr + 1e-9)

    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)
    adx = smooth(dx, period)

    return adx[-1]


def calculate_vwap(prices, volumes):
    prices = np.array(prices)
    volumes = np.array(volumes)
    if len(prices) == 0 or len(volumes) == 0 or None in volumes:
        return None

    vwap = np.cumsum(prices * volumes) / np.cumsum(volumes)
    return vwap[-1]

def calculate_atr(high, low, close, period=14):
    """Calculates the Average True Range (ATR)."""
    if len(high) < period:
        return 0
    high = np.array(high)
    low = np.array(low)
    close = np.array(close)
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr = np.amax((tr1, tr2, tr3), axis=0)
    atr = calculate_ema(tr, period)  # Use EMA for smoothing ATR
    return atr if atr is not None else 0


def get_technicals(symbol, groww_client, interval="1minute", segment="FNO", timeout=5, instrument=None):
    """
    Fetch technical indicators with timeout protection (optimized to 5s).
    Returns None if API call fails or times out.
    """
    try:
        # Detect exchange dynamically (BSE for SENSEX, NSE for others)
        if instrument:
            exch_str = instrument.get("exchange", "NSE").upper()
            exchange_const = groww_client.EXCHANGE_BSE if exch_str == "BSE" else groww_client.EXCHANGE_NSE
        else:
            exchange_const = groww_client.EXCHANGE_NSE
        
        # Fetch 60 mins data (reduced from 120 for speed)
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=60)

        end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")

        print(f"🔄 Fetching historical candles for {symbol}...")
        
        # Add timeout protection using threading
        import signal
        
        def timeout_handler(signum, frame):
            raise TimeoutError("Historical candles fetch timed out")
        
        # Set timeout alarm (Unix-based systems only)
        try:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout)
        except:
            pass  # Windows doesn't support signal.SIGALRM
        
        try:
            historical = groww_client.get_historical_candles(
                groww_symbol=symbol,
                exchange=exchange_const,
                segment=segment,
                start_time=start_str,
                end_time=end_str,
                candle_interval=interval
            )
        finally:
            try:
                signal.alarm(0)  # Cancel alarm
            except:
                pass

        if not historical:
            print("⚠️ No historical data returned")
            return None
            
        candles = historical.get("candles", [])
        if not candles or len(candles) < 20:  # Reduced from 30 for faster processing
            print(f"⚠️ Insufficient candles: {len(candles) if candles else 0}")
            return None

        print(f"✅ Fetched {len(candles)} candles")
        
        # Groww candles: [timestamp, open, high, low, close, volume]
        opens = [c[1] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        close_prices = [c[4] for c in candles]

        vwap = None
        # VWAP is only applicable for instruments with volume
        if segment == "FNO":
            volumes = [c[5] for c in candles]
            vwap = calculate_vwap(close_prices, volumes)

        # If VWAP couldn't be calculated, use last price as a fallback
        if vwap is None:
            vwap = close_prices[-1]

        sma_20 = calculate_sma(close_prices, 20)
        ema_9 = calculate_ema(close_prices, 9)
        rsi_14 = calculate_rsi(close_prices, 14)
        adx_14 = calculate_adx(highs, lows, close_prices, 14)
        atr = calculate_atr(highs, lows, close_prices)


        current_price = close_prices[-1]

        return {
            "sma_20": sma_20,
            "ema_9": ema_9,
            "rsi": rsi_14,
            "adx": adx_14,
            "vwap": vwap,
            "ltp": current_price,
            "atr": atr
        }
    except Exception as e:
        print(f"⚠️ Error fetching technicals: {e}")
        return None

# --- START: Caching layer to prevent API rate limiting ---
_option_chain_cache = {}
_option_chain_cache_lock = threading.Lock()
CACHE_EXPIRY_SECONDS = 15  # Cache for 15 seconds
_last_api_call_time = 0
_api_call_lock = threading.Lock()

def _get_full_option_chain_cached(underlying, expiry_date, access_token):
    """Cached option chain fetcher with rate limit handling"""
    global _last_api_call_time
    cache_key = (underlying, expiry_date)
    now = time.time()

    # Return cached if fresh
    cached_payload, timestamp = _option_chain_cache.get(cache_key, (None, 0))
    if cached_payload and (now - timestamp) < CACHE_EXPIRY_SECONDS:
        return cached_payload

    with _option_chain_cache_lock:
        # Double-check after lock
        cached_payload, timestamp = _option_chain_cache.get(cache_key, (None, 0))
        if cached_payload and (time.time() - timestamp) < CACHE_EXPIRY_SECONDS:
            return cached_payload

        url = f"https://api.groww.in/v1/option-chain/exchange/NSE/underlying/{underlying}?expiry_date={expiry_date}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }

        with _api_call_lock:
            time_since_last = time.time() - _last_api_call_time
            if time_since_last < 0.2:
                time.sleep(0.2 - time_since_last)
            
            resp = session.get(url, headers=headers, timeout=8)
            _last_api_call_time = time.time()

        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text}")

        data = resp.json()
        if data.get("status") != "SUCCESS":
            raise Exception("Failed to fetch option chain")

        payload = data["payload"]
        _option_chain_cache[cache_key] = (payload, time.time())
        return payload

# --- END: Caching layer ---


def get_option_data_from_trading_symbol(
    trading_symbol: str,
    exchange: str = "NSE",
    underlying: str = "NIFTY"
):
    """
    Fetch delta, theta, OI, LTP, IV, volume etc. for a given trading_symbol
    using a cached Groww Option Chain API call
    """
    expiry_date = CONFIG["expiry"].strip()

    try:
        payload = _get_full_option_chain_cached(underlying, expiry_date, access_token)
    except Exception as e:
        raise e

    strikes = payload["strikes"]
    underlying_ltp = payload["underlying_ltp"]

    # 🔍 Find this trading_symbol in option chain
    for strike_str, opt_data in strikes.items():
        for opt_type in ("CE", "PE"):
            opt = opt_data.get(opt_type)
            if not opt:
                continue

            if opt.get("trading_symbol") == trading_symbol:
                greeks = opt.get("greeks", {})

                return {
                    "trading_symbol": trading_symbol,
                    "option_type": opt_type,
                    "strike": int(strike_str),
                    "expiry": expiry_date,
                    "ltp": opt.get("ltp"),
                    "open_interest": opt.get("open_interest"),
                    "volume": opt.get("volume"),
                    "delta": greeks.get("delta"),
                    "theta": greeks.get("theta"),
                    "iv": greeks.get("iv"),
                    "gamma": greeks.get("gamma"),
                    "vega": greeks.get("vega"),
                    "rho": greeks.get("rho"),
                    "underlying_ltp": underlying_ltp
                }

    raise ValueError(f"{trading_symbol} not found in option chain")

# ----------------- Prefetcher (background) -----------------
def _option_chain_prefetcher_loop():
    """Daemon loop to keep option-chain cache warm"""
    cfg = CONFIG
    underlying = cfg.get("index", "NIFTY")
    expiry = cfg.get("expiry")
    interval = 10  # Prefetch every 10 seconds

    while True:
        try:
            _get_full_option_chain_cached(underlying, expiry, access_token)
            time.sleep(interval)
        except Exception as e:
            print(f"⚠️ Prefetcher error: {e}")
            time.sleep(30)

def start_option_chain_prefetcher():
    t = threading.Thread(target=_option_chain_prefetcher_loop, daemon=True, name="OptionChainPrefetcher")
    t.start()

# Start background prefetcher
try:
    start_option_chain_prefetcher()
    print("🔁 Option-chain prefetcher started.")
except Exception as e:
    print(f"⚠️ Failed to start prefetcher: {e}")

# ----------------- End prefetcher -----------------


# ----------------- Place orders with Groww -----------------
def place_market_order_groww(instrument, quantity, transaction_type="BUY", product="MIS"):
    """
    place market order via growwapi wrapper. Returns order response or raises.
    """
    trading_symbol = instrument.get("internal_trading_symbol") or instrument.get("trading_symbol")
    # Detect exchange dynamically (BSE for SENSEX, NSE for others)
    exch_str = instrument.get("exchange", "NSE").upper()
    exchange_const = groww.EXCHANGE_BSE if exch_str == "BSE" else groww.EXCHANGE_NSE
    try:
        order = groww.place_order(
            trading_symbol=trading_symbol,
            quantity=quantity,
            validity=groww.VALIDITY_DAY,
            exchange=exchange_const,
            segment=groww.SEGMENT_FNO,
            product=getattr(groww, f"PRODUCT_{product}") if hasattr(groww, f"PRODUCT_{product}") else getattr(groww, "PRODUCT_MIS", product),
            order_type=groww.ORDER_TYPE_MARKET,
            transaction_type=getattr(groww, f"TRANSACTION_TYPE_{transaction_type}"),
            price=0
        )
        return order
    except Exception as e:
        raise

def place_limit_order_groww(instrument, quantity, price, transaction_type="SELL", product="MIS"):
    trading_symbol = instrument.get("internal_trading_symbol") or instrument.get("trading_symbol")
    # Detect exchange dynamically (BSE for SENSEX, NSE for others)
    exch_str = instrument.get("exchange", "NSE").upper()
    exchange_const = groww.EXCHANGE_BSE if exch_str == "BSE" else groww.EXCHANGE_NSE
    try:
        order = groww.place_order(
            trading_symbol=trading_symbol,
            quantity=quantity,
            validity=groww.VALIDITY_DAY,
            exchange=exchange_const,
            segment=groww.SEGMENT_FNO,
            product=getattr(groww, f"PRODUCT_{product}") if hasattr(groww, f"PRODUCT_{product}") else getattr(groww, "PRODUCT_MIS", product),
            order_type=groww.ORDER_TYPE_LIMIT,
            transaction_type=getattr(groww, f"TRANSACTION_TYPE_{transaction_type}"),
            price=price
        )
        return order
    except Exception as e:
        raise

def cancel_order_groww(order_id, access_token):
    """
    Cancel a pending order using Groww API
    Returns True if cancellation successful, False otherwise
    """
    url = "https://api.groww.in/v1/order/cancel"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0"
    }
    
    payload = {
        "segment": "FNO",
        "groww_order_id": order_id
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        print(f"🔄 Cancel order response: {data}")
        
        # Check if cancellation was successful
        if data.get("success") or data.get("payload", {}).get("order_status") == "CANCELLED":
            return True
        return False
    except Exception as e:
        print(f"⚠️ Error cancelling order {order_id}: {e}")
        return False

# ----------------- Rounding for limits (5 paise) -----------------
def round_to_nearest_5_paise(price):
    # Round to nearest 0.05
    return round(round(price * 20) / 20, 2)

# ----------------- Momentum sampling -----------------

def momentum_check_for_symbol(instrument, MOMENTUM_SAMPLES=MOMENTUM_SAMPLES, MOMENTUM_DELAY=MOMENTUM_DELAY, threshold=0.25):
    """
    Improved short-term momentum check for Nifty options.
    - Uses multiple intermediate samples
    - Smooths noise
    - Checks direction consistency
    - Returns a cleaner momentum signal
    """
    trading_symbol = instrument.get("trading_symbol")
    prices = []

    print(f"\n🧭 Checking momentum for {trading_symbol} ({MOMENTUM_SAMPLES} samples, every {MOMENTUM_DELAY}s):")

    for i in range(MOMENTUM_SAMPLES):
        p = get_ltp_for_instrument(instrument, access_token, verbose=False)
        if p:
            price = float(p)
            prices.append(price)
            print(f"[{trading_symbol}] ⏱ Sample {i+1}/{MOMENTUM_SAMPLES}: LTP = ₹{price:.2f}")
        else:
            print(f"[{trading_symbol}] ⚠️ Sample {i+1}/{MOMENTUM_SAMPLES}: Failed to fetch LTP")
        time.sleep(MOMENTUM_DELAY)

    if len(prices) < 3:
        print(f"[{trading_symbol}] ❌ Not enough data ({len(prices)} samples)")
        return None, len(prices)

    prices = np.array(prices)

    # 1️⃣ Smooth noise with small moving average
    smooth = np.convolve(prices, np.ones(3)/3, mode="valid")

    # 2️⃣ Compute rate of change (%)
    roc = np.diff(smooth) / smooth[:-1] * 100

    # 3️⃣ Remove outliers (big spikes)
    median = np.median(roc)
    std = np.std(roc)
    filtered = roc[(roc > median - 1.5*std) & (roc < median + 1.5*std)]

    if len(filtered) < 2:
        print(f"[{trading_symbol}] ⚠️ Too noisy for reliable momentum reading")
        return None, len(prices)

    # 4️⃣ Average change and direction consistency
    avg_change = np.mean(filtered)
    direction_signs = np.sign(filtered)
    consistency = np.mean(direction_signs == np.sign(avg_change)) * 100

    # 5️⃣ Decision
    if avg_change > threshold and consistency > 70:
        direction = "UP"
    elif avg_change < -threshold and consistency > 70:
        direction = "DOWN"
    else:
        direction = "FLAT"

    print(f"[{trading_symbol}] 📊 Avg Δ = {avg_change:.3f}%, Consistency = {consistency:.1f}% → {direction}")
    print(f"[{trading_symbol}] 📈 Range ₹{prices[0]:.2f} → ₹{prices[-1]:.2f}\n")

    return {"symbol": trading_symbol,
            "avg_change": round(avg_change, 3),
            "consistency": round(consistency, 1),
            "direction": direction}, len(prices)



# ----------------- Find option by premium (parallel) -----------------

def find_option_by_premium_parallel(option_type, min_premium, max_premium,
                                    lots=1, funds_buffer=0.9, momentum_threshold_pct=0.25,
                                    MOMENTUM_SAMPLES=MOMENTUM_SAMPLES, MOMENTUM_DELAY=MOMENTUM_DELAY):
    """
    Filters instruments using INDEX and EXPIRY from config,
    matches by option_type, filters by premium range,
    and runs momentum checks in parallel.
    Returns: (instrument, ltp, lot_size) or (None, None, None)
    """
    config = CONFIG
    INDEX = config["index"].upper()
    EXPIRY = config["expiry"].strip()
    candidates = []

    # 🔍 Filter based on index + expiry + type
    for item in instruments_data:
        try:
            if item.get("underlying_symbol", "").upper() != INDEX:
                continue
            if item.get("instrument_type", "").upper() != option_type.upper():
                continue
            if item.get("expiry_date", "").strip() != EXPIRY:
                continue

            lot_size = int(item.get("lot_size") or item.get("lotsize") or 1)
            ltp = get_ltp_for_instrument(item, access_token, verbose=False)
            if ltp is None:
                continue
            if not (min_premium <= ltp <= max_premium):
                continue

            candidates.append({
                "instrument": item,
                "ltp": float(ltp),
                "lot_size": lot_size
            })

        except Exception as e:
            print(f"⚠️ Error while scanning: {e}")
            continue

    if not candidates:
        print(f"⚠️ No instruments for {INDEX} {EXPIRY} {option_type}")
        return None, None, None

    # ✅ Funds check (fallback = 1.2L if not available)
    try:
        margins = getattr(groww, "get_margins", lambda: {"availablecash": 130000})()
        available_cash = float(margins.get("availablecash", 130000))
    except Exception:
        available_cash = 130000

    affordable = []
    for c in candidates:
        qty = lots * c["lot_size"]
        est_cost = c["ltp"] * qty
        if available_cash <= 0 or est_cost <= available_cash * funds_buffer:
            affordable.append(c)

    if not affordable:
        print(f"⚠️ No affordable instruments for {INDEX} {EXPIRY} {option_type}")
        return None, None, None

    if option_type.upper() == "PE":
        momentum_threshold_pct = 0.30  # PEs move sharper
    else:
        momentum_threshold_pct = 0.25

    # ✅ Sort candidates closest to mid-premium
    mid = (min_premium + max_premium) / 2.0
    affordable.sort(key=lambda x: abs(x["ltp"] - mid))
    probe_list = affordable[:12]

    momentum_results = []

    def check_momentum(cand):
        mom_result, ticks = momentum_check_for_symbol(
            cand["instrument"],
            MOMENTUM_SAMPLES=MOMENTUM_SAMPLES,
            MOMENTUM_DELAY=MOMENTUM_DELAY
        )
        if mom_result and isinstance(mom_result, dict):
            slope_pct = mom_result.get("avg_change", 0)
            direction = mom_result.get("direction", "FLAT")
            consistency = mom_result.get("consistency", 0)

            # ✅ Apply momentum filter right here
            if abs(slope_pct) >= momentum_threshold_pct and consistency >= 70 and direction != "FLAT":
                return {
                    "instrument": cand["instrument"],
                    "ltp": cand["ltp"],
                    "lot_size": cand["lot_size"],
                    "slope_pct": slope_pct,
                    "direction": direction,
                    "consistency": consistency,
                    "ticks": ticks
                }
        return None

    print(f"⚙️ Checking momentum for top {len(probe_list)} {option_type} candidates...")

    with ThreadPoolExecutor(max_workers=min(len(probe_list), 8)) as executor:
        futures = {executor.submit(check_momentum, c): c for c in probe_list}
        for future in as_completed(futures):
            res = future.result()
            if res:
                momentum_results.append(res)

    if not momentum_results:
        print(f"⚠️ No strong momentum found for {option_type} (>{momentum_threshold_pct}%, consistency >70%)")
        # fallback: pick the one closest to mid-premium
        pick = probe_list[0]
        return pick["instrument"], pick["ltp"], pick["lot_size"]

    # ✅ Rank: strongest slope first, then consistency
    momentum_results.sort(key=lambda x: (x["slope_pct"], x["consistency"]), reverse=True)
    pick = momentum_results[0]

    print(f"🏆 Selected {option_type}: {pick['instrument']['trading_symbol']} "
          f"({pick['direction']} | {pick['slope_pct']:.2f}% | Consistency {pick['consistency']}%)")

    return pick["instrument"], pick["ltp"], pick["lot_size"]


# ----------------- Detect CE/PE (parallel) -----------------
def detect_option_type_parallel(index, expiry, min_p, max_p, lots, funds_buffer=0.9):
    print(f"🔍 Detecting best option between CE and PE for {index} {expiry}…")

    def worker(opt_type):
        print(f"➡️  Searching {opt_type} between {min_p}-{max_p}")
        inst, ltp, lot_size = find_option_by_premium_parallel(opt_type, min_p, max_p, lots, funds_buffer)
        mom = None
        if inst:
            print(f"📊 Running momentum check for {opt_type} ({inst.get('trading_symbol')})")
            mom, _ = momentum_check_for_symbol(inst, MOMENTUM_SAMPLES=MOMENTUM_SAMPLES, MOMENTUM_DELAY=MOMENTUM_DELAY)
            print(f"✅ Momentum for {opt_type}: {mom}")
        else:
            print(f"⚠️ No instrument found for {opt_type}")
        return opt_type, inst, ltp, lot_size, mom

    results = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(worker, t): t for t in ["CE", "PE"]}
        for future in as_completed(futures):
            opt_type, inst, ltp, lot_size, mom = future.result()
            results[opt_type] = {"instrument": inst, "ltp": ltp, "lot_size": lot_size, "momentum": mom}
            print(f"🧩 Finished {opt_type}: {inst.get('trading_symbol') if inst else 'None'}, momentum={mom}")

    print("🧮 Comparing CE vs PE momentum...")
    ce_mom = results.get("CE", {}).get("momentum")
    pe_mom = results.get("PE", {}).get("momentum")

    # Handle missing momentum
    if not ce_mom and not pe_mom:
        print("❌ No momentum data found for CE or PE.")
        return None
    if not ce_mom:
        r = results["PE"]
        return "PE", r["instrument"], r["ltp"], r["lot_size"]
    if not pe_mom:
        r = results["CE"]
        return "CE", r["instrument"], r["ltp"], r["lot_size"]

    ce_val = ce_mom["avg_change"]
    pe_val = pe_mom["avg_change"]

    print(f"📈 CE momentum: {ce_val:.3f}% ({ce_mom['direction']}, {ce_mom['consistency']}%)")
    print(f"📉 PE momentum: {pe_val:.3f}% ({pe_mom['direction']}, {pe_mom['consistency']}%)")

    # selection logic
    if abs(ce_val - pe_val) >= 0.25 and ce_val > pe_val and ce_val >= 0.10:
        print("✅ Selected CE (stronger momentum)")
        r = results["CE"]
        return "CE", r["instrument"], r["ltp"], r["lot_size"]
    if abs(pe_val - ce_val) >= 0.25 and pe_val > ce_val and pe_val >= 0.10:
        print("✅ Selected PE (stronger momentum)")
        r = results["PE"]
        return "PE", r["instrument"], r["ltp"], r["lot_size"]

    # fallback
    if ce_val >= pe_val:
        print("⚖️  Momentum similar — choosing CE fallback")
        r = results["CE"]
        return "CE", r["instrument"], r["ltp"], r["lot_size"]
    else:
        print("⚖️  Momentum similar — choosing PE fallback")
        r = results["PE"]
        return "PE", r["instrument"], r["ltp"], r["lot_size"]

def get_order_status(order_id, access_token):
    """
    Fetch the status of a Groww order (CASH, F&O, etc.)
    Works with official Groww REST API response format.
    """
    url = f"https://api.groww.in/v1/order/status/{order_id}?segment=FNO"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()  # raises for non-200 responses

        data = resp.json()  # Proper JSON response from Groww
        print("🔍 Order status response:", data)

        # ✅ Extract status cleanly
        payload = data.get("payload", {})
        status = payload.get("order_status")

        return status

    except requests.exceptions.JSONDecodeError:
        print("⚠️ Error: Non-JSON response received.")
        print("Response text:", resp.text)
        return None

    except Exception as e:
        print(f"⚠️ Error fetching order status: {e}")
        return None



def wait_for_order_status(order_id, access_token, order_type="BUY"):
    """
    Wait indefinitely until a Groww order reaches EXECUTED / COMPLETED / DELIVERY_AWAITED.
    Returns final status (string).
    """
    print(f"🔎 Waiting for {order_type} order ({order_id}) to finish...")

    while True:
        status = get_order_status(order_id, access_token)
        print(f"🕒 {order_type} status: {status}")

        if status in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
            print(f"✅ {order_type} order executed successfully.")
            send_telegram(f"✅ {order_type} order executed successfully.")
            return status

        elif status in ["FAILED", "REJECTED", "CANCELLED"]:
            print(f"❌ {order_type} order failed with status {status}.")
            send_telegram(f"❌ {order_type} order failed ({status}).")
            return status

        # ⚡ Fast polling for buy orders (0.2s), slower for sell (1s)
        time.sleep(0.2 if order_type == "BUY" else 1.0)


import requests

def get_order_executed_price(order_id, access_token, segment="FNO"):
    """
    Fetch executed trades for a given Groww order_id and return average price & total quantity.
    """
    try:
        url = f"https://api.groww.in/v1/order/trades/{order_id}?segment={segment}&page=0&page_size=50"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }

        print(f"\n📦 Fetching trade details for order: {order_id}")
        response = requests.get(url, headers=headers)
        data = response.json()

        if data.get("status") != "SUCCESS":
            print("⚠️ Failed to fetch trade info:", data)
            return None, None

        trades = data.get("payload", {}).get("trade_list", [])
        if not trades:
            print("⚠️ No trades found for order ID.")
            return None, None

        # Compute average price & total quantity
        total_qty = sum(t["quantity"] for t in trades)
        total_value = sum(t["price"] * t["quantity"] for t in trades)
        avg_price = round(total_value / total_qty, 2)

        symbol = trades[0]["trading_symbol"]
        side = trades[0]["transaction_type"]

        print(f"✅ {side} {symbol} | Total Qty={total_qty} | Avg Price=₹{avg_price}")
        return avg_price, total_qty

    except Exception as e:
        print("❌ Error fetching order trades:", e)
        return None, None



# ----------------- Place CP order workflow (mirrors AngelOne logic) -----------------
def place_quick_order(command):
    """Quick mode: Buy at market and instantly set limit sell at +1.5 points"""
    global buy_status, instruments_data, CONFIG
    
    parsed_command = parse_cp_command(command)
    if not parsed_command:
        print("❌ Invalid command format. Expected: <lots> <TRADING_SYMBOL>")
        return

    lots = parsed_command["lots"]
    trading_symbol_str = parsed_command["trading_symbol_str"]

    parsed_symbol_details = parse_trading_symbol_string(trading_symbol_str)
    if not parsed_symbol_details:
        return

    requested_index = parsed_symbol_details["underlying"]
    current_index = CONFIG.get("index", "").upper()
    
    if requested_index != current_index:
        print(f"🔄 Detected index change: {current_index} → {requested_index}")
        print(f"📦 Reloading instruments for {requested_index}...")
        CONFIG["index"] = requested_index
        CONFIG["expiry"] = parsed_symbol_details["expiry_date"]
        CONFIG["spot"] = get_index_spot_price(requested_index, access_token)
        instruments_data = load_instruments_from_json()
        print(f"✅ Switched to {requested_index} | Spot: {CONFIG['spot']}")

    instrument = find_instrument_by_details(
        parsed_symbol_details["underlying"],
        parsed_symbol_details["expiry_date"],
        parsed_symbol_details["strike"],
        parsed_symbol_details["opt_type"],
        instruments_data
    )
    if not instrument:
        return

    lot_size = int(instrument.get("lot_size") or instrument.get("lotsize") or 1)
    quantity = lots * lot_size

    # Fetch LTP
    ltp_before = get_ltp_for_instrument(instrument, access_token, verbose=True, delay=0)
    if ltp_before is None:
        print("❌ Could not fetch LTP before placing order.")
        return

    entry_price = round(float(ltp_before), 2)
    target_price = round(entry_price + 1.5, 2)  # +1.5 points target
    print(f"⚡ QUICK MODE: Entry={entry_price} | Target={target_price} (+1.5)")

    # Place BUY order
    try:
        order_resp = place_market_order_groww(instrument, quantity, transaction_type="BUY", product="MIS")
        order_id = order_resp.get("payload", {}).get("groww_order_id") or order_resp.get("groww_order_id")
        print(f"✅ Buy Order placed:", order_resp, {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
        send_telegram(f"⚡ QUICK BUY: {entry_price} | Target: {target_price} | {instrument.get('internal_trading_symbol')} | qty={quantity}")
    except Exception as e:
        print(f"❌ Buy order failed: {e}")
        send_telegram(f"❌ Buy order failed: {e}")
        return

    # Wait for BUY execution (only if VALIDATE_ORDERS is True)
    if CONFIG.get("VALIDATE_ORDERS", True):
        if order_id:
            buy_status = wait_for_order_status(order_id, access_token, "BUY")
            if buy_status not in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                print(f"⚠️ BUY failed: {buy_status}")
                send_telegram(f"⚠️ BUY failed: {buy_status}")
                return
            
            avg_price, executed_qty = get_order_executed_price(order_id, access_token)
            if not avg_price or not executed_qty:
                print(f"❌ Could not get executed price/qty for BUY order {order_id}.")
                return
            quantity = executed_qty
            target_price = round(avg_price + 1.5, 2)  # Recalculate target based on actual buy price
            print(f"🎯 BUY EXECUTED @ ₹{avg_price} | New Target: ₹{target_price}")
        else:
            print("❌ No BUY order ID received.")
            return
    else:
        # Testing mode: Use entry price estimate, skip validation
        avg_price = entry_price
        target_price = round(entry_price + 1.5, 2)
        print(f"⚠️ Testing mode: Using entry price estimate ₹{avg_price}, target ₹{target_price}")

    # Fetch ATR for dynamic SL
    atr = CONFIG.get("HARD_SL_POINTS", 5)  # Default SL
    try:
        from threading import Thread
        import queue
        
        result_queue = queue.Queue()
        
        def fetch_technicals():
            try:
                techs = get_technicals(instrument['groww_symbol'], groww, segment="FNO", instrument=instrument)
                result_queue.put(techs)
            except Exception as e:
                result_queue.put(None)
        
        thread = Thread(target=fetch_technicals, daemon=True)
        thread.start()
        thread.join(timeout=3)  # Wait max 3 seconds
        
        if not result_queue.empty():
            techs = result_queue.get()
            if techs and techs.get("atr"):
                atr = techs["atr"]
                print(f"✅ ATR fetched: {atr:.2f}")
    except:
        pass
    
    # Calculate SL based on ATR
    sl_price = round(avg_price - (1.5 * atr), 2)
    print(f"🛡️ Dynamic SL: ₹{sl_price} (based on 1.5x ATR={atr:.2f})")

    # Place LIMIT SELL order instantly at target
    try:
        sell_resp = place_limit_order_groww(instrument, quantity, target_price, transaction_type="SELL", product="MIS")
        sell_order_id = sell_resp.get("payload", {}).get("groww_order_id") or sell_resp.get("groww_order_id")
        print(f"✅ LIMIT SELL placed @ ₹{target_price}:", sell_resp)
        send_telegram(f"🎯 LIMIT SELL @ ₹{target_price} | Order ID: {sell_order_id}")
        
        # Validate SELL order placement if flag is true
        if CONFIG.get("VALIDATE_ORDERS", True) and sell_order_id:
            print(f"🔎 Validating SELL order placement...")
            initial_status = get_order_status(sell_order_id, access_token)
            print(f"📋 SELL order status: {initial_status}")
            if initial_status in ["FAILED", "REJECTED", "CANCELLED"]:
                print(f"❌ SELL order placement failed: {initial_status}")
                send_telegram(f"❌ SELL order failed: {initial_status}")
                return
    except Exception as e:
        print(f"❌ Limit SELL order failed: {e}")
        send_telegram(f"❌ Limit SELL failed: {e}")
        return

    # Monitor price until target or SL is hit
    print(f"⏳ Monitoring price... Target: ₹{target_price} | SL: ₹{sl_price}")
    start_time = time.time()
    max_monitor_time = 3600  # 1 hour max
    
    while True:
        if time.time() - start_time >= max_monitor_time:
            print("⏰ Max monitoring time reached (1 hour)")
            break
            
        try:
            ltp = get_ltp_for_instrument(instrument, access_token, verbose=False, delay=0)
            if ltp is None:
                time.sleep(1)
                continue
            ltp = float(ltp)
            
            # Check if target hit
            if ltp >= target_price:
                print(f"🎯 TARGET HIT! LTP: ₹{ltp}")
                send_telegram(f"🎯 TARGET HIT @ ₹{ltp}")
                play_sound_async(SOUND_PROFIT)
                
                # Check SELL order status
                if CONFIG.get("VALIDATE_ORDERS", True) and sell_order_id:
                    final_status = wait_for_order_status(sell_order_id, access_token, "SELL")
                    if final_status in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                        sell_price, sold_qty = get_order_executed_price(sell_order_id, access_token)
                        if sell_price and sold_qty:
                            profit = (sell_price - avg_price) * sold_qty
                            print(f"💰 PROFIT: ₹{profit:.2f} (Buy @ ₹{avg_price}, Sell @ ₹{sell_price})")
                            send_telegram(f"💰 PROFIT: ₹{profit:.2f}")
                            log_trade_to_excel(instrument.get('internal_trading_symbol') or instrument.get('trading_symbol'), avg_price, sell_price, sold_qty, profit)
                        else:
                            profit = (ltp - avg_price) * quantity
                            print(f"💰 Estimated PROFIT: ₹{profit:.2f}")
                            log_trade_to_excel(instrument.get('internal_trading_symbol') or instrument.get('trading_symbol'), avg_price, ltp, quantity, profit)
                else:
                    profit = (ltp - avg_price) * quantity
                    print(f"💰 Estimated PROFIT: ₹{profit:.2f}")
                    log_trade_to_excel(instrument.get('internal_trading_symbol') or instrument.get('trading_symbol'), avg_price, ltp, quantity, profit)
                break
                
            # Check if SL hit
            if ltp <= sl_price:
                print(f"🛑 SL HIT! LTP: ₹{ltp}")
                send_telegram(f"🛑 SL HIT @ ₹{ltp}")
                play_sound_async(SOUND_SL)
                
                # Cancel pending target order and place market sell
                try:
                    print(f"🔄 Cancelling target order and placing market SELL...")
                    
                    # Cancel the pending limit sell order
                    if sell_order_id:
                        cancel_success = cancel_order_groww(sell_order_id, access_token)
                        if cancel_success:
                            print(f"✅ Target order {sell_order_id} cancelled successfully")
                            send_telegram(f"✅ Target order cancelled")
                        else:
                            print(f"⚠️ Could not cancel target order {sell_order_id}, it may have already executed")
                    
                    # Place market sell
                    market_sell_resp = place_market_order_groww(instrument, quantity, "SELL", "MIS")
                    market_sell_id = market_sell_resp.get("payload", {}).get("groww_order_id") or market_sell_resp.get("groww_order_id")
                    print(f"✅ Market SELL placed: {market_sell_id}")
                    
                    if CONFIG.get("VALIDATE_ORDERS", True) and market_sell_id:
                        final_status = wait_for_order_status(market_sell_id, access_token, "SELL")
                        if final_status in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                            sell_price, sold_qty = get_order_executed_price(market_sell_id, access_token)
                            if sell_price and sold_qty:
                                loss = (sell_price - avg_price) * sold_qty
                                print(f"💸 LOSS: ₹{loss:.2f} (Buy @ ₹{avg_price}, Sell @ ₹{sell_price})")
                                send_telegram(f"💸 LOSS: ₹{loss:.2f}")
                                log_trade_to_excel(instrument.get('internal_trading_symbol') or instrument.get('trading_symbol'), avg_price, sell_price, sold_qty, loss)
                    else:
                        loss = (ltp - avg_price) * quantity
                        print(f"💸 Estimated LOSS: ₹{loss:.2f}")
                        log_trade_to_excel(instrument.get('internal_trading_symbol') or instrument.get('trading_symbol'), avg_price, ltp, quantity, loss)
                except Exception as e:
                    print(f"❌ SL execution failed: {e}")
                break
            
            time.sleep(1)  # Poll every second
            
        except Exception as e:
            print(f"⚠️ Monitoring error: {e}")
            time.sleep(2)
    
    print("✅ Quick order complete. Ready for next command.")


def place_cp_order(command, is_auto=False):
    global buy_status, instruments_data, CONFIG
    
    # Start timing for manual mode
    command_start_time = datetime.now()
    print(f"[{command_start_time.strftime('%H:%M:%S.%f')[:-3]}] ⏱️  Command entered: {command}")
    
    if is_auto:
        print("Auto mode not supported in this bot, only manual mode")
    else:
        parsed_command = parse_cp_command(command)
        if not parsed_command:
            print("❌ Invalid command format. Expected: <lots> <TRADING_SYMBOL>")
            return

        lots = parsed_command["lots"]
        trading_symbol_str = parsed_command["trading_symbol_str"]
        
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 🔍 Parsing symbol: {trading_symbol_str}")

        parsed_symbol_details = parse_trading_symbol_string(trading_symbol_str)
        if not parsed_symbol_details:
            return # Error message already printed by parse_trading_symbol_string

        # 🔄 Auto-detect if index changed and reload instruments if needed
        requested_index = parsed_symbol_details["underlying"]
        current_index = CONFIG.get("index", "").upper()
        
        if requested_index != current_index:
            print(f"🔄 Detected index change: {current_index} → {requested_index}")
            print(f"📦 Reloading instruments for {requested_index}...")
            
            # Update CONFIG
            CONFIG["index"] = requested_index
            CONFIG["expiry"] = parsed_symbol_details["expiry_date"]
            CONFIG["spot"] = get_index_spot_price(requested_index, access_token)
            
            # Reload instruments with new index
            instruments_data = load_instruments_from_json()
            print(f"✅ Switched to {requested_index} | Spot: {CONFIG['spot']}")

        instrument = find_instrument_by_details(
            parsed_symbol_details["underlying"],
            parsed_symbol_details["expiry_date"],
            parsed_symbol_details["strike"],
            parsed_symbol_details["opt_type"],
            instruments_data
        )
        if not instrument:
            return # Error message already printed by find_instrument_by_details

        lot_size = int(instrument.get("lot_size") or instrument.get("lotsize") or 1)
        quantity = lots * lot_size
        
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 💰 Fetching LTP...")
        ltp_before = get_ltp_for_instrument(instrument, access_token, verbose=True, delay=0)
        if ltp_before is None:
            print("❌ Could not fetch LTP before placing order.")
            return

        entry_price = round(float(ltp_before), 2)
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 💵 Entry price: ₹{entry_price}")

        # ⚡ PLACE BUY ORDER IMMEDIATELY (no delays before this)
        try:
            order_start = datetime.now()
            print(f"[{order_start.strftime('%H:%M:%S.%f')[:-3]}] 🔄 Placing BUY order for {quantity} units...")
            order_resp = place_market_order_groww(instrument, quantity, transaction_type="BUY", product="MIS")
            order_id = order_resp.get("payload", {}).get("groww_order_id") or order_resp.get("groww_order_id")
            order_placed = datetime.now()
            order_duration = (order_placed - order_start).total_seconds()
            print(f"[{order_placed.strftime('%H:%M:%S.%f')[:-3]}] ✅ BUY Order placed: {order_id} (took {order_duration:.2f}s)")
            send_telegram(f"entry price: {entry_price} | {instrument.get('internal_trading_symbol')} | qty={quantity}")
        except Exception as e:
            print(f"❌ Buy order failed: {e}")
            send_telegram(f"❌ Buy order failed: {e}")
            return

        # Fetch technicals for ATR in background (non-blocking)
        atr = CONFIG["HARD_SL_POINTS"]  # Default fallback
        try:
            from threading import Thread
            import queue
            
            result_queue = queue.Queue()
            
            def fetch_technicals():
                try:
                    techs = get_technicals(instrument['groww_symbol'], groww, segment="FNO", instrument=instrument)
                    result_queue.put(techs)
                except Exception as e:
                    result_queue.put(None)
            
            # Start background fetch (don't wait)
            thread = Thread(target=fetch_technicals, daemon=True)
            thread.start()
        except Exception as e:
            pass

        # STATUS VALIDATION
        # ✅ LIVE TRADING: Wait until BUY order is EXECUTED (controlled by VALIDATE_ORDERS)
        if CONFIG.get("VALIDATE_ORDERS", True):
            if order_id:
                validation_start = datetime.now()
                buy_status = wait_for_order_status(order_id, access_token, "BUY")
                if buy_status not in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] ⚠️ Skipping trade monitoring due to BUY status: {buy_status}")
                    send_telegram(f"⚠️ BUY failed: {buy_status}")
                    return
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] ❌ No BUY order ID received. Aborting trade.")
                send_telegram("❌ No BUY order ID received")
                return
            
            # Fetch actual executed price and quantity
            avg_price, executed_qty = get_order_executed_price(order_id, access_token)
            if not avg_price or not executed_qty:
                print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] ❌ Could not get executed price/qty for BUY order {order_id}. Aborting.")
                return
            quantity = executed_qty # Use the actual executed quantity
            validation_end = datetime.now()
            validation_duration = (validation_end - validation_start).total_seconds()
            total_duration = (validation_end - command_start_time).total_seconds()
            
            print(f"[{validation_end.strftime('%H:%M:%S.%f')[:-3]}] 🎯 Executed avg price: ₹{avg_price}, Qty: {quantity}")
            print(f"[{validation_end.strftime('%H:%M:%S.%f')[:-3]}] ⏱️  Total time: {total_duration:.2f}s (Order: {order_duration:.2f}s, Validation: {validation_duration:.2f}s)")
            send_telegram(f"🎯 BUY EXECUTED @ ₹{avg_price} | Qty={quantity}")
        else:
            # Testing mode: Use entry price estimate
            avg_price = entry_price
            executed_qty = quantity
            print(f"⚠️ Testing mode: Using entry price estimate ₹{avg_price}")

        # Check if ATR fetch completed in background
        try:
            if not result_queue.empty():
                techs = result_queue.get()
                if techs and techs.get("atr"):
                    atr = techs["atr"]
                    print(f"✅ ATR fetched: {atr:.2f}")
        except:
            pass

        highest_price = avg_price
        start_time = time.time()
        last_trail_exit = None  # Track last printed trail exit to avoid spam
        last_heartbeat = time.time()  # Track heartbeat for alive signal

        trail_start = CONFIG["TRAIL_START_PROFIT"]
        trail_step = CONFIG["TRAIL_STEP"]
        poll = CONFIG["POLL_INTERVAL"]
        max_time = CONFIG["MAX_TRAIL_TIME"]
        hard_sl = entry_price - (1.5 * atr)  # Dynamic SL based on 1.5 * ATR

        print(f"📈 Trailing started... Dynamic SL: {hard_sl:.2f} (based on ATR: {atr:.2f})")
        try:
            send_telegram(f"📈 Trailing started... Dynamic SL: {hard_sl:.2f}")
        except:
            pass

        # ⚡ CRITICAL: This loop is optimized for ZERO-DELAY sell execution
        # - LTP fetch: delay=0 (no artificial delays)
        # - Poll interval: 0.15s (fast response)
        # - Telegram: wrapped in try-except (won't block)
        # - Sell order: placed IMMEDIATELY when condition met (no pre-checks)
        while True:
            # Heartbeat every 30 seconds to show script is alive
            if time.time() - last_heartbeat >= 30:
                print(f"💓 Monitoring... LTP last seen: ₹{ltp if 'ltp' in locals() else 'fetching...'}")
                last_heartbeat = time.time()
            
            try:
                # ⚡ Zero delay for fastest trailing response
                ltp = get_ltp_for_instrument(instrument, access_token, verbose=False, delay=0)
            except Exception as e:
                print(f"⚠️ LTP fetch error (retrying): {e}")
                time.sleep(poll)
                continue
                
            if ltp is None:
                print("⚠️ LTP is None (retrying...)")
                time.sleep(poll)
                continue

            ltp = float(ltp)
            sell_reason = None

            # 1. Check exit conditions
            if ltp <= hard_sl:
                sell_reason = f"🛑 DYNAMIC SL HIT @ {ltp}"
            elif time.time() - start_time >= max_time:
                sell_reason = "⏰ Max trail time reached — exiting"
            else:
                if ltp > highest_price:
                    highest_price = ltp
                    print(f"🔼 New High: ₹{highest_price}")
                    # Non-blocking notification (won't delay trading)
                    try:
                        send_telegram(f"🔼 New High: ₹{highest_price}")
                    except:
                        pass

                if highest_price >= avg_price + trail_start:
                    trail_exit = round_to_nearest_5_paise(highest_price - trail_step)
                    
                    # Only print if trail exit changed (avoid spam)
                    if trail_exit != last_trail_exit:
                        print(f"📉 Trail Active | LTP={ltp} | High={highest_price} | Exit={trail_exit}")
                        # Non-blocking notification
                        try:
                            send_telegram(f"📉 Trail Active | LTP={ltp} | High={highest_price} | Exit={trail_exit}")
                        except:
                            pass
                        last_trail_exit = trail_exit
                    
                    if ltp <= trail_exit:
                        sell_reason = f"🔻 Trailing HIT @ {ltp}"
                        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
                        sell_price = ltp
                        sold_qty = quantity

            # 2. If an exit condition is met, place sell order IMMEDIATELY
            if sell_reason:
                print(sell_reason)
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
                
                # 🔊 Play sound immediately on exit trigger
                if "SL HIT" in sell_reason or "DYNAMIC SL" in sell_reason:
                    play_sound_async(SOUND_SL)
                elif "Trailing HIT" in sell_reason or "PROFIT" in sell_reason:
                    play_sound_async(SOUND_PROFIT)
                else:
                    play_sound_async(SOUND_SL)  # Default to SL sound

                try:
                    # ⚡ IMMEDIATE SELL - No delays before this
                    print(f"🔄 Placing SELL order for {quantity} units @ market price...")
                    order_resp = place_market_order_groww(instrument, quantity, "SELL", "MIS")
                    sell_order_id = order_resp.get("payload", {}).get("groww_order_id") or order_resp.get("groww_order_id")
                    print(f"✅ SELL Order placed: {order_resp}")
                    print(f"🆔 SELL Order ID: {sell_order_id} @ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
                    
                    # Send notification AFTER order is placed (non-blocking)
                    try:
                        send_telegram(f"{sell_reason}\n✅ SELL Order: {sell_order_id}")
                    except:
                        pass
                    
                    # ✅ LIVE TRADING: Wait for SELL order to execute (controlled by VALIDATE_ORDERS)
                    if CONFIG.get("VALIDATE_ORDERS", True) and sell_order_id:
                        sell_status = wait_for_order_status(sell_order_id, access_token, "SELL")
                        if sell_status in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                            # Fetch actual executed price
                            sell_price, sold_qty = get_order_executed_price(sell_order_id, access_token)
                            if sell_price and sold_qty:
                                profit = (sell_price - avg_price) * sold_qty
                                print(f"💰 PROFIT: ₹{profit:.2f} (Buy @ ₹{avg_price}, Sell @ ₹{sell_price})")
                                send_telegram(f"💰 PROFIT: ₹{profit:.2f}")
                                play_sound_async(SOUND_PROFIT if profit > 0 else SOUND_SL)
                                log_trade_to_excel(
                                    instrument.get("internal_trading_symbol"),
                                    avg_price, sell_price, sold_qty, profit
                                )
                                # Display account summary
                                display_account_summary(access_token)
                            else:
                                print("⚠️ Could not get executed SELL price. Logging with LTP.")
                                profit = (ltp - avg_price) * quantity
                                play_sound_async(SOUND_PROFIT if profit > 0 else SOUND_SL)
                                log_trade_to_excel(
                                    instrument.get("internal_trading_symbol"),
                                    avg_price, ltp, quantity, profit
                                )
                                # Display account summary
                                display_account_summary(access_token)
                        else:
                            print(f"⚠️ SELL order failed with status: {sell_status}")
                            send_telegram(f"⚠️ SELL failed: {sell_status}")
                            play_sound_async(SOUND_SL)  # Play sound on failure
                    elif not CONFIG.get("VALIDATE_ORDERS", True):
                        # Testing mode: Use LTP estimate
                        sell_price = ltp
                        sold_qty = quantity
                        profit = (sell_price - avg_price) * sold_qty
                        print(f"⚠️ Testing mode: Estimated profit ₹{profit:.2f}")
                        play_sound_async(SOUND_PROFIT if profit > 0 else SOUND_SL)
                        log_trade_to_excel(
                            instrument.get("internal_trading_symbol"),
                            avg_price, sell_price, sold_qty, profit
                        )
                        # Display account summary
                        display_account_summary(access_token)
                    else:
                        print("⚠️ No SELL order ID received.")
                        
                except Exception as e:
                    print(f"❌ SELL order placement failed: {e}")
                    send_telegram(f"❌ SELL failed: {e}")

                print("✅ Trade cycle completed. Ready for next trade.")
                break  # Exit the trailing loop

            time.sleep(poll)


# ----------------- Directional Mode -----------------

def directional_mode():
    """
    Directional mode: User types premium value and direction
    Example: '150 c' = Find Call option with premium closest to ₹150
    Example: '200 p' = Find Put option with premium closest to ₹200
    """
    cfg = CONFIG
    dir_cfg = cfg["DIRECTIONAL_MODE"]
    
    print("\n" + "="*60)
    print("📍 DIRECTIONAL MODE")
    print("="*60)
    print(f"Index: {cfg['index']} | Expiry: {cfg['expiry']}")
    print(f"Lots: {cfg['lots']}")
    print("\n💡 Usage: <premium> <direction>")
    print("   Example: 150 c  → Find Call option near ₹150")
    print("   Example: 200 p  → Find Put option near ₹200")
    print("="*60)
    
    while True:
        user_input = input("\nEnter command (e.g., '150 c') or 'back' to menu: ").strip().lower()
        
        if user_input == "back":
            return
        
        # Parse input: expect "<premium> <c/p>"
        parts = user_input.split()
        if len(parts) != 2:
            print("⚠️ Invalid format. Use: <premium> <c/p>  (e.g., '150 c')")
            continue
        
        try:
            target_premium = float(parts[0])
        except ValueError:
            print("⚠️ Invalid premium value. Use a number (e.g., '150 c')")
            continue
        
        if parts[1] not in ["c", "p"]:
            print("⚠️ Invalid direction. Use 'c' for Call or 'p' for Put")
            continue
        
        option_type = "CE" if parts[1] == "c" else "PE"
        direction_name = "CALL (Bullish)" if parts[1] == "c" else "PUT (Bearish)"
        
        # Start timing
        start_time = datetime.now()
        print(f"\n[{start_time.strftime('%H:%M:%S.%f')[:-3]}] 🎯 Direction: {direction_name} | Target Premium: ₹{target_premium:.2f}")
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 🔍 Searching for matching option...")
        
        try:
            # Use spot price from CONFIG (already fetched at startup)
            spot_price = cfg["spot"]
            if not spot_price or spot_price <= 0:
                # Fallback: try to fetch fresh spot price
                spot_price = get_index_spot_price(cfg["index"], access_token)
                if not spot_price:
                    print("❌ Could not fetch spot price. Try again.")
                    continue
            
            print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 📊 {cfg['index']} Spot: ₹{spot_price:.2f}")
            
            # Find all matching options for this type and expiry
            matching_options = []
            for instrument in instruments_data:
                if (instrument.get("underlying_symbol", "").upper() == cfg["index"].upper() and
                    instrument.get("expiry_date", "") == cfg["expiry"] and
                    instrument.get("instrument_type", "").upper() == option_type):
                    matching_options.append(instrument)
            
            if not matching_options:
                print(f"❌ No {option_type} options found for {cfg['index']} expiry {cfg['expiry']}")
                continue
            
            # Sort options by distance from ATM to scan smartly (ATM first, then expanding)
            step = 100 if "SENSEX" in cfg["index"].upper() else 50
            atm_strike = round(spot_price / step) * step
            matching_options.sort(key=lambda x: abs(float(x.get("strike_price", 0)) - atm_strike))
            
            scan_start = datetime.now()
            print(f"[{scan_start.strftime('%H:%M:%S.%f')[:-3]}] 📋 Scanning {len(matching_options)} options starting from ATM...")
            
            # Parallel LTP fetching for speed (fetch 5 options at once)
            max_checks = 30  # Maximum options to check
            batch_size = 5  # Fetch 5 options in parallel
            
            best_option = None
            best_diff = float('inf')
            best_ltp = None
            checked_count = 0
            
            # Helper function to fetch LTP with option data
            def fetch_ltp_with_option(opt):
                try:
                    ltp = get_ltp_for_instrument(opt, access_token, verbose=False, delay=0)
                    if ltp and ltp > 0:
                        return (opt, float(ltp), abs(float(ltp) - target_premium))
                except:
                    pass
                return None
            
            # Process options in batches
            for batch_start in range(0, min(len(matching_options), max_checks), batch_size):
                batch_end = min(batch_start + batch_size, len(matching_options), max_checks)
                batch = matching_options[batch_start:batch_end]
                
                # Fetch LTPs in parallel
                with ThreadPoolExecutor(max_workers=batch_size) as executor:
                    futures = [executor.submit(fetch_ltp_with_option, opt) for opt in batch]
                    
                    for future in as_completed(futures):
                        result = future.result()
                        if result:
                            opt, ltp, diff = result
                            checked_count += 1
                            
                            # Update best if closer
                            if diff < best_diff:
                                best_diff = diff
                                best_option = opt
                                best_ltp = ltp
                            
                            # Early exit if found very close match (within ₹3)
                            if diff <= 3.0:
                                print(f"✅ Found close match after checking {checked_count} options (diff: ₹{diff:.2f})")
                                break
                
                # Exit if found good match
                if best_option and best_diff <= 3.0:
                    break
            
            if best_option:
                print(f"✅ Best match found after checking {checked_count} options")
            
            if not best_option or not best_ltp:
                print("❌ Could not find any options with valid prices. Try again.")
                continue
            
            selected_option = best_option
            ltp = best_ltp
            
            selection_time = datetime.now()
            scan_duration = (selection_time - scan_start).total_seconds()
            
            # Display selected option
            symbol = selected_option.get("internal_trading_symbol") or selected_option.get("trading_symbol")
            strike = float(selected_option.get("strike_price", 0))
            lot_size = int(selected_option.get("lot_size", 25))
            quantity = cfg["lots"] * lot_size
            total_value = ltp * quantity
            
            # Determine ITM/OTM/ATM
            step = 100 if "SENSEX" in cfg["index"].upper() else 50
            atm_strike = round(spot_price / step) * step
            if option_type == "CE":
                position = "ITM" if strike < atm_strike else "OTM" if strike > atm_strike else "ATM"
            else:
                position = "ITM" if strike > atm_strike else "OTM" if strike < atm_strike else "ATM"
            
            print(f"\n[{selection_time.strftime('%H:%M:%S.%f')[:-3]}] ⏱️  Scan completed in {scan_duration:.2f}s")
            print("\n" + "="*60)
            print("✅ OPTION SELECTED")
            print("="*60)
            print(f"Symbol: {symbol}")
            print(f"Strike: {strike} {option_type} ({position})")
            print(f"Premium: ₹{ltp:.2f} (Target: ₹{target_premium:.2f}, Diff: ₹{abs(ltp - target_premium):.2f})")
            print(f"Lots: {cfg['lots']} × {lot_size} = {quantity} units")
            print(f"Total Value: ₹{total_value:,.2f}")
            print(f"SL: ₹{ltp - cfg['HARD_SL_POINTS']:.2f} ({cfg['HARD_SL_POINTS']} points)")
            print(f"Trail: Activates at ₹{ltp + cfg['TRAIL_START_PROFIT']:.2f}")
            print("="*60)
            
            # Auto-execute trade directly without command parsing
            print(f"\n[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 🚀 Executing trade automatically...")
            
            try:
                # Place BUY order
                order_start = datetime.now()
                print(f"[{order_start.strftime('%H:%M:%S.%f')[:-3]}] 🔄 Placing BUY order for {quantity} units @ market price...")
                buy_order_resp = place_market_order_groww(selected_option, quantity, "BUY", "MIS")
                buy_order_id = buy_order_resp.get("payload", {}).get("groww_order_id") or buy_order_resp.get("groww_order_id")
                
                if not buy_order_id:
                    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] ❌ Failed to get BUY order ID")
                    continue
                
                order_placed = datetime.now()
                order_duration = (order_placed - order_start).total_seconds()
                print(f"[{order_placed.strftime('%H:%M:%S.%f')[:-3]}] ✅ BUY Order placed: {buy_order_id} (took {order_duration:.2f}s)")
                send_telegram(f"✅ BUY {symbol} @ ₹{ltp:.2f} | Qty: {quantity}")
                
                # Wait for BUY order execution
                if cfg.get("VALIDATE_ORDERS", True):
                    validation_start = datetime.now()
                    buy_status = wait_for_order_status(buy_order_id, access_token, "BUY")
                    if buy_status not in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] ❌ BUY order failed with status: {buy_status}")
                        continue
                    
                    # Get executed price
                    avg_price, bought_qty = get_order_executed_price(buy_order_id, access_token)
                    if not avg_price:
                        avg_price = ltp
                        bought_qty = quantity
                    
                    validation_end = datetime.now()
                    validation_duration = (validation_end - validation_start).total_seconds()
                else:
                    avg_price = ltp
                    bought_qty = quantity
                    validation_duration = 0
                
                execution_time = datetime.now()
                total_duration = (execution_time - start_time).total_seconds()
                print(f"[{execution_time.strftime('%H:%M:%S.%f')[:-3]}] ✅ BUY executed @ ₹{avg_price:.2f} | Qty: {bought_qty}")
                print(f"[{execution_time.strftime('%H:%M:%S.%f')[:-3]}] ⏱️  Total time: {total_duration:.2f}s (Scan: {scan_duration:.2f}s, Order: {order_duration:.2f}s, Validation: {validation_duration:.2f}s)")
                
                # Start trailing stop monitoring (inline logic from place_cp_order)
                trail_start_time = datetime.now()
                print(f"\n[{trail_start_time.strftime('%H:%M:%S.%f')[:-3]}] 🔄 Starting trailing stop monitoring...")
                
                # Calculate dynamic SL using ATR
                hard_sl_points = cfg["HARD_SL_POINTS"]
                hard_sl = round_to_nearest_5_paise(avg_price - hard_sl_points)
                
                print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 📈 Trailing started... Hard SL: ₹{hard_sl:.2f}")
                send_telegram(f"📈 Trailing started | Entry: ₹{avg_price:.2f} | SL: ₹{hard_sl:.2f}")
                
                # Trailing parameters
                trail_start = cfg["TRAIL_START_PROFIT"]
                trail_step = cfg["TRAIL_STEP"]
                poll = cfg["POLL_INTERVAL"]
                max_time = cfg["MAX_TRAIL_TIME"]
                
                highest_price = avg_price
                start_time = time.time()
                last_heartbeat = time.time()
                last_trail_exit = None
                
                while True:
                    # Heartbeat every 30 seconds
                    if time.time() - last_heartbeat > 30:
                        print(f"💓 Monitoring... LTP last seen: ₹{ltp if 'ltp' in locals() else 'fetching...'}")
                        last_heartbeat = time.time()
                    
                    try:
                        ltp = get_ltp_for_instrument(selected_option, access_token, verbose=False, delay=0)
                    except Exception as e:
                        print(f"⚠️ LTP fetch error (retrying): {e}")
                        time.sleep(poll)
                        continue
                    
                    if ltp is None:
                        print("⚠️ LTP is None (retrying...)")
                        time.sleep(poll)
                        continue
                    
                    ltp = float(ltp)
                    sell_reason = None
                    
                    # Check exit conditions
                    if ltp <= hard_sl:
                        sell_reason = f"🛑 DYNAMIC SL HIT @ {ltp}"
                    elif time.time() - start_time >= max_time:
                        sell_reason = "⏰ Max trail time reached"
                    else:
                        if ltp > highest_price:
                            highest_price = ltp
                            print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 🔼 New High: ₹{highest_price}")
                        
                        if highest_price >= avg_price + trail_start:
                            trail_exit = round_to_nearest_5_paise(highest_price - trail_step)
                            
                            if trail_exit != last_trail_exit:
                                print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 📉 Trail Active | LTP={ltp} | High={highest_price} | Exit={trail_exit}")
                                last_trail_exit = trail_exit
                            
                            if ltp <= trail_exit:
                                sell_reason = f"🔻 Trailing HIT @ {ltp}"
                    
                    # If exit condition met, place SELL order
                    if sell_reason:
                        exit_time = datetime.now()
                        monitoring_duration = (exit_time - trail_start_time).total_seconds()
                        print(f"[{exit_time.strftime('%H:%M:%S.%f')[:-3]}] {sell_reason}")
                        print(f"[{exit_time.strftime('%H:%M:%S.%f')[:-3]}] ⏱️  Monitored for {monitoring_duration:.2f}s")
                        if "SL HIT" in sell_reason or "DYNAMIC SL" in sell_reason:
                            play_sound_async(SOUND_SL)
                        else:
                            play_sound_async(SOUND_PROFIT)
                        
                        try:
                            sell_order_start = datetime.now()
                            print(f"[{sell_order_start.strftime('%H:%M:%S.%f')[:-3]}] 🔄 Placing SELL order for {bought_qty} units...")
                            sell_order_resp = place_market_order_groww(selected_option, bought_qty, "SELL", "MIS")
                            sell_order_id = sell_order_resp.get("payload", {}).get("groww_order_id") or sell_order_resp.get("groww_order_id")
                            sell_order_placed = datetime.now()
                            sell_order_duration = (sell_order_placed - sell_order_start).total_seconds()
                            print(f"[{sell_order_placed.strftime('%H:%M:%S.%f')[:-3]}] ✅ SELL Order placed: {sell_order_id} (took {sell_order_duration:.2f}s)")
                            send_telegram(f"{sell_reason}\n✅ SELL Order: {sell_order_id}")
                            
                            if cfg.get("VALIDATE_ORDERS", True) and sell_order_id:
                                sell_validation_start = datetime.now()
                                sell_status = wait_for_order_status(sell_order_id, access_token, "SELL")
                                if sell_status in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                                    sell_price, sold_qty = get_order_executed_price(sell_order_id, access_token)
                                    if sell_price and sold_qty:
                                        sell_executed = datetime.now()
                                        sell_validation_duration = (sell_executed - sell_validation_start).total_seconds()
                                        profit = (sell_price - avg_price) * sold_qty
                                        
                                        # Calculate complete trade duration
                                        complete_trade_duration = (sell_executed - start_time).total_seconds()
                                        
                                        print(f"[{sell_executed.strftime('%H:%M:%S.%f')[:-3]}] 💰 PROFIT: ₹{profit:.2f} (Buy @ ₹{avg_price}, Sell @ ₹{sell_price})")
                                        print(f"[{sell_executed.strftime('%H:%M:%S.%f')[:-3]}] ⏱️  SELL validation: {sell_validation_duration:.2f}s | Complete trade: {complete_trade_duration:.2f}s")
                                        send_telegram(f"💰 PROFIT: ₹{profit:.2f}")
                                        play_sound_async(SOUND_PROFIT if profit > 0 else SOUND_SL)
                                        log_trade_to_excel(symbol, avg_price, sell_price, sold_qty, profit)
                                        display_account_summary(access_token)
                            
                            print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] ✅ Trade cycle completed.")
                            break
                        
                        except Exception as sell_error:
                            print(f"❌ SELL order failed: {sell_error}")
                            send_telegram(f"❌ SELL failed: {sell_error}")
                            break
                    
                    time.sleep(poll)
                
            except Exception as trade_error:
                print(f"❌ Trade execution failed: {trade_error}")
                import traceback
                traceback.print_exc()
            
        except Exception as e:
            print(f"❌ Error in directional mode: {e}")
            import traceback
            traceback.print_exc()


# ----------------- Auto mode runner (momentum + premium) -----------------


def auto_mode_runner():
    cfg = CONFIG
    print("\n--- AUTO MODE (momentum + premium) ---")
    send_telegram("\n--- AUTO MODE (momentum + premium) ---")
    index = cfg["index"]
    expiry = cfg["expiry"]
    min_p = cfg["min_premium"]
    max_p = cfg["max_premium"]
    lots = cfg["lots"]
    book_profit = cfg["book_profit"]
    target_pnl = cfg["target_pnl"]

    while True:
        print(f"Not supported auto mode runner, Switch to auto mode BOT for auto mode runnner")
        time.sleep(2)
        break


# ----------------- Main menu -----------------
if __name__ == "__main__":
    print("\n" + "="*60)
    print("✨ Groww Multi-Index Options Trading Bot Ready")
    print("="*60)
    print(f"📊 Index: {CONFIG['index']} | Expiry: {CONFIG['expiry']}")
    print(f"💰 Lots: {CONFIG['lots']} | Poll: {CONFIG['POLL_INTERVAL']}s")
    
    if CONFIG.get("VALIDATE_ORDERS", True):
        print("✅ LIVE TRADING MODE: Order validation ENABLED")
        print("   → BUY/SELL orders will be verified before proceeding")
    else:
        print("⚠️  TESTING MODE: Order validation DISABLED")
        print("   → Using estimated prices (NOT recommended for live)")
    
    print("="*60)
    
    # Display initial account summary
    print("\n📊 Fetching initial account summary...")
    display_account_summary(access_token)
    
    print("="*60)
    print("Supported: NIFTY (NSE) | SENSEX (BSE) | BANKNIFTY | FINNIFTY")
    print("Manual example: 20 NIFTY17MAR202623150CE")
    print("                50 SENSEX12MAR202674600CE")
    print("Directional: c (Call) / p (Put) - Auto-selects option\n")
    
    while True:
        mode = input("Choose mode: (m)anual / (q)uick / (d)irectional / (a)uto / (e)xit: ").strip().lower()
        if mode in ["e", "exit", "quit"]:
            print("Exiting.")
            break
        if mode in ["a", "auto"]:
            auto_mode_runner()
            continue
        if mode in ["d", "directional", "dir"]:
            directional_mode()
            continue
        if mode in ["q", "quick"]:
            user_input = input("\n⚡ QUICK MODE - Enter command (buy + instant 1.5pt target): ").strip()
            command_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            print(f"⏱️  Command entered at: {command_time}")
            if user_input.lower() in ["back"]:
                continue
            if user_input == "":
                continue
            place_quick_order(user_input)
            continue
        if mode in ["m", "manual"]:
            user_input = input("\nEnter command (or press Enter for status, type 'back' to menu): ").strip()
            command_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]  # Include milliseconds
            print(f"⏱️  Command entered at: {command_time}")
            if user_input.lower() in ["back"]:
                continue
            if user_input == "":
                print("Status check not implemented for Groww PnL in this script.")
                continue
            place_cp_order(user_input)
            continue
        print("Invalid input. Choose 'm' (manual), 'q' (quick), 'a' (auto), or 'e' (exit).")