"""
TEST: BUG-B — Can Groww API modify a CE order's trading_symbol to PE?

The official SDK's modify_order() does NOT expose trading_symbol as a parameter.
But can we bypass that by sending trading_symbol directly in the raw REST payload?

3 attack vectors tested:
  1. SDK modify_order()           — with no trading_symbol (baseline)
  2. Raw REST /v1/order/modify    — with CE trading_symbol replaced by PE symbol
  3. Raw REST /v1/order/modify    — with BOTH old + new trading_symbol fields

Expected correct behavior (all 3):
    API ignores or rejects trading_symbol field — instrument stays CE

Confirmed bug if:
    API returns SUCCESS and order instrument changes from CE → PE

HOW TO RUN:
  1. During market hours, place a LIMIT CE order far from market price (won't execute)
  2. Fill ORDER_ID, CE_SYMBOL, PE_SYMBOL below
  3. python TEST_BUGB_ce_to_pe_modify.py

EXAMPLE SYMBOLS (replace with today's valid expiry):
  CE_SYMBOL = "NIFTY2561924000CE"
  PE_SYMBOL = "NIFTY2561924000PE"   (same strike, same expiry, just PE)
"""

import requests
import json
import pyotp
from growwapi import GrowwAPI

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ORDER_ID  = ""    # groww_order_id of a PENDING (not executed) CE order
CE_SYMBOL = ""    # e.g. "NIFTY2561924000CE"  — the original order symbol
PE_SYMBOL = ""    # e.g. "NIFTY2561924000PE"  — same strike/expiry, but PE
QUANTITY  = 1     # same quantity as original order
SEGMENT   = "FNO"

_API_KEY     = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjQ2NTczODEsImlhdCI6MTc3NjI1NzM4MSwibmJmIjoxNzc2MjU3MzgxLCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCJjMjAzMmM5MS04ZGYzLTRkZDUtYjc5NS0yMGVlOWRhZDhhZjlcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZXNzaW9uSWRcIjpcIjI0OWQ2OGRlLTNjZTgtNGQ4OS05ODJkLWM0N2NmYmI1YzdlNFwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIyNDA5OjQwYzQ6MTBhMzozN2UzOjE4NGI6N2IyOTpiMzBlOjIwZTUsMTcyLjcwLjIxOC4xMzUsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjQ2NTczODE2ODYsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.3kotfZI_EC0lzszHKlXiRdqEQv-O8ubYFh0pgoAT0KsSfdQ1sHmts5UtlaAq4PB6DEwY4X2jZUCD8uBgc2nwXQ"
_TOTP_SECRET = "SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4"
# ─────────────────────────────────────────────────────────────────────────────


def get_access_token():
    totp = pyotp.TOTP(_TOTP_SECRET).now()
    token = GrowwAPI.get_access_token(api_key=_API_KEY, totp=totp)
    print("  Access token fetched.")
    return token


def get_order_detail(order_id, access_token):
    url = f"https://api.groww.in/v1/order/status/{order_id}?segment={SEGMENT}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0"
    }
    resp = requests.get(url, headers=headers, timeout=8)
    print(f"  HTTP {resp.status_code}")
    try:
        data = resp.json()
        print(f"  {json.dumps(data, indent=2)}")
        return data.get("payload", {})
    except Exception:
        print(f"  Raw: {resp.text}")
        return {}


def raw_modify(order_id, access_token, extra_payload: dict, label: str):
    """Send raw POST to /v1/order/modify with custom payload fields."""
    url = "https://api.groww.in/v1/order/modify"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0"
    }
    base_payload = {
        "groww_order_id": order_id,
        "segment": SEGMENT,
        "quantity": QUANTITY,
        "order_type": "LIMIT",
        "price": 0.05,          # deep OTM price — won't execute
    }
    payload = {**base_payload, **extra_payload}

    print(f"\n--- {label} ---")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    resp = requests.post(url, headers=headers, json=payload, timeout=8)
    print(f"  HTTP: {resp.status_code}")
    print(f"  Body: {resp.text}")

    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {}


def verdict(label, http_code, data, ce_symbol, pe_symbol):
    print(f"\n  VERDICT [{label}]:")
    accepted = (
        http_code == 200
        or data.get("status") == "SUCCESS"
        or (data.get("payload") and not data.get("error"))
    )
    trading_symbol_changed = (
        data.get("payload", {}).get("trading_symbol") == pe_symbol
    )
    if accepted and trading_symbol_changed:
        print(f"  *** BUG CONFIRMED: API accepted CE→PE symbol change! ***")
        print(f"      Order is now {pe_symbol} instead of {ce_symbol}")
    elif accepted:
        print(f"  API accepted the modify (HTTP {http_code}) — but check order detail to confirm symbol.")
        print(f"  Run Step 3 (post-check) to see if trading_symbol silently changed.")
    else:
        print(f"  API rejected modify. Code={http_code} | {data.get('error') or data.get('message','')}")


def main():
    if not ORDER_ID or not CE_SYMBOL or not PE_SYMBOL:
        print("ERROR: Fill in ORDER_ID, CE_SYMBOL, and PE_SYMBOL at the top.")
        print()
        print("REMINDER: ORDER_ID must be a PENDING (not yet executed) CE order.")
        print("Place a far-OTM CE limit order manually in Groww app to get a pending order ID.")
        return

    print("=" * 60)
    print("BUG-B TEST: Modify CE order trading_symbol → PE via raw API")
    print("=" * 60)

    print("\nFetching access token...")
    access_token = get_access_token()

    # Step 1: Confirm order is PENDING and is CE
    print(f"\nStep 1 — Current order state ({ORDER_ID}):")
    before = get_order_detail(ORDER_ID, access_token)
    print(f"\n  trading_symbol : {before.get('trading_symbol')}")
    print(f"  order_status   : {before.get('order_status')}")

    if before.get("order_status") not in ("OPEN", "NEW", "ACKED", "PENDING", None):
        print(f"\nWARNING: Order status is '{before.get('order_status')}' — not PENDING.")
        print("Modify is only allowed on pending orders. Results may be misleading.")

    # Step 2a: Vector 1 — SDK modify (no trading_symbol, baseline)
    print("\n" + "=" * 60)
    print("VECTOR 1: SDK modify_order() — no trading_symbol in payload (baseline)")
    print("=" * 60)
    try:
        client = GrowwAPI(access_token)
        sdk_resp = client.modify_order(
            order_type="LIMIT",
            segment="FNO",
            groww_order_id=ORDER_ID,
            quantity=QUANTITY,
            price=0.05,
        )
        print(f"  SDK Response: {json.dumps(sdk_resp, indent=2)}")
    except Exception as e:
        print(f"  SDK Error: {e}")

    # Step 2b: Vector 2 — Raw REST with trading_symbol swapped CE → PE
    print("\n" + "=" * 60)
    print("VECTOR 2: Raw REST — trading_symbol replaced with PE symbol")
    print("=" * 60)
    code2, data2 = raw_modify(
        ORDER_ID, access_token,
        extra_payload={"trading_symbol": PE_SYMBOL},
        label="CE→PE swap"
    )
    verdict("Vector 2", code2, data2, CE_SYMBOL, PE_SYMBOL)

    # Step 2c: Vector 3 — Both old_trading_symbol + new trading_symbol
    print("\n" + "=" * 60)
    print("VECTOR 3: Raw REST — both old_trading_symbol and new trading_symbol")
    print("=" * 60)
    code3, data3 = raw_modify(
        ORDER_ID, access_token,
        extra_payload={
            "trading_symbol": PE_SYMBOL,
            "old_trading_symbol": CE_SYMBOL,
        },
        label="old+new symbol fields"
    )
    verdict("Vector 3", code3, data3, CE_SYMBOL, PE_SYMBOL)

    # Step 3: Re-fetch order to see if symbol actually changed
    print("\n" + "=" * 60)
    print("Step 3 — Final order state after all modify attempts:")
    print("=" * 60)
    after = get_order_detail(ORDER_ID, access_token)
    ts_before = before.get("trading_symbol", "N/A")
    ts_after  = after.get("trading_symbol", "N/A")

    print(f"\n  trading_symbol BEFORE : {ts_before}")
    print(f"  trading_symbol AFTER  : {ts_after}")

    if ts_before != ts_after:
        print(f"\n  *** CRITICAL BUG: trading_symbol changed from {ts_before} → {ts_after} ***")
        print(f"  CE order was converted to PE without placing a new order!")
    else:
        print(f"\n  Symbol unchanged — API correctly protected the trading_symbol field.")


if __name__ == "__main__":
    main()
