"""MARKO -> BookerMove export schema + validator (N2).

This module owns the locked validation rules for the export envelope and
the 16-field lead record. It is the single source of truth on the MARKO
side; a future TypeScript port inside BookerMove must mirror these rules
verbatim. BookerMove must NOT import this Python module at runtime --
the contract is the static JSON file, not the Python code.

Rules extracted unchanged from bookermove_export.py (N1):
- envelope required fields, schema_version, source, run_id, exported_at
- lead_count must equal len(leads)
- 16-field lead record, with enum constraints on lead_type,
  move_signal_type, confidence, priority
- ISO-8601 parsing for exported_at and signal_date
"""
from __future__ import annotations

from datetime import datetime

SCHEMA_VERSION = "1.0.0"
SOURCE = "marko"

REQUIRED_ENVELOPE_FIELDS = (
    "schema_version",
    "exported_at",
    "source",
    "run_id",
    "lead_count",
    "leads",
)

REQUIRED_LEAD_FIELDS = (
    "lead_id",
    "lead_type",
    "move_signal_type",
    "address_area",
    "city",
    "state",
    "zip",
    "service_radius_match",
    "source",
    "source_url",
    "signal_date",
    "confidence",
    "priority",
    "recommended_action",
    "outreach_angle",
    "compliance_note",
)

ENUM_SIGNAL_TYPES = {"moved_in", "moved_out", "nearby_homeowner"}
ENUM_CONFIDENCE = {"low", "medium", "high"}
ENUM_PRIORITY = {"call_today", "follow_up", "low_priority"}


class ExportValidationError(ValueError):
    """Raised when an envelope or lead record fails contract validation."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _parse_iso(value):
    # Accept trailing "Z" since stdlib fromisoformat (pre-3.11) rejects it.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_lead(lead, idx):
    errs = []
    if not isinstance(lead, dict):
        return [f"leads[{idx}] must be an object"]
    for k in REQUIRED_LEAD_FIELDS:
        if k not in lead:
            errs.append(f"leads[{idx}] missing field: {k}")
    if errs:
        return errs
    if lead.get("lead_type") != "move_opportunity":
        errs.append(f'leads[{idx}].lead_type must equal "move_opportunity"')
    if lead.get("move_signal_type") not in ENUM_SIGNAL_TYPES:
        errs.append(f"leads[{idx}].move_signal_type invalid")
    if lead.get("confidence") not in ENUM_CONFIDENCE:
        errs.append(f"leads[{idx}].confidence invalid")
    if lead.get("priority") not in ENUM_PRIORITY:
        errs.append(f"leads[{idx}].priority invalid")
    sd = lead.get("signal_date")
    if not isinstance(sd, str):
        errs.append(f"leads[{idx}].signal_date must be a string")
    else:
        try:
            _parse_iso(sd)
        except ValueError:
            errs.append(f"leads[{idx}].signal_date must parse as ISO-8601")
    su = lead.get("source_url")
    if su is not None and not isinstance(su, str):
        errs.append(f"leads[{idx}].source_url must be string or null")
    return errs


def validate_envelope(obj):
    """Return (ok, errors). Does not mutate input.

    Single source of truth for the export contract. MARKO calls this
    write-side; a TypeScript port inside BookerMove must implement the
    identical rule list read-side.
    """
    if not isinstance(obj, dict):
        return False, ["envelope must be a JSON object"]

    errors = []
    for k in REQUIRED_ENVELOPE_FIELDS:
        if k not in obj:
            errors.append(f"envelope missing required field: {k}")
    if errors:
        return False, errors

    if obj.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must equal {SCHEMA_VERSION!r}")

    exported_at = obj.get("exported_at")
    if not isinstance(exported_at, str):
        errors.append("exported_at must be a string")
    else:
        try:
            _parse_iso(exported_at)
        except ValueError:
            errors.append("exported_at must parse as ISO-8601")

    if obj.get("source") != SOURCE:
        errors.append('source must equal "marko"')

    run_id = obj.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        errors.append("run_id must be a non-empty string")

    leads = obj.get("leads")
    if not isinstance(leads, list):
        errors.append("leads must be a list")
        return False, errors

    lead_count = obj.get("lead_count")
    if not isinstance(lead_count, int) or lead_count < 0:
        errors.append("lead_count must be a non-negative integer")
    elif lead_count != len(leads):
        errors.append(
            f"lead_count ({lead_count}) does not match len(leads) ({len(leads)})"
        )

    for i, lead in enumerate(leads):
        errors.extend(_validate_lead(lead, i))

    return (not errors), errors
