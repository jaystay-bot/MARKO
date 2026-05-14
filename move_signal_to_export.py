"""MARKO V1 MoveSignalZone -> BookerMove export mapper (N5).

Translates internal MoveSignalZone records into the locked
16-field BookerMove lead contract WITHOUT changing the contract.

Mapping rules (locked in V1 plan §5):

  signal_type             -> move_signal_type
    moved_in_trend          -> moved_in
    moved_out_trend         -> moved_out
    turnover                -> nearby_homeowner
    construction_surge      -> nearby_homeowner
    seasonal_window         -> nearby_homeowner

  source_type / source_name -> source string carried as-is
  service_radius_match string is composed from radius_miles + city
  compliance_note copied verbatim
"""
from __future__ import annotations

from bookermove_export import write_export
from move_signal_zone import validate_zone

SIGNAL_TYPE_MAP = {
    "moved_in_trend": "moved_in",
    "moved_out_trend": "moved_out",
    "turnover": "nearby_homeowner",
    "construction_surge": "nearby_homeowner",
    "seasonal_window": "nearby_homeowner",
}


def zone_to_lead(zone):
    """Map one MoveSignalZone dict to one BookerMove lead dict.

    Caller is expected to pass a zone that already passes
    validate_zone(). We re-validate defensively so a bad zone never
    leaks into the export pipeline.
    """
    ok, errors = validate_zone(zone)
    if not ok:
        raise ValueError(f"zone failed validation: {errors}")

    bm_signal = SIGNAL_TYPE_MAP[zone["signal_type"]]
    radius_str = (
        f"within {int(round(zone['radius_miles']))} miles of "
        f"{zone['city']} service area"
    )

    return {
        "lead_id": zone["zone_id"],
        "lead_type": "move_opportunity",
        "move_signal_type": bm_signal,
        "address_area": zone["neighborhood"],
        "city": zone["city"],
        "state": zone["state"],
        "zip": zone["zip"],
        "service_radius_match": radius_str,
        "source": zone["source_name"],
        "source_url": zone["source_url"],
        "signal_date": zone["observation_date"],
        "confidence": zone["confidence"],
        "priority": zone["urgency"],
        "recommended_action": zone["recommended_action"],
        "outreach_angle": zone["outreach_angle"],
        "compliance_note": zone["compliance_note"],
    }


def zones_to_leads(zones):
    """Map a list of MoveSignalZone dicts to a list of BookerMove leads."""
    return [zone_to_lead(z) for z in zones]


def write_zones_export(zones, path=None, run_id=None):
    """Validate, map, and atomically write a Richmond zones export.

    Reuses the N1 BookerMove export writer so the envelope contract
    and atomic-write discipline are unchanged.
    """
    leads = zones_to_leads(zones)
    if path is None:
        return write_export(leads, run_id=run_id)
    return write_export(leads, run_id=run_id, path=path)
