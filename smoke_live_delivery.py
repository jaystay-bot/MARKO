"""Fire ONE live delivery smoke through Resend, redirected for safety.

Reads required env vars, refuses to run if anything is missing, and
prints a strict pass/fail report. No silent fallback to dry_run -- if
the env is incomplete the script exits non-zero and tells you exactly
what to set.

Required env:
  RESEND_API_KEY                 Resend API key
  MARKO_FROM_EMAIL               Verified Resend sender (e.g. leads@yourdomain.com)
  MARKO_MOVER_ALLOWLIST          Must contain "M001"
  MARKO_SMOKE_REDIRECT_TO        Inbox the smoke email actually lands in

Usage (PowerShell):
  $env:RESEND_API_KEY="re_..."
  $env:MARKO_FROM_EMAIL="leads@yourdomain.com"
  $env:MARKO_MOVER_ALLOWLIST="M001"
  $env:MARKO_SMOKE_REDIRECT_TO="supportbookermove@gmail.com"
  python smoke_live_delivery.py
"""
import json
import os
import sys

import routing


def fail(msg, payload=None):
    print(f"FAIL: {msg}", file=sys.stderr)
    if payload is not None:
        print(json.dumps(payload, indent=2), file=sys.stderr)
    sys.exit(2)


def main():
    # 1. Env preflight -- never proceed with a missing prerequisite.
    env = routing.env_status()
    domain = routing.from_email_domain_verified()
    missing = []
    if not env["resend_api_key_set"]:
        missing.append("RESEND_API_KEY")
    if not env["marko_from_email"]:
        missing.append("MARKO_FROM_EMAIL")
    if "M001" not in env["mover_allowlist"]:
        missing.append('MARKO_MOVER_ALLOWLIST (must include "M001")')
    if not env["smoke_redirect_to"]:
        missing.append("MARKO_SMOKE_REDIRECT_TO")
    if missing:
        fail("missing required env: " + ", ".join(missing),
             {"env": env, "from_email_domain": domain})

    if domain["status"] != "verified":
        fail(
            f"MARKO_FROM_EMAIL domain {domain.get('domain')!r} is "
            f"status={domain.get('status')!r} at Resend "
            "(must be 'verified' to send). " + str(domain.get("message")),
            {"env": env, "from_email_domain": domain},
        )

    # 2. Fire the smoke -- redirect-only, M001 target, force_live=True
    #    inside routing.smoke_send so dry_run cannot win silently.
    print("Preflight OK. Firing live smoke through Resend...")
    print(f"  From:           {env['marko_from_email']}")
    print(f"  Mover target:   M001 (info@allmysons.com)")
    print(f"  Redirected to:  {env['smoke_redirect_to']}")
    print("")

    result = routing.smoke_send(mover_id="M001")
    routing_record = result.get("routing") or {}
    delivery_mode = routing_record.get("delivery_mode")
    status = routing_record.get("status")
    email_result = routing_record.get("email_result") or {}
    message_id = email_result.get("id")
    provider_error = email_result.get("error")

    # 3. Pull the just-written delivery_log entry to confirm structured
    #    evidence is on disk (redirected=true, to_original preserved).
    try:
        log = routing.storage.read_json(routing.DELIVERY_LOG_FILE)
        last_delivery = (log.get("events") or [])[-1]
    except FileNotFoundError:
        last_delivery = None

    report = {
        "status": status,
        "delivery_mode": delivery_mode,
        "lead_id": (result.get("lead") or {}).get("lead_id"),
        "subject": routing_record.get("subject"),
        "message_id": message_id,
        "provider_error": provider_error,
        "block_reasons": routing_record.get("block_reasons"),
        "last_delivery_log_entry": last_delivery,
    }
    print(json.dumps(report, indent=2))
    print("")

    # 4. Hard pass/fail. PASS only if Resend accepted and redirected.
    if status != "routed":
        fail(f"routing status={status!r} (expected 'routed')", report)
    if delivery_mode != "live_redirected":
        fail(
            f"delivery_mode={delivery_mode!r} (expected 'live_redirected'); "
            "MARKO_SMOKE_REDIRECT_TO must be set",
            report,
        )
    if not message_id:
        fail("Resend returned no message id", report)
    if not last_delivery:
        fail("delivery_log.json has no entry", report)
    if not last_delivery.get("redirected"):
        fail("delivery_log.redirected != true", report)
    if last_delivery.get("to_original") != "info@allmysons.com":
        fail(
            f"delivery_log.to_original={last_delivery.get('to_original')!r} "
            "(expected info@allmysons.com)",
            report,
        )

    print("PASS -- Resend accepted the send, redirect honored, "
          "log evidence on disk.")
    print(f"  Resend message_id: {message_id}")
    print(f"  Recipient (used):  {last_delivery.get('to')}")
    print(f"  Recipient (orig):  {last_delivery.get('to_original')}")
    print(f"  Lead id:           {report['lead_id']}")
    print("Check the redirect inbox to confirm physical delivery.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
