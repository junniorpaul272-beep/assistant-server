"""
sync.py — pulls the CURRENT code and state files straight from the
Tradingbot repo into this service's local working directory, then
(re)imports them.

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

SNAPSHOT CONSISTENCY: every sync() call resolves ONE commit SHA first and
fetches every code + data file at that exact SHA. Without this, a scanner
commit landing mid-sync could hand the Assistant brain.py from commit A
paired with state.json from commit B. If the SHA lookup itself fails
(rate limited, network hiccup), sync() falls back to fetching branch HEAD
directly — files may be a few seconds inconsistent in that fallback case,
which is the same risk this file always carried before this change.

GITHUB_TOKEN (optional): if set, every request — the SHA lookup and every
file fetch — goes through the authenticated GitHub Contents API instead
of the anonymous raw.githubusercontent.com CDN. Two reasons to set it:
  1. Rate limit: unauthenticated GitHub API requests are capped at 60/hr
     per IP; SYNC_MIN_INTERVAL_SEC=20 alone can burn through that. A
     token raises the ceiling to 5000/hr.
  2. If Tradingbot ever becomes a private repo (it should — see the repo
     visibility note below), this is the only reliable way to keep
     reading it; raw.githubusercontent.com does not support token auth
     for private repos.
"""
import os
import sys
import json
import time
import base64
import importlib
import requests

REPO_OWNER = "junniorpaul272-beep"
REPO_NAME = "Tradingbot"
BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/"
API_BASE = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional, see module docstring

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
#
# ⚠ SECURITY: bank_transactions.jsonl lives in this list because the Brain
# reads it. If Tradingbot is a PUBLIC repo, this file (and every state/log
# file below) is publicly downloadable by anyone who finds the repo — no
# auth required. Confirm the repo's visibility and, if it's public, either
# make it private (then set GITHUB_TOKEN) or stop committing financial
# data to it before anything else in this file matters.
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
SYNC_MIN_INTERVAL_SEC = 20  # don't hammer the GitHub API/CDN on every message


def _get_commit_sha():
    """Resolve BRANCH to one commit SHA so every file in this sync comes
    from the same snapshot. Raises on failure — callers must catch."""
    headers = {"Accept": "application/vnd.github.sha"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    resp = requests.get(f"{API_BASE}/commits/{BRANCH}", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.text.strip()


def _fetch(path, sha):
    """Fetch one file's bytes at `sha` (or BRANCH HEAD if sha is None)."""
    if GITHUB_TOKEN:
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.raw",
        }
        params = {"ref": sha} if sha else {}
        resp = requests.get(f"{API_BASE}/contents/{path}", headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.content
    else:
        url = RAW_BASE + (sha or BRANCH) + "/" + path
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.content


def sync(force=False):
    """
    Downloads every code + data file into WORK_DIR, all from one resolved
    commit SHA. Safe to call on every webhook hit — rate-limited to
    SYNC_MIN_INTERVAL_SEC unless force=True. Missing data files (e.g. a
    log that doesn't exist yet) are skipped, not fatal — code files ARE
    fatal if missing, since nothing works without them.
    """
    global _last_sync
    now = time.time()
    if not force and (now - _last_sync) < SYNC_MIN_INTERVAL_SEC:
        return False
    _last_sync = now

    try:
        sha = _get_commit_sha()
    except requests.RequestException as e:
        print(f"sync: could not resolve commit SHA ({e}); falling back to {BRANCH} HEAD per-file "
              f"— files in this sync may not all be from the same commit.")
        sha = None

    for fname in CODE_FILES:
        content = _fetch(fname, sha)  # raises if a code file is missing/renamed
        with open(os.path.join(WORK_DIR, fname), "wb") as f:
            f.write(content)

    for fname in DATA_FILES:
        try:
            content = _fetch(fname, sha)
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
