from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CreateCaseRequest(BaseModel):
    customer_id: str
    order_ref: str
    reason_code: str = "promised_refund_missing"
    target_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    evidence_ids: List[str] = Field(default_factory=list)


class CreateCaseResponse(BaseModel):
    id: str
    status: str
    version: int
    next_step: str


class ApproveActionRequest(BaseModel):
    expected_case_version: int
    action_payload_hash: str
    approval_challenge_id: str


class AnswerRequest(BaseModel):
    answer: Dict[str, Any]


class AgentTurnRequest(BaseModel):
    message: Optional[str] = None


class CloseUnresolvedRequest(BaseModel):
    reason: str = Field(min_length=3)


class ProviderEventRequest(BaseModel):
    provider: str
    event_id: str
    type: str
    occurred_at: str
    environment: str
    data: Dict[str, Any]


class CommerceEventRequest(BaseModel):
    payload: Dict[str, Any]


class ClockAdvanceRequest(BaseModel):
    days: float = 0
    hours: float = 0
    minutes: float = 0


class ReplayRequest(BaseModel):
    provider: str
    event_id: str


class ReleaseRequest(BaseModel):
    target_status: str
    note: str = Field(min_length=3)


class FaultRequest(BaseModel):
    provider: str
    order_ref: str
    fault: Optional[str] = None
