"""MARKO -> BookerMove lead export writer.

Builds and atomically writes the canonical envelope JSON snapshot that
BookerMove consumes. Validation rules live in bookermove_schema; this
module owns construction, file I/O, and demo data only.

Rules enforced by the write path:
- validator runs BEFORE the file is replaced; invalid envelopes never
  overwrite the prior good file
- atomic write via temp file + os.replace, so a half-written file is
  impossible
- source is hardcoded to "marko"
- exported_at is always UTC ISO-8601 with explicit timezone
- lead_count is computed from len(leads), never hand-set
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from bookermove_schema import (
    ENUM_CONFIDENCE,
    ENUM_PRIORITY,
    ENUM_SIGNAL_TYPES,
    REQUIRED_ENVELOPE_FIELDS,
    REQUIRED_LEAD_FIELDS,
    SCHEMA_VERSION,
    SOURCE,
    ExportValidationError,
    validate_envelope,
)

__all__ = [
    "DEFAULT_EXPORT_DIR",
    "DEFAULT_EXPORT_PATH",
    "ENUM_CONFIDENCE",
    "ENUM_PRIORITY",
    "ENUM_SIGNAL_TYPES",
    "ExportValidationError",
    "REQUIRED_ENVELOPE_FIELDS",
    "REQUIRED_LEAD_FIELDS",
    "SCHEMA_VERSION",
    "SOURCE",
    "VA_DEMO_LEADS",
    "build_envelope",
    "generate_demo_export",
    "validate_envelope",
    "write_export",
]

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXPORT_DIR = os.path.join(_REPO_ROOT, "_truth", "exports")
DEFAULT_EXPORT_PATH = os.path.join(DEFAULT_EXPORT_DIR, "leads_export.json")


def build_envelope(leads, run_id=None, exported_at=None):
    """Construct a fresh envelope. Auto-fills run_id and exported_at when absent."""
    if run_id is None:
        run_id = f"marko-{uuid.uuid4().hex[:12]}"
    if exported_at is None:
        exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at,
        "source": SOURCE,
        "run_id": run_id,
        "lead_count": len(leads),
        "leads": list(leads),
    }


def write_export(leads, run_id=None, path=DEFAULT_EXPORT_PATH, exported_at=None):
    """Validate then atomically write the export envelope.

    Order matters: build, validate, then write. On validation failure the
    prior good file at `path` is left untouched and ExportValidationError
    is raised. On success the write is tmp-file + os.replace so readers
    never observe a partial file.
    """
    envelope = build_envelope(leads, run_id=run_id, exported_at=exported_at)
    ok, errors = validate_envelope(envelope)
    if not ok:
        raise ExportValidationError(errors)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(envelope, f, indent=2)
    os.replace(tmp, path)
    return path


# Demo-safe Virginia mover opportunities. Locality-only, no PII, used for
# fixture generation and Truth verification. Safe to ship.
VA_DEMO_LEADS = [
    {
        "lead_id": "va-001",
        "lead_type": "move_opportunity",
        "move_signal_type": "moved_in",
        "address_area": "Short Pump area",
        "city": "Richmond",
        "state": "VA",
        "zip": "23233",
        "service_radius_match": "within 10 miles of Richmond metro",
        "source": "marko-public-signal",
        "source_url": None,
        "signal_date": "2026-05-10T00:00:00Z",
        "confidence": "high",
        "priority": "call_today",
        "recommended_action": "Reach out today with welcome-to-the-area moving offer",
        "outreach_angle": "New residents often need help with secondary moves and storage",
        "compliance_note": "Use only public locality info; verify before outreach",
    },
    {
        "lead_id": "va-002",
        "lead_type": "move_opportunity",
        "move_signal_type": "moved_out",
        "address_area": "Old Town district",
        "city": "Alexandria",
        "state": "VA",
        "zip": "22314",
        "service_radius_match": "within 5 miles of Alexandria service area",
        "source": "marko-public-signal",
        "source_url": None,
        "signal_date": "2026-05-08T00:00:00Z",
        "confidence": "medium",
        "priority": "follow_up",
        "recommended_action": "Follow up within 7 days on transition support",
        "outreach_angle": "Recently vacated homes often need cleanout or staging haul-away",
        "compliance_note": "Public locality only; no resident PII",
    },
    {
        "lead_id": "va-003",
        "lead_type": "move_opportunity",
        "move_signal_type": "nearby_homeowner",
        "address_area": "Ghent neighborhood",
        "city": "Norfolk",
        "state": "VA",
        "zip": "23507",
        "service_radius_match": "within target Hampton Roads metro",
        "source": "marko-public-signal",
        "source_url": None,
        "signal_date": "2026-05-05T00:00:00Z",
        "confidence": "low",
        "priority": "low_priority",
        "recommended_action": "Add to nurture list; revisit next quarter",
        "outreach_angle": "Established homeowners may need long-haul or storage when life changes",
        "compliance_note": "Aggregate-area signal only; do not infer individual move intent",
    },
]


def generate_demo_export(path=DEFAULT_EXPORT_PATH, run_id=None):
    """Write the demo-safe Virginia export. Used for fixture generation."""
    return write_export(VA_DEMO_LEADS, run_id=run_id, path=path)


if __name__ == "__main__":
    out = generate_demo_export()
    print(f"wrote {out}")
