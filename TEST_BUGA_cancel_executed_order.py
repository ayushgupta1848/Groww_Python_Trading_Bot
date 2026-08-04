"""
TEST: BUG-A — Can Groww API cancel an already-EXECUTED order?

Expected correct behavior:
    API should return 400 / error / INVALID_STATE

Suspected bug:
    API returns 200 SUCCESS on an executed order → free reverse/exit without placing a real order

HOW TO RUN:
    1. Fill in ORDER_ID below (any EXECUTED order from today's session)
    2. python TEST_BUGA_cancel_executed_order.py

    ACCESS_TOKEN is auto-fetched using your existing credentials.
"""

import requests
import json
import pyotp
from growwapi import GrowwAPI

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ORDER_ID = "GMKFO2606051444188B8PUKVWV6RO"   # SELL order from 2026-06-01 (most recent in logs)
SEGMENT  = "FNO"  # change to "CASH" if testing equity order

# Credentials from main bot (auto-fetches access token)
_API_KEY     = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjQ2NTczODEsImlhdCI6MTc3NjI1NzM4MSwibmJmIjoxNzc2MjU3MzgxLCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCJjMjAzMmM5MS04ZGYzLTRkZDUtYjc5NS0yMGVlOWRhZDhhZjlcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMmVlMjYyMjItN2MwNS00Y2IwLWIwM2MtNzAzYWRmNWVmN2RkXCIsXCJkZXZpY2VJZFwiOlwiNjA2MzE5M2QtZWZkMC01OWViLTgzYzQtNWQ2NGZkNzdkNzQ3XCIsXCJzZXNzaW9uSWRcIjpcIjI0OWQ2OGRlLTNjZTgtNGQ4OS05ODJkLWM0N2NmYmI1YzdlNFwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYktvMDZXRlpjc241VUNmTWF5aERtNGxSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIyNDA5OjQwYzQ6MTBhMzozN2UzOjE4NGI6N2IyOTpiMzBlOjIwZTUsMTcyLjcwLjIxOC4xMzUsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjQ2NTczODE2ODYsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.3kotfZI_EC0lzszHKlXiRdqEQv-O8ubYFh0pgoAT0KsSfdQ1sHmts5UtlaAq4PB6DEwY4X2jZUCD8uBgc2nwXQ"
_TOTP_SECRET = "SC3YMFLEGLHBWUPHRBOYLPEEOVAT2PZ4"
# ─────────────────────────────────────────────────────────────────────────────


def get_access_token():
    totp = pyotp.TOTP(_TOTP_SECRET).now()
    token = GrowwAPI.get_access_token(api_key=_API_KEY, totp=totp)
    print(f"  Access token fetched successfully.")
    return token


def get_order_status(order_id, access_token):
    url = f"https://api.groww.in/v1/order/status/{order_id}?segment={SEGMENT}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0"
    }
    resp = requests.get(url, headers=headers, timeout=8)
    print(f"\n[STATUS CHECK]")
    print(f"  HTTP: {resp.status_code}")
    print(f"  Body: {json.dumps(resp.json(), indent=2)}")
    data = resp.json()
    return data.get("payload", {}).get("order_status")


def try_cancel_executed_order(order_id, access_token):
    url = "https://api.groww.in/v1/order/cancel"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-API-VERSION": "1.0"
    }
    payload = {
        "segment": SEGMENT,
        "groww_order_id": order_id
    }

    print(f"\n[CANCEL ATTEMPT]")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    resp = requests.post(url, headers=headers, json=payload, timeout=8)

    print(f"\n[CANCEL RESPONSE]")
    print(f"  HTTP Status : {resp.status_code}")
    print(f"  Raw Body    : {resp.text}")

    try:
        data = resp.json()
        print(f"  Parsed JSON : {json.dumps(data, indent=2)}")
    except Exception:
        print("  (non-JSON response)")
        data = {}

    return resp.status_code, data


def main():
    if not ORDER_ID:
        print("ERROR: Fill in ORDER_ID at the top of this script.")
        return

    print("=" * 60)
    print("BUG-A TEST: Cancel an EXECUTED Groww order")
    print("=" * 60)

    # Auto-fetch access token
    print("\nFetching access token...")
    access_token = get_access_token()

    # Step 1: Confirm order is actually EXECUTED
    print(f"\nStep 1 — Checking current status of order: {ORDER_ID}")
    status = get_order_status(ORDER_ID, access_token)
    print(f"\n  >>> Current order status: {status}")

    if status != "EXECUTED":
        print(f"\nWARNING: Order is '{status}', not EXECUTED.")
        print("For a valid BUG-A test, use an EXECUTED order.")
        print("Proceeding anyway to capture the API response...\n")
    else:
        print("\nConfirmed EXECUTED. Now attempting cancel...\n")

    # Step 2: Fire cancel on the executed order
    http_code, response_data = try_cancel_executed_order(ORDER_ID, access_token)

    # Step 3: Verdict
    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)

    success_signals = (
        http_code == 200
        or response_data.get("success") is True
        or response_data.get("status") == "SUCCESS"
        or response_data.get("payload", {}).get("order_status") == "CANCELLED"
    )

    if success_signals:
        print("BUG CONFIRMED: API accepted cancel on an EXECUTED order.")
        print("  This means Groww API does NOT validate order state before cancellation.")
        print("  A bot could call cancel() on a filled order and get a false SUCCESS.")
    else:
        print("API correctly rejected the cancel request.")
        print(f"  HTTP {http_code} — {response_data.get('message') or response_data.get('error') or 'no message'}")

    # Step 4: Re-check status to see if anything changed
    print(f"\nStep 4 — Re-checking order status after cancel attempt...")
    status_after = get_order_status(ORDER_ID, access_token)
    print(f"\n  >>> Status BEFORE cancel: {status}")
    print(f"  >>> Status AFTER  cancel: {status_after}")

    if status == "EXECUTED" and status_after == "CANCELLED":
        print("\nCRITICAL: Order status changed from EXECUTED → CANCELLED!")
        print("This is a confirmed critical bug — position reversed without market order.")
    elif status == status_after:
        print("\nStatus unchanged — cancel had no effect on the executed order.")
    else:
        print(f"\nStatus changed: {status} → {status_after} (investigate this)")


if __name__ == "__main__":
    main()
