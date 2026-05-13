"""MARKO command logic."""
import csv
import io
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta

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
    with open(filepath, "r") as f:
        return json.load(f)


def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


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
    """Get SMTP credentials from environment variables."""
    email = os.environ.get("MARKO_SMTP_EMAIL")
    password = os.environ.get("MARKO_SMTP_PASSWORD")
    if not email or not password:
        return None, None
    return email, password


def get_active_campaign():
    data = load_json(CAMPAIGNS_FILE)
    for c in data["campaigns"]:
        if c["status"] == "ACTIVE":
            return c
    return None


def send_email(smtp_config, from_email, password, to_email, subject, body):
    """Send a single email via SMTP. Returns (success, error, smtp_code)."""
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    host = smtp_config.get("host", "smtp.gmail.com")
    port = smtp_config.get("port", 587)
    use_tls = smtp_config.get("use_tls", True)

    try:
        server = smtplib.SMTP(host, port)
        if use_tls:
            server.starttls()
        server.login(from_email, password)
        server.sendmail(from_email, to_email, msg.as_string())
        server.quit()
        return True, None, None
    except smtplib.SMTPResponseException as e:
        return False, str(e), e.smtp_code
    except Exception as e:
        return False, str(e), None


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


def marko_send(dry_run=False):
    """Send real emails to batch of leads. Returns a result string."""
    config = get_config()
    batch_size = min(config.get("batch_size", BATCH_HARD_CAP), BATCH_HARD_CAP)

    # Get SMTP credentials (not required for dry run)
    smtp_email, smtp_password = get_smtp_credentials()
    if not dry_run and (not smtp_email or not smtp_password):
        msg = "ERROR: SMTP credentials not configured (set MARKO_SMTP_EMAIL / MARKO_SMTP_PASSWORD, or use --dry-run)."
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

    for lead in batch:
        to_email = lead.get("email")
        subject = personalize_template(subject_template, lead, sender_name)
        body = personalize_template(body_template, lead, sender_name)

        if dry_run:
            print(f"  [DRY] {lead['name']} <{to_email}>")
            lead["status"] = "CONTACTED"
            sent_count += 1
            continue

        success, error, smtp_code = send_email(
            smtp_config, smtp_email, smtp_password, to_email, subject, body
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
            print(f"  [OK] {lead['name']} <{to_email}>")
            entry["status"] = "sent"
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

    summary = f"Sent: {sent_count} | Retry: {retry_count} | Failed: {failed_count}"
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
    r"Movers|Moving|Towing|Detail|Restaurant)\b",
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

    # (3) Follow-up needed — contacted/called/emailed leads whose last_attempt_at
    #     is older than the follow-up window.
    cutoff = datetime.now() - timedelta(hours=FOLLOWUP_OVERDUE_HOURS)
    followup_statuses = {"CONTACTED", "CALLED", "EMAILED", "CALLBACK", "INTERESTED"}
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
