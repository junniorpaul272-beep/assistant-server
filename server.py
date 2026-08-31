"""
server.py — the Assistant. Always-on webhook receiver (this is the piece
GitHub Actions/cronjob.com structurally cannot be — nothing wakes it up,
it's just listening).

ARCHITECTURE (per chat, friend's proposal): Interface (Telegram) -> this
Conversation layer (Claude + tool-use loop) -> System Access Layer (the
TOOLS dict below, thin wrappers over sync.py) -> the real system (brain.py
/ min_scanner.py, pulled fresh from the repo, never duplicated here).

The model is the orchestrator of information, not the database — it never
gets raw JSON dumped into the prompt. It only ever sees what a named tool
call returns, and only when it asks for that tool.

V0 SCOPE (deliberate): single-turn per message, no persisted conversation
memory across messages — the "remember earlier in this chat" layer is a
separate, later piece (needs the WorldState conversation/trade_reasoning
keys designed, per brain.py's own reserved-placeholder note). Answers are
also bounded by whatever the scanner last committed to the repo — this
process never talks to Twelve Data or recomputes anything live.
"""
import os
import json
import traceback

import requests
from flask import Flask, request, jsonify
import anthropic

import sync

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Optional but recommended — set the same value when calling Telegram's
# setWebhook(secret_token=...); Telegram echoes it back on every request
# as this header, so anyone who guesses the URL without the secret gets
# ignored. Not required to run, just safer.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """\
You are the conversational Assistant sitting on top of Valentine's GBPUSD \
SMC trading system (scanners -> WorldState -> Brain -> you). You are not \
another Brain and you do not do new market analysis — you read what the \
system has already computed via your tools and explain it.

Behavioral rules, non-negotiable:
- Never manufacture system state. If a tool doesn't have something, say so \
plainly instead of guessing or extrapolating.
- Never describe an inference as a recorded observation. Keep "what the \
data shows" and "what I think that means" visibly separate.
- The system's thesis/intent language is deliberately hypothesis-framed, \
never a verdict ("bullish"/"bearish" as a fact) — preserve that framing, \
don't collapse it into a prediction.
- If asked "is that a trade?" — don't decide. Report what the tools say \
about intent/setup status; the system's own gates decide, not you.
- Be direct and concise by default. Only go deep (full situation report, \
reasoning trace) if asked.
- You're allowed to say a question can't be answered from what's \
currently persisted — that's a true and useful answer, not a failure.
- Call get_market_briefing first for almost anything about "what's \
happening" or "what are you thinking" — it's the cheapest, most complete \
current-state tool. Only reach for get_world_state (raw, verbose) if the \
briefing doesn't have the specific field you need. Use \
get_recent_analysis_changes for "why did X change" / "what's different \
since earlier" questions.
"""

TOOLS = [
    {
        "name": "get_market_briefing",
        "description": (
            "Current market thesis + logic + intent hypothesis + "
            "confirmation/invalidation conditions, assembled from the "
            "system's own build_market_briefing(). This is the primary "
            "'what's happening / what are you thinking' tool — start here."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_world_state",
        "description": (
            "The full raw WorldState tree (phase, structure, system "
            "activity, coherence/freshness of every source file). Verbose "
            "— only use this if get_market_briefing doesn't have the "
            "specific fact you need."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_analysis_changes",
        "description": (
            "Recent Brain interpretations logged over time (brain_log.jsonl), "
            "each tagged with which WorldState snapshot it came from. Use "
            "for 'why did the signal weaken' / 'what changed since earlier' "
            "questions. KNOWN LIMITATION: this log currently only gets a "
            "new entry when someone asks /understand on the bot directly — "
            "it is NOT written every scan yet, so there may be gaps. Say so "
            "if the entries you get don't cover the period being asked about."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "how many recent entries to pull, default 5"}
            },
        },
    },
    {
        "name": "get_system_snapshot",
        "description": (
            "Coarse system health: last known stats.json contents and how "
            "many analysis log entries exist. Use for 'is everything "
            "running ok' / 'is the bot alive' questions, not market questions."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _tool_get_market_briefing(mods):
    state = sync.load_json("state.json", default={})
    ws = mods["min_scanner"].build_world_state(state)
    briefing = mods["brain"].build_market_briefing(ws)
    return briefing


def _tool_get_world_state(mods):
    state = sync.load_json("state.json", default={})
    return mods["min_scanner"].build_world_state(state)


def _tool_get_recent_analysis_changes(mods, n=5):
    entries = sync.load_jsonl_tail("brain_log.jsonl", n=n)
    return {
        "entries": entries,
        "note": (
            "brain_log.jsonl only gains an entry when /understand is sent "
            "to the bot directly — treat gaps between entries as 'no "
            "record', not 'nothing happened'."
        ),
    }


def _tool_get_system_snapshot(mods):
    stats = sync.load_json("stats.json", default={})
    brain_entries = sync.load_jsonl_tail("brain_log.jsonl", n=1000)
    return {
        "stats": stats,
        "brain_log_entry_count": len(brain_entries),
    }


TOOL_IMPL = {
    "get_market_briefing": _tool_get_market_briefing,
    "get_world_state": _tool_get_world_state,
    "get_recent_analysis_changes": _tool_get_recent_analysis_changes,
    "get_system_snapshot": _tool_get_system_snapshot,
}


def run_assistant(user_text):
    sync.sync()
    mods = sync.import_modules()

    messages = [{"role": "user", "content": user_text}]

    for _ in range(6):  # hard cap on tool round-trips per message
        resp = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            impl = TOOL_IMPL.get(block.name)
            try:
                result = impl(mods, **(block.input or {})) if impl else {"error": f"unknown tool {block.name}"}
            except Exception as e:
                result = {"error": str(e)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    return "I went through several tool calls and still don't have a clean answer — something's off, worth checking the logs directly."


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Telegram message cap is 4096 chars — split rather than truncate.
    for i in range(0, len(text), 4000):
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[i:i + 4000],
            "parse_mode": "Markdown",
        }, timeout=10)


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return jsonify({"ok": True})

    chat_id = str(msg.get("chat", {}).get("id"))
    text = msg.get("text")
    if chat_id != str(TELEGRAM_CHAT_ID) or not text:
        return jsonify({"ok": True})  # ignore anyone but Valentine, silently

    try:
        reply = run_assistant(text)
    except Exception:
        reply = "Hit an error answering that:\n```\n" + traceback.format_exc()[-1500:] + "\n```"

    send_telegram(reply)
    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
def health():
    return "Assistant server is up."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
