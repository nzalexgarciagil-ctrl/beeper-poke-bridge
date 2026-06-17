"""
Beeper -> Poke Bridge

Listens to Beeper's WebSocket stream for incoming messages across all platforms
(WhatsApp, Discord, Telegram, Instagram, etc.) and runs a two-stage pipeline:

  1. Gatekeeper (below): a fast LLM triage call -- modelled on Poke's
     own email triage -- decides whether a message is important enough to
     interrupt the owner. If not, the bridge stays completely silent.
  2. Handoff: for messages that clear the gate, the bridge texts Poke a heads-up
     over iMessage and asks it to draft an in-voice reply (Poke reads the chat
     via its Beeper MCP). Draft-only; the bridge never sends replies to third
     parties.

Before the gate, cheap filters drop self-messages, noise (call notices),
emoji-only batches, and chats the owner is already actively replying in.

The handoff transport is selectable via HANDOFF_TRANSPORT in .env:
  - "imessage" (default): texts Poke through the macOS Messages app (AppleScript);
    requires macOS signed in to iMessage, no login step.
  - "telegram": a Telegram user session messages the Poke bot; run
    `python bridge.py --login` once to create the session.

Config is loaded from a local .env (see .env.example).
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import ctypes
from datetime import datetime
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import requests
from openai import OpenAI
import websockets

from dotenv import load_dotenv

# Load secrets from .env before anything reads them.
load_dotenv(Path(__file__).with_name(".env"))

HERE = Path(__file__).resolve().parent


def _env(*names: str, default: str = "") -> str:
    """Return the first set environment variable among names."""
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return default


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Beeper Desktop local API. Ports are configurable in case Beeper changes them
# or you run it behind a proxy.
BEEPER_HOST = os.environ.get("BEEPER_HOST", "localhost")
BEEPER_API_PORT = os.environ.get("BEEPER_API_PORT", "23373")
BEEPER_WS = f"ws://{BEEPER_HOST}:{BEEPER_API_PORT}/v1/ws"
BEEPER_REST = f"http://{BEEPER_HOST}:{BEEPER_API_PORT}"
BEEPER_TOKEN = os.environ.get("BEEPER_TOKEN", "")

OWNER_NAME = _env("OWNER_NAME", default="the owner")

# --- Handoff transport: how the bridge reaches Poke -------------------------
# "imessage" -> macOS Messages (AppleScript), no extra account. (default)
# "telegram" -> a Telegram user session messaging the Poke bot.
HANDOFF_TRANSPORT = _env("HANDOFF_TRANSPORT", default="imessage").lower()

# iMessage handoff target: Poke's iMessage number (or any iMessage handle/email).
# Defaults to Poke's public number; override in .env if yours differs.
POKE_IMESSAGE_HANDLE = os.environ.get("POKE_IMESSAGE_HANDLE", "+16503347837").strip()

# Telegram handoff (only used when HANDOFF_TRANSPORT=telegram).
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH")
TELEGRAM_SESSION = str(HERE / os.environ.get("TELEGRAM_SESSION_NAME", "telegram-poke-bridge"))
POKE_TELEGRAM_USERNAME = os.environ.get("POKE_TELEGRAM_USERNAME", "interaction_poke_bot")

# Keep handoffs bounded so a runaway batch can't produce a multi-screen message.
# 4000 also stays under Telegram's 4096 hard limit when that transport is used.
MAX_HANDOFF_MESSAGE = int(os.environ.get("MAX_HANDOFF_MESSAGE", "4000"))
CONTEXT_LOOKBACK_SECONDS = 15 * 60

# Reconnect delay range (exponential backoff)
RECONNECT_MIN = 2
RECONNECT_MAX = 60

# WebSocket keepalive -- detect a dead Beeper connection instead of hanging.
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 20

# Deduplication window: ignore message IDs seen in the last N seconds
DEDUP_WINDOW = 300
SEEN_FILE = HERE / ".bridge-seen.json"
SEEN_FILE_MAX_ITEMS = 5000
SEEN_SAVE_INTERVAL = 5  # debounce: persist the seen-cache at most this often

# Liveness heartbeat -- an external supervisor can restart the bridge if this
# file goes stale (see README for systemd/launchd/NSSM examples).
HEARTBEAT_FILE = HERE / ".bridge-heartbeat"
HEARTBEAT_INTERVAL = 30

# Single-instance lock. The detached launchers + restart-on-failure supervisors
# could otherwise spawn duplicate bridges (= duplicate Poke pings). This pidfile
# guarantees only one bridge runs the listen loop, regardless of launcher.
LOCK_FILE = HERE / ".bridge.lock"

# Logging
LOG_FILE = Path(os.environ.get("BRIDGE_LOG_FILE", str(HERE / "bridge.log")))
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3

# Avoid replaying historical Beeper upserts when the bridge starts/restarts
STARTED_AT = time.time()
STARTUP_REPLAY_GRACE = 30

# Batch rapid-fire messages from the same chat into one Poke notification.
# Also acts as the window in which the owner can start replying himself before
# we ever bother Poke. Kept long enough for a human to react to a fresh DM.
BATCH_DELAY = 20

# Never feed Poke's own chat back into Poke. This is the Beeper room ID of YOUR
# chat with Poke (the iMessage thread with Poke, as it appears in Beeper) -- it
# differs per user, so it is configured in .env (see README for how to find it).
# If unset, only the handoff marker below protects against feedback loops.
POKE_BEEPER_CHAT_ID = os.environ.get("POKE_BEEPER_CHAT_ID", "").strip()
IGNORED_CHAT_IDS = {c for c in {POKE_BEEPER_CHAT_ID} if c}

# If the owner is manually chatting with Poke, hold bridge updates until the chat
# goes quiet.
ACTIVE_POKE_PAUSE_SECONDS = 60
ACTIVE_POKE_RECHECK_SECONDS = 30

# If the owner has replied in the ORIGINATING chat (e.g. answered the DM
# himself), he is handling it -- drop any pending notification for that chat.
SELF_ACTIVE_PAUSE_SECONDS = 120

# Marker that opens every bridge -> Poke handoff (human-readable).
HANDOFF_PREFIX = "A message worth surfacing just came in."

# Invisible machine marker appended to every handoff. Lets the bridge recognise
# its own messages in the Poke chat even if HANDOFF_PREFIX wording changes, so
# its own forwards are never mistaken for the owner manually talking to Poke.
BRIDGE_MARKER = "⁣​⁣"  # invisible separator + zero-width space


def render_handoff(payload: dict, chat_id: str = "", sender_id: str = "") -> str:
    """Heads-up + in-voice draft request handed to the conversational Poke."""
    chat_name = payload.get("chat", {}).get("name", "Unknown")
    chat_type = payload.get("chat", {}).get("type", "unknown")
    sender = payload.get("from", "Unknown")
    msgs = payload.get("messages") or [{"sender": sender, "text": payload.get("text", "")}]
    verbatim = "\n".join(f"  {m.get('sender', sender)}: {m.get('text', '')}" for m in msgs)
    platform = payload.get("platform", "Unknown")
    owner = OWNER_NAME
    head = (
        f"{HANDOFF_PREFIX} It's for {owner} on {platform}, from "
        f"{sender} (chat: {chat_name} ({chat_type})).\n\n"
        "What they said:\n"
    )
    tail = (
        f"\n\nBeeper lookup -> chatID: {chat_id or 'unknown'}  senderID: {sender_id or 'unknown'}\n\n"
        "Now do this:\n"
        "1) Use your Beeper MCP to open this chat and read back through the history. Work out who "
        f"this person is to {owner}, what their relationship is, and the context behind what they're "
        "asking right now.\n"
        f"2) Study how {owner} actually talks to THIS person specifically -- the tone, length, slang, "
        "emoji, punctuation, and patterns he uses with them (which differ from how he talks to others). "
        "Gather substantive evidence from real prior messages before you write anything; don't guess at "
        "his voice.\n"
        f"3) Once you genuinely understand the relationship and have solid evidence of {owner}'s patterns "
        f"with them, draft the reply {owner} would send back -- in his voice, the way he talks to this "
        "person, calibrated to how close they are and how serious the message is. Preserve any code, "
        "link, amount, or deadline exactly. Don't ask whether to draft; just draft it.\n"
        f"4) Give {owner} a heads-up: who messaged, why they messaged, and then the drafted reply, shown "
        "clearly so he can copy-paste it.\n\n"
        "You cannot send the reply (your Beeper access is read-only) and must not try -- drafting only."
    )
    # Only the quoted message is allowed to be trimmed -- the fixed instructions
    # (and the marker) must always survive so Poke never gets a severed prompt.
    budget = MAX_HANDOFF_MESSAGE - len(BRIDGE_MARKER) - len(head) - len(tail)
    if budget < 0:
        budget = 0
    if len(verbatim) > budget:
        verbatim = verbatim[:max(budget - 1, 0)] + "…"
    return head + verbatim + tail + BRIDGE_MARKER


# ---------------------------------------------------------------------------
# Gatekeeper -- stage 1 LLM triage
#
# A cheap, fast LLM call decides whether an incoming message is relevant and
# time-sensitive enough to interrupt the owner. Adapted from Poke's own
# email-triage prompt: high bar, default silence, judgment over rules.
# ---------------------------------------------------------------------------

GATE_BASE_URL = _env("LLM_BASE_URL", "EIGHTSTATE_BASE_URL", "OPENAI_BASE_URL",
                     default="https://api.openai.com/v1")
GATE_MODEL = _env("GATEKEEPER_MODEL", "CODEX_MODEL", default="gpt-5.4-mini")
GATE_MAX_MSG_CHARS = 2000  # cap any single message fed to the gate (bounds cost/latency)

# --- Gatekeeper provider -----------------------------------------------------
# "openai"  -> standard API key against GATE_BASE_URL (chat.completions).
# "codex"   -> your ChatGPT subscription via `codex login` OAuth (Responses API).
# "auto"    -> use codex when ~/.codex/auth.json exists and no API key is set.
LLM_PROVIDER = _env("LLM_PROVIDER", default="auto").lower()
CODEX_AUTH_FILE = Path(os.environ.get("CODEX_AUTH_FILE", str(Path.home() / ".codex" / "auth.json")))
CODEX_BASE_URL = os.environ.get("CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex").rstrip("/")
CODEX_TOKEN_URL = os.environ.get("CODEX_TOKEN_URL", "https://auth.openai.com/oauth/token")
CODEX_CLIENT_ID = os.environ.get("CODEX_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann")
# Codex/subscription model for the gate. Override in .env if the default rotates.
CODEX_MODEL = _env("CODEX_MODEL", "GATEKEEPER_MODEL", default="gpt-5.4-mini")


def _gate_system_prompt(owner: str) -> str:
    return f"""You are the part of Poke that stands at the door to {owner}'s messages. Poke is a proactive personal assistant that lives in {owner}'s texts: always on, reaching out first when something actually matters and staying quiet when it does not.

A new message (or short burst of messages from one chat) just arrived on one of {owner}'s messaging accounts -- WhatsApp, Discord, Telegram, Instagram, etc. Every incoming message passes through you first. You make exactly one call: is this relevant and time-sensitive enough to interrupt {owner} about right now? You are not writing the notification and you are not replying to anyone. You decide whether a notification should fire at all, and if it should, you capture the substance Poke needs to write it.

## You are triaging for {owner} specifically
You work for {owner}, the account owner. {owner} is NOT any other person who appears in these chats -- not the friend, not the other group members. Judge every message by whether it matters to {owner} personally.

Group chats are mostly people talking to each other, not to {owner}. A message addressed to someone else by name ("Ken, can you sort the invoice?"), a reply continuing someone else's conversation, or general side-chatter {owner} is not part of, is NOT for {owner} -- stay silent. Only surface a group message when it is clearly directed at {owner}, @mentions them, asks them something, or is something they personally need to know or act on. When in doubt about whether a group message is even aimed at {owner}, assume it is not.

## The bar is high, on purpose
A notification buzzes {owner}'s phone and spends the trust they put in Poke to guard their attention. Make the message earn the interruption. When you are genuinely on the fence, stay silent. Holding back a notification you maybe should have sent is a small miss; firing one you should not have is what makes someone mute Poke. Default to silence and let only the messages that clearly deserve it through.

## Notify when the message
- Needs {owner} to do something soon to avoid being blocked, missing a deadline, or losing money/progress: a reply someone is actively waiting on, an approval, a payment, an RSVP that is genuinely open and closing soon.
- Is a real, time-sensitive request directed at {owner} that expects a prompt response (a person actually asking them something that matters).
- Reports something genuinely breaking that affects their work, money, plans, or commitments: a cancellation, an emergency, a problem with travel or an event happening soon.
- Tells them something important they almost certainly do not know yet and that changes what they should do next.
- Concerns an event or meeting starting very soon (within roughly the next two hours) that they need to be ready for.

## Skip when the message
- Is small talk, banter, jokes, reactions, memes, emoji, "lol", "bet", "gm", "you up", ongoing casual back-and-forth, or gaming/hangout chatter with no real stakes.
- Is a link, photo, or video shared with nothing {owner} must act on.
- Is group-chat traffic not actually directed at {owner} -- general chatter, others talking among themselves, a question or reply aimed at someone else by name, @everyone blasts.
- Is a non-critical FYI or update that can comfortably wait until {owner} next opens their phone.
- Is an automated notice, app notification, call/missed-call notice, or mass send with nothing they must act on.

## Threads and follow-ups
Only the new information in the latest message counts. If {owner} would already have been notified when this thread started (the prior messages shown for context), do not fire again just because someone added more or replied "thanks". A later message earns a notification only if it adds something genuinely new that {owner} now has to act on.

## Judgment over rules
These are heuristics, not hard rules. Do not pattern-match on the surface and stop thinking. A casual-looking message can matter and an urgent-sounding one can be noise. A friend asking "what time we hopping on later" is low-stakes social and can wait; a friend saying "i'm outside, where are you" right before plans is worth a ping. Cold sales/marketing DMs almost never clear the bar even dressed up as personal or urgent -- but a genuine, important request from a real person does. Follow the intent: protect {owner}'s attention while surfacing what genuinely matters to them personally.

## Core test
Would {owner} be stuck, miss something that matters, or be genuinely glad you interrupted them, if this message sat unseen until they next picked up their phone themselves? Only notify when the answer is clearly yes. If it is a maybe, the answer is no.

Return ONLY JSON with this exact shape:
{{"justification": "<one sentence naming the call and the reason>", "take_action": <true|false>}}"""


GATE_SYSTEM_PROMPT = _gate_system_prompt(OWNER_NAME)


def _gate_client() -> OpenAI:
    key = _env("LLM_API_KEY", "EIGHTSTATE_API_KEY", "OPENAI_API_KEY")
    if not key:
        raise GateConfigError("LLM_API_KEY (or EIGHTSTATE_API_KEY / OPENAI_API_KEY) is not set")
    return OpenAI(api_key=key, base_url=GATE_BASE_URL)


# ---------------------------------------------------------------------------
# Codex / ChatGPT-subscription provider (OAuth via `codex login`)
#
# Uses the credentials `codex login` writes to ~/.codex/auth.json to call the
# ChatGPT-subscription backend (Responses API) instead of a billed API key.
# This is an undocumented endpoint OpenAI tolerates for third-party use; it can
# change. Access tokens are short-lived JWTs, refreshed here via the OAuth
# refresh token and written back to auth.json so `codex` stays logged in too.
# ---------------------------------------------------------------------------

class GateConfigError(Exception):
    """Auth/config problem with the gatekeeper (missing key, dead OAuth token,
    missing auth file). Distinct from transient errors so the caller can fail
    CLOSED and alert the owner via Poke, instead of forwarding ungated."""


# Cached access token: avoid re-reading/refreshing on every gate call.
_codex_cache: dict = {"access_token": None, "exp": 0.0, "account_id": None}


def use_codex() -> bool:
    """Whether the gatekeeper should use the Codex subscription backend."""
    if LLM_PROVIDER == "codex":
        return True
    if LLM_PROVIDER == "openai":
        return False
    has_key = bool(_env("LLM_API_KEY", "EIGHTSTATE_API_KEY", "OPENAI_API_KEY"))
    return CODEX_AUTH_FILE.exists() and not has_key


def _jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying (we only read exp / account id)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return {}


def _codex_account_id(tokens: dict) -> str:
    aid = tokens.get("account_id")
    if aid:
        return aid
    claims = _jwt_claims(tokens.get("id_token", "") or tokens.get("access_token", ""))
    auth = claims.get("https://api.openai.com/auth", {}) or {}
    return auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id", "") or ""


def _codex_refresh(refresh_token: str) -> dict:
    if not refresh_token:
        raise GateConfigError("no Codex refresh_token in auth.json -- run `codex login`")
    try:
        resp = requests.post(
            CODEX_TOKEN_URL,
            json={
                "client_id": CODEX_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                # offline_access is required to be issued a fresh refresh token.
                "scope": "openid profile email offline_access",
            },
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Codex token endpoint unreachable: {e}") from e
    if resp.status_code in (400, 401, 403):
        raise GateConfigError(f"Codex token refresh rejected ({resp.status_code}) -- re-run `codex login`")
    resp.raise_for_status()
    return resp.json()


def _atomic_write(path: Path, text: str) -> None:
    """Write a file atomically (temp in same dir + os.replace) so a concurrent
    reader (the codex CLI, another bridge run) never sees a half-written file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _codex_access_token() -> tuple[str, str]:
    """Return (access_token, account_id), refreshing + persisting if near expiry."""
    now = time.time()
    if _codex_cache["access_token"] and now < _codex_cache["exp"] - 120:
        return _codex_cache["access_token"], _codex_cache["account_id"]

    if not CODEX_AUTH_FILE.exists():
        raise GateConfigError(f"{CODEX_AUTH_FILE} not found -- run `codex login`")
    try:
        data = json.loads(CODEX_AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise GateConfigError(f"cannot read {CODEX_AUTH_FILE}: {e}") from e
    tokens = data.get("tokens") or {}
    access = tokens.get("access_token", "")
    account_id = _codex_account_id(tokens)
    exp = float(_jwt_claims(access).get("exp", 0))

    if not access or now >= exp - 120:
        refreshed = _codex_refresh(tokens.get("refresh_token", ""))
        access = refreshed.get("access_token", access)
        if not access:
            raise GateConfigError("Codex refresh returned no access_token -- re-run `codex login`")
        if refreshed.get("refresh_token"):
            tokens["refresh_token"] = refreshed["refresh_token"]
        if refreshed.get("id_token"):
            tokens["id_token"] = refreshed["id_token"]
        tokens["access_token"] = access
        data["tokens"] = tokens
        data["last_refresh"] = datetime.now().astimezone().isoformat()
        try:
            _atomic_write(CODEX_AUTH_FILE, json.dumps(data, indent=2))
        except Exception as e:
            log.warning("Could not persist refreshed Codex token: %s", e)
        exp = float(_jwt_claims(access).get("exp", now + 3000))
        account_id = _codex_account_id(tokens) or account_id

    if not account_id:
        raise GateConfigError("no Codex account id -- re-run `codex login`")
    _codex_cache.update(access_token=access, exp=exp, account_id=account_id)
    return access, account_id


def _codex_client() -> OpenAI:
    access, account_id = _codex_access_token()
    return OpenAI(
        api_key=access,
        base_url=CODEX_BASE_URL,
        default_headers={
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
        },
    )


def _loads_json_lenient(text: str) -> dict:
    """Parse JSON from a model reply that may be fenced or have surrounding prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _triage_codex(event_text: str, model: str | None) -> dict:
    """Run the gate through the Codex subscription (Responses API)."""
    client = _codex_client()
    # The Codex backend only serves streamed responses, and its final payload
    # omits the accumulated output -- so collect the text deltas ourselves.
    chunks: list[str] = []
    with client.responses.stream(
        model=model or CODEX_MODEL,
        instructions=GATE_SYSTEM_PROMPT,
        input=[
            {
                "role": "user",
                "content": [{"type": "input_text", "text": event_text}],
            }
        ],
        store=False,
    ) as stream:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                chunks.append(event.delta)
            elif etype in ("response.failed", "response.error", "error"):
                raise RuntimeError(f"Codex stream error event: {etype}")
    text = "".join(chunks)
    if not text.strip():
        raise RuntimeError("Codex returned empty output")
    return _loads_json_lenient(text)


def _gate_clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= GATE_MAX_MSG_CHARS else text[:GATE_MAX_MSG_CHARS] + " […]"


def render_event(platform: str, chat: dict, sender: str, messages: list[dict],
                 context: list[dict] | None = None) -> str:
    """Render the incoming event into the text the gatekeeper reasons over."""
    lines = [
        f"Platform: {platform}",
        f"Chat: {chat.get('name', 'Unknown')} ({chat.get('type', 'unknown')})",
        f"From: {sender}",
    ]
    if context:
        lines.append("\nRecent prior messages in this chat (for context only):")
        for m in context[-3:]:
            lines.append(f"  {m.get('sender', 'Unknown')}: {_gate_clip(m.get('text', ''))}")
    lines.append("\nNew message(s) to triage:")
    for m in messages:
        lines.append(f"  {m.get('sender', sender)}: {_gate_clip(m.get('text', ''))}")
    return "\n".join(lines)


def triage(event_text: str, model: str | None = None, client: OpenAI | None = None) -> dict:
    """Return {'justification', 'take_action', 'error', 'error_kind'}.

    On error returns error=True and take_action=False (fail CLOSED -- never
    forward an ungated message). error_kind is 'config' for auth/config problems
    (so the caller can alert the owner via Poke to re-auth) or 'transient' for
    network/backend blips.
    """
    try:
        if client is None and use_codex():
            data = _triage_codex(event_text, model)
        else:
            client = client or _gate_client()
            resp = client.chat.completions.create(
                model=model or GATE_MODEL,
                messages=[
                    {"role": "system", "content": GATE_SYSTEM_PROMPT},
                    {"role": "user", "content": event_text},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            data = json.loads(resp.choices[0].message.content)
        return {
            "justification": str(data.get("justification", "")),
            "take_action": bool(data.get("take_action", False)),
            "error": False,
            "error_kind": None,
        }
    except GateConfigError as e:
        return {"justification": f"gate config error: {e}", "take_action": False,
                "error": True, "error_kind": "config"}
    except Exception as e:
        return {"justification": f"gatekeeper error: {e}", "take_action": False,
                "error": True, "error_kind": "transient"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    # Line-buffer console so logs are not lost in a stdio buffer on a hard kill.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception as e:  # logging must never crash the bridge
        console.handle(logging.LogRecord(
            "bridge", logging.WARNING, __file__, 0,
            "Could not open log file %s: %s", (LOG_FILE, e), None,
        ))

    return logging.getLogger("bridge")


log = _setup_logging()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# Track seen message IDs to avoid duplicate forwards on edits/reactions
seen_messages: dict[str, float] = {}
_seen_dirty = False
_seen_last_save = 0.0

# Your own Beeper user IDs (populated on startup) -- used to skip your own msgs
self_ids: set[str] = set()

# Chat metadata cache: chatID -> {network, title}
chat_cache: dict[str, dict] = {}

# Pending per-chat batches: chatID -> {chat_info, entries, task}
pending_batches: dict[str, dict] = {}

# Last time the owner sent a message in a given chat (epoch).
last_self_activity: dict[str, float] = {}

# Set once at startup after we confirm macOS + Messages + osascript are present.
_imessage_ready = False

# Telegram transport state (lazy; only used when HANDOFF_TRANSPORT=telegram).
telegram_client = None
poke_entity = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Cross-platform check whether a process id is currently running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_singleton() -> bool:
    """Take the single-instance lock. Returns False if another bridge is live."""
    if LOCK_FILE.exists():
        try:
            other = int(LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            log.warning("Another bridge instance is already running (pid %d); exiting.", other)
            return False
        log.info("Clearing stale lock from pid %s", other or "?")
    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        log.warning("Could not write lock file: %s", e)
    atexit.register(_release_singleton)
    return True


def _release_singleton():
    """Remove the lock file if we still own it."""
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except Exception:
        pass


def beeper_get(path: str) -> dict | list | None:
    """GET a Beeper REST endpoint."""
    try:
        r = requests.get(
            f"{BEEPER_REST}{path}",
            headers={"Authorization": f"Bearer {BEEPER_TOKEN}"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Beeper REST %s failed: %s", path, e)
        return None


def populate_self_ids():
    """Discover the owner's own participant IDs so we can filter them out."""
    accounts = beeper_get("/v1/accounts")
    if accounts:
        acct_list = accounts if isinstance(accounts, list) else accounts.get("items", [])
        for acct in acct_list:
            uid = acct.get("selfParticipantID")
            if uid:
                self_ids.add(uid)

    # Scan a window of chats (not just one) so self-participant IDs are found
    # across as many networks as possible.
    chats = beeper_get("/v1/chats?limit=50")
    if chats:
        chat_list = chats if isinstance(chats, list) else chats.get("items", [])
        for chat in chat_list:
            for p in chat.get("participants", {}).get("items", []):
                if p.get("isSelf") and p.get("id"):
                    self_ids.add(p["id"])
    log.info("Self IDs discovered: %d", len(self_ids))


def get_chat_info(chat_id: str) -> dict:
    """Get or cache chat metadata (network, title)."""
    if chat_id in chat_cache:
        return chat_cache[chat_id]
    data = beeper_get(f"/v1/chats/{chat_id}")
    if data:
        info = {
            "network": data.get("network", "Unknown"),
            "title": data.get("title", "Unknown"),
            "type": data.get("type", "unknown"),
        }
        chat_cache[chat_id] = info
        return info
    return {"network": "Unknown", "title": "Unknown", "type": "unknown"}


def load_seen_messages():
    """Load recently seen message IDs so restarts do not replay old upserts."""
    if not SEEN_FILE.exists():
        return
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not load seen-message cache: %s", e)
        return

    cutoff = time.time() - DEDUP_WINDOW
    for msg_id, seen_at in data.items():
        if isinstance(seen_at, (int, float)) and seen_at >= cutoff:
            seen_messages[msg_id] = seen_at


def save_seen_messages():
    """Persist the dedup cache, capped to avoid unbounded disk growth."""
    try:
        items = sorted(seen_messages.items(), key=lambda item: item[1])[-SEEN_FILE_MAX_ITEMS:]
        tmp = SEEN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dict(items)), encoding="utf-8")
        tmp.replace(SEEN_FILE)  # atomic swap so a crash mid-write can't corrupt it
    except Exception as e:
        log.warning("Could not save seen-message cache: %s", e)


def maybe_flush_seen(force: bool = False):
    """Persist the seen-cache at most once per SEEN_SAVE_INTERVAL (debounced)."""
    global _seen_dirty, _seen_last_save
    if not _seen_dirty:
        return
    now = time.time()
    if not force and now - _seen_last_save < SEEN_SAVE_INTERVAL:
        return
    save_seen_messages()
    _seen_dirty = False
    _seen_last_save = now


def remember_message(msg_id: str):
    """Mark a message ID as seen (persisted lazily by maybe_flush_seen)."""
    global _seen_dirty
    seen_messages[msg_id] = time.time()
    _seen_dirty = True


def cleanup_seen():
    """Prune old entries from the dedup cache."""
    global _seen_dirty
    cutoff = time.time() - DEDUP_WINDOW
    stale = [k for k, v in seen_messages.items() if v < cutoff]
    for k in stale:
        del seen_messages[k]
    if stale:
        _seen_dirty = True


def parse_timestamp(value: str | None) -> float | None:
    """Convert Beeper ISO timestamps to epoch seconds."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_old_replay(entry: dict) -> bool:
    """Skip historical messages replayed by Beeper on bridge startup."""
    ts = parse_timestamp(entry.get("timestamp"))
    return ts is not None and ts < STARTED_AT - STARTUP_REPLAY_GRACE


def extract_message_text(entry: dict) -> str:
    """Pull readable plain text from a message entry."""
    text = entry.get("textContent") or entry.get("text") or ""
    if not isinstance(text, str):
        return ""

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<blockquote[^>]*>.*?</blockquote>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_sender_name(entry: dict) -> str:
    """Get a human-readable sender name from a message entry."""
    sender = entry.get("sender", {})
    return (
        entry.get("senderName")
        or sender.get("fullName")
        or sender.get("username")
        or entry.get("senderID")
        or sender.get("id")
        or "Unknown"
    )


def is_from_self(entry: dict) -> bool:
    """Check if a message was sent by the owner themselves."""
    if entry.get("isSender"):
        return True

    sender = entry.get("sender", {})
    sender_id = entry.get("senderID") or sender.get("id", "")
    if sender_id in self_ids:
        return True
    if sender.get("isSelf"):
        return True
    return False


def should_skip_entry(entry: dict) -> tuple[bool, str]:
    """Return whether this Beeper entry should never be forwarded to Poke."""
    if entry.get("isDeleted"):
        return True, "deleted"
    if entry.get("isHidden"):
        return True, "hidden"
    if entry.get("type") == "REACTION":
        return True, "reaction"
    if is_from_self(entry):
        return True, "self"
    if is_old_replay(entry):
        return True, "startup replay"
    return False, ""


def is_placeholder_text(text: str, entry: dict) -> bool:
    """Skip Beeper attachment captions like 'Name:'."""
    stripped = text.strip()
    if not stripped.endswith(":"):
        return False
    label = stripped[:-1].strip().lower()
    sender = extract_sender_name(entry).strip().lower()
    return label in {sender, OWNER_NAME.lower(), "the user", "the owner"}


def is_noise_text(text: str) -> bool:
    """Drop non-actionable system notices that should never trigger a Poke ping."""
    low = text.strip().lower()
    # Call/system notices Beeper renders like "(X started a call. ...)". Require
    # a notice verb so we don't drop real messages that merely contain "call".
    if re.search(r"\b(started|missed|declined|ended)\b[^)]*\bcall\b", low):
        return True
    return False


def normalize_kind(kind: str | None) -> str:
    """Normalize Beeper message type names for Poke."""
    return (kind or "text").lower()


def normalize_message(entry: dict) -> dict:
    """Return the compact message shape Poke should reason over."""
    return {
        "sender": extract_sender_name(entry),
        "text": extract_message_text(entry),
        "time": entry.get("timestamp"),
        "kind": normalize_kind(entry.get("type")),
    }


def fetch_recent_context(chat_id: str, before_msg_id: str, before_timestamp: str | None, limit: int = 20) -> list[dict]:
    """Fetch recent messages before the current message for context."""
    data = beeper_get(f"/v1/chats/{chat_id}/messages?limit={limit}")
    if not data:
        return []

    messages = data if isinstance(data, list) else data.get("items", [])
    before_epoch = parse_timestamp(before_timestamp)
    context = []
    for msg in messages:
        if msg.get("id") == before_msg_id:
            continue
        msg_epoch = parse_timestamp(msg.get("timestamp"))
        if before_epoch is not None and msg_epoch is not None:
            if msg_epoch >= before_epoch:
                continue
            if before_epoch - msg_epoch > CONTEXT_LOOKBACK_SECONDS:
                continue
        text = extract_message_text(msg)
        if not text or is_placeholder_text(text, msg):
            continue
        context.append({
            "sender": OWNER_NAME if is_from_self(msg) else extract_sender_name(msg),
            "text": text,
            "time": msg.get("timestamp"),
        })

    context.sort(key=lambda msg: parse_timestamp(msg.get("timestamp")) or 0)
    return context[-3:]


def build_poke_payload(chat_id: str, chat_info: dict, entries: list[dict]) -> dict:
    """Build Poke's compact social-triage event schema."""
    entries = sorted(entries, key=lambda entry: parse_timestamp(entry.get("timestamp")) or 0)
    first = entries[0]
    context = fetch_recent_context(
        chat_id=chat_id,
        before_msg_id=first.get("id", ""),
        before_timestamp=first.get("timestamp"),
    )
    messages = [normalize_message(entry) for entry in entries]

    sender_names = []
    for msg in messages:
        name = msg.get("sender") or "Unknown"
        if name not in sender_names:
            sender_names.append(name)
    sender_label = ", ".join(sender_names)

    payload = {
        "v": 2,
        "source": "beeper_bridge",
        "event": "incoming_message_notification",
        "platform": chat_info.get("network", "Unknown"),
        "chat": {
            "name": chat_info.get("title", "Unknown"),
            "type": chat_info.get("type", "unknown"),
        },
        "from": sender_label,
    }

    if len(messages) == 1:
        payload.update({
            "text": messages[0]["text"],
            "time": messages[0].get("time"),
            "kind": messages[0].get("kind", "text"),
        })
    else:
        payload["messages"] = messages

    if context:
        payload["context"] = context[-3:]

    return payload


def is_bridge_payload_text(text: str) -> bool:
    """Return whether a self-sent Poke message originated from this bridge."""
    if not text:
        return False
    if BRIDGE_MARKER in text:
        return True
    if text.lstrip().startswith(HANDOFF_PREFIX):
        return True
    try:
        payload = json.loads(extract_message_text({"text": text}))
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("source") == "beeper_bridge"


def is_poke_chat_manually_active() -> bool:
    """Detect whether the owner is currently talking to Poke manually."""
    if not POKE_BEEPER_CHAT_ID:
        return False
    data = beeper_get(f"/v1/chats/{POKE_BEEPER_CHAT_ID}/messages?limit=12")
    if not data:
        return False

    messages = data if isinstance(data, list) else data.get("items", [])
    now = time.time()
    for msg in messages:
        if not is_from_self(msg):
            continue
        text = extract_message_text(msg)
        if not text or is_bridge_payload_text(text):
            continue
        ts = parse_timestamp(msg.get("timestamp"))
        if ts is not None and now - ts <= ACTIVE_POKE_PAUSE_SECONDS:
            return True
    return False


# AppleScript that sends one message via Messages. Target + message are passed as
# argv (never interpolated) so the text can't break or inject. Two target modes:
#   - a chat GUID like "iMessage;-;urn:biz:..." or "iMessage;-;+1555..." -> sends
#     to that existing thread by id (this is how Apple Messages for Business /
#     urn:biz chats like the AMB Poke are reached -- they have no participant).
#   - anything else (a phone number / email) -> resolved as an iMessage participant.
_APPLESCRIPT_SEND = """
on run argv
    set theTarget to item 1 of argv
    set theMessage to item 2 of argv
    tell application "Messages"
        if theTarget starts with "iMessage;" or theTarget starts with "SMS;" then
            send theMessage to chat id theTarget
        else
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to participant theTarget of targetService
            send theMessage to targetBuddy
        end if
    end tell
end run
"""


def ensure_imessage_ready() -> None:
    """Verify we can drive Messages over AppleScript. Raises on any problem."""
    global _imessage_ready
    if _imessage_ready:
        return
    if sys.platform != "darwin":
        raise RuntimeError("iMessage handoff requires macOS (Messages + osascript).")
    if shutil.which("osascript") is None:
        raise RuntimeError("osascript not found -- is this macOS?")
    if not POKE_IMESSAGE_HANDLE:
        raise RuntimeError("POKE_IMESSAGE_HANDLE is empty; set Poke's iMessage number in .env.")
    _imessage_ready = True
    log.info("iMessage handoff ready -> Poke at %s", POKE_IMESSAGE_HANDLE)


def _imessage_send(handle: str, message: str) -> None:
    """Send one iMessage via the Messages app. Raises on failure."""
    proc = subprocess.run(
        ["osascript", "-", handle, message],
        input=_APPLESCRIPT_SEND,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"osascript exited {proc.returncode}")


async def _send_to_poke_imessage(message: str) -> bool:
    """Text a handoff to Poke over iMessage, retrying once on a transient failure."""
    for attempt in (1, 2):
        try:
            ensure_imessage_ready()
            await asyncio.to_thread(_imessage_send, POKE_IMESSAGE_HANDLE, message)
            return True
        except Exception as e:
            log.warning("iMessage send attempt %d failed: %s", attempt, e)
            if attempt == 2:
                log.error("Giving up sending to Poke over iMessage")
                return False
            await asyncio.sleep(2)
    return False


async def init_telegram():
    """Connect the Telegram user session used to message Poke (lazy import)."""
    global telegram_client, poke_entity
    if telegram_client and telegram_client.is_connected() and poke_entity:
        return
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")
    from telethon import TelegramClient  # imported only when Telegram is used

    if telegram_client is None:
        telegram_client = TelegramClient(TELEGRAM_SESSION, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    if not telegram_client.is_connected():
        await telegram_client.connect()
    if not await telegram_client.is_user_authorized():
        raise RuntimeError(
            "Telegram session is not authorized. Run `python bridge.py --login` once first."
        )
    poke_entity = await telegram_client.get_entity(POKE_TELEGRAM_USERNAME)
    log.info("Connected to Telegram Poke chat: @%s", POKE_TELEGRAM_USERNAME)


async def _send_to_poke_telegram(message: str) -> bool:
    """Send a handoff to Poke on Telegram, reconnecting once if the link dropped."""
    global poke_entity
    for attempt in (1, 2):
        try:
            await init_telegram()
            await telegram_client.send_message(poke_entity, message)
            return True
        except Exception as e:
            log.warning("Telegram send attempt %d failed: %s", attempt, e)
            poke_entity = None  # force re-init / reconnect on next attempt
            if attempt == 2:
                log.error("Giving up sending to Poke after reconnect attempt")
                return False
            await asyncio.sleep(2)
    return False


async def ensure_transport_ready() -> None:
    """Fail fast at startup if the selected handoff transport can't be used."""
    if HANDOFF_TRANSPORT == "telegram":
        await init_telegram()
    else:
        ensure_imessage_ready()


async def send_to_poke(message: str) -> bool:
    """Hand a message to Poke over the configured transport (iMessage or Telegram)."""
    if HANDOFF_TRANSPORT == "telegram":
        return await _send_to_poke_telegram(message)
    return await _send_to_poke_imessage(message)


# Rate-limit "gatekeeper is broken" alerts so a sustained outage pings Poke at
# most once per interval instead of on every incoming message.
GATE_ALERT_INTERVAL = 1800  # 30 min
_last_gate_alert = 0.0


async def maybe_alert_gate_broken(reason: str) -> None:
    """Tell Poke the gate is down so it can prompt the owner to re-auth/fix it.

    Rate-limited; the transport (iMessage/Telegram) is independent of the gate
    LLM, so this alert still gets through when the gatekeeper is the thing broken.
    """
    global _last_gate_alert
    now = time.time()
    if now - _last_gate_alert < GATE_ALERT_INTERVAL:
        return
    _last_gate_alert = now
    alert = (
        f"{HANDOFF_PREFIX} ⚠️ Bridge alert (not a forwarded message): the "
        f"gatekeeper is failing, so I've PAUSED triaging {OWNER_NAME}'s messages "
        f"-- nothing is being surfaced right now. Reason: {reason}. Tell {OWNER_NAME} "
        "to check the bridge -- this usually means re-authenticating (run `codex login`) "
        "or fixing the LLM key in .env." + BRIDGE_MARKER
    )
    if await send_to_poke(alert):
        log.info("Alerted Poke that the gatekeeper is down.")


async def send_batch_to_poke(chat_id: str, chat_info: dict, entries: list[dict]):
    """Triage a per-chat batch; only forward to Poke if it clears the gate."""
    payload = await asyncio.to_thread(build_poke_payload, chat_id, chat_info, entries)

    msgs = payload.get("messages") or [{"sender": payload.get("from"), "text": payload.get("text", "")}]
    network = chat_info.get("network", "Unknown")
    title = chat_info.get("title", "Unknown")
    sender = payload.get("from", "Unknown")

    # Cheap pre-filter: if the whole batch is emoji/punctuation only (no letters
    # or digits in any message, any language), it cannot be important -- skip
    # without spending an LLM call.
    if not any(re.search(r"\w", m.get("text", "") or "", flags=re.UNICODE) for m in msgs):
        log.info("Gate SKIP (no text content): %s/%s from %s", network, title, sender)
        return

    # Stage 1: gatekeeper -- mirrors Poke's email triage.
    event_text = render_event(
        payload.get("platform", "Unknown"), payload.get("chat", {}), sender, msgs, payload.get("context"),
    )
    verdict = await asyncio.to_thread(triage, event_text)

    if verdict.get("error"):
        # Fail CLOSED -- never forward an ungated message. On a config/auth error
        # (the dominant Codex failure mode), alert the owner via Poke to re-auth.
        if verdict.get("error_kind") == "config":
            log.error("Gate CONFIG error -- pausing triage: %s", verdict["justification"][:160])
            await maybe_alert_gate_broken(verdict["justification"])
        else:
            log.warning("Gate transient error on %s/%s -- skipping: %s",
                        network, title, verdict["justification"][:120])
        return
    if not verdict["take_action"]:
        log.info("Gate SKIP: %s/%s from %s -- %s", network, title, sender, verdict["justification"][:100])
        return

    first = entries[0] if entries else {}
    sender_id = first.get("senderID") or first.get("sender", {}).get("id", "")
    message = render_handoff(payload, chat_id=chat_id, sender_id=sender_id)

    if await send_to_poke(message):
        log.info(
            "-> Handoff dispatched to Poke (gate PASS): %s/%s from %s (%d msg%s)",
            network, title, sender, len(entries), "" if len(entries) == 1 else "s",
        )


async def flush_batch_after_delay(chat_id: str, delay: int = BATCH_DELAY):
    """Wait for a quiet period, then send the pending chat batch."""
    try:
        await asyncio.sleep(delay)
        batch = pending_batches.get(chat_id)
        if not batch:
            return

        # Owner already replied in this chat during the debounce window -> he's
        # handling it himself. Drop the notification entirely.
        if time.time() - last_self_activity.get(chat_id, 0) <= SELF_ACTIVE_PAUSE_SECONDS:
            pending_batches.pop(chat_id, None)
            log.info(
                "Dropping %s/%s; owner is active in that chat",
                batch["chat_info"].get("network", "Unknown"),
                batch["chat_info"].get("title", "Unknown"),
            )
            return

        if await asyncio.to_thread(is_poke_chat_manually_active):
            log.info(
                "Holding bridge update for %s/%s; Poke chat is manually active",
                batch["chat_info"].get("network", "Unknown"),
                batch["chat_info"].get("title", "Unknown"),
            )
            batch["task"] = asyncio.create_task(
                flush_batch_after_delay(chat_id, ACTIVE_POKE_RECHECK_SECONDS)
            )
            return

        batch = pending_batches.pop(chat_id, None)
        if not batch:
            return
        await send_batch_to_poke(chat_id, batch["chat_info"], batch["entries"])
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.error("flush_batch failed for %s: %s", chat_id, e)


def register_self_activity(chat_id: str):
    """Owner sent a message in this chat -- drop pending pings for it."""
    last_self_activity[chat_id] = time.time()
    batch = pending_batches.pop(chat_id, None)
    if batch:
        task = batch.get("task")
        if task:
            task.cancel()
        log.info(
            "Owner replied in %s/%s -- dropping %d pending notification(s)",
            batch["chat_info"].get("network", "Unknown"),
            batch["chat_info"].get("title", "Unknown"),
            len(batch["entries"]),
        )


def queue_for_poke(chat_id: str, chat_info: dict, entry: dict):
    """Queue a message into a per-chat debounce batch."""
    batch = pending_batches.get(chat_id)
    if batch:
        batch["entries"].append(entry)
        batch["chat_info"] = chat_info
        batch["task"].cancel()
    else:
        batch = {"chat_info": chat_info, "entries": [entry], "task": None}
        pending_batches[chat_id] = batch
    batch["task"] = asyncio.create_task(flush_batch_after_delay(chat_id))


# ---------------------------------------------------------------------------
# WebSocket listener
# ---------------------------------------------------------------------------

async def listen():
    """Connect to Beeper WebSocket and process message events."""
    headers = {"Authorization": f"Bearer {BEEPER_TOKEN}"}

    async with websockets.connect(
        BEEPER_WS,
        additional_headers=headers,
        ping_interval=WS_PING_INTERVAL,
        ping_timeout=WS_PING_TIMEOUT,
    ) as ws:
        ready = json.loads(await ws.recv())
        if ready.get("type") != "ready":
            raise RuntimeError(f"Expected 'ready' handshake from Beeper, got: {ready}")
        log.info("Connected to Beeper WebSocket")

        await ws.send(json.dumps({"type": "subscriptions.set", "chatIDs": ["*"]}))
        conf = json.loads(await ws.recv())
        if conf.get("type") != "subscriptions.updated":
            raise RuntimeError(f"Expected 'subscriptions.updated' from Beeper, got: {conf}")
        log.info("Subscribed to all chats - listening for messages...")

        async for raw in ws:
            event = json.loads(raw)
            if event.get("type") != "message.upserted":
                continue

            chat_id = event.get("chatID", "")
            if chat_id in IGNORED_CHAT_IDS:
                continue

            entries = event.get("entries", [])

            for entry in entries:
                msg_id = entry.get("id", "")
                if not msg_id:
                    continue

                if msg_id in seen_messages:
                    continue
                remember_message(msg_id)

                skip, reason = should_skip_entry(entry)
                if skip:
                    if reason == "self":
                        self_text = extract_message_text(entry)
                        if self_text and not is_placeholder_text(self_text, entry):
                            register_self_activity(chat_id)
                    log.debug("Skipping %s message %s", reason, msg_id)
                    continue

                text = extract_message_text(entry)
                if not text or is_placeholder_text(text, entry):
                    continue
                if is_noise_text(text):
                    log.debug("Skipping noise message %s: %s", msg_id, text[:60])
                    continue

                sender = extract_sender_name(entry)
                chat_info = await asyncio.to_thread(get_chat_info, chat_id)

                log.info(
                    "<- %s/%s from %s: %s",
                    chat_info["network"],
                    chat_info["title"],
                    sender,
                    text[:80] + ("..." if len(text) > 80 else ""),
                )

                queue_for_poke(chat_id, chat_info, entry)

            cleanup_seen()
            maybe_flush_seen()


async def heartbeat_loop():
    """Write a liveness timestamp and flush the seen-cache on a timer."""
    while True:
        try:
            HEARTBEAT_FILE.write_text(str(int(time.time())), encoding="utf-8")
        except Exception as e:
            log.debug("Heartbeat write failed: %s", e)
        maybe_flush_seen()
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def run_forever():
    """Reconnect loop with exponential backoff."""
    delay = RECONNECT_MIN
    while True:
        try:
            await listen()
        except (
            websockets.ConnectionClosed,
            ConnectionRefusedError,
            OSError,
        ) as e:
            log.warning("WebSocket disconnected: %s - reconnecting in %ds", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)
        except Exception as e:
            log.error("Unexpected error: %s - reconnecting in %ds", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)
        else:
            delay = RECONNECT_MIN


def preflight_config() -> list[str]:
    """Return a list of human-readable problems with the required configuration."""
    problems = []
    if not BEEPER_TOKEN:
        problems.append("BEEPER_TOKEN is not set (Beeper Desktop -> Settings -> Developer).")
    if HANDOFF_TRANSPORT == "telegram":
        if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
            problems.append("Telegram transport: TELEGRAM_API_ID / TELEGRAM_API_HASH not set (https://my.telegram.org).")
    elif HANDOFF_TRANSPORT == "imessage":
        if sys.platform != "darwin":
            problems.append("iMessage transport requires macOS (Messages app + osascript).")
        elif shutil.which("osascript") is None:
            problems.append("osascript not found -- the macOS Messages backend is unavailable.")
        if not POKE_IMESSAGE_HANDLE:
            problems.append("POKE_IMESSAGE_HANDLE is not set (Poke's iMessage number/handle).")
    else:
        problems.append(f"HANDOFF_TRANSPORT='{HANDOFF_TRANSPORT}' is invalid (use 'imessage' or 'telegram').")
    if use_codex():
        if not CODEX_AUTH_FILE.exists():
            problems.append(f"Codex provider selected but {CODEX_AUTH_FILE} is missing -- run `codex login`.")
    elif not _env("LLM_API_KEY", "EIGHTSTATE_API_KEY", "OPENAI_API_KEY"):
        problems.append("No gatekeeper LLM configured -- set LLM_API_KEY, or run `codex login` to use your ChatGPT subscription.")
    return problems


async def main_async():
    log.info("Starting Beeper -> Poke bridge (owner: %s)", OWNER_NAME)
    if not POKE_BEEPER_CHAT_ID:
        log.warning(
            "POKE_BEEPER_CHAT_ID is not set -- the bridge will rely on the handoff "
            "marker alone to avoid feeding Poke its own messages. See README to set it."
        )
    if not acquire_singleton():
        return
    load_seen_messages()
    await asyncio.to_thread(populate_self_ids)
    await ensure_transport_ready()  # fail fast if the handoff transport isn't usable
    target = POKE_TELEGRAM_USERNAME if HANDOFF_TRANSPORT == "telegram" else POKE_IMESSAGE_HANDLE
    log.info("Handoff transport: %s -> Poke (%s)", HANDOFF_TRANSPORT, target)
    if use_codex():
        log.info("Gatekeeper: Codex / ChatGPT subscription (model %s)", CODEX_MODEL)
    else:
        log.info("Gatekeeper: API key provider %s (model %s)", GATE_BASE_URL, GATE_MODEL)
    try:
        await asyncio.gather(run_forever(), heartbeat_loop())
    finally:
        maybe_flush_seen(force=True)
        _release_singleton()


# ---------------------------------------------------------------------------
# Self-test + Telegram first-run login
# ---------------------------------------------------------------------------

def send_test_message():
    """Send a one-off test handoff to Poke over the configured transport."""
    msg = (
        f"{HANDOFF_PREFIX} (bridge self-test) If you can read this, the "
        f"Beeper->Poke {HANDOFF_TRANSPORT} handoff is working. -- bridge for {OWNER_NAME}"
        + BRIDGE_MARKER
    )
    ok = asyncio.run(send_to_poke(msg))
    target = POKE_TELEGRAM_USERNAME if HANDOFF_TRANSPORT == "telegram" else POKE_IMESSAGE_HANDLE
    print(f"Test handoff to Poke ({HANDOFF_TRANSPORT} -> {target}): {'sent' if ok else 'FAILED'}.")


async def login_telegram():
    """Interactive one-time Telegram login to create the session file."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
    from telethon import TelegramClient
    client = TelegramClient(TELEGRAM_SESSION, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.start()  # prompts for phone number + login code on the console
    me = await client.get_me()
    uname = f"@{me.username}" if me.username else me.first_name
    print(f"\nLogged in as {uname}. Session saved to {TELEGRAM_SESSION}.session")
    print("You can now run the bridge with: python bridge.py")
    await client.disconnect()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--login" in sys.argv:
        if HANDOFF_TRANSPORT == "telegram":
            asyncio.run(login_telegram())
        else:
            print("No login needed -- the iMessage transport texts Poke through the macOS Messages app.")
            print("Make sure Messages is signed in to iMessage, then run: python bridge.py")
    elif "--test" in sys.argv:
        send_test_message()
    elif "--gate-test" in sys.argv:
        provider = "Codex subscription" if use_codex() else f"API key ({GATE_BASE_URL})"
        sample = (
            "Platform: WhatsApp\nChat: Mum (single)\nFrom: Mum\n\n"
            "New message(s) to triage:\n  Mum: are you still coming to dinner at 7? need to know now to book the table"
        )
        print(f"Gatekeeper provider: {provider}")
        print(json.dumps(triage(sample), indent=2))
    else:
        problems = preflight_config()
        if problems:
            log.error("Cannot start -- missing required configuration in .env:")
            for p in problems:
                log.error("  - %s", p)
            log.error("Copy .env.example to .env and fill it in (see README.md).")
            sys.exit(1)
        try:
            asyncio.run(main_async())
        except KeyboardInterrupt:
            log.info("Bridge stopped")
