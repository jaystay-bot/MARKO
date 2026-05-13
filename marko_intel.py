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


# ---------- N084: Email Generator ----------
#
# Three template-driven variants for one lead. No AI. Same pain-tag hooks
# as the call script for tonal consistency. Output is always preview-only;
# callers must decide whether to send.

EMAIL_KINDS = ("intro", "followup", "breakup")


def generate_email(lead, kind="intro", sender_name="Jay", config=None):
    """Return {kind, subject, body} for one lead. Never auto-sends.

    Falls back to safe generic copy when fields are missing. When `config`
    is supplied, appends the compliance footer (unsubscribe + physical
    address) from config.unsubscribe_text / config.physical_address so
    the rendered preview matches what would actually be sent.
    """
    if kind not in EMAIL_KINDS:
        kind = "intro"

    owner_first = _first_name(lead.get("owner")) or "there"
    business = (lead.get("name") or "your business").strip()
    city = lead.get("city") or "your area"
    niche = (lead.get("niche") or "local business").strip()
    pain = lead.get("pain_points") or []
    if isinstance(pain, str):
        pain = [pain]
    primary_hook = None
    for tag in pain:
        if tag in WEAKNESS_HOOK:
            primary_hook = WEAKNESS_HOOK[tag]
            break

    if kind == "intro":
        subject = f"Quick question about {business}"
        if primary_hook:
            body = (
                f"Hey {owner_first},\n\n"
                f"Saw {business} when I was looking around at {niche} in {city}. "
                f"Noticed {primary_hook} — usually means a few leads slip through "
                f"the cracks every month.\n\n"
                f"I build something specifically for shops in this spot. Worth a "
                f"30-second preview?\n\n"
                f"– {sender_name}"
            )
        else:
            body = (
                f"Hey {owner_first},\n\n"
                f"Saw {business} when I was looking around at {niche} in {city}. "
                f"I work with a handful of local shops in this niche and have a "
                f"quick idea I think could help.\n\n"
                f"Open to a 30-second preview?\n\n"
                f"– {sender_name}"
            )
    elif kind == "followup":
        subject = f"Re: {business}"
        body = (
            f"Hey {owner_first},\n\n"
            f"Following up on my note from earlier — figured the inbox is "
            f"probably busy.\n\n"
            f"Short version: I help {niche} shops in {city} stop losing "
            f"after-hours leads. Worth a look this week?\n\n"
            f"– {sender_name}"
        )
    else:  # breakup
        subject = f"Closing the loop on {business}"
        body = (
            f"Hey {owner_first},\n\n"
            f"Reaching out one last time before I close the file on {business}. "
            f"If now isn't the right time, totally fine — just hit reply with a "
            f"\"not now\" and I'll stop bugging you.\n\n"
            f"Otherwise, the preview I mentioned is still here when you want it.\n\n"
            f"– {sender_name}"
        )

    if config:
        try:
            from marko_compliance import append_compliance_footer
            body = append_compliance_footer(
                body,
                unsubscribe_text=config.get("unsubscribe_text"),
                physical_address=config.get("physical_address"),
            )
        except Exception:
            pass

    return {"kind": kind, "subject": subject, "body": body}


# ---------- N183: Voicemail Generator (15-second variant) ----------

def generate_voicemail(lead, sender_name="Jay"):
    """Short voicemail script for one lead. ~15 seconds when read aloud.

    Pure function over existing fields. Uses owner first name when known,
    one pain hook if available, ends with a soft callback CTA.
    """
    owner = _first_name(lead.get("owner"))
    business = (lead.get("name") or "your business").strip()
    pain = lead.get("pain_points") or []
    if isinstance(pain, str):
        pain = [pain]
    hook = None
    for tag in pain:
        if tag in WEAKNESS_HOOK:
            hook = WEAKNESS_HOOK[tag]
            break

    if owner:
        line1 = f"Hey {owner}, this is {sender_name}."
    else:
        line1 = f"Hey, this is {sender_name}."
    if hook:
        line2 = f" Quick voicemail about {business} -- {hook}. I'll text you the details. Call back when you get a sec."
    else:
        line2 = f" Quick voicemail about {business}. I'll text you the details -- call back when you get a sec."
    return line1 + line2


# ---------- N191: Why They Buy ----------
#
# Niche -> recommended-service angle pairing. Maps loosely; only used as a
# nudge, not a hard claim. UI always shows it as "recommended angle:" not "fact:".

NICHE_FIT = {
    "mover": ("BookerMove", "after-hours booking capture"),
    "moving": ("BookerMove", "after-hours booking capture"),
    "roofer": ("TalkBot", "storm-call triage"),
    "roofing": ("TalkBot", "storm-call triage"),
    "groomer": ("GroomerOS", "blade-length + per-job pricing"),
    "grooming": ("GroomerOS", "blade-length + per-job pricing"),
    "hvac": ("TalkBot", "missed-emergency-call recovery"),
    "plumber": ("TalkBot", "missed-emergency-call recovery"),
    "plumbing": ("TalkBot", "missed-emergency-call recovery"),
    "med spa": ("TalkBot", "appointment-fill on slow days"),
    "medspa": ("TalkBot", "appointment-fill on slow days"),
    "detail": ("BookerMove", "repeat-detail reminder flow"),
    "detailing": ("BookerMove", "repeat-detail reminder flow"),
    "tow": ("TalkBot", "after-hours dispatch capture"),
    "towing": ("TalkBot", "after-hours dispatch capture"),
    "restaurant": ("TalkBot", "slow-day fill without discount platforms"),
    "salon": ("TalkBot", "appointment-fill on slow days"),
    "hair": ("TalkBot", "appointment-fill on slow days"),
}

# Common operator objections (one per niche cluster) -- for the proof angle.
OBJECTION = {
    "mover": "they think they 'always pick up' -- but ask about Saturday at 6pm",
    "roofer": "they say 'storms are unpredictable' -- that's exactly why triage matters",
    "groomer": "they're skeptical of software, anchor on per-job revenue lift",
    "hvac": "they're loyal to their phone system, focus on after-hours leak",
    "plumber": "they're loyal to their phone system, focus on after-hours leak",
    "med spa": "they think discounts fill slots -- anchor on retention not discounting",
    "detail": "they say 'we already text' -- anchor on no-show reduction",
    "tow": "they think their dispatch is fine -- ask about peak nights",
    "restaurant": "they think foot traffic is the lever -- anchor on Tuesday lunch fill",
}


def _niche_match(niche, table):
    if not niche:
        return None
    nlow = niche.lower()
    for key, value in table.items():
        if key in nlow:
            return value
    return None


def why_they_buy(lead):
    """N191: structured buy-angle for one lead. Pure read.

    Output keys:
      - angle: one-line reason this lead would care
      - primary_pain: the highest-impact pain tag (or None)
      - recommended_service: which MARKO product fits or None
      - service_reason: short phrase for why the service fits
      - likely_objection: how they'll push back
      - confidence: "low"/"med"/"high" based on signal density
    """
    pain = lead.get("pain_points") or []
    if isinstance(pain, str):
        pain = [pain]
    niche = lead.get("niche") or ""

    primary_pain = None
    for tag in pain:
        if tag in WEAKNESS_HOOK:
            primary_pain = tag
            break
    if not primary_pain and pain:
        primary_pain = pain[0]

    fit = _niche_match(niche, NICHE_FIT)
    service = fit[0] if fit else None
    service_reason = fit[1] if fit else None
    objection = _niche_match(niche, OBJECTION)

    if primary_pain and service_reason:
        angle = (f"Site shows {primary_pain}; that's the exact gap "
                 f"{service or 'our offer'} closes via {service_reason}.")
    elif primary_pain:
        angle = f"Site shows {primary_pain} -- money is leaking out of that hole."
    elif service_reason:
        angle = f"Standard {niche or 'local'} weak spot: {service_reason}."
    else:
        angle = "No clear weakness from public signals; lead is borderline."

    if len(pain) >= 3:
        conf = "high"
    elif len(pain) >= 1:
        conf = "med"
    else:
        conf = "low"

    return {
        "angle": angle,
        "primary_pain": primary_pain,
        "recommended_service": service,
        "service_reason": service_reason,
        "likely_objection": objection,
        "confidence": conf,
    }


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


# ---------- N182: Leak Report + Offer Recommendation ----------
#
# Turns the raw pain_points tag list into a salable artifact:
#   compute_leaks(lead)     -> {confirmed, inferred, needs_check}
#   recommend_offer(lead)   -> {kind, price, monthly, basis}
#   niche_key(niche_str)    -> mockup-template slug
#   mockup_variant(...)     -> "emergency" or "booking"
#
# All pure functions. No I/O. No network. No mutation of inputs.
# Whitelisted fields enforced downstream (dashboard route).

# Tag -> (human label, basis phrase). Confirmed only when site-scan evidence exists.
LEAK_LABELS = {
    "no contact form":   ("No after-hours intake on the website",
                          "site scan: no <form> on landing page"),
    "no online booking": ("No online booking / quote button",
                          "site scan: no booking keywords"),
    "weak mobile":       ("Site not mobile-optimized",
                          "site scan: no viewport meta tag"),
    "no SSL":            ("Site still on http — flagged 'not secure'",
                          "URL scheme check"),
    "no social presence":("No Facebook/Instagram/TikTok links",
                          "site scan: no social URLs in page"),
    "empty page":        ("Website fails to load real content",
                          "page body was empty when fetched"),
}

# Niches where missed-call risk is a legitimate inferred leak (high-ticket urgency).
HIGH_VALUE_NICHE_NEEDLES = ("plumb", "hvac", "mover", "moving",
                            "roof", "tow", "restoration")


def _is_high_value(niche):
    if not niche:
        return False
    nl = niche.lower()
    return any(n in nl for n in HIGH_VALUE_NICHE_NEEDLES)


def compute_leaks(lead):
    """Return a structured leak report split by confidence label.

    Output:
        {
          "confirmed":   [{label, basis, tag}],
          "inferred":    [{label, basis, tag}],
          "needs_check": [{label, basis, tag}],
        }

    Confirmed = site-scan evidence (pain_points already proves it).
    Inferred  = derived from niche + lead shape, owner should sanity-check.
    Needs_check = pain we can't verify without acting (e.g. submitting a form).
    """
    pain = lead.get("pain_points") or []
    if isinstance(pain, str):
        pain = [pain]

    confirmed = []
    inferred = []
    needs_check = []

    for tag in pain:
        if tag in LEAK_LABELS:
            label, basis = LEAK_LABELS[tag]
            confirmed.append({"tag": tag, "label": label, "basis": basis})
        elif tag.startswith("site error"):
            confirmed.append({"tag": tag, "label": f"Site is broken — {tag}",
                              "basis": "HTTP status check"})
        elif tag.startswith("copyright "):
            inferred.append({"tag": tag, "label": f"Stale website ({tag})",
                             "basis": "footer copyright year"})

    # Inferred: missed-call risk on high-ticket urgency niches.
    niche = (lead.get("niche") or "").strip()
    if _is_high_value(niche) and lead.get("phone"):
        inferred.append({
            "tag": "missed-call risk",
            "label": "Likely missing inbound calls during business hours",
            "basis": f"high-ticket {niche} — needs human verification "
                     f"(call the number twice to confirm)",
        })

    # Needs-check: speed-to-lead is only verifiable by submitting a form.
    if "no contact form" not in pain:
        needs_check.append({
            "tag": "speed-to-lead",
            "label": "Speed-to-lead on form submissions",
            "basis": "cannot verify without submitting a test form",
        })

    return {
        "confirmed": confirmed,
        "inferred": inferred,
        "needs_check": needs_check,
    }


# Offer catalog. price = one-time setup. monthly = recurring (0 = none).
OFFER_BOOKERMOVE   = {"kind": "BookerMove Setup", "price": 1500, "monthly": 99}
OFFER_TALKBOT      = {"kind": "TalkBot Setup",    "price": 497,  "monthly": 99}
OFFER_QUOTE_INTAKE = {"kind": "Quote Intake Page","price": 497,  "monthly": 0}
OFFER_LANDING      = {"kind": "Mini Landing Page Rebuild",
                      "price": 797, "monthly": 0}
OFFER_AUDIT        = {"kind": "Free Lead Audit",  "price": 0,    "monthly": 0}


def recommend_offer(lead):
    """Pick the single best offer for a lead. Pure read of pain_points + niche.

    Returns {kind, price, monthly, basis}. price/monthly are USD.
    `basis` is a one-line justification the operator can read aloud.
    """
    pain = lead.get("pain_points") or []
    if isinstance(pain, str):
        pain = [pain]
    high_value = _is_high_value(lead.get("niche"))

    if "no contact form" in pain and high_value:
        return {**OFFER_BOOKERMOVE,
                "basis": "no after-hours intake + high-ticket niche"}
    if "no online booking" in pain and high_value:
        return {**OFFER_BOOKERMOVE,
                "basis": "no online booking + high-ticket niche"}
    if "no contact form" in pain:
        return {**OFFER_TALKBOT, "basis": "no after-hours intake on site"}
    if "no online booking" in pain:
        return {**OFFER_QUOTE_INTAKE,
                "basis": "no online booking on site"}
    if "weak mobile" in pain or "no SSL" in pain or "empty page" in pain:
        return {**OFFER_LANDING,
                "basis": "broken or non-mobile site"}
    # Default soft pitch
    return {**OFFER_AUDIT, "basis": "no concrete leak yet — open with an audit"}


# Mockup template slug routing. Order matters; first hit wins.
NICHE_SLUG_RULES = (
    ("plumb",    "plumbers"),
    ("hvac",     "hvac"),
    ("mover",    "movers"),
    ("moving",   "movers"),
    ("roof",     "roofers"),
    ("med spa",  "med_spas"),
    ("medspa",   "med_spas"),
    ("groomer",  "groomers"),
    ("grooming", "groomers"),
    ("mechanic", "auto_shops"),
    ("auto",     "auto_shops"),
    ("detail",   "detailers"),
    ("tow",      "towing"),
    ("barber",   "salons"),
    ("salon",    "salons"),
    ("hair",     "salons"),
)

MOCKUP_NICHES = ("plumbers", "hvac", "movers", "roofers", "towing",
                 "groomers", "auto_shops", "med_spas", "detailers", "salons")

PRIMARY_VARIANT = {
    "plumbers":   "emergency",
    "hvac":       "emergency",
    "towing":     "emergency",
    "movers":     "emergency",
    "roofers":    "emergency",
    "med_spas":   "booking",
    "salons":     "booking",
    "groomers":   "booking",
    "auto_shops": "booking",
    "detailers":  "booking",
}


def niche_key(niche):
    """Map a free-text niche string to a mockup-template slug, or None."""
    if not niche:
        return None
    nl = niche.lower()
    for needle, slug in NICHE_SLUG_RULES:
        if needle in nl:
            return slug
    return None


def mockup_variant(lead, override=None):
    """Pick mockup variant for a lead. Override accepted from query string."""
    if override in ("emergency", "booking"):
        return override
    slug = niche_key(lead.get("niche"))
    return PRIMARY_VARIANT.get(slug, "booking")


def whitelisted_lead(lead):
    """Strip a lead down to the five fields the mockup is allowed to read."""
    return {
        "name":  (lead.get("name")  or "").strip() or "Your Business",
        "city":  (lead.get("city")  or "").strip() or "—",
        "state": (lead.get("state") or "").strip() or "",
        "phone": (lead.get("phone") or "").strip() or "—",
        "niche": (lead.get("niche") or "").strip() or "local business",
    }


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
