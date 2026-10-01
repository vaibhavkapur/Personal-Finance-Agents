"""Request/response schemas for the application API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CreateLoanCaseRequest(BaseModel):
    customer_id: str
    mortgage_id: str
    holding_horizon_months: Optional[int] = Field(default=None, gt=0, le=480)
    maximum_cash_to_close_minor: Optional[int] = Field(default=None, ge=0)
    offer_document_ids: List[str] = Field(default_factory=list)


class LoanCaseCreated(BaseModel):
    id: str
    status: str
    version: int
    missing_fields: List[str]
    outstanding_questions: List[Dict[str, Any]]


class ConfirmFactsRequest(BaseModel):
    current_balance_confirmed: Optional[bool] = None
    current_balance_minor: Optional[int] = Field(default=None, gt=0)
    remaining_months: Optional[int] = Field(default=None, gt=0, le=480)
    payment_includes_escrow: Optional[bool] = None
    holding_horizon_months: Optional[int] = Field(default=None, gt=0, le=480)
    maximum_cash_to_close_minor: Optional[int] = Field(default=None, ge=0)
    finance_costs: Optional[Dict[str, bool]] = None
    income_evidence_document_id: Optional[str] = None


class AddOffersRequest(BaseModel):
    document_ids: List[str] = Field(default_factory=list)
    documents: List[Dict[str, Any]] = Field(default_factory=list, description="Inline synthetic Loan Estimate documents")


class SupplyOfferFieldRequest(BaseModel):
    field: str
    value: Any
    source: str = Field(description="Where the value came from, e.g. 'Loan Estimate page 1' or 'lender email 2026-09-24'")


class CompareRequest(BaseModel):
    horizon_months: Optional[int] = Field(default=None, gt=0, le=480)


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(keep_current)$")
    expected_case_version: Optional[int] = None


class LenderRequestDraftRequest(BaseModel):
    lender_id: str
    target_offer_id: Optional[str] = None
    competing_offer_id: Optional[str] = None
    disclosed_document_ids: List[str] = Field(default_factory=list)
    request_type: str = Field(default="reprice", pattern="^(reprice|comparable_offer)$")


class ApplicationDraftRequest(BaseModel):
    offer_id: str
    document_ids: List[str] = Field(default_factory=list)
    income_assertion_minor: Optional[int] = None


class DocumentReleaseRequest(BaseModel):
    document_ids: List[str]


class ApproveActionRequest(BaseModel):
    expected_case_version: int
    action_payload_hash: str
    approval_challenge_id: str


class ChatRequest(BaseModel):
    message: str = ""
    intent: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class ProviderEventEnvelope(BaseModel):
    id: str
    type: str
    provider: str
    occurred_at: str
    environment: str
    data: Dict[str, Any]


class FaultInjectionRequest(BaseModel):
    kind: str
    lender_id: Optional[str] = None
    operation: Optional[str] = None
    once: bool = True
    params: Dict[str, Any] = Field(default_factory=dict)


class ClockAdvanceRequest(BaseModel):
    days: int = 0
    hours: int = 0
    minutes: int = 0


class ResumeCaseRequest(BaseModel):
    next_state: str
    reason: str
