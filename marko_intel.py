"""MARKO operator intelligence layer.

Pure, deterministic functions that turn an annotated lead into operator
ammo: a cold-call script, a conservative missed-money estimate, and a
daily brief that bundles them with the existing scored call queue.

Reads existing lead fields (name, owner, city, niche, pain_points, and
the _score/_label/_signals injected by commands.score_lead). No I/O,
no network calls, no AI calls, no mutation of inputs. Every output
is reproducible from the lead's stored fields.
"""
from __future__ import annotations

# ---------- N089: Missed Money Estimator ----------
#
# Conservative ranges grounded in publicly typical small-business data
# for these niches. These are estimates, not facts — confidence label
# below reflects how many weakness signals back the number.

NICHE_CONVERSIONS_PER_MONTH = {
    "mover": (5, 15),
    "moving": (5, 15),
    "roofer": (3, 8),
    "roofing": (3, 8),
    "hvac": (5, 12),
    "plumber": (5, 15),
    "plumbing": (5, 15),
    "dentist": (8, 20),
    "med spa": (5, 12),
    "medspa": (5, 12),
    "groomer": (8, 25),
    "grooming": (8, 25),
    "auto": (8, 20),
    "attorney": (2, 6),
    "lawyer": (2, 6),
}

NICHE_AVG_TICKET = {
    "mover": (500, 1500),
    "moving": (500, 1500),
    "roofer": (2000, 8000),
    "roofing": (2000, 8000),
    "hvac": (300, 2500),
    "plumber": (200, 1500),
    "plumbing": (200, 1500),
    "dentist": (150, 800),
    "med spa": (200, 1000),
    "medspa": (200, 1000),
    "groomer": (60, 150),
    "grooming": (60, 150),
    "auto": (150, 1200),
    "attorney": (500, 5000),
    "lawyer": (500, 5000),
}

# Weakness tag → fraction of monthly conversions estimated lost to it.
# Tags match what commands.pain_points_from_html emits.
WEAKNESS_LEAK_FRACTION = {
    "no online booking": 0.20,
    "no contact form": 0.10,
    "weak mobile": 0.15,
    "no SSL": 0.05,
    "no social presence": 0.05,
    "empty page": 0.40,
}


def _niche_lookup(niche, table):
    if not niche:
        return None
    n = niche.lower()
    for key, value in table.items():
        if key in n:
            return value
    return None


def _first_name(full_name):
    if not full_name:
        return None
    parts = str(full_name).strip().split()
    return parts[0] if parts else None


def estimate_missed_money(lead):
    """Return monthly revenue leak range for a lead.

    Output: {low, high, confidence, note}.
    - low/high are USD/month, or None when niche is unknown.
    - confidence ∈ {"low","med","high"} — scales with weakness-signal count.
    - note explains the basis in one short phrase.
    """
    pain = lead.get("pain_points") or []
    if isinstance(pain, str):
        pain = [pain]

    conv_range = _niche_lookup(lead.get("niche"), NICHE_CONVERSIONS_PER_MONTH)
    ticket_range = _niche_lookup(lead.get("niche"), NICHE_AVG_TICKET)
    if not conv_range or not ticket_range:
        return {"low": None, "high": None, "confidence": "low",
                "note": "niche unknown — no estimate"}

    leak = sum(WEAKNESS_LEAK_FRACTION.get(tag, 0.0) for tag in pain)
    leak = min(leak, 0.60)  # never claim more than 60% leakage

    if leak == 0:
        return {"low": 0, "high": 0, "confidence": "low",
                "note": "no detected weakness"}

    lo_conv, hi_conv = conv_range
    lo_ticket, hi_ticket = ticket_range
    low = int(lo_conv * leak * lo_ticket)
    high = int(hi_conv * leak * hi_ticket)

    if len(pain) >= 3:
        conf = "high"
    elif len(pain) >= 2:
        conf = "med"
    else:
        conf = "low"
    return {"low": low, "high": high, "confidence": conf,
            "note": f"based on {len(pain)} weakness signal(s)"}


# ---------- N084: Live Script Generator ----------
#
# Template-based, sounds like a human. NOT AI-generated. Each weakness
# tag maps to a colloquial hook a salesperson would actually say.

WEAKNESS_HOOK = {
    "no online booking":
        "your site doesn't have a way for people to book or grab a quote online",
    "no contact form":
        "your site doesn't have a contact form — anyone landing at 11pm just bounces",
    "weak mobile":
        "your site isn't really set up for phones, which is where most of these searches come from now",
    "no SSL":
        "your site is still on http, so Chrome's flagging it as 'not secure' to visitors",
    "no social presence":
        "I couldn't find you on social — that's usually where local searches start these days",
    "empty page":
        "your site looks like it's barely loading right now — visitors probably bounce before they see anything",
}


def generate_script(lead, sender_name="Jay"):
    """Build a short, human-feeling cold-call opener for one lead.

    Two sentences. Owner first name if known, business + city if known,
    then a pain-specific hook. Falls back gracefully when fields are missing.
    """
    owner = _first_name(lead.get("owner"))
    business = (lead.get("name") or "").strip()
    city = lead.get("city")
    niche = lead.get("niche")
    pain = lead.get("pain_points") or []
    if isinstance(pain, str):
        pain = [pain]

    if owner:
        line1 = f"Hey {owner}, this is {sender_name}"
    else:
        line1 = f"Hey, this is {sender_name}"
    if business:
        line1 += f" — I was just looking at {business}"
    if city:
        line1 += f" over in {city}"
    line1 += "."

    hook = None
    for tag in pain:
        if tag in WEAKNESS_HOOK:
            hook = WEAKNESS_HOOK[tag]
            break
    if not hook and pain:
        hook = f"I noticed {pain[0]} on your site"

    if hook:
        line2 = f" Quick thing — {hook}. Are you the right person to talk to about fixing that?"
    elif niche:
        line2 = f" Quick question — are you still taking on new {niche} jobs, or are you booked out?"
    else:
        line2 = " Got a quick minute?"

    return line1 + line2


# ---------- N098: Daily Brief ----------
#
# Wraps the already-scored call queue with per-lead script + money estimate.
# Caller is responsible for passing in commands.call_queue(limit=N) output.
# Decoupling this means the UI can also pass a filtered/custom queue.

def daily_brief(call_queue_leads, sender_name="Jay", limit=10):
    """Bundle scored leads with script + missed-money for the operator view.

    Input: iterable of lead dicts already carrying _score/_label/_signals
    (i.e. the output of commands.call_queue). Returns a list of dicts
    ready to render. Does not re-score, fetch, or mutate the leads.
    """
    brief = []
    for lead in list(call_queue_leads)[:limit]:
        brief.append({
            "id": lead.get("id"),
            "name": lead.get("name"),
            "owner": lead.get("owner"),
            "phone": lead.get("phone"),
            "email": lead.get("email"),
            "city": lead.get("city"),
            "state": lead.get("state"),
            "niche": lead.get("niche"),
            "score": lead.get("_score"),
            "label": lead.get("_label"),
            "signals": lead.get("_signals", []),
            "pain_points": lead.get("pain_points", []),
            "script": generate_script(lead, sender_name=sender_name),
            "missed_money": estimate_missed_money(lead),
        })
    return brief


if __name__ == "__main__":
    # Smoke test: load real leads, build a brief, print a sample.
    import json
    import os
    import commands

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "leads.json"), "r", encoding="utf-8") as f:
        leads = json.load(f).get("leads", [])

    # Inject sample pain_points / owner on a couple of leads so the demo
    # exercises every code path even when the live data is sparse.
    demo = []
    for l in leads[:6]:
        copy = dict(l)
        if not copy.get("pain_points"):
            copy["pain_points"] = ["no online booking", "weak mobile"]
        demo.append(copy)

    queue = []
    for l in demo:
        s = commands.score_lead(l)
        l["_score"] = s["score"]
        l["_label"] = s["label"]
        l["_signals"] = s["signals"]
        queue.append(l)
    queue.sort(key=lambda l: l["_score"], reverse=True)

    brief = daily_brief(queue, sender_name="Jay", limit=5)
    print(f"=== MARKO daily brief ({len(brief)} leads) ===\n")
    for entry in brief:
        mm = entry["missed_money"]
        money = (f"${mm['low']:,}-${mm['high']:,}/mo ({mm['confidence']})"
                 if mm["low"] is not None else f"n/a ({mm['note']})")
        print(f"[{entry['label']} {entry['score']}] {entry['name'] or entry['id']}")
        print(f"  phone: {entry['phone'] or '-'}   missed: {money}")
        print(f"  pain : {entry['pain_points']}")
        print(f"  >>   {entry['script']}")
        print()
