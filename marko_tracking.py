"""MARKO conversion tracking (N-TALKBOT-CONVERSION-TRACKING).

Lightweight, server-side, no cookies, no third-party SDK. Every observable
funnel step appends one record to conversion_events.json. Reports derive
from that single source.

Event-type vocabulary (closed set; reject anything else at the recorder):
  landing            GET /quote -- a customer reached the public form
  cta_click          POST /api/track for any hero/pricing CTA
  quote_submit       POST /quote success -- form went into routing
  checkout_started   placeholder for the Stripe redirect we don't have yet
  checkout_completed placeholder for the Stripe webhook we don't have yet
  mover_signup_init  placeholder for the mover-onboarding flow

All Stripe-side event types are reserved up front so the moment a real
checkout URL exists, the same recorder accepts the new events without
schema drift -- and the conversion_report.md "weakest funnel step"
calculation already counts them as 0/0 instead of crashing.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(BASE_DIR, "conversion_events.json")

ALLOWED_EVENT_TYPES = {
    "landing",
    "cta_click",
    "quote_submit",
    "checkout_started",
    "checkout_completed",
    "mover_signup_init",
}

# Conversion model: every event has one upstream "source" event type
# (the step that, if present, makes this event count as a conversion of
# that step). Read top-down for the funnel order.
FUNNEL_ORDER = (
    "landing",
    "cta_click",
    "quote_submit",
    "checkout_started",
    "checkout_completed",
)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _device_from_ua(ua):
    """Coarse device sniff. Avoids any tracking-pixel territory -- the
    UA was already on the request, we just bucket it.
    """
    if not ua:
        return "unknown"
    s = ua.lower()
    if any(k in s for k in ("iphone", "android", "mobile", "ipad")):
        # iPad is technically tablet; for funnel-step analysis we treat
        # it as mobile (single-pane layout, touch input).
        return "mobile"
    if "bot" in s or "crawler" in s or "spider" in s:
        return "bot"
    return "desktop"


def _clean(value, max_len=120):
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) > max_len:
        s = s[:max_len]
    # Strip control chars to keep the JSON readable
    return re.sub(r"[\x00-\x1f\x7f]", "", s)


def record(event_type, *, cta_id=None, source=None, campaign=None,
           zip_code=None, pitch=None, device_type=None, landing_page=None,
           destination=None, converted=None, lead_id=None,
           talkbot_session_id=None, mover_id=None, value_usd=None):
    """Append one event. Returns the stored record (with timestamp).

    Refuses unknown event_type -- the closed enum keeps junk out of the
    aggregations. `converted` is a boolean for upstream-step success;
    when None it's recorded as None (the report computes conversion
    rates by joining on lead_id / source / campaign rather than from
    this flag, so leaving it null is safe).
    """
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type!r}")
    entry = {
        "timestamp": _now_iso(),
        "event_type": event_type,
        "cta_id": _clean(cta_id),
        "source": _clean(source),
        "campaign": _clean(campaign),
        "zip": _clean(zip_code, max_len=10),
        "pitch": _clean(pitch),
        "device_type": _clean(device_type or "unknown", max_len=20),
        "landing_page": _clean(landing_page, max_len=200),
        "destination": _clean(destination, max_len=200),
        "converted": (None if converted is None else bool(converted)),
        "lead_id": _clean(lead_id, max_len=40),
        "talkbot_session_id": _clean(talkbot_session_id, max_len=80),
        "mover_id": _clean(mover_id, max_len=20),
        "value_usd": (None if value_usd is None else float(value_usd)),
    }
    try:
        doc = storage.read_json(EVENTS_FILE)
    except FileNotFoundError:
        doc = {"events": []}
    if not isinstance(doc.get("events"), list):
        doc["events"] = []
    doc["events"].append(entry)
    storage.write_json(EVENTS_FILE, doc)
    return entry


def load_events():
    try:
        return storage.read_json(EVENTS_FILE).get("events", [])
    except FileNotFoundError:
        return []


# ---------- Aggregation (used by /admin/conversions and the .md report) ----

def _bucketed(events, key_fn):
    out = {}
    for e in events:
        k = key_fn(e) or "(unset)"
        out.setdefault(k, []).append(e)
    return out


def _count_by_event(events, event_type):
    return sum(1 for e in events if e["event_type"] == event_type)


def funnel_counts(events):
    """Return ordered list of (step, count) tuples in funnel order."""
    return [(step, _count_by_event(events, step)) for step in FUNNEL_ORDER]


def step_conversion_rates(events):
    """For each adjacent pair in FUNNEL_ORDER, compute next/prev * 100.

    None for an undefined ratio (no prev events). Lets the report flag
    the weakest step honestly without dividing by zero.
    """
    counts = dict(funnel_counts(events))
    out = []
    for prev, nxt in zip(FUNNEL_ORDER, FUNNEL_ORDER[1:]):
        denom = counts[prev]
        ratio = (counts[nxt] / denom * 100) if denom else None
        out.append({
            "from": prev, "to": nxt,
            "from_count": counts[prev], "to_count": counts[nxt],
            "rate_pct": (round(ratio, 1) if ratio is not None else None),
        })
    return out


def best_by(events, key):
    """Return (top_value, count) for whichever value of `key` (e.g. cta_id,
    source, campaign, zip) has the most conversion-bearing events. Ties
    break alphabetically. Empty -> (None, 0).
    """
    pool = [e for e in events
            if e["event_type"] in ("cta_click", "quote_submit",
                                   "checkout_started", "checkout_completed")]
    counts = {}
    for e in pool:
        v = e.get(key) or ""
        if not v:
            continue
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return (None, 0)
    top_v, top_n = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return (top_v, top_n)


def device_split(events):
    pool = [e for e in events if e["event_type"] in ("landing", "quote_submit")]
    out = {"mobile": 0, "desktop": 0, "unknown": 0, "bot": 0}
    for e in pool:
        d = (e.get("device_type") or "unknown")
        out[d] = out.get(d, 0) + 1
    return out


def aggregate(events=None):
    if events is None:
        events = load_events()
    return {
        "total_events": len(events),
        "funnel_counts": funnel_counts(events),
        "step_conversion_rates": step_conversion_rates(events),
        "best_cta": best_by(events, "cta_id"),
        "best_source": best_by(events, "source"),
        "best_campaign": best_by(events, "campaign"),
        "best_zip": best_by(events, "zip"),
        "best_pitch": best_by(events, "pitch"),
        "device_split": device_split(events),
    }


def weakest_funnel_step(rates):
    """Lowest non-None rate is the weakest. Returns (label, rate_pct) or None.
    """
    candidates = [r for r in rates if r["rate_pct"] is not None]
    if not candidates:
        return None
    worst = min(candidates, key=lambda r: r["rate_pct"])
    return (f"{worst['from']} -> {worst['to']}", worst["rate_pct"])
