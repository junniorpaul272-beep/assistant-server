"""
sync.py — pulls the CURRENT code and state files straight from the public
Tradingbot repo (raw.githubusercontent.com) into this service's local
working directory, then (re)imports them.

WHY THIS EXISTS (read this before changing it): the Assistant server is a
separate deployment from the scanner (Render vs GitHub Actions/cronjob.com).
It must never carry its own frozen copy of brain.py / min_scanner.py, or
the two will drift the moment either side is edited without remembering to
copy the other. Instead, every sync pulls the actual files that live in
the one real repo. Single source of truth stays the repo; this service has
no code or state of its own beyond this file and server.py.

Code files (scanner_common.py, scanner_observation.py, system_ledger.py,
brain.py, min_scanner.py) are read-only, pure-function modules as far as
this service is concerned — it only ever CALLS build_world_state() /
build_market_briefing() / build_market_understanding(), never anything
that writes back to state. Nothing here re-runs the scanner or touches
Telegram's getUpdates queue that Live/MIN's own cron jobs already own —
doing so would race with them over the same offset.

TELEGRAM_TOKEN / TELEGRAM_CHAT_ID must be set as env vars before importing
min_scanner (scanner_common.py reads them at import time via
os.environ[...] and hard-crashes on import if missing — same contract the
scanner's own workflows already rely on). TWELVE_DATA_KEY is deliberately
NOT required here: build_world_state()/build_market_briefing() are pure
functions of already-persisted state, confirmed by reading the code
directly, not assumed.
"""
import os
import sys
import json
import time
import importlib
import requests

REPO_OWNER = "junniorpaul272-beep"
REPO_NAME = "Tradingbot"
BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/"

WORK_DIR = os.path.dirname(os.path.abspath(__file__))

# The exact import closure build_world_state()/build_market_briefing() need
# — confirmed by reading each file's own import lines, not guessed:
#   min_scanner.py imports scanner_common, system_ledger, scanner_observation, brain
#   scanner_observation.py imports scanner_common
#   brain.py imports nothing local (enforced by its own module docstring)
CODE_FILES = [
    "scanner_common.py",
    "scanner_observation.py",
    "system_ledger.py",
    "brain.py",
    "min_scanner.py",
]

# Every file build_world_state() / its load_*() helpers read from disk via
# BASE_DIR-relative paths (confirmed against scanner_common.py's own
# STATE_FILE/SHADOW_STATE_FILE/etc. constants) — NOT an assumed list.
DATA_FILES = [
    "state.json",
    "stats.json",
    "shadow_state.json",
    "shadow_stats.json",
    "market_intent_state.json",
    "leg_obs_state.json",
    "markov_transitions.json",
    "sr_levels_state.json",
    "bank_transactions.jsonl",
    "brain_log.jsonl",
    "briefing_log.jsonl",
]

_last_sync = 0
SYNC_MIN_INTERVAL_SEC = 20  # don't hammer raw.githubusercontent.com on every message


def _fetch(path):
    url = RAW_BASE + path
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.content


def sync(force=False):
    """
    Downloads every code + data file into WORK_DIR. Safe to call on every
    webhook hit — rate-limited to SYNC_MIN_INTERVAL_SEC unless force=True.
    Missing data files (e.g. a log that doesn't exist yet) are skipped,
    not fatal — code files ARE fatal if missing, since nothing works
    without them.
    """
    global _last_sync
    now = time.time()
    if not force and (now - _last_sync) < SYNC_MIN_INTERVAL_SEC:
        return False
    _last_sync = now

    for fname in CODE_FILES:
        content = _fetch(fname)  # raises if a code file is missing/renamed
        with open(os.path.join(WORK_DIR, fname), "wb") as f:
            f.write(content)

    for fname in DATA_FILES:
        try:
            content = _fetch(fname)
        except requests.HTTPError:
            continue  # file doesn't exist yet (e.g. brain_log.jsonl before first /understand) — fine
        with open(os.path.join(WORK_DIR, fname), "wb") as f:
            f.write(content)

    return True


def import_modules():
    """
    (Re)imports the synced code files fresh each time — so a code change
    pushed to the repo takes effect on the next sync without redeploying
    this service. sys.path already includes WORK_DIR when running from
    here; this just forces a reload if the modules were already imported
    once in this process.
    """
    if WORK_DIR not in sys.path:
        sys.path.insert(0, WORK_DIR)

    names = ["scanner_common", "scanner_observation", "system_ledger", "brain", "min_scanner"]
    mods = {}
    for name in names:
        if name in sys.modules:
            mods[name] = importlib.reload(sys.modules[name])
        else:
            mods[name] = importlib.import_module(name)
    return mods


def load_json(fname, default=None):
    path = os.path.join(WORK_DIR, fname)
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def load_jsonl_tail(fname, n=20):
    path = os.path.join(WORK_DIR, fname)
    if not os.path.exists(path):
        return []
    lines = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(line)
    tail = lines[-n:]
    out = []
    for line in tail:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
