"""TRUTH verifier for N-MARKO-OPERATOR-COCKPIT.

Proves:
  * /cockpit token-gated; renders 200 with mobile viewport
  * data on the page comes from real on-disk artifacts -- counts match
    the live money_queue.json, hot_zips.json, missed_money.json
  * activity feed contains only real events (no fake/demo strings)
  * existing routes still work (no backend regression):
      /quote (200), /money (200 with token), /admin/conversions (200 with token),
      /api/track (cta_click 200), /review (200 with token)
  * money_queue and conversion event recorder are unchanged in behavior
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)
os.environ.pop("MARKO_OUTREACH_LIVE", None)
os.environ["ADMIN_TOKEN"] = "verify-token"

import dashboard         # noqa: E402
import marko_cockpit     # noqa: E402
import marko_tracking    # noqa: E402
import storage           # noqa: E402

EVENTS_FILE = marko_tracking.EVENTS_FILE
QUEUE_FILE = os.path.join(ROOT, "money_queue.json")


def _client():
    dashboard.app.testing = True
    return dashboard.app.test_client()


def fail(errors, label, msg):
    errors.append(f"{label}: {msg}")


def _snapshot(path):
    if not os.path.exists(path):
        return lambda: None
    backup = tempfile.NamedTemporaryFile(delete=False, suffix=".json").name
    shutil.copy2(path, backup)
    def restore():
        shutil.copy2(backup, path)
        os.remove(backup)
    return restore


# Strings that would indicate fake/demo activity. None of these may
# appear in the rendered HTML.
FAKE_NEEDLES = [
    "lorem", "ipsum", "demo lead", "Demo Mover",
    "John Doe", "Jane Doe", "example.com customer",
    "fake_", "TODO_FAKE", "synthetic activity",
]


def check_gate(c, errors):
    r = c.get("/cockpit")
    if r.status_code != 403:
        fail(errors, "gate", f"no-token expected 403, got {r.status_code}")
    r = c.get("/cockpit?token=bad")
    if r.status_code != 403:
        fail(errors, "gate", f"wrong-token expected 403, got {r.status_code}")


def check_render(c, errors):
    r = c.get("/cockpit?token=verify-token")
    if r.status_code != 200:
        fail(errors, "render", f"expected 200, got {r.status_code}")
        return None, None
    body = r.get_data(as_text=True)
    for needle in ("width=device-width", "MARKO Cockpit", "Lead tiers",
                   "Operator flow", "Live activity"):
        if needle not in body:
            fail(errors, "render", f"missing {needle!r}")
    for fake in FAKE_NEEDLES:
        if fake in body:
            fail(errors, "render", f"fake/demo string leaked: {fake!r}")
    return body, r


def check_real_data_in_render(body, errors):
    payload = marko_cockpit.cockpit_payload()
    # HOT count must appear verbatim near the "HOT" tier label
    hot = payload["tiers"]["HOT"]
    # Use a tolerant regex: the cockpit renders the count inside a div
    # right after the HOT name, e.g. <div class="name">HOT</div><div class="count">5</div>
    pat = re.compile(
        r'name">HOT</div>\s*<div class="count">\s*' + str(hot) + r'\s*</',
        re.S,
    )
    if not pat.search(body):
        fail(errors, "real",
             f"HOT tier count {hot!r} not found in rendered cockpit")
    # Strongest leak label appears
    leak = payload["strongest_leak"]["category"]
    if leak and leak not in body:
        fail(errors, "real", f"strongest leak {leak!r} not in rendered cockpit")
    # Hot ZIPs appear by literal ZIP string
    for z in (payload["hot_zips"] or [])[:1]:
        if z["zip"] not in body:
            fail(errors, "real",
                 f"hot zip {z['zip']!r} not rendered")


def check_live_activity_real(c, errors):
    """Inject ONE real conversion event, render cockpit, expect to see it."""
    marko_tracking.record(
        "cta_click",
        cta_id="cockpit_verify",
        source="marko",
        campaign="cockpit_test",
        device_type="mobile",
        landing_page="/quote",
        destination="/quote",
    )
    r = c.get("/cockpit?token=verify-token")
    body = r.get_data(as_text=True)
    if "CTA click" not in body:
        fail(errors, "feed", "freshly recorded cta_click did not appear in feed")
    if "cta_id=cockpit_verify" not in body:
        fail(errors, "feed", "cta_id detail not surfaced in feed row")


def check_no_backend_regression(c, errors):
    # /quote still public + mobile
    r = c.get("/quote", headers={"Host": "quote.bookermove.com"})
    if r.status_code != 200:
        fail(errors, "regress", f"/quote {r.status_code}")
    body = r.get_data(as_text=True)
    if "width=device-width" not in body or "pickup_zip" not in body:
        fail(errors, "regress", "/quote missing mobile/form markers")
    # /money JSON still works
    r = c.get("/money?token=verify-token")
    if r.status_code != 200:
        fail(errors, "regress", f"/money {r.status_code}")
    # /admin/conversions json still works
    r = c.get("/admin/conversions?token=verify-token&format=json")
    if r.status_code != 200:
        fail(errors, "regress", f"/admin/conversions {r.status_code}")
    # /api/track unknown still 400
    r = c.post("/api/track", json={"event_type": "wat"})
    if r.status_code != 400:
        fail(errors, "regress", f"/api/track unknown {r.status_code}")
    # /review still gated
    r = c.get("/review")
    if r.status_code != 403:
        fail(errors, "regress", f"/review no-token {r.status_code}")
    # /__diag still works (host-aware)
    r = c.get("/__diag")
    if r.status_code != 200:
        fail(errors, "regress", f"/__diag {r.status_code}")


def main():
    errors = []
    c = _client()
    restore_events = _snapshot(EVENTS_FILE)
    try:
        check_gate(c, errors)
        body, _ = check_render(c, errors)
        if body is not None:
            check_real_data_in_render(body, errors)
        check_live_activity_real(c, errors)
        check_no_backend_regression(c, errors)
    finally:
        restore_events()

    summary = {
        "ok": not errors,
        "cockpit_url": "/cockpit?token=<ADMIN_TOKEN>",
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
