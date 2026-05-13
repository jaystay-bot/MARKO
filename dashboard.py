#!/usr/bin/env python3
"""MARKO Dashboard - Flask UI."""
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify
import json
import os
import commands
import scraper

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGNS_FILE = os.path.join(BASE_DIR, "campaigns.json")
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
LOG_FILE = os.path.join(BASE_DIR, "marko_log.json")


def load_json(filepath):
    with open(filepath, "r") as f:
        return json.load(f)


@app.route("/")
def index():
    campaigns = load_json(CAMPAIGNS_FILE).get("campaigns", [])
    leads = load_json(LEADS_FILE).get("leads", [])
    log = load_json(LOG_FILE).get("log", [])
    templates_data = commands.get_templates()
    stats = commands.get_stats()
    breakdown = commands.campaign_breakdown()

    # Distinct values for filters
    niches = sorted({l.get("niche", "") for l in leads if l.get("niche")})
    cities = sorted({l.get("city", "") for l in leads if l.get("city")})

    # Eligible-for-retry preview count (no mutation)
    retry_eligible = sum(
        1 for l in leads
        if l.get("status") == "RETRY"
        and int(l.get("retry_count", 0)) < commands.MAX_RETRIES
    )

    # Score every lead in-place so the leads table can show HOT/GOOD/WEAK
    commands.annotate_leads(leads)
    call_first = commands.call_queue(limit=10)

    # N050: touch count per lead (any send/called/retry_status event in log)
    touch_counts = {}
    for e in log:
        lid = e.get("lead_id")
        if lid:
            touch_counts[lid] = touch_counts.get(lid, 0) + 1

    # N048: session resume context — active campaign + top HOT lead with phone
    active_campaign = next((c for c in campaigns if c.get("status") == "ACTIVE"), None)
    top_hot = next((l for l in call_first if l.get("_label") == "HOT"), None) \
              or (call_first[0] if call_first else None)
    resume_state = bool(active_campaign or top_hot)

    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))

    message = request.args.get("message", "")
    return render_template(
        "index.html",
        campaigns=campaigns,
        leads=leads,
        log=log,
        templates=templates_data,
        stats=stats,
        breakdown=breakdown,
        niches=niches,
        cities=cities,
        retry_eligible=retry_eligible,
        call_first=call_first,
        touch_counts=touch_counts,
        active_campaign=active_campaign,
        top_hot=top_hot,
        resume_state=resume_state,
        max_retries=commands.MAX_RETRIES,
        daily_cap=commands.DAILY_SEND_CAP,
        cooldown_minutes=commands.RETRY_COOLDOWN_MINUTES,
        is_vercel=is_vercel,
        message=message,
    )


@app.route("/run", methods=["POST"])
def run():
    name = request.form["name"]
    project = request.form["project"]
    commands.marko_run(name, project)
    return redirect(url_for("index", message=f"Campaign created: {name}"))


@app.route("/add_lead", methods=["POST"])
def add_lead():
    name = request.form["name"]
    email = request.form["email"]
    niche = request.form["niche"]
    commands.add_lead(name, email, niche)
    return redirect(url_for("index", message=f"Lead added: {name}"))


@app.route("/send", methods=["POST"])
def send():
    dry = request.form.get("dry_run") == "1"
    result = commands.marko_send(dry_run=dry) or "Batch sent"
    if dry:
        result = result + " (dry run)"
    return redirect(url_for("index", message=result))


@app.route("/log", methods=["POST"])
def log():
    count = int(request.form["count"])
    opens = int(request.form.get("opens", 0))
    replies = int(request.form.get("replies", 0))
    signups = int(request.form.get("signups", 0))
    commands.marko_log(count, opens, replies, signups)
    return redirect(url_for("index", message=f"Logged: {count} sends"))


@app.route("/analyze", methods=["POST"])
def analyze():
    commands.marko_analyze()
    return redirect(url_for("index", message="Analysis complete"))


@app.route("/scrape", methods=["POST"])
def scrape_route():
    niche = request.form["niche"].strip()
    city = request.form["city"].strip()
    state = request.form["state"].strip()
    try:
        max_results = int(request.form.get("max_results", 20))
    except ValueError:
        max_results = 20
    added = scraper.scrape(niche, city, state, max_results=max_results)
    return redirect(url_for("index", message=f"Scrape complete: {added} leads added"))


@app.route("/lead/<lead_id>/contact", methods=["POST"])
def lead_contact(lead_id):
    ok = commands.set_lead_status(lead_id, "CONTACTED")
    msg = f"Lead {lead_id} marked CONTACTED" if ok else f"Lead {lead_id} not found"
    return redirect(url_for("index", message=msg))


@app.route("/lead/<lead_id>/archive", methods=["POST"])
def lead_archive(lead_id):
    ok = commands.set_lead_status(lead_id, "ARCHIVED")
    msg = f"Lead {lead_id} archived" if ok else f"Lead {lead_id} not found"
    return redirect(url_for("index", message=msg))


@app.route("/lead/<lead_id>/reset", methods=["POST"])
def lead_reset(lead_id):
    ok = commands.set_lead_status(lead_id, "NEW")
    msg = f"Lead {lead_id} reset to NEW" if ok else f"Lead {lead_id} not found"
    return redirect(url_for("index", message=msg))


@app.route("/lead/<lead_id>/called", methods=["POST"])
def lead_called(lead_id):
    ok = commands.mark_called(lead_id)
    msg = f"Lead {lead_id} marked CALLED" if ok else f"Lead {lead_id} not found"
    return redirect(url_for("index", message=msg))


@app.route("/campaign/preset/<preset_id>", methods=["POST"])
def campaign_preset(preset_id):
    """N049: one-click campaign from templates.json preset."""
    templates_data = commands.get_templates()
    p = next((cp for cp in templates_data.get("campaign_presets", []) if cp.get("id") == preset_id), None)
    if not p:
        return redirect(url_for("index", message=f"Preset {preset_id} not found"))
    commands.marko_run(p["name"], p["project"])
    msg = f"Created campaign {p['name']} (niche: {p.get('niche','-')}, area: {p.get('city','-')})"
    return redirect(url_for("index", message=msg))


@app.route("/campaign/<campaign_id>/archive", methods=["POST"])
def campaign_archive(campaign_id):
    ok = commands.archive_campaign(campaign_id)
    msg = f"Campaign {campaign_id} archived" if ok else f"Campaign {campaign_id} not found"
    return redirect(url_for("index", message=msg))


@app.route("/export/leads.csv")
def export_leads():
    campaign_id = request.args.get("campaign_id") or None
    status = request.args.get("status") or None
    csv_data = commands.export_leads_csv(campaign_id=campaign_id, status=status)
    fname = "leads"
    if campaign_id:
        fname = f"leads_{campaign_id}"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}.csv"},
    )


@app.route("/export/campaigns.csv")
def export_campaigns():
    csv_data = commands.export_campaigns_csv()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=campaigns.csv"},
    )


@app.route("/template/add", methods=["POST"])
def template_add():
    name = request.form["name"].strip()
    subject = request.form["subject"]
    body = request.form["body"]
    new_id = commands.save_outreach_template(name, subject, body)
    return redirect(url_for("index", message=f"Template saved: {new_id}"))


@app.route("/template/preview/<template_id>")
def template_preview(template_id):
    """Render a template with merge fields applied against a lead.

    Query: lead_id (optional). Falls back to a sample lead if not provided
    or if the lead doesn't exist. Returns JSON {subject, body}.
    """
    templates_data = commands.get_templates()
    tpl = next((t for t in templates_data.get("outreach", []) if t.get("id") == template_id), None)
    if not tpl:
        return jsonify({"error": "template not found"}), 404

    lead_id = request.args.get("lead_id")
    lead = None
    if lead_id:
        leads = load_json(LEADS_FILE).get("leads", [])
        lead = next((l for l in leads if l.get("id") == lead_id), None)
    if lead is None:
        lead = {"name": "Sample Business", "city": "Richmond", "state": "VA",
                "owner": "there", "phone": "555-123-4567", "niche": "movers"}

    config = json.load(open(commands.CONFIG_FILE)) if os.path.exists(commands.CONFIG_FILE) else {}
    sender = config.get("sender_name", "Jay")

    subject = commands.personalize_template(tpl.get("subject", ""), lead, sender)
    body = commands.personalize_template(tpl.get("body", ""), lead, sender)
    return jsonify({"subject": subject, "body": body,
                    "template_name": tpl.get("name"), "lead_id": lead.get("id")})


@app.route("/retry/run", methods=["POST"])
def retry_run():
    """Reset eligible RETRY leads to NEW. Respects daily cap + cooldown + retry cap."""
    try:
        cooldown = int(request.form.get("cooldown_minutes", commands.RETRY_COOLDOWN_MINUTES))
    except ValueError:
        cooldown = commands.RETRY_COOLDOWN_MINUTES
    count = commands.retry_pending(cooldown_minutes=cooldown)
    msg = (f"Reset {count} RETRY lead(s) to NEW (cooldown {cooldown}m)"
           if count else "No RETRY leads eligible right now (cooldown / cap / retry-limit)")
    return redirect(url_for("index", message=msg))


if __name__ == "__main__":
    print("MARKO Dashboard: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000)
