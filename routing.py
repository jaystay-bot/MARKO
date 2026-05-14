"""MARKO inbound-demand routing engine.

One job: take a customer move request, pick the best mover from the
buyer registry, deliver the lead by email, log the result.

Reuses storage.read_json/write_json (so KV backend works on Vercel) and
email_client.send (so dry_run safety belt + Resend integration are
unchanged). Does not import flask -- callable from the dashboard route
or from a verification script with identical behavior.

Three append-only audit logs:
  inbound_leads.json   submission record (PII, internal use)
  routed_leads.json    routing decision per submission
  delivery_log.json    per-email send outcome
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import email_client
import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MOVERS_FILE = os.path.join(BASE_DIR, "movers.json")
INBOUND_FILE = os.path.join(BASE_DIR, "inbound_leads.json")
ROUTED_FILE = os.path.join(BASE_DIR, "routed_leads.json")
DELIVERY_LOG_FILE = os.path.join(BASE_DIR, "delivery_log.json")
# N-OVERNIGHT-MONEY-SAFE-CAPTURE: leads whose owner-notify failed (or was
# skipped due to env) land here so Jay can recover them by hand. Loud
# surfacing -- never let an inbound lead vanish silently.
MISSED_MONEY_FILE = os.path.join(BASE_DIR, "missed_money.json")

REQUIRED_LEAD_FIELDS = (
    "lead_id", "source", "submitted_at",
    "customer_name", "phone", "email",
    "move_date", "pickup_zip", "dropoff_zip",
    "home_size", "stairs_elevator", "heavy_items",
    "urgency", "notes",
)

URGENCY_VALUES = {"asap", "this_week", "this_month", "flexible"}

# Allowed values for lead.source. The web form sets "inbound_quote";
# the TalkBot HTTP endpoint sets "inbound_talkbot". Smoke runs use
# "smoke" so they're trivially filterable in the audit logs.
SOURCE_VALUES = {"inbound_quote", "inbound_talkbot", "smoke"}

# ---------- Live-delivery env contract (N-MARKO-LIVE-DELIVERY-SMOKE) ----------
#
# RESEND_API_KEY            Resend API key (required for live send)
# MARKO_FROM_EMAIL          Verified Resend sender, e.g. leads@yourdomain.com
# MARKO_QUOTE_LIVE_SEND     "1" enables live send on POST /quote
# MARKO_MOVER_ALLOWLIST     CSV of mover_ids permitted to receive live email
#                           (e.g. "M001"). Anything not listed -> dry_run only,
#                           no matter what MARKO_QUOTE_LIVE_SEND says.
# MARKO_SMOKE_REDIRECT_TO   If set, live send replaces the mover's email with
#                           this address. Used to smoke-test delivery without
#                           emailing a real third-party business. Original
#                           recipient is recorded in delivery_log under
#                           "to_original".
# ADMIN_TOKEN               Gates /admin/delivery and /admin/delivery_smoke
# MARKO_OWNER_NOTIFY_TO     If set, every successful inbound lead also fires
#                           a separate owner-review email here, INDEPENDENT of
#                           the mover-routing path. This is the overnight
#                           money-safe path: customer demand always lands in
#                           the owner inbox even when the mover-side is dry_run.
# TALKBOT_INBOUND_TOKEN     Shared secret for X-Talkbot-Token on
#                           POST /api/talkbot/inbound


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def env_status():
    """Snapshot of live-delivery environment. No secrets ever returned."""
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    from_email = (os.environ.get("MARKO_FROM_EMAIL") or "").strip()
    live = (os.environ.get("MARKO_QUOTE_LIVE_SEND") or "").strip() == "1"
    allowlist = _allowlist()
    redirect_to = (os.environ.get("MARKO_SMOKE_REDIRECT_TO") or "").strip()
    return {
        "resend_api_key_set": bool(api_key),
        "marko_from_email": from_email or None,
        "marko_quote_live_send": live,
        "mover_allowlist": sorted(allowlist) if allowlist else [],
        "smoke_redirect_to": redirect_to or None,
        "admin_token_set": bool((os.environ.get("ADMIN_TOKEN") or "").strip()),
    }


def _allowlist():
    """Parse MARKO_MOVER_ALLOWLIST. Empty/unset -> empty set (= no live sends)."""
    raw = (os.environ.get("MARKO_MOVER_ALLOWLIST") or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _redirect_to():
    return (os.environ.get("MARKO_SMOKE_REDIRECT_TO") or "").strip() or None


def _from_email_default():
    return (os.environ.get("MARKO_FROM_EMAIL") or "").strip() or "leads@marko.local"


def _owner_notify_to():
    """Where to send owner-review emails. Empty/unset -> no owner notify."""
    return (os.environ.get("MARKO_OWNER_NOTIFY_TO") or "").strip() or None


# ---------- Lead enrichment (value + quality) ----------

# Conservative all-in residential-move price ranges (USD), grounded in
# typical local-move quotes. Long-haul (interstate) is out of scope --
# this is small-business intuition, not a quote engine.
_VALUE_BY_HOME_SIZE = {
    "studio":             (250, 600),
    "1 bedroom":          (350, 900),
    "2 bedroom":          (500, 1500),
    "3 bedroom":          (800, 2500),
    "4+ bedroom":         (1200, 4000),
    "office / commercial": (800, 5000),
    "other":              (300, 1500),
}


def estimate_lead_value(lead):
    """Return (low_usd, high_usd) for this lead. Never raises.

    Bumps high end when heavy/specialty items are present (piano, safe,
    treadmill, etc.) since those drive billable-hour overruns. Honest
    fallback (300-1500) when home_size is missing.
    """
    size = (lead.get("home_size") or "").strip().lower()
    low, high = _VALUE_BY_HOME_SIZE.get(size, (300, 1500))
    heavy = (lead.get("heavy_items") or "").strip().lower()
    if heavy and heavy not in ("(none listed)", "none", "no", "n/a"):
        high = int(high * 1.25)
    return low, high


def compute_lead_quality(lead):
    """Return 'HOT' | 'WARM' | 'COOL'. Pure function over lead fields.

    HOT  = ASAP urgency + named customer + (phone OR email)
    WARM = this_week / this_month + named customer + (phone OR email)
    COOL = everything else (still routed, still notified, lower priority)

    Email-or-phone acceptance: a customer who only shares email is still
    actionable -- the owner can reply by email. Don't downgrade quality
    just because phone is missing.
    """
    name = (lead.get("customer_name") or "").strip()
    phone = (lead.get("phone") or "").strip()
    email = (lead.get("email") or "").strip()
    urgency = (lead.get("urgency") or "").strip().lower()
    reachable = bool(phone or email)
    if urgency == "asap" and name and reachable:
        return "HOT"
    if urgency in ("this_week", "this_month") and name and reachable:
        return "WARM"
    return "COOL"


def new_lead_id():
    return f"Q-{uuid.uuid4().hex[:8]}"


def _load(path, default):
    try:
        return storage.read_json(path)
    except FileNotFoundError:
        return default


def _save(path, data):
    storage.write_json(path, data)


def _append(path, key, entry):
    data = _load(path, {key: []})
    if not isinstance(data.get(key), list):
        data[key] = []
    data[key].append(entry)
    _save(path, data)


def load_movers():
    return _load(MOVERS_FILE, {"movers": []}).get("movers", [])


def build_lead(form, source="inbound_quote", talkbot_session_id=None):
    """Construct an inbound lead packet from a flat form dict.

    Pulls each REQUIRED_LEAD_FIELDS value from `form`, defaulting to "".
    Generates lead_id and submitted_at server-side. `source` defaults to
    the web-form value; the TalkBot endpoint passes "inbound_talkbot".
    Unknown source values fall back to "inbound_quote" so a malformed
    caller cannot inject arbitrary tags into the audit logs.

    talkbot_session_id, if present, is preserved on the lead for trace
    join with TalkBot's own logs. It is purely informational -- routing
    behavior does not depend on it.
    """
    if source not in SOURCE_VALUES:
        source = "inbound_quote"
    lead = {
        "lead_id": new_lead_id(),
        "source": source,
        "submitted_at": _now_iso(),
        "customer_name": (form.get("customer_name") or "").strip(),
        "phone": (form.get("phone") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "move_date": (form.get("move_date") or "").strip(),
        "pickup_zip": (form.get("pickup_zip") or "").strip(),
        "dropoff_zip": (form.get("dropoff_zip") or "").strip(),
        "home_size": (form.get("home_size") or "").strip(),
        "stairs_elevator": (form.get("stairs_elevator") or "").strip(),
        "heavy_items": (form.get("heavy_items") or "").strip(),
        "urgency": (form.get("urgency") or "").strip().lower(),
        "notes": (form.get("notes") or "").strip(),
    }
    if talkbot_session_id:
        lead["talkbot_session_id"] = str(talkbot_session_id).strip()
    # Derived enrichment (N-OVERNIGHT-MONEY-SAFE-CAPTURE). Stored on the
    # lead so the owner-notify email and the audit logs share the same
    # numbers -- no drift between what Jay sees and what's on disk.
    low, high = estimate_lead_value(lead)
    lead["estimated_value_low_usd"] = low
    lead["estimated_value_high_usd"] = high
    lead["lead_quality"] = compute_lead_quality(lead)
    return lead


def validate_lead(lead):
    """Return (ok, errors). Cheap shape check before routing."""
    errors = []
    if not isinstance(lead, dict):
        return False, ["lead must be a dict"]
    for k in REQUIRED_LEAD_FIELDS:
        if k not in lead:
            errors.append(f"missing field: {k}")
    if errors:
        return False, errors
    for k in ("customer_name", "pickup_zip", "move_date"):
        if not lead[k]:
            errors.append(f"required field empty: {k}")
    # N-OVERNIGHT-MONEY-SAFE-CAPTURE: phone OR email required (not both).
    # Lets a customer submit with email only if they don't want to share a
    # phone number. Owner notify can still reach them; the mover (when
    # mover routing is enabled) gets whichever is provided.
    phone = (lead.get("phone") or "").strip()
    email = (lead.get("email") or "").strip()
    if not phone and not email:
        errors.append("at least one of phone or email is required")
    pickup = lead.get("pickup_zip") or ""
    if pickup and (len(pickup) != 5 or not pickup.isdigit()):
        errors.append("pickup_zip must be 5 digits")
    dropoff = lead.get("dropoff_zip") or ""
    if dropoff and (len(dropoff) != 5 or not dropoff.isdigit()):
        errors.append("dropoff_zip must be 5 digits")
    if lead.get("urgency") and lead["urgency"] not in URGENCY_VALUES:
        errors.append(f"urgency must be one of {sorted(URGENCY_VALUES)}")
    return (not errors), errors


def record_inbound(lead):
    """Append to inbound_leads.json. Caller is responsible for validation."""
    _append(INBOUND_FILE, "leads", lead)


def select_mover(lead, movers=None):
    """Pick the best active mover for this lead, or None.

    Priority: exact pickup_zip match wins over city match. Exclusive
    movers win over shared when both match the same tier. First match
    inside a tier wins (registry order is the tiebreak).
    """
    if movers is None:
        movers = load_movers()
    pickup_zip = (lead.get("pickup_zip") or "").strip()
    # No "city" field on the inbound lead -- mover selection is ZIP-driven.
    # cities_served is a fallback only if a future intake adds a city field.
    city = (lead.get("city") or "").strip().lower()

    candidates = [m for m in movers if m.get("active")]

    zip_matches = [
        m for m in candidates
        if pickup_zip and pickup_zip in (m.get("zip_codes") or [])
    ]
    if zip_matches:
        ex = [m for m in zip_matches if m.get("exclusive")]
        return (ex or zip_matches)[0]

    if city:
        city_matches = [
            m for m in candidates
            if any((c or "").lower() == city for c in (m.get("cities_served") or []))
        ]
        if city_matches:
            ex = [m for m in city_matches if m.get("exclusive")]
            return (ex or city_matches)[0]

    return None


def build_mover_email(lead, mover):
    """Build the (subject, body) plain-text email sent to the mover.

    Subject is intentionally plain-business: no emoji, no marketing copy,
    no AI phrasing, no bracketed tracker tags. The lead id rides on the
    X-Marko-Lead-Id header (set in route_lead) for webhook resolution.
    """
    name = lead.get("customer_name") or "a customer"
    pickup = lead.get("pickup_zip") or "unknown ZIP"
    subject = f"New move request from {name} - pickup {pickup}"

    body_lines = [
        f"Hi {mover.get('business_name')},",
        "",
        "A homeowner just submitted a moving quote request and your "
        "service area was the closest match.",
        "",
        "Customer",
        f"  Name:         {lead.get('customer_name')}",
        f"  Phone:        {lead.get('phone')}",
        f"  Email:        {lead.get('email') or '(not provided)'}",
        "",
        "Move details",
        f"  Move date:    {lead.get('move_date')}",
        f"  Pickup ZIP:   {lead.get('pickup_zip')}",
        f"  Dropoff ZIP:  {lead.get('dropoff_zip') or '(not provided)'}",
        f"  Home size:    {lead.get('home_size') or '(not provided)'}",
        f"  Stairs/elev:  {lead.get('stairs_elevator') or '(not provided)'}",
        f"  Heavy items:  {lead.get('heavy_items') or '(none listed)'}",
        f"  Urgency:      {lead.get('urgency') or '(not provided)'}",
        "",
        "Notes from customer",
        f"  {lead.get('notes') or '(none)'}",
        "",
        "Reply to this email or call the customer directly. "
        "We share each request with one local mover at a time.",
        "",
        f"Reference: {lead.get('lead_id')}",
    ]
    return subject, "\n".join(body_lines)


# ---------- Owner-review email (N-OVERNIGHT-MONEY-SAFE-CAPTURE) ----------

OWNER_SUBJECT_PREFIX = "NEW MOVING LEAD - OWNER REVIEW ONLY"


def _format_value_range(low, high):
    return f"${low:,} - ${high:,}"


def build_owner_email(lead, would_have_routed_to=None):
    """Plain-text owner-review email. Wraps to <=78 chars per line so it
    reads cleanly in Gmail mobile.

    `would_have_routed_to` is the mover the routing engine selected for
    this lead. Recorded so Jay can see who SHOULD get the lead manually,
    without the system actually contacting them.
    """
    name = (lead.get("customer_name") or "(no name)").strip()
    pickup = lead.get("pickup_zip") or "??"
    urgency = (lead.get("urgency") or "no urgency").strip() or "no urgency"
    quality = lead.get("lead_quality") or "COOL"
    low = lead.get("estimated_value_low_usd") or 0
    high = lead.get("estimated_value_high_usd") or 0

    subject = (
        f"{OWNER_SUBJECT_PREFIX} - {quality} - {urgency.upper()} - "
        f"{name} - {pickup}"
    )

    mover_line = (
        f"{would_have_routed_to.get('business_name')} "
        f"({would_have_routed_to.get('mover_id')})"
        if would_have_routed_to else "(no matching mover for this ZIP)"
    )

    body_lines = [
        "Jay,",
        "",
        "A new moving lead came in via MARKO. The system DID NOT contact",
        "any mover. This email is for your review only -- you decide who",
        "to send it to.",
        "",
        "LEAD",
        f"  Lead id:           {lead.get('lead_id')}",
        f"  Source:            {lead.get('source')}",
        f"  Submitted (UTC):   {lead.get('submitted_at')}",
        f"  Quality:           {quality}",
        f"  Estimated value:   {_format_value_range(low, high)}",
        f"  Would have routed: {mover_line}",
        "",
        "CUSTOMER",
        f"  Name:              {lead.get('customer_name')}",
        f"  Phone:             {lead.get('phone')}",
        f"  Email:             {lead.get('email') or '(not provided)'}",
        "",
        "MOVE",
        f"  Move date:         {lead.get('move_date')}",
        f"  Pickup ZIP:        {lead.get('pickup_zip')}",
        f"  Drop-off ZIP:      {lead.get('dropoff_zip') or '(not provided)'}",
        f"  Home size:         {lead.get('home_size') or '(not provided)'}",
        f"  Stairs/elev:       {lead.get('stairs_elevator') or '(not provided)'}",
        f"  Heavy items:       {lead.get('heavy_items') or '(none listed)'}",
        f"  Urgency:           {lead.get('urgency') or '(not provided)'}",
        "",
        "NOTES FROM CUSTOMER",
        f"  {lead.get('notes') or '(none)'}",
        "",
        "ACTION",
        "  Reply directly to this customer (their phone/email above), or",
        "  forward to the mover noted above. No automated mover contact",
        "  was made.",
    ]
    if lead.get("talkbot_session_id"):
        body_lines += ["", f"TalkBot session: {lead['talkbot_session_id']}"]
    return subject, "\n".join(body_lines)


def _record_missed_money(lead, reason, extra=None):
    """Append a missed-money entry. Loud surfacing for any case where the
    owner notification did not reach Jay -- env unset, Resend error,
    blocked. Stored separately from delivery_log so a daily glance shows
    only the items that need recovery.
    """
    entry = {
        "lead_id": lead.get("lead_id"),
        "at": _now_iso(),
        "reason": reason,
        "lead_summary": {
            "customer_name": lead.get("customer_name"),
            "phone": lead.get("phone"),
            "email": lead.get("email"),
            "pickup_zip": lead.get("pickup_zip"),
            "move_date": lead.get("move_date"),
            "urgency": lead.get("urgency"),
            "quality": lead.get("lead_quality"),
            "estimated_value": [
                lead.get("estimated_value_low_usd"),
                lead.get("estimated_value_high_usd"),
            ],
            "source": lead.get("source"),
        },
    }
    if extra:
        entry["extra"] = extra
    _append(MISSED_MONEY_FILE, "events", entry)


def notify_owner(lead, would_have_routed_to=None, from_email=None):
    """Send the owner-review email if MARKO_OWNER_NOTIFY_TO is set.

    Returns dict: {sent, status, message_id, to, error, missed_money}.
    Never raises. If env unset OR Resend fails OR key missing, writes a
    missed_money entry so Jay can see what didn't reach him.
    """
    to_addr = _owner_notify_to()
    sender = (from_email or "").strip() or _from_email_default()
    api_key_set = bool((os.environ.get("RESEND_API_KEY") or "").strip())

    base = {"sent": False, "status": None, "message_id": None,
            "to": to_addr, "error": None, "missed_money": False}

    if not to_addr:
        # Owner notify is opt-in. If env unset, this is silent (not a
        # failure). Don't write missed_money for this case -- it would
        # spam the file in dev/test.
        base["status"] = "owner_notify_disabled"
        return base

    if not api_key_set:
        # Env says we should notify, but we have no key. That's missed money.
        base["status"] = "no_api_key"
        base["error"] = "RESEND_API_KEY not set; owner notify cannot fire"
        _record_missed_money(lead, base["error"])
        base["missed_money"] = True
        # Still log to delivery_log so the audit trail is unified.
        _append(DELIVERY_LOG_FILE, "events", {
            "lead_id": lead.get("lead_id"),
            "source": lead.get("source"),
            "delivery_kind": "owner_notify",
            "to": to_addr,
            "from": sender,
            "subject": None,
            "provider": "resend",
            "status": "blocked",
            "delivery_mode": "blocked",
            "message_id": None,
            "provider_error": base["error"],
            "block_reasons": [base["error"]],
            "at": _now_iso(),
            "dry_run": True,
            "requested_live": True,
        })
        return base

    subject, body = build_owner_email(lead, would_have_routed_to)
    result = email_client.send(
        to=to_addr,
        subject=subject,
        body=body,
        from_=sender,
        reply_to=lead.get("email") or None,
        dry_run=False,
        headers={
            "X-Marko-Lead-Id": str(lead.get("lead_id") or ""),
            "X-Marko-Delivery-Kind": "owner_notify",
        },
    )
    base["status"] = result.get("status")
    base["message_id"] = result.get("id")
    base["error"] = result.get("error")

    delivered = result.get("status") == "sent"
    base["sent"] = delivered

    _append(DELIVERY_LOG_FILE, "events", {
        "lead_id": lead.get("lead_id"),
        "source": lead.get("source"),
        "talkbot_session_id": lead.get("talkbot_session_id"),
        "delivery_kind": "owner_notify",
        "to": to_addr,
        "from": sender,
        "subject": subject,
        "provider": "resend",
        "status": result.get("status"),
        "delivery_mode": "owner_notify",
        "message_id": result.get("id"),
        "provider_error": result.get("error"),
        "block_reasons": None,
        "at": _now_iso(),
        "dry_run": False,
        "requested_live": True,
    })

    if not delivered:
        _record_missed_money(
            lead,
            f"owner notify failed: {result.get('error')}",
            extra={"provider_status": result.get("status")},
        )
        base["missed_money"] = True

    return base


def route_lead(lead, dry_run=True, from_email=None, force_live=False):
    """Pick a mover, send the lead email, log routing + delivery.

    Live-send rules (each must be true to fire a real email):
      * dry_run=False (or force_live=True overriding it for the smoke route)
      * RESEND_API_KEY is set (enforced by email_client.send)
      * mover.mover_id is in MARKO_MOVER_ALLOWLIST
    If any of those fails, the call falls back to dry_run for the actual
    HTTP send -- but the routing record still reflects the requested mode
    so the admin panel can show "blocked: not allowlisted" rather than
    silently succeeding.

    If MARKO_SMOKE_REDIRECT_TO is set, the live email goes to that address
    instead of the mover's real inbox; the original recipient is preserved
    in delivery_log.to_original. This is the safe path for first-flight
    smoke testing without disturbing a real third-party business.

    Every outcome -- match, no-match, blocked, redirected, sent, failed --
    is recorded. No silent fallthrough.
    """
    routed_at = _now_iso()
    mover = select_mover(lead)

    if mover is None:
        record = {
            "lead_id": lead.get("lead_id"),
            "routed_at": routed_at,
            "mover": None,
            "status": "no_match",
            "email_result": None,
            "subject": None,
            "error": "no active mover matched pickup ZIP or city",
            "delivery_mode": "n/a",
        }
        _append(ROUTED_FILE, "events", record)
        return record

    subject, body = build_mover_email(lead, mover)
    sender = (from_email or "").strip() or _from_email_default()

    requested_live = bool(force_live or not dry_run)
    allowlist = _allowlist()
    allowlisted = mover.get("mover_id") in allowlist
    redirect_to = _redirect_to()
    api_key_set = bool((os.environ.get("RESEND_API_KEY") or "").strip())

    # Decide actual send mode + reason. We always log the reason so the
    # admin panel can show why a "live" attempt fell back to dry_run.
    block_reasons = []
    if requested_live and not api_key_set:
        block_reasons.append("RESEND_API_KEY not set")
    if requested_live and not allowlisted:
        block_reasons.append(
            f"mover {mover.get('mover_id')} not in MARKO_MOVER_ALLOWLIST"
        )

    actual_dry_run = (not requested_live) or bool(block_reasons)
    to_original = mover["email"]
    to_used = redirect_to if (not actual_dry_run and redirect_to) else to_original

    if actual_dry_run:
        delivery_mode = "dry_run" if not requested_live else "blocked"
    elif redirect_to:
        delivery_mode = "live_redirected"
    else:
        delivery_mode = "live"

    email_result = email_client.send(
        to=to_used,
        subject=subject,
        body=body,
        from_=sender,
        reply_to=lead.get("email") or None,
        dry_run=actual_dry_run,
        headers={"X-Marko-Lead-Id": str(lead.get("lead_id") or "")},
    )

    delivered = email_result.get("status") in ("sent", "dry_run")
    if delivery_mode == "blocked":
        status = "delivery_blocked"
    elif delivered:
        status = "routed"
    else:
        status = "delivery_failed"

    record = {
        "lead_id": lead.get("lead_id"),
        "source": lead.get("source"),
        "talkbot_session_id": lead.get("talkbot_session_id"),
        "routed_at": routed_at,
        "mover": {
            "mover_id": mover.get("mover_id"),
            "business_name": mover.get("business_name"),
            "email": mover.get("email"),
        },
        "status": status,
        "delivery_mode": delivery_mode,
        "block_reasons": block_reasons or None,
        "email_result": email_result,
        "subject": subject,
        "error": None if delivered and not block_reasons else (
            "; ".join(block_reasons) if block_reasons else email_result.get("error")
        ),
    }
    _append(ROUTED_FILE, "events", record)
    _append(DELIVERY_LOG_FILE, "events", {
        "lead_id": lead.get("lead_id"),
        "source": lead.get("source"),
        "talkbot_session_id": lead.get("talkbot_session_id"),
        "mover_id": mover.get("mover_id"),
        "to": to_used,
        "to_original": to_original,
        "redirected": (to_used != to_original),
        "from": sender,
        "subject": subject,
        "provider": "resend",
        "status": email_result.get("status"),
        "delivery_mode": delivery_mode,
        "message_id": email_result.get("id"),
        "provider_error": email_result.get("error"),
        "block_reasons": block_reasons or None,
        "at": routed_at,
        "dry_run": actual_dry_run,
        "requested_live": requested_live,
    })
    return record


def check_resend_domains(timeout=10):
    """Query Resend's /domains for a status snapshot of verified senders.

    Returns dict: {"ok": bool, "domains": [...], "error": str|None}.
    Never raises -- failures land in the error field. Used by the admin
    panel and verify_resend_env.py to confirm MARKO_FROM_EMAIL's domain
    is actually verified before a live smoke is attempted.
    """
    import json as _json
    import urllib.request as _ur
    import urllib.error as _ue

    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not key:
        return {"ok": False, "domains": [], "error": "RESEND_API_KEY not set"}
    req = _ur.Request("https://api.resend.com/domains", method="GET")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "MARKO/1.0 (+marko-engine)")
    try:
        with _ur.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        body = _json.loads(raw) if raw else {}
        # Resend returns {"data": [{name, status, region, ...}, ...]}
        domains = body.get("data") if isinstance(body, dict) else body
        domains = domains if isinstance(domains, list) else []
        return {"ok": True, "domains": domains, "error": None}
    except _ue.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        return {"ok": False, "domains": [],
                "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "domains": [],
                "error": f"{type(exc).__name__}: {exc}"}


def from_email_domain_verified(from_email=None):
    """Is the configured MARKO_FROM_EMAIL's domain verified at Resend?

    Returns dict: {ok, domain, status, message}. status is one of
    "verified", "pending", "failed", "not_found", "unknown".
    """
    addr = (from_email or _from_email_default() or "").strip()
    if "@" not in addr:
        return {"ok": False, "domain": None, "status": "unknown",
                "message": f"MARKO_FROM_EMAIL is not a valid address: {addr!r}"}
    domain = addr.split("@", 1)[1].strip().lower()
    snap = check_resend_domains()
    if not snap["ok"]:
        return {"ok": False, "domain": domain, "status": "unknown",
                "message": snap["error"]}
    for d in snap["domains"]:
        if (d.get("name") or "").lower() == domain:
            status = (d.get("status") or "unknown").lower()
            return {"ok": status == "verified", "domain": domain,
                    "status": status,
                    "message": f"Resend reports domain status: {status}"}
    return {"ok": False, "domain": domain, "status": "not_found",
            "message": (
                f"domain {domain!r} is not registered on this Resend account"
            )}


def submit_quote(form, dry_run=True, from_email=None,
                 source="inbound_quote", talkbot_session_id=None):
    """End-to-end inbound flow: build, validate, record, route.

    Returns dict: {ok, lead, errors, routing}. On validation failure the
    lead is not recorded and routing is None. Caller decides whether to
    surface validation errors or just acknowledge.
    """
    lead = build_lead(form, source=source,
                      talkbot_session_id=talkbot_session_id)
    ok, errors = validate_lead(lead)
    if not ok:
        return {"ok": False, "lead": lead, "errors": errors,
                "routing": None, "owner_notify": None}
    record_inbound(lead)
    routing_record = route_lead(lead, dry_run=dry_run, from_email=from_email)
    # Owner notify is independent of mover routing. Even when the mover
    # path is dry_run (overnight default), the owner still receives the
    # lead so Jay can follow up by hand. notify_owner is a no-op when
    # MARKO_OWNER_NOTIFY_TO is unset.
    would_have = (routing_record or {}).get("mover")
    owner = notify_owner(lead, would_have_routed_to=would_have,
                         from_email=from_email)
    return {"ok": True, "lead": lead, "errors": [],
            "routing": routing_record, "owner_notify": owner}


def smoke_send(mover_id="M001", from_email=None):
    """One-shot live-fire smoke. Builds a synthetic-but-clearly-labeled
    smoke lead, forces live mode, and routes it through the full pipeline.

    Honors MARKO_MOVER_ALLOWLIST and MARKO_SMOKE_REDIRECT_TO. If the mover
    is not allowlisted or the redirect/key isn't configured, returns
    status="delivery_blocked" with explicit reasons -- never silently
    falls back to dry_run.

    Caller must pass mover_id explicitly so an automation can't fan out
    to every mover by accident.
    """
    movers = load_movers()
    mover = next((m for m in movers if m.get("mover_id") == mover_id), None)
    if mover is None:
        return {"ok": False, "error": f"mover_id {mover_id!r} not in registry",
                "lead": None, "routing": None}

    pickup = (mover.get("zip_codes") or ["00000"])[0]
    lead = build_lead({
        "customer_name": "MARKO Smoke Test",
        "phone": "555-000-0000",
        "email": _redirect_to() or "smoke@marko.local",
        "move_date": _now_iso()[:10],
        "pickup_zip": pickup,
        "dropoff_zip": pickup,
        "home_size": "Studio",
        "stairs_elevator": "Ground floor",
        "heavy_items": "(smoke test - ignore)",
        "urgency": "flexible",
        "notes": (
            "This is a delivery-pipeline smoke test from MARKO. "
            "If you received this in error, please disregard."
        ),
    }, source="smoke")
    ok, errors = validate_lead(lead)
    if not ok:
        return {"ok": False, "error": "validation: " + "; ".join(errors),
                "lead": lead, "routing": None}
    record_inbound(lead)
    record = route_lead(lead, dry_run=False, from_email=from_email,
                        force_live=True)
    return {"ok": record["status"] == "routed",
            "error": record.get("error"),
            "lead": lead, "routing": record}
