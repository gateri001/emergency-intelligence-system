from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles

from src import ml
from src.auth import authenticate_officer, create_access_token, get_current_officer
from src.database import get_connection, init_db
from src.routing import find_safe_route
from src.schemas import (
    BulkReportRequest,
    IncidentOut,
    IncidentReport,
    PredictionRequest,
    PredictionResponse,
    SafeRouteRequest,
    SafeRouteResponse,
)

app = FastAPI(title="Emergency Intelligence System", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


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
def predict(request: PredictionRequest):
    severity = ml.predict_severity(request.area, request.type, request.timestamp)
    return PredictionResponse(
        area=request.area,
        type=request.type,
        predicted_severity=severity,
        message=f"Predicted {request.type} risk in {request.area} is {severity}",
    )


@app.get("/model/known-values")
def model_known_values():
    return {"areas": ml.known_areas(), "types": ml.known_types()}


# -------------------------------------------------------------------
# Safe routing
# -------------------------------------------------------------------

@app.post("/route/safe", response_model=SafeRouteResponse)
def safe_route(request: SafeRouteRequest):
    result = find_safe_route(request.latitude, request.longitude, request.risk_aversion)
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
    severity = ml.predict_severity(report.area, report.type, report.timestamp)
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO incidents
           (source, type, area, latitude, longitude, description, predicted_severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, report.type.strip().lower(), report.area.strip().title(),
         report.latitude, report.longitude, report.description, severity, report.timestamp),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


@app.post("/report/citizen")
def report_citizen(report: IncidentReport):
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


@app.get("/incidents", response_model=list[IncidentOut])
def list_incidents(limit: int = 100):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [IncidentOut(**dict(row)) for row in rows]


# -------------------------------------------------------------------
# Static dashboard
# -------------------------------------------------------------------

app.mount("/dashboard", StaticFiles(directory="static", html=True), name="dashboard")
