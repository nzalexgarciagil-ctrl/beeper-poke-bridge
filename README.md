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
3. **Gate** — the gatekeeper (in `bridge.py`) asks an LLM one question: *is this
   worth interrupting the owner right now?* Adapted from Poke's own email-triage
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

## Quickstart

Three steps. The setup script asks for your credentials and writes `.env` for
you — no file editing.

```bash
git clone <your-fork> beeper-poke-bridge && cd beeper-poke-bridge

# 1. Paste in your credentials (name, Beeper token, Telegram API id/hash, LLM key)
python configure.py

# 2. Authorize Telegram once (phone number + login code)
python bridge.py --login

# 3. Run it (or: uv run --with-requirements requirements.txt python bridge.py)
python bridge.py     # Windows always-on: see "Keeping it running" below
```

(`configure.py` only uses the standard library; if you hit an import error, run
it through `uv run --with-requirements requirements.txt python configure.py`.)

You should see `Connected to Beeper WebSocket` and `Subscribed to all chats`.
If you forget a credential, the bridge tells you exactly which one. To keep it
running in the background, see [Keeping it running](#keeping-it-running).

> Where to get each credential: **Beeper token** → Beeper Desktop → Settings →
> Developer. **Telegram API id/hash** → <https://my.telegram.org> → API
> development tools. **LLM key** → your OpenAI-compatible provider.

## Setup (manual)

Prefer to edit the file yourself? Copy the template and fill it in:

```bash
cp .env.example .env          # then edit it (see Configuration below)
uv run --with-requirements requirements.txt python bridge.py --login
uv run --with-requirements requirements.txt python bridge.py
```

Everything works the same with a plain `pip install -r requirements.txt` in a venv.

### Letting Poke read your chats (the tunnel)

For Poke to read a chat and draft a reply, it needs access to Beeper's MCP. The
Poke CLI exposes your local Beeper MCP to Poke's cloud — one command, leave it
running alongside the bridge:

```bash
npm i -g poke && poke login
poke tunnel http://localhost:23375/v0/mcp -n "Beeper Desktop"
```

The bridge works without this, but Poke won't be able to read history to draft
in-voice replies. (On Windows, wrap that command in a `.vbs`/Task Scheduler entry
if you want it windowless and persistent.)

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
logoff, OOM), something has to restart it. It writes `.bridge-heartbeat` every
30s and holds a single-instance lock (`.bridge.lock`), so redundant launches from
a supervisor are harmless. Pick your platform:

**Windows** — `watchdog.ps1` both launches the bridge (windowless) and keeps it
alive; `run-watchdog-hidden.vbs` runs the watchdog itself with no console window.
Task Scheduler's own restart-on-failure can't help here, because the bridge runs
detached in the background and the task "completes" instantly — so the watchdog
polls the heartbeat instead. Register it to run every minute:

```bat
schtasks /Create /TN PokeBridge ^
  /TR "wscript.exe \"C:\path\to\run-watchdog-hidden.vbs\"" ^
  /SC MINUTE /MO 1 /RU %USERNAME% /IT /RL LIMITED /F
```

It starts the bridge within a minute of logon and relaunches it within a minute
of any death. If cold starts are slow, exclude the uv dirs from Defender
(elevated PowerShell):

```powershell
Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\uv","$env:APPDATA\uv"
```

**Linux** — a systemd user unit:

```ini
[Unit]
Description=Beeper -> Poke bridge
After=network-online.target
[Service]
WorkingDirectory=%h/beeper-poke-bridge
ExecStart=%h/beeper-poke-bridge/.venv/bin/python bridge.py
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
```

**macOS** — a launchd agent with `<key>KeepAlive</key><true/>` pointing
`ProgramArguments` at your venv's `python bridge.py`, with `RunAtLoad` true.

## Tuning the gate

Behaviour is almost entirely in the gate system prompt — the `_gate_system_prompt`
function near the top of `bridge.py`. Edit it to change what earns an
interruption: who counts as the owner, how high the bar is, what always passes or
always stays silent.

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
| `bridge.py` | The whole bridge: Beeper listener, filters, debounce, single-instance lock, the LLM gate, and the Telegram handoff. |
| `configure.py` | Interactive setup — collects credentials and writes `.env`. |
| `watchdog.ps1` | Windows: starts the bridge windowless **and** relaunches it if the heartbeat goes stale. |
| `run-watchdog-hidden.vbs` | Windows: runs the watchdog with no console window (used by the scheduled task). |
| `requirements.txt` / `.env.example` | Dependencies and the config template. |

## License

MIT (see `LICENSE`).
