"""MARKO money-engine derivation (N-SLEEP-MONEY-ENGINE).

Reads the artifacts that already exist on disk and produces two
operator-facing summaries:

  overnight_money_report.json   end-of-night snapshot for tomorrow
  daily_revenue_queue.json      ranked "who to call to make money today"

Inputs (no new sources, no scrape, no HTTP):
  overnight_money_queue.json    real ranked mover targets (marko_overnight)
  hot_zips.json                 derived public demand (marko_demand)
  routing_ready.json            per-opp mover match preview (marko_demand)
  inbound_leads.json            customer inbound (routing.submit_quote)
  delivery_log.json             every routing/owner-notify outcome
  missed_money.json             owner-notify failures Jay must recover

Pure derive. Never sends, never scrapes, never mutates input files.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(BASE_DIR, "overnight_money_queue.json")
HOT_ZIPS_FILE = os.path.join(BASE_DIR, "hot_zips.json")
ROUTING_READY_FILE = os.path.join(BASE_DIR, "routing_ready.json")
INBOUND_FILE = os.path.join(BASE_DIR, "inbound_leads.json")
DELIVERY_LOG_FILE = os.path.join(BASE_DIR, "delivery_log.json")
MISSED_MONEY_FILE = os.path.join(BASE_DIR, "missed_money.json")
MOVERS_FILE = os.path.join(BASE_DIR, "movers.json")

REPORT_FILE = os.path.join(BASE_DIR, "overnight_money_report.json")
REVENUE_QUEUE_FILE = os.path.join(BASE_DIR, "daily_revenue_queue.json")

# Honest close-probability bands. Not invented for marketing -- these
# are coarse priors based on call_priority + score band so the operator
# panel doesn't have to invent its own. Every consumer of this number
# must read it as "MARKO's prior, not a forecast".
PROB_BANDS = (
    (90,  "20-35%"),
    (70,  "12-20%"),
    (40,  "5-12%"),
    (10,  "2-5%"),
    (-1000, "<2%"),
)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path, default):
    try:
        return storage.read_json(path)
    except FileNotFoundError:
        return default


def _close_prob_band(score):
    for threshold, band in PROB_BANDS:
        if score >= threshold:
            return band
    return "<2%"


def _best_call_window(target):
    """Heuristic: owner-operated movers answer best 8-10am or 4-6pm
    (between jobs). Aggregators/chains take calls on a desk-hours window.
    """
    parts = target.get("score_parts") or {}
    if "owner_operated" in parts:
        return "08:00-10:00 ET or 16:00-18:00 ET (between job blocks)"
    if "chain" in parts or "aggregator" in parts:
        return "10:00-15:00 ET (desk hours)"
    return "09:00-12:00 ET"


def _overnight_window_hours(hours=12):
    """Inbound leads received in the last N hours. Default 12 = overnight."""
    inbound = _load(INBOUND_FILE, {"leads": []}).get("leads", [])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for l in inbound:
        ts = (l.get("submitted_at") or "").strip()
        if not ts:
            continue
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if t >= cutoff:
            out.append(l)
    return out


def _delivery_outcomes(hours=12):
    events = _load(DELIVERY_LOG_FILE, {"events": []}).get("events", [])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    counts = {"sent": 0, "dry_run": 0, "blocked": 0, "failed": 0, "other": 0}
    for e in events:
        ts = (e.get("at") or "").strip()
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if t < cutoff:
            continue
        s = (e.get("status") or "").lower()
        if s in counts:
            counts[s] += 1
        elif s.startswith("delivery_blocked"):
            counts["blocked"] += 1
        elif s.startswith("delivery_failed"):
            counts["failed"] += 1
        else:
            counts["other"] += 1
    return counts


def _routing_gaps():
    rr = _load(ROUTING_READY_FILE, {"routing_previews": []})
    previews = rr.get("routing_previews", [])
    return [
        {
            "zip": p["zip"], "city": p["city"],
            "opportunity_id": p["opportunity_id"],
            "reason": "no active mover covers this ZIP or city",
        }
        for p in previews if not p.get("matched_mover")
    ]


def _estimated_revenue(targets, hot_zips, hours_inbound):
    """Honest range: closes-this-week estimate based on call_today bucket
    and the live demand surface. Floor uses very pessimistic 5% close;
    ceiling uses 25%. Reported as a band, never a single number.
    """
    call_today = [t for t in targets if t.get("call_priority") == "call_today"]
    hot = len(hot_zips)
    inbound = len(hours_inbound)
    # First-paid-lead test is $20-$50. Conservative weekly model:
    #   weekly_low  = call_today_count * 0.05 * $20  + inbound * 0.10 * $20
    #   weekly_high = call_today_count * 0.25 * $50  + inbound * 0.30 * $50
    # Hot ZIP count adds opportunity surface but no direct $; surfaced
    # as context, not multiplied in.
    low  = int(len(call_today) * 0.05 * 20 + inbound * 0.10 * 20)
    high = int(len(call_today) * 0.25 * 50 + inbound * 0.30 * 50)
    return {
        "weekly_low_usd": low,
        "weekly_high_usd": high,
        "basis": (
            f"{len(call_today)} call_today movers @ 5-25% close * $20-$50 "
            f"first-lead test, plus {inbound} overnight inbound @ 10-30% "
            f"close. Hot-ZIP count ({hot}) is opportunity surface, not "
            "counted in the dollar band."
        ),
    }


def build_revenue_queue():
    """Per-target row in the format the contract specified.

    Reuses the ranked queue from marko_overnight as the substrate so
    there is exactly one ranking source of truth.
    """
    queue = _load(QUEUE_FILE, {"targets": []}).get("targets", [])
    rows = []
    for t in queue:
        rows.append({
            "rank": t.get("rank"),
            "business": t.get("business_name"),
            "phone": t.get("phone"),
            "email": t.get("email"),
            "city": t.get("city"),
            "priority": t.get("call_priority"),
            "score": t.get("score"),
            "why_they_might_buy": t.get("why_they_might_buy"),
            "recommended_pitch": (t.get("outreach_message") or {}).get(
                "phone_script"
            ),
            "estimated_close_probability": _close_prob_band(t.get("score") or 0),
            "best_capture_url": t.get("capture_url"),
            "best_time_to_contact": _best_call_window(t),
            "covers_hot_zip": bool((t.get("score_parts") or {}).get("covers_hot_zip")),
        })
    return rows


def build_overnight_report():
    queue = _load(QUEUE_FILE, {"targets": []}).get("targets", [])
    hot = _load(HOT_ZIPS_FILE, {"hot_zips": []}).get("hot_zips", [])
    inbound = _overnight_window_hours(12)
    deliveries = _delivery_outcomes(12)
    gaps = _routing_gaps()
    movers = _load(MOVERS_FILE, {"movers": []}).get("movers", [])

    top_call = [t for t in queue if t.get("call_priority") == "call_today"][:5]
    top_zips = hot[:3]

    missed = _load(MISSED_MONEY_FILE, {"events": []}).get("events", [])

    return {
        "schema_version": "1.0.0",
        "generated_at": _now_iso(),
        "kind": "overnight_money_report",
        "totals": {
            "mover_targets": len(queue),
            "active_movers_in_registry": sum(1 for m in movers if m.get("active")),
            "hot_zip_count": len(hot),
            "overnight_inbound_count": len(inbound),
            "missed_money_events": len(missed),
            "routing_gaps": len(gaps),
        },
        "top_movers_to_call_tomorrow": [
            {
                "rank": t.get("rank"),
                "business": t.get("business_name"),
                "phone": t.get("phone"),
                "email": t.get("email"),
                "score": t.get("score"),
                "why_first": t.get("why_they_might_buy"),
                "best_time": _best_call_window(t),
            }
            for t in top_call
        ],
        "top_zips": top_zips,
        "best_capture_pages": sorted({h.get("recommended_capture_page")
                                      for h in hot if h.get("recommended_capture_page")}),
        "best_cta_source": (
            "https://quote.bookermove.com/quote?source=marko"
            "&campaign=richmond_movers"
        ),
        "best_lead_source_observed": (
            _best_lead_source(inbound) if inbound else
            "no overnight inbound -- best source unknown"
        ),
        "unresolved_routing_gaps": gaps,
        "delivery_outcomes_last_12h": deliveries,
        "missed_money_recent": missed[-5:] if missed else [],
        "estimated_revenue_band": _estimated_revenue(queue, hot, inbound),
        "policy": {
            "auto_send": False,
            "live_email_send": (
                (os.environ.get("MARKO_QUOTE_LIVE_SEND") or "").strip() == "1"
            ),
            "data_sources_public_only": True,
        },
    }


def _best_lead_source(inbound):
    """Group inbound by `source` (inbound_quote / inbound_talkbot / smoke)
    and return the dominant non-smoke source.
    """
    counts = {}
    for l in inbound:
        s = (l.get("source") or "unknown").strip()
        if s == "smoke":
            continue
        counts[s] = counts.get(s, 0) + 1
    if not counts:
        return "no non-smoke inbound in window"
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[0]
    return f"{top[0]} (count={top[1]})"


def write_all():
    report = build_overnight_report()
    queue = build_revenue_queue()
    storage.write_json(REPORT_FILE, report)
    storage.write_json(REVENUE_QUEUE_FILE, {
        "schema_version": "1.0.0",
        "generated_at": _now_iso(),
        "kind": "daily_revenue_queue",
        "row_count": len(queue),
        "rows": queue,
        "policy": {"auto_send": False, "data_sources_public_only": True},
    })
    return {"report": report, "revenue_queue": queue}


if __name__ == "__main__":
    out = write_all()
    print(json.dumps({
        "report_totals": out["report"]["totals"],
        "revenue_queue_rows": len(out["revenue_queue"]),
        "files": [
            os.path.relpath(REPORT_FILE, BASE_DIR),
            os.path.relpath(REVENUE_QUEUE_FILE, BASE_DIR),
        ],
    }, indent=2))
