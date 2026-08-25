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
