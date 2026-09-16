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


def category_for(incident_type: str) -> str:
    return TYPE_CATEGORY.get(incident_type, "hazard")


def compute_initial_confidence(source: str, incident_type: str, has_evidence: bool, nearby_count: int) -> float:
    category = category_for(incident_type)
    if source in SOURCE_BASE_CONFIDENCE:
        base = SOURCE_BASE_CONFIDENCE[source]
    else:
        base = CITIZEN_CATEGORY_BASE.get(category, DEFAULT_CITIZEN_BASE)

    score = base
    if has_evidence:
        score += EVIDENCE_BONUS
    score += min(nearby_count * NEARBY_REPORT_BONUS, MAX_NEARBY_BONUS)
    return round(min(score, 1.0), 3)


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
                          exclude_source: str | None = None) -> int:
    """Independent reports of the same type, close in space and time - the
    corroboration signal used at report-creation time. Distance is computed
    in Python (haversine) rather than SQL since sqlite has no geo functions;
    the incidents table is small enough for this MVP scale.

    `exclude_source`: a single real fire lights up several adjacent pixels
    in one satellite pass - those aren't independent corroboration the way
    two different people separately reporting the same fire would be, just
    spatial resolution of one detection. Bulk/sensor ingestion should pass
    its own source here so it only gets a corroboration bonus from a
    genuinely different source (a citizen or officer report), not from
    counting itself several times over. Citizen/officer reports don't need
    this - two different citizens reporting the same fire IS real
    corroboration even though they share a source type."""
    from src.geo import haversine_km

    try:
        ref_time = datetime.strptime(timestamp, "%Y-%m-%d %H:%M")
    except ValueError:
        ref_time = datetime.now()

    rows = conn.execute(
        "SELECT latitude, longitude, timestamp FROM incidents "
        "WHERE type = ? AND latitude IS NOT NULL AND longitude IS NOT NULL"
        + (" AND source != ?" if exclude_source else ""),
        (incident_type, exclude_source) if exclude_source else (incident_type,),
    ).fetchall()

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
