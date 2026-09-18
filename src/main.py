import json
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.auth import authenticate_officer, create_access_token, get_current_officer
from src.broadcast import get_provider
from src.confidence import alert_tier, apply_vote, is_within_known_flood_zone
from src.database import get_connection, init_db
from src.geo import haversine_km
from src.reflex import reflex_assessment
from src.risk_surface import point_risk
from src.routing import find_safe_route
from src.strategic import refine_incident
from src.schemas import (
    BroadcastRequest,
    BroadcastResponse,
    BulkReportRequest,
    HealthFacilityOut,
    IncidentOut,
    IncidentReport,
    MissingChildCaseOfficerOut,
    MissingChildCaseOut,
    MissingChildReport,
    MissingChildStatusUpdate,
    PredictionRequest,
    PredictionResponse,
    SafeRouteRequest,
    SafeRouteResponse,
    SubscriberIn,
    VoteRequest,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Emergency Intelligence System", version="0.1.0", lifespan=lifespan)

# Public, unauthenticated endpoints (citizen reports, subscribing, voting)
# are the system's real attack surface - anyone can call them, and a report
# can trigger a real SMS broadcast downstream. Rate limiting is the cheap,
# scrappy first line of defense against spam and fabricated-panic flooding;
# per-IP is a known-weak signal (shared NAT, mobile carriers) but a real
# improvement over no limit at all, and free to run.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"message": "Emergency Intelligence System backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


# -------------------------------------------------------------------
# Auth
# -------------------------------------------------------------------

@app.post("/token")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    officer = authenticate_officer(form_data.username, form_data.password)
    if officer is None:
        raise HTTPException(status_code=400, detail="Invalid credentials")
    token = create_access_token(subject=officer["username"])
    return {"access_token": token, "token_type": "bearer"}


# -------------------------------------------------------------------
# Prediction
# -------------------------------------------------------------------

@app.post("/predict", response_model=PredictionResponse)
@limiter.limit("30/minute")
def predict(request: Request, body: PredictionRequest):
    """
    Risk at any point on the map - not restricted to a fixed list of named
    areas. Real incidents happen anywhere, anytime, and possibly several at
    once; this reads the same continuous, recency-weighted spatial surface
    that /route/safe uses, filtered to the queried incident type's category.
    """
    score, severity = point_risk(body.latitude, body.longitude, body.type)
    return PredictionResponse(
        latitude=body.latitude,
        longitude=body.longitude,
        type=body.type,
        risk_score=round(score, 3),
        predicted_severity=severity,
        message=f"Predicted {body.type} risk at this location is {severity}",
    )


# -------------------------------------------------------------------
# Safe routing
# -------------------------------------------------------------------

@app.post("/route/safe", response_model=SafeRouteResponse)
@limiter.limit("20/minute")
def safe_route(request: Request, body: SafeRouteRequest):
    result = find_safe_route(body.latitude, body.longitude, body.risk_aversion)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No safe zone found nearby - location may be outside the covered area.",
        )
    return result


# -------------------------------------------------------------------
# Incident ingestion (citizen reports are public; officer/bulk require auth)
# -------------------------------------------------------------------

def _insert_incident(source: str, report: IncidentReport, background_tasks: BackgroundTasks) -> int:
    """
    Reflex-first ingestion: an instant, bounded-cost assessment (source
    trust, evidence, known-flood-zone check - see src/reflex.py) is what
    actually gets inserted and returned to the reporter. The slower,
    fuller pass - nearby-report corroboration and risk-grid severity, both
    of which scale with data volume - runs afterward as a background task
    (src/strategic.py) and updates the row moments later. A reporter's
    "received" response no longer waits on a full grid rebuild or table
    scan; see docs/architecture.md for the reasoning and the R&D behind it.
    """
    incident_type = report.type.strip().lower()
    conn = get_connection()
    flood_zone_hit = is_within_known_flood_zone(conn, incident_type, report.latitude, report.longitude)
    confidence, tier = reflex_assessment(source, incident_type, report.has_evidence, flood_zone_hit)

    cursor = conn.execute(
        """INSERT INTO incidents
           (source, type, area, latitude, longitude, description, predicted_severity, timestamp,
            visibility, has_evidence, confidence_score, corroboration_count, alert_tier)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
        (source, incident_type, report.area.strip(),
         report.latitude, report.longitude, report.description, report.timestamp,
         report.visibility, int(report.has_evidence), confidence, 0, tier),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()

    background_tasks.add_task(
        refine_incident, new_id, source, incident_type,
        report.latitude, report.longitude, report.timestamp, report.has_evidence,
    )
    return new_id


@app.post("/report/citizen")
@limiter.limit("10/minute")
def report_citizen(request: Request, report: IncidentReport, background_tasks: BackgroundTasks):
    incident_id = _insert_incident("citizen", report, background_tasks)
    return {"status": "received", "incident_id": incident_id}


@app.post("/report/officer")
def report_officer(report: IncidentReport, background_tasks: BackgroundTasks, officer: str = Depends(get_current_officer)):
    incident_id = _insert_incident("officer", report, background_tasks)
    return {"status": "received", "incident_id": incident_id, "logged_by": officer}


@app.post("/report/bulk")
def report_bulk(request: BulkReportRequest, background_tasks: BackgroundTasks, officer: str = Depends(get_current_officer)):
    ids = [_insert_incident("bulk", r, background_tasks) for r in request.reports]
    return {"status": "received", "count": len(ids), "incident_ids": ids, "logged_by": officer}


@app.post("/report/{incident_id}/vote", response_model=IncidentOut)
@limiter.limit("20/minute")
def vote_on_incident(incident_id: int, request: Request, vote: VoteRequest):
    """
    Crowd corroboration: anyone can confirm or dispute an existing PUBLIC
    report. This is the mechanism that raises (or lowers) confidence after
    the initial report - a report doesn't just sit at its starting score,
    it moves as real people weigh in. No identity is collected here either,
    by the same reasoning as reports themselves - voting shouldn't expose
    who voted any more than reporting exposes who reported.

    officers_only reports are excluded entirely, not just filtered from the
    response - this endpoint used to fetch by bare incident_id with no
    visibility check, and returned the full record either way. Since
    incident IDs are small sequential integers, that made visibility
    scoping trivially bypassable: enumerate IDs, call vote, read back the
    exact lat/lon and description of a report a witness deliberately kept
    off the public feed to stay safe. Fixed by treating an officers_only
    incident exactly like a nonexistent one here (404, not 403) - a 403
    would itself confirm something restricted exists at that ID.
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None or row["visibility"] == "officers_only":
        conn.close()
        raise HTTPException(status_code=404, detail="Incident not found")

    new_confidence = apply_vote(row["confidence_score"], row["type"], vote.confirm)
    new_tier = alert_tier(new_confidence, row["type"])
    new_corroboration = row["corroboration_count"] + (1 if vote.confirm else 0)

    conn.execute(
        "UPDATE incidents SET confidence_score = ?, alert_tier = ?, corroboration_count = ? WHERE id = ?",
        (new_confidence, new_tier, new_corroboration, incident_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    conn.close()
    return IncidentOut(**dict(updated))


@app.get("/incidents", response_model=list[IncidentOut])
def list_incidents(limit: int = 100):
    """
    Public feed. Deliberately excludes visibility='officers_only' reports -
    those exist so a witness to something dangerous (e.g. a crime in
    progress) can reach responders without immediately broadcasting their
    report - and by extension, their position - to everyone, including
    whoever they just reported. See /incidents/all for the officer view.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM incidents WHERE visibility = 'public' ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [IncidentOut(**dict(row)) for row in rows]


@app.get("/incidents/all", response_model=list[IncidentOut])
def list_incidents_all(limit: int = 100, officer: str = Depends(get_current_officer)):
    """Officer view: includes officers_only reports. Still never exposes a
    reporter identity, because none is ever collected in the first place."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [IncidentOut(**dict(row)) for row in rows]


@app.get("/alert/recommended", response_model=list[IncidentOut])
def list_recommended_alerts(officer: str = Depends(get_current_officer)):
    """
    Incidents whose confidence has crossed the sms_recommended or critical
    threshold for their category - i.e. the system's recommendation of what
    an officer should consider broadcasting via /alert/broadcast. This is
    surfaced, not auto-fired: a mass SMS is still a human decision (see
    docs/architecture.md for why confidence-driven auto-broadcast was
    deliberately not built yet).

    Deliberately requires scoring_stage='strategic': the reflex layer's
    instant tier (src/reflex.py) is a provisional, pre-corroboration guess
    - it's fine as an internal acknowledgment signal, but it shouldn't by
    itself be able to assert "worth an officer's attention as broadcast-
    worthy" to a human decision-maker. Only the fully-refined strategic
    assessment earns a place on this list. In practice the gap between
    reflex and strategic is small (milliseconds to a few hundred ms), so
    this mostly excludes nothing - but it exists so a stuck/failed
    strategic pass (scoring_stage='failed') can never silently masquerade
    as a real recommendation either.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM incidents WHERE alert_tier IN ('sms_recommended', 'critical') "
        "AND scoring_stage = 'strategic' "
        "ORDER BY confidence_score DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return [IncidentOut(**dict(row)) for row in rows]


# -------------------------------------------------------------------
# Surjection: geo-targeted broadcast alerts
# -------------------------------------------------------------------

@app.post("/subscribers")
@limiter.limit("10/minute")
def add_subscriber(request: Request, sub: SubscriberIn):
    """Anyone can opt in to receive area alerts - no auth required to subscribe."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO subscribers (phone_number, area, latitude, longitude) VALUES (?, ?, ?, ?)",
        (sub.phone_number, sub.area, sub.latitude, sub.longitude),
    )
    conn.commit()
    conn.close()
    return {"status": "subscribed"}


@app.post("/alert/broadcast", response_model=BroadcastResponse)
def trigger_broadcast(request: BroadcastRequest, officer: str = Depends(get_current_officer)):
    """
    Officer-only, deliberately: a mass alert is a consequential action and
    needs a human decision behind it, not an automatic trigger off a
    severity score. Geo-targets every subscriber within radius_km of the
    incident and sends through whatever BroadcastProvider is configured.

    visibility='officers_only' does NOT block a broadcast here - that's a
    deliberate answer to a question that was left open during design: does
    a reporter choosing officers_only permanently cap how public a report
    can go, or just delay it until an officer has had time to act? This
    says delay, not cap - officers_only exists to protect a witness in the
    window before responders can act, not to silence a real, confirmed
    danger forever. What it can't do is protect against the officer's own
    broadcast message re-exposing the original reporter through over-
    specific detail (exact time/vantage point, distinctive circumstances) -
    that's not something code can enforce on free text, so it's surfaced as
    a warning instead.
    """
    conn = get_connection()
    incident = conn.execute(
        "SELECT * FROM incidents WHERE id = ?", (request.incident_id,)
    ).fetchone()
    if incident is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident["latitude"] is None or incident["longitude"] is None:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="Incident has no location on file - cannot geo-target a broadcast",
        )

    subscribers = conn.execute("SELECT * FROM subscribers").fetchall()
    targets = [
        s for s in subscribers
        if haversine_km(incident["latitude"], incident["longitude"], s["latitude"], s["longitude"])
        <= request.radius_km
    ]

    provider = get_provider()
    for s in targets:
        provider.send(s["phone_number"], request.message)

    cursor = conn.execute(
        "INSERT INTO broadcasts (incident_id, message, radius_km, recipient_count, triggered_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (request.incident_id, request.message, request.radius_km, len(targets), officer),
    )
    conn.commit()
    broadcast_id = cursor.lastrowid
    conn.close()

    warning = None
    if incident["visibility"] == "officers_only":
        warning = (
            "This report was originally officers_only - the reporter chose that "
            "to stay safe. Make sure your broadcast message doesn't include "
            "enough specific detail (exact time, vantage point, distinctive "
            "circumstances) to let someone work out who reported it."
        )

    return BroadcastResponse(
        broadcast_id=broadcast_id,
        recipients_reached=len(targets),
        radius_km=request.radius_km,
        message=request.message,
        reporter_safety_warning=warning,
    )


@app.get("/alert/broadcasts")
def list_broadcasts(officer: str = Depends(get_current_officer)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# -------------------------------------------------------------------
# Health facilities - "route to help" for medical incidents, distinct from
# risk-avoidance safe routing (which answers "route away from danger").
# See scripts/ingest_health_facilities.py.
# -------------------------------------------------------------------

@app.get("/facilities/nearest", response_model=list[HealthFacilityOut])
@limiter.limit("30/minute")
def nearest_facilities(request: Request, latitude: float, longitude: float,
                        limit: int = 5, emergency_only: bool = False):
    conn = get_connection()
    query = "SELECT * FROM health_facilities"
    if emergency_only:
        query += " WHERE has_emergency = 'yes'"
    rows = conn.execute(query).fetchall()
    conn.close()

    ranked = sorted(
        (dict(r) | {"distance_km": round(haversine_km(latitude, longitude, r["latitude"], r["longitude"]), 2)}
         for r in rows),
        key=lambda r: r["distance_km"],
    )
    return ranked[:limit]


@app.get("/facilities", response_model=list[HealthFacilityOut])
def list_facilities(limit: int = 2000):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM health_facilities LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [HealthFacilityOut(**dict(r)) for r in rows]


# -------------------------------------------------------------------
# Missing Child Alert - its own table and trust model, not a variant of
# incident reporting. Every report starts hidden (status='reported') and
# requires explicit officer verification before any public visibility -
# the opposite default from flood/fire, given the real risk of a
# weaponized report (custody disputes, harassment) and the legal weight of
# a minor's data. reporter_phone is collected (unlike every other report
# type) because this is an investigation, not a risk signal, and is never
# exposed on the public endpoints below.
# -------------------------------------------------------------------

@app.post("/missing-child/report")
@limiter.limit("10/minute")
def report_missing_child(request: Request, report: MissingChildReport):
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO missing_child_cases
           (child_name, age, physical_description, clothing_description, last_seen_latitude,
            last_seen_longitude, last_seen_area, last_seen_time, has_photo, reporter_phone,
            reporter_relationship)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (report.child_name.strip(), report.age, report.physical_description.strip(),
         report.clothing_description.strip(), report.last_seen_latitude, report.last_seen_longitude,
         report.last_seen_area.strip(), report.last_seen_time, int(report.has_photo),
         report.reporter_phone.strip(), report.reporter_relationship.strip()),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return {"status": "received", "case_id": new_id}


@app.get("/missing-child/cases", response_model=list[MissingChildCaseOut])
def list_missing_child_cases():
    """Public feed - only verified cases (an active search worth the public
    knowing about) and found_safe resolutions (good news, appropriate to
    share). Never 'reported' (unverified), 'found_deceased' (needs a human-
    mediated, not automated, channel), or 'closed_false_report' (no reason
    to have ever surfaced it, and no reason to publicly flag a false
    report against whoever filed it)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM missing_child_cases WHERE status IN ('verified', 'found_safe') "
        "ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [MissingChildCaseOut(**dict(r)) for r in rows]


@app.get("/missing-child/cases/all", response_model=list[MissingChildCaseOfficerOut])
def list_missing_child_cases_all(officer: str = Depends(get_current_officer)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM missing_child_cases ORDER BY created_at DESC").fetchall()
    conn.close()
    return [MissingChildCaseOfficerOut(**dict(r)) for r in rows]


@app.post("/missing-child/cases/{case_id}/verify", response_model=MissingChildCaseOfficerOut)
def verify_missing_child_case(case_id: int, officer: str = Depends(get_current_officer)):
    conn = get_connection()
    row = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Case not found")
    if row["status"] != "reported":
        conn.close()
        raise HTTPException(status_code=400, detail=f"Case is already '{row['status']}', not pending verification")

    conn.execute(
        "UPDATE missing_child_cases SET status = 'verified', verified_by = ?, updated_at = datetime('now') WHERE id = ?",
        (officer, case_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (case_id,)).fetchone()
    conn.close()
    return MissingChildCaseOfficerOut(**dict(updated))


@app.post("/missing-child/cases/{case_id}/status", response_model=MissingChildCaseOfficerOut)
def update_missing_child_case_status(case_id: int, update: MissingChildStatusUpdate,
                                      officer: str = Depends(get_current_officer)):
    conn = get_connection()
    row = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Case not found")
    if row["status"] in ("found_safe", "found_deceased", "closed_false_report"):
        conn.close()
        raise HTTPException(status_code=400, detail=f"Case is already resolved as '{row['status']}'")

    conn.execute(
        "UPDATE missing_child_cases SET status = ?, verified_by = COALESCE(verified_by, ?), "
        "updated_at = datetime('now') WHERE id = ?",
        (update.status, officer, case_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (case_id,)).fetchone()
    conn.close()
    return MissingChildCaseOfficerOut(**dict(updated))


# -------------------------------------------------------------------
# Verified external events (GDACS, etc.) - corroboration, not training data
# -------------------------------------------------------------------

@app.get("/events/external")
def list_external_events():
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM external_events ORDER BY from_date DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/events/flood-extents")
def list_flood_extents():
    """Real, satellite-mapped flood boundaries (UNOSAT). See scripts/ingest_unosat_flood.py."""
    conn = get_connection()
    rows = conn.execute("SELECT event_code, region, geojson, source_date FROM flood_extents").fetchall()
    conn.close()
    return [
        {"event_code": r["event_code"], "region": r["region"],
         "source_date": r["source_date"], "geometry": json.loads(r["geojson"])}
        for r in rows
    ]


@app.get("/events/affected-structures-summary")
def affected_structures_summary():
    """Real, satellite-verified counts of flood-damaged structures per area (UNOSAT)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT event_code, area, structure_count FROM affected_structures_summary ORDER BY structure_count DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# -------------------------------------------------------------------
# Static dashboard
# -------------------------------------------------------------------

app.mount("/dashboard", StaticFiles(directory="static", html=True), name="dashboard")
