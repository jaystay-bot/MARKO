"""MARKO V1 MoveSignalZone scoring function (N4).

Pure function. No I/O, no globals, no randomness. Given a zone's raw
signal inputs and the mover's base context, produces:

  - move_activity_score: int 0-100
  - urgency: call_today | follow_up | low_priority
  - confidence: low | medium | high

Component breakdown (locked in V1 plan §3):

  freshness         0-25
  proximity         0-20
  signal_strength   0-25
  mover_relevance   0-20
  confidence_safety 0-10
  ----------------------
  total             0-100

Scoring is deliberately additive and explainable. No ML, no opaque
weights -- a mover who asks "why is this a call_today?" can be told
the exact component contributions.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

CONSTRUCTION_LIKE = {"construction_surge", "moved_in_trend"}
MOVE_DIRECT = {"moved_in_trend", "moved_out_trend"}
SEASONAL = {"seasonal_window"}


def _haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.7613
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(a))


def freshness_component(freshness_days):
    if freshness_days < 14:
        return 25
    if freshness_days < 45:
        return 15
    if freshness_days < 180:
        return 5
    return 0


def proximity_component(distance_miles):
    if distance_miles < 15:
        return 20
    if distance_miles < 35:
        return 15
    if distance_miles < 50:
        return 10
    if distance_miles <= 75:
        return 5
    return 0


def signal_strength_component(signal_strength):
    # signal_strength is the source-normalized 0-100 magnitude on the
    # zone record itself. Scale linearly into the 0-25 band.
    s = max(0, min(100, signal_strength))
    return round(s * 25 / 100)


def mover_relevance_component(signal_type, in_seasonal_window=False):
    if signal_type in MOVE_DIRECT:
        base = 20
    elif signal_type == "turnover":
        base = 12
    elif signal_type in CONSTRUCTION_LIKE:
        base = 10
    elif signal_type in SEASONAL:
        base = 8
    else:
        base = 0
    if in_seasonal_window and signal_type not in SEASONAL:
        base = min(20, base + 5)
    return base


def confidence_safety_component(source_type, corroborating_sources=0):
    if source_type == "composite" or corroborating_sources >= 2:
        return 10
    if source_type in ("permits", "vacancy") and corroborating_sources >= 1:
        return 9
    if source_type in ("permits", "vacancy"):
        return 7
    if source_type == "acs":
        return 6
    if source_type == "calendar":
        return 4
    return 0


def derive_urgency(score):
    if score >= 80:
        return "call_today"
    if score >= 60:
        return "follow_up"
    return "low_priority"


def derive_confidence(source_type, corroborating_sources=0):
    if source_type == "composite" or corroborating_sources >= 2:
        return "high"
    if source_type in ("permits", "vacancy"):
        return "medium"
    return "low"


def score_zone(
    *,
    freshness_days,
    distance_miles,
    signal_strength,
    signal_type,
    source_type,
    corroborating_sources=0,
    in_seasonal_window=False,
):
    """Compute (score, urgency, confidence, breakdown).

    All inputs are plain numbers / strings; the function does not
    require a full MoveSignalZone object. This keeps it trivially
    testable and re-usable from the mapper (N5).
    """
    parts = {
        "freshness": freshness_component(freshness_days),
        "proximity": proximity_component(distance_miles),
        "signal_strength": signal_strength_component(signal_strength),
        "mover_relevance": mover_relevance_component(signal_type, in_seasonal_window),
        "confidence_safety": confidence_safety_component(source_type, corroborating_sources),
    }
    score = sum(parts.values())
    return {
        "move_activity_score": score,
        "urgency": derive_urgency(score),
        "confidence": derive_confidence(source_type, corroborating_sources),
        "breakdown": parts,
    }


def distance_between(zone, mover_lat, mover_lon):
    """Helper: zone-to-mover great-circle distance, miles."""
    return _haversine_miles(zone["latitude"], zone["longitude"], mover_lat, mover_lon)
