"""
Confidence scoring: how much the system trusts an incident report, and what
that earns it - an in-app-only notice to the immediate area, an
officer-visible "worth an SMS broadcast" recommendation, or a critical flag.

Deliberately type-specific, not one universal formula (see docs/architecture.md
for the reasoning): a flood report has low malice-motive - nobody fabricates
a flood for personal gain - so a single citizen report starts with real trust
and needs little corroboration to justify wider warning. A crime accusation
carries real cost if wrong (reputational harm, retaliation risk against an
accused party), so it starts more skeptical and needs more independent
corroboration before the system treats it as SMS-worthy.

This is a scrappy first version: real signals (source trust, self-reported
evidence, independent nearby reports, explicit community votes), no live
satellite cross-referencing or ML calibration yet - that's flagged as a
follow-up in the architecture doc, not silently skipped.

Auto-escalation is a RECOMMENDATION, not an automatic trigger: the system
computes and surfaces `alert_tier`, but sending an actual SMS broadcast stays
behind the existing officer-gated /alert/broadcast endpoint. Wiring a new,
unvalidated scrappy heuristic directly to mass-message real phone numbers is
a real liability and trust risk with no funding or legal backing behind it
yet - flagged as a deliberate choice, not an oversight.
"""
import json
from datetime import datetime

from src.risk_surface import TYPE_CATEGORY

# Confidence a single report starts at, before any corroboration - trusted
# sources (an officer on the ground, or a bulk-ingested verified sensor feed
# like NASA FIRMS) don't need corroboration to be believed.
SOURCE_BASE_CONFIDENCE = {"officer": 0.9, "bulk": 0.8}

# For citizen reports specifically: starting trust by hazard category,
# reflecting how costly a false positive is for that category.
CITIZEN_CATEGORY_BASE = {"hazard": 0.35, "medical": 0.35, "crime": 0.15}
DEFAULT_CITIZEN_BASE = 0.20

EVIDENCE_BONUS = 0.15
NEARBY_REPORT_BONUS = 0.20  # per independent corroborating report, capped below
MAX_NEARBY_BONUS = 0.40
HISTORICAL_GROUND_TRUTH_BONUS = 0.20  # report falls inside a real, satellite-verified past flood zone

VOTE_CONFIRM_BONUS = 0.15
VOTE_DISPUTE_PENALTY = 0.20

# (sms_recommended threshold, critical threshold) per category. Flood/medical
# use the illustrative 30%/60% split from early design discussion; crime is
# set higher, matching the "don't cry wolf, and don't expose an accused
# person on a single unproven report" reasoning.
TIER_THRESHOLDS = {
    "hazard": (0.30, 0.60),
    "medical": (0.30, 0.60),
    "crime": (0.50, 0.80),
}
DEFAULT_THRESHOLDS = (0.40, 0.70)

NEARBY_RADIUS_KM = 2.0
NEARBY_WINDOW_HOURS = 6.0


# Satellite fire urgency. Confidence answers "is this real" (0.8 for a satellite
# hit, always); URGENCY is a separate question, so a satellite fire's tier comes
# from its fire radiative power (MW), not from confidence - otherwise every one
# of ~150 detections a day was `critical`. These two numbers are JUDGEMENT
# CALLS, not validated against ground truth (typical savanna / agricultural-burn
# pixels are ~10-50 MW; big wildfire pixels 100+): tune with real feedback.
# Not modelled, and worth more than either number: proximity to people.
FIRE_FRP_SMS_MW = 25.0
FIRE_FRP_CRITICAL_MW = 100.0
_TIER_ORDER = ["in_app", "sms_recommended", "critical"]


def fire_detection_tier(frp_mw: float, corroborated: bool) -> str:
    """Tier for a satellite fire detection from its radiative power; a human
    report of fire nearby (or a confirm vote) bumps it up one level."""
    if frp_mw >= FIRE_FRP_CRITICAL_MW:
        level = 2
    elif frp_mw >= FIRE_FRP_SMS_MW:
        level = 1
    else:
        level = 0
    if corroborated:
        level = min(level + 1, 2)
    return _TIER_ORDER[level]


def category_for(incident_type: str) -> str:
    return TYPE_CATEGORY.get(incident_type, "hazard")


def compute_initial_confidence(source: str, incident_type: str, has_evidence: bool, nearby_count: int,
                                historical_ground_truth: bool = False) -> float:
    category = category_for(incident_type)
    if source in SOURCE_BASE_CONFIDENCE:
        base = SOURCE_BASE_CONFIDENCE[source]
    else:
        base = CITIZEN_CATEGORY_BASE.get(category, DEFAULT_CITIZEN_BASE)

    score = base
    if has_evidence:
        score += EVIDENCE_BONUS
    score += min(nearby_count * NEARBY_REPORT_BONUS, MAX_NEARBY_BONUS)
    if historical_ground_truth:
        score += HISTORICAL_GROUND_TRUTH_BONUS
    return round(min(score, 1.0), 3)


FLOOD_ZONE_BUFFER_DEG = 0.01  # ~1.1km - see docstring below for why this isn't strict containment


def is_within_known_flood_zone(conn, incident_type: str, latitude: float, longitude: float) -> bool:
    """True if (lat, lon) is at or near a real, satellite-verified historical
    flood extent (UNOSAT, via scripts/ingest_unosat_flood.py) - only
    meaningful for flood reports; every other type returns False
    immediately.

    Deliberately a small buffer (~1.1km), not strict point-in-polygon
    containment: tested against real data, the reference coordinate for
    Githurai (used elsewhere for area-matching) sits ~73m outside the
    mapped polygon despite being a real, known-flooded location - a
    combination of the reference point being an approximate town-center
    coordinate, not a precise boundary, and the polygon itself being
    simplified for file size (see ingest_unosat_flood.py). Strict
    containment would silently drop real, physically meaningful evidence
    right at the boundary; a small buffer treats "clearly at this mapped
    flood zone" and "exactly inside its simplified outline" as the same
    thing, which they should be."""
    if incident_type != "flood":
        return False

    from shapely.geometry import Point, shape

    rows = conn.execute("SELECT geojson FROM flood_extents").fetchall()
    if not rows:
        return False
    point = Point(longitude, latitude)
    for r in rows:
        try:
            geom = shape(json.loads(r["geojson"]))
        except (ValueError, TypeError):
            continue
        if geom.distance(point) <= FLOOD_ZONE_BUFFER_DEG:
            return True
    return False


def apply_vote(current_confidence: float, incident_type: str, confirm: bool) -> float:
    delta = VOTE_CONFIRM_BONUS if confirm else -VOTE_DISPUTE_PENALTY
    return round(min(max(current_confidence + delta, 0.0), 1.0), 3)


def alert_tier(confidence: float, incident_type: str) -> str:
    category = category_for(incident_type)
    sms_threshold, critical_threshold = TIER_THRESHOLDS.get(category, DEFAULT_THRESHOLDS)
    if confidence >= critical_threshold:
        return "critical"
    if confidence >= sms_threshold:
        return "sms_recommended"
    return "in_app"


def count_nearby_reports(conn, latitude: float, longitude: float, incident_type: str, timestamp: str,
                          radius_km: float = NEARBY_RADIUS_KM, window_hours: float = NEARBY_WINDOW_HOURS,
                          exclude_source: str | None = None, exclude_id: int | None = None) -> int:
    """Independent reports of the same type, close in space and time - the
    corroboration signal used by the strategic layer (src/strategic.py).
    Distance is computed in Python (haversine) rather than SQL since
    sqlite has no geo functions; the incidents table is small enough for
    this MVP scale.

    `exclude_source`: a single real fire lights up several adjacent pixels
    in one satellite pass - those aren't independent corroboration the way
    two different people separately reporting the same fire would be, just
    spatial resolution of one detection. Bulk/sensor ingestion should pass
    its own source here so it only gets a corroboration bonus from a
    genuinely different source (a citizen or officer report), not from
    counting itself several times over. Citizen/officer reports don't need
    this - two different citizens reporting the same fire IS real
    corroboration even though they share a source type.

    `exclude_id`: the strategic layer runs after the report is already
    inserted (see src/strategic.py), unlike the old single-pass pipeline
    which computed this before insert - without excluding the report's own
    row, it would trivially match itself (same location, same timestamp)
    and inflate its own corroboration count by one every time."""
    from src.geo import haversine_km

    try:
        ref_time = datetime.strptime(timestamp, "%Y-%m-%d %H:%M")
    except ValueError:
        ref_time = datetime.now()

    query = "SELECT latitude, longitude, timestamp FROM incidents WHERE type = ? AND latitude IS NOT NULL AND longitude IS NOT NULL"
    params = [incident_type]
    if exclude_source:
        query += " AND source != ?"
        params.append(exclude_source)
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    rows = conn.execute(query, params).fetchall()

    count = 0
    for r in rows:
        try:
            t = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if abs((t - ref_time).total_seconds()) > window_hours * 3600:
            continue
        if haversine_km(latitude, longitude, r["latitude"], r["longitude"]) <= radius_km:
            count += 1
    return count
