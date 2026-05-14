"""MARKO V1 internal MoveSignalZone model + validator (N3).

Zone-level move-activity record. NEVER carries individual homeowner
identity -- aggregated to ZIP/neighborhood at ingest. Validation rules
mirror the discipline used for the BookerMove export contract: enums
locked, ISO-8601 for dates, single source-of-truth list of required
fields.

Scoring (move_activity_score, urgency, confidence) is computed elsewhere
(N4). The model accepts pre-scored values; validation only checks shape
and ranges.
"""
from __future__ import annotations

from datetime import datetime

REQUIRED_FIELDS = (
    "zone_id",
    "city",
    "state",
    "zip",
    "neighborhood",
    "latitude",
    "longitude",
    "radius_miles",
    "source_type",
    "source_name",
    "source_url",
    "signal_type",
    "signal_strength",
    "freshness_days",
    "observation_date",
    "move_activity_score",
    "urgency",
    "confidence",
    "recommended_action",
    "outreach_angle",
    "compliance_note",
)

ENUM_SOURCE_TYPES = {"permits", "vacancy", "acs", "calendar", "composite"}
ENUM_SIGNAL_TYPES = {
    "moved_in_trend",
    "moved_out_trend",
    "turnover",
    "construction_surge",
    "seasonal_window",
}
ENUM_URGENCY = {"call_today", "follow_up", "low_priority"}
ENUM_CONFIDENCE = {"low", "medium", "high"}


class ZoneValidationError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_zone(zone):
    """Return (ok, errors). Does not mutate input."""
    if not isinstance(zone, dict):
        return False, ["zone must be a JSON object"]

    errors = []
    for k in REQUIRED_FIELDS:
        if k not in zone:
            errors.append(f"zone missing required field: {k}")
    if errors:
        return False, errors

    if not isinstance(zone["zone_id"], str) or not zone["zone_id"]:
        errors.append("zone_id must be a non-empty string")
    if not isinstance(zone["city"], str) or not zone["city"]:
        errors.append("city must be a non-empty string")
    if not isinstance(zone["state"], str) or len(zone["state"]) != 2:
        errors.append("state must be a 2-letter string")
    if not isinstance(zone["zip"], str) or len(zone["zip"]) != 5 or not zone["zip"].isdigit():
        errors.append("zip must be a 5-digit string")
    if not isinstance(zone["neighborhood"], str):
        errors.append("neighborhood must be a string")

    for k in ("latitude", "longitude", "radius_miles", "signal_strength",
              "freshness_days", "move_activity_score"):
        v = zone[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errors.append(f"{k} must be a number")
    if isinstance(zone["latitude"], (int, float)) and not (-90 <= zone["latitude"] <= 90):
        errors.append("latitude out of range")
    if isinstance(zone["longitude"], (int, float)) and not (-180 <= zone["longitude"] <= 180):
        errors.append("longitude out of range")
    if isinstance(zone["radius_miles"], (int, float)) and zone["radius_miles"] <= 0:
        errors.append("radius_miles must be positive")
    if isinstance(zone["signal_strength"], (int, float)) and not (0 <= zone["signal_strength"] <= 100):
        errors.append("signal_strength must be in [0,100]")
    if isinstance(zone["freshness_days"], (int, float)) and zone["freshness_days"] < 0:
        errors.append("freshness_days must be >= 0")
    if isinstance(zone["move_activity_score"], (int, float)) and not (0 <= zone["move_activity_score"] <= 100):
        errors.append("move_activity_score must be in [0,100]")

    if zone["source_type"] not in ENUM_SOURCE_TYPES:
        errors.append(f"source_type must be one of {sorted(ENUM_SOURCE_TYPES)}")
    if not isinstance(zone["source_name"], str) or not zone["source_name"]:
        errors.append("source_name must be a non-empty string")
    if zone["source_url"] is not None and not isinstance(zone["source_url"], str):
        errors.append("source_url must be string or null")

    if zone["signal_type"] not in ENUM_SIGNAL_TYPES:
        errors.append(f"signal_type must be one of {sorted(ENUM_SIGNAL_TYPES)}")
    if zone["urgency"] not in ENUM_URGENCY:
        errors.append(f"urgency must be one of {sorted(ENUM_URGENCY)}")
    if zone["confidence"] not in ENUM_CONFIDENCE:
        errors.append(f"confidence must be one of {sorted(ENUM_CONFIDENCE)}")

    od = zone["observation_date"]
    if not isinstance(od, str):
        errors.append("observation_date must be a string")
    else:
        try:
            _parse_iso(od)
        except ValueError:
            errors.append("observation_date must parse as ISO-8601")

    for k in ("recommended_action", "outreach_angle", "compliance_note"):
        if not isinstance(zone[k], str) or not zone[k]:
            errors.append(f"{k} must be a non-empty string")

    return (not errors), errors
