"""Truth verification for N3-MOVESIGNALZONE-MODEL-IMPLEMENTATION."""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from move_signal_zone import (  # noqa: E402
    ENUM_CONFIDENCE,
    ENUM_SIGNAL_TYPES,
    ENUM_SOURCE_TYPES,
    ENUM_URGENCY,
    REQUIRED_FIELDS,
    validate_zone,
)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" :: {detail}" if detail else ""))


VALID_ZONE = {
    "zone_id": "rva-chesterfield-midlothian-001",
    "city": "Chesterfield",
    "state": "VA",
    "zip": "23112",
    "neighborhood": "Midlothian corridor",
    "latitude": 37.5076,
    "longitude": -77.6663,
    "radius_miles": 3.0,
    "source_type": "permits",
    "source_name": "Chesterfield County Permits Portal",
    "source_url": "https://www.chesterfield.gov/2114/Open-GIS-Data",
    "signal_type": "construction_surge",
    "signal_strength": 72,
    "freshness_days": 12,
    "observation_date": "2026-05-01T00:00:00Z",
    "move_activity_score": 78,
    "urgency": "follow_up",
    "confidence": "high",
    "recommended_action": "Prioritize follow-up campaigns and monitor inbound quote demand.",
    "outreach_angle": "New residential construction surge in this area indicates upcoming move-in demand.",
    "compliance_note": "Aggregate-area signal only; do not infer individual move intent.",
}


def case(label, mutator, expect_ok, expect_substr=None):
    z = copy.deepcopy(VALID_ZONE)
    mutator(z)
    ok, errors = validate_zone(z)
    if expect_ok:
        check(label, ok and not errors, f"errors={errors}")
    else:
        substr_hit = any(expect_substr in e for e in errors) if expect_substr else (not ok)
        check(label, (not ok) and substr_hit, f"errors={errors}")


def main():
    # Happy path
    ok, errors = validate_zone(VALID_ZONE)
    check("valid zone passes", ok and not errors, f"errors={errors}")

    # Required-field coverage
    for field in REQUIRED_FIELDS:
        case(
            f"missing {field} rejected",
            lambda z, f=field: z.pop(f),
            False,
            f"missing required field: {field}",
        )

    # Enum violations
    case("bad source_type rejected",
         lambda z: z.update(source_type="other"), False, "source_type must be one of")
    case("bad signal_type rejected",
         lambda z: z.update(signal_type="moved_in"), False, "signal_type must be one of")
    case("bad urgency rejected",
         lambda z: z.update(urgency="urgent"), False, "urgency must be one of")
    case("bad confidence rejected",
         lambda z: z.update(confidence="very_high"), False, "confidence must be one of")

    # Range / type violations
    case("zip wrong length rejected",
         lambda z: z.update(zip="2311"), False, "zip must be a 5-digit string")
    case("zip non-digit rejected",
         lambda z: z.update(zip="231AB"), False, "zip must be a 5-digit string")
    case("state non-2-letter rejected",
         lambda z: z.update(state="VAA"), False, "state must be a 2-letter string")
    case("latitude out of range rejected",
         lambda z: z.update(latitude=999.0), False, "latitude out of range")
    case("longitude out of range rejected",
         lambda z: z.update(longitude=999.0), False, "longitude out of range")
    case("score over 100 rejected",
         lambda z: z.update(move_activity_score=150), False, "move_activity_score must be in")
    case("signal_strength under 0 rejected",
         lambda z: z.update(signal_strength=-5), False, "signal_strength must be in")
    case("negative freshness rejected",
         lambda z: z.update(freshness_days=-1), False, "freshness_days must be >= 0")
    case("non-positive radius rejected",
         lambda z: z.update(radius_miles=0), False, "radius_miles must be positive")
    case("bad observation_date rejected",
         lambda z: z.update(observation_date="not-a-date"), False, "observation_date")
    case("source_url=None accepted",
         lambda z: z.update(source_url=None), True)
    case("empty recommended_action rejected",
         lambda z: z.update(recommended_action=""), False, "recommended_action")

    # Sanity: enum sets are exactly what the V1 plan locked
    check("ENUM_SOURCE_TYPES exact",
          ENUM_SOURCE_TYPES == {"permits", "vacancy", "acs", "calendar", "composite"})
    check("ENUM_SIGNAL_TYPES exact",
          ENUM_SIGNAL_TYPES == {"moved_in_trend", "moved_out_trend", "turnover",
                                "construction_surge", "seasonal_window"})
    check("ENUM_URGENCY exact",
          ENUM_URGENCY == {"call_today", "follow_up", "low_priority"})
    check("ENUM_CONFIDENCE exact",
          ENUM_CONFIDENCE == {"low", "medium", "high"})

    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"summary: {len(RESULTS) - len(failed)}/{len(RESULTS)} zone-model checks passed")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
