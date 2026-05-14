"""Truth verification for N4-MOVESIGNALZONE-SCORING-FUNCTION."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from move_signal_score import (  # noqa: E402
    confidence_safety_component,
    derive_confidence,
    derive_urgency,
    distance_between,
    freshness_component,
    mover_relevance_component,
    proximity_component,
    score_zone,
    signal_strength_component,
)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" :: {detail}" if detail else ""))


# Mover base: Richmond, VA roughly (23230)
MOVER_LAT, MOVER_LON = 37.5407, -77.4360


def main():
    # --- Component edge cases (locked thresholds) ---
    check("freshness 0d -> 25", freshness_component(0) == 25)
    check("freshness 13d -> 25", freshness_component(13) == 25)
    check("freshness 14d -> 15", freshness_component(14) == 15)
    check("freshness 44d -> 15", freshness_component(44) == 15)
    check("freshness 45d -> 5", freshness_component(45) == 5)
    check("freshness 179d -> 5", freshness_component(179) == 5)
    check("freshness 180d -> 0", freshness_component(180) == 0)

    check("proximity 0mi -> 20", proximity_component(0) == 20)
    check("proximity 14.9mi -> 20", proximity_component(14.9) == 20)
    check("proximity 15mi -> 15", proximity_component(15) == 15)
    check("proximity 34.9mi -> 15", proximity_component(34.9) == 15)
    check("proximity 35mi -> 10", proximity_component(35) == 10)
    check("proximity 49mi -> 10", proximity_component(49) == 10)
    check("proximity 50mi -> 5", proximity_component(50) == 5)
    check("proximity 75mi -> 5", proximity_component(75) == 5)
    check("proximity 76mi -> 0", proximity_component(76) == 0)

    check("signal_strength 0 -> 0", signal_strength_component(0) == 0)
    check("signal_strength 100 -> 25", signal_strength_component(100) == 25)
    # Python's round() is banker's rounding: round(12.5) -> 12.
    check("signal_strength 50 -> 12", signal_strength_component(50) == 12)
    check("signal_strength clamps over", signal_strength_component(200) == 25)
    check("signal_strength clamps under", signal_strength_component(-50) == 0)

    check("mover_relevance moved_in_trend -> 20",
          mover_relevance_component("moved_in_trend") == 20)
    check("mover_relevance moved_out_trend -> 20",
          mover_relevance_component("moved_out_trend") == 20)
    check("mover_relevance turnover -> 12",
          mover_relevance_component("turnover") == 12)
    check("mover_relevance construction_surge -> 10",
          mover_relevance_component("construction_surge") == 10)
    check("mover_relevance seasonal_window -> 8",
          mover_relevance_component("seasonal_window") == 8)
    check("mover_relevance turnover + seasonal -> 17",
          mover_relevance_component("turnover", in_seasonal_window=True) == 17)
    check("mover_relevance construction + seasonal capped at 20",
          mover_relevance_component("moved_in_trend", in_seasonal_window=True) == 20)

    check("confidence_safety composite -> 10",
          confidence_safety_component("composite") == 10)
    check("confidence_safety permits + 2 corroborators -> 10",
          confidence_safety_component("permits", corroborating_sources=2) == 10)
    check("confidence_safety vacancy + 1 corroborator -> 9",
          confidence_safety_component("vacancy", corroborating_sources=1) == 9)
    check("confidence_safety permits alone -> 7",
          confidence_safety_component("permits") == 7)
    check("confidence_safety acs alone -> 6",
          confidence_safety_component("acs") == 6)
    check("confidence_safety calendar alone -> 4",
          confidence_safety_component("calendar") == 4)

    # --- Urgency / confidence derivations ---
    check("urgency 100 -> call_today", derive_urgency(100) == "call_today")
    check("urgency 80 -> call_today", derive_urgency(80) == "call_today")
    check("urgency 79 -> follow_up", derive_urgency(79) == "follow_up")
    check("urgency 60 -> follow_up", derive_urgency(60) == "follow_up")
    check("urgency 59 -> low_priority", derive_urgency(59) == "low_priority")
    check("urgency 0 -> low_priority", derive_urgency(0) == "low_priority")

    check("confidence composite -> high", derive_confidence("composite") == "high")
    check("confidence permits 2-corroborated -> high",
          derive_confidence("permits", 2) == "high")
    check("confidence permits alone -> medium",
          derive_confidence("permits") == "medium")
    check("confidence vacancy alone -> medium",
          derive_confidence("vacancy") == "medium")
    check("confidence calendar -> low", derive_confidence("calendar") == "low")
    check("confidence acs -> low", derive_confidence("acs") == "low")

    # --- Synthetic Richmond scenarios (end-to-end) ---

    # Chesterfield/Midlothian: hot composite zone, fresh, 12 mi from mover
    chesterfield_zone = {"latitude": 37.5076, "longitude": -77.6663}
    dist = distance_between(chesterfield_zone, MOVER_LAT, MOVER_LON)
    out = score_zone(
        freshness_days=10,
        distance_miles=dist,
        signal_strength=85,
        signal_type="moved_in_trend",
        source_type="composite",
        corroborating_sources=2,
        in_seasonal_window=True,
    )
    check(
        "Chesterfield/Midlothian composite hot zone -> call_today/high",
        out["urgency"] == "call_today" and out["confidence"] == "high"
        and out["move_activity_score"] >= 80,
        f"score={out['move_activity_score']} dist={dist:.1f}mi breakdown={out['breakdown']}",
    )

    # Petersburg: federal-only, moderate signal, ~25 mi from Richmond mover
    petersburg_zone = {"latitude": 37.2279, "longitude": -77.4019}
    dist = distance_between(petersburg_zone, MOVER_LAT, MOVER_LON)
    out = score_zone(
        freshness_days=40,
        distance_miles=dist,
        signal_strength=55,
        signal_type="moved_out_trend",
        source_type="vacancy",
        corroborating_sources=0,
    )
    check(
        "Petersburg federal-only moderate -> follow_up/medium",
        out["urgency"] == "follow_up" and out["confidence"] == "medium",
        f"score={out['move_activity_score']} dist={dist:.1f}mi",
    )

    # Hopewell: stale ACS-only background, 22 mi from Richmond mover
    hopewell_zone = {"latitude": 37.3043, "longitude": -77.2872}
    dist = distance_between(hopewell_zone, MOVER_LAT, MOVER_LON)
    out = score_zone(
        freshness_days=300,
        distance_miles=dist,
        signal_strength=20,
        signal_type="turnover",
        source_type="acs",
    )
    check(
        "Hopewell stale ACS background -> low_priority/low",
        out["urgency"] == "low_priority" and out["confidence"] == "low",
        f"score={out['move_activity_score']}",
    )

    # Henrico Short Pump: recent permits, very close, no corroboration.
    # Fresh+near+strong permit signal is genuinely call-worthy for a
    # mover even without composite corroboration -- score lands at 80.
    short_pump_zone = {"latitude": 37.6517, "longitude": -77.6147}
    dist = distance_between(short_pump_zone, MOVER_LAT, MOVER_LON)
    out = score_zone(
        freshness_days=8,
        distance_miles=dist,
        signal_strength=70,
        signal_type="construction_surge",
        source_type="permits",
    )
    check(
        "Henrico Short Pump permits-only fresh -> call_today/medium",
        out["urgency"] == "call_today" and out["confidence"] == "medium",
        f"score={out['move_activity_score']} dist={dist:.1f}mi",
    )

    # Variant: moderate-strength permits-only zone lands in follow_up.
    out_moderate = score_zone(
        freshness_days=20,
        distance_miles=20,
        signal_strength=60,
        signal_type="construction_surge",
        source_type="permits",
    )
    check(
        "Henrico moderate permits-only -> follow_up/medium",
        out_moderate["urgency"] == "follow_up"
        and out_moderate["confidence"] == "medium",
        f"score={out_moderate['move_activity_score']}",
    )

    # Beyond service area: 90 mi out should drop priority sharply
    out_of_radius = score_zone(
        freshness_days=10,
        distance_miles=90,
        signal_strength=85,
        signal_type="moved_in_trend",
        source_type="composite",
        corroborating_sources=2,
    )
    check(
        "Out-of-radius (90mi) zone -> follow_up at most",
        out_of_radius["urgency"] in ("follow_up", "low_priority"),
        f"score={out_of_radius['move_activity_score']}",
    )

    # Score caps at 100
    capped = score_zone(
        freshness_days=0,
        distance_miles=0,
        signal_strength=100,
        signal_type="moved_in_trend",
        source_type="composite",
        corroborating_sources=5,
        in_seasonal_window=True,
    )
    check("max-everything score == 100", capped["move_activity_score"] == 100,
          f"score={capped['move_activity_score']}")

    # Score floor at 0
    floored = score_zone(
        freshness_days=999,
        distance_miles=999,
        signal_strength=0,
        signal_type="seasonal_window",
        source_type="calendar",
    )
    check("min-everything score >= 0", floored["move_activity_score"] >= 0)

    # Determinism: same inputs -> same outputs
    a = score_zone(freshness_days=20, distance_miles=10, signal_strength=60,
                   signal_type="moved_in_trend", source_type="permits")
    b = score_zone(freshness_days=20, distance_miles=10, signal_strength=60,
                   signal_type="moved_in_trend", source_type="permits")
    check("scoring is deterministic", a == b)

    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"summary: {len(RESULTS) - len(failed)}/{len(RESULTS)} scoring checks passed")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
