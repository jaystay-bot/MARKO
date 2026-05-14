"""Verify the inbound-demand routing loop end-to-end.

Asserts the smallest meaningful loop:
  1. movers.json loads and validates basic shape
  2. an intake form posts and validates
  3. select_mover picks a real Richmond mover by ZIP
  4. routing produces a real email payload (dry-run, but real fields)
  5. inbound_leads.json / routed_leads.json / delivery_log.json all grow
  6. the BookerMove export validator still validates the existing
     leads_export.json (no contract drift introduced)
  7. a no-match ZIP yields status="no_match", not a fake routed event

No network. No real email send. No mutation of leads.json. Audit-only
files (inbound_leads.json / routed_leads.json / delivery_log.json) are
intentionally appended to -- they're the audit trail this build produces.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import routing  # noqa: E402
import bookermove_export  # noqa: E402


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def assert_eq(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(cond, label):
    if not cond:
        raise AssertionError(f"{label}: condition false")


def main():
    out = {"checks": []}

    # 1. movers.json
    movers_path = os.path.join(ROOT, "movers.json")
    movers = _read(movers_path)["movers"]
    assert_true(len(movers) >= 3, "registry must have 3+ movers")
    for m in movers:
        for f in ("business_name", "email", "cities_served", "zip_codes",
                  "exclusive", "active"):
            assert_true(f in m, f"mover {m.get('mover_id')} missing {f}")
        assert_true("@" in m["email"], f"mover {m.get('mover_id')} email shape")
    out["mover_count"] = len(movers)
    out["checks"].append("movers.json: 3+ real movers loaded")

    # 2. submit a real Richmond intake (ZIP 23230 — multiple movers serve it)
    form_richmond = {
        "customer_name": "Audit Tester",
        "phone": "804-555-0123",
        "email": "audit@example.com",
        "move_date": "2026-06-15",
        "pickup_zip": "23230",
        "dropoff_zip": "23113",
        "home_size": "2 bedroom",
        "stairs_elevator": "Elevator",
        "heavy_items": "upright piano",
        "urgency": "this_week",
        "notes": "Audit run; do not contact.",
    }
    result = routing.submit_quote(form_richmond, dry_run=True)
    assert_true(result["ok"], f"submit_quote ok=False: {result['errors']}")
    lead = result["lead"]
    routing_record = result["routing"]
    assert_eq(lead["source"], "inbound_quote", "lead.source")
    assert_true(lead["lead_id"].startswith("Q-"), "lead.lead_id prefix")
    assert_true(bool(lead["submitted_at"]), "lead.submitted_at set")
    out["checks"].append(f"intake validated, lead_id={lead['lead_id']}")

    # 3. mover selection produced a real Richmond mover
    assert_true(routing_record["mover"] is not None, "no mover selected for 23230")
    selected = routing_record["mover"]
    assert_true(selected["mover_id"].startswith("M"), "mover_id shape")
    assert_true("@" in selected["email"], "selected mover email shape")
    out["selected_mover"] = selected
    out["checks"].append(
        f"ZIP 23230 -> {selected['business_name']} <{selected['email']}>"
    )

    # 4. email payload real (subject + body present, spam-safer format)
    email_result = routing_record["email_result"]
    assert_eq(email_result["status"], "dry_run", "dry_run status")
    assert_true(routing_record["subject"], "subject present")
    # Subject is plain business now -- no bracketed tracker tags.
    assert_true("[Lead" not in routing_record["subject"], "subject is bot-free")
    assert_true(form_richmond["customer_name"] in routing_record["subject"],
                "subject names the customer")
    assert_true(routing_record["delivery_mode"] in ("dry_run", "blocked"),
                "delivery_mode set")
    out["subject"] = routing_record["subject"]
    out["delivery_mode"] = routing_record["delivery_mode"]
    out["checks"].append("email payload generated (dry_run, spam-safer subject)")

    # 5. audit logs grew
    inbound = _read(routing.INBOUND_FILE)
    routed = _read(routing.ROUTED_FILE)
    delivery = _read(routing.DELIVERY_LOG_FILE)
    assert_true(len(inbound["leads"]) >= 1, "inbound_leads.json empty")
    assert_true(len(routed["events"]) >= 1, "routed_leads.json empty")
    assert_true(len(delivery["events"]) >= 1, "delivery_log.json empty")
    last_inbound = inbound["leads"][-1]
    last_routed = routed["events"][-1]
    last_delivery = delivery["events"][-1]
    assert_eq(last_inbound["lead_id"], lead["lead_id"], "inbound tail lead_id")
    assert_eq(last_routed["lead_id"], lead["lead_id"], "routed tail lead_id")
    assert_eq(last_delivery["lead_id"], lead["lead_id"], "delivery tail lead_id")
    out["checks"].append("inbound/routed/delivery logs appended")

    # 6. BookerMove export validator still PASSES on existing snapshot
    snapshot = _read(os.path.join(ROOT, "_truth", "exports", "leads_export.json"))
    ok, errors = bookermove_export.validate_envelope(snapshot)
    assert_true(ok, f"bookermove envelope no longer validates: {errors}")
    out["checks"].append("bookermove_export validator: PASS on existing snapshot")

    # 7. no-match ZIP yields no_match -- no fake success
    form_nomatch = dict(form_richmond,
                        customer_name="Boundary",
                        phone="555-0000",
                        pickup_zip="99999")
    nomatch = routing.submit_quote(form_nomatch, dry_run=True)
    assert_true(nomatch["ok"], "validation should still pass")
    assert_eq(nomatch["routing"]["status"], "no_match", "no_match status")
    assert_true(nomatch["routing"]["mover"] is None, "no_match mover is None")
    out["checks"].append("ZIP 99999 -> no_match (honest)")

    # 8. validation rejects empty/bad inputs (truth gate)
    bad = routing.submit_quote({"customer_name": "", "phone": "",
                                "pickup_zip": "abc"}, dry_run=True)
    assert_true(not bad["ok"], "bad form should fail validation")
    assert_true(len(bad["errors"]) > 0, "bad form should list errors")
    out["checks"].append("bad form rejected with explicit errors")

    # 9. live-delivery gate is honest: with no env set, smoke_send must
    #    surface delivery_blocked, never silently succeed.
    smoke = routing.smoke_send(mover_id="M001")
    assert_true(smoke["routing"] is not None, "smoke_send returned no routing")
    assert_eq(smoke["routing"]["status"], "delivery_blocked",
              "smoke must block when env is unconfigured")
    assert_true(bool(smoke["routing"].get("block_reasons")),
                "block_reasons must be populated")
    out["smoke_block_reasons"] = smoke["routing"]["block_reasons"]
    out["checks"].append("smoke_send refuses live without env (no fake success)")

    # 10. delivery_log entries carry full evidence (provider, message_id,
    #     to/to_original, redirected flag, mode, timestamps).
    delivery = _read(routing.DELIVERY_LOG_FILE)
    last = delivery["events"][-1]
    for f in ("provider", "to", "to_original", "redirected", "delivery_mode",
              "status", "message_id", "from", "subject", "at"):
        assert_true(f in last, f"delivery_log missing {f}")
    assert_eq(last["provider"], "resend", "provider tag")
    out["last_delivery_keys"] = sorted(last.keys())
    out["checks"].append("delivery_log: full provider evidence captured")

    out["pass"] = True
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
