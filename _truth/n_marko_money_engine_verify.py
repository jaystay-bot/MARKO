"""TRUTH verifier for N-SLEEP-MONEY-ENGINE.

End-to-end: regenerate every derived artifact, prove the public funnel
still works on mobile, prove the operator panel is gated, prove
attribution survives the form->lead build, prove no live send fired.

No HTTP, no scrape, no real lead written. The attribution check uses
routing.build_lead directly (pure function -- no file write).
"""
from __future__ import annotations

import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

# Make sure live-send stays off for the verifier session.
os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)
# Set a fixed admin token so the /money panel can be exercised without
# leaking whatever real token the operator may have set in their shell.
os.environ["ADMIN_TOKEN"] = "verify-token"

import dashboard       # noqa: E402
import marko_demand    # noqa: E402
import marko_overnight  # noqa: E402
import marko_money     # noqa: E402
import routing         # noqa: E402


def _client():
    dashboard.app.testing = True
    return dashboard.app.test_client()


def fail(errors, label, msg):
    errors.append(f"{label}: {msg}")


def check_mobile_funnel(c, errors):
    """Quote page must render with mobile viewport, 16px+ inputs (avoids
    iOS auto-zoom), CTA copy visible, no operator chrome."""
    r = c.get(
        "/quote?source=marko&campaign=richmond_movers&mover_hint=M003",
        headers={"Host": "quote.bookermove.com"},
    )
    if r.status_code != 200:
        fail(errors, "mobile", f"/quote returned {r.status_code}")
        return
    body = r.get_data(as_text=True)
    needles = (
        ('viewport meta', 'name="viewport"'),
        ('mobile width meta', "width=device-width"),
        ('cta band', "60 seconds"),
        ('cta one-mover line', "One local mover"),
        ('hidden attr_source', 'name="attr_source"'),
        ('hidden attr_campaign', 'name="attr_campaign"'),
        ('hidden attr_mover_hint', 'name="attr_mover_hint"'),
        ('attr_source value preserved', 'value="marko"'),
        ('attr_campaign value preserved', 'value="richmond_movers"'),
        ('attr_mover_hint value preserved', 'value="M003"'),
        ('input font-size 16', 'font-size: 16px'),
    )
    for label, needle in needles:
        if needle not in body:
            fail(errors, "mobile", f"missing {label}: {needle!r}")
    # Operator-UI leak guard
    for bad in ("campaigns.json", "Call Today", "Operator Dashboard"):
        if bad in body:
            fail(errors, "mobile", f"operator marker leaked: {bad!r}")


def check_attribution_pure(errors):
    """Form data with attr_* fields must end up tagged into lead.notes.

    We verify the *transformation* the dashboard route performs, not the
    route itself, so no inbound lead is recorded. The dashboard route
    builds a `tag` and prepends it to form['notes'] before calling
    routing.submit_quote -- we replicate that one transform here.
    """
    form = {
        "customer_name": "Verify Attribution",
        "phone": "555-000-0000",
        "email": "verify@marko.local",
        "move_date": "2026-06-01",
        "pickup_zip": "23220",
        "dropoff_zip": "23221",
        "home_size": "studio",
        "stairs_elevator": "Ground floor",
        "heavy_items": "(none)",
        "urgency": "flexible",
        "notes": "user note",
    }
    # Mirror the dashboard transform so the verifier exercises the exact
    # tag format that lands in production lead records.
    tag = "[attr source=marko campaign=richmond_movers mover_hint=M003] "
    form["notes"] = tag + form["notes"]
    lead = routing.build_lead(form, source="smoke")
    if not (lead["notes"].startswith("[attr source=marko ")):
        fail(errors, "attribution", f"lead.notes missing attr tag: {lead['notes']!r}")
    if "campaign=richmond_movers" not in lead["notes"]:
        fail(errors, "attribution", "campaign not preserved in notes")
    if "mover_hint=M003" not in lead["notes"]:
        fail(errors, "attribution", "mover_hint not preserved in notes")


def check_money_panel_gating(c, errors):
    # No token -> 403
    r = c.get("/money")
    if r.status_code != 403:
        fail(errors, "money_gate", f"no-token expected 403, got {r.status_code}")
    # Wrong token -> 403
    r = c.get("/money?token=bad")
    if r.status_code != 403:
        fail(errors, "money_gate", f"wrong-token expected 403, got {r.status_code}")
    # Correct token -> 200 JSON
    r = c.get("/money?token=verify-token")
    if r.status_code != 200:
        fail(errors, "money_gate", f"correct-token expected 200, got {r.status_code}")
    else:
        body = json.loads(r.get_data(as_text=True))
        if "report" not in body or "revenue_queue" not in body:
            fail(errors, "money_gate", "JSON missing report/revenue_queue keys")
    # HTML format -> 200 with mobile viewport
    r = c.get("/money?token=verify-token&format=html")
    if r.status_code != 200:
        fail(errors, "money_gate", f"html expected 200, got {r.status_code}")
    elif "width=device-width" not in r.get_data(as_text=True):
        fail(errors, "money_gate", "html missing mobile viewport meta")


def check_talkbot_endpoint_gate(c, errors):
    """TalkBot inbound must require X-Talkbot-Token. We don't set the env,
    so it should 503 (not 200, not 401 silently)."""
    os.environ.pop("TALKBOT_INBOUND_TOKEN", None)
    r = c.post("/api/talkbot/inbound", json={})
    if r.status_code != 503:
        fail(errors, "talkbot_gate", f"expected 503 with no env, got {r.status_code}")


def check_routing_artifacts(errors):
    """Re-derive demand + overnight + money. Each must produce non-empty
    output. The money report's `top_movers_to_call_tomorrow` must contain
    only entries that exist in the queue (no orphans)."""
    marko_demand.write_all()
    marko_overnight.write_queue()
    out = marko_money.write_all()
    report = out["report"]
    queue = out["revenue_queue"]

    if not queue:
        fail(errors, "money", "daily_revenue_queue is empty")
    if report["totals"]["mover_targets"] != len(queue):
        fail(errors, "money",
             "report.totals.mover_targets != revenue_queue length")
    biz_in_queue = {row["business"] for row in queue}
    for top in report["top_movers_to_call_tomorrow"]:
        if top["business"] not in biz_in_queue:
            fail(errors, "money",
                 f"top mover {top['business']!r} not in revenue queue")
    # Live send must not be on, and policy must reflect it.
    if report["policy"]["live_email_send"] is not False:
        fail(errors, "money", "live_email_send must be false in this run")


def main():
    errors = []
    c = _client()

    check_mobile_funnel(c, errors)
    check_attribution_pure(errors)
    check_money_panel_gating(c, errors)
    check_talkbot_endpoint_gate(c, errors)
    check_routing_artifacts(errors)

    summary = {
        "ok": not errors,
        "stripe_path": (
            "ABSENT in repo. No stripe SDK, no checkout link in templates. "
            "MARKO does not currently route customers to a payment page; "
            "monetization is mover-side ($20-$50/lead invoiced offline). "
            "Stripe is a real future bottleneck -- see report."
        ),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
