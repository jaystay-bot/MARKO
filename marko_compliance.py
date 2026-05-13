"""MARKO outbound compliance + safety guardrails.

Pure functions: take a config/lead/message and return what's wrong with it.
Callers (commands.marko_send, the /send route, the dashboard) decide whether
to refuse the send. Nothing here writes to disk or sends email.

Rules enforced (2026 outbound reality):
- Config must declare sender, from-address, unsubscribe text, physical address.
- Subject + body must be non-empty.
- Recipient must have an email.
- Lead must not be opted out, do_not_contact, or in the stop-contact list.
- Daily send cap must not be exceeded.
- Outbound body must include unsubscribe language (added by us if absent).
"""
from __future__ import annotations

# Lead statuses that block any outbound contact.
NO_CONTACT_STATUSES = {"DO_NOT_CONTACT", "UNSUBSCRIBED", "STOP", "OPTED_OUT"}

# Config fields that must be present (truthy + non-blank) before any real send.
REQUIRED_CONFIG_FIELDS = (
    "sender_name",
    "from_email",
    "unsubscribe_text",
    "physical_address",
)


def is_no_contact(lead):
    """Lead has explicitly opted out or been flagged DO_NOT_CONTACT."""
    if (lead.get("status") or "").upper() in NO_CONTACT_STATUSES:
        return True
    if lead.get("do_not_contact"):
        return True
    if lead.get("opted_out"):
        return True
    return False


def _norm_email(value):
    return (value or "").strip().lower() or None


def _norm_phone(value):
    if not value:
        return None
    digits = "".join(c for c in str(value) if c.isdigit())
    if len(digits) >= 10:
        digits = digits[-10:]
    return digits or None


def lead_in_stop_list(lead, stop_list):
    """Match a lead against a stop-contact list by email or phone (normalized)."""
    if not stop_list:
        return False
    email = _norm_email(lead.get("email"))
    phone = _norm_phone(lead.get("phone"))
    for entry in stop_list:
        if not entry:
            continue
        e = str(entry).strip().lower()
        if email and email == e:
            return True
        if phone and _norm_phone(e) == phone:
            return True
    return False


def config_blockers(config):
    """Return human-readable list of config fields that block any real send."""
    blockers = []
    config = config or {}
    for field in REQUIRED_CONFIG_FIELDS:
        v = config.get(field)
        if not v or (isinstance(v, str) and not v.strip()):
            blockers.append(f"config.{field} missing")
    return blockers


def lead_blockers(lead, stop_list=None):
    """Return list of reasons this lead cannot be safely emailed."""
    blockers = []
    if not lead.get("email"):
        blockers.append("no recipient email")
    if is_no_contact(lead):
        blockers.append("lead marked DO_NOT_CONTACT / opted-out")
    if stop_list and lead_in_stop_list(lead, stop_list):
        blockers.append("lead in stop-contact list")
    return blockers


def message_blockers(subject, body):
    blockers = []
    if not (subject or "").strip():
        blockers.append("empty subject")
    if not (body or "").strip():
        blockers.append("empty body")
    return blockers


def compliance_check(config, lead, subject, body,
                     stop_list=None, sends_today=0, daily_cap=None):
    """Single gate. Returns list of blocker strings; empty list = safe to send."""
    blockers = []
    blockers.extend(config_blockers(config))
    blockers.extend(lead_blockers(lead, stop_list=stop_list))
    blockers.extend(message_blockers(subject, body))
    if daily_cap is not None and sends_today >= daily_cap:
        blockers.append(f"daily cap reached ({sends_today}/{daily_cap})")
    return blockers


def append_compliance_footer(body, unsubscribe_text=None, physical_address=None):
    """Append unsubscribe + physical address to a message body if missing.

    Idempotent: if the body already mentions unsubscribe / 'reply stop',
    we don't double-up. Returns the body unchanged when nothing to add.
    """
    if not body:
        return body

    lower = body.lower()
    has_unsub = any(token in lower for token in (
        "unsubscribe", "reply 'stop'", "reply stop", "opt out", "opt-out",
    ))
    has_addr = bool(physical_address and physical_address.lower() in lower)

    out = body.rstrip()
    if not has_unsub:
        line = unsubscribe_text or (
            "If you'd rather not hear from me, reply 'stop' and I'll take you off the list."
        )
        out += "\n\n—\n" + line
    if physical_address and not has_addr:
        out += "\n" + physical_address.strip()
    return out


def deliverability_checklist(config):
    """Surface-level deliverability hints for the dashboard.

    These are NOT actual DNS lookups — they only check that the operator
    has acknowledged each item via config flags. Caller renders this as
    a checklist; ticked items mean "operator says they've done it."
    """
    config = config or {}
    deliver = config.get("deliverability") or {}
    items = [
        ("SPF record published",    bool(deliver.get("spf_ok"))),
        ("DKIM signing enabled",    bool(deliver.get("dkim_ok"))),
        ("DMARC policy published",  bool(deliver.get("dmarc_ok"))),
        ("Custom domain (not gmail.com)",
            bool(config.get("from_email")) and "@gmail.com" not in (config.get("from_email") or "").lower()),
        ("Unsubscribe text set",    bool(config.get("unsubscribe_text"))),
        ("Physical address set",    bool(config.get("physical_address"))),
    ]
    return [{"label": label, "ok": ok} for label, ok in items]
