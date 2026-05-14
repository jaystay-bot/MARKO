"""MARKO inbound-demand discovery layer (N+1).

Reads existing public/legal move-signal data from
_truth/exports/rva_move_zones_export.json (aggregate ZIP-level zones,
sourced from county permits, USPS/HUD vacancy, ACS, public academic
calendars -- never individual-person data) and derives three downstream
JSON artifacts the routing layer can consume:

  demand_opportunities.json   one record per public demand signal
  routing_ready.json          per-opp preview of which mover would win
  hot_zips.json               ZIPs whose signal is HOT enough to act on

No new scraping, no private data, no rebuilds. Buyer registry
(movers.json) and routing match logic (routing.select_mover) are
reused as-is.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import routing
import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ZONES_FILE = os.path.join(
    BASE_DIR, "_truth", "exports", "rva_move_zones_export.json"
)
DEMAND_FILE = os.path.join(BASE_DIR, "demand_opportunities.json")
ROUTING_READY_FILE = os.path.join(BASE_DIR, "routing_ready.json")
HOT_ZIPS_FILE = os.path.join(BASE_DIR, "hot_zips.json")

# Map zone.priority -> S1 urgency vocab. Routing layer (TalkBot etc.)
# only cares about coarse buckets here -- this is opportunity-level,
# not customer-level urgency.
PRIORITY_TO_URGENCY = {
    "call_today": "high",
    "follow_up": "medium",
    "low_priority": "low",
}

# Signal-type defaults: where to send the customer + what mover profile
# best fits. Capture pages are real routes served by main.py / dashboard.py
# (/quote is the live inbound form; the segment query is a no-op tag the
# form already preserves into the lead notes via existing plumbing).
SIGNAL_DEFAULTS = {
    "moved_in": {
        "capture_page": "/quote?segment=move_in",
        "mover_type": "full_service_local",
        "value_low": 500, "value_high": 2500,
    },
    "moved_out": {
        "capture_page": "/quote?segment=move_out",
        "mover_type": "haul_away_or_cleanout",
        "value_low": 300, "value_high": 1500,
    },
    "nearby_homeowner": {
        "capture_page": "/quote?segment=neighborhood",
        "mover_type": "short_haul_local",
        "value_low": 350, "value_high": 1800,
    },
    "turnover": {
        "capture_page": "/quote?segment=turnover",
        "mover_type": "full_service_local",
        "value_low": 500, "value_high": 2500,
    },
    "construction_surge": {
        "capture_page": "/quote?segment=newbuild",
        "mover_type": "full_service_local",
        "value_low": 800, "value_high": 3500,
    },
    "seasonal_window": {
        "capture_page": "/quote?segment=seasonal",
        "mover_type": "small_haul_or_student",
        "value_low": 250, "value_high": 1200,
    },
}

ALLOWED_SIGNAL_TYPES = set(SIGNAL_DEFAULTS.keys())
ALLOWED_CONFIDENCE = {"low", "medium", "high"}
ALLOWED_URGENCY = {"low", "medium", "high"}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_zone_export():
    with open(ZONES_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def to_demand_opportunity(zone):
    """Map one MoveSignalZone-style record to the S1 demand-opportunity shape.

    Carries source + compliance metadata through so downstream consumers
    can re-prove provenance without re-reading the zones file.
    """
    sig = zone.get("move_signal_type") or "nearby_homeowner"
    defaults = SIGNAL_DEFAULTS.get(sig, SIGNAL_DEFAULTS["nearby_homeowner"])
    urgency = PRIORITY_TO_URGENCY.get(zone.get("priority"), "low")
    return {
        "opportunity_id": zone.get("lead_id"),
        "city": zone.get("city") or "",
        "state": zone.get("state") or "",
        "zip": zone.get("zip") or "",
        "signal_type": sig,
        "confidence": zone.get("confidence") or "low",
        "urgency": urgency,
        "recommended_capture_page": defaults["capture_page"],
        "recommended_mover_type": defaults["mover_type"],
        "estimated_lead_value": {
            "low_usd": defaults["value_low"],
            "high_usd": defaults["value_high"],
        },
        "signal_date": zone.get("signal_date"),
        "source": zone.get("source"),
        "source_url": zone.get("source_url"),
        "compliance_note": zone.get("compliance_note"),
    }


def derive_demand_opportunities(zones=None):
    if zones is None:
        zones = _load_zone_export().get("leads", [])
    return [to_demand_opportunity(z) for z in zones]


def derive_routing_ready(opps, movers=None):
    """For each opp, ask routing.select_mover who would win the inbound
    quote at that pickup ZIP. This is preview-only -- no email is sent,
    no inbound_leads.json append, no missed_money side-effect.
    """
    if movers is None:
        movers = routing.load_movers()
    out = []
    for opp in opps:
        synthetic = {"pickup_zip": opp["zip"], "city": opp["city"]}
        mover = routing.select_mover(synthetic, movers=movers)
        out.append({
            "opportunity_id": opp["opportunity_id"],
            "zip": opp["zip"],
            "city": opp["city"],
            "matched_mover": (
                {
                    "mover_id": mover["mover_id"],
                    "business_name": mover["business_name"],
                    "exclusive": bool(mover.get("exclusive")),
                }
                if mover else None
            ),
            "match_basis": "zip" if mover and opp["zip"] in (
                mover.get("zip_codes") or []
            ) else ("city" if mover else "no_match"),
            "estimated_lead_value": opp["estimated_lead_value"],
            "urgency": opp["urgency"],
            "confidence": opp["confidence"],
        })
    return out


def derive_hot_zips(opps):
    """ZIPs we should treat as hot today: high urgency AND non-low confidence.

    Sorted by (confidence rank desc, value high desc) so the top entry
    is the strongest single ZIP to pre-position outreach against.
    """
    rank = {"high": 2, "medium": 1, "low": 0}
    hot = [
        o for o in opps
        if o["urgency"] == "high" and o["confidence"] in ("high", "medium")
    ]
    hot.sort(
        key=lambda o: (
            rank.get(o["confidence"], 0),
            o["estimated_lead_value"]["high_usd"],
        ),
        reverse=True,
    )
    return [
        {
            "zip": o["zip"],
            "city": o["city"],
            "signal_type": o["signal_type"],
            "confidence": o["confidence"],
            "urgency": o["urgency"],
            "estimated_lead_value": o["estimated_lead_value"],
            "recommended_capture_page": o["recommended_capture_page"],
            "opportunity_id": o["opportunity_id"],
        }
        for o in hot
    ]


def _wrap(payload, kind, count_key):
    return {
        "schema_version": "1.0.0",
        "exported_at": _now_iso(),
        "source": "marko",
        "kind": kind,
        count_key: len(payload),
        kind: payload,
    }


def write_all(zones=None, movers=None):
    """End-to-end derive + write. Returns the three payloads for inspection."""
    if zones is None:
        zones = _load_zone_export().get("leads", [])
    if movers is None:
        movers = routing.load_movers()
    opps = derive_demand_opportunities(zones)
    routing_ready = derive_routing_ready(opps, movers=movers)
    hot = derive_hot_zips(opps)

    storage.write_json(DEMAND_FILE, _wrap(opps, "opportunities", "opportunity_count"))
    storage.write_json(
        ROUTING_READY_FILE,
        _wrap(routing_ready, "routing_previews", "preview_count"),
    )
    storage.write_json(HOT_ZIPS_FILE, _wrap(hot, "hot_zips", "hot_zip_count"))
    return {
        "opportunities": opps,
        "routing_ready": routing_ready,
        "hot_zips": hot,
    }


if __name__ == "__main__":
    out = write_all()
    print(json.dumps({
        "opportunities": len(out["opportunities"]),
        "routing_ready": len(out["routing_ready"]),
        "hot_zips": len(out["hot_zips"]),
        "files": [
            os.path.relpath(DEMAND_FILE, BASE_DIR),
            os.path.relpath(ROUTING_READY_FILE, BASE_DIR),
            os.path.relpath(HOT_ZIPS_FILE, BASE_DIR),
        ],
    }, indent=2))
