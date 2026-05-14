"""Truth verification for N3-MARKO-FIXTURE-FILE-GENERATION.

Confirms each canonical fixture in _truth/fixtures/leads/ produces the
expected validator outcome. Loader-level concerns (staleness threshold,
file-missing handling) are out of scope here -- the stale fixture must
be schema-valid; loader rules will reject it later in N4.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookermove_schema import validate_envelope  # noqa: E402

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "leads"
)

# Each entry: (filename, expected_valid, expected_error_substring_or_None)
CASES = [
    ("fixture_happy.json",          True,  None),
    ("fixture_empty.json",          True,  None),
    ("fixture_missing_field.json",  False, "missing required field: run_id"),
    ("fixture_count_mismatch.json", False, "does not match len(leads)"),
    ("fixture_bad_source.json",     False, 'source must equal "marko"'),
    ("fixture_stale.json",          True,  None),
]

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {label}" + (f" :: {detail}" if detail else ""))


def main():
    for name, expect_valid, expect_substr in CASES:
        path = os.path.join(FIXTURE_DIR, name)
        if not os.path.exists(path):
            check(f"{name} exists", False, f"missing at {path}")
            continue
        with open(path, "r", encoding="utf-8") as f:
            envelope = json.load(f)

        ok, errors = validate_envelope(envelope)

        if expect_valid:
            check(
                f"{name} validates",
                ok and not errors,
                f"errors={errors}",
            )
        else:
            substr_hit = any(expect_substr in e for e in errors) if expect_substr else False
            check(
                f"{name} rejects (only because: {expect_substr})",
                (not ok) and substr_hit,
                f"errors={errors}",
            )

    # Extra check: the happy fixture must cover all 3 signal types.
    happy = json.load(open(os.path.join(FIXTURE_DIR, "fixture_happy.json"), "r", encoding="utf-8"))
    types = sorted({lead["move_signal_type"] for lead in happy["leads"]})
    check(
        "fixture_happy covers all 3 signal types",
        types == ["moved_in", "moved_out", "nearby_homeowner"],
        f"types={types}",
    )

    # Extra check: stale fixture is schema-valid (loader will mark stale later).
    stale = json.load(open(os.path.join(FIXTURE_DIR, "fixture_stale.json"), "r", encoding="utf-8"))
    ok, errs = validate_envelope(stale)
    check(
        "fixture_stale is schema-valid (loader marks staleness, not validator)",
        ok and not errs,
        f"exported_at={stale['exported_at']}",
    )

    failed = [r for r in results if not r[1]]
    print()
    print(f"summary: {len(results) - len(failed)}/{len(results)} fixture checks passed")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
