from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Intent(StrictModel):
    customer_id: str = 'cus_demo_8'
    beneficiary_id: str = 'beneficiary_demo_1'
    source_currency: Literal['USD'] = 'USD'
    target_currency: Literal['INR'] = 'INR'
    budget_mode: Literal['total_sender_cost', 'recipient_target'] = 'total_sender_cost'
    source_budget_minor: Optional[int] = Field(default=50000, ge=1000, le=1000000, strict=True)
    target_required_minor: Optional[int] = Field(default=None, ge=10000, le=83000000, strict=True)
    deadline_at: datetime
    purpose: Literal['family_support', 'education', 'medical', 'savings'] = 'family_support'
    recipient_confirmed: bool = False

    @model_validator(mode='after')
    def check_mode(self):
        if self.deadline_at.tzinfo is None:
            raise ValueError('deadline_at must include a timezone')
        if self.budget_mode == 'total_sender_cost' and (self.source_budget_minor is None or self.target_required_minor is not None):
            raise ValueError('Total-budget mode requires source_budget_minor only')
        if self.budget_mode == 'recipient_target' and (self.target_required_minor is None or self.source_budget_minor is not None):
            raise ValueError('Recipient-target mode requires target_required_minor only')
        return self

class Versioned(StrictModel):
    expected_case_version: int

class Draft(Versioned):
    quote_id: str
    recipient_version: int
    recipient_confirmed: Literal[True]

class Approval(Versioned):
    action_payload_hash: str
    approval_challenge_id: str

class DocumentPacket(Versioned):
    requirement_id: str
    document_ids: list[str] = Field(min_length=1, max_length=3)

class ClockAdvance(StrictModel):
    minutes: int = Field(ge=1, le=10080, strict=True)

class ProviderEvent(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    provider_id: Literal['swift', 'bridge', 'lotus']
    provider_transfer_ref: str
    type: Literal['funding_pending', 'processing', 'information_required', 'payout_pending', 'delivered', 'rejected', 'cancelled', 'delayed']
    occurred_at: datetime
    environment: Literal['mock']
    data: dict = Field(default_factory=dict)

class Fault(StrictModel):
    scenario: Literal['normal', 'timeout', 'missing_document', 'short_payment', 'delay', 'rejection', 'cancel_denied', 'malformed']
