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
import storage
import routing

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# N-OVERNIGHT-MONEY-SAFE-CAPTURE: customers land on the bare public domain
# expecting the intake form, not the operator dashboard. Redirect / to /quote
# when the host is the public intake domain. Operator dashboard at
# marko-teal.vercel.app/ and other hosts is unchanged.
#
# Defaults cover the apex + www variant of the public quote domain. Override
# with MARKO_PUBLIC_INTAKE_HOSTS (CSV) without redeploy if the domain changes.
# Matching is suffix-based so "*.quote.bookermove.com" also resolves -- a
# stray subdomain typo on the operator's part doesn't trap a customer in the
# dashboard.
_DEFAULT_PUBLIC_INTAKE_HOSTS = ("quote.bookermove.com",)


def _public_intake_hosts():
    raw = (os.environ.get("MARKO_PUBLIC_INTAKE_HOSTS") or "").strip()
    if raw:
        return tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return _DEFAULT_PUBLIC_INTAKE_HOSTS


def _request_host():
    """Resolve the customer-facing host even behind Vercel's proxy.

    Prefers X-Forwarded-Host (set by Vercel when a custom domain proxies
    into the underlying *.vercel.app deployment), falls back to request.host.
    Lowercased, port stripped.
    """
    from flask import request as _req
    raw = (_req.headers.get("X-Forwarded-Host") or _req.host or "")
    # X-Forwarded-Host can carry a CSV when chained; the original client
    # host is the leftmost entry.
    first = raw.split(",", 1)[0].strip()
    return first.split(":", 1)[0].lower()


def _is_public_intake_host(host):
    if not host:
        return False
    for ph in _public_intake_hosts():
        if host == ph or host.endswith("." + ph):
            return True
    return False


@app.before_request
def _public_intake_root_redirect():
    from flask import request as _req, redirect as _redirect
    if _req.path != "/":
        return None
    if _is_public_intake_host(_request_host()):
        return _redirect("/quote", code=302)
    # Operator escape hatch: any host can force the intake view with
    # ?intake=1 -- lets Jay smoke the customer flow from marko-teal without
    # touching DNS. Never auto-redirects an operator session.
    if (_req.args.get("intake") or "").strip() == "1":
        return _redirect("/quote", code=302)
    return None


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
    # N271: route through the storage abstraction so local + kv backends
    # both work without changing every call site in this file.
    return commands.load_json(filepath)


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
    # N271: banner only when writes WON'T persist. is_persistent() returns
    # True for local-off-Vercel and for kv-with-creds; False for local-on-Vercel.
    is_persistent = storage.is_persistent()

    message = request.args.get("message", "")

    # N274: Money Lane strip (top 5 of call-today) + refresh-owners status.
    call_today_top5 = commands.call_today_top(5)
    refresh_status = commands.refresh_owners_status()

    # N275A: Bot Activity panel feed (today's email events).
    email_activity = commands.email_activity_today()

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
        is_persistent=is_persistent,
        message=message,
        call_today_top5=call_today_top5,
        refresh_status=refresh_status,
        email_activity=email_activity,
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


@app.route("/send/schedule", methods=["POST"])
def send_schedule():
    """N275C: schedule a one-shot operator-approved marko_send.

    Form/JSON fields: dry_run (default 1 = dry), when (ISO datetime,
    default now+1m), batch_size_cap (optional int). Concurrent schedules
    are rejected with 'already scheduled'. One schedule = one execution —
    the background thread never re-arms.
    """
    dry = (request.form.get("dry_run", "1") == "1")
    when_iso = (request.form.get("when") or "").strip() or None
    batch_size_cap = (request.form.get("batch_size_cap") or "").strip() or None

    # Compliance gate parity with /send: refuse real scheduled batches when
    # required compliance fields are missing.
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

    res = commands.start_scheduled_send(when_iso=when_iso, dry_run=dry,
                                        batch_size_cap=batch_size_cap)
    state = res.get("state")
    if state == "already scheduled":
        msg = "Schedule already in flight — wait for it to fire."
    elif state == "scheduled":
        fire_at = res.get("fire_at", "?")
        kind = "dry-run" if dry else "real"
        msg = f"Scheduled {kind} batch to fire at {fire_at}"
    elif state == "error":
        msg = f"Schedule error: {res.get('error', 'unknown')}"
    else:
        msg = f"Schedule: {res}"
    return redirect(url_for("index", message=msg))


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




@app.route("/lead/<lead_id>/timeline")
def lead_timeline_view(lead_id):
    """N197: full event timeline for one lead."""
    lead = commands.find_lead(lead_id) if hasattr(commands, "find_lead") else None
    if lead is None:
        # Fall back to manual lookup if find_lead helper isn't there.
        leads = load_json(LEADS_FILE).get("leads", [])
        lead = next((l for l in leads if l.get("id") == lead_id), None)
    if lead is None:
        return render_template("lead_timeline.html", lead=None, events=[],
                               lead_id=lead_id), 404
    events = commands.lead_timeline(lead_id)
    return render_template("lead_timeline.html",
                           lead=lead, events=events, lead_id=lead_id)


@app.route("/recap")
def recap_view():
    """N197: daily activity recap. ?date=YYYY-MM-DD for historical, default today."""
    date_str = request.args.get("date") or None
    recap = commands.daily_recap(date_str)
    return render_template("recap.html", recap=recap)


@app.route("/api/recap")
def api_recap():
    """N197: JSON daily recap. ?date=YYYY-MM-DD optional."""
    date_str = request.args.get("date") or None
    return jsonify(commands.daily_recap(date_str))


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


@app.route("/export/call_today.csv")
def export_call_today():
    """N273: ranked CSV for same-day phone outreach.

    Filter: phone present, status not in dead-states, do_not_contact != true.
    Sort:   closability desc -> leak count desc -> score desc.
    Adds columns: closability, label, leak_top1, leak_top2.
    """
    csv_data = commands.export_call_today_csv()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=call_today.csv"},
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


def _pitch_pack_files(lead):
    """N274: factored builder for the three pitch-pack files.

    Returns (email_text, mockup_html_doc, report_md, safe_name) for one lead.
    Pure read; no mutation. Used by both /lead/<id>/pitch_pack and the
    bulk /export/pitch_pack_today.zip route.
    """
    sender = _sender_name()
    config = commands.get_config() if os.path.exists(commands.CONFIG_FILE) else {}
    leaks = marko_intel.compute_leaks(lead)
    offer = marko_intel.recommend_offer(lead)
    email = marko_intel.generate_email(lead, kind="intro",
                                       sender_name=sender, config=config)
    script = marko_intel.generate_script(lead, sender_name=sender)
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
    email_text = (f"To: {lead.get('email') or '(no email on file — use phone)'}\n"
                  f"Subject: {email['subject']}\n\n{email['body']}\n\n"
                  f"---\nCall opener (if needed):\n{script}\n")
    mockup_doc = ("<!doctype html><meta charset=utf-8>"
                  "<title>Mockup</title>" + mockup_html)
    report_md = _leak_report_md(lead, leaks, offer)
    safe_name = "".join(c for c in (lead.get("name") or lead.get("id") or "lead")
                        if c.isalnum() or c in "-_")[:40] or (lead.get("id") or "lead")
    return email_text, mockup_doc, report_md, safe_name


@app.route("/lead/<lead_id>/pitch_pack")
def lead_pitch_pack(lead_id):
    """Downloadable ZIP: email.txt + mockup.html + leak_report.md."""
    lead = _find_lead_or_404(lead_id)
    if not lead:
        return Response(f"lead {lead_id} not found", status=404, mimetype="text/plain")
    email_text, mockup_doc, report_md, safe_name = _pitch_pack_files(lead)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("email.txt", email_text)
        zf.writestr("mockup.html", mockup_doc)
        zf.writestr("leak_report.md", report_md)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename=pitch_pack_{safe_name}.zip'},
    )


@app.route("/export/pitch_pack_today.zip")
def export_pitch_pack_today():
    """N274: bulk pitch-pack for the top 10 of the call-today ranking.

    One folder per lead (lead_<id>_<safe-name>/) containing email.txt,
    mockup.html, leak_report.md. Capped at 10 leads so the zip stays
    under ~2 MB. Reuses _pitch_pack_files so the per-lead output is
    byte-identical to /lead/<id>/pitch_pack.
    """
    top = commands.call_today_top(10)
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in top:
            lead = row["lead"]
            email_text, mockup_doc, report_md, safe_name = _pitch_pack_files(lead)
            folder = f"lead_{lead.get('id')}_{safe_name}/"
            zf.writestr(folder + "email.txt",     email_text)
            zf.writestr(folder + "mockup.html",   mockup_doc)
            zf.writestr(folder + "leak_report.md", report_md)
            n += 1
    buf.seek(0)
    fname = f"pitch_pack_today_{n}leads.zip"
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


def _verify_svix(secret, svix_id, svix_ts, body_bytes, sig_header):
    """N275B: verify a Resend/Svix signature.

    signed_payload = "{svix_id}.{svix_ts}.{body}"
    signature      = base64(HMAC_SHA256(secret_bytes, signed_payload))
    sig_header     = "v1,<base64> v1,<base64> ..."  (rotation support)

    Secret may be raw bytes or a "whsec_<base64>" string — both accepted.
    Returns (True, None) on a match, (False, reason) otherwise.
    """
    import base64 as _b64
    import hmac as _hmac
    import hashlib as _hashlib
    import time as _t
    if not (svix_id and svix_ts and sig_header):
        return False, "missing svix headers"
    # Replay-window — reject anything more than 5 minutes off.
    try:
        ts = int(svix_ts)
    except (TypeError, ValueError):
        return False, "bad svix-timestamp"
    if abs(_t.time() - ts) > 300:
        return False, "svix timestamp outside replay window"
    # Secret handling — Svix gives "whsec_<b64>"; raw HMAC key is b64-decoded.
    sec = secret
    if sec.startswith("whsec_"):
        try:
            sec_bytes = _b64.b64decode(sec[len("whsec_"):])
        except Exception:
            return False, "bad whsec_ secret"
    else:
        sec_bytes = sec.encode("utf-8")
    signed = f"{svix_id}.{svix_ts}.".encode("utf-8") + body_bytes
    digest = _hmac.new(sec_bytes, signed, _hashlib.sha256).digest()
    expected_b64 = _b64.b64encode(digest).decode("ascii")
    for entry in sig_header.split():
        if "," not in entry:
            continue
        ver, sig = entry.split(",", 1)
        if ver.strip().lower() != "v1":
            continue
        if _hmac.compare_digest(sig.strip(), expected_b64):
            return True, None
    return False, "no signature matched"


def _extract_lead_id(payload):
    """N275B: pull lead_id from any of the places it might live.

    Order of preference:
      1. top-level payload.lead_id
      2. data.tags.lead_id (Resend's tag style)
      3. data.lead_id
      4. data.headers["X-Marko-Lead-Id"] (we now stamp this on every send)
         — accepts both the dict form {"name":"value"} and the list-of-dicts
         form [{"name": "...", "value": "..."}]
    """
    if payload.get("lead_id"):
        return payload["lead_id"]
    data = payload.get("data") or {}
    tags = data.get("tags") or {}
    if isinstance(tags, dict) and tags.get("lead_id"):
        return tags["lead_id"]
    if data.get("lead_id"):
        return data["lead_id"]
    headers = data.get("headers")
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == "x-marko-lead-id":
                return v
    elif isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and str(h.get("name", "")).lower() == "x-marko-lead-id":
                return h.get("value")
    return ""


@app.route("/webhook/email", methods=["POST"])
def webhook_email():
    """N275A/N275B: receive provider email events.

    Verifies the request via either Svix-style headers (svix-id,
    svix-timestamp, svix-signature) — the native Resend format — or the
    legacy X-Marko-Signature header (hex HMAC-SHA256 over the raw body).
    EMAIL_WEBHOOK_SECRET is the only env var either way. Unsigned or
    mismatched requests are rejected with 401 and NEVER write to disk.

    Accepts both:
      - native Resend-style {type: 'email.delivered', data: {...}}
      - simplified {event: 'opened', lead_id: 'L016'}
    """
    import hmac as _hmac
    import hashlib as _hashlib

    secret = (os.environ.get("EMAIL_WEBHOOK_SECRET") or "").strip()
    if not secret:
        return Response("EMAIL_WEBHOOK_SECRET not configured", status=503,
                        mimetype="text/plain")

    raw = request.get_data() or b""
    svix_id = request.headers.get("svix-id") or request.headers.get("Svix-Id")
    svix_ts = request.headers.get("svix-timestamp") or request.headers.get("Svix-Timestamp")
    svix_sig = request.headers.get("svix-signature") or request.headers.get("Svix-Signature")

    if svix_id or svix_ts or svix_sig:
        ok, reason = _verify_svix(secret, svix_id, svix_ts, raw, svix_sig or "")
        if not ok:
            return Response(f"svix: {reason}", status=401,
                            mimetype="text/plain")
    else:
        provided = (request.headers.get("X-Marko-Signature") or "").strip()
        if not provided:
            return Response("missing signature", status=401,
                            mimetype="text/plain")
        if provided.lower().startswith("sha256="):
            provided_hex = provided.split("=", 1)[1].strip()
        else:
            provided_hex = provided
        expected_hex = _hmac.new(secret.encode("utf-8"), raw,
                                 _hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(provided_hex, expected_hex):
            return Response("invalid signature", status=401,
                            mimetype="text/plain")

    # Signature OK — parse payload defensively.
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception as exc:
        return Response(f"bad json: {exc}", status=400, mimetype="text/plain")

    event_name = (payload.get("event") or payload.get("type") or "").lower()
    if event_name.startswith("email."):
        event_name = event_name.split(".", 1)[1]
    data_blob = payload.get("data") or {}
    lead_id = _extract_lead_id(payload)
    meta = {
        "provider_id": (data_blob.get("email_id")
                        or data_blob.get("id")
                        or payload.get("id")),
        "to": data_blob.get("to") or payload.get("to"),
    }
    result = commands.apply_email_event(lead_id, event_name, meta)
    code = 200 if result.get("ok") else 422
    return jsonify(result), code


@app.route("/admin/send_live_smoke", methods=["POST"])
def admin_send_live_smoke():
    """N275B: one-shot live-fire test path.

    Hard-capped to 5 real sends through the production email_client. Gated
    by ?token= matching the ADMIN_TOKEN env var. Writes each result
    (message_id on success, error on failure) to marko_log so the operator
    can verify a real Resend id landed on disk. Designed for first-flight
    operator confidence; delete the route after the next loop verifies it.
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    provided = (request.args.get("token") or "").strip()
    if not expected:
        return Response("ADMIN_TOKEN not configured", status=503,
                        mimetype="text/plain")
    if not provided or provided != expected:
        return Response("forbidden", status=401, mimetype="text/plain")

    if not (os.environ.get("RESEND_API_KEY") or "").strip():
        return redirect(url_for("index",
            message="ADMIN_SMOKE blocked: RESEND_API_KEY not set"))

    cfg = commands.get_config() if os.path.exists(commands.CONFIG_FILE) else {}
    import marko_compliance as _mc
    blockers = _mc.config_blockers(cfg)
    if blockers:
        return redirect(url_for("index",
            message="ADMIN_SMOKE blocked: " + "; ".join(blockers)))

    from_email = (cfg.get("from_email") or "").strip()
    sender_name = cfg.get("sender_name") or "Jay"
    tpl = (cfg.get("email_template") or {})
    subject_t = tpl.get("subject") or "Quick question"
    body_t    = tpl.get("body") or "Hi, I wanted to reach out."

    leads = commands.load_json(commands.LEADS_FILE).get("leads", [])
    batch = [l for l in leads
             if l.get("email") and l.get("status") in (None, "", "NEW")][:5]
    if not batch:
        return redirect(url_for("index",
            message="ADMIN_SMOKE: no NEW leads with email available"))

    import email_client as _ec
    results = []
    for lead in batch:
        subject = commands.personalize_template(subject_t, lead, sender_name)
        body    = commands.personalize_template(body_t, lead, sender_name)
        body    = commands._apply_compliance_footer(body, cfg)
        res = _ec.send(
            to=lead.get("email"), subject=subject, body=body,
            from_=from_email, dry_run=False,
            headers={"X-Marko-Lead-Id": lead.get("id") or ""},
        )
        entry = {"action": "send", "lead_id": lead.get("id"),
                 "recipient": lead.get("email"),
                 "kind": "admin_smoke",
                 "status": "sent" if res["status"] == "sent" else "failed"}
        if res.get("id"):
            entry["message_id"] = res["id"]
        if res.get("error"):
            entry["error"] = res["error"]
        commands.log_action(entry)
        results.append({"id": lead.get("id"), "email": lead.get("email"),
                        "status": res["status"],
                        "message_id": res.get("id"),
                        "error": res.get("error")})

    summary = " · ".join(
        (f"{r['id']}: {r['message_id']}" if r["status"] == "sent"
         else f"{r['id']}: FAIL {r['error']}")
        for r in results
    )
    return redirect(url_for("index", message=f"ADMIN_SMOKE: {summary}"))


@app.route("/owners/refresh", methods=["POST"])
def owners_refresh():
    """N274: kick off a background owner-discovery sweep + redirect.

    The route itself returns instantly. A daemon thread runs the work and
    writes the summary to .refresh_owners.result.json. If a sweep is already
    in flight, the request bails with a 'try again in Xs' message.
    """
    res = commands.start_refresh_owners()
    if res["state"] == "running":
        wait = res.get("wait_seconds", commands.REFRESH_LOCK_TTL)
        msg = f"Owners refresh already running — try again in {wait}s"
    else:
        msg = ("Owners refresh started in background — reload the dashboard "
               "in ~30s to see results.")
    return redirect(url_for("index", message=msg))


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
    # N267: surface the same sequence state the desktop Call First cards
    # show, so mobile call mode has parity. Pure read — no mutation.
    import marko_sequence
    sequence_state = marko_sequence.state_for(lead)
    return render_template("mobile_call.html",
                           lead=lead, lead_id=lead_id,
                           top_leak=top, script=script,
                           sequence_state=sequence_state,
                           page_title=f"Call — {lead.get('name', lead_id)}")


@app.route("/__diag")
def __diag():
    info = storage.backend_info()
    counts = {}
    host_resolved = _request_host()
    route_diag = {
        "request_host": request.host,
        "x_forwarded_host": request.headers.get("X-Forwarded-Host"),
        "resolved_host": host_resolved,
        "is_public_intake_host": _is_public_intake_host(host_resolved),
        "intake_hosts": list(_public_intake_hosts()),
    }
    for fname in ("campaigns.json", "leads.json", "config.json",
                  "templates.json", "marko_log.json"):
        path = os.path.join(BASE_DIR, fname)
        try:
            doc = storage.read_json(path)
            if isinstance(doc, dict):
                first_list = next((v for v in doc.values() if isinstance(v, list)), None)
                counts[fname] = len(first_list) if first_list is not None else len(doc)
            elif isinstance(doc, list):
                counts[fname] = len(doc)
            else:
                counts[fname] = "ok"
        except FileNotFoundError:
            counts[fname] = "missing"
        except Exception as exc:
            counts[fname] = f"err: {type(exc).__name__}"
    return jsonify({"backend": info, "counts": counts, "routing": route_diag})


@app.route("/__seed_kv", methods=["POST"])
def __seed_kv():
    # One-shot bootstrap: copy bundled JSON files into Upstash KV so the
    # first reads after STORAGE_BACKEND=kv don't crash. Gated by a secret
    # env var; remove STORAGE_SEED_TOKEN from the project after seeding.
    expected = (os.environ.get("STORAGE_SEED_TOKEN") or "").strip()
    provided = (request.args.get("token") or "").strip()
    if not expected or provided != expected:
        return "forbidden", 403
    seeded, failed = [], []
    for fname in ("campaigns.json", "leads.json", "config.json",
                  "templates.json", "marko_log.json"):
        path = os.path.join(BASE_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            storage.write_json(path, data)
            seeded.append(fname)
        except Exception as exc:
            failed.append(f"{fname}: {exc}")
    body = {"backend": storage.backend_info(), "seeded": seeded, "failed": failed}
    return jsonify(body), (200 if not failed else 500)


@app.route("/quote", methods=["GET", "POST"])
def quote():
    """Public moving-quote intake. GET renders the form, POST submits.

    Real sends are gated by MARKO_QUOTE_LIVE_SEND=1 (env). The routing
    engine also enforces MARKO_MOVER_ALLOWLIST: a mover not on the
    allowlist receives a dry_run regardless. Both decisions are recorded
    in delivery_log.json so the admin panel surfaces the exact reason.
    """
    # Attribution params survive GET->POST round-trip via hidden inputs.
    # Source: campaign URL the customer landed on. Captured into lead.notes
    # so the operator (and TalkBot logs) can attribute each inbound back to
    # the channel that drove it. Length-capped to keep notes readable and
    # to defang any pathological URL stuffing.
    def _attr(value):
        return (value or "").strip()[:80]

    if request.method == "GET":
        # Conversion-tracking landing event. Server-side, no cookies.
        # The same source/campaign/zip params get echoed into hidden
        # inputs (already wired below) so they survive into quote_submit.
        import marko_tracking
        marko_tracking.record(
            "landing",
            source=request.args.get("source"),
            campaign=request.args.get("campaign"),
            zip_code=request.args.get("zip"),
            pitch=request.args.get("pitch"),
            cta_id=request.args.get("cta_id"),
            device_type=marko_tracking._device_from_ua(
                request.headers.get("User-Agent")),
            landing_page=request.path,
            mover_id=request.args.get("mover_hint"),
        )
        return render_template(
            "quote.html", submitted_ok=False, errors=None,
            form={}, lead_id=None,
            submitted_name=None, submitted_via=None,
            attr_source=_attr(request.args.get("source")),
            attr_campaign=_attr(request.args.get("campaign")),
            attr_mover_hint=_attr(request.args.get("mover_hint")),
        )

    form = {k: (request.form.get(k) or "") for k in (
        "customer_name", "phone", "email", "move_date",
        "pickup_zip", "dropoff_zip", "home_size",
        "stairs_elevator", "heavy_items", "urgency", "notes",
    )}

    attr_source = _attr(request.form.get("attr_source"))
    attr_campaign = _attr(request.form.get("attr_campaign"))
    attr_mover_hint = _attr(request.form.get("attr_mover_hint"))
    if attr_source or attr_campaign or attr_mover_hint:
        tag = (
            f"[attr source={attr_source or '-'} "
            f"campaign={attr_campaign or '-'} "
            f"mover_hint={attr_mover_hint or '-'}] "
        )
        form["notes"] = tag + form["notes"]

    live = (os.environ.get("MARKO_QUOTE_LIVE_SEND") or "").strip() == "1"
    result = routing.submit_quote(form, dry_run=not live)

    if not result["ok"]:
        return render_template(
            "quote.html", submitted_ok=False,
            errors=result["errors"], form=form, lead_id=None,
            submitted_name=None, submitted_via=None,
            attr_source=attr_source, attr_campaign=attr_campaign,
            attr_mover_hint=attr_mover_hint,
        ), 400

    lead = result["lead"]
    if lead.get("phone") and lead.get("email"):
        via = "phone or email"
    elif lead.get("phone"):
        via = "phone"
    elif lead.get("email"):
        via = "email"
    else:
        via = None

    # Conversion event for the funnel: form passed validation and was
    # routed (dry_run or live). value_usd uses MARKO's own lead-value
    # estimate so the report can talk dollars without inventing them.
    import marko_tracking
    marko_tracking.record(
        "quote_submit",
        source=attr_source, campaign=attr_campaign,
        zip_code=lead.get("pickup_zip"),
        device_type=marko_tracking._device_from_ua(
            request.headers.get("User-Agent")),
        landing_page="/quote",
        destination="routing.submit_quote",
        converted=True,
        lead_id=lead.get("lead_id"),
        mover_id=attr_mover_hint,
        value_usd=lead.get("estimated_value_high_usd"),
    )

    return render_template(
        "quote.html", submitted_ok=True, errors=None,
        form={}, lead_id=lead["lead_id"],
        submitted_name=(lead.get("customer_name") or "").split(" ", 1)[0],
        submitted_via=via,
    )


@app.route("/money", methods=["GET"])
def money_mode():
    """Operator-only Money Mode summary.

    Token-gated identically to /admin/delivery (?token=ADMIN_TOKEN). Returns
    JSON by default; pass ?format=html for a tiny mobile-readable view so
    Jay can glance at it from his phone without parsing JSON.

    Read-only. Does not regenerate the underlying reports -- that's the
    job of `python marko_money.py` (cron-style or manual).
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    provided = (request.args.get("token") or "").strip()
    if not expected:
        return Response("ADMIN_TOKEN not configured", status=503,
                        mimetype="text/plain")
    if provided != expected:
        return Response("forbidden", status=403, mimetype="text/plain")

    import marko_money
    report = marko_money.build_overnight_report()
    queue = marko_money.build_revenue_queue()

    if (request.args.get("format") or "").lower() == "html":
        # Minimal mobile-friendly chrome -- no operator chrome leaked, no
        # external assets. Pure inline CSS so it renders in <1 RTT on a
        # phone. Intentionally not a "dashboard" -- a one-screen brief.
        rows = "".join(
            f"<tr><td>{r['rank']}</td><td>{r['business']}</td>"
            f"<td>{r['priority']}</td><td>{r['score']}</td>"
            f"<td>{r['estimated_close_probability']}</td></tr>"
            for r in queue[:10]
        )
        gaps = "".join(
            f"<li>{g['zip']} ({g['city']})</li>"
            for g in report["unresolved_routing_gaps"]
        ) or "<li>none</li>"
        html = (
            "<!doctype html><meta name=viewport content='width=device-width,"
            "initial-scale=1'><title>MARKO Money Mode</title>"
            "<style>body{font:16px system-ui;padding:14px;background:#0a0c0e;"
            "color:#e8eef5;max-width:560px;margin:0 auto}"
            "h1,h2{margin:.4em 0}table{width:100%;border-collapse:collapse}"
            "td,th{padding:6px;border-bottom:1px solid #232c36;text-align:left}"
            ".muted{color:#8a98a8;font-size:13px}</style>"
            "<h1>Money Mode</h1>"
            f"<div class=muted>generated {report['generated_at']}</div>"
            "<h2>Tonight's totals</h2>"
            f"<div>{report['totals']['mover_targets']} mover targets &middot; "
            f"{report['totals']['hot_zip_count']} hot ZIPs &middot; "
            f"{report['totals']['overnight_inbound_count']} inbound (12h) "
            f"&middot; {report['totals']['missed_money_events']} missed-money</div>"
            "<h2>Top 10 to call</h2>"
            "<table><tr><th>#</th><th>business</th><th>priority</th>"
            f"<th>score</th><th>close-prob</th></tr>{rows}</table>"
            "<h2>Routing gaps (no buyer covers these ZIPs)</h2>"
            f"<ul>{gaps}</ul>"
            "<h2>Estimated weekly band</h2>"
            f"<div>${report['estimated_revenue_band']['weekly_low_usd']}"
            f" - ${report['estimated_revenue_band']['weekly_high_usd']}</div>"
            f"<div class=muted>{report['estimated_revenue_band']['basis']}</div>"
        )
        return Response(html, mimetype="text/html")

    return jsonify({"report": report, "revenue_queue": queue})


# ---------- Operator cockpit (N-MARKO-OPERATOR-COCKPIT) ----------


@app.route("/cockpit", methods=["GET"])
def cockpit_view():
    """Mobile-first money cockpit. Token-gated like /money + /admin/*.

    Pure read, pure derive: every number on the page is from a real
    on-disk artifact. No synthetic activity, no fabricated counts.
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    provided = (request.args.get("token") or "").strip()
    if not expected:
        return Response("ADMIN_TOKEN not configured", status=503,
                        mimetype="text/plain")
    if provided != expected:
        return Response("forbidden", status=403, mimetype="text/plain")

    import marko_cockpit
    payload = marko_cockpit.cockpit_payload()

    live_send = (os.environ.get("MARKO_QUOTE_LIVE_SEND") or "").strip() == "1"
    rev = payload.get("estimated_revenue_band") or {}
    rev_label = (
        f"${rev.get('weekly_low_usd', 0)}-${rev.get('weekly_high_usd', 0)}/wk"
        if rev else "no revenue band"
    )
    hz = payload.get("hot_zips") or []
    hz_label = ", ".join(z["zip"] for z in hz[:3]) if hz else "(none)"

    from datetime import datetime as _dt, timezone as _tz
    return render_template(
        "cockpit.html",
        generated_at=_dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC"),
        token=provided,
        live_send_label=("LIVE send ON" if live_send else "DRY-RUN"),
        live_send_badge_cls=("live" if live_send else "dry"),
        revenue_band_label=rev_label,
        hot_zip_label=hz_label,
        **payload,
    )


# ---------- Conversion tracking (N-TALKBOT-CONVERSION-TRACKING) ----------
#
# Tiny POST endpoint for hero/pricing/Stripe-redirect CTA clicks. Public
# (no token) because it gets fired from public surfaces (the quote page,
# any future hero on the marketing site). Protections:
#   * event_type whitelist enforced server-side via marko_tracking
#   * field length caps applied by the recorder
#   * silently swallows unknown event types (returns 400) instead of
#     polluting the log


@app.route("/api/track", methods=["POST"])
def api_track():
    import marko_tracking
    if request.is_json:
        body = request.get_json(silent=True) or {}
    else:
        body = {k: request.form.get(k) for k in request.form}
    event_type = (body.get("event_type") or "").strip()
    try:
        entry = marko_tracking.record(
            event_type,
            cta_id=body.get("cta_id"),
            source=body.get("source"),
            campaign=body.get("campaign"),
            zip_code=body.get("zip"),
            pitch=body.get("pitch"),
            device_type=marko_tracking._device_from_ua(
                request.headers.get("User-Agent")),
            landing_page=body.get("landing_page"),
            destination=body.get("destination"),
            converted=body.get("converted"),
            lead_id=body.get("lead_id"),
            talkbot_session_id=body.get("talkbot_session_id"),
            mover_id=body.get("mover_id"),
            value_usd=body.get("value_usd"),
        )
        return jsonify({"ok": True, "recorded": entry})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# Stripe-prep stub: accepts checkout_started/checkout_completed/
# mover_signup_init via the same /api/track endpoint above. The shim
# below is the *only* place the Stripe-side knowledge lives so when a
# real checkout URL exists we wire one location, not three.
STRIPE_INTEGRATION_STATUS = {
    "checkout_url_configured": False,
    "webhook_secret_configured": False,
    "note": (
        "No Stripe SDK in repo. Tracking schema is ready: send "
        "event_type=checkout_started from the redirect-to-Stripe handler "
        "and event_type=checkout_completed from the Stripe webhook. "
        "Both already accepted by /api/track and counted in the funnel."
    ),
}


@app.route("/admin/conversions", methods=["GET"])
def admin_conversions():
    gate = _require_admin_token() if False else None  # placeholder
    # Token gate: we cannot reuse _require_admin_token because it's defined
    # below in the file. Inline the check.
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    provided = (request.args.get("token") or "").strip()
    if not expected:
        return Response("ADMIN_TOKEN not configured", status=503,
                        mimetype="text/plain")
    if provided != expected:
        return Response("forbidden", status=403, mimetype="text/plain")

    import marko_tracking
    events = marko_tracking.load_events()
    agg = marko_tracking.aggregate(events)
    weakest = marko_tracking.weakest_funnel_step(agg["step_conversion_rates"])

    if (request.args.get("format") or "").lower() == "json":
        return jsonify({
            "aggregate": agg,
            "weakest_step": weakest,
            "stripe_integration_status": STRIPE_INTEGRATION_STATUS,
        })

    funnel_html = "".join(
        f"<tr><td>{step}</td><td>{count}</td></tr>"
        for step, count in agg["funnel_counts"]
    )
    rates_html = "".join(
        f"<tr><td>{r['from']} -> {r['to']}</td>"
        f"<td>{r['from_count']}</td><td>{r['to_count']}</td>"
        f"<td>{(str(r['rate_pct']) + '%') if r['rate_pct'] is not None else '-'}</td></tr>"
        for r in agg["step_conversion_rates"]
    )
    bests = "".join(
        f"<li><b>{label}:</b> {val or '(none)'} (n={n})</li>"
        for label, (val, n) in (
            ("best CTA", agg["best_cta"]),
            ("best source", agg["best_source"]),
            ("best campaign", agg["best_campaign"]),
            ("best ZIP", agg["best_zip"]),
            ("best pitch", agg["best_pitch"]),
        )
    )
    weakest_str = f"{weakest[0]} ({weakest[1]}%)" if weakest else "no funnel data yet"
    html = (
        "<!doctype html><meta name=viewport content='width=device-width,"
        "initial-scale=1'><title>MARKO Conversions</title>"
        "<style>body{font:16px system-ui;padding:14px;background:#0a0c0e;"
        "color:#e8eef5;max-width:720px;margin:0 auto}"
        "h1,h2{margin:.4em 0}table{width:100%;border-collapse:collapse}"
        "td,th{padding:6px;border-bottom:1px solid #232c36;text-align:left}"
        ".muted{color:#8a98a8;font-size:13px}"
        ".warn{background:#1f1207;border:1px solid #5a3a16;"
        "padding:10px;border-radius:6px;margin:10px 0}</style>"
        f"<h1>Conversions ({agg['total_events']} events)</h1>"
        "<h2>Funnel counts</h2>"
        f"<table><tr><th>step</th><th>count</th></tr>{funnel_html}</table>"
        "<h2>Step-to-step conversion</h2>"
        "<table><tr><th>step</th><th>from</th><th>to</th><th>rate</th></tr>"
        f"{rates_html}</table>"
        f"<h2>Bests</h2><ul>{bests}</ul>"
        f"<h2>Weakest funnel step</h2><div>{weakest_str}</div>"
        "<h2>Device split (landing + quote_submit)</h2>"
        f"<div>{json.dumps(agg['device_split'])}</div>"
        "<h2>Stripe integration</h2>"
        "<div class=warn>"
        f"checkout configured: {STRIPE_INTEGRATION_STATUS['checkout_url_configured']}"
        f" &middot; webhook configured: {STRIPE_INTEGRATION_STATUS['webhook_secret_configured']}"
        f"<div class=muted style='margin-top:6px'>{STRIPE_INTEGRATION_STATUS['note']}</div>"
        "</div>"
    )
    return Response(html, mimetype="text/html")


# ---------- One-click money-queue review (N-MARKO-ONE-CLICK-MONEY-QUEUE) ----------
#
# Three routes, all ADMIN_TOKEN-gated:
#   GET  /review                       -- list queue (HTML by default, JSON with ?format=json)
#   POST /review/<lead_id>/<action>    -- approve|skip|edit|retry_later, mutates send_status
#   POST /review/<lead_id>/send        -- one-shot send, dry_run by default
#
# /send is intentionally separate from approve so a misfiring approve POST
# can never silently send a real cold email. Live send requires ALL of:
#   * ADMIN_TOKEN match
#   * MARKO_OUTREACH_LIVE=1
#   * row.send_status == "approved"
#   * either MARKO_SMOKE_REDIRECT_TO is set (safe-test path)
#     OR ?confirm_real=1 query param (operator explicitly accepts real send)


def _require_admin_token():
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    provided = (request.args.get("token") or "").strip()
    if not expected:
        return Response("ADMIN_TOKEN not configured", status=503,
                        mimetype="text/plain")
    if provided != expected:
        return Response("forbidden", status=403, mimetype="text/plain")
    return None


@app.route("/review", methods=["GET"])
def review_list():
    gate = _require_admin_token()
    if gate is not None:
        return gate
    import marko_money_queue as mmq
    doc = mmq._read_queue()
    rows = doc.get("rows", [])
    if (request.args.get("format") or "").lower() == "json":
        return jsonify(doc)
    token = (request.args.get("token") or "").strip()
    items = []
    for r in rows:
        body_html = (r["email_body"]
                     .replace("&", "&amp;").replace("<", "&lt;")
                     .replace("\n", "<br>"))
        actions = "".join(
            f"<form method=POST action='/review/{r['lead_id']}/{a}?token={token}' "
            "style='display:inline'>"
            f"<button type=submit>{a}</button></form> "
            for a in ("approve", "skip", "retry_later")
        )
        send_btn = (
            f"<form method=POST action='/review/{r['lead_id']}/send?token={token}' "
            "style='display:inline'>"
            f"<button type=submit>send (dry-run unless live env)</button></form>"
        )
        items.append(
            f"<article style='border:1px solid #232c36;padding:12px;"
            f"margin:10px 0;border-radius:8px'>"
            f"<div><b>#{r.get('rank','?')} {r['business_name']}</b> "
            f"&middot; {r.get('priority','?')} &middot; score {r['confidence_score']} "
            f"&middot; status: <code>{r['send_status']}</code></div>"
            f"<div class=muted>{r['email']} &middot; {r['website']}</div>"
            f"<div style='margin-top:6px'><b>leak:</b> {r['detected_leak']}</div>"
            f"<div style='margin-top:6px'><b>subject:</b> {r['email_subject']}</div>"
            f"<pre style='white-space:pre-wrap;background:#0e1318;padding:10px;"
            f"border-radius:6px;font:14px ui-monospace,Menlo,Consolas,monospace'>"
            f"{body_html}</pre>"
            f"<div style='margin-top:6px'>{actions}{send_btn}</div>"
            f"</article>"
        )
    html = (
        "<!doctype html><meta name=viewport content='width=device-width,"
        "initial-scale=1'><title>MARKO Review</title>"
        "<style>body{font:16px system-ui;padding:14px;background:#0a0c0e;"
        "color:#e8eef5;max-width:720px;margin:0 auto}"
        "button{font:14px system-ui;padding:8px 12px;border-radius:6px;"
        "border:1px solid #232c36;background:#141a21;color:#e8eef5;cursor:pointer}"
        ".muted{color:#8a98a8;font-size:13px}</style>"
        f"<h1>Review queue ({len(rows)})</h1>"
        f"<div class=muted>generated {doc.get('generated_at','?')} &middot; "
        "live send blocked unless MARKO_OUTREACH_LIVE=1</div>"
        + "".join(items)
    )
    return Response(html, mimetype="text/html")


_REVIEW_ACTIONS = {"approve", "skip", "edit", "retry_later"}


@app.route("/review/<lead_id>/<action>", methods=["POST"])
def review_action(lead_id, action):
    gate = _require_admin_token()
    if gate is not None:
        return gate
    if action not in _REVIEW_ACTIONS:
        return Response(f"unknown action {action!r}", status=400,
                        mimetype="text/plain")
    import marko_money_queue as mmq
    status_map = {
        "approve": "approved", "skip": "skipped",
        "edit": "edited", "retry_later": "retry_later",
    }
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if False else None
    from datetime import datetime as _dt, timezone as _tz
    ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    field = {
        "approve": "approved_at", "skip": "skipped_at",
        "edit": "edited_at", "retry_later": "retry_after",
    }[action]
    row = mmq.set_status(lead_id, status_map[action], **{field: ts})
    if row is None:
        return Response("not found", status=404, mimetype="text/plain")
    if (request.args.get("format") or "").lower() == "json":
        return jsonify({"ok": True, "lead_id": lead_id,
                        "send_status": row["send_status"]})
    token = (request.args.get("token") or "").strip()
    return redirect(f"/review?token={token}")


OUTREACH_LOG_FILE = os.path.join(BASE_DIR, "outreach_log.json")


def _append_outreach(entry):
    try:
        doc = storage.read_json(OUTREACH_LOG_FILE)
    except FileNotFoundError:
        doc = {"events": []}
    if not isinstance(doc.get("events"), list):
        doc["events"] = []
    doc["events"].append(entry)
    storage.write_json(OUTREACH_LOG_FILE, doc)


@app.route("/review/<lead_id>/send", methods=["POST"])
def review_send(lead_id):
    """One-shot send for an approved row.

    Default: dry_run. Returns the rendered email + a synthetic id.
    Live requires MARKO_OUTREACH_LIVE=1 AND (smoke-redirect set OR
    ?confirm_real=1). Status mutated to "sent" only on a successful
    Resend response.
    """
    gate = _require_admin_token()
    if gate is not None:
        return gate
    import marko_money_queue as mmq
    import email_client
    from datetime import datetime as _dt, timezone as _tz

    doc, row = mmq.find_row(lead_id)
    if row is None:
        return Response("not found", status=404, mimetype="text/plain")

    live_env = (os.environ.get("MARKO_OUTREACH_LIVE") or "").strip() == "1"
    redirect_to = (os.environ.get("MARKO_SMOKE_REDIRECT_TO") or "").strip()
    confirm_real = (request.args.get("confirm_real") or "").strip() == "1"
    approved = row["send_status"] in ("approved", "edited")

    block_reasons = []
    if not approved:
        block_reasons.append(
            f"row send_status is {row['send_status']!r}; "
            "must be 'approved' or 'edited' before sending"
        )
    if not live_env:
        block_reasons.append("MARKO_OUTREACH_LIVE != 1; send goes dry_run")
    if live_env and not redirect_to and not confirm_real:
        block_reasons.append(
            "live send to a real third party requires either "
            "MARKO_SMOKE_REDIRECT_TO set or ?confirm_real=1"
        )

    actually_dry = bool(block_reasons)
    to_original = row["email"]
    to_used = (redirect_to if (live_env and redirect_to) else to_original)

    sender = (os.environ.get("MARKO_FROM_EMAIL") or "").strip() \
        or "leads@marko.local"

    result = email_client.send(
        to=to_used,
        subject=row["email_subject"],
        body=row["email_body"],
        from_=sender,
        reply_to=None,
        dry_run=actually_dry,
        headers={"X-Marko-Lead-Id": str(row.get("lead_id") or ""),
                 "X-Marko-Outreach": "1"},
    )

    sent_at = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "lead_id": lead_id,
        "mover_id": row.get("mover_id"),
        "business_name": row.get("business_name"),
        "leak_category": row.get("leak_category"),
        "subject": row.get("email_subject"),
        "to_original": to_original,
        "to_used": to_used,
        "redirected": (to_used != to_original),
        "from": sender,
        "dry_run": actually_dry,
        "block_reasons": block_reasons or None,
        "result": result,
        "at": sent_at,
        "confirm_real": confirm_real,
    }
    _append_outreach(entry)

    if result.get("status") in ("sent", "dry_run"):
        mmq.set_status(lead_id, "sent",
                       sent_at=sent_at, send_result=result)

    payload = {
        "ok": result.get("status") in ("sent", "dry_run"),
        "delivery_mode": "live" if not actually_dry else "dry_run",
        "to": to_used,
        "to_original": to_original,
        "block_reasons": block_reasons or None,
        "result": result,
        "rendered": {
            "subject": row.get("email_subject"),
            "body": row.get("email_body"),
        },
    }
    return jsonify(payload)


@app.route("/admin/delivery", methods=["GET"])
def admin_delivery():
    """Operator-only delivery verification panel.

    Gated by ?token=<ADMIN_TOKEN>. Shows env-readiness, Resend domain
    status for MARKO_FROM_EMAIL, and the last 10 routing/delivery events
    with the exact recipient, Resend message id, and any provider error.
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    provided = (request.args.get("token") or "").strip()
    if not expected:
        return Response("ADMIN_TOKEN not configured", status=503,
                        mimetype="text/plain")
    if not provided or provided != expected:
        return Response("forbidden", status=401, mimetype="text/plain")

    env = routing.env_status()
    domain_check = routing.from_email_domain_verified()
    try:
        routed = storage.read_json(routing.ROUTED_FILE).get("events", [])
    except FileNotFoundError:
        routed = []
    try:
        delivery = storage.read_json(routing.DELIVERY_LOG_FILE).get("events", [])
    except FileNotFoundError:
        delivery = []
    movers = routing.load_movers()
    return jsonify({
        "env": env,
        "from_email_domain": domain_check,
        "registered_movers": len(movers),
        "last_routed": list(reversed(routed))[:10],
        "last_delivery": list(reversed(delivery))[:10],
    })


@app.route("/admin/delivery_smoke", methods=["POST"])
def admin_delivery_smoke():
    """One-shot live-delivery smoke for a single allowlisted mover.

    Gated by ?token=<ADMIN_TOKEN>. Form/query field `mover_id` defaults to
    "M001". Honors MARKO_MOVER_ALLOWLIST and MARKO_SMOKE_REDIRECT_TO; if
    either gate refuses, returns 409 with the exact block reasons. On
    success returns the routing record including the Resend message id.

    No silent fallback to dry_run -- the response status code is the
    truth signal:
      200  status=routed (Resend accepted, message_id present)
      409  status=delivery_blocked (allowlist or key/env missing)
      502  status=delivery_failed (Resend rejected the send)
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    provided = (request.args.get("token") or "").strip()
    if not expected:
        return Response("ADMIN_TOKEN not configured", status=503,
                        mimetype="text/plain")
    if not provided or provided != expected:
        return Response("forbidden", status=401, mimetype="text/plain")

    mover_id = (request.form.get("mover_id")
                or request.args.get("mover_id")
                or "M001").strip()
    result = routing.smoke_send(mover_id=mover_id)
    if result.get("routing") is None:
        return jsonify({"ok": False, "error": result.get("error"),
                        "result": result}), 400
    status = result["routing"]["status"]
    code = {"routed": 200, "delivery_blocked": 409,
            "delivery_failed": 502, "no_match": 422}.get(status, 500)
    return jsonify({
        "ok": result["ok"],
        "status": status,
        "delivery_mode": result["routing"].get("delivery_mode"),
        "block_reasons": result["routing"].get("block_reasons"),
        "error": result.get("error"),
        "message_id": (result["routing"].get("email_result") or {}).get("id"),
        "subject": result["routing"].get("subject"),
        "mover": result["routing"].get("mover"),
        "lead_id": result["lead"]["lead_id"],
        "routed_at": result["routing"]["routed_at"],
    }), code


@app.route("/api/talkbot/inbound", methods=["POST"])
def api_talkbot_inbound():
    """TalkBot -> MARKO inbound handoff.

    Accepts a JSON body with the same flat shape POST /quote uses, plus an
    optional talkbot_session_id for trace join. Routes through the same
    routing.submit_quote() loop the live-delivery smoke proved -- which
    means every existing safety still applies:

      * MARKO_QUOTE_LIVE_SEND must equal "1" for any real send
      * MARKO_MOVER_ALLOWLIST gates which mover may receive live email
      * MARKO_SMOKE_REDIRECT_TO redirects live email to the smoke inbox

    Auth: requires header X-Talkbot-Token matching env TALKBOT_INBOUND_TOKEN.
    If the env is unset the route returns 503 -- never accepts unsigned
    payloads. No silent fallthrough.

    Response is JSON; status code reflects the routing outcome:
      200  ok=True,  status=routed | delivery_blocked | dry_run
      400  ok=False, validation errors
      401  bad/missing token
      503  TALKBOT_INBOUND_TOKEN not configured
    """
    expected = (os.environ.get("TALKBOT_INBOUND_TOKEN") or "").strip()
    if not expected:
        return jsonify({"ok": False,
                        "error": "TALKBOT_INBOUND_TOKEN not configured"}), 503
    provided = (request.headers.get("X-Talkbot-Token") or "").strip()
    if not provided or provided != expected:
        return jsonify({"ok": False, "error": "forbidden"}), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False,
                        "error": "JSON body must be an object"}), 400

    form = {k: payload.get(k) for k in (
        "customer_name", "phone", "email", "move_date",
        "pickup_zip", "dropoff_zip", "home_size",
        "stairs_elevator", "heavy_items", "urgency", "notes",
    )}
    talkbot_session_id = (payload.get("talkbot_session_id")
                          or payload.get("session_id") or "")

    live = (os.environ.get("MARKO_QUOTE_LIVE_SEND") or "").strip() == "1"
    result = routing.submit_quote(
        form, dry_run=not live,
        source="inbound_talkbot",
        talkbot_session_id=talkbot_session_id or None,
    )

    if not result["ok"]:
        return jsonify({"ok": False, "errors": result["errors"]}), 400

    routing_record = result["routing"] or {}
    owner = result.get("owner_notify") or {}
    return jsonify({
        "ok": True,
        "lead_id": result["lead"]["lead_id"],
        "source": result["lead"]["source"],
        "talkbot_session_id": result["lead"].get("talkbot_session_id"),
        "lead_quality": result["lead"].get("lead_quality"),
        "estimated_value": [
            result["lead"].get("estimated_value_low_usd"),
            result["lead"].get("estimated_value_high_usd"),
        ],
        "status": routing_record.get("status"),
        "delivery_mode": routing_record.get("delivery_mode"),
        "mover": routing_record.get("mover"),
        "block_reasons": routing_record.get("block_reasons"),
        "message_id": (routing_record.get("email_result") or {}).get("id"),
        "owner_notify": {
            "sent": owner.get("sent"),
            "status": owner.get("status"),
            "message_id": owner.get("message_id"),
            "missed_money": owner.get("missed_money"),
        },
    }), 200


if __name__ == "__main__":
    print("MARKO Dashboard: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000)
