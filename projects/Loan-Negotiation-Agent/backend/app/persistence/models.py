"""SQLAlchemy models for the loan negotiation case store.

Amounts are integer minor units with an explicit ``currency``; rates are stored as
fixed-precision decimal strings. Tenant ownership is enforced through
``customer_id``/``tenant_id`` on every customer-scoped row and checked in the
service layer on every read.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    api_token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    verified_facts_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    property_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)


class Mortgage(Base):
    __tablename__ = "mortgages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    balance_minor: Mapped[int] = mapped_column(Integer)
    note_rate_decimal: Mapped[str] = mapped_column(String(16))
    remaining_months: Mapped[int] = mapped_column(Integer)
    monthly_pi_minor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    escrow_minor: Mapped[int] = mapped_column(Integer, default=0)
    payment_includes_escrow: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    as_of: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    evidence_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    servicer_lender_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    balance_confirmed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    extraction_version: Mapped[str] = mapped_column(String(32), default="fixture-1")
    content_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), index=True)
    workflow_type: Mapped[str] = mapped_column(String(64), default="mortgage_refinance_review")
    mortgage_id: Mapped[str] = mapped_column(String(64), ForeignKey("mortgages.id"))
    state: Mapped[str] = mapped_column(String(48), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    holding_horizon_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    maximum_cash_to_close_minor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    finance_costs_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    missing_fields_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    outstanding_questions_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    selected_offer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    latest_comparison_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    completion_evidence_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class LoanOffer(Base):
    __tablename__ = "loan_offers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    lender_id: Mapped[str] = mapped_column(String(64), index=True)
    document_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    offer_family_id: Mapped[str] = mapped_column(String(64), index=True)  # groups versions of one lender's offer
    version: Mapped[int] = mapped_column(Integer, default=1)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    principal_minor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    term_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    note_rate_decimal: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    apr_disclosed: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    cost_items_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    credits_minor: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="indicative_quote")  # indicative_quote|revised_quote|final_offer|refused|expired|superseded
    normalized_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    provider_reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Comparison(Base):
    __tablename__ = "comparisons"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    mortgage_id: Mapped[str] = mapped_column(String(64))
    offer_ids_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    horizon_months: Mapped[int] = mapped_column(Integer)
    calculation_version: Mapped[str] = mapped_column(String(64))
    schedule_refs_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    cash_to_close_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    economic_cost_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class LenderRequest(Base):
    __tablename__ = "lender_requests"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    lender_id: Mapped[str] = mapped_column(String(64))
    target_offer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    competing_offer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    message_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    approved_message_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    disclosed_documents_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    external_request_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    action_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft|approved|sent|answered|refused|failed
    response_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RefinanceApplication(Base):
    __tablename__ = "refinance_applications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    offer_id: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[str] = mapped_column(String(64))
    lender_id: Mapped[str] = mapped_column(String(64))
    document_manifest_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    packet_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    external_application_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    client_request_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    conditions_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    final_terms_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    closing_evidence_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft|approved|submitted|conditions_outstanding|approved_offer|final_review|mock_closed|declined|withdrawn|manual_review
    action_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TermReviewRecord(Base):
    __tablename__ = "term_reviews"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(String(64), ForeignKey("refinance_applications.id"), index=True)
    earlier_offer_id: Mapped[str] = mapped_column(String(64))
    final_terms_id: Mapped[str] = mapped_column(String(64))
    differences_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    requires_reapproval: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    type: Mapped[str] = mapped_column(String(48))  # send_negotiation|submit_application|provide_documents
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|approved|executing|completed|failed|uncertain|cancelled|invalidated
    approval_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    client_request_ref: Mapped[str] = mapped_column(String(128), unique=True)
    review_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    bound_case_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("case_id", "idempotency_key", name="uq_actions_case_idem"),)


class ApprovalChallenge(Base):
    __tablename__ = "approval_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(64), ForeignKey("actions.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(64), ForeignKey("actions.id"), index=True)
    approver_customer_id: Mapped[str] = mapped_column(String(64))
    action_hash: Mapped[str] = mapped_column(String(128))
    scope_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    revocation_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    consumed_by_job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CaseEvent(Base):
    __tablename__ = "case_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    source_event_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    previous_state: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    next_state: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    expected_case_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    data_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("case_id", "sequence", name="uq_case_events_seq"),)


class ToolRun(Base):
    __tablename__ = "tool_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    input_ref: Mapped[str] = mapped_column(Text)
    output_ref: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    authority: Mapped[str] = mapped_column(String(16))  # authoritative|estimated|simulated
    source_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    model_version: Mapped[str] = mapped_column(String(64), default="rules-v1")
    prompt_version: Mapped[str] = mapped_column(String(64), default="prompt-v1")
    outcome: Mapped[str] = mapped_column(String(32), default="ok")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OutboxMessage(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    signature: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|delivered|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class ProviderEvent(Base):
    __tablename__ = "provider_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    provider_event_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="received")  # received|processed|ignored|failed
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    processed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    __table_args__ = (UniqueConstraint("provider", "provider_event_id", name="uq_provider_event"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|leased|done|failed|dead
    run_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("cases.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # borrower|agent|system
    content: Mapped[str] = mapped_column(Text)
    data_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


Index("ix_actions_status", Action.status)
Index("ix_jobs_status_run_at", Job.status, Job.run_at)
