"""
whatsapp_gateway.py
===================
Drop-in WhatsApp replacement for the Telegram send_telegram() pattern.

Outbound (send_whatsapp):
    Same async daemon-thread pattern — fire and forget, never blocks trading.

Inbound (2-way chatbot):
    start_webhook_server(port=5055) starts a Flask server in a background thread.
    Twilio POSTs incoming WhatsApp messages to /whatsapp.
    Commands are written to .wa_control.json so any bot process can poll them.

Setup
-----
1. Sign up at https://www.twilio.com  → get Account SID + Auth Token
2. Activate WhatsApp sandbox (Console → Messaging → Try WhatsApp)
3. Set environment variables:
       export TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxx
       export TWILIO_AUTH_TOKEN=your_auth_token
       export TWILIO_WA_FROM=whatsapp:+14155238886   # Sandbox default
       export WHATSAPP_TO=whatsapp:+91XXXXXXXXXX     # Your number
4. Expose port 5055 publicly:
       ngrok http 5055
5. In Twilio console → Sandbox Settings → set webhook URL:
       https://<ngrok-id>.ngrok.io/whatsapp

Supported WhatsApp commands (send from your phone):
    HELP    — show command list
    STATUS  — request bot status (bot replies with current state)
    PAUSE   — pause auto-trading
    RESUME  — resume auto-trading
    STOP    — emergency stop all bots
"""

import json
import os
import threading
from datetime import datetime
from typing import Optional

import requests
from flask import Flask, request, Response

# ── Twilio credentials (set via environment variables) ────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WA_FROM     = os.getenv("TWILIO_WA_FROM", "whatsapp:+14155238886")
WHATSAPP_TO        = os.getenv("WHATSAPP_TO", "whatsapp:+916012308856")

_BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
_CONTROL_FILE = os.path.join(_BASE_DIR, ".wa_control.json")
_server_lock  = threading.Lock()
_server_started = False


# ── Outbound ──────────────────────────────────────────────────────────────────

def send_whatsapp(message: str) -> None:
    """Send a WhatsApp message via Twilio asynchronously (non-blocking)."""
    def _send():
        try:
            if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
                print(f"⚠️ WhatsApp not configured — message dropped: {message[:60]}")
                return
            url = (
                f"https://api.twilio.com/2010-04-01/Accounts/"
                f"{TWILIO_ACCOUNT_SID}/Messages.json"
            )
            requests.post(
                url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                data={"From": TWILIO_WA_FROM, "To": WHATSAPP_TO, "Body": message},
                timeout=10,
            )
        except Exception as e:
            print(f"⚠️ WhatsApp send error: {e}")
    threading.Thread(target=_send, daemon=True).start()


# ── IPC control file (bots poll this for incoming commands) ───────────────────

def get_pending_command() -> Optional[str]:
    """Returns an uppercase command string if a new command is pending, else None."""
    try:
        if not os.path.exists(_CONTROL_FILE):
            return None
        with open(_CONTROL_FILE, "r") as f:
            data = json.load(f)
        if not data.get("processed", True):
            return data.get("command", "").upper()
    except Exception:
        pass
    return None


def mark_command_processed(ack: str = "") -> None:
    """Mark the pending command as done. Optionally send an ack back to WhatsApp."""
    try:
        with open(_CONTROL_FILE, "r") as f:
            data = json.load(f)
        data["processed"] = True
        with open(_CONTROL_FILE, "w") as f:
            json.dump(data, f, indent=2)
        if ack:
            send_whatsapp(ack)
    except Exception:
        pass


def _write_command(cmd: str) -> None:
    data = {
        "command": cmd,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "processed": False,
    }
    with open(_CONTROL_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Inbound webhook server ────────────────────────────────────────────────────

_flask_app = Flask(__name__)


@_flask_app.route("/whatsapp", methods=["POST"])
def _whatsapp_webhook():
    incoming = request.form.get("Body", "").strip().upper()
    xml = _handle_command(incoming)
    return Response(xml, mimetype="application/xml")


@_flask_app.route("/health", methods=["GET"])
def _health():
    return {"status": "ok", "service": "whatsapp-gateway"}, 200


def _handle_command(cmd: str) -> str:
    HELP_TEXT = (
        "🤖 Trading Bot Commands:\n"
        "STATUS  — check if bot is running\n"
        "PAUSE   — pause auto-trading\n"
        "RESUME  — resume auto-trading\n"
        "STOP    — emergency stop\n"
        "HELP    — show this list"
    )
    if cmd == "HELP":
        body = HELP_TEXT
    elif cmd in ("STATUS", "PAUSE", "RESUME", "STOP"):
        _write_command(cmd)
        body = f"✅ Command *{cmd}* queued — bot will acknowledge shortly."
    else:
        body = f"Unknown command '{cmd}'. Send HELP to see options."

    safe_body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{safe_body}</Message></Response>"
    )


def start_webhook_server(port: int = 5055) -> None:
    """Start the Twilio webhook server in a background daemon thread.
    Safe to call from multiple bots — only the first call actually starts it."""
    global _server_started
    with _server_lock:
        if _server_started:
            return
        _server_started = True

    def _run():
        import logging
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        _flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    threading.Thread(target=_run, daemon=True, name="wa-webhook").start()
    print(f"📱 WhatsApp webhook listening on port {port}")
    print(f"   Expose with:  ngrok http {port}")
    print(f"   Twilio URL:   https://<ngrok-id>.ngrok.io/whatsapp")
