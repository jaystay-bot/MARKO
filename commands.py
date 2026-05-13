"""MARKO command logic."""
import csv
import io
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGNS_FILE = os.path.join(BASE_DIR, "campaigns.json")
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "marko_log.json")
TEMPLATES_FILE = os.path.join(BASE_DIR, "templates.json")

DAILY_SEND_CAP = 50
BATCH_HARD_CAP = 10
TRANSIENT_SMTP_CODES = {421, 450, 451, 452}
MAX_RETRIES = 3
RETRY_COOLDOWN_MINUTES = 60

# N275C: per-domain throttle. No more than N successful sends to the same
# recipient domain in any rolling 60-minute window. Belt-and-suspenders on top
# of the daily cap so a single domain can't be hammered even within one batch.
PER_DOMAIN_HOURLY_CAP = 3

# N275C: send-window gate. Manual and scheduled sends both refuse leads whose
# local wall-clock falls outside business hours. Endpoint is exclusive — a
# lead whose local hour is 18 (6pm) skips.
BUSINESS_HOURS_START_HOUR = 8
BUSINESS_HOURS_END_HOUR = 18

# Tiny built-in state->UTC-offset map (standard time, hours). DST adds 1
# for every state EXCEPT AZ and HI. We avoid zoneinfo+tzdata because Windows
# doesn't ship the IANA db by default and we don't want a new dep just for
# an 8am-6pm gate. DST handling here is approximate — accurate everywhere
# except a 1-hour band twice a year around the spring/fall switch, which is
# acceptable for this throttle.
STATE_UTC_OFFSET_STD = {
    # Eastern (-5 standard, -4 daylight)
    "ME": -5, "NH": -5, "VT": -5, "MA": -5, "RI": -5, "CT": -5,
    "NY": -5, "NJ": -5, "PA": -5, "DE": -5, "MD": -5, "DC": -5,
    "VA": -5, "WV": -5, "NC": -5, "SC": -5, "GA": -5, "FL": -5,
    "OH": -5, "MI": -5, "IN": -5, "KY": -5,
    # Central (-6 standard, -5 daylight)
    "AL": -6, "TN": -6, "MS": -6, "LA": -6, "AR": -6, "MO": -6,
    "IL": -6, "WI": -6, "MN": -6, "IA": -6, "OK": -6, "TX": -6,
    "KS": -6, "NE": -6, "SD": -6, "ND": -6,
    # Mountain (-7 standard, -6 daylight; AZ stays on -7 year-round)
    "MT": -7, "WY": -7, "CO": -7, "NM": -7, "UT": -7, "ID": -7,
    "AZ": -7,
    # Pacific (-8 standard, -7 daylight)
    "NV": -8, "CA": -8, "OR": -8, "WA": -8,
    # Alaska (-9 standard, -8 daylight)
    "AK": -9,
    # Hawaii (-10 standard year-round; no DST)
    "HI": -10,
}
STATES_NO_DST = {"AZ", "HI"}
_DEFAULT_STATE_OFFSET_STD = -5  # ET fallback for missing/unknown state
LEAD_STATUSES = [
    "NEW", "CONTACTED", "RETRY", "FAILED", "REPLIED", "ARCHIVED", "CALLED",
    # N183/N194 dispositions
    "INTERESTED", "NOT_INTERESTED", "CALLBACK", "BOOKED", "DNC", "SKIPPED",
    # N128 close-outcome dispositions (cashflow tracker)
    "VOICEMAIL", "CLOSED_WON", "CLOSED_LOST",
]

# Quality scoring (N031 → N182: 5-tier)
SCORE_MONEY_THRESHOLD = 90    # N182: 🔥 MONEY tier — near-perfect signal set
SCORE_HOT_THRESHOLD = 70
SCORE_GOOD_THRESHOLD = 40
SCORE_LOW_THRESHOLD = 20      # below this == DEAD

# Statuses that should NOT appear in the call queue.
# DNC / DO_NOT_CONTACT / UNSUBSCRIBED / STOP / OPTED_OUT = legal/compliance hard
#   stop (aligned with marko_compliance.NO_CONTACT_STATUSES).
# BOOKED / NOT_INTERESTED = closed dispositions, don't re-call.
# SKIPPED + CALLED = already worked.
CALL_QUEUE_EXCLUDE = {
    "FAILED", "ARCHIVED", "CALLED", "BOOKED", "NOT_INTERESTED", "SKIPPED",
    "DNC", "DO_NOT_CONTACT", "UNSUBSCRIBED", "STOP", "OPTED_OUT",
    "CLOSED_WON", "CLOSED_LOST",
}

# Follow-up overdue threshold (hours since last_attempt_at on CONTACTED leads)
FOLLOWUP_OVERDUE_HOURS = 48


def load_json(filepath):
    """N271: delegates to storage.read_json so the same call site serves both
    the local (file-on-disk) and kv (Upstash Redis) backends. Behavior on
    missing path is unchanged -- still raises FileNotFoundError so existing
    call sites that don't guard with os.path.exists() get the same exception.
    """
    return storage.read_json(filepath)


def save_json(filepath, data):
    """N271: delegates to storage.write_json. Local backend keeps the same
    atomic tmp+rename semantics as before. KV backend issues a single SET.
    """
    return storage.write_json(filepath, data)


def get_config():
    return load_json(CONFIG_FILE)


# Fields that the in-dashboard compliance editor (N122) is allowed to write.
# Anything outside this set (smtp, email_template, batch_size, etc.) is left
# untouched so a malicious POST can't clobber sensitive sections.
CONFIG_EDITABLE_TOP = {
    "sender_name", "from_email", "unsubscribe_text",
    "physical_address", "stop_contact_list",
}
CONFIG_EDITABLE_DELIVERABILITY = {"spf_ok", "dkim_ok", "dmarc_ok"}


def save_config(updates):
    """Update whitelisted compliance fields in config.json.

    Refuses to touch anything outside CONFIG_EDITABLE_TOP /
    CONFIG_EDITABLE_DELIVERABILITY so SMTP credentials, batch size, and the
    canonical email template stay safe. This is an intentional config mutation:
    it saves config.json and appends a config_update audit entry. Returns the
    merged config.
    """
    if not isinstance(updates, dict):
        raise TypeError("save_config: updates must be a dict")

    cfg = get_config() if os.path.exists(CONFIG_FILE) else {}
    changed = []

    for key, value in updates.items():
        if key in CONFIG_EDITABLE_TOP:
            cfg[key] = value
            changed.append(key)
        elif key == "deliverability" and isinstance(value, dict):
            current = cfg.get("deliverability") or {}
            for sub_key, sub_value in value.items():
                if sub_key in CONFIG_EDITABLE_DELIVERABILITY:
                    current[sub_key] = bool(sub_value)
                    changed.append(f"deliverability.{sub_key}")
            cfg["deliverability"] = current

    save_json(CONFIG_FILE, cfg)
    log_action({
        "action": "config_update",
        "scope": "compliance",
        "fields": sorted(set(changed)),
    })
    return cfg


def get_smtp_credentials():
    """N275A: legacy name kept so old call sites compile.

    Returns (from_address, sentinel_or_None). The 'password' slot is now a
    truthy sentinel ('resend') when RESEND_API_KEY is set so existing
    `if not smtp_email or not smtp_password` gates keep working unchanged.
    """
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not api_key:
        # Try config-level from_email so the gate fails with a clearer message.
        return None, None
    cfg = {}
    try:
        cfg = get_config()
    except Exception:
        cfg = {}
    from_email = (cfg.get("from_email") or
                  os.environ.get("MARKO_SMTP_EMAIL") or "")
    if not from_email:
        return None, None
    return from_email, "resend"


def get_active_campaign():
    data = load_json(CAMPAIGNS_FILE)
    for c in data["campaigns"]:
        if c["status"] == "ACTIVE":
            return c
    return None


def find_lead(lead_id):
    """Return the lead dict with this id, or None. Single source of truth.

    Many dashboard routes share the 'load leads.json, find one by id, or 404'
    pattern. Centralizing means one place to optimize if we ever move off
    file-on-disk storage. Returns None on falsy lead_id so callers can pass
    request args without guarding first.
    """
    if not lead_id:
        return None
    data = load_json(LEADS_FILE)
    for l in data.get("leads", []):
        if l.get("id") == lead_id:
            return l
    return None


def send_email(smtp_config, from_email, password, to_email, subject, body,
               reply_to=None, dry_run=False, headers=None):
    """N275A/N275B: Resend-backed shim.

    Returns ``(success, error, smtp_code, message_id)``. The 4th element
    is new in N275B — it carries the Resend message id when status=='sent'
    so callers can persist it to marko_log for downstream webhook joining.
    On any non-success path it's None. Existing call sites that unpack to
    a 3-tuple were updated alongside this change; new ones MUST unpack 4.
    """
    if not to_email:
        return False, "missing to-address", None, None
    if not from_email:
        return False, "missing from-address (config.from_email)", None, None
    import email_client
    res = email_client.send(
        to=to_email, subject=subject, body=body,
        from_=from_email, reply_to=reply_to, dry_run=dry_run,
        headers=headers,
    )
    if res["status"] in ("sent", "dry_run"):
        return True, None, None, res.get("id")
    return False, res.get("error") or res["status"], None, None


# N276: production-polished footer. Replaces the old "-- MARKO compliance
# footer --" sentinel — which read like an automated tag in Gmail — with a
# human-looking signoff block. Idempotency anchors on the unsubscribe text
# string itself, so we never double-stamp even if a downstream caller routes
# the same body through twice. Config-supplied unsubscribe_text and
# physical_address are still the source of truth: marko_compliance.config_blockers
# gates real sends on both being non-empty, and both render into the visible
# footer below (so an empty config keeps the old behavior of skipping the
# footer entirely rather than producing a half-rendered block).


def _apply_compliance_footer(body, config):
    """Append a clean BookerMove-style sign-off + unsubscribe block.

    Layout (one blank line of breathing room before, no trailing wrapper):

        <body>

        —
        <physical_address>

        <unsubscribe_text>

    Idempotent: if the unsubscribe_text (or the older N275B/A marker) is
    already in the body, returns the body unchanged.
    """
    if not isinstance(body, str) or not body:
        return body
    cfg = config or {}
    unsubscribe = (cfg.get("unsubscribe_text") or "").strip()
    address     = (cfg.get("physical_address") or "").strip()
    if not unsubscribe and not address:
        return body
    # Idempotency: never double-stamp. Check both the new anchor and any
    # legacy "-- MARKO compliance footer --" line that might already be in
    # a stored pending_send body from before N276.
    if unsubscribe and unsubscribe in body:
        return body
    if "-- MARKO compliance footer --" in body:
        return body

    parts = ["—"]
    if address:
        # Multi-line addresses (the production one is) come through verbatim
        # so each line shows on its own row in Gmail.
        parts.append(address)
    if unsubscribe:
        parts.append("")          # blank line separates address from unsub
        parts.append(unsubscribe)

    return body.rstrip() + "\n\n" + "\n".join(parts) + "\n"


MERGE_FIELDS = ("business_name", "city", "state", "owner", "phone",
                "email", "niche", "sender_name")


def personalize_template(template, lead, sender_name):
    """Personalize a template. Supports both {field} and {{field}} syntax."""
    values = {
        "business_name": lead.get("name") or "your business",
        "city": lead.get("city") or "your area",
        "state": lead.get("state") or "",
        "owner": lead.get("owner") or "there",
        "phone": lead.get("phone") or "",
        "email": lead.get("email") or "",
        "niche": lead.get("niche") or "",
        "sender_name": sender_name or "",
    }
    text = template
    for key in MERGE_FIELDS:
        v = values[key]
        text = text.replace("{{" + key + "}}", v)
        text = text.replace("{" + key + "}", v)
    return text


def marko_run(name, project):
    """Create a new campaign."""
    data = load_json(CAMPAIGNS_FILE)

    new_id = f"C{len(data['campaigns']) + 1:03d}"

    campaign = {
        "id": new_id,
        "name": name,
        "project": project,
        "status": "ACTIVE",
        "sends": 0,
        "open_rate": 0,
        "replies": 0,
        "signups": 0,
        "verdict": "PENDING",
        "last_action": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "next": "SEND"
    }

    data["campaigns"].append(campaign)
    save_json(CAMPAIGNS_FILE, data)
    print(f"Campaign created: {new_id} - {name} [{project}]")


def add_lead(name, email, niche):
    """Add a new lead, linked to active campaign if any."""
    data = load_json(LEADS_FILE)

    new_id = f"L{len(data['leads']) + 1:03d}"
    active = get_active_campaign()

    lead = {
        "id": new_id,
        "name": name,
        "email": email,
        "niche": niche,
        "status": "NEW",
        "source": "manual",
        "campaign_id": active["id"] if active else None,
        "created_at": datetime.now().isoformat(),
    }

    data["leads"].append(lead)
    save_json(LEADS_FILE, data)
    print(f"Lead added: {new_id} - {name}")


def _count_sends_today():
    """Count successful sends recorded today in marko_log.json."""
    log = load_json(LOG_FILE).get("log", [])
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(
        1 for e in log
        if e.get("action") == "send"
        and e.get("status") == "sent"
        and (e.get("timestamp") or "").startswith(today)
    )


# ---------- N275C: per-domain throttle + send-window gate ----------

def _domain_of(email):
    """Return the lowercase domain part of an email or '' if unparseable."""
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].strip().lower()


def _count_sends_by_domain_last_hour(domain, now=None):
    """How many successful sends to recipients at this domain in the last 60m.

    Pure read over marko_log.json. now=None falls through to datetime.now().
    A bad/empty timestamp on an old log entry is ignored (treated as outside
    the window) rather than crashing the whole gate.
    """
    if not domain:
        return 0
    log = load_json(LOG_FILE).get("log", [])
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=60)
    domain = domain.lower()
    count = 0
    for e in log:
        if e.get("action") != "send" or e.get("status") != "sent":
            continue
        if _domain_of(e.get("recipient") or "") != domain:
            continue
        ts = e.get("timestamp") or ""
        try:
            t = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if t >= cutoff:
            count += 1
    return count


def _is_us_dst(dt_naive):
    """Approximate US DST window: 2am local 2nd Sun March -> 2am local 1st Sun Nov.

    Accepts a naive datetime treated as the instant to test. Off by at most
    one hour around the switch — fine for an 8am-6pm gate.
    """
    y = dt_naive.year
    march_first = datetime(y, 3, 1)
    # Monday=0 ... Sunday=6
    march_dst_start = datetime(y, 3,
                               1 + ((6 - march_first.weekday()) % 7) + 7,
                               2, 0)
    nov_first = datetime(y, 11, 1)
    nov_dst_end = datetime(y, 11,
                           1 + ((6 - nov_first.weekday()) % 7),
                           2, 0)
    return march_dst_start <= dt_naive < nov_dst_end


def _lead_in_business_hours(lead, now=None):
    """True iff the lead's local wall-clock falls inside [8:00, 18:00).

    now=None reads datetime.now() (system local). We convert to UTC, then
    apply the lead-state's standard-time offset (with a DST bump everywhere
    except AZ/HI). Stdlib-only; no zoneinfo/tzdata required.
    """
    from datetime import timezone as _tz
    state = ((lead or {}).get("state") or "").strip().upper()
    offset_h = STATE_UTC_OFFSET_STD.get(state, _DEFAULT_STATE_OFFSET_STD)

    base = now or datetime.now()
    if base.tzinfo is None:
        # Treat naive as system-local — attach system offset, then convert to UTC.
        base = base.astimezone()
    utc = base.astimezone(_tz.utc)

    if state not in STATES_NO_DST and _is_us_dst(utc.replace(tzinfo=None)):
        offset_h += 1

    lead_local = utc.astimezone(_tz(timedelta(hours=offset_h)))
    return BUSINESS_HOURS_START_HOUR <= lead_local.hour < BUSINESS_HOURS_END_HOUR


def marko_send(dry_run=False, batch_size_cap=None):
    """Send real emails to batch of leads. Returns a result string.

    N275C: ``batch_size_cap`` (optional int) lets a scheduled batch ask for a
    smaller batch than the config default. Always clamped to BATCH_HARD_CAP.
    """
    config = get_config()
    batch_size = min(config.get("batch_size", BATCH_HARD_CAP), BATCH_HARD_CAP)
    if batch_size_cap is not None:
        try:
            batch_size = max(0, min(int(batch_size_cap), batch_size))
        except (TypeError, ValueError):
            pass

    # Get SMTP credentials (not required for dry run)
    smtp_email, smtp_password = get_smtp_credentials()
    if not dry_run and (not smtp_email or not smtp_password):
        msg = ("ERROR: Email send is not configured. "
               "Set RESEND_API_KEY and config.from_email, or use --dry-run.")
        print(msg)
        return msg

    campaign = get_active_campaign()
    if not campaign:
        print("No active campaign.")
        return "No active campaign."

    # Enforce daily cap (real sends only)
    if not dry_run:
        today_count = _count_sends_today()
        if today_count >= DAILY_SEND_CAP:
            msg = f"BLOCKED: daily cap {DAILY_SEND_CAP} reached ({today_count} sent today). Try again tomorrow."
            print(msg)
            return msg
        remaining_today = DAILY_SEND_CAP - today_count
        if remaining_today < batch_size:
            print(f"Throttling batch to {remaining_today} (daily cap {DAILY_SEND_CAP}, {today_count} sent today).")
            batch_size = remaining_today

    leads_data = load_json(LEADS_FILE)
    available = [l for l in leads_data["leads"] if l.get("status") == "NEW" and l.get("email")]
    batch = available[:batch_size]

    if not batch:
        print("No new leads with email available.")
        return "No new leads with email available."

    # Get email template
    template_config = config.get("email_template", {})
    subject_template = template_config.get("subject", "Quick question")
    body_template = template_config.get("body", "Hi, I wanted to reach out.")
    sender_name = config.get("sender_name", "MARKO")
    smtp_config = config.get("smtp", {})

    print(f"Campaign: {campaign['name']} [{campaign['id']}]")
    print(f"Sending to {len(batch)} leads...")
    if dry_run:
        print("(DRY RUN - no emails will be sent)")
    print("---")

    log_data = load_json(LOG_FILE)
    sent_count = 0
    failed_count = 0
    retry_count = 0
    skipped_domain_count = 0   # N275C: domain hourly cap hits
    skipped_window_count = 0   # N275C: outside lead-local business hours

    # In-batch throttle: track domains we've sent to in THIS batch so we
    # don't have to re-scan marko_log per-lead. Seed with the last-hour
    # counts already on disk, then increment as we go.
    in_batch_domain_sent = {}

    for lead in batch:
        to_email = lead.get("email")
        subject = personalize_template(subject_template, lead, sender_name)
        body = personalize_template(body_template, lead, sender_name)
        # N275B: append unsubscribe + physical-address footer (idempotent)
        body = _apply_compliance_footer(body, config)

        # N275C: per-domain hourly cap. Skip if at/above PER_DOMAIN_HOURLY_CAP.
        dom = _domain_of(to_email)
        if dom:
            already = in_batch_domain_sent.get(dom)
            if already is None:
                already = _count_sends_by_domain_last_hour(dom)
                in_batch_domain_sent[dom] = already
            if already >= PER_DOMAIN_HOURLY_CAP:
                skipped_domain_count += 1
                print(f"  [SKIP domain-cap] {lead['name']} <{to_email}> "
                      f"({already}/{PER_DOMAIN_HOURLY_CAP} in last 60m)")
                if not dry_run:
                    log_data["log"].append({
                        "timestamp": datetime.now().isoformat(),
                        "action": "send_skipped",
                        "reason": "domain_cap",
                        "campaign_id": campaign["id"],
                        "lead_id": lead["id"],
                        "recipient": to_email,
                        "domain": dom,
                    })
                continue

        # N275C: send-window gate (8am-6pm in the lead's local TZ).
        if not _lead_in_business_hours(lead):
            skipped_window_count += 1
            print(f"  [SKIP outside-hours] {lead['name']} <{to_email}> "
                  f"(state={lead.get('state') or '?'})")
            if not dry_run:
                log_data["log"].append({
                    "timestamp": datetime.now().isoformat(),
                    "action": "send_skipped",
                    "reason": "outside_business_hours",
                    "campaign_id": campaign["id"],
                    "lead_id": lead["id"],
                    "recipient": to_email,
                    "state": lead.get("state") or "",
                })
            continue

        if dry_run:
            print(f"  [DRY] {lead['name']} <{to_email}>")
            continue

        # N275B: stamp X-Marko-Lead-Id so the inbound webhook can resolve
        # this lead even when Resend strips custom tags.
        success, error, smtp_code, message_id = send_email(
            smtp_config, smtp_email, smtp_password, to_email, subject, body,
            headers={"X-Marko-Lead-Id": lead.get("id") or ""},
        )

        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": "send",
            "campaign_id": campaign["id"],
            "lead_id": lead["id"],
            "recipient": to_email,
        }

        if success:
            lead["status"] = "CONTACTED"
            sent_count += 1
            # N275C: bump in-batch domain counter so subsequent leads at the
            # same domain see the updated total without re-reading marko_log.
            if dom:
                in_batch_domain_sent[dom] = in_batch_domain_sent.get(dom, 0) + 1
            print(f"  [OK] {lead['name']} <{to_email}>")
            entry["status"] = "sent"
            if message_id:
                entry["message_id"] = message_id
        elif smtp_code in TRANSIENT_SMTP_CODES:
            rc = int(lead.get("retry_count", 0)) + 1
            lead["retry_count"] = rc
            lead["last_attempt_at"] = datetime.now().isoformat()
            if rc >= MAX_RETRIES:
                lead["status"] = "FAILED"
                failed_count += 1
                print(f"  [FAIL retry-cap] {lead['name']} <{to_email}> - {error}")
                entry.update({"status": "failed", "smtp_code": smtp_code,
                              "error": error, "retry_count": rc,
                              "reason": "retry_cap"})
            else:
                lead["status"] = "RETRY"
                retry_count += 1
                print(f"  [RETRY {smtp_code} #{rc}] {lead['name']} <{to_email}> - {error}")
                entry.update({"status": "retry", "smtp_code": smtp_code,
                              "error": error, "retry_count": rc})
        else:
            lead["status"] = "FAILED"
            lead["last_attempt_at"] = datetime.now().isoformat()
            failed_count += 1
            print(f"  [FAIL] {lead['name']} <{to_email}> - {error}")
            entry.update({"status": "failed", "smtp_code": smtp_code, "error": error})

        log_data["log"].append(entry)

    if dry_run:
        summary = (f"Dry run: {len(batch)} leads checked | Sent: 0 | "
                   f"Retry: 0 | Failed: 0 | Skipped: "
                   f"{skipped_domain_count} domain cap, "
                   f"{skipped_window_count} outside business hours")
        print("---")
        print(summary)
        return summary

    # Save leads + log
    save_json(LEADS_FILE, leads_data)
    save_json(LOG_FILE, log_data)

    # Update campaign sends
    camp_data = load_json(CAMPAIGNS_FILE)
    for c in camp_data["campaigns"]:
        if c["id"] == campaign["id"]:
            c["sends"] += sent_count
            c["last_action"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            c["next"] = "ANALYZE"
            break
    save_json(CAMPAIGNS_FILE, camp_data)

    summary = (f"Sent: {sent_count} | Retry: {retry_count} | "
               f"Failed: {failed_count} | Skipped: "
               f"{skipped_domain_count} domain cap, "
               f"{skipped_window_count} outside business hours")
    print("---")
    print(summary)
    return summary


def marko_log(count, opens=0, replies=0, signups=0):
    """Track sends for active campaign."""
    campaign = get_active_campaign()
    if not campaign:
        print("No active campaign.")
        return

    data = load_json(CAMPAIGNS_FILE)
    for c in data["campaigns"]:
        if c["id"] == campaign["id"]:
            c["sends"] += count
            c["replies"] += replies
            c["signups"] += signups
            if c["sends"] > 0:
                c["open_rate"] = round((opens / c["sends"]) * 100, 1)
            c["last_action"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            c["next"] = "ANALYZE"
            break
    save_json(CAMPAIGNS_FILE, data)

    log_data = load_json(LOG_FILE)
    log_data["log"].append({
        "timestamp": datetime.now().isoformat(),
        "campaign_id": campaign["id"],
        "sends": count,
        "opens": opens,
        "replies": replies,
        "signups": signups
    })
    save_json(LOG_FILE, log_data)

    print(f"Logged: {count} sends, {opens} opens, {replies} replies, {signups} signups")


def marko_analyze():
    """Assign verdict to active campaign."""
    campaign = get_active_campaign()
    if not campaign:
        print("No active campaign.")
        return

    data = load_json(CAMPAIGNS_FILE)
    for c in data["campaigns"]:
        if c["id"] == campaign["id"]:
            sends = c["sends"]
            replies = c["replies"]
            signups = c["signups"]

            # Verdict logic
            if sends == 0:
                verdict = "PENDING"
                next_action = "SEND"
            elif signups > 0:
                verdict = "SCALE"
                next_action = "EXPAND"
            elif replies > 0:
                verdict = "HOLD"
                next_action = "OPTIMIZE"
            elif sends >= 50 and replies == 0:
                verdict = "PIVOT"
                next_action = "REFRAME"
            elif sends >= 100 and signups == 0:
                verdict = "KILL"
                next_action = "STOP"
                c["status"] = "KILLED"
            else:
                verdict = "HOLD"
                next_action = "SEND"

            c["verdict"] = verdict
            c["next"] = next_action
            c["last_action"] = datetime.now().strftime("%Y-%m-%d %H:%M")

            print(f"Campaign: {c['id']} - {c['name']}")
            print(f"Sends: {sends} | Replies: {replies} | Signups: {signups}")
            print(f"Verdict: {verdict}")
            print(f"Next: {next_action}")
            break

    save_json(CAMPAIGNS_FILE, data)


def marko_report():
    """Display all campaigns."""
    data = load_json(CAMPAIGNS_FILE)
    campaigns = data.get("campaigns", [])

    if not campaigns:
        print("No campaigns.")
        return

    print("=== MARKO REPORT ===\n")
    for c in campaigns:
        print(f"[{c['id']}] {c['name']}")
        print(f"  Project: {c['project']}")
        print(f"  Status: {c['status']}")
        print(f"  Sends: {c['sends']} | Open Rate: {c['open_rate']}%")
        print(f"  Replies: {c['replies']} | Signups: {c['signups']}")
        print(f"  Verdict: {c['verdict']}")
        print(f"  Next: {c['next']}")
        print(f"  Last: {c['last_action']}")
        print()


# ---------- Dashboard helpers ----------

def log_action(entry):
    """Append a structured entry to marko_log.json."""
    log_data = load_json(LOG_FILE)
    entry = {"timestamp": datetime.now().isoformat(), **entry}
    log_data["log"].append(entry)
    save_json(LOG_FILE, log_data)


def get_templates():
    if not os.path.exists(TEMPLATES_FILE):
        return {"outreach": [], "campaign_presets": [], "niche_presets": []}
    return load_json(TEMPLATES_FILE)


def save_outreach_template(name, subject, body):
    data = get_templates()
    new_id = f"T{len(data.get('outreach', [])) + 1:03d}"
    data.setdefault("outreach", []).append({
        "id": new_id, "name": name, "subject": subject, "body": body,
    })
    save_json(TEMPLATES_FILE, data)
    return new_id


def set_lead_status(lead_id, status):
    """Update a lead's status (CONTACTED, ARCHIVED, NEW)."""
    data = load_json(LEADS_FILE)
    for l in data["leads"]:
        if l["id"] == lead_id:
            l["status"] = status
            save_json(LEADS_FILE, data)
            log_action({"action": "lead_status", "lead_id": lead_id, "status": status})
            return True
    return False


def archive_campaign(campaign_id):
    data = load_json(CAMPAIGNS_FILE)
    for c in data["campaigns"]:
        if c["id"] == campaign_id:
            c["status"] = "ARCHIVED"
            c["last_action"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            save_json(CAMPAIGNS_FILE, data)
            log_action({"action": "campaign_archive", "campaign_id": campaign_id})
            return True
    return False


def _lead_csv_rows(leads):
    fields = [
        "id", "name", "owner", "email", "phone", "city", "state", "website",
        "niche", "contact_type", "status", "retry_count", "campaign_id",
        "source", "created_at", "last_attempt_at", "source_url", "notes",
    ]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for l in leads:
        w.writerow({f: l.get(f, "") for f in fields})
    return out.getvalue()


def export_leads_csv(campaign_id=None, status=None):
    """Return CSV string of leads, optionally filtered.

    Pure read: exporting must not mutate JSON or append audit log entries.
    """
    leads = load_json(LEADS_FILE).get("leads", [])
    if campaign_id:
        leads = [l for l in leads if l.get("campaign_id") == campaign_id]
    if status:
        leads = [l for l in leads if l.get("status") == status]
    return _lead_csv_rows(leads)


def export_campaigns_csv():
    """Return CSV string of campaigns.

    Pure read: exporting must not mutate JSON or append audit log entries.
    """
    campaigns = load_json(CAMPAIGNS_FILE).get("campaigns", [])
    fields = [
        "id", "name", "project", "status", "sends", "open_rate",
        "replies", "signups", "verdict", "next", "last_action",
    ]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for c in campaigns:
        w.writerow({f: c.get(f, "") for f in fields})
    return out.getvalue()


def _norm_domain(url):
    """Strip scheme, www., path, query → host only, lowercased."""
    if not url:
        return None
    s = str(url).strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0]
    return s or None


def _norm_email(email):
    return email.strip().lower() if email else None


def _norm_email_domain(email):
    e = _norm_email(email)
    if not e or "@" not in e:
        return None
    return e.split("@", 1)[1] or None


def _norm_phone(phone):
    """Digits only; collapse to last 10 if at least 10 digits present."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if not digits:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


def is_duplicate_lead(leads, name=None, email=None, phone=None, website=None, city=None):
    """True if any existing lead matches by phone, email, website domain, or name+city."""
    new_email = _norm_email(email)
    new_web = _norm_domain(website)
    new_phone = _norm_phone(phone)
    nm = (name or "").strip().lower()
    ct = (city or "").strip().lower()

    for l in leads:
        if new_phone and _norm_phone(l.get("phone")) == new_phone:
            return True
        if new_email and _norm_email(l.get("email")) == new_email:
            return True
        if new_web and _norm_domain(l.get("website")) == new_web:
            return True
        if nm and ct \
                and (l.get("name") or "").strip().lower() == nm \
                and (l.get("city") or "").strip().lower() == ct:
            return True
    return False


# ---------- N041: Owner extractor (conservative, no hallucinations) ----------

_CORP_WORDS = re.compile(
    r"\b(?:Inc|LLC|Corp|Corporation|Company|Services|Group|Team|Studio|"
    r"Salon|Shop|Store|Center|Clinic|Pet|Dog|Cat|Grooming|Roofing|"
    r"Movers|Moving|Towing|Detail|Restaurant|"
    # N273: common page-UI phrases that look like person names but aren't.
    # Picked up false-positives like "Background Check" on a movers directory.
    r"Background|Check|Privacy|Policy|Reviews|Rights|Reserved|Sitemap|"
    r"Helpers|Verified|Sponsored|Quote|Booking|Licensed|Bonded|Insured|"
    r"Frequently|Asked|Customer|Sign|Login|Logout|Contact|Cookie|Cookies)\b",
    re.I,
)
_PERSON_NAME = re.compile(r"^[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){1,2}$")


def _looks_like_person_name(s):
    if not s:
        return False
    s = s.strip()
    if len(s) < 4 or len(s) > 60:
        return False
    if _CORP_WORDS.search(s):
        return False
    return bool(_PERSON_NAME.match(s))


def extract_owner_from_html(text):
    """Conservative owner-name extractor. Returns name or None.

    Sources (priority order):
      1. <meta name="author" content="...">
      2. JSON-LD "founder"/"author" name
      3. "Owner: X" / "Founded by X" / "Owned by X" / "Proprietor: X" text patterns
      4. "About <Name>, owner" pattern
    Never returns a name unless it passes _looks_like_person_name (rejects corp words).
    """
    if not text:
        return None

    m = re.search(
        r'<meta\s+name=["\']author["\']\s+content=["\']([^"\']{2,60})["\']',
        text, re.I,
    )
    if m and _looks_like_person_name(m.group(1)):
        return m.group(1).strip()

    m = re.search(
        r'"(?:founder|author)"\s*:\s*\{\s*"name"\s*:\s*"([^"]{2,60})"',
        text, re.I,
    )
    if m and _looks_like_person_name(m.group(1)):
        return m.group(1).strip()

    patterns = [
        r'\b(?:Owner|Founder|Owned by|Founded by|Proprietor)\s*[:\-]?\s*'
        r'([A-Z][a-z\'\-]+(?:\s+[A-Z][a-z\'\-]+){1,2})',
        r'(?:Meet|About)\s+([A-Z][a-z\'\-]+(?:\s+[A-Z][a-z\'\-]+){1,2})\s*[,\-]\s*(?:the\s+)?(?:owner|founder|proprietor)',
        r'([A-Z][a-z\'\-]+(?:\s+[A-Z][a-z\'\-]+){1,2})\s*[,\-]\s*(?:owner|founder|proprietor)\b',
        # N273: about-page voice ("Hi, I'm Jane Doe", "I am John Smith, owner",
        # "My name is Jesse Whitacre"). Still gated by _looks_like_person_name.
        r"\bI(?:'m|\s+am)\s+([A-Z][a-z\'\-]+(?:\s+[A-Z][a-z\'\-]+){1,2})\b",
        r"\bMy name is\s+([A-Z][a-z\'\-]+(?:\s+[A-Z][a-z\'\-]+){1,2})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m and _looks_like_person_name(m.group(1)):
            return m.group(1).strip()

    return None


# ---------- N044/N045: Website health + pain-point tags ----------

def pain_points_from_html(text, url, status=200):
    """Detect operator-relevant weaknesses from a page response.

    Returns a list of short tag strings (max 5). These become cold-call ammo.
    Conservative: only emit tags backed by an observable signal in the HTML.
    """
    tags = []
    if status and status >= 400:
        tags.append(f"site error {status}")
        return tags
    if not text:
        tags.append("empty page")
        return tags

    tl = text.lower()

    if isinstance(url, str) and url.startswith("http://"):
        tags.append("no SSL")

    if "viewport" not in tl:
        tags.append("weak mobile")

    booking_keys = (
        "book online", "online booking", "schedule online", "book now",
        "schedule appointment", "online appointment", "request appointment",
        "request a quote", "instant quote", "get a quote",
    )
    if not any(k in tl for k in booking_keys):
        tags.append("no online booking")

    if "<form" not in tl:
        tags.append("no contact form")

    years = re.findall(r"(?:©|copyright|&copy;|&#169;)\s*\D{0,8}(20\d{2})", text, re.I)
    if years:
        latest = max(int(y) for y in years)
        current = datetime.now().year
        if current - latest >= 2:
            tags.append(f"copyright {latest}")

    if "facebook.com" not in tl and "instagram.com" not in tl and "tiktok.com" not in tl:
        tags.append("no social presence")

    return tags[:5]


def score_lead(lead):
    """Score a lead 0-100 with a label (HOT/GOOD/WEAK) and a signal trace.

    Signals are deterministic, derived from existing lead fields. No external
    API calls, no enrichment. The trace lets the UI explain WHY a lead is hot
    so Jay can verify rather than trust a black box.
    """
    signals = []
    score = 0

    if lead.get("email"):
        score += 20
        signals.append("email")
    if lead.get("phone"):
        score += 20
        signals.append("phone")
    # Synergy: having BOTH is more useful than the sum of parts
    if lead.get("email") and lead.get("phone"):
        score += 10
        signals.append("both_contacts")
    if lead.get("website"):
        score += 15
        signals.append("website")
    if lead.get("owner"):
        score += 15
        signals.append("owner")
    # Subpage extraction inferred when contact_type == 'both' and source == 'scrape'
    if lead.get("contact_type") == "both" and lead.get("source") == "scrape":
        score += 10
        signals.append("contact_page")
    # Local relevance: campaign linked and city present
    if lead.get("campaign_id") and lead.get("city"):
        score += 5
        signals.append("local")
    # Niche match: lead has explicit niche
    if lead.get("niche"):
        score += 5
        signals.append("niche")

    if score >= SCORE_MONEY_THRESHOLD:
        label = "MONEY"
    elif score >= SCORE_HOT_THRESHOLD:
        label = "HOT"
    elif score >= SCORE_GOOD_THRESHOLD:
        label = "GOOD"
    elif score >= SCORE_LOW_THRESHOLD:
        label = "LOW"
    else:
        label = "DEAD"
    return {"score": min(score, 100), "label": label, "signals": signals}


def annotate_leads(leads):
    """Return a list of (lead, score_dict) tuples, score added in-place too.

    N182: also injects `_offer` (recommended offer dict from marko_intel)
    so Money Mode views can render pipeline totals without re-importing.
    Pure read of the lead; no persistence.
    """
    import marko_intel as mi
    out = []
    for l in leads:
        s = score_lead(l)
        l["_score"] = s["score"]
        l["_label"] = s["label"]
        l["_signals"] = s["signals"]
        l["_offer"] = mi.recommend_offer(l)
        out.append((l, s))
    return out


def pipeline_total(leads, statuses=("CONTACTED", "INTERESTED")):
    """N182: sum of _offer.price across leads in the given statuses.

    Operator pipeline value = setup fees already in the closing window.
    Recurring monthly is intentionally NOT summed here — pipeline is one-time.
    """
    import marko_intel as mi
    target = {s.upper() for s in statuses}
    total = 0
    counted = 0
    for l in leads:
        if (l.get("status") or "").upper() not in target:
            continue
        offer = l.get("_offer") or mi.recommend_offer(l)
        price = int(offer.get("price") or 0)
        if price > 0:
            total += price
            counted += 1
    return {"total": total, "count": counted}


def call_queue(limit=20):
    """Top N leads to call first.

    Filter: has phone, status not in CALL_QUEUE_EXCLUDE.
    Sort: score desc, then has-email (true first), then created_at desc.
    Returns the leads list (with _score/_label injected).
    """
    leads = load_json(LEADS_FILE).get("leads", [])
    annotated = []
    for l in leads:
        if not l.get("phone"):
            continue
        if (l.get("status") or "NEW") in CALL_QUEUE_EXCLUDE:
            continue
        s = score_lead(l)
        l["_score"] = s["score"]
        l["_label"] = s["label"]
        l["_signals"] = s["signals"]
        annotated.append(l)
    annotated.sort(
        key=lambda l: (l["_score"], 1 if l.get("email") else 0, l.get("created_at") or ""),
        reverse=True,
    )
    return annotated[:limit]


def mark_called(lead_id):
    """Mark a lead as CALLED. Returns True if found."""
    data = load_json(LEADS_FILE)
    for l in data.get("leads", []):
        if l.get("id") == lead_id:
            l["status"] = "CALLED"
            l["last_attempt_at"] = datetime.now().isoformat()
            save_json(LEADS_FILE, data)
            log_action({"action": "lead_status", "lead_id": lead_id, "status": "CALLED"})
            return True
    return False


def retry_pending(max_n=None, cooldown_minutes=None):
    """Reset eligible RETRY leads back to NEW.

    Eligibility: status == RETRY, retry_count < MAX_RETRIES, and
    last_attempt_at older than cooldown_minutes (default 60).
    Honors the daily send cap — never resets more than (DAILY_SEND_CAP - sent today).
    Returns the count actually reset.
    """
    if cooldown_minutes is None:
        cooldown_minutes = RETRY_COOLDOWN_MINUTES
    cutoff = datetime.now() - timedelta(minutes=cooldown_minutes)

    data = load_json(LEADS_FILE)
    eligible = []
    for l in data.get("leads", []):
        if l.get("status") != "RETRY":
            continue
        if int(l.get("retry_count", 0)) >= MAX_RETRIES:
            continue
        last = l.get("last_attempt_at")
        if last:
            try:
                if datetime.fromisoformat(last) > cutoff:
                    continue  # within cooldown
            except ValueError:
                pass
        eligible.append(l)

    remaining_today = max(0, DAILY_SEND_CAP - _count_sends_today())
    cap = remaining_today if max_n is None else min(remaining_today, max_n)
    eligible = eligible[:cap]

    for l in eligible:
        l["status"] = "NEW"

    save_json(LEADS_FILE, data)
    log_action({"action": "retry_pending", "count": len(eligible),
                "cooldown_minutes": cooldown_minutes, "cap_applied": cap})
    return len(eligible)


def campaign_breakdown():
    """Per-campaign counts: total, NEW, SENT (CONTACTED), RETRY, FAILED, REPLIED, daily_cap_remaining.

    Also returns each campaign's last scrape timestamp from marko_log.json.
    Lead 'SENT' for the UI = lead.status == 'CONTACTED'.
    """
    leads = load_json(LEADS_FILE).get("leads", [])
    log = load_json(LOG_FILE).get("log", [])
    remaining = max(0, DAILY_SEND_CAP - _count_sends_today())

    breakdown = {}
    for l in leads:
        cid = l.get("campaign_id")
        if not cid:
            continue
        b = breakdown.setdefault(cid, {
            "total": 0, "NEW": 0, "SENT": 0, "RETRY": 0,
            "FAILED": 0, "REPLIED": 0, "daily_cap_remaining": remaining,
            "last_scrape": None,
        })
        b["total"] += 1
        s = l.get("status") or "NEW"
        ui_key = "SENT" if s == "CONTACTED" else s
        if ui_key in b:
            b[ui_key] += 1
    # last scrape per campaign (best-effort)
    for e in log:
        if e.get("action") == "scrape":
            cid = e.get("campaign_id")
            if cid and cid in breakdown:
                ts = e.get("timestamp")
                if ts and (not breakdown[cid]["last_scrape"] or ts > breakdown[cid]["last_scrape"]):
                    breakdown[cid]["last_scrape"] = ts
    return breakdown


def pipeline_summary():
    """N181: top-of-dashboard money pipeline counts.

    Pure read; no mutation. Counts derive from current lead state + log.
    Returns a flat dict so the template can render stat tiles directly.
    """
    leads = load_json(LEADS_FILE).get("leads", [])
    log = load_json(LOG_FILE).get("log", [])
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = datetime.now() - timedelta(hours=FOLLOWUP_OVERDUE_HOURS)

    # Score each lead to count by tier
    tier_counts = {"MONEY": 0, "HOT": 0, "GOOD": 0, "LOW": 0, "DEAD": 0}
    for l in leads:
        if (l.get("status") or "NEW") in CALL_QUEUE_EXCLUDE:
            continue
        s = score_lead(l)
        tier_counts[s["label"]] = tier_counts.get(s["label"], 0) + 1

    # Activity counts derived from log
    calls_today = sum(1 for e in log
                      if (e.get("timestamp") or "").startswith(today)
                      and e.get("action") == "lead_status"
                      and e.get("status") == "CALLED")
    emails_today = sum(1 for e in log
                       if (e.get("timestamp") or "").startswith(today)
                       and e.get("action") == "send"
                       and e.get("status") == "sent")
    demos_booked_today = sum(1 for e in log
                             if (e.get("timestamp") or "").startswith(today)
                             and e.get("action") == "lead_status"
                             and e.get("status") == "BOOKED")

    # Follow-ups overdue: CONTACTED leads whose last_attempt_at is older than cutoff
    followups_overdue = 0
    for l in leads:
        if l.get("status") != "CONTACTED":
            continue
        last = l.get("last_attempt_at")
        if not last:
            continue
        try:
            if datetime.fromisoformat(last) < cutoff:
                followups_overdue += 1
        except ValueError:
            continue

    return {
        "money_count": tier_counts.get("MONEY", 0),
        "hot_count": tier_counts.get("HOT", 0),
        "good_count": tier_counts.get("GOOD", 0),
        "low_count": tier_counts.get("LOW", 0),
        "dead_count": tier_counts.get("DEAD", 0),
        "calls_today": calls_today,
        "emails_today": emails_today,
        "demos_booked_today": demos_booked_today,
        "followups_overdue": followups_overdue,
        "followup_window_hours": FOLLOWUP_OVERDUE_HOURS,
    }


def set_lead_disposition(lead_id, disposition):
    """N183/N194: set a disposition status on a lead with safety check.

    Only accepts statuses in LEAD_STATUSES; rejects everything else so callers
    can't write garbage into leads.json via /lead/<id>/disposition/<x>.
    Returns True on success, False on bad status or missing lead.
    """
    if disposition not in LEAD_STATUSES:
        return False
    data = load_json(LEADS_FILE)
    for l in data.get("leads", []):
        if l.get("id") == lead_id:
            l["status"] = disposition
            l["last_attempt_at"] = datetime.now().isoformat()
            save_json(LEADS_FILE, data)
            log_action({"action": "lead_status", "lead_id": lead_id,
                        "status": disposition})
            return True
    return False


def get_stats():
    """Aggregate counts for the dashboard home section."""
    campaigns = load_json(CAMPAIGNS_FILE).get("campaigns", [])
    leads = load_json(LEADS_FILE).get("leads", [])
    log = load_json(LOG_FILE).get("log", [])

    active = [c for c in campaigns if c.get("status") == "ACTIVE"]
    contacted = [l for l in leads if l.get("status") == "CONTACTED"]
    with_email = [l for l in leads if l.get("email")]
    scrapes = [e for e in log if e.get("action") == "scrape"]
    exports = [e for e in log if e.get("action") == "export"]

    return {
        "campaigns_total": len(campaigns),
        "campaigns_active": len(active),
        "leads_total": len(leads),
        "leads_contacted": len(contacted),
        "leads_with_email": len(with_email),
        "scrape_count": len(scrapes),
        "export_count": len(exports),
        "recent_scrapes": scrapes[-5:][::-1],
    }


# ---------- N121: Money Mode aggregator ----------
#
# "What should Jay do RIGHT NOW?" — six action-oriented sections rather than
# stat tiles. Pure read; no mutation. Picks up compliance blockers from
# marko_compliance so the dashboard can refuse unsafe sends.

def money_mode(sender_name="Jay"):
    """Return the six Money Mode sections for the dashboard.

    Output is a dict the template renders directly. Sections:
      call_now           — top 5 phone-callable leads sorted by score
      email_safe         — top 10 emailable leads that pass compliance per-lead
      followup           — up to 10 leads needing follow-up (overdue)
      best_niche         — niche with the highest avg score (>=3 leads)
      pipeline_low/high  — sum of missed-money for HOT+ uncontacted leads
      blockers           — global compliance issues that block sending
    """
    import marko_compliance as mc
    import marko_intel as mi

    leads_data = load_json(LEADS_FILE)
    leads = leads_data.get("leads", [])
    config = get_config() if os.path.exists(CONFIG_FILE) else {}
    stop_list = config.get("stop_contact_list") or []

    # Score leads for ranking; reuse existing tier labels.
    annotated = []
    for l in leads:
        s = score_lead(l)
        l["_score"] = s["score"]
        l["_label"] = s["label"]
        l["_signals"] = s["signals"]
        annotated.append(l)

    # (1) Top 5 to CALL — has phone, not opted-out, not in stop list,
    #     status not in CALL_QUEUE_EXCLUDE.
    callable_now = []
    for l in annotated:
        if not l.get("phone"):
            continue
        if mc.is_no_contact(l) or mc.lead_in_stop_list(l, stop_list):
            continue
        if (l.get("status") or "NEW") in CALL_QUEUE_EXCLUDE:
            continue
        callable_now.append(l)
    callable_now.sort(key=lambda l: (l["_score"], 1 if l.get("email") else 0),
                      reverse=True)
    call_now = callable_now[:5]

    # (2) Top 10 to EMAIL safely — passes per-lead compliance.
    email_safe = []
    for l in annotated:
        if (l.get("status") or "NEW") != "NEW":
            continue
        per_lead = mc.lead_blockers(l, stop_list=stop_list)
        if per_lead:
            continue
        email_safe.append(l)
    email_safe.sort(key=lambda l: l["_score"], reverse=True)
    email_safe = email_safe[:10]

    # (3) Follow-up needed — leads whose last_attempt_at is older than the
    #     follow-up window. N083: removed "EMAILED" — no code path ever
    #     assigns that status, so it was a ghost. CONTACTED is the canonical
    #     post-send status (set by marko_send).
    cutoff = datetime.now() - timedelta(hours=FOLLOWUP_OVERDUE_HOURS)
    followup_statuses = {"CONTACTED", "CALLED", "CALLBACK", "INTERESTED"}
    followup = []
    for l in annotated:
        s = (l.get("status") or "").upper()
        if s not in followup_statuses:
            continue
        last = l.get("last_attempt_at")
        if last:
            try:
                if datetime.fromisoformat(last) > cutoff:
                    continue
            except ValueError:
                pass
        followup.append(l)
    followup.sort(key=lambda l: l.get("last_attempt_at") or "")
    followup = followup[:10]

    # (4) Best niche — highest avg score, min 3 leads.
    niche_totals = {}
    niche_counts = {}
    for l in annotated:
        n = (l.get("niche") or "").strip()
        if not n:
            continue
        niche_totals[n] = niche_totals.get(n, 0) + l["_score"]
        niche_counts[n] = niche_counts.get(n, 0) + 1
    best_niche = None
    best_avg = -1
    for n, total in niche_totals.items():
        if niche_counts[n] < 3:
            continue
        avg = total / niche_counts[n]
        if avg > best_avg:
            best_avg = avg
            best_niche = {"niche": n, "avg_score": round(avg, 1),
                          "count": niche_counts[n]}

    # (5) Pipeline value — sum of missed-money for HOT+ leads not yet contacted.
    pipeline_low = 0
    pipeline_high = 0
    pipeline_count = 0
    for l in annotated:
        if l["_label"] not in ("HOT", "MONEY"):
            continue
        if (l.get("status") or "NEW") != "NEW":
            continue
        mm = mi.estimate_missed_money(l)
        if mm.get("low"):
            pipeline_low += mm["low"]
            pipeline_high += mm["high"]
            pipeline_count += 1

    # (6) Blockers — config-level issues + daily cap.
    blockers = list(mc.config_blockers(config))
    sends_today = _count_sends_today()
    cap_remaining = max(0, DAILY_SEND_CAP - sends_today)
    if sends_today >= DAILY_SEND_CAP:
        blockers.append(f"daily cap reached ({sends_today}/{DAILY_SEND_CAP})")

    return {
        "call_now": call_now,
        "email_safe": email_safe,
        "email_safe_count": len(email_safe),
        "followup": followup,
        "followup_count": len(followup),
        "best_niche": best_niche,
        "pipeline_low": pipeline_low,
        "pipeline_high": pipeline_high,
        "pipeline_count": pipeline_count,
        "blockers": blockers,
        "sends_today": sends_today,
        "cap_remaining": cap_remaining,
        "daily_cap": DAILY_SEND_CAP,
        "deliverability": mc.deliverability_checklist(config),
    }


# ---------- N128: Cashflow tracker ----------
#
# Real outcomes only — counts come from lead status transitions, not
# projections. `mrr_value` and `closed_at` are written by set_lead_closed
# when Jay marks a deal won/lost in the dashboard.

def set_lead_closed(lead_id, won, mrr_value=0, note=None):
    """Mark a lead as CLOSED_WON or CLOSED_LOST.

    Writes `closed_at`, `closed_won` (bool), and `mrr_value` (number, 0 if
    lost or unknown). Returns True on success, False if the lead is missing.
    Records an audit entry in marko_log.
    """
    try:
        mrr = max(0, int(round(float(mrr_value or 0))))
    except (TypeError, ValueError):
        mrr = 0
    won = bool(won)
    new_status = "CLOSED_WON" if won else "CLOSED_LOST"

    data = load_json(LEADS_FILE)
    for l in data.get("leads", []):
        if l.get("id") == lead_id:
            l["status"] = new_status
            l["closed_won"] = won
            l["mrr_value"] = mrr if won else 0
            l["closed_at"] = datetime.now().isoformat()
            if note:
                l["closed_note"] = str(note)[:200]
            save_json(LEADS_FILE, data)
            log_action({
                "action": "lead_close",
                "lead_id": lead_id,
                "status": new_status,
                "mrr_value": l["mrr_value"],
            })
            return True
    return False


def cashflow_summary():
    """Return real-outcome counts for the Money Mode cashflow card.

    Pure read; uses status + mrr_value + closed_at fields. Never invents
    numbers. `mrr_total_won` only sums leads marked CLOSED_WON. The "this
    month" bucket uses closed_at month boundary.
    """
    leads = load_json(LEADS_FILE).get("leads", [])
    now = datetime.now()
    this_month_prefix = now.strftime("%Y-%m")

    demos_booked = 0
    closed_won = 0
    closed_lost = 0
    mrr_total_won = 0
    mrr_this_month = 0
    won_this_month = 0
    recent_wins = []

    for l in leads:
        s = (l.get("status") or "").upper()
        if s == "BOOKED":
            demos_booked += 1
        elif s == "CLOSED_WON":
            closed_won += 1
            mrr = int(l.get("mrr_value") or 0)
            mrr_total_won += mrr
            closed_at = l.get("closed_at") or ""
            if closed_at.startswith(this_month_prefix):
                won_this_month += 1
                mrr_this_month += mrr
            recent_wins.append({
                "id": l.get("id"),
                "name": l.get("name"),
                "mrr": mrr,
                "closed_at": closed_at,
            })
        elif s == "CLOSED_LOST":
            closed_lost += 1

    recent_wins.sort(key=lambda x: x.get("closed_at") or "", reverse=True)

    total_attempts = closed_won + closed_lost
    close_rate = round(100.0 * closed_won / total_attempts, 1) if total_attempts else None

    return {
        "demos_booked": demos_booked,
        "closed_won": closed_won,
        "closed_lost": closed_lost,
        "close_rate_pct": close_rate,
        "mrr_total_won": mrr_total_won,
        "mrr_this_month": mrr_this_month,
        "won_this_month": won_this_month,
        "recent_wins": recent_wins[:5],
    }


# ---------- N124: Sequence engine helpers ----------
#
# Thin commands-layer wrappers around marko_sequence so the dashboard
# never imports it directly for write-paths. State machine + transition
# logic stays pure in marko_sequence; this module owns the disk write.

def apply_sequence_event(lead_id, event):
    """Apply a sequence event to a lead. Returns True if anything changed.

    Loads leads.json, finds the lead, asks marko_sequence what fields to
    update, persists. Logs a `sequence_event` audit entry on success.
    Silently no-ops when the lead is missing or the event doesn't apply.
    """
    import marko_sequence as ms

    data = load_json(LEADS_FILE)
    for l in data.get("leads", []):
        if l.get("id") != lead_id:
            continue
        updates = ms.advance_for_event(l, event)
        if not updates:
            return False
        l.update(updates)
        save_json(LEADS_FILE, data)
        log_action({
            "action": "sequence_event",
            "lead_id": lead_id,
            "event": event,
            "step": l.get("sequence_step"),
            "done": bool(l.get("sequence_done")),
        })
        return True
    return False


def sequence_start_for_sent_leads(now=None):
    """After a real or dry send, mark CONTACTED leads without a sequence
    as step 1 (EMAIL_SENT). Only touches leads that have no `sequence_step`
    yet, so this is idempotent and never reverses a later state.

    Returns the count of leads moved into the sequence.
    """
    import marko_sequence as ms
    data = load_json(LEADS_FILE)
    moved = 0
    for l in data.get("leads", []):
        if l.get("status") != "CONTACTED":
            continue
        if l.get("sequence_step") or l.get("sequence_done"):
            continue
        updates = ms.advance_for_event(l, "email_sent", now=now)
        if not updates:
            continue
        l.update(updates)
        moved += 1
    if moved:
        save_json(LEADS_FILE, data)
        log_action({"action": "sequence_batch_start", "moved": moved})
    return moved


def sequence_due_count():
    """How many leads are sequence-due-now (action overdue)."""
    import marko_sequence as ms
    leads = load_json(LEADS_FILE).get("leads", [])
    return ms.overdue_count(leads)


def sequence_due_now(limit=10):
    """Top N leads with sequence actions due, most overdue first."""
    import marko_sequence as ms
    leads = load_json(LEADS_FILE).get("leads", [])
    return ms.due_now(leads, limit=limit)


# ---------- N127: Auto-stage pending sends (follow-up + final bump) ----------
#
# When a sequence lands at step 4 or 5 with next_at <= now, the lead becomes
# eligible to "stage" — we generate the personalized email body and stash it
# on the lead as `pending_send`. Jay then reviews and clicks Send (or Send
# All), at which point the email actually fires through SMTP and the
# sequence advances. Compliance + daily cap are enforced on every send.

PENDING_KIND_BY_STEP = {
    4: "followup",   # step 4 -> follow-up email
    5: "breakup",    # step 5 -> final bump (matches marko_intel kind)
}
PENDING_EVENT_BY_STEP = {
    4: "followup_sent",
    5: "final_bump_sent",
}


def _is_pending_eligible(lead, now=None):
    """Step 4 or 5, not done, next_at past, has email, no pending_send yet."""
    import marko_sequence as ms
    if now is None:
        now = datetime.now()
    if lead.get("sequence_done"):
        return False
    if not lead.get("email"):
        return False
    if lead.get("pending_send"):
        return False
    step = int(lead.get("sequence_step") or 0)
    if step not in (ms.STEP_FOLLOWUP_DUE, ms.STEP_FINAL_BUMP_DUE):
        return False
    next_at = ms._parse_iso(lead.get("sequence_next_at"))
    if next_at and next_at > now:
        return False
    return True


def stage_pending_send(lead_id, sender_name=None):
    """Generate and stash a pending_send on one lead. Returns True on success."""
    import marko_intel as mi
    data = load_json(LEADS_FILE)
    config = get_config() if os.path.exists(CONFIG_FILE) else {}
    if sender_name is None:
        sender_name = config.get("sender_name", "Jay")
    for l in data.get("leads", []):
        if l.get("id") != lead_id:
            continue
        if not _is_pending_eligible(l):
            return False
        step = int(l.get("sequence_step") or 0)
        kind = PENDING_KIND_BY_STEP.get(step, "followup")
        email = mi.generate_email(l, kind=kind, sender_name=sender_name, config=config)
        l["pending_send"] = {
            "kind":       kind,
            "step":       step,
            "subject":    email.get("subject", ""),
            "body":       email.get("body", ""),
            "queued_at":  datetime.now().isoformat(),
        }
        save_json(LEADS_FILE, data)
        log_action({
            "action":  "pending_send_staged",
            "lead_id": lead_id,
            "kind":    kind,
            "step":    step,
        })
        return True
    return False


def stage_all_pending_sends():
    """Stage every currently-eligible lead. Returns the count newly staged."""
    import marko_intel as mi
    data = load_json(LEADS_FILE)
    config = get_config() if os.path.exists(CONFIG_FILE) else {}
    sender_name = config.get("sender_name", "Jay")
    staged = 0
    for l in data.get("leads", []):
        if not _is_pending_eligible(l):
            continue
        step = int(l.get("sequence_step") or 0)
        kind = PENDING_KIND_BY_STEP.get(step, "followup")
        email = mi.generate_email(l, kind=kind, sender_name=sender_name, config=config)
        l["pending_send"] = {
            "kind":      kind,
            "step":      step,
            "subject":   email.get("subject", ""),
            "body":      email.get("body", ""),
            "queued_at": datetime.now().isoformat(),
        }
        staged += 1
    if staged:
        save_json(LEADS_FILE, data)
        log_action({"action": "pending_send_staged_batch", "count": staged})
    return staged


def pending_send_queue():
    """Return list of leads currently carrying a pending_send, with the
    stashed subject/body and the compliance verdict for the operator.
    Pure read; no writes."""
    import marko_compliance as mc
    leads = load_json(LEADS_FILE).get("leads", [])
    config = get_config() if os.path.exists(CONFIG_FILE) else {}
    stop_list = config.get("stop_contact_list") or []
    out = []
    for l in leads:
        ps = l.get("pending_send")
        if not ps:
            continue
        per_lead_blockers = mc.lead_blockers(l, stop_list=stop_list)
        out.append({
            "id":         l.get("id"),
            "name":       l.get("name"),
            "email":      l.get("email"),
            "city":       l.get("city"),
            "niche":      l.get("niche"),
            "kind":       ps.get("kind"),
            "step":       ps.get("step"),
            "subject":    ps.get("subject"),
            "body":       ps.get("body"),
            "queued_at":  ps.get("queued_at"),
            "blockers":   per_lead_blockers,
        })
    out.sort(key=lambda x: x.get("queued_at") or "")
    return out


def pending_send_count():
    return len(pending_send_queue())


def _send_one_pending_record(lead, config, smtp_email, smtp_password):
    """Actually fire one pending_send. Returns (ok, error, message_id).

    N275B: applies compliance footer + stamps X-Marko-Lead-Id header.
    N275C: per-domain hourly throttle + lead-local business-hours gate run
    *before* dispatch. A throttle/window miss returns ok=False with a
    distinguishable error prefix so callers can split it from a real failure.
    """
    import marko_compliance as mc
    ps = lead.get("pending_send") or {}
    subject = ps.get("subject") or ""
    body = ps.get("body") or ""
    if not subject or not body:
        return False, "pending_send missing subject/body", None
    # Per-lead compliance check (config-level already verified by caller).
    stop_list = (config or {}).get("stop_contact_list") or []
    blockers = mc.lead_blockers(lead, stop_list=stop_list)
    if blockers:
        return False, "; ".join(blockers), None
    # N275C: per-domain hourly cap.
    dom = _domain_of(lead.get("email"))
    if dom and _count_sends_by_domain_last_hour(dom) >= PER_DOMAIN_HOURLY_CAP:
        return False, f"SKIPPED: domain cap ({dom})", None
    # N275C: lead-local business-hours window.
    if not _lead_in_business_hours(lead):
        return False, "SKIPPED: outside business hours", None
    smtp_cfg = (config or {}).get("smtp", {})
    body = _apply_compliance_footer(body, config)
    success, error, _smtp_code, message_id = send_email(
        smtp_cfg, smtp_email, smtp_password,
        lead.get("email"), subject, body,
        headers={"X-Marko-Lead-Id": lead.get("id") or ""},
    )
    return success, error, message_id


def send_pending(lead_id, dry_run=False):
    """Send one pending_send email and advance the sequence.

    Refuses on config compliance gaps unless dry_run=True. Daily cap is
    enforced. On a successful real send, clears pending_send and fires
    the followup_sent or final_bump_sent sequence event.
    """
    import marko_compliance as mc
    import marko_sequence as ms

    config = get_config() if os.path.exists(CONFIG_FILE) else {}
    config_blockers = mc.config_blockers(config)
    if not dry_run and config_blockers:
        return False, "BLOCKED: " + "; ".join(config_blockers)

    smtp_email, smtp_password = (None, None)
    if not dry_run:
        smtp_email, smtp_password = get_smtp_credentials()
        if not smtp_email or not smtp_password:
            return False, ("ERROR: Email send not configured "
                           "(set RESEND_API_KEY and config.from_email).")
        if _count_sends_today() >= DAILY_SEND_CAP:
            return False, f"BLOCKED: daily cap {DAILY_SEND_CAP} reached."

    data = load_json(LEADS_FILE)
    for l in data.get("leads", []):
        if l.get("id") != lead_id:
            continue
        ps = l.get("pending_send")
        if not ps:
            return False, f"Lead {lead_id} has no pending send"
        if dry_run:
            log_action({"action": "send", "lead_id": lead_id,
                        "status": "dry-run", "kind": ps.get("kind")})
            return True, "dry-run OK"
        ok, error, message_id = _send_one_pending_record(
            l, config, smtp_email, smtp_password)
        if not ok:
            log_action({"action": "send", "lead_id": lead_id,
                        "status": "failed", "kind": ps.get("kind"),
                        "error": error})
            return False, error or "send failed"
        # Success: clear pending, advance sequence, persist.
        step = int(ps.get("step") or 0)
        event = PENDING_EVENT_BY_STEP.get(step, "followup_sent")
        l.pop("pending_send", None)
        seq_updates = ms.advance_for_event(l, event)
        l.update(seq_updates)
        l["last_attempt_at"] = datetime.now().isoformat()
        save_json(LEADS_FILE, data)
        log_entry = {"action": "send", "lead_id": lead_id,
                     "status": "sent", "kind": ps.get("kind"),
                     "sequence_event": event}
        if message_id:
            log_entry["message_id"] = message_id
        log_action(log_entry)
        return True, f"Sent {ps.get('kind')} to {lead_id}"
    return False, f"Lead {lead_id} not found"


def send_all_pending(dry_run=False):
    """Send every queued pending_send respecting compliance + daily cap.

    Returns dict {sent, failed, skipped, errors: [...], remaining_cap}.
    """
    import marko_compliance as mc

    config = get_config() if os.path.exists(CONFIG_FILE) else {}
    config_blockers = mc.config_blockers(config)
    if not dry_run and config_blockers:
        return {"sent": 0, "failed": 0, "skipped": 0,
                "errors": ["BLOCKED: " + "; ".join(config_blockers)],
                "remaining_cap": 0}

    smtp_email = smtp_password = None
    if not dry_run:
        smtp_email, smtp_password = get_smtp_credentials()
        if not smtp_email or not smtp_password:
            return {"sent": 0, "failed": 0, "skipped": 0,
                    "errors": ["ERROR: Email send not configured "
                               "(set RESEND_API_KEY and config.from_email)"],
                    "remaining_cap": 0}

    data = load_json(LEADS_FILE)
    queue = [l for l in data.get("leads", []) if l.get("pending_send")]

    sent = failed = skipped = 0
    errors = []
    cap_remaining = max(0, DAILY_SEND_CAP - _count_sends_today())

    for l in queue:
        if not dry_run and cap_remaining <= 0:
            skipped += 1
            errors.append(f"{l.get('id')}: daily cap reached")
            continue
        if dry_run:
            sent += 1
            continue
        ok, error, message_id = _send_one_pending_record(
            l, config, smtp_email, smtp_password)
        if ok:
            import marko_sequence as ms
            ps = l.get("pending_send") or {}
            step = int(ps.get("step") or 0)
            event = PENDING_EVENT_BY_STEP.get(step, "followup_sent")
            l.pop("pending_send", None)
            l.update(ms.advance_for_event(l, event))
            l["last_attempt_at"] = datetime.now().isoformat()
            log_entry = {"action": "send", "lead_id": l.get("id"),
                         "status": "sent", "kind": ps.get("kind"),
                         "sequence_event": event}
            if message_id:
                log_entry["message_id"] = message_id
            log_action(log_entry)
            sent += 1
            cap_remaining -= 1
        elif (error or "").startswith("SKIPPED:"):
            # N275C: distinguish throttle/window skips from real failures so
            # they don't pollute marko_log with "failed" entries or trip the
            # campaign-failure alert path.
            skipped += 1
            errors.append(f"{l.get('id')}: {error}")
            log_action({"action": "send_skipped", "lead_id": l.get("id"),
                        "reason": ("domain_cap"
                                   if "domain cap" in (error or "")
                                   else "outside_business_hours"),
                        "recipient": l.get("email")})
        else:
            failed += 1
            errors.append(f"{l.get('id')}: {error}")
            log_action({"action": "send", "lead_id": l.get("id"),
                        "status": "failed", "error": error})

    if sent or failed:
        save_json(LEADS_FILE, data)
    return {"sent": sent, "failed": failed, "skipped": skipped,
            "errors": errors, "remaining_cap": cap_remaining}


def discard_pending(lead_id):
    """Skip this round — drop the staged pending_send without sending."""
    data = load_json(LEADS_FILE)
    for l in data.get("leads", []):
        if l.get("id") == lead_id and l.get("pending_send"):
            kind = l["pending_send"].get("kind")
            l.pop("pending_send", None)
            save_json(LEADS_FILE, data)
            log_action({"action": "pending_send_discarded",
                        "lead_id": lead_id, "kind": kind})
            return True
    return False


# ---------- N197: Lead timeline + daily recap (pure reads) ----------
#
# Stitches marko_log.json events + lead-record fields into one chronological
# view per lead, and aggregates a daily brief. Read-only — nothing here
# writes to disk.

# Event kind -> short human label for the timeline glyph + summary.
TIMELINE_KIND_LABEL = {
    "scrape":                  "🔍 scrape",
    "send":                    "📧 email",
    "lead_status":             "🏷 disposition",
    "sequence_event":          "↪ sequence",
    "lead_close":              "💰 close",
    "pending_send_staged":     "📋 staged",
    "pending_send_staged_batch": "📋 staged batch",
    "pending_send_discarded":  "🗑 discarded",
    "sequence_batch_start":    "↪ batch start",
    "config_update":           "⚙ config",
    "export":                  "📤 export",
    "retry_pending":           "♻ retry",
    "campaign_archive":        "🗄 archive",
}


def _event_summary(entry, lead=None):
    """One-line human summary of a log entry, lead-aware when possible."""
    action = entry.get("action") or ""
    if action == "send":
        status = entry.get("status") or ""
        if status == "sent":
            kind = entry.get("kind") or ""
            return f"sent{(' ' + kind) if kind else ''} email"
        if status == "failed":
            return f"send failed: {entry.get('error') or 'unknown'}"
        if status == "retry":
            return f"transient SMTP retry (code {entry.get('smtp_code')})"
        if status == "dry-run":
            return f"dry-run {entry.get('kind') or 'send'}"
        return f"send: {status}"
    if action == "lead_status":
        return f"disposition -> {entry.get('status')}"
    if action == "sequence_event":
        ev = entry.get("event") or "?"
        step = entry.get("step")
        done = entry.get("done")
        bits = [f"event={ev}"]
        if step is not None:
            bits.append(f"step={step}")
        if done:
            bits.append("done")
        return " ".join(bits)
    if action == "lead_close":
        st = entry.get("status") or "?"
        mrr = entry.get("mrr_value") or 0
        return f"{st}{f' (${mrr}/mo)' if mrr else ''}"
    if action == "pending_send_staged":
        return f"staged {entry.get('kind') or 'email'} (step {entry.get('step')})"
    if action == "pending_send_discarded":
        return f"discarded staged {entry.get('kind') or 'email'}"
    if action == "scrape":
        n = entry.get("results") or entry.get("count")
        return f"scrape{f' added {n}' if n else ''}"
    if action == "config_update":
        fields = entry.get("fields") or []
        return f"updated config: {', '.join(fields) if fields else 'compliance'}"
    if action == "export":
        return f"export {entry.get('scope') or ''} ({entry.get('count') or 0} rows)"
    return action or "event"


def lead_timeline(lead_id):
    """All events touching one lead, chronologically.

    Combines marko_log.json entries where lead_id matches with implicit
    lead-record milestones (created_at, sequence_started_at, closed_at).
    Returns list of {ts, kind, label, summary, raw} sorted ascending.
    """
    if not lead_id:
        return []
    log = load_json(LOG_FILE).get("log", [])
    leads = load_json(LEADS_FILE).get("leads", [])
    lead = next((l for l in leads if l.get("id") == lead_id), None)

    events = []
    if lead and lead.get("created_at"):
        events.append({
            "ts":      lead["created_at"],
            "kind":    "lead_created",
            "label":   "✨ created",
            "summary": f"lead added (source: {lead.get('source') or 'unknown'})",
            "raw":     {"action": "lead_created", "source": lead.get("source")},
        })
    if lead and lead.get("sequence_started_at"):
        events.append({
            "ts":      lead["sequence_started_at"],
            "kind":    "sequence_start",
            "label":   "▶ seq start",
            "summary": "outbound sequence started",
            "raw":     {"action": "sequence_start"},
        })
    if lead and lead.get("closed_at"):
        won = bool(lead.get("closed_won"))
        mrr = int(lead.get("mrr_value") or 0)
        events.append({
            "ts":      lead["closed_at"],
            "kind":    "lead_close",
            "label":   "💰 close",
            "summary": ("CLOSED_WON" + (f" (${mrr}/mo)" if mrr else "")) if won
                       else "CLOSED_LOST",
            "raw":     {"action": "lead_close", "won": won, "mrr_value": mrr},
        })

    for entry in log:
        if entry.get("lead_id") != lead_id:
            continue
        ts = entry.get("timestamp") or ""
        action = entry.get("action") or "event"
        events.append({
            "ts":      ts,
            "kind":    action,
            "label":   TIMELINE_KIND_LABEL.get(action, "·"),
            "summary": _event_summary(entry, lead=lead),
            "raw":     entry,
        })

    events.sort(key=lambda e: e.get("ts") or "")
    return events


def daily_recap(date_str=None):
    """Aggregate everything MARKO did on one calendar day.

    Default = today. Returns dict the recap.html template renders directly.
    Always reads from current marko_log + leads.json; never caches.
    """
    if date_str:
        target = date_str
    else:
        target = datetime.now().strftime("%Y-%m-%d")

    log = load_json(LOG_FILE).get("log", [])
    leads = load_json(LEADS_FILE).get("leads", [])

    events_today = [e for e in log if (e.get("timestamp") or "").startswith(target)]

    sends_sent = sends_failed = sends_dry = sends_retry = 0
    by_disposition = {}
    sequence_events = {}
    closes_won = closes_lost = 0
    mrr_won_today = 0
    pending_staged = 0
    pending_discarded = 0
    config_updates = 0
    scrapes_run = 0
    scrape_added = 0
    exports_run = 0
    touches_by_lead = {}

    for e in events_today:
        action = e.get("action") or ""
        lid = e.get("lead_id")
        if lid:
            touches_by_lead[lid] = touches_by_lead.get(lid, 0) + 1

        if action == "send":
            st = e.get("status") or ""
            if st == "sent":      sends_sent += 1
            elif st == "failed":  sends_failed += 1
            elif st == "dry-run": sends_dry += 1
            elif st == "retry":   sends_retry += 1
        elif action == "lead_status":
            disp = e.get("status") or "?"
            by_disposition[disp] = by_disposition.get(disp, 0) + 1
        elif action == "sequence_event":
            ev = e.get("event") or "?"
            sequence_events[ev] = sequence_events.get(ev, 0) + 1
        elif action == "lead_close":
            if (e.get("status") or "") == "CLOSED_WON":
                closes_won += 1
                mrr_won_today += int(e.get("mrr_value") or 0)
            elif (e.get("status") or "") == "CLOSED_LOST":
                closes_lost += 1
        elif action == "pending_send_staged":
            pending_staged += 1
        elif action == "pending_send_staged_batch":
            pending_staged += int(e.get("count") or 0)
        elif action == "pending_send_discarded":
            pending_discarded += 1
        elif action == "config_update":
            config_updates += 1
        elif action == "scrape":
            scrapes_run += 1
            scrape_added += int(e.get("results") or e.get("count") or 0)
        elif action == "export":
            exports_run += 1

    new_leads_today = sum(
        1 for l in leads
        if (l.get("created_at") or "").startswith(target)
    )

    # Top movers: leads with the most events today
    top_movers = []
    lead_by_id = {l.get("id"): l for l in leads}
    for lid, n in sorted(touches_by_lead.items(),
                         key=lambda x: x[1], reverse=True)[:5]:
        l = lead_by_id.get(lid) or {}
        top_movers.append({
            "id":     lid,
            "name":   l.get("name") or lid,
            "status": l.get("status") or "",
            "events": n,
        })

    # Compact narrative — one line if everything's zero, otherwise key wins.
    headline_parts = []
    if sends_sent:    headline_parts.append(f"{sends_sent} sent")
    if sends_failed:  headline_parts.append(f"{sends_failed} send fail")
    if closes_won:    headline_parts.append(f"{closes_won} won (${mrr_won_today}/mo)")
    if closes_lost:   headline_parts.append(f"{closes_lost} lost")
    if by_disposition.get("BOOKED"): headline_parts.append(f"{by_disposition['BOOKED']} demo")
    if new_leads_today: headline_parts.append(f"+{new_leads_today} leads")
    if scrape_added:  headline_parts.append(f"{scrape_added} scraped")
    if not headline_parts:
        headline_parts.append("quiet day")
    headline = " · ".join(headline_parts)

    return {
        "date":              target,
        "headline":          headline,
        "sends_sent":        sends_sent,
        "sends_failed":      sends_failed,
        "sends_dry":         sends_dry,
        "sends_retry":       sends_retry,
        "by_disposition":    by_disposition,
        "sequence_events":   sequence_events,
        "closes_won":        closes_won,
        "closes_lost":       closes_lost,
        "mrr_won_today":     mrr_won_today,
        "pending_staged":    pending_staged,
        "pending_discarded": pending_discarded,
        "config_updates":    config_updates,
        "scrapes_run":       scrapes_run,
        "scrape_added":      scrape_added,
        "exports_run":       exports_run,
        "new_leads_today":   new_leads_today,
        "top_movers":        top_movers,
        "event_count":       len(events_today),
        "events":            events_today,
    }


# ---------- N273: owner backfill, name normalization, call-today export ----------

def refresh_owners(timeout=5):
    """Re-fetch homepage + /about + /about-us for any lead with no owner,
    run extract_owner_from_html, and write the result back. Pure additive —
    never overwrites an existing owner, never touches other fields.

    Returns: {"scanned": int, "updated": int, "errors": int}
    """
    import requests as _requests
    from urllib.parse import urlparse
    data = load_json(LEADS_FILE)
    leads = data.get("leads", [])
    scanned = updated = errors = 0
    for l in leads:
        if l.get("owner"):
            continue
        site = (l.get("website") or "").strip()
        if not site:
            continue
        scanned += 1
        candidates = [site]
        try:
            p = urlparse(site)
            if p.scheme and p.netloc:
                base = f"{p.scheme}://{p.netloc}"
                for sp in ("/about", "/about-us"):
                    candidates.append(base + sp)
        except Exception:
            pass
        owner = None
        for url in candidates:
            try:
                resp = _requests.get(url, timeout=timeout,
                                     headers={"User-Agent": "Mozilla/5.0"})
                if getattr(resp, "status_code", 0) == 200:
                    owner = extract_owner_from_html(resp.text)
                    if owner:
                        break
            except Exception:
                errors += 1
                continue
        if owner:
            l["owner"] = owner
            updated += 1
            log_action({"action": "owner_refreshed",
                        "lead_id": l.get("id"), "owner": owner})
    if updated:
        save_json(LEADS_FILE, data)
    return {"scanned": scanned, "updated": updated, "errors": errors}


def normalize_names(rename_existing=False):
    """Run scraper.normalize_business_name across existing leads.

    rename_existing=False (default) reports what WOULD change without
    touching leads.json. rename_existing=True writes the cleaned names back.
    """
    from scraper import normalize_business_name as _norm
    data = load_json(LEADS_FILE)
    leads = data.get("leads", [])
    changed = []
    for l in leads:
        before = l.get("name") or ""
        after = _norm(before)
        if after and after != before:
            changed.append({"id": l.get("id"), "before": before, "after": after})
            if rename_existing:
                l["name"] = after
    if rename_existing and changed:
        save_json(LEADS_FILE, data)
    return {"renamed": len(changed) if rename_existing else 0,
            "candidates": changed}


# Statuses that should NOT appear in the call-today export — same dead-set
# the dashboard's call-queue uses, with two extra (CLOSED_WON / CLOSED_LOST)
# explicitly listed for clarity.
CALL_TODAY_EXCLUDE = {
    "DNC", "DO_NOT_CONTACT", "UNSUBSCRIBED", "STOP", "OPTED_OUT",
    "ARCHIVED", "CLOSED_LOST", "CLOSED_WON",
}


def _call_today_rows():
    """Internal: shared filter + sort used by both the CSV export and the
    Money Lane widget. Returns a list of annotated dicts in priority order.
    Pure read.
    """
    import marko_intel
    import marko_brain
    leads = load_json(LEADS_FILE).get("leads", [])
    rows = []
    for l in leads:
        if not l.get("phone"):
            continue
        if (l.get("status") or "NEW") in CALL_TODAY_EXCLUDE:
            continue
        if l.get("do_not_contact"):
            continue
        s = score_lead(l)
        try:
            closability = marko_brain.closability_score(l)
        except Exception:
            closability = 0.0
        leaks = marko_intel.compute_leaks(l) or {}
        leak_list = (leaks.get("confirmed") or []) + (leaks.get("inferred") or [])
        rows.append({
            "lead": l,
            "score": s["score"],
            "label": s["label"],
            "closability": closability,
            "leak_count": len(leak_list),
            "leak_top1": leak_list[0].get("label", "") if leak_list else "",
            "leak_top2": leak_list[1].get("label", "") if len(leak_list) > 1 else "",
        })
    rows.sort(key=lambda r: (-(r["closability"] or 0.0),
                             -r["leak_count"],
                             -r["score"]))
    return rows


def call_today_top(n=5):
    """N274: top-N entries from the same filter+sort that drives the
    /export/call_today.csv route. Used by the dashboard Money Lane.
    """
    rows = _call_today_rows()
    return rows[: max(0, int(n))]


def export_call_today_csv():
    """CSV ranked for same-day phone calls.

    Columns = leads.csv columns + closability, label, leak_top1, leak_top2.
    Filter: has phone, status not in CALL_TODAY_EXCLUDE, do_not_contact != True.
    Sort:   closability desc → leak count desc → score desc.
    Pure read.
    """
    rows = _call_today_rows()
    fields = [
        "id", "name", "owner", "email", "phone", "city", "state", "website",
        "niche", "contact_type", "status", "retry_count", "campaign_id",
        "source", "created_at", "last_attempt_at", "source_url", "notes",
        "closability", "label", "leak_top1", "leak_top2",
    ]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        merged = {f: r["lead"].get(f, "") for f in fields}
        merged["closability"] = r["closability"]
        merged["label"] = r["label"]
        merged["leak_top1"] = r["leak_top1"]
        merged["leak_top2"] = r["leak_top2"]
        w.writerow(merged)
    return out.getvalue()


# ---------- N274: background owner-refresh (lock + thread, no new deps) ----------

REFRESH_LOCK   = os.path.join(BASE_DIR, ".refresh_owners.lock")
REFRESH_RESULT = os.path.join(BASE_DIR, ".refresh_owners.result.json")
REFRESH_LOCK_TTL = 30  # seconds Jay is told to wait between retries
_REFRESH_LOCK_STALE = 180  # seconds before a stale lock is ignored


def refresh_owners_status():
    """Inspect on-disk lock + last-result. Pure read."""
    import time as _t
    state = {"running": False, "lock_age": None,
             "last_result": None, "last_result_age": None}
    if os.path.exists(REFRESH_LOCK):
        age = int(_t.time() - os.path.getmtime(REFRESH_LOCK))
        if age < _REFRESH_LOCK_STALE:
            state["running"] = True
            state["lock_age"] = age
    if os.path.exists(REFRESH_RESULT):
        try:
            with open(REFRESH_RESULT, "r", encoding="utf-8") as f:
                state["last_result"] = json.load(f)
            state["last_result_age"] = int(_t.time() - os.path.getmtime(REFRESH_RESULT))
        except Exception:
            pass
    return state


def start_refresh_owners():
    """Spawn a daemon thread to run refresh_owners(); return immediately.

    Returns: {"state": "started"} or {"state": "running", "wait_seconds": int}.
    The thread writes the summary to REFRESH_RESULT and removes the lock when done.
    """
    import threading
    st = refresh_owners_status()
    if st["running"]:
        wait = max(0, REFRESH_LOCK_TTL - int(st["lock_age"] or 0))
        return {"state": "running", "wait_seconds": wait}
    try:
        with open(REFRESH_LOCK, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat())
    except Exception as exc:
        return {"state": "error", "error": str(exc)}

    def _job():
        try:
            res = refresh_owners()
            res["finished_at"] = datetime.now().isoformat()
        except Exception as exc:
            res = {"error": str(exc),
                   "finished_at": datetime.now().isoformat()}
        try:
            with open(REFRESH_RESULT, "w", encoding="utf-8") as f:
                json.dump(res, f)
        except Exception:
            pass
        finally:
            try:
                os.remove(REFRESH_LOCK)
            except FileNotFoundError:
                pass

    threading.Thread(target=_job, daemon=True).start()
    return {"state": "started"}


# ---------- N275C: one-shot scheduled send (operator-approved, no recurring) ----------
#
# Same file-lock pattern as refresh_owners. The presence of SCHEDULED_SEND_FILE
# is the lock — a second start_scheduled_send() call while a job is in flight
# bails with state="already scheduled". The background thread sleeps until the
# target time, fires marko_send ONCE, writes a banner, and removes the lock.
# By design there is no rescheduling, no retry, no recurring behavior. One
# schedule equals one execution.

SCHEDULED_SEND_FILE   = os.path.join(BASE_DIR, ".scheduled_send.json")
SCHEDULED_SEND_RESULT = os.path.join(BASE_DIR, ".scheduled_send.result.json")
_SCHEDULED_SEND_STALE = 24 * 3600  # 24h before we'd ever consider the lock stale


def scheduled_send_status():
    """Inspect on-disk scheduled-send job + last banner. Pure read."""
    state = {"in_flight": False, "job": None, "last_result": None}
    if os.path.exists(SCHEDULED_SEND_FILE):
        try:
            with open(SCHEDULED_SEND_FILE, "r", encoding="utf-8") as f:
                state["job"] = json.load(f)
            state["in_flight"] = True
        except Exception:
            pass
    if os.path.exists(SCHEDULED_SEND_RESULT):
        try:
            with open(SCHEDULED_SEND_RESULT, "r", encoding="utf-8") as f:
                state["last_result"] = json.load(f)
        except Exception:
            pass
    return state


def start_scheduled_send(when_iso=None, dry_run=True, batch_size_cap=None):
    """Schedule ONE marko_send to fire at when_iso. Default fire time = now+60s.

    Concurrent calls (while an existing job is in flight) bail with
    state='already scheduled'. The background thread is one-shot — it never
    re-arms itself, never spawns a child schedule, and never auto-repeats.
    """
    import threading
    import time as _t

    if os.path.exists(SCHEDULED_SEND_FILE):
        return {"state": "already scheduled"}

    try:
        if when_iso:
            target = datetime.fromisoformat(when_iso)
        else:
            target = datetime.now() + timedelta(minutes=1)
    except (TypeError, ValueError) as exc:
        return {"state": "error", "error": f"bad when: {exc}"}

    if target < datetime.now() - timedelta(minutes=1):
        return {"state": "error", "error": "scheduled time is in the past"}

    try:
        cap_int = int(batch_size_cap) if batch_size_cap not in (None, "") else None
    except (TypeError, ValueError):
        cap_int = None

    job = {
        "scheduled_at":   datetime.now().isoformat(),
        "fire_at":        target.isoformat(),
        "dry_run":        bool(dry_run),
        "batch_size_cap": cap_int,
    }
    try:
        with open(SCHEDULED_SEND_FILE, "w", encoding="utf-8") as f:
            json.dump(job, f)
    except Exception as exc:
        return {"state": "error", "error": str(exc)}

    # Clear any stale banner from a previous run so the dashboard reflects
    # only the current job's outcome once it fires.
    try:
        os.remove(SCHEDULED_SEND_RESULT)
    except FileNotFoundError:
        pass

    def _job():
        try:
            wait_s = max(0.0, (target - datetime.now()).total_seconds())
            if wait_s > 0:
                _t.sleep(wait_s)
            result = marko_send(dry_run=job["dry_run"],
                                batch_size_cap=job["batch_size_cap"])
            banner = {
                "scheduled_at": job["scheduled_at"],
                "fire_at":      job["fire_at"],
                "fired_at":     datetime.now().isoformat(),
                "dry_run":      job["dry_run"],
                "result":       str(result),
                "ok":           True,
            }
        except Exception as exc:
            banner = {
                "scheduled_at": job["scheduled_at"],
                "fire_at":      job["fire_at"],
                "fired_at":     datetime.now().isoformat(),
                "dry_run":      job["dry_run"],
                "result":       f"ERROR: {exc}",
                "ok":           False,
            }
        try:
            with open(SCHEDULED_SEND_RESULT, "w", encoding="utf-8") as f:
                json.dump(banner, f)
        except Exception:
            pass
        finally:
            try:
                os.remove(SCHEDULED_SEND_FILE)
            except FileNotFoundError:
                pass

    threading.Thread(target=_job, daemon=True).start()
    return {"state": "scheduled", "fire_at": target.isoformat()}


# ---------- N275A: inbound email events + Bot Activity panel ----------

# Webhook event vocabulary we accept. Resend's native names are mapped via
# the route handler so storage doesn't depend on provider naming.
EMAIL_EVENT_TYPES = ("sent", "delivered", "opened", "clicked",
                     "replied", "bounced", "complained", "failed")


def apply_email_event(lead_id, event, meta=None):
    """N275A: write one inbound email event to marko_log + lead.email_status.

    event is one of EMAIL_EVENT_TYPES. meta is an opaque dict (provider id,
    headers, etc.) that gets attached to the log entry verbatim. On 'replied',
    fires the 'replied' sequence event so the drip stops automatically.
    Returns dict {"ok": bool, "reason": str|None, "lead_id": str}.
    """
    ev = (event or "").lower().strip()
    if ev not in EMAIL_EVENT_TYPES:
        return {"ok": False, "reason": f"unknown event '{ev}'",
                "lead_id": lead_id}
    if not lead_id:
        return {"ok": False, "reason": "missing lead_id", "lead_id": None}

    data = load_json(LEADS_FILE)
    found = None
    for l in data.get("leads", []):
        if l.get("id") == lead_id:
            found = l
            break
    if not found:
        # Still log the event — Jay needs to see "ghost" webhook hits.
        log_action({"action": "email_event", "event": ev,
                    "lead_id": lead_id, "status": "lead_not_found",
                    "meta": (meta or {})})
        return {"ok": False, "reason": "lead not found", "lead_id": lead_id}

    # Update lead.email_status only when this event "outranks" the prior one.
    rank = {"sent": 1, "delivered": 2, "opened": 3, "clicked": 4,
            "replied": 6, "bounced": 5, "complained": 5, "failed": 5}
    prev = (found.get("email_status") or "").lower()
    if not prev or rank.get(ev, 0) >= rank.get(prev, 0):
        found["email_status"] = ev
        found["email_status_at"] = datetime.now().isoformat()

    if ev == "replied":
        try:
            import marko_sequence as _ms
            seq_updates = _ms.advance_for_event(found, "replied")
            found.update(seq_updates)
        except Exception:
            pass
        # Also surface this on the dashboard lead status row.
        if (found.get("status") or "") not in ("BOOKED", "CLOSED_WON",
                                               "CLOSED_LOST", "DNC"):
            found["status"] = "REPLIED"

    save_json(LEADS_FILE, data)
    log_action({"action": "email_event", "event": ev,
                "lead_id": lead_id, "status": "applied",
                "meta": (meta or {})})
    return {"ok": True, "reason": None, "lead_id": lead_id}


def email_activity_today(limit_recent=5):
    """Counts of today's email events + most-recent N. Pure read.

    N275C: also surfaces dispatch skips (per-domain throttle, off-hours) so
    the Bot Activity panel makes it obvious *why* a batch produced fewer
    sends than the operator expected.
    """
    today = datetime.now().date().isoformat()
    log = load_json(LOG_FILE).get("log", [])
    counts = {"sent": 0, "delivered": 0, "opened": 0,
              "replied": 0, "bounced": 0, "complained": 0,
              "failed": 0, "clicked": 0}
    skips = {"domain_cap": 0, "outside_business_hours": 0}
    recent = []
    for entry in reversed(log):
        ts = entry.get("timestamp") or ""
        if not ts.startswith(today):
            continue
        if entry.get("action") == "send_skipped":
            reason = entry.get("reason") or ""
            if reason in skips:
                skips[reason] += 1
            continue
        ev_name = None
        if entry.get("action") == "email_event":
            ev_name = (entry.get("event") or "").lower()
        elif entry.get("action") == "send":
            # Original SMTP/Resend dispatch logs land here with status sent.
            st = (entry.get("status") or "").lower()
            ev_name = "sent" if st == "sent" else None
        if not ev_name:
            continue
        counts[ev_name] = counts.get(ev_name, 0) + 1
        if len(recent) < limit_recent:
            recent.append({
                "ts": ts, "event": ev_name,
                "lead_id": entry.get("lead_id") or entry.get("recipient"),
                "status": entry.get("status") or "",
            })
    return {"counts": counts, "skips": skips, "recent": recent,
            "as_of": datetime.now().isoformat()}
