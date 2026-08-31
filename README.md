# Assistant Server — v0

A separate, always-on service (Render) that gives you a real Telegram
conversation with the trading system, sitting on top of `brain.py` /
`min_scanner.py` exactly as designed — it never reimplements them, it
pulls them fresh from https://github.com/junniorpaul272-beep/Tradingbot
on every message.

## Files
- `server.py` — Flask webhook + Claude tool-use loop + Telegram reply
- `sync.py` — pulls code + state files from the repo, imports them
- `requirements.txt`

## Deploy (Render, free tier)
1. Push these 3 files to their own small repo (don't mix into Tradingbot —
   different deploy target, different requirements.txt).
2. Render dashboard → New → Web Service → connect that repo.
3. Build command: `pip install -r requirements.txt`
   Start command: `gunicorn server:app`
4. Environment variables (Render → Environment):
   - `TELEGRAM_TOKEN` — same token the scanner uses
   - `TELEGRAM_CHAT_ID` — same chat id the scanner uses
   - `ANTHROPIC_API_KEY` — new, from console.anthropic.com
   - `WEBHOOK_SECRET` — any random string you make up (recommended)
5. Deploy. Copy the `https://<your-service>.onrender.com` URL Render gives you.
6. One-time, from your own machine/Termux (replace the bracketed parts):

   ```
   curl -X POST "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook" \
     -d "url=https://<your-service>.onrender.com/telegram-webhook" \
     -d "secret_token=<same WEBHOOK_SECRET>"
   ```

7. Message the bot. First message after any 15+ min gap will be slow
   (Render free tier cold start, ~30-60s) — that's the tradeoff for $0/mo,
   not a bug.

## Known limitations of this v0 (stated plainly, not hidden)
- **No multi-turn memory.** Each message is answered fresh. "Remember
  what we were just talking about" doesn't work yet — that needs the
  WorldState `conversation`/`trade_reasoning` keys brain.py already
  reserves but hasn't designed.
- **`get_recent_analysis_changes` has real gaps.** `brain_log.jsonl` only
  gets a new entry when `/understand` is sent directly to the bot on
  Telegram — not every scan. The tool says this to the model, so the
  Assistant should tell you when it doesn't have coverage rather than
  guessing. Fix (separate, small change to `scanner_live.py`): call
  `_append_brain_log()` on every scan pass, not only the `/understand`
  branch — worth doing before leaning on this tool much.
- **Twelve Data / live candles are never touched by this service.**
  Everything it answers from is whatever the scanner last committed —
  by design, not an oversight, but it means the Assistant can lag the
  market by up to one scan interval.
- Only 4 tools exist (`get_market_briefing`, `get_world_state`,
  `get_recent_analysis_changes`, `get_system_snapshot`). The "department"
  tools from the architecture discussion (evidence status, integrity,
  investigate a setup) are real, buildable next steps — `compute_evidence`,
  `scanner_integrity.py`, and the Trade Investigation Bureau's
  `investigate_failure_case()` all already exist and are natural tools to
  add next, same wrapper pattern as the 4 here.
