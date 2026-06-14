"""
Message gatekeeper -- stage 1 triage for the Beeper -> Poke bridge.

Mirrors how Poke triages email: a cheap, fast LLM call decides whether an
incoming message is relevant and time-sensitive enough to interrupt the owner.
If not, the bridge stays completely silent (the conversational Poke never sees
it). If yes, the bridge hands Poke a heads-up and asks it to draft a reply.

Prompt adapted from Poke's own email-triage prompt (high bar, default silence,
judgment over rules).

Config (env, OpenAI-compatible provider):
    LLM_API_KEY      -- required (falls back to EIGHTSTATE_API_KEY / OPENAI_API_KEY)
    LLM_BASE_URL     -- default https://api.openai.com/v1
                        (falls back to EIGHTSTATE_BASE_URL / OPENAI_BASE_URL)
    GATEKEEPER_MODEL -- default gpt-4o-mini
    OWNER_NAME       -- the account owner the bridge triages for (default "the owner")
"""

from __future__ import annotations

import json
import os

from openai import OpenAI


def _env(*names: str, default: str = "") -> str:
    """Return the first set environment variable among names."""
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return default


# Provider config. New canonical names first, legacy EightState/OpenAI names as
# fallback so existing .env files keep working.
BASE_URL = _env("LLM_BASE_URL", "EIGHTSTATE_BASE_URL", "OPENAI_BASE_URL",
                default="https://api.openai.com/v1")
MODEL = _env("GATEKEEPER_MODEL", default="gpt-4o-mini")
OWNER_NAME = _env("OWNER_NAME", default="the owner")


def _system_prompt(owner: str) -> str:
    """Build the triage system prompt for a specific account owner."""
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


SYSTEM_PROMPT = _system_prompt(OWNER_NAME)


def _client() -> OpenAI:
    key = _env("LLM_API_KEY", "EIGHTSTATE_API_KEY", "OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM_API_KEY (or EIGHTSTATE_API_KEY / OPENAI_API_KEY) must be set"
        )
    return OpenAI(api_key=key, base_url=BASE_URL)


MAX_MSG_CHARS = 2000  # cap any single message fed to the gate (bounds cost/latency)


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= MAX_MSG_CHARS else text[:MAX_MSG_CHARS] + " […]"


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
            lines.append(f"  {m.get('sender', 'Unknown')}: {_clip(m.get('text', ''))}")
    lines.append("\nNew message(s) to triage:")
    for m in messages:
        lines.append(f"  {m.get('sender', sender)}: {_clip(m.get('text', ''))}")
    return "\n".join(lines)


def triage(event_text: str, model: str | None = None, client: OpenAI | None = None) -> dict:
    """Return {'justification', 'take_action', 'summary', 'error'}.

    On any provider/parse error returns error=True with take_action=True; the
    caller decides how to fail (open for DMs, closed for groups) so a gateway
    outage cannot spam busy group chats.
    """
    client = client or _client()
    try:
        resp = client.chat.completions.create(
            model=model or MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
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
