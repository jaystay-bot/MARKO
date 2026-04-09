"""MARKO command logic."""
import json
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGNS_FILE = os.path.join(BASE_DIR, "campaigns.json")
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "marko_log.json")


def load_json(filepath):
    with open(filepath, "r") as f:
        return json.load(f)


def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def get_config():
    return load_json(CONFIG_FILE)


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
    """Send a single email via SMTP."""
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
        return True, None
    except Exception as e:
        return False, str(e)


def personalize_template(template, lead, sender_name):
    """Personalize email template with lead data."""
    business_name = lead.get("name", "your business")
    city = lead.get("city", "your area")

    text = template.replace("{business_name}", business_name)
    text = text.replace("{city}", city)
    text = text.replace("{sender_name}", sender_name)
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
    """Add a new lead."""
    data = load_json(LEADS_FILE)

    new_id = f"L{len(data['leads']) + 1:03d}"

    lead = {
        "id": new_id,
        "name": name,
        "email": email,
        "niche": niche,
        "status": "NEW"
    }

    data["leads"].append(lead)
    save_json(LEADS_FILE, data)
    print(f"Lead added: {new_id} - {name}")


def marko_send(dry_run=False):
    """Send real emails to batch of leads."""
    config = get_config()
    batch_size = min(config.get("batch_size", 10), 10)  # Max 10 for safety

    # Get SMTP credentials (not required for dry run)
    smtp_email, smtp_password = get_smtp_credentials()
    if not dry_run and (not smtp_email or not smtp_password):
        print("ERROR: SMTP credentials not configured.")
        print("Set environment variables:")
        print("  MARKO_SMTP_EMAIL=your@email.com")
        print("  MARKO_SMTP_PASSWORD=your_app_password")
        print("\nOr use --dry-run to test without sending:")
        print("  python main.py send --dry-run")
        return

    campaign = get_active_campaign()
    if not campaign:
        print("No active campaign.")
        return

    leads_data = load_json(LEADS_FILE)
    available = [l for l in leads_data["leads"] if l.get("status") == "NEW" and l.get("email")]
    batch = available[:batch_size]

    if not batch:
        print("No new leads with email available.")
        return

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

    for lead in batch:
        to_email = lead.get("email")
        subject = personalize_template(subject_template, lead, sender_name)
        body = personalize_template(body_template, lead, sender_name)

        if dry_run:
            print(f"  [DRY] {lead['name']} <{to_email}>")
            lead["status"] = "CONTACTED"
            sent_count += 1
        else:
            success, error = send_email(smtp_config, smtp_email, smtp_password, to_email, subject, body)

            if success:
                lead["status"] = "CONTACTED"
                sent_count += 1
                print(f"  [OK] {lead['name']} <{to_email}>")

                # Log success
                log_data["log"].append({
                    "timestamp": datetime.now().isoformat(),
                    "action": "send",
                    "campaign_id": campaign["id"],
                    "lead_id": lead["id"],
                    "recipient": to_email,
                    "status": "sent"
                })
            else:
                lead["status"] = "FAILED"
                failed_count += 1
                print(f"  [FAIL] {lead['name']} <{to_email}> - {error}")

                # Log failure
                log_data["log"].append({
                    "timestamp": datetime.now().isoformat(),
                    "action": "send",
                    "campaign_id": campaign["id"],
                    "lead_id": lead["id"],
                    "recipient": to_email,
                    "status": "failed",
                    "error": error
                })

    # Save leads
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

    print("---")
    print(f"Sent: {sent_count} | Failed: {failed_count}")


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
