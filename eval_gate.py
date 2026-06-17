#!/usr/bin/env python3
"""
Gatekeeper eval harness.

Runs a set of labeled message events through the live gate (whatever provider
.env selects -- here: Codex subscription) and reports per-case PASS/FAIL plus
accuracy / precision / recall and latency. Run:  .venv/bin/python eval_gate.py
"""
from __future__ import annotations

import logging
import time

import bridge

# Quiet the per-request HTTP logging so the eval output stays readable.
logging.getLogger().setLevel(logging.WARNING)

# (name, expected_take_action, event_text)
CASES = [
    # ---- should NOTIFY -----------------------------------------------------
    ("dinner RSVP from Mum", True,
     "Platform: WhatsApp\nChat: Mum (single)\nFrom: Mum\n\nNew message(s) to triage:\n  Mum: are you still coming to dinner at 7? need to know now to book the table"),
    ("landlord chasing rent", True,
     "Platform: SMS\nChat: Landlord (single)\nFrom: Landlord\n\nNew message(s) to triage:\n  Landlord: Rent is 4 days overdue. Please transfer the $1,450 today or I have to start the late-fee process."),
    ("friend outside waiting", True,
     "Platform: iMessage\nChat: Jake (single)\nFrom: Jake\n\nNew message(s) to triage:\n  Jake: yo i'm outside your place, where are you??"),
    ("boss urgent approval", True,
     "Platform: Slack\nChat: Sarah (single)\nFrom: Sarah\n\nNew message(s) to triage:\n  Sarah: Need your sign-off on the client deck in the next 30 min or we miss the send window. Can you approve?"),
    ("flight cancelled", True,
     "Platform: SMS\nChat: Air NZ (single)\nFrom: Air NZ\n\nNew message(s) to triage:\n  Air NZ: Your flight NZ123 tomorrow 9:00am is CANCELLED. Reply or call to rebook."),
    ("meeting in 20 min", True,
     "Platform: Telegram\nChat: Dan (single)\nFrom: Dan\n\nNew message(s) to triage:\n  Dan: standup got moved up, we're starting in 20 mins not 3pm — you good to join?"),
    ("@mention asking owner in group", True,
     "Platform: Discord\nChat: Project Server (group)\nFrom: Priya\n\nNew message(s) to triage:\n  Priya: @Alex can you push the fix before the 5pm release? we're blocked on it"),

    # ---- should STAY SILENT ------------------------------------------------
    ("banter", False,
     "Platform: iMessage\nChat: Boys (group)\nFrom: Tom\n\nNew message(s) to triage:\n  Tom: lmaooo bet\n  Tom: gm lads"),
    ("cold sales DM", False,
     "Platform: Instagram\nChat: growthguy23 (single)\nFrom: growthguy23\n\nNew message(s) to triage:\n  growthguy23: Hey! Loved your profile 🚀 I help founders 10x their reach in 30 days, open to a quick chat??"),
    ("group side-chatter to someone else", False,
     "Platform: WhatsApp\nChat: Flat (group)\nFrom: Mia\n\nNew message(s) to triage:\n  Mia: Ken can you take the bins out tonight? it's your turn"),
    ("shared link no ask", False,
     "Platform: Telegram\nChat: Sam (single)\nFrom: Sam\n\nNew message(s) to triage:\n  Sam: https://youtube.com/watch?v=xyz this is hilarious 😂"),
    ("thanks follow-up", False,
     "Platform: SMS\nChat: Plumber (single)\nFrom: Plumber\n\nRecent prior messages in this chat (for context only):\n  Plumber: All booked for Tuesday 10am.\n\nNew message(s) to triage:\n  Plumber: thanks!"),
    ("automated newsletter", False,
     "Platform: Email-ish (single)\nChat: TechCrunch (single)\nFrom: TechCrunch\n\nNew message(s) to triage:\n  TechCrunch: Your Daily Digest: 10 startups to watch this week 📈"),
    ("gaming hangout chatter", False,
     "Platform: Discord\nChat: Squad (group)\nFrom: Leo\n\nNew message(s) to triage:\n  Leo: who's hopping on valorant later\n  Leo: need one more for ranked"),
]


def main() -> int:
    print(f"Provider: {'Codex subscription' if bridge.use_codex() else 'API key'}  "
          f"| model: {bridge.CODEX_MODEL if bridge.use_codex() else bridge.GATE_MODEL}\n")
    tp = fp = tn = fn = 0
    latencies = []
    fails = []

    for name, expected, event in CASES:
        t0 = time.time()
        verdict = bridge.triage(event)
        dt = time.time() - t0
        latencies.append(dt)
        got = verdict["take_action"]
        err = verdict.get("error")
        ok = (got == expected) and not err
        mark = "PASS" if ok else "FAIL"
        if ok:
            print(f"  [{mark}] {name:<34} -> {'NOTIFY' if got else 'silent'}  ({dt:.1f}s)")
        else:
            extra = " ERROR" if err else ""
            print(f"  [{mark}] {name:<34} exp={'NOTIFY' if expected else 'silent'} "
                  f"got={'NOTIFY' if got else 'silent'}{extra}  ({dt:.1f}s)")
            fails.append((name, verdict))

        if expected and got:       tp += 1
        elif expected and not got: fn += 1
        elif not expected and got: fp += 1
        else:                      tn += 1

    n = len(CASES)
    acc = (tp + tn) / n
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    print(f"\n  accuracy {acc:.0%}  precision {prec:.0%}  recall {rec:.0%}  "
          f"(tp={tp} tn={tn} fp={fp} fn={fn})")
    print(f"  latency: avg {sum(latencies)/n:.1f}s  min {min(latencies):.1f}s  max {max(latencies):.1f}s")

    if fails:
        print("\n  Misses (justification):")
        for name, v in fails:
            print(f"   - {name}: {v['justification'][:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
