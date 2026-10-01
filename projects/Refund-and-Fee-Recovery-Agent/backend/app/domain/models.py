"""Typed records shared across persistence, workflows, tools and the API."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return str(self.value)


class CaseStatus(StrEnum):
    detected = "detected"
    investigating = "investigating"
    awaiting_approval = "awaiting_approval"
    merchant_pending = "merchant_pending"
    refund_promised = "refund_promised"
    credit_pending = "credit_pending"
    issuer_review = "issuer_review"
    issuer_pending = "issuer_pending"
    provisional_credit = "provisional_credit"
    credit_reversed = "credit_reversed"
    final_credit = "final_credit"
    recovered = "recovered"
    unresolved = "unresolved"
    already_refunded = "already_refunded"
    not_supported = "not_supported"
    manual_review = "manual_review"


TERMINAL_STATES = {
    CaseStatus.recovered,
    CaseStatus.unresolved,
    CaseStatus.already_refunded,
    CaseStatus.not_supported,
}


class ReasonCode(StrEnum):
    promised_refund_missing = "promised_refund_missing"
    duplicate_billing = "duplicate_billing"
    unauthorized_transaction = "unauthorized_transaction"
    goodwill_fee_waiver = "goodwill_fee_waiver"


# Reason codes supported by the first release. Others are recorded as not_supported.
SUPPORTED_REASON_CODES = {ReasonCode.promised_refund_missing}


class DestinationType(StrEnum):
    original_payment = "original_payment"
    store_credit = "store_credit"
    unknown = "unknown"


class ChannelType(StrEnum):
    merchant = "merchant"
    issuer = "issuer"


class ChannelStatus(StrEnum):
    draft = "draft"
    awaiting_approval = "awaiting_approval"
    pending = "pending"
    promised = "promised"
    credited = "credited"
    provisional = "provisional"
    reversed = "reversed"
    declined = "declined"
    unresolved = "unresolved"
    closed = "closed"


class CreditKind(StrEnum):
    final = "final"
    provisional = "provisional"
    store_credit = "store_credit"


class Direction(StrEnum):
    debit = "debit"
    credit = "credit"


class TransactionKind(StrEnum):
    purchase = "purchase"
    refund = "refund"
    provisional_credit = "provisional_credit"
    reversal = "reversal"
    fee = "fee"
    other = "other"


class MatchingMethod(StrEnum):
    provider_reference = "provider_reference"
    amount_merchant_window = "amount_merchant_window"
    customer_confirmed = "customer_confirmed"


class ActionType(StrEnum):
    send_merchant_message = "send_merchant_message"
    send_merchant_followup = "send_merchant_followup"
    submit_issuer_dispute = "submit_issuer_dispute"


class ActionStatus(StrEnum):
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    submitting = "submitting"
    submitted = "submitted"
    unknown = "unknown"
    failed = "failed"
    declined = "declined"
    expired = "expired"
    superseded = "superseded"


class Authority(StrEnum):
    authoritative = "authoritative"
    estimated = "estimated"
    simulated = "simulated"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class Customer(BaseModel):
    id: str
    tenant_id: str
    display_name: str
    email: str


class PaymentInstrument(BaseModel):
    id: str
    customer_id: str
    network: str
    last4: str
    issuer_id: str


class Merchant(BaseModel):
    id: str
    name: str
    support_email: str
    support_channel: str
    contact_verified_at: str
    contact_source: str


class Document(BaseModel):
    id: str
    customer_id: str
    kind: str
    object_key: str
    content_hash: str
    source: str
    captured_at: str
    extraction_version: str
    extracted: Dict[str, Any] = Field(default_factory=dict)


class PurchaseRecord(BaseModel):
    id: str
    customer_id: str
    merchant_id: str
    order_ref: str
    original_transaction_id: str
    amount_minor: int
    currency: str
    payment_instrument_ref: str
    purchased_at: str


class RefundPromise(BaseModel):
    id: str
    purchase_id: str
    promised_minor: int
    currency: str
    destination_type: DestinationType
    promised_by: str
    promised_at: str
    expected_by: Optional[str] = None
    evidence_id: Optional[str] = None
    provider_refund_ref: Optional[str] = None
    verified_at: Optional[str] = None


class Transaction(BaseModel):
    id: str
    customer_id: str
    payment_instrument_ref: str
    merchant_id: Optional[str] = None
    direction: Direction
    kind: TransactionKind
    amount_minor: int
    currency: str
    posted_at: str
    description: str
    provider_ref: Optional[str] = None
    reverses_transaction_id: Optional[str] = None
    source: str


class PendingQuestion(BaseModel):
    kind: str
    prompt: str
    options: List[Dict[str, Any]] = Field(default_factory=list)
    asked_at: str


class RecoveryCase(BaseModel):
    id: str
    customer_id: str
    purchase_id: str
    promise_id: Optional[str] = None
    reason_code: ReasonCode
    target_minor: int
    currency: str
    final_recovered_minor: int = 0
    provisional_minor: int = 0
    store_credit_minor: int = 0
    reversed_minor: int = 0
    status: CaseStatus
    version: int = 1
    pending_question: Optional[PendingQuestion] = None
    completion_evidence_ref: Optional[str] = None
    outcome_note: Optional[str] = None
    created_at: str
    updated_at: str

    @property
    def outstanding_minor(self) -> int:
        return max(0, self.target_minor - self.final_recovered_minor)


class RecoveryChannel(BaseModel):
    id: str
    case_id: str
    channel_type: ChannelType
    provider: str
    provider_case_ref: Optional[str] = None
    status: ChannelStatus
    deadline_at: Optional[str] = None
    deadline_source: Optional[str] = None
    followup_count: int = 0
    last_contact_at: Optional[str] = None
    next_followup_at: Optional[str] = None
    last_provider_status: Optional[str] = None
    created_at: str


class CreditMatch(BaseModel):
    id: str
    case_id: str
    transaction_id: str
    amount_minor: int
    currency: str
    credit_kind: CreditKind
    channel_type: str
    matching_method: MatchingMethod
    confidence: float
    confirmed_by: str
    confirmed_at: str
    reversed_at: Optional[str] = None
    reversal_transaction_id: Optional[str] = None


class OutboundPacket(BaseModel):
    id: str
    channel_id: str
    action_id: str
    recipient_ref: str
    recipient_address: str
    subject: str
    body: str
    message_hash: str
    attachment_manifest: List[Dict[str, Any]] = Field(default_factory=list)
    approval_id: Optional[str] = None
    provider_message_ref: Optional[str] = None
    sent_at: Optional[str] = None
    created_at: str


class Action(BaseModel):
    id: str
    case_id: str
    channel_id: Optional[str] = None
    type: ActionType
    payload: Dict[str, Any]
    payload_hash: str
    status: ActionStatus
    approval_id: Optional[str] = None
    provider_ref: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_ref: str
    expected_case_version: int
    failure_reason: Optional[str] = None
    created_at: str
    updated_at: str


class Approval(BaseModel):
    id: str
    action_id: str
    approver_id: Optional[str] = None
    action_hash: str
    scope: Dict[str, Any]
    challenge_id: str
    challenge_expires_at: str
    expected_case_version: int
    approved_at: Optional[str] = None
    revoked_at: Optional[str] = None
    revoke_reason: Optional[str] = None
    consumed_at: Optional[str] = None
    consumed_by_action_id: Optional[str] = None
    created_at: str


class CaseEvent(BaseModel):
    id: str
    case_id: str
    sequence: int
    event_type: str
    source_event_id: Optional[str] = None
    actor: str
    occurred_at: str
    previous_state: Optional[str] = None
    next_state: Optional[str] = None
    expected_version: Optional[int] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    id: str
    type: str
    case_id: Optional[str] = None
    payload: Dict[str, Any]
    run_at: str
    lease_owner: Optional[str] = None
    lease_until: Optional[str] = None
    attempts: int = 0
    status: str
    dedupe_key: Optional[str] = None
    last_error: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
