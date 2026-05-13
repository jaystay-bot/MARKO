#!/usr/bin/env python3
"""MARKO Dashboard - Flask UI."""
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify
import json
import os
import commands
import marko_compliance
import marko_intel
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

    # N084 + N089: attach a personalized cold-call script and a missed-money
    # estimate to each Call First card. Pure functions over existing fields.
    try:
        sender_name = commands.get_config().get("sender_name", "Jay")
    except Exception:
        sender_name = "Jay"
    for _l in call_first:
        _l["_script"] = marko_intel.generate_script(_l, sender_name=sender_name)
        _l["_missed_money"] = marko_intel.estimate_missed_money(_l)

    # N050: touch count per lead (any send/called/retry_status event in log)
    touch_counts = {}
    for e in log:
        lid = e.get("lead_id")
        if lid:
            touch_counts[lid] = touch_counts.get(lid, 0) + 1

    # N048: session resume context — active campaign + top HOT lead with phone.
    # N182: prefer MONEY tier above HOT now that scoring is 5-tier.
    active_campaign = next((c for c in campaigns if c.get("status") == "ACTIVE"), None)
    top_hot = (next((l for l in call_first if l.get("_label") == "MONEY"), None)
               or next((l for l in call_first if l.get("_label") == "HOT"), None)
               or (call_first[0] if call_first else None))
    resume_state = bool(active_campaign or top_hot)

    # N181: MAKE MONEY TODAY banner counts
    try:
        money_pipeline = commands.pipeline_summary()
    except Exception:
        money_pipeline = None

    # N121: Money Mode — "what should Jay do right now?" aggregator.
    # N122: compliance state for the outbound safety banner.
    try:
        money_mode_data = commands.money_mode(sender_name=sender_name)
    except Exception as exc:
        money_mode_data = {"blockers": [f"money_mode failed: {exc}"], "call_now": [],
                           "email_safe": [], "followup": [], "pipeline_low": 0,
                           "pipeline_high": 0, "best_niche": None, "deliverability": [],
                           "sends_today": 0, "cap_remaining": commands.DAILY_SEND_CAP,
                           "daily_cap": commands.DAILY_SEND_CAP}
    config_for_view = commands.get_config() if os.path.exists(commands.CONFIG_FILE) else {}
    compliance_state = {
        "config_blockers": marko_compliance.config_blockers(config_for_view),
        "deliverability": marko_compliance.deliverability_checklist(config_for_view),
        "safe_to_send": not marko_compliance.config_blockers(config_for_view)
                        and money_mode_data.get("cap_remaining", 0) > 0,
        "stop_list_size": len(config_for_view.get("stop_contact_list") or []),
    }

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
        money_pipeline=money_pipeline,
        money_mode=money_mode_data,
        compliance=compliance_state,
        lead_statuses=commands.LEAD_STATUSES,
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
    # N122: dry_run defaults ON. Operator must explicitly check "real send".
    dry = request.form.get("dry_run", "1") == "1"

    # N122: compliance gate. Real sends are refused when config is missing
    # required compliance fields (sender, unsubscribe text, address, etc).
    if not dry:
        try:
            cfg = commands.get_config()
        except Exception:
            cfg = {}
        config_blockers = marko_compliance.config_blockers(cfg)
        if config_blockers:
            msg = ("BLOCKED — fix Compliance panel before real sends: "
                   + "; ".join(config_blockers))
            return redirect(url_for("index", message=msg))

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


@app.route("/lead/<lead_id>/disposition/<disposition>", methods=["POST"])
def lead_disposition(lead_id, disposition):
    """N127: set a follow-up disposition on a lead.

    Allowed values match commands.LEAD_STATUSES so unknown inputs are rejected.
    Used by Call First card buttons (Interested / Booked / Not Interested / DNC).
    """
    disp = (disposition or "").upper()
    ok = commands.set_lead_disposition(lead_id, disp)
    if ok:
        msg = f"Lead {lead_id} → {disp}"
    else:
        msg = (f"Lead {lead_id} not found, or disposition '{disp}' rejected "
               f"(allowed: {', '.join(commands.LEAD_STATUSES)})")
    return redirect(url_for("index", message=msg))


@app.route("/lead/<lead_id>/stop", methods=["POST"])
def lead_stop(lead_id):
    """N122: one-click do-not-contact on a lead.

    Sets status=DNC and writes do_not_contact=true so every compliance check
    refuses any future send to this lead.
    """
    data = load_json(LEADS_FILE)
    for l in data.get("leads", []):
        if l.get("id") == lead_id:
            l["status"] = "DNC"
            l["do_not_contact"] = True
            l["last_attempt_at"] = __import__("datetime").datetime.now().isoformat()
            with open(LEADS_FILE, "w") as f:
                json.dump(data, f, indent=2)
            commands.log_action({"action": "lead_status", "lead_id": lead_id,
                                 "status": "DNC", "reason": "stop_button"})
            return redirect(url_for("index", message=f"Lead {lead_id} marked DO_NOT_CONTACT"))
    return redirect(url_for("index", message=f"Lead {lead_id} not found"))


@app.route("/api/compliance")
def api_compliance():
    """N122: JSON compliance state — config blockers + deliverability checklist."""
    try:
        cfg = commands.get_config()
    except Exception:
        cfg = {}
    return jsonify({
        "config_blockers": marko_compliance.config_blockers(cfg),
        "deliverability": marko_compliance.deliverability_checklist(cfg),
        "stop_list_size": len(cfg.get("stop_contact_list") or []),
        "no_contact_statuses": sorted(marko_compliance.NO_CONTACT_STATUSES),
    })


@app.route("/lead/<lead_id>/intel")
def lead_intel(lead_id):
    """N081: full intel JSON for a single lead.

    Returns the lead's score + signals + pain points + missed-money estimate +
    a 'soft' cold-call script. Pure read; no mutation. Used by the UI for an
    intel panel and by headless tooling that wants the raw numbers.
    """
    leads = load_json(LEADS_FILE).get("leads", [])
    lead = next((l for l in leads if l.get("id") == lead_id), None)
    if not lead:
        return jsonify({"error": "lead not found", "id": lead_id}), 404

    # Score it once so the intel response is consistent with the UI
    s = commands.score_lead(lead)
    lead["_score"] = s["score"]
    lead["_label"] = s["label"]
    lead["_signals"] = s["signals"]

    sender = "Jay"
    try:
        sender = commands.get_config().get("sender_name", "Jay")
    except Exception:
        pass

    return jsonify({
        "id": lead.get("id"),
        "name": lead.get("name"),
        "owner": lead.get("owner"),
        "phone": lead.get("phone"),
        "email": lead.get("email"),
        "city": lead.get("city"),
        "state": lead.get("state"),
        "niche": lead.get("niche"),
        "status": lead.get("status"),
        "score": s["score"],
        "label": s["label"],
        "signals": s["signals"],
        "pain_points": lead.get("pain_points") or [],
        "missed_money": marko_intel.estimate_missed_money(lead),
        "script": marko_intel.generate_script(lead, sender_name=sender),
    })


@app.route("/lead/<lead_id>/email/<kind>")
def lead_email(lead_id, kind):
    """N084: preview-only email generation. Returns {kind, subject, body}.

    kind ∈ {intro, followup, breakup}. Never sends — UI surfaces this for
    operator copy/edit/paste workflows.
    """
    leads = load_json(LEADS_FILE).get("leads", [])
    lead = next((l for l in leads if l.get("id") == lead_id), None)
    if not lead:
        return jsonify({"error": "lead not found", "id": lead_id}), 404

    sender = "Jay"
    config = {}
    try:
        config = commands.get_config()
        sender = config.get("sender_name", "Jay")
    except Exception:
        pass
    return jsonify(marko_intel.generate_email(
        lead, kind=kind, sender_name=sender, config=config))


@app.route("/lead/<lead_id>/voicemail")
def lead_voicemail(lead_id):
    """N183: short voicemail script for one lead. JSON; never auto-sends."""
    leads = load_json(LEADS_FILE).get("leads", [])
    lead = next((l for l in leads if l.get("id") == lead_id), None)
    if not lead:
        return jsonify({"error": "lead not found", "id": lead_id}), 404
    sender = "Jay"
    try:
        sender = commands.get_config().get("sender_name", "Jay")
    except Exception:
        pass
    return jsonify({
        "id": lead.get("id"),
        "script": marko_intel.generate_voicemail(lead, sender_name=sender),
    })


@app.route("/lead/<lead_id>/why")
def lead_why(lead_id):
    """N191: structured 'why they buy' angle for one lead. JSON."""
    leads = load_json(LEADS_FILE).get("leads", [])
    lead = next((l for l in leads if l.get("id") == lead_id), None)
    if not lead:
        return jsonify({"error": "lead not found", "id": lead_id}), 404
    return jsonify(marko_intel.why_they_buy(lead))


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
