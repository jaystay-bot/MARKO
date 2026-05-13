#!/usr/bin/env python3
"""MARKO Dashboard - Flask UI."""
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify
import io
import json
import os
import zipfile
import commands
import marko_brain
import marko_compliance
import marko_intel
import marko_sequence

import scraper

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# N261: Mockup catalog built once at import time from templates/mockup/*.
# Acts as a whitelist so the /mockup route can't be tricked into rendering
# templates outside this directory (no path traversal).
_MOCKUP_DIR = os.path.join(BASE_DIR, "templates", "mockup")


def _build_mockup_catalog():
    """Scan templates/mockup/*.html and return {slug: {variant: filename}}.

    Filenames are <slug>_<variant>.html (e.g. movers_booking.html).
    Files starting with underscore (e.g. _booking_base.html) are skipped.
    The rightmost underscore splits slug from variant so multi-word slugs
    like 'med_spas' or 'auto_shops' resolve correctly.
    """
    catalog = {}
    if not os.path.isdir(_MOCKUP_DIR):
        return catalog
    for fname in os.listdir(_MOCKUP_DIR):
        if not fname.endswith(".html") or fname.startswith("_"):
            continue
        stem = fname[:-5]  # strip .html
        if "_" not in stem:
            continue
        slug, variant = stem.rsplit("_", 1)
        catalog.setdefault(slug, {})[variant] = fname
    return catalog


MOCKUP_CATALOG = _build_mockup_catalog()
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
    # N182: re-annotate call_first leads so each has _offer for the Money Lane buttons
    for _l in call_first:
        if "_offer" not in _l:
            _l["_offer"] = marko_intel.recommend_offer(_l)
    # N182: pipeline total = sum of offer.price for leads in closing window
    pipeline = commands.pipeline_total(leads,
                                       statuses=("CONTACTED", "INTERESTED"))

    # N084 + N089: attach a personalized cold-call script and a missed-money
    # estimate to each Call First card. Pure functions over existing fields.
    try:
        sender_name = commands.get_config().get("sender_name", "Jay")
    except Exception:
        sender_name = "Jay"
    for _l in call_first:
        _l["_script"] = marko_intel.generate_script(_l, sender_name=sender_name)
    for _l in call_first:
        _l["_sequence_state"] = marko_sequence.state_for(_l)
        _l["_script"] = marko_intel.generate_script(_l, sender_name=sender_name)
        _l["_missed_money"] = marko_intel.estimate_missed_money(_l)
        # N261: brain bundle = recommended action + closability + best angle.
        # Pure read; no mutation of the on-disk lead record.
        _brain = marko_brain.recommended_first_action(_l)
        _brain["closability"] = marko_brain.closability_score(_l)
        _brain["best_angle"] = marko_brain.best_angle(_l)
        # Mockup hint: niche -> slug (or None) + best variant for that niche.
        _slug = marko_brain.niche_to_mockup_slug(_l.get("niche"))
        if _slug and _slug in MOCKUP_CATALOG:
            _variant = marko_brain.best_mockup_variant(_l.get("niche"))
            if _variant not in MOCKUP_CATALOG[_slug]:
                # Fall back to whatever variant exists for that slug.
                _variant = next(iter(MOCKUP_CATALOG[_slug].keys()))
            _brain["mockup_slug"] = _slug
            _brain["mockup_variant"] = _variant
        _l["_brain"] = _brain

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
    # N128: real cashflow outcomes (BOOKED, CLOSED_WON, CLOSED_LOST, MRR).
    try:
        cashflow = commands.cashflow_summary()
    except Exception:
        cashflow = {"demos_booked": 0, "closed_won": 0, "closed_lost": 0,
                    "mrr_total_won": 0, "mrr_this_month": 0,
                    "won_this_month": 0, "close_rate_pct": None,
                    "recent_wins": []}
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
        pipeline=pipeline,
        compliance=compliance_state,
        compliance_config=config_for_view,
        cashflow=cashflow,        sequence_due=commands.sequence_due_now(limit=10),
        sequence_due_count=commands.sequence_due_count(),
        pending_queue=commands.pending_send_queue(),
        pending_count=commands.pending_send_count(),

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
    # N124: after a real send, mark each newly-CONTACTED lead as sequence step 1.
    # Idempotent; dry runs leave the sequence alone.
    if not dry:
        try:
            commands.sequence_start_for_sent_leads()
        except Exception:
            pass

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
    # N124: advance the outbound sequence if this disposition maps to an event.
    if ok:
        try:
            import marko_sequence as _ms
            ev = _ms.DISPOSITION_TO_EVENT.get(disp)
            if ev:
                commands.apply_sequence_event(lead_id, ev)
        except Exception:
            pass

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
            # N083: route through commands.save_json so the write is atomic.
            commands.save_json(LEADS_FILE, data)
            commands.log_action({"action": "lead_status", "lead_id": lead_id,
                                 "status": "DNC", "reason": "stop_button"})
            return redirect(url_for("index", message=f"Lead {lead_id} marked DO_NOT_CONTACT"))
    return redirect(url_for("index", message=f"Lead {lead_id} not found"))


@app.route("/lead/<lead_id>/close", methods=["POST"])
def lead_close(lead_id):
    """N128: mark a deal CLOSED_WON or CLOSED_LOST + record MRR.

    Form: outcome=won|lost, mrr_value=number, note=optional<=200 chars.
    """
    outcome = (request.form.get("outcome") or "").lower().strip()
    won = outcome == "won"
    mrr_raw = request.form.get("mrr_value") or "0"
    note = request.form.get("note") or None
    ok = commands.set_lead_closed(lead_id, won=won, mrr_value=mrr_raw, note=note)
    # N124: close events end the sequence regardless of step.
    if ok:
        try:
            commands.apply_sequence_event(lead_id, "booked" if won else "not_interested")
        except Exception:
            pass

    if ok:
        if won:
            try:
                msg = f"Lead {lead_id} -> CLOSED_WON | ${int(round(float(mrr_raw or 0)))}/mo"
            except (TypeError, ValueError):
                msg = f"Lead {lead_id} -> CLOSED_WON"
        else:
            msg = f"Lead {lead_id} -> CLOSED_LOST"
    else:
        msg = f"Lead {lead_id} not found"
    return redirect(url_for("index", message=msg))


@app.route("/mode/call")
def mode_call():
    """N130: single-card mobile/focus mode for working the call queue.

    Renders one lead at a time with big buttons, tap-to-call, copy script,
    disposition row, close-deal mini-form, and Prev/Next nav. Re-uses the
    same call_queue + brain attachments as the main dashboard.
    """
    try:
        idx = max(0, int(request.args.get("i", 0)))
    except (TypeError, ValueError):
        idx = 0

    try:
        sender_name = commands.get_config().get("sender_name", "Jay")
    except Exception:
        sender_name = "Jay"

    queue = commands.call_queue(limit=25)
    if not queue:
        return render_template("mode_call.html", lead=None, idx=0, total=0,
                               sender_name=sender_name)

    if idx >= len(queue):
        idx = len(queue) - 1
    lead = queue[idx]
    lead["_script"] = marko_intel.generate_script(lead, sender_name=sender_name)
    lead["_missed_money"] = marko_intel.estimate_missed_money(lead)
    try:
        brain = marko_brain.recommended_first_action(lead)
        brain["closability"] = marko_brain.closability_score(lead)
        brain["best_angle"] = marko_brain.best_angle(lead)
        lead["_brain"] = brain
    except Exception:
        lead["_brain"] = None

    return render_template(
        "mode_call.html",
        lead=lead,
        idx=idx,
        total=len(queue),
        prev_idx=max(0, idx - 1),
        next_idx=min(len(queue) - 1, idx + 1),
        sender_name=sender_name,
    )




@app.route("/lead/<lead_id>/sequence/<event>", methods=["POST"])
def lead_sequence_event(lead_id, event):
    """N124: explicit sequence transition.

    event in marko_sequence.VALID_EVENTS. Used by the dashboard for
    operator-driven steps that aren't captured by dispositions —
    primarily Email Sent / Followup Sent / Final Bump Sent / Reset.
    """
    ev = (event or "").lower().strip()
    if ev not in marko_sequence.VALID_EVENTS:
        return redirect(url_for("index",
            message=f"Unknown sequence event: {ev}"))
    ok = commands.apply_sequence_event(lead_id, ev)
    if ok:
        msg = f"Lead {lead_id} sequence → {ev}"
    else:
        msg = f"Lead {lead_id} not found or event did not apply"
    return redirect(url_for("index", message=msg))


@app.route("/api/sequence/due")
def api_sequence_due():
    """N124: JSON snapshot of leads with sequence actions due now."""
    due = commands.sequence_due_now(limit=25)
    return jsonify({
        "count": len(due),
        "leads": [
            {
                "id":             d["lead"].get("id"),
                "name":           d["lead"].get("name"),
                "phone":          d["lead"].get("phone"),
                "email":          d["lead"].get("email"),
                "step":           d["state"]["step"],
                "step_name":      d["state"]["step_name"],
                "hint":           d["state"]["hint"],
                "overdue_minutes": d["state"]["overdue_minutes"],
                "next_at":        d["state"]["next_at"],
            }
            for d in due
        ],
    })




@app.route("/sequence/stage_all", methods=["POST"])
def sequence_stage_all():
    """N127: stage every eligible follow-up / final bump email for review."""
    count = commands.stage_all_pending_sends()
    msg = (f"Staged {count} pending send(s) — review the queue and click Send."
           if count else "No leads currently eligible for auto-staging.")
    return redirect(url_for("index", message=msg))


@app.route("/lead/<lead_id>/sequence/stage", methods=["POST"])
def lead_sequence_stage(lead_id):
    """N127: stage one lead's pending email."""
    ok = commands.stage_pending_send(lead_id)
    msg = (f"Lead {lead_id} pending send staged"
           if ok else
           f"Lead {lead_id} not eligible (wrong step / no email / already staged)")
    return redirect(url_for("index", message=msg))


@app.route("/lead/<lead_id>/sequence/send_pending", methods=["POST"])
def lead_sequence_send_pending(lead_id):
    """N127: send one staged email."""
    dry = request.form.get("dry_run") == "1"
    ok, info = commands.send_pending(lead_id, dry_run=dry)
    prefix = "DRY: " if dry and ok else ""
    return redirect(url_for("index", message=prefix + (info or "")))


@app.route("/lead/<lead_id>/sequence/discard", methods=["POST"])
def lead_sequence_discard(lead_id):
    """N127: skip this round — drop the staged email without sending."""
    ok = commands.discard_pending(lead_id)
    msg = (f"Lead {lead_id} pending send discarded"
           if ok else f"Lead {lead_id} had no pending send")
    return redirect(url_for("index", message=msg))


@app.route("/sequence/send_all_pending", methods=["POST"])
def sequence_send_all_pending():
    """N127: send the entire pending review queue (compliance + cap enforced)."""
    dry = request.form.get("dry_run") == "1"
    result = commands.send_all_pending(dry_run=dry)
    parts = [f"sent {result['sent']}"]
    if result["failed"]:
        parts.append(f"failed {result['failed']}")
    if result["skipped"]:
        parts.append(f"skipped {result['skipped']}")
    msg = ("DRY: " if dry else "") + " · ".join(parts)
    if result["errors"]:
        msg += " · " + result["errors"][0]
        if len(result["errors"]) > 1:
            msg += f" (+{len(result['errors']) - 1} more)"
    return redirect(url_for("index", message=msg))


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


@app.route("/config/compliance", methods=["POST"])
def config_compliance():
    """N122: in-UI editor for compliance config fields.

    Operator fills sender_name / from_email / unsubscribe_text /
    physical_address / stop_contact_list / SPF-DKIM-DMARC ack flags
    without leaving the dashboard. commands.save_config enforces a
    whitelist so smtp, email_template, batch_size stay untouched.
    """
    form = request.form
    stop_raw = (form.get("stop_contact_list") or "").strip()
    stop_list = [line.strip() for line in stop_raw.splitlines() if line.strip()]

    updates = {
        "sender_name": (form.get("sender_name") or "").strip(),
        "from_email": (form.get("from_email") or "").strip(),
        "unsubscribe_text": (form.get("unsubscribe_text") or "").strip(),
        "physical_address": (form.get("physical_address") or "").strip(),
        "stop_contact_list": stop_list,
        "deliverability": {
            "spf_ok": form.get("spf_ok") == "1",
            "dkim_ok": form.get("dkim_ok") == "1",
            "dmarc_ok": form.get("dmarc_ok") == "1",
        },
    }
    commands.save_config(updates)

    cfg = commands.get_config()
    blockers = marko_compliance.config_blockers(cfg)
    if blockers:
        msg = ("Compliance config saved. Still missing: "
               + "; ".join(b.replace("config.", "") for b in blockers))
    else:
        msg = "Compliance config saved. All blockers cleared — real sends now allowed."
    return redirect(url_for("index", message=msg))


@app.route("/lead/<lead_id>/intel")
def lead_intel(lead_id):
    """N081: full intel JSON for a single lead.

    Returns the lead's score + signals + pain points + missed-money estimate +
    a 'soft' cold-call script. Pure read; no mutation. Used by the UI for an
    intel panel and by headless tooling that wants the raw numbers.
    """
    lead = commands.find_lead(lead_id)
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
    lead = commands.find_lead(lead_id)
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
    lead = commands.find_lead(lead_id)
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
    lead = commands.find_lead(lead_id)
    if not lead:
        return jsonify({"error": "lead not found", "id": lead_id}), 404
    return jsonify(marko_intel.why_they_buy(lead))


@app.route("/lead/<lead_id>/brain")
def lead_brain(lead_id):
    """N261: market-brain decision bundle for one lead.

    Returns closability + best_angle + recommended_first_action + money range
    + the suggested mockup (slug + variant), all derived from existing fields.
    Pure read; no mutation.
    """
    lead = commands.find_lead(lead_id)
    if not lead:
        return jsonify({"error": "lead not found", "id": lead_id}), 404

    # Ensure the lead has a fresh score so closability stays consistent
    # with the dashboard's call queue view.
    s = commands.score_lead(lead)
    lead["_score"] = s["score"]
    lead["_label"] = s["label"]
    lead["_signals"] = s["signals"]

    rec = marko_brain.recommended_first_action(lead)
    slug = marko_brain.niche_to_mockup_slug(lead.get("niche"))
    mockup = None
    if slug and slug in MOCKUP_CATALOG:
        variant = marko_brain.best_mockup_variant(lead.get("niche"))
        if variant not in MOCKUP_CATALOG[slug]:
            variant = next(iter(MOCKUP_CATALOG[slug].keys()))
        mockup = {
            "slug": slug,
            "variant": variant,
            "url": url_for("show_mockup", slug=slug, variant=variant,
                           lead_id=lead.get("id")),
        }

    return jsonify({
        "id": lead.get("id"),
        "score": s["score"],
        "label": s["label"],
        "closability": marko_brain.closability_score(lead),
        "path": rec["path"],
        "action": rec["action"],
        "by_when": rec["by_when"],
        "reason": rec["reason"],
        "best_angle": marko_brain.best_angle(lead),
        "opportunity": marko_brain.opportunity_size(lead),
        "mockup": mockup,
    })


@app.route("/mockup/<slug>/<variant>")
def show_mockup(slug, variant):
    """N261: render a niche mockup as a 'show prospect what theirs could look like'.

    Only renders mockups in the whitelist built at import (MOCKUP_CATALOG)
    so this route cannot be tricked into loading arbitrary templates.
    Optional ?lead_id= populates the business name / city / state / phone;
    otherwise placeholder values are used so the page still demos cleanly.
    """
    if slug not in MOCKUP_CATALOG or variant not in MOCKUP_CATALOG[slug]:
        return jsonify({
            "error": "mockup not found",
            "slug": slug, "variant": variant,
            "available": {k: sorted(v.keys()) for k, v in MOCKUP_CATALOG.items()},
        }), 404

    lead_id = request.args.get("lead_id") or ""
    lead = None
    if lead_id:
        lead = commands.find_lead(lead_id)

    if lead:
        name = lead.get("name") or "Your Business"
        city = lead.get("city") or ""
        state = lead.get("state") or ""
        phone = lead.get("phone") or "(555) 123-4567"
    else:
        name = "Your Business"
        city = "Your City"
        state = ""
        phone = "(555) 123-4567"

    return render_template(
        f"mockup/{MOCKUP_CATALOG[slug][variant]}",
        name=name, city=city, state=state, phone=phone,
        slug=slug, variant=variant, lead_id=lead_id,
    )


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
        lead = commands.find_lead(lead_id)
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


# ---------- N182: Leak / Mockup / Pitch / Pitch Pack / Mobile Call ----------

def _find_lead_or_404(lead_id):
    """Return a lead dict by id, or abort 404. Pure read."""
    leads = load_json(LEADS_FILE).get("leads", [])
    for l in leads:
        if l.get("id") == lead_id:
            return l
    return None


def _sender_name():
    try:
        return commands.get_config().get("sender_name", "Jay")
    except Exception:
        return "Jay"


def _leak_report_md(lead, leaks, offer):
    """Format the leak report as plain markdown for the Pitch Pack zip."""
    lines = [f"# Leak Report — {lead.get('name', lead.get('id'))}",
             "",
             f"- City: {lead.get('city') or '—'}, {lead.get('state') or ''}",
             f"- Niche: {lead.get('niche') or '—'}",
             f"- Phone: {lead.get('phone') or '—'}",
             f"- Email: {lead.get('email') or '—'}",
             ""]
    for section, rows in (("Confirmed", leaks["confirmed"]),
                          ("Inferred", leaks["inferred"]),
                          ("Needs human check", leaks["needs_check"])):
        if rows:
            lines.append(f"## {section}")
            for r in rows:
                lines.append(f"- **{r['label']}** — basis: {r['basis']}")
            lines.append("")
    lines.append("## Recommended Offer")
    lines.append(f"**{offer['kind']}** — ${offer['price']:,} setup"
                 + (f" + ${offer['monthly']}/mo" if offer.get('monthly') else ""))
    lines.append(f"_Basis:_ {offer['basis']}")
    return "\n".join(lines) + "\n"


@app.route("/lead/<lead_id>/leak")
def lead_leak(lead_id):
    """Lead Leak Panel — confirmed / inferred / needs_check + offer recommendation."""
    lead = _find_lead_or_404(lead_id)
    if not lead:
        return Response(f"lead {lead_id} not found", status=404, mimetype="text/plain")
    leaks = marko_intel.compute_leaks(lead)
    offer = marko_intel.recommend_offer(lead)
    return render_template("leak.html",
                           lead=lead, lead_id=lead_id,
                           leaks=leaks, offer=offer,
                           page_title=f"Leak — {lead.get('name', lead_id)}")


@app.route("/lead/<lead_id>/mockup")
def lead_mockup(lead_id):
    """Mockup Pitch Panel — renders niche+variant template with whitelisted fields ONLY.

    Truth check: the dict passed to the included mockup template contains only
    name/city/state/phone/niche. The full lead is exposed for the outer panel
    chrome (breadcrumb, action buttons) but never to the mockup itself.
    """
    lead = _find_lead_or_404(lead_id)
    if not lead:
        return Response(f"lead {lead_id} not found", status=404, mimetype="text/plain")

    variant_override = request.args.get("variant")
    variant = marko_intel.mockup_variant(lead, override=variant_override)
    slug = marko_intel.niche_key(lead.get("niche"))
    if slug not in marko_intel.MOCKUP_NICHES:
        return render_template("mockup_panel.html",
                               lead=lead, lead_id=lead_id, variant=variant,
                               niche_slug=slug or "unknown",
                               mockup_template="mockup/_pending.html",
                               page_title=f"Mockup — {lead.get('name', lead_id)}",
                               **marko_intel.whitelisted_lead(lead))

    mockup_template = f"mockup/{slug}_{variant}.html"
    # Pass ONLY whitelisted fields by spreading the whitelisted dict into the
    # render context. The outer chrome (`lead`) also still renders but the
    # mockup include uses `{{ name }}`/`{{ city }}` etc. resolved from the
    # whitelist below — never `lead.email` or anything else.
    return render_template("mockup_panel.html",
                           lead=lead, lead_id=lead_id, variant=variant,
                           niche_slug=slug, mockup_template=mockup_template,
                           page_title=f"Mockup — {lead.get('name', lead_id)}",
                           **marko_intel.whitelisted_lead(lead))


@app.route("/lead/<lead_id>/pitch")
def lead_pitch(lead_id):
    """One-Click Email Panel. Auto-flips to call-script when email is null."""
    lead = _find_lead_or_404(lead_id)
    if not lead:
        return Response(f"lead {lead_id} not found", status=404, mimetype="text/plain")

    mode_override = request.args.get("mode")
    if mode_override in ("email", "call"):
        mode = mode_override
        if mode == "email" and not lead.get("email"):
            mode = "call"
    else:
        mode = "email" if lead.get("email") else "call"

    sender = _sender_name()
    config = commands.get_config() if os.path.exists(commands.CONFIG_FILE) else {}

    email = marko_intel.generate_email(lead, kind="intro",
                                       sender_name=sender, config=config)
    script = marko_intel.generate_script(lead, sender_name=sender)
    voicemail = marko_intel.generate_voicemail(lead, sender_name=sender)
    return render_template("pitch.html",
                           lead=lead, lead_id=lead_id, mode=mode,
                           email=email, script=script, voicemail=voicemail,
                           page_title=f"Pitch — {lead.get('name', lead_id)}")


@app.route("/lead/<lead_id>/pitch_pack")
def lead_pitch_pack(lead_id):
    """Downloadable ZIP: email.txt + mockup.html + leak_report.md."""
    lead = _find_lead_or_404(lead_id)
    if not lead:
        return Response(f"lead {lead_id} not found", status=404, mimetype="text/plain")

    sender = _sender_name()
    config = commands.get_config() if os.path.exists(commands.CONFIG_FILE) else {}
    leaks = marko_intel.compute_leaks(lead)
    offer = marko_intel.recommend_offer(lead)
    email = marko_intel.generate_email(lead, kind="intro",
                                       sender_name=sender, config=config)
    script = marko_intel.generate_script(lead, sender_name=sender)

    # Render the mockup HTML in isolation so the pack contains a usable preview.
    variant = marko_intel.mockup_variant(lead)
    slug = marko_intel.niche_key(lead.get("niche"))
    wl = marko_intel.whitelisted_lead(lead)
    mockup_html = ""
    if slug in marko_intel.MOCKUP_NICHES:
        try:
            from flask import render_template_string
            tpl_path = os.path.join(app.root_path, "templates", "mockup",
                                    f"{slug}_{variant}.html")
            with open(tpl_path, "r", encoding="utf-8") as f:
                mockup_html = render_template_string(f.read(), **wl)
        except Exception as exc:
            mockup_html = f"<!-- mockup render failed: {exc} -->"

    email_text = f"To: {lead.get('email') or '(no email on file — use phone)'}\n" \
                 f"Subject: {email['subject']}\n\n{email['body']}\n\n" \
                 f"---\nCall opener (if needed):\n{script}\n"
    report_md = _leak_report_md(lead, leaks, offer)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("email.txt", email_text)
        zf.writestr("mockup.html",
                    "<!doctype html><meta charset=utf-8>"
                    "<title>Mockup</title>" + mockup_html)
        zf.writestr("leak_report.md", report_md)

    buf.seek(0)
    safe_name = "".join(c for c in (lead.get("name") or lead_id)
                        if c.isalnum() or c in "-_")[:40] or lead_id
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename=pitch_pack_{safe_name}.zip'},
    )


@app.route("/m/lead/<lead_id>")
def mobile_lead(lead_id):
    """Mobile Call Mode — top leak, opener, tap-to-dial, 4-button outcome."""
    lead = _find_lead_or_404(lead_id)
    if not lead:
        return Response(f"lead {lead_id} not found", status=404, mimetype="text/plain")
    leaks = marko_intel.compute_leaks(lead)
    top = None
    for bucket in (leaks["confirmed"], leaks["inferred"]):
        if bucket:
            top = bucket[0]
            break
    script = marko_intel.generate_script(lead, sender_name=_sender_name())
    return render_template("mobile_call.html",
                           lead=lead, lead_id=lead_id,
                           top_leak=top, script=script,
                           page_title=f"Call — {lead.get('name', lead_id)}")


if __name__ == "__main__":
    print("MARKO Dashboard: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000)
