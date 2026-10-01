"""Request/response schemas for the application API."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AddressIn(BaseModel):
    line1: str
    city: str
    state_code: str
    postal_code: str


class CreateCaseRequest(BaseModel):
    customer_id: Optional[str] = Field(default=None, description="Must match the authenticated customer when provided.")
    state_code: str
    product: str = "renters"
    desired_effective_date: Optional[date] = None
    property_limit_minor: Optional[int] = None
    liability_limit_minor: Optional[int] = None
    deductible_cap_minor: Optional[int] = None
    replacement_cost_required: Optional[bool] = None
    required_item_classes: Optional[List[str]] = None
    address: Optional[AddressIn] = None
    deductible_preference: Optional[str] = None


class CreateCaseResponse(BaseModel):
    id: str
    status: str
    version: int
    missing_fields: List[str]
    needs_version: int


class AnswerItem(BaseModel):
    field: Optional[str] = None
    question_id: Optional[str] = None
    value: Any = None
    evidence_id: Optional[str] = None


class AnswersRequest(BaseModel):
    answers: List[AnswerItem]


class ApplicationRequest(BaseModel):
    quote_id: str
    answers_version: Optional[int] = None
    idempotency_key: Optional[str] = None


class ApproveActionRequest(BaseModel):
    expected_case_version: int
    action_payload_hash: str
    approval_challenge_id: str


class RejectActionRequest(BaseModel):
    reason: Optional[str] = None


class SelectQuoteRequest(BaseModel):
    quote_id: str


class MessageRequest(BaseModel):
    text: str


class ClockAdvanceRequest(BaseModel):
    minutes: int = 0
    hours: int = 0
    days: int = 0


class FaultRequest(BaseModel):
    insurer_id: str
    kind: str
    operation: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class ResolveReviewRequest(BaseModel):
    resolution: str  # e.g. "resume_underwriting" | "accept_issued" | "decline" | "back_to_selection"
    note: Optional[str] = None
