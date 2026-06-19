from pydantic import BaseModel
from typing import Optional


class ThresholdCreate(BaseModel):
    sample_type: str
    temp_min: float
    temp_max: float
    timeout_minutes: int


class BoxImportItem(BaseModel):
    box_code: str
    sample_type: str
    current_temp: Optional[float] = None


class BoxImportJSON(BaseModel):
    boxes: list[BoxImportItem]


class TransitionRequest(BaseModel):
    role: str
    operator: str
    reason: Optional[str] = None
    current_temp: Optional[float] = None


class BoxOut(BaseModel):
    box_code: str
    sample_type: str
    current_temp: Optional[float] = None
    status: str
    created_at: str
    updated_at: str
    dispatch_at: Optional[str] = None
    receive_at: Optional[str] = None


class AuditOut(BaseModel):
    id: int
    box_code: str
    from_status: Optional[str] = None
    to_status: str
    role: str
    operator: str
    reason: Optional[str] = None
    temp_at_action: Optional[float] = None
    created_at: str


class ImportResult(BaseModel):
    imported: list[str]
    rejected: list[dict]
