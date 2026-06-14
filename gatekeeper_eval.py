"""
Offline gatekeeper eval. No Poke involved -- just runs labeled messages through
the triage gate across candidate models and reports accuracy + latency.

    uv run --with openai --with python-dotenv python gatekeeper_eval.py
"""
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# Load the bridge's local .env so the eval uses the same provider config.
load_dotenv(Path(__file__).with_name(".env"))

import gatekeeper

# Models to compare. Override with EVAL_MODELS="m1,m2,m3", or edit this list to
# whatever your provider exposes.
CANDIDATE_MODELS = [
    m.strip() for m in os.environ.get("EVAL_MODELS", gatekeeper.MODEL).split(",") if m.strip()
]

# Labeled set: (id, platform, chat, sender, [messages], expected_take_action)
CASES = [
    # --- should STAY SILENT ---
    ("banter_bet", "Discord", {"name": "qualk", "type": "single"}, "qualk", ["bet"], False),
    ("banter_lol", "Discord", {"name": "Theo", "type": "single"}, "Theo", ["lmaooo yeah fair"], False),
    ("casual_plans", "Discord", {"name": "Riley", "type": "single"}, "Riley", ["what time we hopping on later"], False),
    ("group_chatter", "WhatsApp", {"name": "The Lads", "type": "group"}, "Dom", ["nah he's dogshit at rocket league"], False),
    ("group_for_ken", "WhatsApp", {"name": "The Lads", "type": "group"}, "Dom", ["ken can you sort the booking for saturday? need it locked in"], False),
    ("group_side_q", "Discord", {"name": "Dev Server", "type": "group"}, "Priya", ["ken did you push the fix yet or still broken"], False),
    ("emoji_react", "Instagram", {"name": "kayla.dr", "type": "single"}, "kayla.dr", ["🔥🔥 so good"], False),
    ("link_no_ask", "Discord", {"name": "MentallySound", "type": "single"}, "MentallySound", ["https://youtu.be/dQw4w9WgXcQ"], False),
    ("gm", "WhatsApp", {"name": "Nan", "type": "single"}, "Nan", ["morning love x"], False),
    ("promo_dm", "Instagram", {"name": "growth_guru_ai", "type": "single"}, "growth_guru_ai",
     ["Hey! I help founders 10x their reach. Free audit this week only — interested?"], False),
    ("ack_spam", "Discord", {"name": "MentallySound", "type": "single"}, "MentallySound", ["mate", "m8", "you there"], False),
    ("call_notice", "Discord", {"name": "MentallySound", "type": "single"}, "MentallySound", ["(MentallySound started a call)"], False),

    # --- should NOTIFY ---
    ("landlord_rent", "WhatsApp", {"name": "Marcus (Landlord)", "type": "single"}, "Marcus",
     ["Hi Alex, this month's rent hasn't come through. Can you sort it today? Late fee kicks in tomorrow."], True),
    ("accountant_bas", "WhatsApp", {"name": "Priya (Accountant)", "type": "single"}, "Priya Nair",
     ["The ATO needs your BAS lodged by tomorrow 5pm or there's a penalty. Have you sent the figures?"], True),
    ("client_deadline", "WhatsApp", {"name": "MotorOne CX", "type": "group"}, "Angelo Palioportas",
     ["Hey mate, City Mazda need this month's content before EOFY. Can you get it over by Friday?"], True),
    ("urgent_call", "WhatsApp", {"name": "Jess", "type": "single"}, "Jess",
     ["can you call me when you get this? it's about your car, kind of urgent"], True),
    ("leaving_now", "Discord", {"name": "Sam", "type": "single"}, "Sam",
     ["yo we still on for 3? i'm leaving now, you good to meet at the spot"], True),
    ("boss_approval", "Telegram", {"name": "Daniel (work)", "type": "single"}, "Daniel",
     ["need you to approve the deck before the 2pm client call, otherwise we can't present"], True),
    ("flight_cancel", "WhatsApp", {"name": "Mum", "type": "single"}, "Mum",
     ["your flight tomorrow got cancelled, the airline rebooked you to 6am — you need to confirm by tonight"], True),
    ("group_for_alex", "WhatsApp", {"name": "The Lads", "type": "group"}, "Dom",
     ["alex you still good to drive saturday? need to know by tonight or i'll book a maxi"], True),
]


def run_model(model: str, client: OpenAI):
    tp = fp = tn = fn = 0
    total_t = 0.0
    fails = []
    for cid, platform, chat, sender, msgs, expected in CASES:
        ev = gatekeeper.render_event(platform, chat, sender, [{"sender": sender, "text": m} for m in msgs])
        t = time.time()
        r = gatekeeper.triage(ev, model=model, client=client)
        total_t += time.time() - t
        got = r["take_action"]
        if got and expected:
            tp += 1
        elif got and not expected:
            fp += 1; fails.append((cid, "FALSE ALARM", r["summary"][:70]))
        elif not got and not expected:
            tn += 1
        else:
            fn += 1; fails.append((cid, "MISSED", r["justification"][:70]))
    n = len(CASES)
    acc = (tp + tn) / n
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    return {"model": model, "acc": acc, "prec": prec, "rec": rec,
            "avg_latency": total_t / n, "fails": fails,
            "tp": tp, "fp": fp, "tn": tn, "fn": fn}


def main():
    client = gatekeeper._client()
    print(f"# {len(CASES)} labeled cases  (base={gatekeeper.BASE_URL})\n")
    for model in CANDIDATE_MODELS:
        try:
            res = run_model(model, client)
        except Exception as e:
            print(f"{model:<22} ERROR {str(e)[:80]}\n")
            continue
        print(f"{res['model']:<22} acc={res['acc']:.0%}  prec={res['prec']:.0%}  "
              f"rec={res['rec']:.0%}  avg={res['avg_latency']:.2f}s  "
              f"(tp{res['tp']} fp{res['fp']} tn{res['tn']} fn{res['fn']})")
        for cid, kind, detail in res["fails"]:
            print(f"    {kind:<12} {cid:<16} {detail}")
        print()


if __name__ == "__main__":
    main()
