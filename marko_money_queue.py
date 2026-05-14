"""MARKO one-click money queue (N-MARKO-ONE-CLICK-MONEY-QUEUE).

Per-mover record with a real, leak-specific outreach draft. Jay reads,
approves (or skips/edits/retries), then sends one at a time through the
existing email_client. Nothing here ever sends.

Inputs (no scrape, no HTTP):
  leads.json                   real public mover businesses + observed pain_points
  movers.json                  buyer registry (mover_id mapping)
  hot_zips.json                derived public demand
  routing_ready.json           per-opp mover preview (drives capture URL)
  overnight_money_queue.json   shared score / call_priority source-of-truth

Output:
  money_queue.json             ranked, draftable, send_status="draft_only"

Status state machine (review queue):
  draft_only  initial state, no Jay decision yet
  approved    Jay clicked approve; eligible for /review/<id>/send
  skipped     Jay declined; will not surface again unless reset
  retry_later Jay deferred; will surface in next overnight rebuild
  edited      Jay rewrote the draft; treat as approved on next surface
  sent        send route completed (dry_run or live)
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
MOVERS_FILE = os.path.join(BASE_DIR, "movers.json")
HOT_ZIPS_FILE = os.path.join(BASE_DIR, "hot_zips.json")
QUEUE_SOURCE_FILE = os.path.join(BASE_DIR, "overnight_money_queue.json")
MONEY_QUEUE_FILE = os.path.join(BASE_DIR, "money_queue.json")

DRAFT_ONLY = "draft_only"
ALLOWED_STATUSES = {
    DRAFT_ONLY, "approved", "skipped", "retry_later", "edited", "sent",
}

# Rate-limit guidance. Documented, not enforced -- the send route reads
# this and refuses to fire above the day cap, but there is no scheduler.
RATE_LIMIT_GUIDANCE = {
    "max_sends_per_day": 10,
    "min_minutes_between_sends": 5,
    "duplicate_window_days": 30,
    "domain_warming_note": (
        "First 7 days on a fresh sending domain: cap at 5/day, "
        "reply-rate matters more than volume. After 7 days of clean "
        "deliverability (no bounces, no spam complaints), raise to "
        "max_sends_per_day."
    ),
}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path, default):
    try:
        return storage.read_json(path)
    except FileNotFoundError:
        return default


# ---------- Leak detection (real signals only) ----------

# Each detector returns (category, plain-English evidence string) or None.
# Evidence strings get rendered into the email body verbatim, so they
# must read like a sentence Jay would write -- no template scaffolding,
# no all-caps tags, no copy that hallucinates anything we didn't observe.

def _detect_no_quote_form(lead):
    pains = [p.lower() for p in (lead.get("pain_points") or [])]
    if any("no contact form" in p for p in pains):
        return ("no_quote_form",
                "no quote/contact form on the public site")
    return None


def _detect_no_online_booking(lead):
    pains = [p.lower() for p in (lead.get("pain_points") or [])]
    if any("no online booking" in p for p in pains):
        return ("no_online_booking",
                "no online booking flow -- callers can't lock in a slot")
    return None


_COPYRIGHT_RE = re.compile(r"copyright\s+20(\d{2})", re.I)


def _detect_stale_site(lead):
    for p in (lead.get("pain_points") or []):
        m = _COPYRIGHT_RE.search(p or "")
        if m:
            year = int("20" + m.group(1))
            return ("stale_site",
                    f"site footer still shows copyright {year} -- the web "
                    "presence isn't actively maintained")
    return None


def _detect_owner_overload(lead):
    """Owner-operated + weak capture = phone-tag/missed-call pain.

    This is a *derived* leak, not a hallucinated one: the inputs are
    observed (gmail-like address OR 804 local number) AND observed
    weak-capture pain points. Both conditions come from the original
    public scrape, not from imagination.
    """
    email = (lead.get("email") or "").lower()
    phone = (lead.get("phone") or "")
    pains = [p.lower() for p in (lead.get("pain_points") or [])]
    looks_owner = (
        email.endswith("@gmail.com")
        or email.endswith("@yahoo.com")
        or phone.replace("-", "").replace(" ", "").replace("(", "").replace(")", "").startswith("804")
    )
    weak_capture = any("no contact form" in p or "no online booking" in p
                       for p in pains)
    if looks_owner and weak_capture:
        return ("owner_overload",
                "looks owner-operated (local number / personal email) and "
                "the site has no automated capture, so after-hours callers "
                "go to voicemail")
    return None


_DETECTORS = (
    _detect_no_quote_form,
    _detect_no_online_booking,
    _detect_stale_site,
    _detect_owner_overload,
)


def detect_leaks(lead):
    """Return list of (category, evidence) tuples. Empty if nothing real."""
    out = []
    seen = set()
    for det in _DETECTORS:
        result = det(lead)
        if result and result[0] not in seen:
            seen.add(result[0])
            out.append(result)
    return out


# ---------- Pitch + draft (leak-specific, non-generic) ----------

# Each angle is short, local, and references the leak. No "AI", no "grow
# your business", no "10x". Subject lines are plain-business; body opens
# with the observed signal, names the pain, names the offer, signs Jay.

PITCH_BY_LEAK = {
    "no_quote_form": {
        "angle": (
            "your site has no quote form -- after-hours visitors can't "
            "leave their move details, so they bounce to a competitor"
        ),
        "subject_template": (
            "{business} -- a moving lead while your phone is busy"
        ),
        "body_open": (
            "I was looking at {website} and noticed there's no quick way "
            "for someone to drop their move details if they can't get you "
            "on the phone. That's where leads bleed at night."
        ),
    },
    "no_online_booking": {
        "angle": (
            "no way for a customer to lock in a date themselves -- "
            "every booking goes through phone tag"
        ),
        "subject_template": (
            "{business} -- letting customers lock in a move date"
        ),
        "body_open": (
            "Looked at {website}. Customers can read about you but they "
            "can't lock in a date themselves -- every booking is phone "
            "tag, which is fine until you're on a job."
        ),
    },
    "stale_site": {
        "angle": (
            "the site footer still shows an old copyright year -- "
            "leads probably ask 'are they still in business?'"
        ),
        "subject_template": (
            "{business} -- one-page fix that picks up after-hours leads"
        ),
        "body_open": (
            "Checked {website}. The footer still shows an older "
            "copyright year, which makes some movers wonder if you're "
            "still active. That same page has no quote form, so the "
            "ones who do trust it can't leave details."
        ),
    },
    "owner_overload": {
        "angle": (
            "you take the calls yourself -- after-hours and "
            "between-job callers go to voicemail"
        ),
        "subject_template": (
            "{business} -- catching the after-hours moving calls"
        ),
        "body_open": (
            "I'm guessing you take most of the booking calls yourself. "
            "Looked at {website} -- there's no automated way for a "
            "customer to leave their move details when you're on a job, "
            "so those calls go to voicemail."
        ),
    },
}

OFFER_LINE = (
    "I'd send you ONE moving lead from your service area free as a test "
    "-- no contract, no signup. If it's a real fit, future leads are "
    "$20-$50 each, pay as you go."
)

CAPTURE_LINE_TEMPLATE = (
    "Public intake page -- this is where the leads come from: "
    "{capture_url}"
)

SIGN_OFF = "-- Jay, BookerMove (Richmond, VA)"


def _strongest_leak(leaks):
    """Pick the highest-impact leak as the pitch driver. Order mirrors the
    money the leak directly costs the mover: owner_overload > no_quote_form
    > no_online_booking > stale_site.
    """
    priority = ("owner_overload", "no_quote_form",
                "no_online_booking", "stale_site")
    by_cat = {cat: ev for cat, ev in leaks}
    for cat in priority:
        if cat in by_cat:
            return cat, by_cat[cat]
    return (None, None) if not leaks else leaks[0]


def _capture_url(mover_id):
    base = "https://quote.bookermove.com/quote?source=marko&campaign=richmond_movers"
    if mover_id:
        return f"{base}&mover_hint={mover_id}"
    return base


def _draft_email(business, website, leak_category, capture_url):
    pitch = PITCH_BY_LEAK.get(leak_category)
    if pitch is None:
        return None
    subject = pitch["subject_template"].format(business=business)
    body_open = pitch["body_open"].format(website=website or "your site")
    body = "\n\n".join([
        body_open,
        OFFER_LINE,
        CAPTURE_LINE_TEMPLATE.format(capture_url=capture_url),
        SIGN_OFF,
    ])
    return subject, body


def _confidence_band(score):
    if score >= 90:  return "high"
    if score >= 40:  return "medium"
    return "low"


def _close_prob(score):
    if score >= 90:  return "20-35%"
    if score >= 70:  return "12-20%"
    if score >= 40:  return "5-12%"
    if score >= 10:  return "2-5%"
    return "<2%"


def build_queue():
    leads_doc = _load(LEADS_FILE, {"leads": []})
    leads = [l for l in leads_doc.get("leads", [])
             if (l.get("niche") or "").lower() == "movers"]
    movers = _load(MOVERS_FILE, {"movers": []}).get("movers", [])
    movers_by_lead_id = {m.get("source_lead_id"): m for m in movers
                         if m.get("source_lead_id")}
    queue_src = _load(QUEUE_SOURCE_FILE, {"targets": []}).get("targets", [])
    score_by_lead = {t.get("lead_id"): t for t in queue_src}

    rows = []
    for l in leads:
        leaks = detect_leaks(l)
        if not leaks:
            # Nothing real to pitch on. Skip rather than invent a leak.
            continue
        leak_cat, leak_evidence = _strongest_leak(leaks)
        mover = movers_by_lead_id.get(l.get("id"))
        mover_id = (mover or {}).get("mover_id")
        capture_url = _capture_url(mover_id)
        # Use the same business-name resolution as marko_overnight so the
        # two queues agree on the human-readable name.
        from marko_overnight import _short_name
        business = _short_name(l.get("name") or "", l.get("website"))

        scored = score_by_lead.get(l.get("id"), {})
        score = scored.get("score", 0)
        priority = scored.get("call_priority", "low")

        draft = _draft_email(business, l.get("website") or "", leak_cat,
                             capture_url)
        if draft is None:
            continue
        subject, body = draft

        rows.append({
            "lead_id": l.get("id"),
            "mover_id": mover_id,
            "business_name": business,
            "city": l.get("city") or "",
            "website": l.get("website") or "",
            "phone": l.get("phone") or "",
            "email": l.get("email") or "",
            "priority": priority,
            "confidence_score": score,
            "confidence_band": _confidence_band(score),
            "estimated_close_probability": _close_prob(score),
            "leaks_observed": [
                {"category": c, "evidence": e} for c, e in leaks
            ],
            "detected_leak": leak_evidence,
            "leak_category": leak_cat,
            "why_they_might_buy": (
                f"Real public-data weakness: {leak_evidence}. They are a "
                f"{priority.replace('_', ' ')} ranked target."
            ),
            "recommended_pitch_angle": (PITCH_BY_LEAK[leak_cat]["angle"]
                                        if leak_cat in PITCH_BY_LEAK else ""),
            "capture_url": capture_url,
            "email_subject": subject,
            "email_body": body,
            "send_status": DRAFT_ONLY,
            "review_state": {
                "approved_at": None,
                "skipped_at": None,
                "edited_at": None,
                "retry_after": None,
                "sent_at": None,
                "send_result": None,
            },
        })

    # Rank by score desc; drafts without an actionable email address sink.
    rows.sort(key=lambda r: (bool(r["email"]), r["confidence_score"]),
              reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def write_queue():
    rows = build_queue()
    payload = {
        "schema_version": "1.0.0",
        "generated_at": _now_iso(),
        "kind": "money_queue",
        "row_count": len(rows),
        "policy": {
            "auto_send": False,
            "default_send_mode": "dry_run",
            "live_send_requires": [
                "MARKO_OUTREACH_LIVE=1",
                "ADMIN_TOKEN match",
                "either MARKO_SMOKE_REDIRECT_TO set OR confirm_real=1 query param",
            ],
            "rate_limit_guidance": RATE_LIMIT_GUIDANCE,
        },
        "rows": rows,
    }
    storage.write_json(MONEY_QUEUE_FILE, payload)
    return payload


# ---------- Mutation helpers (used by /review routes) ----------

def _read_queue():
    return _load(MONEY_QUEUE_FILE, {"rows": []})


def _write_queue(doc):
    storage.write_json(MONEY_QUEUE_FILE, doc)


def find_row(lead_id):
    doc = _read_queue()
    for r in doc.get("rows", []):
        if r.get("lead_id") == lead_id:
            return doc, r
    return doc, None


def set_status(lead_id, new_status, **review_updates):
    """Mutate one row's send_status and review_state. Returns the new row.

    Refuses unknown statuses (defends the audit log from typos in URLs).
    """
    if new_status not in ALLOWED_STATUSES:
        raise ValueError(f"unknown status {new_status!r}")
    doc, row = find_row(lead_id)
    if row is None:
        return None
    row["send_status"] = new_status
    row["review_state"].update(review_updates)
    _write_queue(doc)
    return row


if __name__ == "__main__":
    payload = write_queue()
    print(json.dumps({
        "row_count": payload["row_count"],
        "by_priority": {
            p: sum(1 for r in payload["rows"] if r["priority"] == p)
            for p in ("call_today", "warm", "low")
        },
        "by_leak": {
            cat: sum(1 for r in payload["rows"]
                     if r["leak_category"] == cat)
            for cat in PITCH_BY_LEAK
        },
        "file": os.path.relpath(MONEY_QUEUE_FILE, BASE_DIR),
    }, indent=2))
