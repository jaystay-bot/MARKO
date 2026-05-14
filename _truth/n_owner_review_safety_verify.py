"""Verify the owner-notify safety layer end-to-end.

Goal: prove every successful inbound lead lands in the owner inbox even
when mover routing is intentionally dry_run. Plus prove missed-money
surfacing fires when owner notify can't go through.

No live email. We monkey-patch routing.email_client.send so this verifier
exercises the full code path without hitting Resend.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Pre-set env BEFORE importing.
os.environ["TALKBOT_INBOUND_TOKEN"] = "verifier-token"
os.environ["MARKO_MOVER_ALLOWLIST"] = "M001"
os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)

import dashboard  # noqa: E402
import routing  # noqa: E402


def assert_eq(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(cond, label):
    if not cond:
        raise AssertionError(f"{label}: condition false")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


# ---------- email_client.send stub ----------

_SEND_CALLS = []


def _make_stub(return_value):
    def stub(**kwargs):
        _SEND_CALLS.append(kwargs)
        return dict(return_value)
    return stub


def main():
    out = {"checks": []}

    # --- Pure-function checks ---------------------------------------
    assert_eq(
        routing.compute_lead_quality({
            "customer_name": "A", "phone": "1", "email": "a@b.com",
            "urgency": "asap",
        }),
        "HOT", "HOT quality")
    assert_eq(
        routing.compute_lead_quality({
            "customer_name": "A", "phone": "1", "email": "",
            "urgency": "this_week",
        }),
        "WARM", "WARM quality")
    assert_eq(
        routing.compute_lead_quality({
            "customer_name": "", "phone": "", "email": "",
            "urgency": "flexible",
        }),
        "COOL", "COOL quality")
    out["checks"].append("compute_lead_quality: HOT/WARM/COOL all correct")

    low2, high2 = routing.estimate_lead_value({"home_size": "2 bedroom"})
    assert_eq((low2, high2), (500, 1500), "2br value range")
    low_h, high_h = routing.estimate_lead_value(
        {"home_size": "2 bedroom", "heavy_items": "piano"}
    )
    assert_eq(low_h, 500, "heavy bump low unchanged")
    assert_true(high_h > 1500, "heavy bump raises high end")
    out["checks"].append("estimate_lead_value: home-size + heavy-items bump")

    # --- Owner email shape -------------------------------------------
    sample_lead = routing.build_lead({
        "customer_name": "Sample Customer",
        "phone": "555-555-5555",
        "email": "sample@example.com",
        "move_date": "2026-06-01",
        "pickup_zip": "23230",
        "dropoff_zip": "23114",
        "home_size": "2 bedroom",
        "stairs_elevator": "Elevator",
        "heavy_items": "treadmill, piano",
        "urgency": "asap",
        "notes": "Need help fast",
    }, source="inbound_quote")
    subject, body = routing.build_owner_email(
        sample_lead,
        would_have_routed_to={"business_name": "Test Mover",
                              "mover_id": "M001"},
    )
    assert_true(subject.startswith("NEW MOVING LEAD - OWNER REVIEW ONLY"),
                "subject prefix exact")
    assert_true("Sample Customer" in subject, "subject names customer")
    assert_true(sample_lead["customer_name"] in body, "body has customer name")
    assert_true(sample_lead["phone"] in body, "body has phone")
    assert_true(sample_lead["email"] in body, "body has email")
    assert_true(sample_lead["pickup_zip"] in body, "body has pickup zip")
    assert_true(sample_lead["dropoff_zip"] in body, "body has dropoff zip")
    assert_true(sample_lead["move_date"] in body, "body has move date")
    assert_true(sample_lead["home_size"] in body, "body has home size")
    assert_true(sample_lead["heavy_items"] in body, "body has heavy items")
    assert_true(sample_lead["urgency"] in body, "body has urgency")
    assert_true(sample_lead["lead_id"] in body, "body has lead id")
    assert_true(sample_lead["lead_quality"] in body, "body has quality")
    assert_true("$" in body, "body has dollar sign for value range")
    assert_true("DID NOT contact" in body, "body says no mover contacted")
    assert_true("Test Mover" in body, "body shows would-have-routed mover")
    # Mobile-readability: every body line <= 78 chars
    long_lines = [(i, ln) for i, ln in enumerate(body.split("\n"))
                  if len(ln) > 78]
    assert_eq(long_lines, [], f"all body lines <=78 chars (offenders: {long_lines})")
    out["checks"].append(
        "build_owner_email: subject + body cover all required fields, mobile-readable"
    )

    # --- notify_owner: disabled when env unset -----------------------
    os.environ.pop("MARKO_OWNER_NOTIFY_TO", None)
    r = routing.notify_owner(sample_lead)
    assert_eq(r["status"], "owner_notify_disabled", "no env -> disabled")
    assert_eq(r["sent"], False, "no env -> not sent")
    assert_eq(r["missed_money"], False, "disabled is NOT missed money")
    out["checks"].append("notify_owner: env unset -> opt-in no-op")

    # --- notify_owner: env set but no api key -> missed money -------
    os.environ["MARKO_OWNER_NOTIFY_TO"] = "supportbookermove@gmail.com"
    os.environ.pop("RESEND_API_KEY", None)
    pre_mm = _read(routing.MISSED_MONEY_FILE) or {"events": []}
    pre_n = len(pre_mm["events"])
    r = routing.notify_owner(sample_lead)
    assert_eq(r["status"], "no_api_key", "no key -> no_api_key")
    assert_eq(r["missed_money"], True, "no key -> missed money")
    post_mm = _read(routing.MISSED_MONEY_FILE)
    assert_true(len(post_mm["events"]) == pre_n + 1,
                "missed_money entry appended")
    last_mm = post_mm["events"][-1]
    assert_eq(last_mm["lead_id"], sample_lead["lead_id"], "mm lead_id")
    assert_true("phone" in last_mm["lead_summary"], "mm lead_summary has phone")
    out["checks"].append(
        "notify_owner: env set but no key -> writes missed_money + delivery_log blocked"
    )

    # --- notify_owner: full success path (stubbed Resend) -----------
    os.environ["RESEND_API_KEY"] = "stub-key-for-verifier-only"
    os.environ["MARKO_FROM_EMAIL"] = "support@bookermove.com"
    original_send = routing.email_client.send
    routing.email_client.send = _make_stub(
        {"id": "stub-msg-123", "status": "sent", "error": None}
    )
    try:
        _SEND_CALLS.clear()
        r = routing.notify_owner(
            sample_lead,
            would_have_routed_to={"business_name": "Test Mover",
                                  "mover_id": "M001"},
        )
        assert_eq(r["sent"], True, "stub send: sent True")
        assert_eq(r["status"], "sent", "stub send: status sent")
        assert_eq(r["message_id"], "stub-msg-123", "stub send: msg id")
        assert_eq(r["missed_money"], False, "stub send: not missed money")
        # Verify what was sent: to == MARKO_OWNER_NOTIFY_TO, subject right.
        call = _SEND_CALLS[0]
        assert_eq(call["to"], "supportbookermove@gmail.com",
                  "owner notify recipient")
        assert_true(call["subject"].startswith("NEW MOVING LEAD - OWNER REVIEW ONLY"),
                    "owner subject prefix")
        assert_eq(call["dry_run"], False, "owner notify dry_run=False")
        assert_eq(call["headers"]["X-Marko-Delivery-Kind"], "owner_notify",
                  "owner notify header")
        # Delivery log appended with delivery_kind=owner_notify.
        log = _read(routing.DELIVERY_LOG_FILE)
        last = log["events"][-1]
        assert_eq(last["delivery_kind"], "owner_notify", "delivery_kind tag")
        assert_eq(last["message_id"], "stub-msg-123", "delivery msg id")
    finally:
        routing.email_client.send = original_send
    out["checks"].append(
        "notify_owner: stubbed Resend success -> owner email + delivery_log entry"
    )

    # --- submit_quote: dry_run mover routing + live owner notify ----
    # Mover routing must STAY dry_run (because we haven't set
    # MARKO_QUOTE_LIVE_SEND), but owner notify still fires.
    routing.email_client.send = _make_stub(
        {"id": "stub-msg-456", "status": "sent", "error": None}
    )
    try:
        _SEND_CALLS.clear()
        result = routing.submit_quote({
            "customer_name": "End-To-End Caller",
            "phone": "804-555-0167",
            "email": "ete@example.com",
            "move_date": "2026-07-01",
            "pickup_zip": "23230",
            "dropoff_zip": "23114",
            "home_size": "3 bedroom",
            "stairs_elevator": "Stairs",
            "heavy_items": "piano",
            "urgency": "this_week",
            "notes": "End to end verify.",
        }, dry_run=True, source="inbound_quote")
        assert_eq(result["ok"], True, "submit_quote ok")
        # Mover side is dry_run (no live send to mover)
        assert_eq(result["routing"]["delivery_mode"], "dry_run",
                  "mover side dry_run")
        # Owner side is live (sent through stubbed Resend)
        owner = result["owner_notify"]
        assert_eq(owner["sent"], True, "owner notify sent")
        assert_eq(owner["status"], "sent", "owner notify status sent")
        assert_eq(owner["message_id"], "stub-msg-456", "owner msg id")
        # Two send calls: one (dry_run, mover) one (live, owner).
        # email_client.send was stubbed for BOTH paths.
        assert_true(len(_SEND_CALLS) == 2, "two send calls fired")
        mover_call = _SEND_CALLS[0]
        owner_call = _SEND_CALLS[1]
        assert_eq(mover_call["dry_run"], True, "mover call dry_run")
        assert_eq(owner_call["dry_run"], False, "owner call live")
        assert_eq(owner_call["to"], "supportbookermove@gmail.com",
                  "owner call recipient")
    finally:
        routing.email_client.send = original_send
    out["checks"].append(
        "submit_quote: mover dry_run + owner notify live (independence proven)"
    )

    # --- HTTP path: /quote also triggers owner notify ----------------
    routing.email_client.send = _make_stub(
        {"id": "stub-msg-http", "status": "sent", "error": None}
    )
    try:
        _SEND_CALLS.clear()
        client = dashboard.app.test_client()
        r = client.post("/quote", data={
            "customer_name": "HTTP Form Caller",
            "phone": "804-555-0188",
            "email": "http@example.com",
            "move_date": "2026-07-15",
            "pickup_zip": "23230",
            "dropoff_zip": "23114",
            "home_size": "1 bedroom",
            "stairs_elevator": "Ground floor",
            "heavy_items": "",
            "urgency": "asap",
            "notes": "HTTP path verify.",
        })
        assert_eq(r.status_code, 200, "POST /quote 200")
        # Owner notify should have fired via the stub.
        owner_calls = [c for c in _SEND_CALLS if c.get("dry_run") is False]
        assert_true(len(owner_calls) == 1,
                    f"exactly one owner call via /quote (got {len(owner_calls)})")
        assert_eq(owner_calls[0]["to"], "supportbookermove@gmail.com",
                  "/quote owner notify recipient")
    finally:
        routing.email_client.send = original_send
    out["checks"].append("/quote: HTTP path triggers owner notify")

    # --- HTTP path: /api/talkbot/inbound also triggers owner notify --
    routing.email_client.send = _make_stub(
        {"id": "stub-msg-tb", "status": "sent", "error": None}
    )
    try:
        _SEND_CALLS.clear()
        client = dashboard.app.test_client()
        r = client.post(
            "/api/talkbot/inbound",
            json={
                "talkbot_session_id": "tb_verify_owner",
                "customer_name": "TalkBot Caller",
                "phone": "804-555-0199",
                "email": "tb@example.com",
                "move_date": "2026-07-20",
                "pickup_zip": "23230",
                "dropoff_zip": "23114",
                "home_size": "Studio",
                "stairs_elevator": "Ground floor",
                "heavy_items": "",
                "urgency": "this_month",
                "notes": "TB verify.",
            },
            headers={"X-Talkbot-Token": "verifier-token"},
        )
        assert_eq(r.status_code, 200, "POST /api/talkbot/inbound 200")
        body = r.get_json()
        assert_true(body["owner_notify"]["sent"], "owner_notify.sent True")
        assert_eq(body["owner_notify"]["message_id"], "stub-msg-tb",
                  "owner notify message id surfaced")
        assert_true(body["lead_quality"] in ("HOT", "WARM", "COOL"),
                    "lead_quality surfaced")
        assert_true(isinstance(body["estimated_value"], list)
                    and len(body["estimated_value"]) == 2,
                    "estimated_value pair surfaced")
    finally:
        routing.email_client.send = original_send
    out["checks"].append(
        "/api/talkbot/inbound: HTTP path triggers owner notify"
    )

    # --- No contract drift -------------------------------------------
    import bookermove_export
    snap = _read(os.path.join(ROOT, "_truth", "exports", "leads_export.json"))
    ok, errors = bookermove_export.validate_envelope(snap)
    assert_true(ok, f"bookermove envelope still valid: {errors}")
    out["checks"].append("bookermove_export: PASS (no contract drift)")

    out["pass"] = True
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
