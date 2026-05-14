"""TRUTH verifier for the N+1 inbound-demand discovery layer.

Runs marko_demand.write_all() against the live zone export + movers
registry, then asserts:

  * every demand_opportunities entry passes shape + enum checks
  * every routing_ready entry resolves to a real mover OR an explicit
    no_match record (no silent drops)
  * hot_zips is a strict subset of opportunities, sorted high-to-low
    by confidence rank then estimated value high-end
  * no demand record carries homeowner-identifying fields (privacy
    drift guard -- compliance_note must be present and aggregate-only)

Exit 0 = pass, exit 1 = fail. Prints a human summary regardless.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

import marko_demand  # noqa: E402
import routing       # noqa: E402

REQUIRED_OPP_FIELDS = (
    "opportunity_id", "city", "state", "zip", "signal_type",
    "confidence", "urgency", "recommended_capture_page",
    "recommended_mover_type", "estimated_lead_value",
    "compliance_note",
)

# Names that would suggest individual-person tracking. None of these
# are allowed in any demand record. If even one shows up the layer has
# privacy-drifted and must fail the truth check.
FORBIDDEN_OPP_FIELDS = (
    "owner_name", "homeowner_name", "resident_name", "occupant",
    "phone", "email", "address_line", "street_address",
    "ssn", "license_plate",
)


def fail(errors, label, msg):
    errors.append(f"{label}: {msg}")


def check_opportunity(opp, errors, idx):
    label = f"opp[{idx}]"
    for f in REQUIRED_OPP_FIELDS:
        if f not in opp:
            fail(errors, label, f"missing field {f!r}")
    for f in FORBIDDEN_OPP_FIELDS:
        if f in opp:
            fail(errors, label, f"forbidden field present {f!r}")
    if opp.get("zip") and (
        len(opp["zip"]) != 5 or not opp["zip"].isdigit()
    ):
        fail(errors, label, f"zip not 5-digit: {opp['zip']!r}")
    if opp.get("state") and len(opp["state"]) != 2:
        fail(errors, label, f"state not 2-letter: {opp['state']!r}")
    if opp.get("confidence") not in marko_demand.ALLOWED_CONFIDENCE:
        fail(errors, label, f"confidence not in enum: {opp.get('confidence')!r}")
    if opp.get("urgency") not in marko_demand.ALLOWED_URGENCY:
        fail(errors, label, f"urgency not in enum: {opp.get('urgency')!r}")
    if opp.get("signal_type") not in marko_demand.ALLOWED_SIGNAL_TYPES:
        fail(errors, label, f"signal_type not in enum: {opp.get('signal_type')!r}")
    val = opp.get("estimated_lead_value") or {}
    if not isinstance(val, dict):
        fail(errors, label, "estimated_lead_value must be object")
    else:
        for k in ("low_usd", "high_usd"):
            if not isinstance(val.get(k), int) or val[k] < 0:
                fail(errors, label, f"estimated_lead_value.{k} must be non-neg int")
        if (
            isinstance(val.get("low_usd"), int)
            and isinstance(val.get("high_usd"), int)
            and val["low_usd"] > val["high_usd"]
        ):
            fail(errors, label, "estimated_lead_value low > high")
    note = (opp.get("compliance_note") or "").lower()
    if "aggregate" not in note:
        fail(errors, label, "compliance_note must declare aggregate-only")


def check_routing(ready, opps, movers, errors):
    opps_by_id = {o["opportunity_id"]: o for o in opps}
    movers_by_id = {m["mover_id"]: m for m in movers}
    if len(ready) != len(opps):
        fail(errors, "routing", f"count mismatch ready={len(ready)} opps={len(opps)}")
    for r in ready:
        oid = r.get("opportunity_id")
        if oid not in opps_by_id:
            fail(errors, "routing", f"unknown opportunity_id {oid!r}")
            continue
        if r.get("matched_mover") is None:
            if r.get("match_basis") != "no_match":
                fail(errors, "routing",
                     f"{oid}: matched_mover null but match_basis != no_match")
            continue
        mid = r["matched_mover"].get("mover_id")
        if mid not in movers_by_id:
            fail(errors, "routing",
                 f"{oid}: matched_mover {mid!r} not in registry")
            continue
        # Sanity: if match_basis=zip, the opp ZIP must really be in that
        # mover's zip_codes. Catches drift between select_mover and
        # routing_ready.
        if r.get("match_basis") == "zip":
            mover = movers_by_id[mid]
            if r["zip"] not in (mover.get("zip_codes") or []):
                fail(errors, "routing",
                     f"{oid}: zip {r['zip']} claimed match but not in mover zips")


def check_hot_zips(hot, opps, errors):
    opp_ids = {o["opportunity_id"] for o in opps}
    rank = {"high": 2, "medium": 1, "low": 0}
    last = (10, 10**9)
    for h in hot:
        if h["opportunity_id"] not in opp_ids:
            fail(errors, "hot", f"unknown opportunity_id {h['opportunity_id']!r}")
        if h["urgency"] != "high":
            fail(errors, "hot", f"{h['opportunity_id']}: urgency != high")
        if h["confidence"] not in ("high", "medium"):
            fail(errors, "hot",
                 f"{h['opportunity_id']}: confidence {h['confidence']!r} disallowed")
        cur = (rank[h["confidence"]], h["estimated_lead_value"]["high_usd"])
        if cur > last:
            fail(errors, "hot", f"{h['opportunity_id']}: sort order broken")
        last = cur


def main():
    payloads = marko_demand.write_all()
    opps = payloads["opportunities"]
    ready = payloads["routing_ready"]
    hot = payloads["hot_zips"]
    movers = routing.load_movers()

    errors = []
    if not opps:
        fail(errors, "opps", "no opportunities derived (empty zones export?)")
    for i, o in enumerate(opps):
        check_opportunity(o, errors, i)
    check_routing(ready, opps, movers, errors)
    check_hot_zips(hot, opps, errors)

    summary = {
        "ok": not errors,
        "mover_count": len(movers),
        "opportunity_count": len(opps),
        "routing_ready_count": len(ready),
        "hot_zip_count": len(hot),
        "matched_routing": sum(1 for r in ready if r.get("matched_mover")),
        "files": [
            os.path.relpath(marko_demand.DEMAND_FILE, ROOT),
            os.path.relpath(marko_demand.ROUTING_READY_FILE, ROOT),
            os.path.relpath(marko_demand.HOT_ZIPS_FILE, ROOT),
        ],
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
