import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.risk_surface import TYPE_CATEGORY

# ---------------------------------------------------------------------------
# Shared input validation. Everything the API STORES goes through these, so
# junk (0,0 "null island" from a failed GPS, malformed times, free-text types,
# phone numbers in six different formats) is refused at the door instead of
# silently poisoning scoring and broadcasting downstream.
# ---------------------------------------------------------------------------

# Kenya's bounding box plus ~1 degree of margin (border areas, GPS drift).
# lat_min, lon_min, lat_max, lon_max
SERVICE_AREA = (-5.8, 32.5, 6.0, 43.0)
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M"  # the one format every parser here assumes
FUTURE_TOLERANCE = timedelta(hours=24)  # clock skew; past dates stay allowed (officer back-fills)
MAX_BULK_REPORTS = 500


def check_in_service_area(lat: float, lon: float) -> None:
    lat_min, lon_min, lat_max, lon_max = SERVICE_AREA
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        raise ValueError(
            "location is outside the area this system covers (Kenya and its borders) - "
            "check the coordinates"
        )


def check_timestamp(value: str) -> str:
    try:
        parsed = datetime.strptime(value, TIMESTAMP_FORMAT)
    except (TypeError, ValueError):
        raise ValueError("must look like 2026-09-21 14:30 (YYYY-MM-DD HH:MM)")
    if parsed > datetime.now() + FUTURE_TOLERANCE:
        raise ValueError("cannot be more than 24 hours in the future")
    return value


def normalize_phone(value: str) -> str:
    """Kenyan numbers in any common form (0712 345 678, 254712345678,
    +254-712-345-678, 712345678) -> +254712345678. Other countries are
    accepted only as E.164 (+ then 8-15 digits). Anything else is refused.
    One canonical form is also what lets us tell that two subscriptions are
    the same person."""
    cleaned = re.sub(r"[\s\-().]", "", value or "")
    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif cleaned.startswith("254"):
        digits, cleaned = cleaned, "+" + cleaned
    elif cleaned.startswith("0") and len(cleaned) == 10:
        digits, cleaned = "254" + cleaned[1:], "+254" + cleaned[1:]
    elif len(cleaned) == 9 and cleaned[0] in "17":
        digits, cleaned = "254" + cleaned, "+254" + cleaned
    else:
        raise ValueError("not a recognisable phone number (try +2547XXXXXXXX)")
    if not digits.isdigit() or not (8 <= len(digits) <= 15):
        raise ValueError("not a recognisable phone number (try +2547XXXXXXXX)")
    if digits.startswith("254") and not re.fullmatch(r"254[17]\d{8}", digits):
        raise ValueError("Kenyan mobile numbers look like +2547XXXXXXXX or +2541XXXXXXXX")
    return "+" + digits


class IncidentReport(BaseModel):
    type: str = Field(..., max_length=40, examples=["robbery", "flood", "fire", "medical_emergency"])
    # Real coordinates, required - risk scoring is point-based, not tied to a
    # fixed list of named places. `area` is an optional human-readable label
    # only (for display), never used for scoring.
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    area: str = Field("", max_length=200, examples=["near Kibera market"], description="Optional free-text label, not used for scoring")
    description: str = Field("", max_length=1000)
    timestamp: str = Field(..., examples=["2026-08-25 14:30"])

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TYPE_CATEGORY:
            raise ValueError(f"unknown incident type; use one of: {', '.join(sorted(TYPE_CATEGORY))}")
        return v

    @field_validator("timestamp")
    @classmethod
    def _valid_timestamp(cls, v: str) -> str:
        return check_timestamp(v)

    @model_validator(mode="after")
    def _in_service_area(self):
        check_in_service_area(self.latitude, self.longitude)
        return self
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
    officer_id: str = Field(..., max_length=100)
    reports: list[IncidentReport] = Field(..., max_length=MAX_BULK_REPORTS)


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
    phone_number: str = Field(..., max_length=30, examples=["+254712345678"])
    area: str = Field("", max_length=200)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)

    @field_validator("phone_number")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @model_validator(mode="after")
    def _in_service_area(self):
        check_in_service_area(self.latitude, self.longitude)
        return self


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
    child_name: str = Field(..., min_length=1, max_length=100)
    age: int | None = Field(None, ge=0, le=17)
    physical_description: str = Field(..., min_length=1, max_length=500, description="Height, build, distinguishing features")
    clothing_description: str = Field("", max_length=300)
    last_seen_latitude: float = Field(..., ge=-90, le=90)
    last_seen_longitude: float = Field(..., ge=-180, le=180)
    last_seen_area: str = Field("", max_length=200)
    last_seen_time: str = Field(..., examples=["2026-09-18 14:30"])
    has_photo: bool = False
    # The one deliberate exception to "no reporter identity is ever
    # collected" - this is an investigation, not a risk signal, and
    # officers need to be able to follow up. Never exposed publicly.
    reporter_phone: str = Field(..., max_length=30, examples=["+254712345678"])
    reporter_relationship: str = Field("", max_length=100, examples=["parent", "guardian", "neighbor"])

    @field_validator("last_seen_time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        return check_timestamp(v)

    @field_validator("reporter_phone")
    @classmethod
    def _normalize_reporter_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @model_validator(mode="after")
    def _in_service_area(self):
        check_in_service_area(self.last_seen_latitude, self.last_seen_longitude)
        return self


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


class RiskCell(BaseModel):
    lat: float
    lon: float
    risk: float


class RiskHeatmapResponse(BaseModel):
    cells: list[RiskCell]
    cell_lat_deg: float
    cell_lon_deg: float
    category: str | None = None


class HealthFacilityOut(BaseModel):
    id: int
    name: str
    amenity: str
    has_emergency: str | None
    latitude: float
    longitude: float
    distance_km: float | None = None
