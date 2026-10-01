"""SQLAlchemy models mirroring the plan's data model (section 10)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    workflow_type: Mapped[str] = mapped_column(String(64), default="insurance_shopping")
    state: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    current_needs_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    selected_quote_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_application_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completion_evidence_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class InsuranceNeedsRow(Base):
    __tablename__ = "insurance_needs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    state_code: Mapped[str] = mapped_column(String(2))
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    property_limit_minor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    liability_limit_minor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deductible_cap_minor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    replacement_cost_required: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    data_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("case_id", "version", name="uq_needs_case_version"),)


class UnderwritingAnswer(Base):
    __tablename__ = "underwriting_answers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    needs_id: Mapped[str] = mapped_column(String(64), ForeignKey("insurance_needs.id"))
    question_id: Mapped[str] = mapped_column(String(64))
    provider_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    answer_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    evidence_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    answer_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("case_id", "question_id", "answer_version", name="uq_answer_version"),)


class QuoteTask(Base):
    __tablename__ = "quote_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    needs_id: Mapped[str] = mapped_column(String(64), ForeignKey("insurance_needs.id"))
    needs_version: Mapped[int] = mapped_column(Integer)
    insurer_id: Mapped[str] = mapped_column(String(64), index=True)
    external_task_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    request_ref: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32))  # pending|input_required|quoted|declined|failed|timeout|superseded
    open_questions_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    environment: Mapped[str] = mapped_column(String(16), default="mock")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("insurer_id", "external_task_id", name="uq_task_external"),)


class InsuranceQuote(Base):
    __tablename__ = "insurance_quotes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    quote_task_id: Mapped[str] = mapped_column(String(64), ForeignKey("quote_tasks.id"))
    insurer_id: Mapped[str] = mapped_column(String(64), index=True)
    needs_version: Mapped[int] = mapped_column(Integer)
    quote_version: Mapped[int] = mapped_column(Integer, default=1)
    quote_ref: Mapped[str] = mapped_column(String(128))
    annual_premium_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    coverage_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    exclusions_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON)
    policy_form_version: Mapped[str] = mapped_column(String(64))
    valid_until: Mapped[datetime] = mapped_column(UTCDateTime)
    answers_hash: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32))
    quote_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    superseded_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("insurer_id", "quote_ref", "quote_version", name="uq_quote_ref_version"),)


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    quote_id: Mapped[str] = mapped_column(String(64), ForeignKey("insurance_quotes.id"))
    answers_hash: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(80))
    submission_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # prepared|approved|submitted|underwriting|revised|bound|declined|superseded
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class IssuedPolicy(Base):
    __tablename__ = "issued_policies"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    application_id: Mapped[str] = mapped_column(String(64), ForeignKey("applications.id"))
    insurer_policy_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    effective_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expires_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    declarations_document_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    declarations_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    verified_against_quote_id: Mapped[str] = mapped_column(String(64))
    verification_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    object_key: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime)
    extraction_version: Mapped[str] = mapped_column(String(32), default="none")
    content_json: Mapped[Dict[str, Any]] = mapped_column(JSON)  # prototype: inline; production: object storage


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32))  # proposed|approved|executing|executed|uncertain|failed|rejected|invalidated
    approval_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    challenge_id: Mapped[str] = mapped_column(String(64))
    challenge_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expected_case_version: Mapped[int] = mapped_column(Integer)
    application_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    result_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(64), ForeignKey("actions.id"), index=True)
    approver_id: Mapped[str] = mapped_column(String(64))
    action_hash: Mapped[str] = mapped_column(String(80))
    scope_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    consumed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CaseEvent(Base):
    __tablename__ = "case_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(96))
    source_event_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    from_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    expected_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    data_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("case_id", "sequence", name="uq_case_event_sequence"),)


class ToolRun(Base):
    __tablename__ = "tool_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    input_redacted: Mapped[Dict[str, Any]] = mapped_column(JSON)
    output_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime)
    latency_ms: Mapped[int] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(96))
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|delivered|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class InboxEvent(Base):
    __tablename__ = "inbox_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    external_event_id: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    processed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="received")  # received|processed|ignored|failed
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("provider", "external_event_id", name="uq_inbox_provider_event"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, unique=True)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|dead
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # customer|agent|tool
    content: Mapped[str] = mapped_column(Text)
    tool_calls_json: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ProviderRequestLog(Base):
    __tablename__ = "provider_requests"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    insurer_id: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(64))
    request_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    request_redacted: Mapped[Dict[str, Any]] = mapped_column(JSON)
    response_redacted: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    environment: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    latency_ms: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))


Index("ix_jobs_claim", Job.status, Job.run_at, Job.lease_until)
