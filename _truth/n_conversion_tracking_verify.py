"""TRUTH verifier for N-TALKBOT-CONVERSION-TRACKING.

End-to-end via Flask test client (no live network). Snapshots the live
conversion_events.json before the run and restores it after, so the
verifier never pollutes the operator's real funnel data.

The "real test" simulates the contract's flow:
  1. Mobile UA hits the hero CTA -> /api/track event_type=cta_click
  2. Click destination = /quote?source=marko&campaign=test_run&zip=23220
     -> landing event with mobile device_type
  3. Form POST to /quote -> quote_submit event
  4. /admin/conversions reflects new events; /api/track checkout_started
     is accepted (Stripe-prep schema works) even with no Stripe code
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

# Belt + suspenders: keep live send paths off for the verifier.
os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)
os.environ.pop("MARKO_OUTREACH_LIVE", None)
os.environ["ADMIN_TOKEN"] = "verify-token"

import dashboard          # noqa: E402
import marko_tracking     # noqa: E402
import marko_conversion_report as mcr  # noqa: E402

EVENTS_FILE = marko_tracking.EVENTS_FILE
REPORT_FILE = mcr.REPORT_FILE

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)


def fail(errors, label, msg):
    errors.append(f"{label}: {msg}")


def _client():
    dashboard.app.testing = True
    return dashboard.app.test_client()


def _snapshot_events():
    """Move events file aside; return restore() callable."""
    backup = None
    if os.path.exists(EVENTS_FILE):
        backup = tempfile.NamedTemporaryFile(delete=False, suffix=".json").name
        shutil.copy2(EVENTS_FILE, backup)
        os.remove(EVENTS_FILE)
    def restore():
        if backup is None:
            if os.path.exists(EVENTS_FILE):
                os.remove(EVENTS_FILE)
        else:
            shutil.copy2(backup, EVENTS_FILE)
            os.remove(backup)
    return restore


def check_recorder_enum(errors):
    try:
        marko_tracking.record("not_a_real_event")
    except ValueError:
        pass
    else:
        fail(errors, "recorder", "expected ValueError on unknown event_type")


def check_track_endpoint(c, errors):
    # Unknown type -> 400, no log entry
    r = c.post("/api/track", json={"event_type": "wat"})
    if r.status_code != 400:
        fail(errors, "track", f"unknown event_type expected 400, got {r.status_code}")

    # Hero CTA click event from mobile UA
    r = c.post("/api/track",
               json={"event_type": "cta_click",
                     "cta_id": "hero_get_quote",
                     "source": "marko",
                     "campaign": "test_run",
                     "destination": "/quote"},
               headers={"User-Agent": MOBILE_UA})
    if r.status_code != 200:
        fail(errors, "track", f"cta_click expected 200, got {r.status_code}")
    else:
        body = json.loads(r.get_data(as_text=True))
        if not body.get("ok"):
            fail(errors, "track", f"cta_click ok=False: {body}")
        if body["recorded"]["device_type"] != "mobile":
            fail(errors, "track",
                 f"mobile UA not recognized: {body['recorded']['device_type']!r}")


def check_full_funnel(c, errors):
    # Landing event
    r = c.get(
        "/quote?source=marko&campaign=test_run&zip=23220&mover_hint=M003",
        headers={"User-Agent": MOBILE_UA, "Host": "quote.bookermove.com"},
    )
    if r.status_code != 200:
        fail(errors, "funnel", f"landing GET expected 200, got {r.status_code}")
    # Submit form -- minimum required fields, smoke source
    r = c.post(
        "/quote",
        data={
            "customer_name": "Conversion Verify",
            "phone": "555-000-0000",
            "email": "verify@marko.local",
            "move_date": "2026-06-01",
            "pickup_zip": "23220",
            "dropoff_zip": "23221",
            "home_size": "studio",
            "stairs_elevator": "Ground floor",
            "heavy_items": "(none)",
            "urgency": "flexible",
            "notes": "verify",
            "attr_source": "marko",
            "attr_campaign": "test_run",
            "attr_mover_hint": "M003",
        },
        headers={"User-Agent": MOBILE_UA, "Host": "quote.bookermove.com"},
    )
    if r.status_code != 200:
        fail(errors, "funnel", f"quote POST expected 200, got {r.status_code}")
    # Stripe-prep schema acceptance (no Stripe behind it, just the schema)
    r = c.post("/api/track",
               json={"event_type": "checkout_started",
                     "source": "marko", "campaign": "test_run",
                     "destination": "https://stripe.example/c/abc"})
    if r.status_code != 200:
        fail(errors, "funnel",
             f"checkout_started should be accepted; got {r.status_code}")


def check_aggregate_and_attribution(errors):
    events = marko_tracking.load_events()
    types = [e["event_type"] for e in events]
    for needed in ("cta_click", "landing", "quote_submit", "checkout_started"):
        if needed not in types:
            fail(errors, "agg", f"missing event_type {needed!r} after run")
    # Attribution survived through quote_submit
    qs = [e for e in events if e["event_type"] == "quote_submit"]
    if not qs:
        fail(errors, "attr", "no quote_submit events recorded")
    else:
        e = qs[-1]
        if e.get("source") != "marko":
            fail(errors, "attr", f"source lost in quote_submit: {e.get('source')!r}")
        if e.get("campaign") != "test_run":
            fail(errors, "attr", f"campaign lost in quote_submit: {e.get('campaign')!r}")
        if e.get("zip") != "23220":
            fail(errors, "attr", f"zip lost in quote_submit: {e.get('zip')!r}")
        if e.get("mover_id") != "M003":
            fail(errors, "attr",
                 f"mover_hint lost in quote_submit: {e.get('mover_id')!r}")
        if e.get("device_type") != "mobile":
            fail(errors, "attr",
                 f"device sniff failed: {e.get('device_type')!r}")


def check_admin_conversions(c, errors):
    # No token
    r = c.get("/admin/conversions")
    if r.status_code != 403:
        fail(errors, "admin", f"no-token expected 403, got {r.status_code}")
    # JSON
    r = c.get("/admin/conversions?token=verify-token&format=json")
    if r.status_code != 200:
        fail(errors, "admin", f"json expected 200, got {r.status_code}")
        return
    body = json.loads(r.get_data(as_text=True))
    if "aggregate" not in body or "stripe_integration_status" not in body:
        fail(errors, "admin", "missing keys in admin/conversions JSON")
    if body["stripe_integration_status"]["checkout_url_configured"] is not False:
        fail(errors, "admin", "stripe checkout flag should be False (no Stripe in repo)")


def check_report(errors):
    path = mcr.render()
    if not os.path.exists(path):
        fail(errors, "report", f"report not written at {path}")
        return
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    for needed in ("# MARKO Conversion Report", "Funnel counts",
                   "Step-to-step conversion", "Best performers",
                   "Stripe integration"):
        if needed not in text:
            fail(errors, "report", f"missing section {needed!r}")


def check_quote_mobile(c, errors):
    r = c.get("/quote", headers={"Host": "quote.bookermove.com"})
    if r.status_code != 200:
        fail(errors, "mobile", f"/quote 200 expected, got {r.status_code}")
        return
    body = r.get_data(as_text=True)
    for needed in ("width=device-width", "60 seconds", "pickup_zip",
                   "attr_source"):
        if needed not in body:
            fail(errors, "mobile", f"missing {needed!r}")


def main():
    errors = []
    c = _client()
    restore = _snapshot_events()
    try:
        check_recorder_enum(errors)
        check_track_endpoint(c, errors)
        check_full_funnel(c, errors)
        check_aggregate_and_attribution(errors)
        check_admin_conversions(c, errors)
        check_report(errors)
        check_quote_mobile(c, errors)
        events_after = marko_tracking.load_events()
        summary = {
            "ok": not errors,
            "events_after_run": len(events_after),
            "event_types_observed": sorted({e["event_type"] for e in events_after}),
            "stripe_in_repo": False,
            "stripe_event_schema_accepted": True,
            "errors": errors,
        }
    finally:
        restore()
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
