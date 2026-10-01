"""SQLAlchemy models.

Conventions (plan §10):
* fiat values are integer minor units and always carry a currency;
* rates are stored as fixed-precision decimal strings (``apy_decimal``);
* timestamps are stored as ISO-8601 UTC strings so SQLite and PostgreSQL
  behave identically and the values sort lexicographically;
* tenant ownership (customer_id) and unique provider/event references are
  enforced by database constraints.

The ``mock_bank_*`` tables belong to the bank simulator. They are kept
separate from the application's own snapshot tables so verification can
discover a mismatch between what the app projected and what the bank did.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Store aware UTC datetimes as ISO strings."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON, datetime: UTCDateTime}


# --------------------------------------------------------------------------- #
# Customer data
# --------------------------------------------------------------------------- #


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime]


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(16), default="mock")


class BankAccount(Base):
    """Application-side point-in-time snapshot of a customer account."""

    __tablename__ = "bank_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    account_kind: Mapped[str] = mapped_column(String(32))  # checking|savings|fixed_term_deposit
    display_name: Mapped[str] = mapped_column(String(128))
    ownership_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    access_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    available_minor: Mapped[int] = mapped_column(Integer)
    current_minor: Mapped[int] = mapped_column(Integer)
    pending_json: Mapped[list] = mapped_column(JSON, default=list)
    snapshot_at: Mapped[datetime]
    snapshot_source: Mapped[str] = mapped_column(String(32), default="mock")
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)


class Obligation(Base):
    __tablename__ = "obligations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    description: Mapped[str] = mapped_column(String(128))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    due_date: Mapped[date] = mapped_column(Date)
    certainty: Mapped[str] = mapped_column(String(16), default="confirmed")  # confirmed|estimated
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)


class DepositContract(Base):
    __tablename__ = "deposit_contracts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    principal_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    apy_decimal: Mapped[str] = mapped_column(String(16))
    maturity_date: Mapped[date] = mapped_column(Date)
    renewal_instruction_deadline: Mapped[date] = mapped_column(Date)
    grace_period_days: Mapped[int] = mapped_column(Integer, default=10)
    contract_version: Mapped[str] = mapped_column(String(32))
    default_maturity_behavior: Mapped[str] = mapped_column(String(64))
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)


class DepositOffer(Base):
    """A provider-sourced product version, refreshed from the adapter."""

    __tablename__ = "deposit_offers"
    __table_args__ = (UniqueConstraint("provider_id", "product_code", "product_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    product_code: Mapped[str] = mapped_column(String(64))
    product_name: Mapped[str] = mapped_column(String(128))
    product_version: Mapped[str] = mapped_column(String(32))
    offer_kind: Mapped[str] = mapped_column(String(32))  # cd_renewal | cd_new | savings_transfer
    apy_decimal: Mapped[str] = mapped_column(String(16))
    rate_type: Mapped[str] = mapped_column(String(16), default="fixed")  # fixed|variable
    term_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fees_minor: Mapped[int] = mapped_column(Integer, default=0)
    fee_description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    restrictions_json: Mapped[dict] = mapped_column(JSON, default=dict)
    accrual_method: Mapped[str] = mapped_column(String(32))  # apy_compound | simple_contractual | unknown
    rounding: Mapped[str] = mapped_column(String(32), default="ROUND_HALF_EVEN")
    valid_until: Mapped[date] = mapped_column(Date)
    eligibility_status: Mapped[str] = mapped_column(String(16))  # eligible|conditional|unknown|ineligible
    eligibility_notes: Mapped[str | None] = mapped_column(String(256), nullable=True)
    destination_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    for_deposit_id: Mapped[str] = mapped_column(String(64), index=True)
    retrieved_at: Mapped[datetime]
    environment: Mapped[str] = mapped_column(String(16), default="mock")
    authoritative: Mapped[bool] = mapped_column(Boolean, default=True)
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime]
    extraction_version: Mapped[str] = mapped_column(String(32), default="fixture-v1")
    summary: Mapped[str | None] = mapped_column(String(256), nullable=True)


# --------------------------------------------------------------------------- #
# Case, plan, action, approval
# --------------------------------------------------------------------------- #


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    workflow_type: Mapped[str] = mapped_column(String(32), default="cd_maturity")
    state: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    deposit_id: Mapped[str] = mapped_column(ForeignKey("deposit_contracts.id"))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    minimum_buffer_minor: Mapped[int] = mapped_column(Integer)
    obligation_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    preferred_lockup_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buffer_includes_obligations: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    concentration_limit_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missing_fields_json: Mapped[list] = mapped_column(JSON, default=list)
    outstanding_questions_json: Mapped[list] = mapped_column(JSON, default=list)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    provider_mode: Mapped[str] = mapped_column(String(32), default="normal")
    selected_offer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_evidence_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class CashPlan(Base):
    __tablename__ = "cash_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    inputs_hash: Mapped[str] = mapped_column(String(80))
    projection_json: Mapped[dict] = mapped_column(JSON)
    lowest_balance_minor: Mapped[int] = mapped_column(Integer)
    max_lockable_minor: Mapped[int] = mapped_column(Integer)
    allocation_minor: Mapped[int] = mapped_column(Integer)
    offer_id: Mapped[str | None] = mapped_column(ForeignKey("deposit_offers.id"), nullable=True)
    offer_product_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comparison_json: Mapped[list] = mapped_column(JSON, default=list)
    assumptions_json: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|selected|superseded
    created_at: Mapped[datetime]


class Action(Base):
    """Immutable proposed external action. Approval binds to ``payload_hash``."""

    __tablename__ = "actions"
    __table_args__ = (UniqueConstraint("idempotency_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))  # cd_renewal | same_owner_transfer
    payload_json: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    # proposed|approved|submitting|submitted|outcome_unknown|rejected|effective|verified|superseded|cancelled|manual_review
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80))
    request_ref: Mapped[str] = mapped_column(String(80), unique=True)
    case_version_at_creation: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApprovalChallenge(Base):
    __tablename__ = "approval_challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), index=True)
    customer_id: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(80))
    case_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime]


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), index=True)
    approver_id: Mapped[str] = mapped_column(String(64))
    action_hash: Mapped[str] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(64))
    challenge_id: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime]


class BankInstruction(Base):
    __tablename__ = "bank_instructions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("cash_plans.id"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"))
    instruction_type: Mapped[str] = mapped_column(String(32))
    source_account_id: Mapped[str] = mapped_column(String(64))
    destination_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    effective_at: Mapped[date] = mapped_column(Date)
    offer_id: Mapped[str] = mapped_column(String(64))
    product_version: Mapped[str] = mapped_column(String(32))
    term_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    apy_decimal: Mapped[str] = mapped_column(String(16))
    external_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_ref: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default="prepared")
    reconciliation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class CaseEvent(Base):
    __tablename__ = "case_events"
    __table_args__ = (
        UniqueConstraint("case_id", "sequence"),
        Index("ix_case_events_source", "source_event_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    source_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_case_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime]


class ToolRun(Base):
    __tablename__ = "tool_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    input_ref: Mapped[str] = mapped_column(String(80))
    output_ref: Mapped[str] = mapped_column(String(80))
    input_redacted_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_timestamps_json: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(16))  # ok | error | refused
    created_at: Mapped[datetime]


# --------------------------------------------------------------------------- #
# Messaging, jobs, provider observability
# --------------------------------------------------------------------------- #


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64))
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON)
    signature: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime]
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)


class InboxEvent(Base):
    __tablename__ = "inbox_events"
    __table_args__ = (UniqueConstraint("provider_id", "event_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime]
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    result: Mapped[str | None] = mapped_column(String(256), nullable=True)
    replay_count: Mapped[int] = mapped_column(Integer, default=0)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(32))  # execute_action | reconcile_action | verify_action | expire_approvals
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|leased|done|failed
    run_at: Mapped[datetime]
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AdapterRequest(Base):
    """Operator-visible record of every provider call (redacted)."""

    __tablename__ = "adapter_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(64))
    request_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_redacted_json: Mapped[dict] = mapped_column(JSON, default=dict)
    response_redacted_json: Mapped[dict] = mapped_column(JSON, default=dict)
    environment: Mapped[str] = mapped_column(String(16))
    latency_ms: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))  # ok | declined | timeout | malformed | error
    created_at: Mapped[datetime]


class SimClock(Base):
    __tablename__ = "sim_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    now: Mapped[datetime]


# --------------------------------------------------------------------------- #
# Mock bank ledger (provider side)
# --------------------------------------------------------------------------- #


class MockBankAccount(Base):
    __tablename__ = "mock_bank_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    available_minor: Mapped[int] = mapped_column(Integer)
    current_minor: Mapped[int] = mapped_column(Integer)
    pending_json: Mapped[list] = mapped_column(JSON, default=list)
    access_revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class MockBankDeposit(Base):
    __tablename__ = "mock_bank_deposits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    principal_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    apy_decimal: Mapped[str] = mapped_column(String(16))
    term_days: Mapped[int] = mapped_column(Integer)
    opened_on: Mapped[date] = mapped_column(Date)
    maturity_date: Mapped[date] = mapped_column(Date)
    product_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|matured|renewed|closed
    matured_from_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    maturity_proceeds_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MockBankOffer(Base):
    __tablename__ = "mock_bank_offers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    for_deposit_id: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MockBankInstruction(Base):
    __tablename__ = "mock_bank_instructions"

    request_ref: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider_reference: Mapped[str] = mapped_column(String(64), unique=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))  # accepted|effective|declined|cancelled
    decline_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    effective_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    callback_delay_days: Mapped[int] = mapped_column(Integer, default=0)
    callback_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    credited_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted_at: Mapped[datetime]


class MockBankConfig(Base):
    __tablename__ = "mock_bank_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    next_submit_mode: Mapped[str] = mapped_column(String(32), default="normal")
    offer_mode: Mapped[str] = mapped_column(String(32), default="normal")
