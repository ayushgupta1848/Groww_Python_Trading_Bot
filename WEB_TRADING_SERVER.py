"""
WEB TRADING SERVER - ONE-CLICK TRADING
========================================
Interactive web interface for instant order placement
Click any option in the HTML page → Order placed instantly with trailing SL

Features:
- One-click order placement from browser
- Real-time LTP display
- Same trailing SL logic as manual bot
- Faster than typing commands

Usage: python WEB_TRADING_SERVER.py
Then open: http://localhost:5000
"""

import os
import sys
import json
import csv
import time
import threading
import socket
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template_string, jsonify, request
from flask_cors import CORS
import pyotp
import requests

# Force IPv4 for all outbound connections (Groww API rejects IPv6)
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(host, port, family=0, *args, **kwargs):
    return _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)
socket.getaddrinfo = _ipv4_getaddrinfo

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# Import from main bot
from growwapi import GrowwAPI

# ==================== CONFIGURATION ====================
api_key = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjQ2NTAwNzUsImlhdCI6MTc3NjI1MDA3NSwibmJmIjoxNzc2MjUwMDc1LCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCIxNzgxZjJiOS04Yzg3LTQ2MTQtYTkwOS1jYzFiODI2MGE0YzBcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZXNzaW9uSWRcIjpcImY1YWNmYWI1LWNhM2MtNDVmMC1hYTg2LTg5M2UxMmUzZTEyNFwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIyNDAxOjQ5MDA6MWMwOToyYTAzOmQxZmU6MTY0NDplMDkzOmRlMDcsMTcyLjY5LjEzMS4yMDgsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjQ2NTAwNzU3NTIsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.qjNigaEkjkjwsVYCDA4Y5HVJKj2QgyoHbxO1t2RJRHUVe5s4LVVz3Bf0CRAhcUQfIqetuZFWWKDukn8cZSQYfA"
totp_gen = pyotp.TOTP('KU6bexkBZk3xV-Tn2KQ60u8W423*2s)$')

csv_path = os.path.join(PROJECT_ROOT, "instrument.csv")

# Trading configuration
CONFIG = {
    "HARD_SL_POINTS": 10,
    "TRAIL_START_PROFIT": 10,
    "TRAIL_STEP": 5,
    "POLL_INTERVAL": 1,
    "MAX_TRAIL_TIME": 3600,
    "VALIDATE_ORDERS": True
}

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Global variables
access_token = None
groww = None
instruments_data = []
active_trades = {}

# ==================== UTILITY FUNCTIONS ====================
def groww_init():
    """Initialize Groww API client - same as manual bot"""
    global access_token, groww
    totp = totp_gen.now()
    try:
        access_token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
        groww = GrowwAPI(access_token)
        print(f"Access Token: {access_token[:50]}...")
        print("✅ Groww API initialized")
        return True
    except Exception as e:
        print(f"❌ Groww init failed: {e}")
        return False

def load_instruments():
    """Load instruments from CSV"""
    global instruments_data
    if not os.path.exists(csv_path):
        return False
    
    instruments_data = []
    with open(csv_path, encoding='utf-8') as csv_file:
        csv_reader = csv.DictReader(csv_file)
        for row in csv_reader:
            instruments_data.append(row)
    
    print(f"✅ Loaded {len(instruments_data)} instruments")
    return True

def get_spot_price(index_name):
    """Get spot price for index"""
    try:
        # Get current expiry
        expiries = set()
        for item in instruments_data:
            if item.get("underlying_symbol", "").upper() == index_name.upper():
                expiry = item.get("expiry_date", "").strip()
                if expiry:
                    try:
                        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
                        if expiry_date >= datetime.now().date():
                            expiries.add(expiry)
                    except:
                        pass
        
        if not expiries:
            return None
        
        current_expiry = sorted(list(expiries))[0]
        exchange = "BSE" if "SENSEX" in index_name.upper() else "NSE"
        
        url = f"https://api.groww.in/v1/option-chain/exchange/{exchange}/underlying/{index_name}?expiry_date={current_expiry}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }
        
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
        
        if data.get("status") == "SUCCESS":
            return float(data.get("payload", {}).get("underlying_ltp", 0))
        
        return None
    except:
        return None

def get_ltp_for_instrument(instrument):
    """Get LTP for option"""
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
        data = resp.json()
        
        ltp = data.get("payload", {}).get(exchange_symbol)
        return float(ltp) if ltp else None
    except:
        return None

def place_market_order(instrument, quantity, transaction_type="BUY"):
    """Place market order - exact same as manual bot"""
    try:
        trading_symbol = instrument.get("internal_trading_symbol") or instrument.get("trading_symbol")
        exch_str = instrument.get("exchange", "NSE").upper()
        exchange_const = groww.EXCHANGE_BSE if exch_str == "BSE" else groww.EXCHANGE_NSE
        
        print(f"📝 Order details:")
        print(f"   Symbol: {trading_symbol}")
        print(f"   Exchange: {exch_str}")
        print(f"   Quantity: {quantity}")
        print(f"   Type: {transaction_type}")
        
        order = groww.place_order(
            trading_symbol=trading_symbol,
            quantity=quantity,
            validity=groww.VALIDITY_DAY,
            exchange=exchange_const,
            segment=groww.SEGMENT_FNO,
            product=groww.PRODUCT_MIS,
            order_type=groww.ORDER_TYPE_MARKET,
            transaction_type=getattr(groww, f"TRANSACTION_TYPE_{transaction_type}"),
            price=0
        )
        
        print(f"✅ Order response: {order}")
        return order
    except Exception as e:
        print(f"❌ Order failed: {e}")
        import traceback
        traceback.print_exc()
        return None

def wait_for_order_status(order_id, expected_type="BUY"):
    """Wait for order execution"""
    max_wait = 10
    start = time.time()

    url = f"https://api.groww.in/v1/order/status/{order_id}?segment=FNO"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0"
    }

    while time.time() - start < max_wait:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            data = resp.json()
            order_status = data.get("payload", {}).get("order_status", "")

            if order_status in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                return order_status
            elif order_status in ["REJECTED", "CANCELLED", "FAILED"]:
                return order_status
        except:
            pass

        time.sleep(0.5)

    return "TIMEOUT"

def get_order_executed_price(order_id):
    """Get executed price and quantity from trades"""
    try:
        url = f"https://api.groww.in/v1/order/trades/{order_id}?segment=FNO&page=0&page_size=50"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0"
        }
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()
        if data.get("status") != "SUCCESS":
            return None, None
        trades = data.get("payload", {}).get("trade_list", [])
        if not trades:
            return None, None
        total_qty = sum(t["quantity"] for t in trades)
        total_value = sum(t["price"] * t["quantity"] for t in trades)
        avg_price = round(total_value / total_qty, 2)
        return avg_price, total_qty
    except:
        return None, None

def round_to_nearest_5_paise(price):
    """Round to nearest 0.05"""
    return round(price * 20) / 20

def monitor_with_trailing_stop(instrument, buy_order_id, entry_price, quantity):
    """Monitor position with trailing SL - same logic as manual bot"""
    try:
        cfg = CONFIG
        hard_sl_points = cfg["HARD_SL_POINTS"]
        hard_sl = round_to_nearest_5_paise(entry_price - hard_sl_points)
        
        trail_start = cfg["TRAIL_START_PROFIT"]
        trail_step = cfg["TRAIL_STEP"]
        poll = cfg["POLL_INTERVAL"]
        max_time = cfg["MAX_TRAIL_TIME"]
        
        highest_price = entry_price
        start_time = time.time()
        
        print(f"📈 Trailing started | Entry: ₹{entry_price:.2f} | Hard SL: ₹{hard_sl:.2f}")
        
        while time.time() - start_time < max_time:
            # Get current LTP
            current_ltp = get_ltp_for_instrument(instrument)
            
            if current_ltp is None:
                time.sleep(poll)
                continue
            
            # Update highest price
            if current_ltp > highest_price:
                highest_price = current_ltp
                print(f"🚀 New high: ₹{highest_price:.2f}")
            
            # Calculate trailing SL
            profit = highest_price - entry_price
            
            if profit >= trail_start:
                # Activate trailing
                steps_moved = int((profit - trail_start) / trail_step)
                trailing_sl = round_to_nearest_5_paise(entry_price + trail_start + (steps_moved * trail_step))
                effective_sl = max(hard_sl, trailing_sl)
            else:
                effective_sl = hard_sl
            
            # Check if SL hit
            if current_ltp <= effective_sl:
                print(f"🛑 SL Hit at ₹{current_ltp:.2f} | Selling...")
                sell_order = place_market_order(instrument, quantity, "SELL")
                
                if sell_order:
                    sell_order_id = sell_order.get("payload", {}).get("groww_order_id") or sell_order.get("groww_order_id")
                    if sell_order_id:
                        sell_status = wait_for_order_status(sell_order_id, "SELL")
                        if sell_status in ["EXECUTED", "COMPLETED"]:
                            exit_price, _ = get_order_executed_price(sell_order_id)
                            pnl = (exit_price - entry_price) * quantity
                            print(f"✅ Exit @ ₹{exit_price:.2f} | P&L: ₹{pnl:.2f}")
                            return {"status": "SL_HIT", "exit_price": exit_price, "pnl": pnl}
                break
            
            time.sleep(poll)
        
        print("⏰ Max time reached")
        return {"status": "TIMEOUT"}
        
    except Exception as e:
        print(f"❌ Monitoring error: {e}")
        return {"status": "ERROR", "message": str(e)}

# ==================== WEB ROUTES ====================
@app.route('/')
def index():
    """Main page with option chain selector"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Web Trading - One Click Orders</title>
        <meta charset="UTF-8">
        <style>
            body {
                background: #0a0e27;
                color: #fff;
                font-family: 'Segoe UI', Arial, sans-serif;
                margin: 0;
                padding: 20px;
            }
            .header {
                text-align: center;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border-radius: 10px;
                margin-bottom: 30px;
            }
            h1 { margin: 0; font-size: 2em; }
            .subtitle { opacity: 0.9; margin-top: 10px; }
            .index-selector {
                display: flex;
                justify-content: center;
                gap: 20px;
                margin: 30px 0;
            }
            .index-btn {
                padding: 15px 40px;
                font-size: 18px;
                font-weight: bold;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                transition: all 0.3s;
                text-decoration: none;
                color: white;
            }
            .nifty-btn {
                background: linear-gradient(135deg, #0dcaf0 0%, #0d6efd 100%);
            }
            .sensex-btn {
                background: linear-gradient(135deg, #ffc107 0%, #fd7e14 100%);
            }
            .index-btn:hover {
                transform: translateY(-3px);
                box-shadow: 0 10px 20px rgba(0,0,0,0.3);
            }
            .status {
                text-align: center;
                padding: 20px;
                background: #1e2139;
                border-radius: 8px;
                margin-top: 20px;
            }
            .status.success { border-left: 4px solid #0dcaf0; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>⚡ Web Trading - One Click Orders</h1>
            <div class="subtitle">Click any option to place order instantly with trailing SL</div>
        </div>
        
        <div class="index-selector">
            <a href="/chain/NIFTY" class="index-btn nifty-btn">📊 NIFTY Option Chain</a>
            <a href="/chain/SENSEX" class="index-btn sensex-btn">📈 SENSEX Option Chain</a>
        </div>
        
        <div class="status success">
            <strong>✅ Server Running</strong><br>
            Trading Bot Active | Real-time LTP Updates
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/chain/<index_name>')
def option_chain(index_name):
    """Display interactive option chain"""
    index_name = index_name.upper()
    
    # Get spot price
    spot = get_spot_price(index_name)
    if not spot:
        return "❌ Could not fetch spot price"
    
    # Get expiries
    expiries = set()
    for item in instruments_data:
        if item.get("underlying_symbol", "").upper() == index_name:
            expiry = item.get("expiry_date", "").strip()
            if expiry:
                try:
                    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
                    if expiry_date >= datetime.now().date():
                        expiries.add(expiry)
                except:
                    pass
    
    sorted_expiries = sorted(list(expiries))[:2]  # Current and next
    
    # Build option chain data
    step = 100 if "SENSEX" in index_name else 50
    atm = round(spot / step) * step
    strike_range = 15
    
    options_by_expiry = {}
    
    for expiry in sorted_expiries:
        options = []
        for i in range(-strike_range, strike_range + 1):
            strike = atm + (i * step)
            
            ce_option = None
            pe_option = None
            
            for item in instruments_data:
                if (item.get("underlying_symbol", "").upper() == index_name and
                    item.get("expiry_date", "") == expiry):
                    
                    item_strike = float(item.get("strike_price", 0))
                    if abs(item_strike - strike) < 0.01:
                        opt_type = item.get("instrument_type", "").upper()
                        if opt_type == "CE":
                            ce_option = item
                        elif opt_type == "PE":
                            pe_option = item
            
            if ce_option or pe_option:
                options.append({
                    "strike": strike,
                    "ce": ce_option,
                    "pe": pe_option
                })
        
        options_by_expiry[expiry] = options
    
    # Generate HTML
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{index_name} Option Chain - Web Trading</title>
        <meta charset="UTF-8">
        <style>
            body {{
                background: #0a0e27;
                color: #fff;
                font-family: 'Courier New', monospace;
                margin: 0;
                padding: 20px;
                font-size: 14px;
            }}
            .header {{
                text-align: center;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border-radius: 10px;
                margin-bottom: 20px;
            }}
            .spot {{
                font-size: 24px;
                font-weight: bold;
                color: #ffd700;
            }}
            .back-btn {{
                display: inline-block;
                padding: 10px 20px;
                background: #6c757d;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                margin-bottom: 20px;
            }}
            .back-btn:hover {{ background: #5a6268; }}
            .expiry-section {{
                margin: 30px 0;
                background: #1e2139;
                padding: 20px;
                border-radius: 10px;
            }}
            .expiry-title {{
                font-size: 20px;
                font-weight: bold;
                color: #0dcaf0;
                margin-bottom: 15px;
                text-align: center;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }}
            th {{
                background: #2d3250;
                padding: 12px;
                text-align: center;
                border-bottom: 2px solid #667eea;
            }}
            td {{
                padding: 10px;
                text-align: center;
                border-bottom: 1px solid #2d3250;
            }}
            .strike-cell {{
                font-weight: bold;
                font-size: 16px;
            }}
            .atm {{
                background: #ffd70020;
                color: #ffd700;
            }}
            .option-btn {{
                padding: 8px 15px;
                border: none;
                border-radius: 5px;
                cursor: pointer;
                font-weight: bold;
                font-size: 13px;
                transition: all 0.2s;
                width: 100%;
            }}
            .ce-btn {{
                background: linear-gradient(135deg, #00ff88 0%, #00cc66 100%);
                color: #000;
            }}
            .pe-btn {{
                background: linear-gradient(135deg, #ff4444 0%, #cc0000 100%);
                color: #fff;
            }}
            .option-btn:hover {{
                transform: scale(1.05);
                box-shadow: 0 5px 15px rgba(0,0,0,0.5);
            }}
            .option-btn:disabled {{
                opacity: 0.3;
                cursor: not-allowed;
            }}
            .ltp {{
                font-size: 12px;
                opacity: 0.8;
                margin-top: 3px;
            }}
            #notification {{
                position: fixed;
                top: 20px;
                right: 20px;
                padding: 15px 25px;
                border-radius: 8px;
                background: #28a745;
                color: white;
                font-weight: bold;
                display: none;
                z-index: 1000;
                box-shadow: 0 5px 15px rgba(0,0,0,0.3);
            }}
            #notification.error {{ background: #dc3545; }}
            #notification.show {{ display: block; animation: slideIn 0.3s; }}
            @keyframes slideIn {{
                from {{ transform: translateX(400px); opacity: 0; }}
                to {{ transform: translateX(0); opacity: 1; }}
            }}
        </style>
    </head>
    <body>
        <a href="/" class="back-btn">← Back to Index Selection</a>
        
        <div class="header">
            <h1>📊 {index_name} Option Chain</h1>
            <div class="spot">Spot: ₹{spot:.2f}</div>
            <div style="margin-top: 10px; opacity: 0.9;">Click any option to place order instantly</div>
        </div>
        
        <div id="notification"></div>
    """
    
    # Add option chains for each expiry
    for expiry in sorted_expiries:
        options = options_by_expiry[expiry]
        exp_label = datetime.strptime(expiry, "%Y-%m-%d").strftime("%d %b %Y")
        
        html += f"""
        <div class="expiry-section">
            <div class="expiry-title">📅 {exp_label}</div>
            <table>
                <thead>
                    <tr>
                        <th>CALL (CE)</th>
                        <th>STRIKE</th>
                        <th>PUT (PE)</th>
                    </tr>
                </thead>
                <tbody>
        """
        
        for opt in options:
            strike = opt["strike"]
            ce = opt["ce"]
            pe = opt["pe"]
            
            is_atm = abs(strike - atm) < (step / 2)
            atm_class = "atm" if is_atm else ""
            
            ce_html = ""
            if ce:
                ce_symbol = ce.get("internal_trading_symbol", "")
                ce_html = f'<button class="option-btn ce-btn" onclick="placeOrder(\'{ce_symbol}\', \'CE\', {strike})">BUY CALL<div class="ltp" id="ltp_ce_{strike}_{expiry}">Loading...</div></button>'
            
            pe_html = ""
            if pe:
                pe_symbol = pe.get("internal_trading_symbol", "")
                pe_html = f'<button class="option-btn pe-btn" onclick="placeOrder(\'{pe_symbol}\', \'PE\', {strike})">BUY PUT<div class="ltp" id="ltp_pe_{strike}_{expiry}">Loading...</div></button>'
            
            html += f"""
                <tr>
                    <td>{ce_html}</td>
                    <td class="strike-cell {atm_class}">{int(strike)}</td>
                    <td>{pe_html}</td>
                </tr>
            """
        
        html += """
                </tbody>
            </table>
        </div>
        """
    
    # Add JavaScript
    html += """
        <script>
            function showNotification(message, isError = false) {
                const notif = document.getElementById('notification');
                notif.textContent = message;
                notif.className = isError ? 'error show' : 'show';
                setTimeout(() => {
                    notif.className = '';
                }, 5000);
            }
            
            async function placeOrder(symbol, optType, strike) {
                if (!confirm(`Place BUY order for ${symbol}?`)) {
                    return;
                }
                
                showNotification('⏳ Placing order...');
                
                try {
                    const response = await fetch('/api/place_order', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({
                            symbol: symbol,
                            option_type: optType,
                            strike: strike
                        })
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        showNotification(`✅ Order placed! Entry: ₹${data.entry_price} | Qty: ${data.quantity}`);
                    } else {
                        showNotification(`❌ Order failed: ${data.message}`, true);
                    }
                } catch (error) {
                    showNotification(`❌ Error: ${error.message}`, true);
                }
            }
            
            // Auto-refresh LTP every 3 seconds (implement if needed)
        </script>
    </body>
    </html>
    """
    
    return render_template_string(html)

@app.route('/api/place_order', methods=['POST'])
def api_place_order():
    """API endpoint for placing orders"""
    try:
        data = request.json
        symbol = data.get('symbol')
        
        # Find instrument
        instrument = None
        for item in instruments_data:
            if item.get("internal_trading_symbol") == symbol or item.get("trading_symbol") == symbol:
                instrument = item
                break
        
        if not instrument:
            return jsonify({"success": False, "message": "Instrument not found"})
        
        # Determine quantity based on index
        index = instrument.get("underlying_symbol", "NIFTY")
        lot_size = int(instrument.get("lot_size", 25))
        lots = 50 if "SENSEX" in index.upper() else 20
        quantity = lots * lot_size
        
        # Get LTP
        ltp = get_ltp_for_instrument(instrument)
        if not ltp:
            return jsonify({"success": False, "message": "Could not fetch LTP"})
        
        # Place order
        print(f"\n⚡ WEB ORDER: {symbol} | Qty: {quantity} | LTP: ₹{ltp:.2f}")
        
        order_resp = place_market_order(instrument, quantity, "BUY")
        if not order_resp:
            return jsonify({"success": False, "message": "Order placement failed"})
        
        order_id = order_resp.get("payload", {}).get("groww_order_id") or order_resp.get("groww_order_id")
        if not order_id:
            return jsonify({"success": False, "message": "No order ID received"})
        
        # Wait for execution
        if CONFIG.get("VALIDATE_ORDERS", True):
            status = wait_for_order_status(order_id, "BUY")
            if status not in ["EXECUTED", "COMPLETED", "DELIVERY_AWAITED"]:
                return jsonify({"success": False, "message": f"Order status: {status}"})
            
            # Get executed price
            avg_price, executed_qty = get_order_executed_price(order_id)
            if not avg_price:
                avg_price = ltp
                executed_qty = quantity
        else:
            avg_price = ltp
            executed_qty = quantity
        
        print(f"✅ Order executed @ ₹{avg_price:.2f}")
        
        # Start trailing SL in background
        trade_id = f"{symbol}_{datetime.now().strftime('%H%M%S')}"
        threading.Thread(
            target=monitor_with_trailing_stop,
            args=(instrument, order_id, avg_price, executed_qty),
            daemon=True
        ).start()
        
        return jsonify({
            "success": True,
            "order_id": order_id,
            "entry_price": avg_price,
            "quantity": executed_qty,
            "symbol": symbol
        })
        
    except Exception as e:
        print(f"❌ API Error: {e}")
        return jsonify({"success": False, "message": str(e)})

# ==================== MAIN ====================
if __name__ == "__main__":
    print("\n" + "="*60)
    print("⚡ WEB TRADING SERVER - ONE CLICK ORDERS")
    print("="*60)
    
    # Initialize
    if not groww_init():
        print("❌ Failed to initialize Groww API")
        sys.exit(1)
    
    if not load_instruments():
        print("❌ Failed to load instruments")
        sys.exit(1)
    
    print("\n✅ Server ready!")
    print("📱 Open in browser: http://localhost:5000")
    print("\n" + "="*60 + "\n")
    
    # Run Flask server
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
