"""
Beeper -> Poke Bridge

Listens to Beeper's WebSocket stream for incoming messages across all platforms
(WhatsApp, Discord, Telegram, Instagram, etc.) and runs a two-stage pipeline:

  1. Gatekeeper (below): a fast LLM triage call -- modelled on Poke's
     own email triage -- decides whether a message is important enough to
     interrupt the owner. If not, the bridge stays completely silent.
  2. Handoff: for messages that clear the gate, the bridge sends Poke a heads-up
     and asks it to draft an in-voice reply (Poke reads the chat via its Beeper
     MCP). Draft-only; the bridge never sends replies to third parties.

Before the gate, cheap filters drop self-messages, noise (call notices),
emoji-only batches, and chats the owner is already actively replying in.

Config is loaded from a local .env (see .env.example). Run `python bridge.py
--login` once to create the Telegram session, then `python bridge.py` to run.
"""

from __future__ import annotations

import asyncio
import atexit
import ctypes
from datetime import datetime
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time

import requests
from openai import OpenAI
from telethon import TelegramClient
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

TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH")
TELEGRAM_SESSION = str(HERE / os.environ.get("TELEGRAM_SESSION_NAME", "telegram-poke-bridge"))
POKE_TELEGRAM_USERNAME = os.environ.get("POKE_TELEGRAM_USERNAME", "interaction_poke_bot")
MAX_TELEGRAM_MESSAGE = 3900
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
# Telegram chat with the Poke bot -- it differs per user, so it is configured in
# .env (see README for how to find it). If unset, only the handoff marker below
# protects against feedback loops.
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


def render_handoff(payload: dict, summary: str, chat_id: str = "", sender_id: str = "") -> str:
    """Heads-up + in-voice draft request handed to the conversational Poke."""
    chat_name = payload.get("chat", {}).get("name", "Unknown")
    chat_type = payload.get("chat", {}).get("type", "unknown")
    sender = payload.get("from", "Unknown")
    msgs = payload.get("messages") or [{"sender": sender, "text": payload.get("text", "")}]
    verbatim = "\n".join(f"  {m.get('sender', sender)}: {m.get('text', '')}" for m in msgs)
    text = (
        f"{HANDOFF_PREFIX} Do two things:\n"
        f"1) Give {OWNER_NAME} a one-line heads-up in your own voice -- who it's from and what they need.\n"
        "2) Use your Beeper MCP to open this chat and read the recent history, so you understand who "
        f"this person is to {OWNER_NAME} and exactly how {OWNER_NAME} talks to them (tone, length, slang, "
        f"punctuation). Then draft a reply IN {OWNER_NAME.upper()}'S VOICE -- the message {OWNER_NAME} "
        "himself would send back to this person, calibrated to how close they are and how serious this "
        "message is. Don't ask whether to draft; just draft it. Show the draft clearly so it can be "
        "copy-pasted. You cannot send it (your Beeper access is read-only) and you must not try -- "
        "drafting only. Preserve any code, link, amount, or deadline exactly.\n\n"
        f"From: {sender} on {payload.get('platform', 'Unknown')} -- chat: {chat_name} ({chat_type})\n"
        f"Beeper lookup -> chatID: {chat_id or 'unknown'}  senderID: {sender_id or 'unknown'}\n\n"
        f"What they actually said:\n{verbatim}\n\n"
        f"Why it's worth surfacing: {summary}"
    )
    return text[:MAX_TELEGRAM_MESSAGE - len(BRIDGE_MARKER)] + BRIDGE_MARKER


# ---------------------------------------------------------------------------
# Gatekeeper -- stage 1 LLM triage
#
# A cheap, fast LLM call decides whether an incoming message is relevant and
# time-sensitive enough to interrupt the owner. Adapted from Poke's own
# email-triage prompt: high bar, default silence, judgment over rules.
# ---------------------------------------------------------------------------

GATE_BASE_URL = _env("LLM_BASE_URL", "EIGHTSTATE_BASE_URL", "OPENAI_BASE_URL",
                     default="https://api.openai.com/v1")
GATE_MODEL = _env("GATEKEEPER_MODEL", default="gpt-4o-mini")
GATE_MAX_MSG_CHARS = 2000  # cap any single message fed to the gate (bounds cost/latency)


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
{{"justification": "<one sentence naming the call and the reason>", "take_action": <true|false>, "summary": "<if take_action is true: a tight, actionable summary of the single most important thing -- who it's from, what they need, and any deadline/amount/code/link preserved exactly. If take_action is false: empty string>"}}"""


GATE_SYSTEM_PROMPT = _gate_system_prompt(OWNER_NAME)


def _gate_client() -> OpenAI:
    key = _env("LLM_API_KEY", "EIGHTSTATE_API_KEY", "OPENAI_API_KEY")
    if not key:
        raise RuntimeError("LLM_API_KEY (or EIGHTSTATE_API_KEY / OPENAI_API_KEY) must be set")
    return OpenAI(api_key=key, base_url=GATE_BASE_URL)


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
    """Return {'justification', 'take_action', 'summary', 'error'}.

    On any provider/parse error returns error=True with take_action=True; the
    caller decides how to fail (open for DMs, closed for groups) so a gateway
    outage cannot spam busy group chats.
    """
    client = client or _gate_client()
    try:
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
            "summary": str(data.get("summary", "") or ""),
            "error": False,
        }
    except Exception as e:
        return {"justification": f"gatekeeper error: {e}", "take_action": True, "summary": "", "error": True}


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

telegram_client: TelegramClient | None = None
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


async def init_telegram():
    """Connect the Telegram user session used to message Poke."""
    global telegram_client, poke_entity
    if telegram_client and telegram_client.is_connected() and poke_entity:
        return
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")

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


async def send_to_poke(message: str) -> bool:
    """Send a handoff to Poke, reconnecting once if the Telegram link dropped."""
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


async def send_batch_to_poke(chat_id: str, chat_info: dict, entries: list[dict]):
    """Triage a per-chat batch; only forward to Poke if it clears the gate."""
    await init_telegram()
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
        # Gateway failure: fail open for 1:1 DMs (likely personal), but stay
        # quiet on groups to avoid blasting a busy chat on an outage.
        if payload.get("chat", {}).get("type") == "group":
            log.warning("Gate ERROR on group %s/%s -- skipping: %s", network, title, verdict["justification"][:120])
            return
        log.warning("Gate ERROR on DM %s/%s -- failing open: %s", network, title, verdict["justification"][:120])
    elif not verdict["take_action"]:
        log.info("Gate SKIP: %s/%s from %s -- %s", network, title, sender, verdict["justification"][:100])
        return

    first = entries[0] if entries else {}
    sender_id = first.get("senderID") or first.get("sender", {}).get("id", "")
    message = render_handoff(
        payload, verdict["summary"] or msgs[-1].get("text", ""),
        chat_id=chat_id, sender_id=sender_id,
    )

    if await send_to_poke(message):
        log.info(
            "-> Poke notified (gate PASS): %s/%s from %s (%d msg%s)",
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
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        problems.append("TELEGRAM_API_ID / TELEGRAM_API_HASH are not set (get them at https://my.telegram.org).")
    if not _env("LLM_API_KEY", "EIGHTSTATE_API_KEY", "OPENAI_API_KEY"):
        problems.append("LLM_API_KEY is not set (your OpenAI-compatible provider key).")
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
    await init_telegram()  # fail fast if Telegram isn't logged in
    try:
        await asyncio.gather(run_forever(), heartbeat_loop())
    finally:
        maybe_flush_seen(force=True)
        _release_singleton()


# ---------------------------------------------------------------------------
# Telegram first-run login (interactive)
# ---------------------------------------------------------------------------

async def login_telegram():
    """Interactive one-time Telegram login to create the session file."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
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
        asyncio.run(login_telegram())
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
