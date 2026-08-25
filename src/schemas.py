from pydantic import BaseModel, Field


class IncidentReport(BaseModel):
    type: str = Field(..., examples=["robbery", "flood", "fire", "medical_emergency"])
    area: str = Field(..., examples=["Kibera", "Kayole", "CBD"])
    description: str = ""
    timestamp: str = Field(..., examples=["2026-08-25 14:30"])
    latitude: float | None = None
    longitude: float | None = None


class BulkReportRequest(BaseModel):
    officer_id: str
    reports: list[IncidentReport]


class PredictionRequest(BaseModel):
    area: str
    type: str
    timestamp: str


class PredictionResponse(BaseModel):
    area: str
    type: str
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
