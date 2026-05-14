"""MARKO operator cockpit data layer (N-MARKO-OPERATOR-COCKPIT).

One job: turn the existing on-disk artifacts into the small, opinionated
data shape the cockpit template renders. Pure derive -- no scrape, no
mutation of any input file, no synthetic activity.

Inputs (every file already exists; this layer reads, never writes):
  money_queue.json              ranked, draftable, send_status state
  overnight_money_report.json   totals + estimated revenue band
  hot_zips.json                 derived public demand (high urgency)
  missed_money.json             owner-notify failures
  conversion_events.json        landing/cta_click/quote_submit/checkout_*
  outreach_log.json             every send (dry_run + live)
  delivery_log.json             mover-side delivery outcomes
  inbound_leads.json            customer submissions (from /quote)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load(path, default):
    full = os.path.join(BASE_DIR, path)
    try:
        return storage.read_json(full)
    except FileNotFoundError:
        return default


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


# ---------- Lead tiers (real states only) ----------

# Maps to the cockpit's six visual tiers. We never invent tier counts:
# WON and DEAD are not in the queue's send_status enum, so until we add
# them they stay at 0 with an honest tooltip.
TIER_BUCKETS = (
    ("HOT",       "draft_only_call_today"),
    ("WARM",      "draft_only_warm"),
    ("LOW",       "draft_only_low"),
    ("APPROVED",  "approved_or_edited"),
    ("CONTACTED", "sent"),
    ("SKIPPED",   "skipped"),
)


def _tier_for(row):
    s = row.get("send_status")
    p = row.get("priority")
    if s == "sent":               return "CONTACTED"
    if s == "skipped":            return "SKIPPED"
    if s in ("approved", "edited"): return "APPROVED"
    if s == "retry_later":        return "LOW"  # deferred -> back-of-queue
    # draft_only -> classify by priority
    if p == "call_today":         return "HOT"
    if p == "warm":               return "WARM"
    return "LOW"


def lead_tier_counts(rows):
    counts = {label: 0 for label, _ in TIER_BUCKETS}
    counts["WON"] = 0
    counts["DEAD"] = 0
    for r in rows:
        counts[_tier_for(r)] += 1
    return counts


# ---------- Live activity feed (real events only) ----------

def _ev_inbound(l):
    name = l.get("customer_name") or "(no name)"
    pickup = l.get("pickup_zip") or "??"
    return {
        "ts": l.get("submitted_at"),
        "kind": "inbound",
        "headline": f"New customer quote: {name} from {pickup}",
        "detail": f"lead_id {l.get('lead_id')} · source {l.get('source') or '?'}",
    }


def _ev_outreach(e):
    biz = e.get("business_name") or "(unknown business)"
    leak = e.get("leak_category") or "?"
    if e.get("dry_run"):
        kind = "outreach_dry"
        head = f"Outreach prepared (dry-run): {biz}"
    elif e.get("redirected"):
        kind = "outreach_smoke"
        head = f"Outreach sent (smoke-redirect): {biz}"
    else:
        kind = "outreach_live"
        head = f"Outreach sent (LIVE): {biz}"
    return {
        "ts": e.get("at"),
        "kind": kind,
        "headline": head,
        "detail": f"leak {leak} · subject \"{e.get('subject') or ''}\"",
    }


def _ev_delivery(e):
    status = e.get("status") or "?"
    mover = e.get("mover_id") or e.get("delivery_kind") or "?"
    return {
        "ts": e.get("at"),
        "kind": f"delivery_{status}",
        "headline": f"Delivery {status}: {mover}",
        "detail": f"to {e.get('to') or '?'} · mode {e.get('delivery_mode') or '?'}",
    }


def _ev_missed(e):
    summ = e.get("lead_summary") or {}
    return {
        "ts": e.get("at"),
        "kind": "missed_money",
        "headline": f"MISSED MONEY: {summ.get('customer_name') or '(no name)'}",
        "detail": (
            f"reason: {e.get('reason') or '?'}; "
            f"value est ${(summ.get('estimated_value') or [0,0])[1]}"
        ),
    }


def _ev_conv(e):
    et = e.get("event_type") or "?"
    pieces = []
    for k in ("cta_id", "source", "campaign", "zip"):
        v = e.get(k)
        if v:
            pieces.append(f"{k}={v}")
    head_map = {
        "landing":            "Public landing",
        "cta_click":          "CTA click",
        "quote_submit":       "Quote submitted",
        "checkout_started":   "Checkout started",
        "checkout_completed": "Checkout completed",
        "mover_signup_init":  "Mover signup started",
    }
    return {
        "ts": e.get("timestamp"),
        "kind": f"track_{et}",
        "headline": head_map.get(et, et),
        "detail": " · ".join(pieces) or "(no params)",
    }


def live_activity(limit=15):
    """Merge real events from every log; return newest first."""
    inbound = _load("inbound_leads.json", {"leads": []}).get("leads", [])
    outreach = _load("outreach_log.json", {"events": []}).get("events", [])
    delivery = _load("delivery_log.json", {"events": []}).get("events", [])
    missed = _load("missed_money.json", {"events": []}).get("events", [])
    conv = _load("conversion_events.json", {"events": []}).get("events", [])

    feed = []
    feed.extend(_ev_inbound(l) for l in inbound)
    feed.extend(_ev_outreach(e) for e in outreach)
    feed.extend(_ev_delivery(e) for e in delivery)
    feed.extend(_ev_missed(e) for e in missed)
    feed.extend(_ev_conv(e) for e in conv)

    feed = [f for f in feed if f["ts"]]
    feed.sort(key=lambda f: _parse(f["ts"]) or datetime.min.replace(
        tzinfo=timezone.utc), reverse=True)
    return feed[:limit]


# ---------- Top-of-cockpit summary ----------

def cockpit_payload():
    queue_doc = _load("money_queue.json", {"rows": []})
    rows = queue_doc.get("rows", [])
    report = _load("overnight_money_report.json", {})
    hot_zips = _load("hot_zips.json", {"hot_zips": []}).get("hot_zips", [])
    missed = _load("missed_money.json", {"events": []}).get("events", [])
    outreach = _load("outreach_log.json", {"events": []}).get("events", [])

    tiers = lead_tier_counts(rows)
    top_hot = [r for r in rows
               if _tier_for(r) == "HOT" and r.get("email")]
    top_hot = top_hot[:5]
    approved_ready = sum(1 for r in rows
                         if r.get("send_status") in ("approved", "edited"))
    sent_today = sum(1 for r in rows if r.get("send_status") == "sent")

    leak_counts = {}
    for r in rows:
        cat = r.get("leak_category") or "?"
        leak_counts[cat] = leak_counts.get(cat, 0) + 1
    strongest_leak = (sorted(leak_counts.items(),
                             key=lambda kv: kv[1], reverse=True)[0]
                      if leak_counts else (None, 0))

    rev_band = (report.get("estimated_revenue_band") or {})

    return {
        "tiers": tiers,
        "top_hot": top_hot,
        "hot_zips": hot_zips,
        "approved_ready_to_send": approved_ready,
        "outreach_sent_count": sent_today,
        "outreach_log_total": len(outreach),
        "missed_money_count": len(missed),
        "strongest_leak": {
            "category": strongest_leak[0],
            "count": strongest_leak[1],
        },
        "estimated_revenue_band": rev_band,
        "operator_workflow": [
            ("review",   "Review opportunities", "/review",
             f"{tiers['HOT'] + tiers['WARM']} drafts open"),
            ("approve",  "Approve outreach", "/review",
             "click approve on a draft"),
            ("send",     "Send queue", "/review",
             f"{approved_ready} approved, ready"),
            ("monitor",  "Monitor delivery", "/admin/delivery",
             f"{tiers['CONTACTED']} contacted"),
            ("track",    "Track conversions", "/admin/conversions",
             "/api/track + funnel"),
        ],
        "live_activity": live_activity(15),
    }
