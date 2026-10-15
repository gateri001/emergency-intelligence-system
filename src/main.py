import json
import os
from contextlib import asynccontextmanager

from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.auth import (authenticate_officer, create_access_token, get_current_account, get_current_officer,
                      require_role)
from src.broadcast import get_provider, phone_key, select_targets, send_all
from src.confidence import alert_tier, fire_detection_tier, is_within_known_flood_zone, with_votes
from src.database import get_connection, init_db
from src.geo import haversine_km
from src.privacy import (PUBLIC_INCIDENT_SQL, decrypt, encrypt, encryption_available, is_crime, log_action,
                         public_incident)
from src.reflex import reflex_assessment
from src.risk_surface import point_risk, risk_cells
from src.routing import find_safe_route
from src.strategic import refine_incident
from src.schemas import (
    BroadcastRequest,
    BroadcastResponse,
    BulkReportRequest,
    HealthFacilityOut,
    IncidentBroadcastPreview,
    IncidentOut,
    IncidentReport,
    IncidentReviewRequest,
    MissingChildBroadcastPreview,
    MissingChildBroadcastRequest,
    MissingChildBroadcastResponse,
    MissingChildCaseOfficerOut,
    MissingChildCaseOut,
    MissingChildProposalOut,
    MissingChildRenewRequest,
    MissingChildReport,
    MissingChildStatusUpdate,
    MissingChildVerifyRequest,
    NearbyIncident,
    NearbyResponse,
    PredictionRequest,
    PredictionResponse,
    RevealRequest,
    RiskHeatmapResponse,
    SafeRouteRequest,
    SafeRouteResponse,
    SubscriberIn,
    SERVICE_AREA,
    VoteRequest,
    check_in_service_area,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not encryption_available():
        print(
            "WARNING: EIS_DATA_KEY not set - crime reporters' phone numbers will NOT be stored. "
            "Generate one with scripts/generate_keys.py and keep an offline copy."
        )
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

# Auth is a bearer token in the Authorization header - no cookies anywhere -
# so credentialed CORS is unnecessary (and wildcard-origin + credentials is a
# combination worth never having). Public endpoints stay open to any origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


OFFICER_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data: https://*.tile.openstreetmap.org; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)


# Pages built without inline code, so they can take the strict policy. The
# older full map (map.html) still has inline script and is not on this list.
STRICT_CSP_PAGES = {"/dashboard/", "/dashboard/index.html"}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Cheap, standard hardening on every response: stop MIME sniffing, refuse
    to be framed (clickjacking on the officer console), don't leak the URL."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Other sites get our origin only, never the path or query. Not
    # 'no-referrer': OpenStreetMap's tile servers refuse requests without a
    # Referer, which blanked every map (found in a real browser, 2026-10-15).
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path.startswith("/dashboard"):
        # Revalidate on every load (still cheap - ETag/304). Without this the
        # browser heuristically cached the page, so after a security fix a
        # visitor could keep running the OLD, vulnerable dashboard: found when
        # a real browser kept executing an XSS payload the server had already
        # stopped serving code for.
        response.headers.setdefault("Cache-Control", "no-cache")
    if request.url.path in STRICT_CSP_PAGES or request.url.path.startswith("/dashboard/officer"):
        # The console holds the login token (and the app shares its origin), so
        # both get a strict policy: only
        # our own scripts and styles run (no inline code, no CDNs), it talks
        # only to this server, and map tiles come from OpenStreetMap only.
        response.headers.setdefault("Content-Security-Policy", OFFICER_CSP)
    return response


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
@limiter.limit("5/minute")
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    # Rate-limited: without it, an officer password can be guessed as fast
    # as the server answers.
    officer = authenticate_officer(form_data.username, form_data.password)
    if officer is None:
        raise HTTPException(status_code=400, detail="Invalid credentials")
    token = create_access_token(subject=officer["username"])
    return {"access_token": token, "token_type": "bearer"}


@app.get("/me")
def whoami(account: dict = Depends(get_current_account)):
    """The signed-in account's username and role, so the console can show
    only the buttons this role may use. The server still checks every action."""
    return account


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

def _insert_incident(source: str, report: IncidentReport, background_tasks: BackgroundTasks) -> tuple[int, bool]:
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
    # Crime defaults to officers-only: a witness opts IN to warning people
    # nearby, never has to remember to opt out.
    visibility = report.visibility or ("officers_only" if is_crime(incident_type) else "public")
    phone_enc = encrypt(report.reporter_phone) if report.reporter_phone else None

    conn = get_connection()
    flood_zone_hit = is_within_known_flood_zone(conn, incident_type, report.latitude, report.longitude)
    confidence, tier = reflex_assessment(source, incident_type, report.has_evidence, flood_zone_hit)

    cursor = conn.execute(
        """INSERT INTO incidents
           (source, type, area, latitude, longitude, description, predicted_severity, timestamp,
            visibility, has_evidence, base_confidence, confidence_score, corroboration_count, alert_tier,
            reporter_phone_enc)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, incident_type, report.area.strip(),
         report.latitude, report.longitude, report.description, report.timestamp,
         visibility, int(report.has_evidence), confidence, confidence, 0, tier, phone_enc),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()

    background_tasks.add_task(
        refine_incident, new_id, source, incident_type,
        report.latitude, report.longitude, report.timestamp, report.has_evidence,
    )
    return new_id, phone_enc is not None


def _received(incident_id: int, report: IncidentReport, phone_stored: bool, **extra) -> dict:
    out = {"status": "received", "incident_id": incident_id, **extra}
    if report.reporter_phone:
        # Said plainly rather than dropped silently: without EIS_DATA_KEY the
        # number is not kept, and the reporter should know nobody can call back.
        out["phone_stored"] = phone_stored
    return out


@app.post("/report/citizen")
@limiter.limit("10/minute")
def report_citizen(request: Request, report: IncidentReport, background_tasks: BackgroundTasks):
    incident_id, phone_stored = _insert_incident("citizen", report, background_tasks)
    return _received(incident_id, report, phone_stored)


@app.post("/report/officer")
def report_officer(report: IncidentReport, background_tasks: BackgroundTasks, officer: str = Depends(get_current_officer)):
    incident_id, phone_stored = _insert_incident("officer", report, background_tasks)
    return _received(incident_id, report, phone_stored, logged_by=officer)


@app.post("/report/bulk")
def report_bulk(request: BulkReportRequest, background_tasks: BackgroundTasks, officer: str = Depends(get_current_officer)):
    ids = [_insert_incident("bulk", r, background_tasks)[0] for r in request.reports]
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
    # Write transaction from the read onward: two votes arriving together, or
    # a vote racing the strategic pass, must not overwrite each other.
    conn.execute("BEGIN IMMEDIATE")
    # Same rule as the public feed: officers-only rows, and crime rows still
    # inside their delay, look exactly like rows that don't exist.
    row = conn.execute(f"SELECT * FROM incidents WHERE id = ? AND {PUBLIC_INCIDENT_SQL}", (incident_id,)).fetchone()
    if row is None:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=404, detail="Incident not found")

    # Rows from before base_confidence existed: treat their current score as the base.
    base = row["base_confidence"] if row["base_confidence"] is not None else row["confidence_score"]
    confirms = row["vote_confirms"] + (1 if vote.confirm else 0)
    disputes = row["vote_disputes"] + (0 if vote.confirm else 1)
    new_confidence = with_votes(base, confirms, disputes)
    new_corroboration = row["corroboration_count"] + (1 if vote.confirm else 0)
    if row["source"] == "bulk" and row["type"] == "fire" and row["magnitude"] is not None:
        # A satellite fire's tier is its urgency (radiative power), not its
        # confidence (always high): deriving it from confidence here would let
        # one confirm vote jump a weak 10 MW detection straight to critical.
        new_tier = fire_detection_tier(row["magnitude"], corroborated=new_corroboration > 0)
    else:
        new_tier = alert_tier(new_confidence, row["type"])

    conn.execute(
        "UPDATE incidents SET base_confidence = ?, confidence_score = ?, alert_tier = ?, corroboration_count = ?, "
        "vote_confirms = ?, vote_disputes = ? WHERE id = ?",
        (base, new_confidence, new_tier, new_corroboration, confirms, disputes, incident_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    conn.close()
    return IncidentOut(**public_incident(dict(updated)))


@app.get("/incidents", response_model=list[IncidentOut])
def list_incidents(limit: int = Query(100, ge=1, le=500)):
    """
    Public feed. Deliberately excludes visibility='officers_only' reports -
    those exist so a witness to something dangerous (e.g. a crime in
    progress) can reach responders without immediately broadcasting their
    report - and by extension, their position - to everyone, including
    whoever they just reported. See /incidents/all for the officer view.
    Public crime reports also wait out a delay and show a blurred location
    (src/privacy.py).
    """
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM incidents WHERE {PUBLIC_INCIDENT_SQL} ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [IncidentOut(**public_incident(dict(row))) for row in rows]


@app.post("/incidents/{incident_id}/review", response_model=IncidentOut)
def review_incident(incident_id: int, body: IncidentReviewRequest, officer: str = Depends(get_current_officer)):
    """An officer's call on a report. 'dismiss' takes it off the recommended
    list; 'verify' marks it checked and puts it first. Neither changes the
    confidence score, which stays the system's own estimate."""
    status = "verified" if body.action == "verify" else "dismissed"
    conn = get_connection()
    cur = conn.execute("UPDATE incidents SET review_status = ?, reviewed_by = ? WHERE id = ?",
                       (status, officer, incident_id))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Incident not found")
    log_action(conn, officer, f"{body.action}_incident", "incident", incident_id, body.note.strip())
    conn.commit()
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    conn.close()
    return IncidentOut(**dict(row))


@app.post("/incidents/{incident_id}/reveal-reporter")
def reveal_crime_reporter(incident_id: int, body: RevealRequest,
                          account: dict = Depends(require_role("admin"))):
    """Opens a crime reporter's phone number - admin or owner only, with a
    written reason. The opening is logged (who, when, why) in the same
    transaction that reads the number, so there is no unlogged path."""
    conn = get_connection()
    row = conn.execute("SELECT reporter_phone_enc FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None or not row["reporter_phone_enc"]:
        conn.close()
        raise HTTPException(status_code=404, detail="No reporter number on file for this incident")
    try:
        phone = decrypt(row["reporter_phone_enc"])
    except Exception:
        conn.close()
        raise HTTPException(status_code=503, detail="The number can't be opened: EIS_DATA_KEY is missing or wrong")
    log_action(conn, account["username"], "reveal_reporter_phone", "incident", incident_id, body.reason.strip())
    conn.commit()
    conn.close()
    return {"incident_id": incident_id, "reporter_phone": phone}


@app.get("/audit-log")
def read_audit_log(limit: int = Query(200, ge=1, le=1000), account: dict = Depends(require_role("owner"))):
    """Every sensitive action, newest first. Owner only."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


NEARBY_RADIUS_KM = 2.0
NEARBY_DAYS = 7
# One representative type per category: point_risk filters by the type's category.
_CATEGORY_PROBE = {"crime": "robbery", "hazard": "flood", "medical": "medical_emergency"}


@app.get("/nearby", response_model=NearbyResponse)
@limiter.limit("30/minute")
def nearby(request: Request, latitude: float, longitude: float):
    """Everything the "Am I safe here?" screen needs in one small request:
    modelled risk per category at this point, plus public incidents from the
    last week within 2 km (crime blurred and delayed, as everywhere)."""
    try:
        check_in_service_area(latitude, longitude)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    risk = {}
    for category, probe in _CATEGORY_PROBE.items():
        score, severity = point_risk(latitude, longitude, probe)
        risk[category] = {"risk_score": round(score, 3), "severity": severity}

    box = NEARBY_RADIUS_KM / 100  # degrees; a generous prefilter, exact distance below
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM incidents WHERE {PUBLIC_INCIDENT_SQL} "
        f"AND created_at >= datetime('now', '-{NEARBY_DAYS} days') "
        "AND latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?",
        (latitude - box, latitude + box, longitude - box, longitude + box),
    ).fetchall()
    conn.close()
    found = []
    for r in rows:
        p = public_incident(dict(r))
        d = haversine_km(latitude, longitude, p["latitude"], p["longitude"])
        if d <= NEARBY_RADIUS_KM:
            found.append(NearbyIncident(id=p["id"], type=p["type"], latitude=p["latitude"],
                                        longitude=p["longitude"], timestamp=p["timestamp"],
                                        alert_tier=p["alert_tier"], distance_km=round(d, 2)))
    found.sort(key=lambda i: i.distance_km)
    return NearbyResponse(latitude=latitude, longitude=longitude, risk=risk, incidents=found[:50])


@app.get("/incidents/all", response_model=list[IncidentOut])
def list_incidents_all(limit: int = Query(100, ge=1, le=500), officer: str = Depends(get_current_officer)):
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
        # Dismissed reports leave the list; verified ones go first.
        "SELECT * FROM incidents WHERE alert_tier IN ('sms_recommended', 'critical') "
        "AND scoring_stage = 'strategic' AND (review_status IS NULL OR review_status != 'dismissed') "
        "ORDER BY review_status IS NULL, confidence_score DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return [IncidentOut(**dict(row)) for row in rows]


# -------------------------------------------------------------------
# Surjection: geo-targeted broadcast alerts
# -------------------------------------------------------------------

def signup_enabled() -> bool:
    return os.environ.get("EIS_SIGNUP_ENABLED", "0") == "1"


def demo_mode() -> bool:
    # On unless explicitly turned off: the risk baseline is synthetic, and the
    # public must never mistake a demo map for real danger information.
    return os.environ.get("EIS_DEMO_MODE", "1") != "0"


@app.get("/config")
def public_config():
    """What the citizen app needs to know about this deployment."""
    return {"demo_mode": demo_mode(), "signup_enabled": signup_enabled(),
            "emergency_numbers": ALERT_CONTACT}


@app.post("/subscribers")
@limiter.limit("10/minute")
def add_subscriber(request: Request, sub: SubscriberIn):
    """Anyone can opt in to receive area alerts - no auth required to subscribe.

    Upserts by phone number: subscribing again (same person, any phone
    format) updates their location instead of adding a duplicate row - which
    used to mean the same person got the same alert SMS several times.

    OFF unless EIS_SIGNUP_ENABLED=1: nothing yet proves the person owns the
    number (that needs a one-time SMS code, which needs a live SMS provider),
    so a public deployment must not collect numbers by accident."""
    if not signup_enabled():
        raise HTTPException(status_code=503, detail="SMS alert sign-up is not open yet")
    conn = get_connection()
    key = phone_key(sub.phone_number)
    existing = next(
        (r for r in conn.execute("SELECT id, phone_number FROM subscribers").fetchall()
         if phone_key(r["phone_number"]) == key),
        None,
    )
    if existing:
        conn.execute(
            "UPDATE subscribers SET phone_number = ?, area = ?, latitude = ?, longitude = ? WHERE id = ?",
            (sub.phone_number, sub.area, sub.latitude, sub.longitude, existing["id"]),
        )
        status = "updated"
    else:
        conn.execute(
            "INSERT INTO subscribers (phone_number, area, latitude, longitude) VALUES (?, ?, ?, ?)",
            (sub.phone_number, sub.area, sub.latitude, sub.longitude),
        )
        status = "subscribed"
    conn.commit()
    conn.close()
    return {"status": status}


OFFICERS_ONLY_WARNING = (
    "This report was originally officers_only - the reporter chose that "
    "to stay safe. Make sure your broadcast message doesn't include "
    "enough specific detail (exact time, vantage point, distinctive "
    "circumstances) to let someone work out who reported it."
)


@app.post("/alert/broadcast-preview", response_model=IncidentBroadcastPreview)
def preview_broadcast(request: BroadcastRequest, officer: str = Depends(get_current_officer)):
    """What /alert/broadcast WOULD do, without sending: the exact message and
    how many people it reaches. Shown before the officer confirms."""
    conn = get_connection()
    incident = conn.execute("SELECT * FROM incidents WHERE id = ?", (request.incident_id,)).fetchone()
    if incident is None or incident["latitude"] is None or incident["longitude"] is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Incident not found or has no location")
    subscribers = conn.execute("SELECT * FROM subscribers").fetchall()
    conn.close()
    targets = select_targets(subscribers, incident["latitude"], incident["longitude"], request.radius_km)
    return IncidentBroadcastPreview(
        incident_id=request.incident_id, message=request.message, radius_km=request.radius_km,
        recipient_count=len(targets),
        reporter_safety_warning=OFFICERS_ONLY_WARNING if incident["visibility"] == "officers_only" else None,
    )


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
    targets = select_targets(subscribers, incident["latitude"], incident["longitude"], request.radius_km)
    sent, failed = send_all(get_provider(), targets, request.message)

    cursor = conn.execute(
        "INSERT INTO broadcasts (incident_id, message, radius_km, recipient_count, failed_count, triggered_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (request.incident_id, request.message, request.radius_km, sent, failed, officer),
    )
    broadcast_id = cursor.lastrowid
    log_action(conn, officer, "incident_broadcast", "incident", request.incident_id,
               f"broadcast {broadcast_id}: {sent} sent, {failed} failed, {request.radius_km} km")
    conn.commit()
    conn.close()

    warning = OFFICERS_ONLY_WARNING if incident["visibility"] == "officers_only" else None

    return BroadcastResponse(
        broadcast_id=broadcast_id,
        recipients_reached=sent,
        failed_count=failed,
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
# Risk surface layer - the model's actual output, as data the map can draw
# -------------------------------------------------------------------

MAX_HEATMAP_SPAN_DEG = 12.0


@app.get("/risk/heatmap", response_model=RiskHeatmapResponse)
@limiter.limit("30/minute")
def risk_heatmap(request: Request, lat_min: float, lon_min: float, lat_max: float, lon_max: float,
                 category: Literal["crime", "hazard", "medical"] | None = None,
                 size: int = Query(40, ge=5, le=60)):
    """Risk for a size x size grid over the requested box (the dashboard's
    map viewport). The box is CLAMPED to the service area rather than
    rejected - people pan past Kenya's border and shouldn't get an error - and
    a box entirely outside it just returns no cells. Values match
    /predict at each cell's centre (same normaliser; see
    risk_surface.risk_cells for why build_risk_grid isn't used)."""
    if not (lat_min < lat_max and lon_min < lon_max):
        raise HTTPException(status_code=400, detail="lat_min/lon_min must be smaller than lat_max/lon_max")
    s_lat_min, s_lon_min, s_lat_max, s_lon_max = SERVICE_AREA
    lat_min, lat_max = max(lat_min, s_lat_min), min(lat_max, s_lat_max)
    lon_min, lon_max = max(lon_min, s_lon_min), min(lon_max, s_lon_max)
    if lat_min >= lat_max or lon_min >= lon_max:
        return RiskHeatmapResponse(cells=[], cell_lat_deg=0.0, cell_lon_deg=0.0, category=category)
    if (lat_max - lat_min) > MAX_HEATMAP_SPAN_DEG or (lon_max - lon_min) > MAX_HEATMAP_SPAN_DEG:
        raise HTTPException(status_code=400, detail=f"area too large; keep each side under {MAX_HEATMAP_SPAN_DEG} degrees")

    cells = risk_cells(lat_min, lon_min, lat_max, lon_max, category=category, size=size)
    return RiskHeatmapResponse(
        cells=cells, cell_lat_deg=(lat_max - lat_min) / size, cell_lon_deg=(lon_max - lon_min) / size,
        category=category,
    )


# -------------------------------------------------------------------
# Health facilities - "route to help" for medical incidents, distinct from
# risk-avoidance safe routing (which answers "route away from danger").
# See scripts/ingest_health_facilities.py.
# -------------------------------------------------------------------

@app.get("/facilities/nearest", response_model=list[HealthFacilityOut])
@limiter.limit("30/minute")
def nearest_facilities(request: Request, latitude: float, longitude: float,
                        limit: int = Query(5, ge=1, le=50), emergency_only: bool = False):
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
def list_facilities(limit: int = Query(2000, ge=1, le=5000)):
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


# A verified case leaves public view after this long unless an officer
# re-confirms it with the police and renews it.
CASE_TTL_HOURS = 72
ACTIVE_CASE_SQL = "status = 'verified' AND expires_at > datetime('now')"


@app.get("/missing-child/cases", response_model=list[MissingChildCaseOut])
def list_missing_child_cases():
    """Public feed - only active verified cases (an active search worth the
    public knowing about) and found_safe resolutions of verified cases (good
    news, appropriate to share). Never 'reported' (unverified),
    'found_deceased' (needs a human-mediated, not automated, channel),
    'closed_false_report', or a verified case past its expiry: a stale
    listing for a child who may already be home is its own harm, so a case
    leaves public view after CASE_TTL_HOURS unless an officer re-confirms it
    with the police and renews it."""
    conn = get_connection()
    rows = conn.execute(
        # found_safe is public only if the case was verified first. A case
        # closed as found_safe straight from 'reported' was never checked, so
        # publishing it would put an unverified child's details on the feed.
        f"SELECT * FROM missing_child_cases WHERE ({ACTIVE_CASE_SQL}) "
        "OR (status = 'found_safe' AND verified_by IS NOT NULL) "
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


def _get_case_or_404(conn, case_id: int):
    row = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Case not found")
    return row


@app.post("/missing-child/cases/{case_id}/verify", response_model=MissingChildCaseOfficerOut)
def verify_missing_child_case(case_id: int, body: MissingChildVerifyRequest,
                              officer: str = Depends(get_current_officer)):
    """Makes a case public. Needs the police Occurrence Book number, which the
    officer confirms with the station first: a missing-child alert can be
    weaponised (custody disputes, a child who fled abuse), and the OB number
    ties every public case to a real police report."""
    conn = get_connection()
    row = _get_case_or_404(conn, case_id)
    if row["status"] != "reported":
        conn.close()
        raise HTTPException(status_code=400, detail=f"Case is already '{row['status']}', not pending verification")

    conn.execute(
        "UPDATE missing_child_cases SET status = 'verified', verified_by = ?, ob_number = ?, "
        f"expires_at = datetime('now', '+{CASE_TTL_HOURS} hours'), updated_at = datetime('now') WHERE id = ?",
        (officer, body.ob_number, case_id),
    )
    log_action(conn, officer, "verify_missing_child", "missing_child_case", case_id, f"OB {body.ob_number}")
    conn.commit()
    updated = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (case_id,)).fetchone()
    conn.close()
    return MissingChildCaseOfficerOut(**dict(updated))


@app.post("/missing-child/cases/{case_id}/renew", response_model=MissingChildCaseOfficerOut)
def renew_missing_child_case(case_id: int, body: MissingChildRenewRequest,
                             officer: str = Depends(get_current_officer)):
    """Keeps a verified case public for another CASE_TTL_HOURS, after the
    officer re-confirms with the police that the child is still missing."""
    conn = get_connection()
    row = _get_case_or_404(conn, case_id)
    if row["status"] != "verified":
        conn.close()
        raise HTTPException(status_code=400, detail=f"Only verified cases can be renewed; this one is '{row['status']}'")
    conn.execute(
        f"UPDATE missing_child_cases SET expires_at = datetime('now', '+{CASE_TTL_HOURS} hours'), "
        "updated_at = datetime('now') WHERE id = ?",
        (case_id,),
    )
    log_action(conn, officer, "renew_missing_child", "missing_child_case", case_id, body.note.strip())
    conn.commit()
    updated = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (case_id,)).fetchone()
    conn.close()
    return MissingChildCaseOfficerOut(**dict(updated))


@app.post("/missing-child/cases/{case_id}/status", response_model=MissingChildCaseOfficerOut)
def update_missing_child_case_status(case_id: int, update: MissingChildStatusUpdate,
                                      officer: str = Depends(get_current_officer)):
    conn = get_connection()
    row = _get_case_or_404(conn, case_id)
    if row["status"] in ("found_safe", "found_deceased", "closed_false_report"):
        conn.close()
        raise HTTPException(status_code=400, detail=f"Case is already resolved as '{row['status']}'")

    conn.execute(
        # resolved_by, not verified_by: resolving a case is not verifying it,
        # and verified_by is what makes a found_safe case public.
        "UPDATE missing_child_cases SET status = ?, resolved_by = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (update.status, officer, case_id),
    )
    # Proposals for a closed case must never be approved later.
    conn.execute(
        "UPDATE missing_child_broadcast_proposals SET status = 'rejected', decided_by = ?, "
        "decided_at = datetime('now') WHERE case_id = ? AND status = 'pending'",
        (officer, case_id),
    )
    log_action(conn, officer, "resolve_missing_child", "missing_child_case", case_id, update.status)
    conn.commit()
    updated = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (case_id,)).fetchone()
    conn.close()
    return MissingChildCaseOfficerOut(**dict(updated))


# -- Missing Child Alert: geo-targeted broadcast ------------------------------
# The reach is the point of this feature, and a wrong alert is how this
# project gets sued. So: only a verified, unexpired case; the message names
# the child by first name only; and it takes two people. An officer or admin
# PROPOSES the exact message and radius; a different admin, or the owner,
# APPROVES it and only then is it sent. The owner may also send alone.

ALERT_CONTACT = os.environ.get("EIS_ALERT_CONTACT", "999 or 112")
SMS_MAX = 300


def _first_name(full_name: str) -> str:
    parts = full_name.strip().split()
    return parts[0] if parts else full_name.strip()


def _compose_missing_child_message(case) -> str:
    """Built ONLY from public case fields - never the reporter's phone or
    relationship - and with the child's FIRST name only: an SMS can be
    forwarded anywhere and never recalled, so it carries the least that
    still lets someone recognise the child. Trimmed to fit one 300-char
    message: the fixed parts (who, where, what to do) always survive;
    description/clothing are cut first if it's too long."""
    who = _first_name(case["child_name"]) + (f", {case['age']}" if case["age"] is not None else "")
    where = case["last_seen_area"] or "nearby"
    tail = f" Last seen {where} at {case['last_seen_time']}. If seen call {ALERT_CONTACT}."
    head = f"MISSING CHILD: {who}."
    room = SMS_MAX - len(head) - len(tail) - 2
    details = case["physical_description"].strip()
    if case["clothing_description"].strip():
        details += ". Wearing " + case["clothing_description"].strip()
    if len(details) > room:
        details = details[: max(room - 1, 0)].rstrip() + "…"
    return f"{head} {details}.{tail}" if details else f"{head}{tail}"


def _compose_followup_message(case) -> str:
    # Neutral on purpose: the same words fit found safe, found deceased and
    # a false report, and none of those outcomes belongs in a mass SMS.
    return (f"UPDATE: the missing-child alert for {_first_name(case['child_name'])} is now closed. "
            "Please disregard it. Thank you for your help.")


def _message_contains_phone(message: str, phone: str) -> bool:
    key = phone_key(phone)
    digits = "".join(ch for ch in message if ch.isdigit())
    return len(key) >= 7 and key in digits


def _get_broadcastable_case_or_error(conn, case_id: int):
    case = _get_case_or_404(conn, case_id)
    if case["status"] != "verified":
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Only verified cases can be broadcast; this one is '{case['status']}'",
        )
    active = conn.execute(f"SELECT 1 FROM missing_child_cases WHERE id = ? AND {ACTIVE_CASE_SQL}", (case_id,)).fetchone()
    if active is None:
        conn.close()
        raise HTTPException(status_code=400, detail="This case has expired; re-confirm with the police and renew it first")
    return case


def _checked_message(conn, case, message: str | None) -> str:
    message = message or _compose_missing_child_message(case)
    if _message_contains_phone(message, case["reporter_phone"]):
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="Message contains the reporter's phone number. Reporter contact must never be broadcast.",
        )
    return message


def _send_missing_child_broadcast(conn, case, message: str, radius_km: float, triggered_by: str,
                                  approved_by: str) -> tuple[int, int, int]:
    """Sends, records the broadcast and who it reached. Returns (broadcast_id, sent, failed).
    The caller commits."""
    subscribers = conn.execute("SELECT * FROM subscribers").fetchall()
    targets = select_targets(subscribers, case["last_seen_latitude"], case["last_seen_longitude"], radius_km)
    delivered: list[str] = []
    sent, failed = send_all(get_provider(), targets, message, delivered)
    cursor = conn.execute(
        "INSERT INTO missing_child_broadcasts "
        "(case_id, message, radius_km, recipient_count, failed_count, triggered_by, approved_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (case["id"], message, radius_km, sent, failed, triggered_by, approved_by),
    )
    broadcast_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO missing_child_broadcast_recipients (broadcast_id, phone_number) VALUES (?, ?)",
        [(broadcast_id, p) for p in delivered],
    )
    return broadcast_id, sent, failed


@app.get("/missing-child/cases/{case_id}/broadcast-preview", response_model=MissingChildBroadcastPreview)
def preview_missing_child_broadcast(case_id: int, radius_km: float = 10.0,
                                     officer: str = Depends(get_current_officer)):
    if not (0 < radius_km <= 100):
        raise HTTPException(status_code=400, detail="radius_km must be between 0 and 100")
    conn = get_connection()
    case = _get_broadcastable_case_or_error(conn, case_id)
    subscribers = conn.execute("SELECT * FROM subscribers").fetchall()
    conn.close()
    targets = select_targets(subscribers, case["last_seen_latitude"], case["last_seen_longitude"], radius_km)
    return MissingChildBroadcastPreview(
        message=_compose_missing_child_message(case), recipient_count=len(targets), radius_km=radius_km
    )


def _proposal_out(conn, row) -> MissingChildProposalOut:
    out = MissingChildProposalOut(**dict(row))
    if row["status"] == "pending":
        case = conn.execute("SELECT * FROM missing_child_cases WHERE id = ?", (row["case_id"],)).fetchone()
        subscribers = conn.execute("SELECT * FROM subscribers").fetchall()
        out.recipient_count_now = len(select_targets(
            subscribers, case["last_seen_latitude"], case["last_seen_longitude"], row["radius_km"]))
    return out


@app.post("/missing-child/cases/{case_id}/broadcast-proposals", response_model=MissingChildProposalOut)
def propose_missing_child_broadcast(case_id: int, body: MissingChildBroadcastRequest,
                                    officer: str = Depends(get_current_officer)):
    """Step one of the two-person rule: fixes the exact message and radius
    that the approver will see and that will be sent. Nothing is sent yet."""
    conn = get_connection()
    case = _get_broadcastable_case_or_error(conn, case_id)
    message = _checked_message(conn, case, body.message)
    cursor = conn.execute(
        "INSERT INTO missing_child_broadcast_proposals (case_id, message, radius_km, proposed_by) VALUES (?, ?, ?, ?)",
        (case_id, message, body.radius_km, officer),
    )
    proposal_id = cursor.lastrowid
    log_action(conn, officer, "propose_missing_child_broadcast", "missing_child_case", case_id,
               f"proposal {proposal_id}, {body.radius_km} km")
    conn.commit()
    row = conn.execute("SELECT * FROM missing_child_broadcast_proposals WHERE id = ?", (proposal_id,)).fetchone()
    out = _proposal_out(conn, row)
    conn.close()
    return out


@app.get("/missing-child/broadcast-proposals", response_model=list[MissingChildProposalOut])
def list_missing_child_proposals(status: Literal["pending", "approved", "rejected"] | None = "pending",
                                 officer: str = Depends(get_current_officer)):
    conn = get_connection()
    if status:
        rows = conn.execute("SELECT * FROM missing_child_broadcast_proposals WHERE status = ? ORDER BY id DESC",
                            (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM missing_child_broadcast_proposals ORDER BY id DESC LIMIT 100").fetchall()
    out = [_proposal_out(conn, r) for r in rows]
    conn.close()
    return out


def _get_pending_proposal(conn, proposal_id: int, account: dict):
    conn.execute("BEGIN IMMEDIATE")  # two approvers clicking at once must not send twice
    row = conn.execute("SELECT * FROM missing_child_broadcast_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=404, detail="Proposal not found")
    if row["status"] != "pending":
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=400, detail=f"Proposal is already '{row['status']}'")
    if row["proposed_by"] == account["username"]:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=403, detail="You proposed this alert; a different person must decide on it")
    return row


@app.post("/missing-child/broadcast-proposals/{proposal_id}/approve", response_model=MissingChildBroadcastResponse)
def approve_missing_child_broadcast(proposal_id: int, account: dict = Depends(require_role("admin"))):
    """Step two: a different admin, or the owner, approves and the proposal's
    exact message is sent to the radius it named."""
    conn = get_connection()
    proposal = _get_pending_proposal(conn, proposal_id, account)
    case = conn.execute(
        f"SELECT * FROM missing_child_cases WHERE id = ? AND {ACTIVE_CASE_SQL}", (proposal["case_id"],)
    ).fetchone()
    if case is None:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=400, detail="The case is no longer verified and active; nothing was sent")
    broadcast_id, sent, failed = _send_missing_child_broadcast(
        conn, case, proposal["message"], proposal["radius_km"], proposal["proposed_by"], account["username"])
    conn.execute(
        "UPDATE missing_child_broadcast_proposals SET status = 'approved', decided_by = ?, broadcast_id = ?, "
        "decided_at = datetime('now') WHERE id = ?",
        (account["username"], broadcast_id, proposal_id),
    )
    log_action(conn, account["username"], "approve_missing_child_broadcast", "missing_child_case", case["id"],
               f"proposal {proposal_id} by {proposal['proposed_by']}: {sent} sent, {failed} failed")
    conn.commit()
    conn.close()
    return MissingChildBroadcastResponse(
        broadcast_id=broadcast_id, recipients_reached=sent, failed_count=failed,
        radius_km=proposal["radius_km"], message=proposal["message"],
    )


@app.post("/missing-child/broadcast-proposals/{proposal_id}/reject", response_model=MissingChildProposalOut)
def reject_missing_child_broadcast(proposal_id: int, account: dict = Depends(require_role("admin"))):
    conn = get_connection()
    proposal = _get_pending_proposal(conn, proposal_id, account)
    conn.execute(
        "UPDATE missing_child_broadcast_proposals SET status = 'rejected', decided_by = ?, "
        "decided_at = datetime('now') WHERE id = ?",
        (account["username"], proposal_id),
    )
    log_action(conn, account["username"], "reject_missing_child_broadcast", "missing_child_case",
               proposal["case_id"], f"proposal {proposal_id} by {proposal['proposed_by']}")
    conn.commit()
    row = conn.execute("SELECT * FROM missing_child_broadcast_proposals WHERE id = ?", (proposal_id,)).fetchone()
    out = _proposal_out(conn, row)
    conn.close()
    return out


@app.post("/missing-child/cases/{case_id}/broadcast", response_model=MissingChildBroadcastResponse)
def broadcast_missing_child(case_id: int, body: MissingChildBroadcastRequest,
                             account: dict = Depends(require_role("owner"))):
    """Owner only: sends without a second person. Everyone else proposes."""
    conn = get_connection()
    case = _get_broadcastable_case_or_error(conn, case_id)
    message = _checked_message(conn, case, body.message)
    broadcast_id, sent, failed = _send_missing_child_broadcast(
        conn, case, message, body.radius_km, account["username"], account["username"])
    log_action(conn, account["username"], "owner_missing_child_broadcast", "missing_child_case", case_id,
               f"broadcast {broadcast_id}: {sent} sent, {failed} failed")
    conn.commit()
    conn.close()
    return MissingChildBroadcastResponse(
        broadcast_id=broadcast_id, recipients_reached=sent, failed_count=failed,
        radius_km=body.radius_km, message=message,
    )


@app.post("/missing-child/cases/{case_id}/followup")
def send_missing_child_followup(case_id: int, officer: str = Depends(get_current_officer)):
    """After a case is resolved, tells exactly the people who got the alert
    that it is closed. Once per case."""
    conn = get_connection()
    case = _get_case_or_404(conn, case_id)
    if case["status"] not in ("found_safe", "found_deceased", "closed_false_report"):
        conn.close()
        raise HTTPException(status_code=400, detail="Resolve the case first (found safe, found deceased or false report)")
    if case["followup_sent_at"]:
        conn.close()
        raise HTTPException(status_code=400, detail="The follow-up was already sent")
    phones = [r["phone_number"] for r in conn.execute(
        "SELECT DISTINCT r.phone_number FROM missing_child_broadcast_recipients r "
        "JOIN missing_child_broadcasts b ON b.id = r.broadcast_id WHERE b.case_id = ?", (case_id,)
    ).fetchall()]
    if not phones:
        conn.close()
        raise HTTPException(status_code=400, detail="No alert reached anyone for this case, so there is no one to update")
    message = _compose_followup_message(case)
    sent, failed = send_all(get_provider(), [{"phone_number": p} for p in phones], message)
    conn.execute("UPDATE missing_child_cases SET followup_sent_at = datetime('now') WHERE id = ?", (case_id,))
    log_action(conn, officer, "missing_child_followup", "missing_child_case", case_id, f"{sent} sent, {failed} failed")
    conn.commit()
    conn.close()
    return {"case_id": case_id, "message": message, "recipients_reached": sent, "failed_count": failed}


@app.get("/missing-child/broadcasts")
def list_missing_child_broadcasts(officer: str = Depends(get_current_officer)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM missing_child_broadcasts ORDER BY id DESC LIMIT 50").fetchall()
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
