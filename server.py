"""
server.py — the Assistant. Always-on webhook receiver (this is the piece
GitHub Actions/cronjob.com structurally cannot be — nothing wakes it up,
it's just listening).

ARCHITECTURE (unchanged from the Claude version — only the LLM layer
changed): Interface (Telegram) -> Conversation layer (Gemini + automatic
function calling) -> System Access Layer (the tool functions below, thin
wrappers over sync.py) -> the real system (brain.py / min_scanner.py,
pulled fresh from the repo, never duplicated here).

The model is the orchestrator of information, not the database — it never
gets raw JSON dumped into the prompt. It only ever sees what a named tool
call returns, and only when it asks for that tool.

WHY GEMINI HANDLES THE TOOL LOOP ITSELF: the google-genai SDK's automatic
function calling accepts plain Python functions directly as `tools` and
runs the whole ask -> call -> feed-result-back -> answer loop internally
(see AutomaticFunctionCallingConfig). That replaces the ~35 lines of
manual message-history bookkeeping the Claude version needed — same
architecture, less code to maintain.

ONE RUNTIME PER MESSAGE: sync + import happen once per incoming message,
and the four tool functions are built as closures over that one `mods`
dict (see build_tools()) so every tool call within a single answer reads
the same synced snapshot — never a re-sync mid-answer.

V0 SCOPE (deliberate): single-turn per message, no persisted conversation
memory across messages — the "remember earlier in this chat" layer is a
separate, later piece (needs the WorldState conversation/trade_reasoning
keys designed, per brain.py's own reserved-placeholder note). Answers are
also bounded by whatever the scanner last committed to the repo — this
process never talks to Twelve Data or recomputes anything live.
"""
import os
import traceback

import requests
from flask import Flask, request, jsonify
from google import genai
from google.genai import types

import sync

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# Overridable so you can swap models without a code change — you'll need
# this again; Google retires/renames these fast. Currently gemini-3.6-flash
# (2.5-flash was retired for new users as of this deploy). If you're
# hitting free-tier rate limits, gemini-3.5-flash-lite is the cheaper
# fallback — check ai.google.dev/gemini-api/docs/models for what's current
# before assuming either name still works.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
# Optional but recommended — set the same value when calling Telegram's
# setWebhook(secret_token=...); Telegram echoes it back on every request
# as this header, so anyone who guesses the URL without the secret gets
# ignored. Not required to run, just safer.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

client = genai.Client(api_key=GEMINI_API_KEY)

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

# Hard cap on tool round-trips per message. Passed to Gemini's automatic
# function calling as maximum_remote_calls (it counts total model turns,
# so +1 vs. the old Claude loop's "6 tool round-trips" to leave room for
# the final answer turn).
MAX_TOOL_TURNS = 7


def build_tools(mods):
    """
    Returns plain Python functions closing over one synced `mods` dict —
    this is the one-runtime-per-message guarantee. Gemini never sees
    `mods`; the SDK derives each tool's schema from the function's type
    hints and docstring, so both need to stay accurate.
    """

    def get_market_briefing() -> dict:
        """Current market thesis + logic + intent hypothesis + confirmation/
        invalidation conditions, assembled from the system's own
        build_market_briefing(). This is the primary 'what's happening /
        what are you thinking' tool — start here for almost any question.
        """
        state = sync.load_json("state.json", default={})
        ws = mods["min_scanner"].build_world_state(state)
        return mods["brain"].build_market_briefing(ws)

    def get_world_state() -> dict:
        """The full raw WorldState tree (phase, structure, system activity,
        coherence/freshness of every source file). Verbose — only use this
        if get_market_briefing doesn't have the specific fact you need.
        """
        state = sync.load_json("state.json", default={})
        return mods["min_scanner"].build_world_state(state)

    def get_recent_analysis_changes(n: int = 5) -> dict:
        """Recent Brain interpretations logged over time (brain_log.jsonl),
        each tagged with which WorldState snapshot it came from. Use for
        'why did the signal weaken' / 'what changed since earlier'
        questions. KNOWN LIMITATION: this log currently only gets a new
        entry when someone asks /understand on the bot directly — it is
        NOT written every scan yet, so there may be gaps. Say so if the
        entries returned don't cover the period being asked about.

        Args:
            n: how many recent entries to pull, default 5.
        """
        entries = sync.load_jsonl_tail("brain_log.jsonl", n=n)
        return {
            "entries": entries,
            "note": (
                "brain_log.jsonl only gains an entry when /understand is sent "
                "to the bot directly — treat gaps between entries as 'no "
                "record', not 'nothing happened'."
            ),
        }

    def get_system_snapshot() -> dict:
        """Coarse system health: last known stats.json contents and how many
        analysis log entries exist. Use for 'is everything running ok' /
        'is the bot alive' questions, not market questions.
        """
        stats = sync.load_json("stats.json", default={})
        brain_entries = sync.load_jsonl_tail("brain_log.jsonl", n=1000)
        return {
            "stats": stats,
            "brain_log_entry_count": len(brain_entries),
        }

    return [get_market_briefing, get_world_state, get_recent_analysis_changes, get_system_snapshot]


def run_assistant(user_text):
    sync.sync()
    mods = sync.import_modules()
    tools = build_tools(mods)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=MAX_TOOL_TURNS,
            ),
        ),
    )

    text = getattr(response, "text", None)
    if text:
        return text

    # Automatic function calling hit its cap, or the model returned only
    # function-call parts with no final text turn (or got safety-blocked).
    finish_reason = None
    if getattr(response, "candidates", None):
        finish_reason = getattr(response.candidates[0], "finish_reason", None)
    return (
        "I went through several tool calls and still don't have a clean "
        f"answer (finish_reason={finish_reason}) — something's off, worth "
        "checking the logs directly."
    )


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
    except Exception as e:
        # Full trace goes to Render's logs (stderr), not to Telegram — a
        # bare traceback in chat leaks filesystem paths and internals for
        # no benefit once this isn't actively being debugged turn-by-turn.
        traceback.print_exc()
        reply = (
            f"Something broke while I was checking that ({type(e).__name__}: {e}). "
            "The system itself may still be fine — this looks like an Assistant-side "
            "error. Check Render logs for the full trace."
        )

    send_telegram(reply)
    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
def health():
    return "Assistant server is up."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
