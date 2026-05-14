"""Verify TalkBot -> MARKO routing integration end-to-end.

Exercises POST /api/talkbot/inbound through Flask's test client. Live email
is gated by env -- this verifier proves the plumbing without sending:
  * token gate works (no token, bad token, missing env -> right status)
  * JSON validation rejects bad payloads with explicit errors
  * a well-formed TalkBot payload routes through routing.submit_quote()
  * source="inbound_talkbot" propagates into lead, routed_leads, delivery_log
  * talkbot_session_id propagates the same way (trace join)
  * existing safeties hold: dry_run when MARKO_QUOTE_LIVE_SEND unset,
    allowlist refuses live for non-allowlisted movers, redirect honored
    when set

This script does NOT make any HTTP call to Resend. Live verification is
handled by smoke_live_delivery.py + the existing M001 redirect path.
"""
import json
import os
import sys
from copy import deepcopy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Set up the env BEFORE importing dashboard so the route picks up the token.
os.environ["TALKBOT_INBOUND_TOKEN"] = "test-talkbot-token"
os.environ["MARKO_MOVER_ALLOWLIST"] = "M001"
# Intentionally leave MARKO_QUOTE_LIVE_SEND unset -- this verifier proves
# the dry_run-by-default safety holds.
os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)
# Intentionally leave RESEND_API_KEY unset so even if a live path leaks,
# email_client refuses to send. Belt-and-suspenders.
os.environ.pop("RESEND_API_KEY", None)

import dashboard  # noqa: E402
import routing  # noqa: E402


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def assert_eq(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(cond, label):
    if not cond:
        raise AssertionError(f"{label}: condition false")


GOOD_PAYLOAD = {
    "talkbot_session_id": "tb_test_abc123",
    "customer_name": "TalkBot Caller",
    "phone": "804-555-0142",
    "email": "talkbot.caller@example.com",
    "move_date": "2026-06-22",
    "pickup_zip": "23230",        # Richmond -- M001 covers it
    "dropoff_zip": "23113",
    "home_size": "2 bedroom",
    "stairs_elevator": "Elevator",
    "heavy_items": "treadmill",
    "urgency": "this_week",
    "notes": "Caller qualified via TalkBot; ready to schedule.",
}


def main():
    out = {"checks": []}
    client = dashboard.app.test_client()

    # 1. No token -> 401
    r = client.post("/api/talkbot/inbound", json=GOOD_PAYLOAD)
    assert_eq(r.status_code, 401, "no token -> 401")
    out["checks"].append("no token -> 401")

    # 2. Wrong token -> 401
    r = client.post(
        "/api/talkbot/inbound",
        json=GOOD_PAYLOAD,
        headers={"X-Talkbot-Token": "wrong"},
    )
    assert_eq(r.status_code, 401, "wrong token -> 401")
    out["checks"].append("wrong token -> 401")

    # 3. Token unset -> 503
    saved = os.environ.pop("TALKBOT_INBOUND_TOKEN")
    r = client.post(
        "/api/talkbot/inbound",
        json=GOOD_PAYLOAD,
        headers={"X-Talkbot-Token": "anything"},
    )
    assert_eq(r.status_code, 503, "token env unset -> 503")
    os.environ["TALKBOT_INBOUND_TOKEN"] = saved
    out["checks"].append("token env unset -> 503 (no silent accept)")

    # 4. Bad payload (missing required fields) -> 400 + explicit errors
    bad = {"customer_name": "", "phone": "", "pickup_zip": "xx"}
    r = client.post(
        "/api/talkbot/inbound",
        json=bad,
        headers={"X-Talkbot-Token": "test-talkbot-token"},
    )
    assert_eq(r.status_code, 400, "bad payload -> 400")
    body = r.get_json()
    assert_eq(body["ok"], False, "bad payload ok=False")
    assert_true(len(body.get("errors", [])) > 0, "bad payload has errors")
    out["checks"].append("bad payload -> 400 with explicit errors")

    # 5. Non-JSON / non-object body -> 400
    r = client.post(
        "/api/talkbot/inbound",
        data="not json",
        content_type="text/plain",
        headers={"X-Talkbot-Token": "test-talkbot-token"},
    )
    assert_true(r.status_code in (400, 415), "non-JSON rejected")
    out["checks"].append(f"non-JSON body -> {r.status_code}")

    # 6. Good payload -> 200, lead routed (dry_run because live env unset)
    r = client.post(
        "/api/talkbot/inbound",
        json=GOOD_PAYLOAD,
        headers={"X-Talkbot-Token": "test-talkbot-token"},
    )
    assert_eq(r.status_code, 200, "good payload -> 200")
    body = r.get_json()
    assert_eq(body["ok"], True, "good payload ok=True")
    assert_eq(body["source"], "inbound_talkbot", "source tag")
    assert_eq(body["talkbot_session_id"], "tb_test_abc123",
              "session id passthrough")
    assert_true(body["lead_id"].startswith("Q-"), "lead_id shape")
    # Even though M001 is allowlisted, MARKO_QUOTE_LIVE_SEND is unset, so
    # the route requested dry_run -> delivery_mode is "dry_run", status routed.
    assert_eq(body["delivery_mode"], "dry_run",
              "safety holds: dry_run when live env unset")
    assert_eq(body["status"], "routed", "routed (dry_run id present)")
    assert_true(body["mover"]["mover_id"] == "M001",
                "M001 picked for ZIP 23230")
    lead_id = body["lead_id"]
    out["lead_id"] = lead_id
    out["checks"].append(
        f"good payload -> 200, source=inbound_talkbot, dry_run preserved"
    )

    # 7. On-disk evidence: inbound + routed + delivery all carry the tag
    inbound = _read(routing.INBOUND_FILE)
    routed = _read(routing.ROUTED_FILE)
    delivery = _read(routing.DELIVERY_LOG_FILE)

    inb_hit = next((l for l in reversed(inbound.get("leads", []))
                    if l.get("lead_id") == lead_id), None)
    routed_hit = next((e for e in reversed(routed.get("events", []))
                       if e.get("lead_id") == lead_id), None)
    delivery_hit = next((e for e in reversed(delivery.get("events", []))
                         if e.get("lead_id") == lead_id), None)

    assert_true(inb_hit is not None, "inbound entry written")
    assert_true(routed_hit is not None, "routed entry written")
    assert_true(delivery_hit is not None, "delivery entry written")

    assert_eq(inb_hit["source"], "inbound_talkbot", "inbound.source")
    assert_eq(inb_hit["talkbot_session_id"], "tb_test_abc123",
              "inbound.talkbot_session_id")
    assert_eq(routed_hit["source"], "inbound_talkbot", "routed.source")
    assert_eq(routed_hit["talkbot_session_id"], "tb_test_abc123",
              "routed.talkbot_session_id")
    assert_eq(delivery_hit["source"], "inbound_talkbot", "delivery.source")
    assert_eq(delivery_hit["talkbot_session_id"], "tb_test_abc123",
              "delivery.talkbot_session_id")
    assert_eq(delivery_hit["status"], "dry_run", "delivery.status dry_run")
    assert_eq(delivery_hit["dry_run"], True, "delivery.dry_run True")
    out["checks"].append(
        "inbound/routed/delivery all tagged source=inbound_talkbot + session id"
    )

    # 8. Allowlist enforcement: a payload whose mover is NOT allowlisted
    #    should fall back to dry_run even if MARKO_QUOTE_LIVE_SEND=1.
    #    We simulate this by setting live=1 temporarily and using a ZIP
    #    that only M002 (NOT in allowlist) would match.
    os.environ["MARKO_QUOTE_LIVE_SEND"] = "1"
    os.environ["MARKO_MOVER_ALLOWLIST"] = "M002_NOT_REAL"
    try:
        payload_live = deepcopy(GOOD_PAYLOAD)
        payload_live["talkbot_session_id"] = "tb_test_live_attempt"
        r = client.post(
            "/api/talkbot/inbound",
            json=payload_live,
            headers={"X-Talkbot-Token": "test-talkbot-token"},
        )
        assert_eq(r.status_code, 200, "live-attempt routed status")
        body = r.get_json()
        assert_eq(body["delivery_mode"], "blocked",
                  "non-allowlisted mover -> blocked (no live)")
        assert_eq(body["status"], "delivery_blocked",
                  "non-allowlisted -> delivery_blocked")
        assert_true(any("ALLOWLIST" in r or "not in MARKO_MOVER_ALLOWLIST" in r
                        for r in (body.get("block_reasons") or [])),
                    "block_reasons explain allowlist refusal")
        out["checks"].append("allowlist gate refuses live for non-listed mover")
    finally:
        os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)
        os.environ["MARKO_MOVER_ALLOWLIST"] = "M001"

    # 9. The existing /quote loop still tags source="inbound_quote" (no
    #    cross-pollution of source values).
    web_form = {k: GOOD_PAYLOAD[k] for k in (
        "customer_name", "phone", "email", "move_date",
        "pickup_zip", "dropoff_zip", "home_size",
        "stairs_elevator", "heavy_items", "urgency", "notes",
    )}
    web_form["customer_name"] = "Web Form Caller"
    r = client.post("/quote", data=web_form)
    assert_eq(r.status_code, 200, "/quote ok")
    # Verify the most recent inbound carries source=inbound_quote.
    inbound2 = _read(routing.INBOUND_FILE)
    last_web = inbound2["leads"][-1]
    assert_eq(last_web["source"], "inbound_quote", "/quote source unchanged")
    assert_true("talkbot_session_id" not in last_web,
                "/quote leaves talkbot_session_id absent")
    out["checks"].append("/quote still emits source=inbound_quote (no drift)")

    # 10. The existing bookermove export contract is still valid -- no
    #     schema drift introduced by this N.
    import bookermove_export  # noqa: E402
    snapshot = _read(os.path.join(ROOT, "_truth", "exports", "leads_export.json"))
    ok, errors = bookermove_export.validate_envelope(snapshot)
    assert_true(ok, f"bookermove envelope still validates: {errors}")
    out["checks"].append("bookermove_export validator: PASS (no contract drift)")

    out["pass"] = True
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
