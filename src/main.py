import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.auth import authenticate_officer, create_access_token, get_current_officer
from src.broadcast import get_provider
from src.confidence import alert_tier, apply_vote, compute_initial_confidence, count_nearby_reports
from src.database import get_connection, init_db
from src.geo import haversine_km
from src.risk_surface import point_risk
from src.routing import find_safe_route
from src.schemas import (
    BroadcastRequest,
    BroadcastResponse,
    BulkReportRequest,
    IncidentOut,
    IncidentReport,
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

def _insert_incident(source: str, report: IncidentReport) -> int:
    _, severity = point_risk(report.latitude, report.longitude, report.type)
    incident_type = report.type.strip().lower()

    conn = get_connection()
    # Corroboration signal computed BEFORE inserting this report, so it
    # doesn't count itself.
    nearby = count_nearby_reports(conn, report.latitude, report.longitude, incident_type, report.timestamp)
    confidence = compute_initial_confidence(source, incident_type, report.has_evidence, nearby)
    tier = alert_tier(confidence, incident_type)

    cursor = conn.execute(
        """INSERT INTO incidents
           (source, type, area, latitude, longitude, description, predicted_severity, timestamp,
            visibility, has_evidence, confidence_score, corroboration_count, alert_tier)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, incident_type, report.area.strip(),
         report.latitude, report.longitude, report.description, severity, report.timestamp,
         report.visibility, int(report.has_evidence), confidence, 0, tier),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


@app.post("/report/citizen")
@limiter.limit("10/minute")
def report_citizen(request: Request, report: IncidentReport):
    incident_id = _insert_incident("citizen", report)
    return {"status": "received", "incident_id": incident_id}


@app.post("/report/officer")
def report_officer(report: IncidentReport, officer: str = Depends(get_current_officer)):
    incident_id = _insert_incident("officer", report)
    return {"status": "received", "incident_id": incident_id, "logged_by": officer}


@app.post("/report/bulk")
def report_bulk(request: BulkReportRequest, officer: str = Depends(get_current_officer)):
    ids = [_insert_incident("bulk", r) for r in request.reports]
    return {"status": "received", "count": len(ids), "incident_ids": ids, "logged_by": officer}


@app.post("/report/{incident_id}/vote", response_model=IncidentOut)
@limiter.limit("20/minute")
def vote_on_incident(incident_id: int, request: Request, vote: VoteRequest):
    """
    Crowd corroboration: anyone can confirm or dispute an existing report.
    This is the mechanism that raises (or lowers) confidence after the
    initial report - a report doesn't just sit at its starting score, it
    moves as real people weigh in. No identity is collected here either, by
    the same reasoning as reports themselves - voting shouldn't expose who
    voted any more than reporting exposes who reported.
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
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
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM incidents WHERE alert_tier IN ('sms_recommended', 'critical') "
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

    return BroadcastResponse(
        broadcast_id=broadcast_id,
        recipients_reached=len(targets),
        radius_km=request.radius_km,
        message=request.message,
    )


@app.get("/alert/broadcasts")
def list_broadcasts(officer: str = Depends(get_current_officer)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
