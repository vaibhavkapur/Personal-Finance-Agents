from __future__ import annotations

from pydantic import BaseModel, Field


class ObligationInput(BaseModel):
    description: str = Field(min_length=1, max_length=128)
    amount_minor: int = Field(gt=0)
    due_date: str
    certainty: str = Field(default="confirmed", pattern="^(confirmed|estimated)$")


class CreateCaseRequest(BaseModel):
    customer_id: str
    deposit_id: str
    currency: str = Field(default="USD", min_length=3, max_length=3)
    minimum_buffer_minor: int = Field(ge=0)
    obligation_ids: list[str] = Field(default_factory=list)
    preferred_lockup_days: int | None = Field(default=None, ge=0)
    buffer_includes_obligations: bool | None = None
    concentration_limit_minor: int | None = Field(default=None, ge=0)
    provider_mode: str = "normal"


class CreateCaseResponse(BaseModel):
    id: str
    status: str
    version: int
    missing_fields: list[str]
    outstanding_questions: list[dict]


class AnswersRequest(BaseModel):
    preferred_lockup_days: int | None = Field(default=None, ge=0)
    buffer_includes_obligations: bool | None = None
    minimum_buffer_minor: int | None = Field(default=None, ge=0)
    concentration_limit_minor: int | None = Field(default=None, ge=0)
    obligation_ids: list[str] | None = None
    obligations: list[ObligationInput] | None = None


class PrepareInstructionRequest(BaseModel):
    option_id: str
    amount_minor: int | None = Field(default=None, gt=0)


class ChallengeResponse(BaseModel):
    approval_challenge_id: str
    action_id: str
    action_payload_hash: str
    expected_case_version: int
    expires_at: str


class ApproveRequest(BaseModel):
    expected_case_version: int
    action_payload_hash: str
    approval_challenge_id: str


class ApproveResponse(BaseModel):
    approval_id: str
    action_id: str
    case_id: str
    case_status: str
    case_version: int
    expires_at: str


class ProviderEvent(BaseModel):
    id: str
    type: str
    occurred_at: str
    environment: str = "mock"
    data: dict = Field(default_factory=dict)


class ClockAdvanceRequest(BaseModel):
    days: int = Field(default=0, ge=0, le=400)
    hours: int = Field(default=0, ge=0, le=48)
    minutes: int = Field(default=0, ge=0, le=600)


class ModeRequest(BaseModel):
    mode: str
    offer_id: str | None = None


class RevokeRequest(BaseModel):
    account_id: str
    revoked: bool = True


class AgentTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    case_id: str | None = None
    deposit_id: str | None = None
