"""
Strategic layer: the slower, fuller refinement of a report's confidence,
run after the reflex layer (src/reflex.py) has already given the reporter
an instant response. Adds the one signal reflex can't compute cheaply -
nearby-report corroboration, which scans the incidents table - and the
risk-surface severity, which rebuilds the national risk grid.

Runs as a FastAPI BackgroundTask (wired in src/main.py) - after the
response, not before. No new infrastructure: Starlette's BackgroundTasks
run in-process, which is the right amount of machinery for this system's
actual scale right now. If ingestion volume ever outgrows a single
process, this is the seam to swap in a real task queue without touching
the reflex layer at all - reflex doesn't know or care how strategic gets
run, only that it eventually does.
"""
from src.confidence import alert_tier, compute_initial_confidence, count_nearby_reports, is_within_known_flood_zone
from src.database import get_connection
from src.risk_surface import point_risk


def refine_incident(incident_id: int, source: str, incident_type: str, latitude: float, longitude: float,
                     timestamp: str, has_evidence: bool) -> None:
    """Recomputes confidence/tier/severity using the expensive signals the
    reflex layer skipped, then updates the stored row. Opens its own
    connection deliberately - this runs after the original request's
    connection has already closed."""
    conn = get_connection()
    try:
        nearby = count_nearby_reports(
            conn, latitude, longitude, incident_type, timestamp, exclude_id=incident_id
        )
        ground_truth = is_within_known_flood_zone(conn, incident_type, latitude, longitude)
        confidence = compute_initial_confidence(source, incident_type, has_evidence, nearby, ground_truth)
        tier = alert_tier(confidence, incident_type)
        _, severity = point_risk(latitude, longitude, incident_type)

        conn.execute(
            "UPDATE incidents SET confidence_score = ?, alert_tier = ?, predicted_severity = ? WHERE id = ?",
            (confidence, tier, severity, incident_id),
        )
        conn.commit()
    finally:
        conn.close()
