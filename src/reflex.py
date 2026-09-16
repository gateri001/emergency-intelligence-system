"""
Reflex layer: instant, bounded-cost assessment of an incoming report - no
table scan, no risk-grid rebuild. Mirrors the reflex/strategic split
validated in Triagia's core-engine R&D (Crafter prototype, n=10):
hierarchical matched monolithic on task performance with ~12x fewer
expensive calls and zero missed hazards, versus monolithic's measured
~4-tick average latency and 10/48 missed hazard events.

This isn't a blind copy of that result - it's drawn along EIS's actual
cost boundary, which is different from Crafter's. What's cheap and
bounded lives here; what grows with data volume is deferred to
src/strategic.py:
- Cheap and bounded (reflex): source trust, self-reported evidence, and
  whether a flood report sits near a known historical flood zone -
  bounded by the number of ingested disaster events (currently one),
  not the number of reports.
- Grows with data volume (strategic, deferred): nearby-report
  corroboration, which scans every report of that type ever filed, and
  risk-surface severity, which rebuilds a grid weighted by every
  historical point. Both get slower as real usage accumulates, not
  faster - exactly the "monolithic" cost the R&D measured, and exactly
  what a reporter would otherwise wait on synchronously before getting
  a response.
"""
from src.confidence import (
    CITIZEN_CATEGORY_BASE,
    DEFAULT_CITIZEN_BASE,
    EVIDENCE_BONUS,
    HISTORICAL_GROUND_TRUTH_BONUS,
    SOURCE_BASE_CONFIDENCE,
    alert_tier,
    category_for,
)


def reflex_confidence(source: str, incident_type: str, has_evidence: bool, flood_zone_hit: bool) -> float:
    """Everything compute_initial_confidence() can know without a table
    scan or a grid rebuild. Deliberately omits the nearby-report
    corroboration bonus - the one signal that requires scanning the
    incidents table, and the reason a full strategic pass still happens
    afterward (src/strategic.py) rather than this being the whole story."""
    category = category_for(incident_type)
    if source in SOURCE_BASE_CONFIDENCE:
        score = SOURCE_BASE_CONFIDENCE[source]
    else:
        score = CITIZEN_CATEGORY_BASE.get(category, DEFAULT_CITIZEN_BASE)
    if has_evidence:
        score += EVIDENCE_BONUS
    if flood_zone_hit:
        score += HISTORICAL_GROUND_TRUTH_BONUS
    return round(min(score, 1.0), 3)


def reflex_assessment(source: str, incident_type: str, has_evidence: bool, flood_zone_hit: bool):
    """(confidence, tier) - the instant, pre-corroboration read on a
    report. A reporter gets this immediately; src/strategic.py refines it
    moments later, in the background, once the slower signals are in."""
    confidence = reflex_confidence(source, incident_type, has_evidence, flood_zone_hit)
    return confidence, alert_tier(confidence, incident_type)
