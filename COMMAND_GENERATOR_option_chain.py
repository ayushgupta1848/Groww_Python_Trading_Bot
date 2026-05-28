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
import glob
from datetime import datetime, timedelta
import pyotp
import requests

# ==================== CONFIGURATION ====================
api_key = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjQ2NTczODEsImlhdCI6MTc3NjI1NzM4MSwibmJmIjoxNzc2MjU3MzgxLCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCJjMjAzMmM5MS04ZGYzLTRkZDUtYjc5NS0yMGVlOWRhZDhhZjlcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZXNzaW9uSWRcIjpcIjI0OWQ2OGRlLTNjZTgtNGQ4OS05ODJkLWM0N2NmYmI1YzdlNFwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIyNDA5OjQwYzQ6MTBhMzozN2UzOjE4NGI6N2IyOTpiMzBlOjIwZTUsMTcyLjcwLjIxOC4xMzUsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjQ2NTczODE2ODYsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.3kotfZI_EC0lzszHKlXiRdqEQv-O8ubYFh0pgoAT0KsSfdQ1sHmts5UtlaAq4PB6DEwY4X2jZUCD8uBgc2nwXQ"
totp_gen = pyotp.TOTP('SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4')

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
    
    # Sort expiries, excluding past dates
    today = datetime.now().date()
    sorted_expiries = sorted(
        e for e in expiries
        if datetime.strptime(e, "%Y-%m-%d").date() >= today
    )
    
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
    html_filename = os.path.join(PROJECT_ROOT, f"commands_{index_name}_{expiry_label.replace(' ', '_')}.html")

    step = 100 if "SENSEX" in index_name.upper() else 50

    # Ascending order: low strikes on top (matches real option chain display)
    sorted_strikes = sorted(options_data.keys())

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
        h2, h3 { margin: 4px 0; }
        /* Classic option-chain layout: CE ── strike ── PE */
        .row { display: flex; align-items: center; margin: 3px 0; }
        .ce-group {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: flex-end;
            gap: 6px;
            padding-right: 14px;
        }
        .strike-col {
            width: 100px;
            text-align: center;
            flex-shrink: 0;
            white-space: nowrap;
        }
        .pe-group {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: flex-start;
            gap: 6px;
            padding-left: 14px;
        }
        .ce { color: #0dc710; font-weight: bold; }
        .pe { color: #e51717; font-weight: bold; }
        .spot { color: #ffd700; font-weight: bold; }
        .dim { color: #555; }
        .sep { color: #444; }
        .copy-btn {
            background: none;
            border: 1px solid #444;
            color: #777;
            cursor: pointer;
            padding: 1px 6px;
            border-radius: 3px;
            font-size: 12px;
            line-height: 1.4;
            flex-shrink: 0;
        }
        .copy-btn:hover { color: #ccc; border-color: #aaa; }
        .copy-btn.copied { color: #0dc710; border-color: #0dc710; }
    </style>
    <script>
    function cp(text, btn) {
        navigator.clipboard.writeText(text).then(() => {
            const orig = btn.textContent;
            btn.textContent = '✓';
            btn.classList.add('copied');
            setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 1500);
        });
    }
    </script>
</head>
<body>
<h2>""" + f"{index_name} Option Commands — {expiry_label}" + """</h2>
<h3 style="color:#ffd700">Spot: """ + f"{spot_price:.2f}" + """</h3>
<br>
""")

        for strike in sorted_strikes:
            ce_option = options_data[strike].get("CE")
            pe_option = options_data[strike].get("PE")

            ce_cmd = generate_command(ce_option, quantity) if ce_option else ""
            pe_cmd = generate_command(pe_option, quantity) if pe_option else ""

            if not ce_cmd and not pe_cmd:
                continue

            strike_int = int(float(strike))
            is_spot = abs(strike_int - spot_price) < (step / 2)
            strikes_from_spot = abs(strike_int - spot_price) / step
            in_range = strikes_from_spot <= 10

            spot_marker = " 📍" if is_spot else ""
            strike_cls = "spot" if is_spot else ("dim" if not in_range else "")
            strike_html = f'<span class="{strike_cls}">{strike_int}{spot_marker}</span>' if strike_cls else f'{strike_int}{spot_marker}'

            def ce_html(cmd):
                if not cmd:
                    return ''
                escaped = cmd.replace("'", "\\'")
                color_cls = "ce" if in_range else "dim"
                # CE: command text first, copy button on the right (nearest the center)
                return (
                    f'<span class="{color_cls}">{cmd}</span>'
                    f'<button class="copy-btn" onclick="cp(\'{escaped}\', this)">📋</button>'
                )

            def pe_html(cmd):
                if not cmd:
                    return ''
                escaped = cmd.replace("'", "\\'")
                color_cls = "pe" if in_range else "dim"
                # PE: copy button on the left (nearest the center), command text after
                return (
                    f'<button class="copy-btn" onclick="cp(\'{escaped}\', this)">📋</button>'
                    f'<span class="{color_cls}">{cmd}</span>'
                )

            f.write(
                f'<div class="row">'
                f'<span class="ce-group">{ce_html(ce_cmd)}</span>'
                f'<span class="strike-col sep">|&nbsp;{strike_html}&nbsp;|</span>'
                f'<span class="pe-group">{pe_html(pe_cmd)}</span>'
                f'</div>\n'
            )

        f.write("</body>\n</html>")

    return html_filename

# ==================== MAIN EXECUTION ====================
def main():
    # Clear any previously generated command sheets
    for old_file in glob.glob(os.path.join(PROJECT_ROOT, "commands_*.html")):
        try:
            os.remove(old_file)
        except OSError:
            pass

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
