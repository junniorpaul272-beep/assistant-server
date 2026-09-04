"""
Run this ONCE, locally, after your Render service is deployed and live.
It tells Telegram where to send updates.

Usage:
    TELEGRAM_BOT_TOKEN=xxx TELEGRAM_WEBHOOK_SECRET=yyy python set_webhook.py https://your-app.onrender.com
"""

import os
import sys

import requests

if len(sys.argv) != 2:
    print("Usage: python set_webhook.py https://your-app.onrender.com")
    sys.exit(1)

render_url = sys.argv[1].rstrip("/")
token = os.environ["TELEGRAM_BOT_TOKEN"]
secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

resp = requests.post(
    f"https://api.telegram.org/bot{token}/setWebhook",
    json={
        "url": f"{render_url}/webhook",
        "secret_token": secret,
        "allowed_updates": ["message"],
    },
    timeout=10,
)
print(resp.status_code, resp.json())
