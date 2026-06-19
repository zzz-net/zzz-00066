from pydantic import BaseModel
from typing import Optional, List


class ThresholdCreate(BaseModel):
    sample_type: str
    temp_min: float
    temp_max: float
    timeout_minutes: int


class BoxImportItem(BaseModel):
    box_code: str
    sample_type: str
    current_temp: Optional[float] = None
    batch_no: Optional[str] = None


class BoxImportJSON(BaseModel):
    boxes: list[BoxImportItem]
    batch_no: Optional[str] = None
    scheduled_outbound_time: Optional[str] = None
    estimated_arrival_deadline: Optional[str] = None


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
    batch_no: Optional[str] = None


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
    batch_no: Optional[str] = None


class ImportResult(BaseModel):
    imported: list[str]
    rejected: list[dict]


class BatchCreate(BaseModel):
    batch_no: str
    sample_type: str
    scheduled_outbound_time: Optional[str] = None
    estimated_arrival_deadline: Optional[str] = None
    operator: Optional[str] = None


class BatchOut(BaseModel):
    batch_no: str
    sample_type: str
    status: str
    scheduled_outbound_time: Optional[str] = None
    estimated_arrival_deadline: Optional[str] = None
    total_boxes: int
    received_boxes: int
    missing_boxes: int
    created_at: str
    updated_at: str
    created_by: Optional[str] = None
    review_status: Optional[str] = None
    archived_at: Optional[str] = None
    archived_by: Optional[str] = None


class BatchBoxOut(BaseModel):
    box_code: str
    sample_type: str
    status: str
    box_batch_status: str
    received_at: Optional[str] = None
    missing_reason: Optional[str] = None
    missing_registered_at: Optional[str] = None
    missing_registered_by: Optional[str] = None
    missing_cancelled_at: Optional[str] = None
    missing_cancelled_by: Optional[str] = None
    missing_cancel_reason: Optional[str] = None


class BatchDetailOut(BaseModel):
    batch: BatchOut
    boxes: list[BatchBoxOut]
    pending_todos: list[str]


class BatchTransitionRequest(BaseModel):
    role: str
    operator: str
    reason: Optional[str] = None
    current_temp: Optional[float] = None


class BatchReceiveRequest(BaseModel):
    role: str
    operator: str
    reason: Optional[str] = None
    received_boxes: list[str]
    missing_boxes: Optional[list[str]] = None
    missing_reason: Optional[str] = None


class MissingBoxRegisterRequest(BaseModel):
    role: str
    operator: str
    reason: str
    box_codes: list[str]


class MissingBoxCancelRequest(BaseModel):
    role: str
    operator: str
    reason: Optional[str] = None
    box_codes: list[str]


class BatchAuditOut(BaseModel):
    id: int
    batch_no: str
    box_code: Optional[str] = None
    action: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    role: str
    operator: str
    reason: Optional[str] = None
    detail: Optional[str] = None
    created_at: str


class ReviewConfigUpdate(BaseModel):
    require_double_review: bool
    operator: str


class ReviewInitiateRequest(BaseModel):
    role: str
    operator: str
    handed_over_by: Optional[str] = None


class ReviewBoxResult(BaseModel):
    box_code: str
    result: str
    reason: Optional[str] = None


class ReviewBoxRequest(BaseModel):
    role: str
    operator: str
    reviews: List[ReviewBoxResult]


class ReviewCancelRequest(BaseModel):
    role: str
    operator: str
    reason: str


class ArchiveRequest(BaseModel):
    role: str
    operator: str


class ReviewBoxDetail(BaseModel):
    box_code: str
    first_review_result: Optional[str] = None
    first_reviewer: Optional[str] = None
    first_review_role: Optional[str] = None
    first_review_reason: Optional[str] = None
    first_review_at: Optional[str] = None
    second_review_result: Optional[str] = None
    second_reviewer: Optional[str] = None
    second_review_role: Optional[str] = None
    second_review_reason: Optional[str] = None
    second_review_at: Optional[str] = None
    final_result: Optional[str] = None
