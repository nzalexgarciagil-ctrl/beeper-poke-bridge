# Beeper → Poke Bridge

Make [Poke](https://poke.com) proactive about **all** your messaging, not just
email. This bridge listens to every chat in [Beeper](https://www.beeper.com)
(WhatsApp, Discord, Telegram, Instagram, Signal, iMessage, …), runs each
incoming message through a fast LLM **gatekeeper**, and — only when something
genuinely deserves your attention — pings Poke to give you a heads-up and draft
a reply in your voice.

The whole point is **silence by default**. The gate is tuned to protect your
attention: banter, memes, group side-chatter, and cold DMs stay invisible; a
landlord chasing rent or a friend saying "I'm outside, where are you?" gets
through.

```
Beeper Desktop  ──ws──▶  bridge.py  ──▶  gatekeeper (LLM triage)  ──pass──▶  Telegram ▶ Poke bot
   (all chats)            debounce,          high-bar, default-silent          (heads-up +
                          self/own-msg                                          in-voice draft)
                          filtering
```

## How it works

1. **Listen** — `bridge.py` subscribes to Beeper Desktop's local WebSocket API
   for all chats.
2. **Cheap filters** — drops your own messages, reactions, call notices,
   emoji-only bursts, and chats you're already actively replying in. Rapid-fire
   messages from one chat are debounced into a single event.
3. **Gate** — `gatekeeper.py` asks an LLM one question: *is this worth
   interrupting the owner right now?* Adapted from Poke's own email-triage
   prompt. Returns JSON `{justification, take_action, summary}`.
4. **Handoff** — on a pass, the bridge messages the Poke bot on Telegram with a
   heads-up and asks Poke to read the chat (via its Beeper MCP) and draft a
   reply in your voice. **Draft only — the bridge never sends messages to anyone
   but the Poke bot.**

## Requirements

- **Beeper Desktop**, running, with the local Desktop API enabled (Settings →
  Developer). The bridge talks to `localhost:23373`.
- A **Telegram account** (the one you DM the Poke bot from) and a Telegram API
  ID/hash from <https://my.telegram.org>.
- An **OpenAI-compatible LLM endpoint** + key (OpenAI, OpenRouter, a local
  server — anything that speaks the chat-completions API).
- **Python 3.10+**. [`uv`](https://docs.astral.sh/uv/) recommended (handles deps
  automatically); plain `pip` works too.

## Setup

```bash
git clone <your-fork> poke-bridge && cd poke-bridge
cp .env.example .env          # then fill it in (see Configuration)
```

Authorize Telegram once (interactive — asks for your phone number + login code).
This creates the session file so the detached run never needs a console:

```bash
# with uv:
uv run --with-requirements requirements.txt python bridge.py --login
# or with a venv:
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python bridge.py --login
```

Then run it:

```bash
uv run --with-requirements requirements.txt python bridge.py
# or: ./run-bridge.sh   (macOS/Linux)   |   run-bridge.bat (Windows)
```

You should see `Connected to Beeper WebSocket` and `Subscribed to all chats`.

### Letting Poke read your chats (the tunnel)

For Poke to read a chat and draft a reply, it needs access to Beeper's MCP. The
Poke CLI exposes your local Beeper MCP to Poke's cloud:

```bash
npm i -g poke && poke login
./poke-tunnel.sh                     # macOS/Linux
#   or  poke-tunnel.vbs / poke-tunnel.bat on Windows
```

Override the endpoint with `POKE_MCP_URL` if your Beeper version serves the MCP
elsewhere. The bridge works without this, but Poke won't be able to read history
to draft in-voice replies.

## Configuration

All config lives in `.env` (see `.env.example` for the annotated template):

| Variable | Required | Description |
|---|---|---|
| `OWNER_NAME` | yes | Who the bridge triages for; injected into the gate prompt. |
| `BEEPER_TOKEN` | yes | Beeper Desktop API token. |
| `POKE_BEEPER_CHAT_ID` | recommended | Beeper room ID of your Telegram↔Poke chat, so it's never fed back into Poke. |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | yes | From my.telegram.org. |
| `LLM_API_KEY` | yes | Key for your LLM provider (`EIGHTSTATE_API_KEY` / `OPENAI_API_KEY` also accepted). |
| `LLM_BASE_URL` | no | Defaults to `https://api.openai.com/v1`. |
| `GATEKEEPER_MODEL` | no | Defaults to `gpt-4o-mini`. Use a cheap, fast model. |
| `BEEPER_HOST` / `BEEPER_API_PORT` | no | Override Beeper's local API location. |
| `POKE_TELEGRAM_USERNAME` | no | Poke's Telegram bot (default `interaction_poke_bot`). |

### Finding `POKE_BEEPER_CHAT_ID`

Open your Telegram chat with the Poke bot inside Beeper and copy its chat/room
ID (looks like `!xxxxxxxxxxxx:beeper.local`). This lets the bridge (a) ignore
Poke's own messages and (b) detect when you're manually talking to Poke and hold
notifications until that conversation goes quiet.

## Keeping it running

The bridge auto-reconnects to Beeper, but if the **process** is killed (sleep,
logoff, OOM), something has to restart it. Use a supervisor:

- **Linux** — `deploy/systemd/poke-bridge.service` (`Restart=always`)
- **macOS** — `deploy/launchd/co.poke.bridge.plist` (`KeepAlive`)
- **Windows** — `deploy/windows/README.md` (a 1-minute heartbeat watchdog; note
  that Task Scheduler's restart-on-failure does *not* work with the detached
  launcher, so use the watchdog).

The bridge writes `.bridge-heartbeat` (a unix timestamp) every 30s; the Windows
watchdog (`watchdog.ps1`) relaunches the bridge if it goes stale. A
single-instance lock (`.bridge.lock`) guarantees only one bridge ever runs, so
redundant launches from a supervisor are harmless.

## Tuning the gate

Behaviour is almost entirely in the `gatekeeper.py` system prompt. Edit it to
change what earns an interruption. `gatekeeper_eval.py` runs a labelled set of
messages through one or more models and reports accuracy + latency:

```bash
uv run --with-requirements requirements.txt python gatekeeper_eval.py
# compare models: EVAL_MODELS="gpt-4o-mini,gpt-4.1-nano" ...python gatekeeper_eval.py
```

## Privacy & safety

- **Your messages are sent to your LLM provider.** Every message that passes the
  cheap filters is POSTed to `LLM_BASE_URL` for triage. Point it at a provider
  (or local model) you trust. Nothing is sent anywhere else.
- The bridge's **only outbound action** is messaging the Poke bot on Telegram.
  It never replies to third parties — Poke's Beeper access is read-only and
  drafts are for you to copy-paste.
- On an LLM outage the gate **fails open for 1:1 DMs** (you still get pinged) and
  **fails closed for group chats** (so an outage can't blast a busy group).
- **Never commit `.env` or `*.session`.** The session file is a logged-in
  Telegram session — treat it like a password. `.gitignore` already covers both.

## Files

| File | Purpose |
|---|---|
| `bridge.py` | Main process: Beeper listener, filters, debounce, Telegram handoff. |
| `gatekeeper.py` | LLM triage gate + prompt. |
| `gatekeeper_eval.py` | Offline accuracy/latency eval for the gate. |
| `run-bridge.*` | Launchers (`.sh`, `.bat`, `.vbs`). |
| `deploy/` | systemd / launchd / Windows supervisor configs. |

## License

MIT (see `LICENSE`).
