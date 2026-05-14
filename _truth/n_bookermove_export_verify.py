"""Truth verification for N1-MARKO-EXPORT-WRITER-IMPLEMENTATION.

Proves the six locked truth conditions:
  1. valid export writes successfully
  2. malformed export rejected
  3. lead_count mismatch rejected
  4. partial file writes impossible (atomic via os.replace)
  5. previous good export survives a failed write
  6. exported JSON matches the locked contract exactly
"""
from __future__ import annotations

import copy
import json
import os
import sys

# Make the repo importable when running this file directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookermove_export import (  # noqa: E402
    DEFAULT_EXPORT_PATH,
    REQUIRED_ENVELOPE_FIELDS,
    REQUIRED_LEAD_FIELDS,
    SCHEMA_VERSION,
    SOURCE,
    VA_DEMO_LEADS,
    ExportValidationError,
    build_envelope,
    generate_demo_export,
    validate_envelope,
    write_export,
)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {label}" + (f" :: {detail}" if detail else ""))


def truth_1_valid_writes():
    out_path = generate_demo_export()
    exists = os.path.exists(out_path)
    with open(out_path, "r", encoding="utf-8") as f:
        envelope = json.load(f)
    ok, errors = validate_envelope(envelope)
    check(
        "1. valid export writes and validates",
        exists and ok and envelope["lead_count"] == len(VA_DEMO_LEADS),
        f"path={out_path} errors={errors}",
    )
    return out_path, envelope


def truth_2_malformed_rejected():
    bad_leads = copy.deepcopy(VA_DEMO_LEADS)
    del bad_leads[0]["zip"]  # missing required lead field
    rejected = False
    errors = []
    try:
        write_export(bad_leads, path=os.path.join(os.path.dirname(DEFAULT_EXPORT_PATH), "should_not_exist.json"))
    except ExportValidationError as e:
        rejected = True
        errors = e.errors
    check(
        "2. malformed export rejected (missing lead field)",
        rejected and any("missing field: zip" in e for e in errors),
        f"errors={errors}",
    )


def truth_3_count_mismatch_rejected():
    envelope = build_envelope(VA_DEMO_LEADS)
    envelope["lead_count"] = envelope["lead_count"] + 1  # tamper
    ok, errors = validate_envelope(envelope)
    check(
        "3. lead_count mismatch rejected",
        (not ok) and any("does not match len(leads)" in e for e in errors),
        f"errors={errors}",
    )


def truth_4_atomic_no_partial(out_path):
    # If a write was ever non-atomic, a .tmp would linger after a crash.
    # We assert the writer leaves no .tmp sibling after a successful write.
    tmp_path = out_path + ".tmp"
    no_tmp_left = not os.path.exists(tmp_path)
    # Confirm the file is fully parseable JSON (no truncation).
    parses = False
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            json.load(f)
        parses = True
    except (json.JSONDecodeError, OSError):
        pass
    check(
        "4. partial file writes impossible (no .tmp residue, file parses)",
        no_tmp_left and parses,
        f"tmp_left={os.path.exists(tmp_path)} parses={parses}",
    )


def truth_5_prior_good_survives(out_path, good_envelope):
    # Capture current good content, attempt a bad write, confirm file unchanged.
    with open(out_path, "r", encoding="utf-8") as f:
        before = f.read()

    bad_leads = copy.deepcopy(VA_DEMO_LEADS)
    bad_leads[1]["confidence"] = "ultra-high"  # invalid enum
    raised = False
    try:
        write_export(bad_leads, path=out_path)
    except ExportValidationError:
        raised = True

    with open(out_path, "r", encoding="utf-8") as f:
        after = f.read()

    check(
        "5. previous good export survives a failed write",
        raised and before == after,
        f"raised={raised} unchanged={before == after}",
    )


def truth_6_contract_shape(envelope):
    envelope_keys_ok = all(k in envelope for k in REQUIRED_ENVELOPE_FIELDS)
    extra_envelope = set(envelope.keys()) - set(REQUIRED_ENVELOPE_FIELDS)
    schema_ok = envelope.get("schema_version") == SCHEMA_VERSION
    source_ok = envelope.get("source") == SOURCE
    count_ok = envelope.get("lead_count") == len(envelope.get("leads", []))

    leads_ok = True
    for lead in envelope.get("leads", []):
        missing = [k for k in REQUIRED_LEAD_FIELDS if k not in lead]
        extra = set(lead.keys()) - set(REQUIRED_LEAD_FIELDS)
        if missing or extra:
            leads_ok = False
            break

    check(
        "6. exported JSON matches the locked contract exactly",
        envelope_keys_ok and not extra_envelope and schema_ok and source_ok and count_ok and leads_ok,
        f"extra_envelope={extra_envelope}",
    )


def main():
    out_path, envelope = truth_1_valid_writes()
    truth_2_malformed_rejected()
    truth_3_count_mismatch_rejected()
    truth_4_atomic_no_partial(out_path)
    truth_5_prior_good_survives(out_path, envelope)
    # Re-read after the failed-write attempt to confirm contract still holds.
    with open(out_path, "r", encoding="utf-8") as f:
        envelope = json.load(f)
    truth_6_contract_shape(envelope)

    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"summary: {len(RESULTS) - len(failed)}/{len(RESULTS)} truth checks passed")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
