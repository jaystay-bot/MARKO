"""Verify the live-delivery environment is ready, without sending anything.

Prints a human-readable readiness report plus a JSON blob. Exit code 0
means the env is wired for a live smoke. Non-zero means at least one
prerequisite is missing -- the offending line is highlighted.

This script never sends email. It only:
  1. checks env vars (presence only -- never prints the API key)
  2. hits Resend /domains and reports MARKO_FROM_EMAIL's domain status
  3. confirms the buyer registry has at least one allowlisted mover

Usage:
  python verify_resend_env.py
"""
import json
import sys

import routing


def main():
    env = routing.env_status()
    domain = routing.from_email_domain_verified()
    movers = routing.load_movers()
    allowlist = set(env["mover_allowlist"])
    allowlisted_movers = [m["mover_id"] for m in movers
                          if m.get("mover_id") in allowlist]

    blockers = []
    if not env["resend_api_key_set"]:
        blockers.append("RESEND_API_KEY is not set")
    if not env["marko_from_email"]:
        blockers.append("MARKO_FROM_EMAIL is not set")
    elif domain["status"] != "verified":
        blockers.append(
            f"MARKO_FROM_EMAIL domain status = {domain['status']!r} "
            f"({domain['message']})"
        )
    if not allowlist:
        blockers.append(
            "MARKO_MOVER_ALLOWLIST is empty -- no mover may receive live email"
        )
    elif not allowlisted_movers:
        blockers.append(
            f"MARKO_MOVER_ALLOWLIST values {sorted(allowlist)} "
            "match no mover in movers.json"
        )
    if not env["admin_token_set"]:
        blockers.append(
            "ADMIN_TOKEN is not set -- /admin/delivery_smoke will refuse"
        )

    report = {
        "env": env,
        "from_email_domain": domain,
        "registered_mover_count": len(movers),
        "allowlisted_movers_present": allowlisted_movers,
        "blockers": blockers,
        "ready_for_live_smoke": not blockers,
    }
    print(json.dumps(report, indent=2))
    print("")
    if blockers:
        print(f"NOT READY -- {len(blockers)} blocker(s):", file=sys.stderr)
        for b in blockers:
            print(f"  - {b}", file=sys.stderr)
        sys.exit(1)
    print("READY -- live smoke is safe to fire.")
    print("Trigger with:")
    print("  POST /admin/delivery_smoke?token=$ADMIN_TOKEN&mover_id=M001")


if __name__ == "__main__":
    main()
