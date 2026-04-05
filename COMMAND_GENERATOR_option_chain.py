"""
COMMAND GENERATOR - OPTION CHAIN FORMAT
========================================
Generates trading commands for manual bot mode
Supports: NIFTY & SENSEX
Shows: All strikes in option chain format for current & next expiry
Output: Ready-to-use commands for manual trading bot

Usage: python COMMAND_GENERATOR_option_chain.py
"""

import os
import json
import csv
from datetime import datetime, timedelta
import pyotp
import requests

# ==================== CONFIGURATION ====================
api_key = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NTAwNDY3MzksImlhdCI6MTc2MTY0NjczOSwibmJmIjoxNzYxNjQ2NzM5LCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCI2MmEwMTc4YS0wOTk3LTQ0ZDAtOWRiNC0wZDAzOWM5MzY3YmZcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNWQwYzdjODgtMGI1OS01MDU0LTk5ZTYtYWU5MzY5OTc2ZmRiXCIsXCJzZXNzaW9uSWRcIjpcIjY1NzBiNDUwLWE2YzYtNDMyYi1hYTJmLTA4MjExZjk0YzRiOVwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIxNzEuNjAuMTY5LjI1MiwxNzIuNjkuOTUuOTMsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NTAwNDY3Mzk5MTV9IiwiaXNzIjoiYXBleC1hdXRoLXByb2QtYXBwIn0.EKERC7OzG-lblhaOSQPyb44mafdNFpErGbcELiTiLnRk4WEW9p7aBBf6iq-3LGagY4ORdOCnrXbRhyGzbscxSw"
totp_gen = pyotp.TOTP('WI4M7KCAMH5CGN2I6SVB6MN2QDKUXRJF')

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
csv_path = os.path.join(PROJECT_ROOT, "instrument.csv")

# Import Groww API
try:
    from growwapi import GrowwAPI
except Exception:
    print("❗ growwapi module not found. Make sure it's installed.")
    exit(1)

# ==================== GROWW INITIALIZATION ====================
def groww_init(api_key):
    """Initialize Groww API client"""
    totp = totp_gen.now()
    try:
        access_token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
        client = GrowwAPI(access_token)
        # Suppress output - just return client and token
        return client, access_token
    except Exception as e:
        raise

groww, access_token = groww_init(api_key)

# ==================== UTILITY FUNCTIONS ====================
def download_latest_instruments():
    """Download latest instrument.csv from Groww"""
    try:
        url = "https://growwapi-assets.groww.in/instruments/instrument.csv"
        print("📥 Downloading latest instruments from Groww...")
        
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        with open(csv_path, 'wb') as f:
            f.write(response.content)
        
        print("✅ Instruments updated successfully")
        return True
    except Exception as e:
        print(f"⚠️ Failed to download instruments: {e}")
        return False

def load_instruments_from_csv():
    """Load instruments from CSV file - Auto-downloads if missing or older than 1 day"""
    # Check if file exists and its age
    should_download = False
    
    if not os.path.exists(csv_path):
        should_download = True
    else:
        # Check if file is older than 1 day
        file_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(csv_path))
        if file_age > timedelta(days=1):
            should_download = True
    
    # Download if needed
    if should_download:
        download_latest_instruments()
    
    instruments = []
    if not os.path.exists(csv_path):
        return instruments
    
    with open(csv_path, encoding='utf-8') as csv_file:
        csv_reader = csv.DictReader(csv_file)
        for row in csv_reader:
            instruments.append(row)
    
    return instruments

def get_spot_price(index_name, access_token):
    """Get current spot price for index using option chain API - SILENT"""
    try:
        # Get current expiry first
        instruments = load_instruments_from_csv()
        current_expiry, _ = get_expiry_dates(instruments, index_name)
        
        if not current_expiry:
            return None
        
        # Determine exchange: SENSEX is on BSE, others on NSE
        exchange = "BSE" if "SENSEX" in index_name.upper() else "NSE"
        
        # Use option chain API to get underlying LTP (most reliable)
        url = f"https://api.groww.in/v1/option-chain/exchange/{exchange}/underlying/{index_name}?expiry_date={current_expiry}"
        
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }
        
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get("status") == "SUCCESS":
            payload = data.get("payload", {})
            underlying_ltp = payload.get("underlying_ltp")
            
            if underlying_ltp:
                return float(underlying_ltp)
        
        return None
        
    except Exception as e:
        return None

def get_expiry_dates(instruments, index_name):
    """Get current and next expiry dates for an index"""
    expiries = set()
    
    for item in instruments:
        if item.get("underlying_symbol", "").upper() == index_name.upper():
            expiry = item.get("expiry_date", "").strip()
            if expiry:
                expiries.add(expiry)
    
    # Sort expiries
    sorted_expiries = sorted(list(expiries))
    
    if len(sorted_expiries) >= 2:
        return sorted_expiries[0], sorted_expiries[1]
    elif len(sorted_expiries) == 1:
        return sorted_expiries[0], None
    else:
        return None, None

def get_strikes_for_index(instruments, index_name, expiry, spot_price, strike_range=10):
    """Get all strikes around spot price"""
    # Determine step size
    step = 100 if "SENSEX" in index_name.upper() else 50
    
    # Calculate strike range
    nearest_strike = round(spot_price / step) * step
    strikes = []
    
    for i in range(-strike_range, strike_range + 1):
        strike = nearest_strike + (i * step)
        strikes.append(strike)
    
    # Get options for these strikes
    options_data = {}
    
    for strike in strikes:
        ce_option = None
        pe_option = None
        
        for item in instruments:
            if (item.get("underlying_symbol", "").upper() == index_name.upper() and
                item.get("expiry_date", "").strip() == expiry):
                
                item_strike = float(item.get("strike_price", 0))
                if abs(item_strike - strike) < 0.01:  # Match strike
                    option_type = item.get("instrument_type", "").upper()
                    
                    if option_type == "CE":
                        ce_option = item
                    elif option_type == "PE":
                        pe_option = item
        
        if ce_option or pe_option:
            options_data[strike] = {
                "CE": ce_option,
                "PE": pe_option
            }
    
    return options_data

def get_ltp_for_option(instrument, access_token):
    """Get LTP for option"""
    if not instrument:
        return None
    
    try:
        trading_symbol = instrument.get("trading_symbol")
        exchange_symbol = f"NSE_{trading_symbol}"
        
        url = f"https://api.groww.in/v1/live-data/ltp?segment=FNO&exchange_symbols={exchange_symbol}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }
        
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        ltp = data.get("payload", {}).get(exchange_symbol)
        return float(ltp) if ltp else None
    except:
        return None

def generate_command(option, quantity):
    """Generate trading command in new format: QUANTITY INDEXDDMMMYYYYSTRIKEOPTIONTYPE"""
    if not option:
        return None
    
    trading_symbol = option.get("trading_symbol", "")
    expiry = option.get("expiry_date", "")
    strike = option.get("strike_price", "")
    option_type = option.get("instrument_type", "")
    
    # Parse expiry to format like 17MAR2026
    try:
        exp_date = datetime.strptime(expiry, "%Y-%m-%d")
        exp_formatted = exp_date.strftime("%d%b%Y").upper()
    except:
        exp_formatted = expiry.replace("-", "")
    
    index = option.get("underlying_symbol", "NIFTY")
    
    # Remove decimal from strike if present
    strike_str = str(int(float(strike)))
    
    # Format: 20 NIFTY17MAR202623500CE
    command = f"{quantity} {index}{exp_formatted}{strike_str}{option_type}"
    
    return command

# ==================== DISPLAY FUNCTIONS ====================
def save_all_commands_to_file(index_name, expiry_label, options_data, quantity, spot_price):
    """Save commands to HTML file with colors - FORMAT: STRIKE == QTY CE | QTY PE"""
    html_filename = f"commands_{index_name}_{expiry_label.replace(' ', '_')}.html"
    
    # Determine strike step for spot detection (SENSEX uses 100, NIFTY uses 50)
    step = 100 if "SENSEX" in index_name.upper() else 50
    
    sorted_strikes = sorted(options_data.keys(), reverse=True)
    
    # Write HTML file with colors
    with open(html_filename, 'w') as f:
        f.write("""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>""" + f"{index_name} Commands - {expiry_label}" + """</title>
    <style>
        body { 
            background-color: #1e1e1e; 
            color: #d4d4d4; 
            font-family: 'Courier New', monospace; 
            padding: 20px;
            font-size: 14px;
        }
        .ce { color: #0dc710; font-weight: bold; }
        .pe { color: #e51717; font-weight: bold; }
        .spot { color: #ffd700; font-weight: bold; }
        pre { line-height: 1.6; }
    </style>
</head>
<body>
<h2>""" + f"{index_name} Option Commands - {expiry_label}" + """</h2>
<h3>Spot Price: """ + f"{spot_price:.2f}" + """</h3>
<pre>""")
        
        for strike in sorted_strikes:
            ce_option = options_data[strike].get("CE")
            pe_option = options_data[strike].get("PE")
            
            ce_cmd = generate_command(ce_option, quantity) if ce_option else ""
            pe_cmd = generate_command(pe_option, quantity) if pe_option else ""
            
            if ce_cmd and pe_cmd:
                strike_int = int(float(strike))
                spot_marker = "📍" if abs(strike_int - spot_price) < (step / 2) else "  "
                
                # Calculate strikes from spot (±10 strikes)
                strikes_from_spot = abs(strike_int - spot_price) / step
                
                # Apply color coding for ±10 strikes from spot
                if strikes_from_spot <= 10:
                    ce_html = f'<span class="ce">{ce_cmd}</span>'
                    pe_html = f'<span class="pe">{pe_cmd}</span>'
                else:
                    ce_html = ce_cmd
                    pe_html = pe_cmd
                
                # Highlight strike if it's spot
                strike_html = f'<span class="spot">{strike_int}</span>' if spot_marker == "📍" else str(strike_int)
                
                f.write(f"{strike_html} {spot_marker} == {ce_html}   |    {pe_html}\n")
        
        f.write("""</pre>
</body>
</html>""")
    
    return html_filename

# ==================== MAIN EXECUTION ====================
def main():
    # Suppress all banner output - just load and generate
    
    # Load instruments
    instruments = load_instruments_from_csv()
    
    if not instruments:
        return
    
    # Configuration - HARDCODED, NO USER INPUT
    indices = ["NIFTY", "SENSEX"]
    strike_range = 20  # 20 strikes above and below ATM
    
    # Fixed quantities: NIFTY=20, SENSEX=50
    quantities = {"NIFTY": 20, "SENSEX": 50}
    
    # Process each index
    for index_name in indices:
        
        # Get spot price (silently)
        spot_price = get_spot_price(index_name, access_token)
        if not spot_price:
            continue
        
        # Get expiry dates
        current_expiry, next_expiry = get_expiry_dates(instruments, index_name)
        
        if not current_expiry:
            continue
        
        # Get quantity for this index
        quantity = quantities.get(index_name, 20)
        
        # Generate option chain for current expiry (silently)
        current_options = get_strikes_for_index(instruments, index_name, current_expiry, spot_price, strike_range)
        
        if current_options:
            filename = save_all_commands_to_file(index_name, f"Current_{current_expiry}", current_options, quantity, spot_price)
            print(f"✅ {filename}")
        
        # Generate option chain for next expiry (silently)
        if next_expiry:
            next_options = get_strikes_for_index(instruments, index_name, next_expiry, spot_price, strike_range)
            
            if next_options:
                filename = save_all_commands_to_file(index_name, f"Next_{next_expiry}", next_options, quantity, spot_price)
                print(f"✅ {filename}")
    
    print("\n✅ Complete")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        pass
