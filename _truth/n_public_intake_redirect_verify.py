"""TRUTH verifier for the public-intake redirect.

Customers hitting quote.bookermove.com must land on the moving-quote form,
not the operator dashboard. Operator hosts (marko-teal.vercel.app, localhost,
etc.) must keep showing the dashboard. Verified entirely via Flask's test
client so this runs anywhere -- no live HTTP required.

Cases:
  1. Host = quote.bookermove.com         -> 302 to /quote
  2. Host via X-Forwarded-Host (Vercel)  -> 302 to /quote
  3. Host = www.quote.bookermove.com     -> 302 to /quote (suffix match)
  4. Host = marko-teal.vercel.app        -> 200 dashboard (no redirect)
  5. Host = localhost                    -> 200 dashboard
  6. ?intake=1 on any host               -> 302 to /quote (operator smoke)
  7. /quote GET on intake host           -> 200 public form, no operator UI
  8. MARKO_PUBLIC_INTAKE_HOSTS override  -> custom host redirects
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

import dashboard  # noqa: E402


OPERATOR_MARKERS = (
    "MARKO", "campaigns", "Call Today", "leads.json", "Dashboard",
)
PUBLIC_FORM_MARKERS = (
    "Get your moving quote", "pickup_zip", "customer_name", "move_date",
)


def _client():
    dashboard.app.testing = True
    return dashboard.app.test_client()


def _no_operator_chrome(body):
    """Public form must not leak operator-only strings."""
    lc = body.lower()
    # The /quote template has its own minimal chrome. We just need to make
    # sure it doesn't accidentally render the operator dashboard partials.
    bad = []
    for needle in ("campaigns.json", "call today", "operator", "dashboard"):
        if needle in lc:
            bad.append(needle)
    return bad


def run():
    errors = []
    cases = []
    c = _client()

    # 1) Apex intake host
    r = c.get("/", headers={"Host": "quote.bookermove.com"})
    cases.append(("apex-host root", r.status_code, r.headers.get("Location")))
    if r.status_code != 302 or r.headers.get("Location") not in ("/quote",
                                                                 "http://quote.bookermove.com/quote"):
        errors.append(f"apex root: expected 302->/quote, got {r.status_code} {r.headers.get('Location')!r}")

    # 2) X-Forwarded-Host (Vercel custom-domain proxy)
    r = c.get("/", headers={"Host": "marko-teal.vercel.app",
                            "X-Forwarded-Host": "quote.bookermove.com"})
    cases.append(("x-forwarded-host root", r.status_code, r.headers.get("Location")))
    if r.status_code != 302:
        errors.append(
            f"x-forwarded-host root: expected 302, got {r.status_code}"
        )

    # 3) www subdomain suffix match
    r = c.get("/", headers={"Host": "www.quote.bookermove.com"})
    cases.append(("www subdomain root", r.status_code, r.headers.get("Location")))
    if r.status_code != 302:
        errors.append(
            f"www subdomain root: expected 302, got {r.status_code}"
        )

    # 4) Operator host should NOT redirect
    r = c.get("/", headers={"Host": "marko-teal.vercel.app"})
    cases.append(("operator root", r.status_code, None))
    if r.status_code != 200:
        errors.append(
            f"operator root: expected 200, got {r.status_code}"
        )

    # 5) Localhost = operator
    r = c.get("/", headers={"Host": "localhost:5000"})
    cases.append(("localhost root", r.status_code, None))
    if r.status_code != 200:
        errors.append(f"localhost root: expected 200, got {r.status_code}")

    # 6) ?intake=1 escape hatch from operator host
    r = c.get("/?intake=1", headers={"Host": "marko-teal.vercel.app"})
    cases.append(("intake-flag root", r.status_code, r.headers.get("Location")))
    if r.status_code != 302:
        errors.append(
            f"intake-flag root: expected 302, got {r.status_code}"
        )

    # 7) /quote on the public host renders the customer form, no operator UI
    r = c.get("/quote", headers={"Host": "quote.bookermove.com"})
    body = r.get_data(as_text=True)
    cases.append(("/quote GET", r.status_code, len(body)))
    if r.status_code != 200:
        errors.append(f"/quote GET: expected 200, got {r.status_code}")
    for needle in PUBLIC_FORM_MARKERS:
        if needle not in body:
            errors.append(f"/quote missing form marker: {needle!r}")
    leaked = _no_operator_chrome(body)
    if leaked:
        errors.append(f"/quote leaked operator markers: {leaked}")

    # 8) Env override picks up a custom host
    os.environ["MARKO_PUBLIC_INTAKE_HOSTS"] = "intake.example.com"
    try:
        r = c.get("/", headers={"Host": "intake.example.com"})
        cases.append(("env override root", r.status_code, r.headers.get("Location")))
        if r.status_code != 302:
            errors.append(
                f"env override root: expected 302, got {r.status_code}"
            )
        # And the original default should NOT redirect when overridden
        r = c.get("/", headers={"Host": "quote.bookermove.com"})
        cases.append(("override-replaces-default root", r.status_code, None))
        if r.status_code != 200:
            errors.append(
                "override-replaces-default root: expected 200 (env override "
                f"should fully replace defaults), got {r.status_code}"
            )
    finally:
        del os.environ["MARKO_PUBLIC_INTAKE_HOSTS"]

    summary = {
        "ok": not errors,
        "cases": cases,
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(run())
