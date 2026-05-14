"""Truth verification for N5-MOVESIGNALZONE-TO-BOOKERMOVE-EXPORT-MAPPER.

Generates a real Richmond Move Activity Zones export and validates
that:
  - mapper covers every signal_type enum
  - resulting envelope passes the locked validate_envelope()
  - resulting leads pass per-lead validation
  - file is atomically written into the same export path BookerMove
    already consumes via the env-path loader (N4 BookerMove side)
  - no contract drift: envelope keys + lead keys exactly match the
    locked 16-field contract
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookermove_export import (  # noqa: E402
    DEFAULT_EXPORT_DIR,
    REQUIRED_ENVELOPE_FIELDS,
    REQUIRED_LEAD_FIELDS,
    validate_envelope,
)
from move_signal_score import distance_between, score_zone  # noqa: E402
from move_signal_to_export import (  # noqa: E402
    SIGNAL_TYPE_MAP,
    write_zones_export,
    zone_to_lead,
    zones_to_leads,
)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" :: {detail}" if detail else ""))


# Mover base: Richmond city center (~ZIP 23230).
MOVER_LAT, MOVER_LON = 37.5407, -77.4360


def make_zone(zone_id, city, zip_code, neighborhood, lat, lon, signal_type,
              source_type, source_name, signal_strength, freshness_days,
              observation_date, recommended_action, outreach_angle,
              corroborators=0, in_seasonal=False, source_url=None):
    dist = distance_between({"latitude": lat, "longitude": lon},
                            MOVER_LAT, MOVER_LON)
    scored = score_zone(
        freshness_days=freshness_days,
        distance_miles=dist,
        signal_strength=signal_strength,
        signal_type=signal_type,
        source_type=source_type,
        corroborating_sources=corroborators,
        in_seasonal_window=in_seasonal,
    )
    return {
        "zone_id": zone_id,
        "city": city,
        "state": "VA",
        "zip": zip_code,
        "neighborhood": neighborhood,
        "latitude": lat,
        "longitude": lon,
        "radius_miles": 3.0,
        "source_type": source_type,
        "source_name": source_name,
        "source_url": source_url,
        "signal_type": signal_type,
        "signal_strength": signal_strength,
        "freshness_days": freshness_days,
        "observation_date": observation_date,
        "move_activity_score": scored["move_activity_score"],
        "urgency": scored["urgency"],
        "confidence": scored["confidence"],
        "recommended_action": recommended_action,
        "outreach_angle": outreach_angle,
        "compliance_note": "Aggregate-area signal only; do not infer individual move intent.",
    }


# Five hand-built Richmond-area zones covering all 5 signal_type enums.
RICHMOND_ZONES = [
    make_zone(
        "rva-chesterfield-midlothian-001", "Chesterfield", "23112",
        "Midlothian corridor", 37.5076, -77.6663,
        signal_type="moved_in_trend", source_type="composite",
        source_name="Chesterfield County Permits + USPS/HUD Vacancy",
        signal_strength=85, freshness_days=10,
        observation_date="2026-05-01T00:00:00Z",
        recommended_action="Prioritize follow-up campaigns and monitor inbound quote demand.",
        outreach_angle="New residents arriving in Midlothian; helpful for unloading, storage, and short-haul moves.",
        corroborators=2, in_seasonal=True,
    ),
    make_zone(
        "rva-henrico-shortpump-001", "Henrico", "23233",
        "Short Pump area", 37.6517, -77.6147,
        signal_type="construction_surge", source_type="permits",
        source_name="Henrico County Permits Portal",
        signal_strength=70, freshness_days=12,
        observation_date="2026-05-03T00:00:00Z",
        recommended_action="Watch for new-build move-in demand over the next 60-90 days.",
        outreach_angle="Recent residential permits indicate upcoming homeowner move-ins in this corridor.",
        source_url="https://data-henrico.opendata.arcgis.com",
    ),
    make_zone(
        "rva-petersburg-vacancy-001", "Petersburg", "23803",
        "central Petersburg", 37.2279, -77.4019,
        signal_type="moved_out_trend", source_type="vacancy",
        source_name="USPS/HUD Aggregated Vacancy Data",
        signal_strength=60, freshness_days=40,
        observation_date="2026-04-15T00:00:00Z",
        recommended_action="Follow up on transition-out support; cleanout and haul-away likely.",
        outreach_angle="Recent vacancy uptick may signal local relocation activity.",
    ),
    make_zone(
        "rva-hopewell-acs-001", "Hopewell", "23860",
        "Hopewell residential core", 37.3043, -77.2872,
        signal_type="turnover", source_type="acs",
        source_name="Census ACS B25004 Vacancy Status (5-year)",
        signal_strength=35, freshness_days=300,
        observation_date="2025-08-01T00:00:00Z",
        recommended_action="Add to nurture list; revisit when fresh permit data arrives.",
        outreach_angle="Background turnover trend in this ZIP; useful for long-term territory planning.",
    ),
    make_zone(
        "rva-vcu-college-window-001", "Richmond", "23220",
        "VCU campus area", 37.5485, -77.4517,
        signal_type="seasonal_window", source_type="calendar",
        source_name="VCU academic calendar (move-in window)",
        signal_strength=50, freshness_days=5,
        observation_date="2026-05-08T00:00:00Z",
        recommended_action="Pre-position outreach for August move-in surge.",
        outreach_angle="College move-in window approaching; small-haul and storage demand will spike.",
    ),
]


def main():
    # Sanity: every signal_type in the locked enum is exercised by at
    # least one fixture zone.
    types_in_fixtures = {z["signal_type"] for z in RICHMOND_ZONES}
    check("fixtures cover every MARKO signal_type",
          types_in_fixtures == set(SIGNAL_TYPE_MAP.keys()),
          f"covered={sorted(types_in_fixtures)}")

    # Map zones -> leads and confirm each lead has the locked 16 fields.
    leads = zones_to_leads(RICHMOND_ZONES)
    check(f"mapped {len(RICHMOND_ZONES)} zones -> {len(leads)} leads",
          len(leads) == len(RICHMOND_ZONES))
    for i, lead in enumerate(leads):
        missing = [k for k in REQUIRED_LEAD_FIELDS if k not in lead]
        extra = set(lead.keys()) - set(REQUIRED_LEAD_FIELDS)
        check(f"lead[{i}] {lead['lead_id']} has exactly the 16 locked fields",
              not missing and not extra,
              f"missing={missing} extra={sorted(extra)}")

    # signal_type translation table is honored.
    for z, l in zip(RICHMOND_ZONES, leads):
        expected_bm = SIGNAL_TYPE_MAP[z["signal_type"]]
        check(f"{z['zone_id']} signal_type {z['signal_type']!r} -> {expected_bm!r}",
              l["move_signal_type"] == expected_bm,
              f"got={l['move_signal_type']}")

    # Per-zone field carry-over.
    for z, l in zip(RICHMOND_ZONES, leads):
        check(f"{z['zone_id']} city/state/zip carried verbatim",
              l["city"] == z["city"] and l["state"] == z["state"]
              and l["zip"] == z["zip"])
        check(f"{z['zone_id']} address_area = neighborhood",
              l["address_area"] == z["neighborhood"])
        check(f"{z['zone_id']} priority = urgency",
              l["priority"] == z["urgency"])
        check(f"{z['zone_id']} confidence carried",
              l["confidence"] == z["confidence"])
        check(f"{z['zone_id']} signal_date = observation_date",
              l["signal_date"] == z["observation_date"])

    # Write the export file (atomic, validated).
    out_path = os.path.join(DEFAULT_EXPORT_DIR, "rva_move_zones_export.json")
    written = write_zones_export(RICHMOND_ZONES, path=out_path,
                                 run_id="rva-zones-truth-001")
    check("export file written", written == out_path and os.path.exists(out_path))

    with open(out_path, "r", encoding="utf-8") as f:
        envelope = json.load(f)

    # Envelope passes the locked validator (no drift).
    ok, errors = validate_envelope(envelope)
    check("Richmond zones envelope passes locked validator",
          ok and not errors, f"errors={errors}")

    # Envelope shape exactly matches the locked contract -- no extras.
    extra_envelope = set(envelope.keys()) - set(REQUIRED_ENVELOPE_FIELDS)
    check("envelope has no extra keys (no contract drift)",
          not extra_envelope, f"extras={sorted(extra_envelope)}")
    check("envelope schema_version is 1.0.0",
          envelope["schema_version"] == "1.0.0")
    check("envelope source is marko", envelope["source"] == "marko")
    check("envelope lead_count matches", envelope["lead_count"] == len(leads))

    # Spot-check the call_today bucket exists (mover gets useful work).
    call_today_count = sum(1 for l in envelope["leads"] if l["priority"] == "call_today")
    check("at least one call_today lead in Richmond export",
          call_today_count >= 1, f"call_today={call_today_count}")

    # Customer-safe vocab spot-check: no internals leaked.
    forbidden = ("scrape", "vendor", "scoring engine", "MARKO internal", "run_id")
    for l in envelope["leads"]:
        for field in ("recommended_action", "outreach_angle", "compliance_note",
                      "source", "service_radius_match"):
            v = l[field].lower()
            for bad in forbidden:
                if bad.lower() in v:
                    check(f"{l['lead_id']}.{field} customer-safe", False,
                          f"contains forbidden term {bad!r}")
                    break
            else:
                continue
            break
        else:
            check(f"{l['lead_id']} text fields customer-safe", True)

    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"summary: {len(RESULTS) - len(failed)}/{len(RESULTS)} mapper checks passed")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
