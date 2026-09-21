from typing import Literal

from pydantic import BaseModel, Field


class IncidentReport(BaseModel):
    type: str = Field(..., examples=["robbery", "flood", "fire", "medical_emergency"])
    # Real coordinates, required - risk scoring is point-based, not tied to a
    # fixed list of named places. `area` is an optional human-readable label
    # only (for display), never used for scoring.
    latitude: float
    longitude: float
    area: str = Field("", examples=["near Kibera market"], description="Optional free-text label, not used for scoring")
    description: str = ""
    timestamp: str = Field(..., examples=["2026-08-25 14:30"])
    # Reporter-chosen visibility: "officers_only" keeps a time-critical but
    # unverified report (e.g. a witnessed crime in progress) out of the
    # public feed so the reporter isn't put at risk before responders can
    # act. No reporter identity is ever collected on this model in the first
    # place, so officers never see who filed an officers_only report either.
    visibility: Literal["public", "officers_only"] = "public"
    # Scrappy stand-in for real evidence upload (no file storage built yet -
    # logged as a deliberate skip, see docs/architecture.md): a
    # self-attested "I have a photo/video of this" boolean that nudges
    # confidence up slightly. Lying about it is possible; it's one signal
    # among several (source trust, corroboration), not the whole score.
    has_evidence: bool = False


class BulkReportRequest(BaseModel):
    officer_id: str
    reports: list[IncidentReport]


class PredictionRequest(BaseModel):
    latitude: float
    longitude: float
    type: str


class PredictionResponse(BaseModel):
    latitude: float
    longitude: float
    type: str
    risk_score: float
    predicted_severity: str
    message: str


class SafeRouteRequest(BaseModel):
    latitude: float
    longitude: float
    risk_aversion: float = Field(4.0, ge=0, le=20, description="Higher = detour more readily to avoid risk")


class RoutePoint(BaseModel):
    lat: float
    lon: float
    risk: float


class SafeRouteResponse(BaseModel):
    start: RoutePoint
    safe_zone: RoutePoint
    waypoints: list[RoutePoint]
    distance_km: float


class SubscriberIn(BaseModel):
    phone_number: str = Field(..., examples=["+254712345678"])
    area: str = ""
    latitude: float
    longitude: float


class BroadcastRequest(BaseModel):
    incident_id: int
    message: str = Field(..., max_length=300)
    radius_km: float = Field(5.0, gt=0, le=50)


class BroadcastResponse(BaseModel):
    broadcast_id: int
    recipients_reached: int
    radius_km: float
    message: str
    failed_count: int = 0
    reporter_safety_warning: str | None = None


class IncidentOut(BaseModel):
    id: int
    source: str
    type: str
    area: str
    latitude: float | None
    longitude: float | None
    description: str
    predicted_severity: str | None
    timestamp: str
    visibility: str = "public"
    has_evidence: bool = False
    confidence_score: float = 0.0
    corroboration_count: int = 0
    alert_tier: str = "in_app"
    scoring_stage: str = "reflex"


class VoteRequest(BaseModel):
    confirm: bool = Field(..., description="True to corroborate the report, false to dispute it")


class MissingChildReport(BaseModel):
    child_name: str = Field(..., min_length=1)
    age: int | None = Field(None, ge=0, le=17)
    physical_description: str = Field(..., min_length=1, description="Height, build, distinguishing features")
    clothing_description: str = ""
    last_seen_latitude: float
    last_seen_longitude: float
    last_seen_area: str = ""
    last_seen_time: str = Field(..., examples=["2026-09-18 14:30"])
    has_photo: bool = False
    # The one deliberate exception to "no reporter identity is ever
    # collected" - this is an investigation, not a risk signal, and
    # officers need to be able to follow up. Never exposed publicly.
    reporter_phone: str = Field(..., examples=["+254712345678"])
    reporter_relationship: str = Field("", examples=["parent", "guardian", "neighbor"])


class MissingChildCaseOut(BaseModel):
    """Public-facing view - no reporter contact, ever."""
    id: int
    child_name: str
    age: int | None
    physical_description: str
    clothing_description: str
    last_seen_latitude: float
    last_seen_longitude: float
    last_seen_area: str
    last_seen_time: str
    has_photo: bool
    status: str
    created_at: str


class MissingChildCaseOfficerOut(MissingChildCaseOut):
    """Officer view - adds the reporter contact needed to actually follow up."""
    reporter_phone: str
    reporter_relationship: str
    verified_by: str | None


class MissingChildStatusUpdate(BaseModel):
    status: Literal["verified", "found_safe", "found_deceased", "closed_false_report"]


class MissingChildBroadcastRequest(BaseModel):
    # Optional: if omitted, a message is composed from PUBLIC case fields only.
    message: str | None = Field(None, min_length=1, max_length=300)
    radius_km: float = Field(10.0, gt=0, le=100)


class MissingChildBroadcastPreview(BaseModel):
    message: str
    recipient_count: int
    radius_km: float


class MissingChildBroadcastResponse(BaseModel):
    broadcast_id: int
    recipients_reached: int
    failed_count: int
    radius_km: float
    message: str


class HealthFacilityOut(BaseModel):
    id: int
    name: str
    amenity: str
    has_emergency: str | None
    latitude: float
    longitude: float
    distance_km: float | None = None
