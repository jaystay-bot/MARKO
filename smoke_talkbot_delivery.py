"""Fire ONE live TalkBot -> MARKO delivery smoke.

Identical safety posture to smoke_live_delivery.py, but the inbound path
exercises POST /api/talkbot/inbound (the new HTTP endpoint) rather than
calling routing.submit_quote() directly. This proves the full TalkBot
integration loop without exposing any new safety surface.

Required env:
  RESEND_API_KEY               Resend API key
  MARKO_FROM_EMAIL             Verified Resend sender (e.g. support@bookermove.com)
  MARKO_MOVER_ALLOWLIST        Must include "M001"
  MARKO_SMOKE_REDIRECT_TO      Inbox the smoke email lands in
  MARKO_QUOTE_LIVE_SEND        Must equal "1" for live routing on this path
  TALKBOT_INBOUND_TOKEN        Shared secret for X-Talkbot-Token header

Usage (PowerShell):
  $env:RESEND_API_KEY="re_..."
  $env:MARKO_FROM_EMAIL="support@bookermove.com"
  $env:MARKO_MOVER_ALLOWLIST="M001"
  $env:MARKO_SMOKE_REDIRECT_TO="supportbookermove@gmail.com"
  $env:MARKO_QUOTE_LIVE_SEND="1"
  $env:TALKBOT_INBOUND_TOKEN="any-strong-random-string"
  python smoke_talkbot_delivery.py
"""
import json
import os
import sys


def fail(msg, payload=None):
    print(f"FAIL: {msg}", file=sys.stderr)
    if payload is not None:
        print(json.dumps(payload, indent=2, default=str), file=sys.stderr)
    sys.exit(2)


def main():
    # 1. Env preflight -- never proceed with a missing prerequisite.
    missing = []
    if not (os.environ.get("RESEND_API_KEY") or "").strip():
        missing.append("RESEND_API_KEY")
    if not (os.environ.get("MARKO_FROM_EMAIL") or "").strip():
        missing.append("MARKO_FROM_EMAIL")
    if "M001" not in {
        p.strip()
        for p in (os.environ.get("MARKO_MOVER_ALLOWLIST") or "").split(",")
    }:
        missing.append('MARKO_MOVER_ALLOWLIST (must include "M001")')
    if not (os.environ.get("MARKO_SMOKE_REDIRECT_TO") or "").strip():
        missing.append("MARKO_SMOKE_REDIRECT_TO")
    if (os.environ.get("MARKO_QUOTE_LIVE_SEND") or "").strip() != "1":
        missing.append('MARKO_QUOTE_LIVE_SEND (must equal "1")')
    if not (os.environ.get("TALKBOT_INBOUND_TOKEN") or "").strip():
        missing.append("TALKBOT_INBOUND_TOKEN")
    if missing:
        fail("missing required env: " + ", ".join(missing))

    # Import lazy so env is set first.
    import dashboard  # noqa: F401  -- gives us the Flask app
    import routing

    # Confirm sender domain verified at Resend BEFORE we send.
    domain = routing.from_email_domain_verified()
    if domain.get("status") != "verified":
        fail(f"MARKO_FROM_EMAIL domain status={domain.get('status')!r}: "
             f"{domain.get('message')}", {"from_email_domain": domain})

    redirect_to = (os.environ.get("MARKO_SMOKE_REDIRECT_TO") or "").strip()
    talkbot_session_id = "tb_smoke_" + os.urandom(4).hex()

    payload = {
        "talkbot_session_id": talkbot_session_id,
        "customer_name": "TalkBot Smoke Test",
        "phone": "555-000-0000",
        "email": redirect_to,
        "move_date": "2026-06-30",
        "pickup_zip": "23230",   # Richmond -- M001 covers it
        "dropoff_zip": "23230",
        "home_size": "Studio",
        "stairs_elevator": "Ground floor",
        "heavy_items": "(smoke test - ignore)",
        "urgency": "flexible",
        "notes": (
            "TalkBot -> MARKO integration smoke. "
            "Redirected to the smoke inbox; ignore."
        ),
    }

    print("Preflight OK. Firing TalkBot integration smoke...")
    print(f"  From:           {os.environ['MARKO_FROM_EMAIL']}")
    print(f"  Endpoint:       POST /api/talkbot/inbound")
    print(f"  Mover target:   M001 (info@allmysons.com)")
    print(f"  Redirected to:  {redirect_to}")
    print(f"  Session id:     {talkbot_session_id}")
    print("")

    client = dashboard.app.test_client()
    r = client.post(
        "/api/talkbot/inbound",
        json=payload,
        headers={"X-Talkbot-Token": os.environ["TALKBOT_INBOUND_TOKEN"]},
    )
    body = r.get_json()
    if r.status_code != 200 or not body or not body.get("ok"):
        fail(f"endpoint refused (HTTP {r.status_code})", body)

    # Pull the freshest delivery_log entry for this lead and assert.
    try:
        log = routing.storage.read_json(routing.DELIVERY_LOG_FILE)
        last = next(
            (e for e in reversed(log.get("events", []))
             if e.get("lead_id") == body["lead_id"]),
            None,
        )
    except FileNotFoundError:
        last = None

    report = {
        "endpoint_status": r.status_code,
        "endpoint_body": body,
        "delivery_log_entry": last,
    }
    print(json.dumps(report, indent=2))
    print("")

    if not last:
        fail("delivery_log entry missing for this lead", report)
    if last.get("source") != "inbound_talkbot":
        fail(f"delivery.source = {last.get('source')!r}, expected 'inbound_talkbot'",
             report)
    if last.get("talkbot_session_id") != talkbot_session_id:
        fail("delivery.talkbot_session_id mismatch", report)
    if last.get("delivery_mode") != "live_redirected":
        fail(f"delivery_mode = {last.get('delivery_mode')!r}, "
             "expected 'live_redirected'", report)
    if not last.get("redirected"):
        fail("delivery.redirected != true", report)
    if last.get("to_original") != "info@allmysons.com":
        fail("to_original != info@allmysons.com (mover routing drifted)",
             report)
    if last.get("to") != redirect_to:
        fail("to != MARKO_SMOKE_REDIRECT_TO (redirect not honored)", report)
    if not last.get("message_id"):
        fail("Resend returned no message_id", report)
    if last.get("status") != "sent":
        fail(f"delivery.status = {last.get('status')!r}, expected 'sent'", report)

    print("PASS -- TalkBot payload routed through MARKO, Resend accepted, "
          "redirect honored, log evidence on disk.")
    print(f"  Resend message_id:    {last['message_id']}")
    print(f"  Lead id:              {body['lead_id']}")
    print(f"  TalkBot session id:   {last['talkbot_session_id']}")
    print(f"  Recipient (used):     {last['to']}")
    print(f"  Recipient (original): {last['to_original']}")
    print("Check the redirect inbox to confirm physical delivery.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
