"""
Phase 1: Telegram -> Render -> Groq -> Telegram

Scope, on purpose:
- No memory. No WorldState. No Brain. No command routing.
- Slash commands (e.g. /thesis, /taken, /skip) are explicitly IGNORED here so
  this webhook never fights with however your existing trading system already
  processes those. Only free-text messages get a Groq reply.
- Goal: prove the webhook + Groq round trip is reliable and Render doesn't
  fall asleep mid-conversation. Everything else builds on top of this file.

Required env vars (set these in Render's dashboard, not in code):
  TELEGRAM_BOT_TOKEN   - from @BotFather
  GROQ_API_KEY         - from console.groq.com
  TELEGRAM_WEBHOOK_SECRET - a random string you pick; used to verify that
                             incoming requests actually came from Telegram
  ALLOWED_CHAT_ID      - (optional but recommended) your personal Telegram
                          chat id, so only you can talk to this bot. If unset,
                          the bot will reply to anyone who finds it.

Optional env vars:
  GROQ_MODEL           - defaults to llama-3.3-70b-versatile
  SYSTEM_PROMPT         - defaults to a short generic assistant prompt
"""

import logging
import os

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("phase1")

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")  # string compare, keep as str
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a concise, direct assistant. No filler, no hedging. "
    "If you don't know something, say so plainly.",
)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"

# Commands your existing trading system already owns. We never touch these.
RESERVED_COMMANDS = ("/thesis", "/marketintent", "/understand", "/taken", "/skip")


def send_telegram_message(chat_id: int, text: str) -> None:
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if not resp.ok:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    except requests.RequestException:
        log.exception("Telegram send raised an exception")


def call_groq(user_text: str) -> str:
    try:
        resp = requests.post(
            GROQ_API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 1024,
                "temperature": 0.7,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.RequestException:
        log.exception("Groq call failed")
        return "Groq call failed — check Render logs."
    except (KeyError, IndexError, ValueError):
        log.exception("Unexpected Groq response shape")
        return "Got an unexpected response from Groq — check Render logs."


@app.route("/ping", methods=["GET"])
def ping():
    """Hit by Cronjob.com every ~10 min to keep the free-tier instance awake."""
    return jsonify(status="awake"), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    # Verify the request actually came from Telegram, not a random POST to
    # this public URL.
    if WEBHOOK_SECRET:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming_secret != WEBHOOK_SECRET:
            log.warning("Rejected webhook call with bad/missing secret token")
            return jsonify(status="forbidden"), 403

    update = request.get_json(silent=True) or {}
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text")

    if chat_id is None or not text:
        # Non-text update (photo, sticker, edited message, etc). Ignore.
        return jsonify(status="ignored"), 200

    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        log.warning("Ignored message from unauthorized chat_id=%s", chat_id)
        return jsonify(status="ignored"), 200

    if text.startswith("/") and text.split()[0] in RESERVED_COMMANDS:
        # Owned by the existing trading system, not this webhook.
        return jsonify(status="ignored_reserved_command"), 200

    log.info("Message from chat_id=%s: %s", chat_id, text)
    reply = call_groq(text)
    send_telegram_message(chat_id, reply)

    return jsonify(status="ok"), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
