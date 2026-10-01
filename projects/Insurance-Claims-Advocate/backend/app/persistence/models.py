"""SQLAlchemy models. Timestamps are ISO-8601 UTC strings so timezone is explicit and portable across SQLite/Postgres.
Fiat values are integer minor units with an explicit currency."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="tenant_demo")
    full_name: Mapped[str] = mapped_column(String(200))
    email_masked: Mapped[str] = mapped_column(String(200), default="")
    payout_destination_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40))


class Principal(Base):
    """Fixture authentication: bearer token -> identity. Tokens are synthetic."""
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(80), unique=True)
    role: Mapped[str] = mapped_column(String(32))  # customer | representative | operator
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200))


class Policy(Base):
    __tablename__ = "policies"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"))
    insurer_name: Mapped[str] = mapped_column(String(200))
    fixture_json: Mapped[Dict[str, Any]] = mapped_column(JSON)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"))
    doc_type: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(64), default="customer_upload")
    captured_at: Mapped[str] = mapped_column(String(40))
    extraction_version: Mapped[str] = mapped_column(String(32), default="")
    content_json: Mapped[Dict[str, Any]] = mapped_column(JSON)  # immutable "OCR" text per page/line
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(40))


class ClaimCase(Base):
    __tablename__ = "claim_cases"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="tenant_demo")
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), index=True)
    policy_id: Mapped[str] = mapped_column(String(64), ForeignKey("policies.id"))
    policy_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    workflow_type: Mapped[str] = mapped_column(String(64), default="baggage_delay_claim")
    loss_type: Mapped[str] = mapped_column(String(64))
    loss_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="collecting")
    version: Mapped[int] = mapped_column(Integer, default=1)
    external_claim_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    mock_scenario: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    evaluation_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    followups_used: Mapped[int] = mapped_column(Integer, default=0)
    completion_evidence_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


class CaseDocument(Base):
    __tablename__ = "case_documents"
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), ForeignKey("documents.id"), primary_key=True)
    attached_at: Mapped[str] = mapped_column(String(40))


class ClaimFact(Base):
    __tablename__ = "claim_facts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    fact_type: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    evidence_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_locator: Mapped[str] = mapped_column(String(200))
    confidence: Mapped[str] = mapped_column(String(16), default="high")  # high | low
    uncertainty_flags_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    confirmation_status: Mapped[str] = mapped_column(String(32), default="extracted")  # extracted | needs_confirmation | confirmed | rejected | customer_statement
    confirmed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(40))


class ClaimedExpense(Base):
    __tablename__ = "claimed_expenses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    receipt_id: Mapped[str] = mapped_column(String(64))
    receipt_hash: Mapped[str] = mapped_column(String(80))
    merchant: Mapped[str] = mapped_column(String(200))
    purchased_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text, default="")
    eligibility_status: Mapped[str] = mapped_column(String(24), default="uncertain")  # supported | excluded | uncertain | duplicate
    rule_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    exclusion_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duplicate_of: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_locator: Mapped[str] = mapped_column(String(200), default="")
    receipt_flags_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    confirmations_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))


class CaseQuestion(Base):
    __tablename__ = "case_questions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    field: Mapped[str] = mapped_column(String(80))
    question: Mapped[str] = mapped_column(Text)
    fact_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expense_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | answered
    answer_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    asked_by: Mapped[str] = mapped_column(String(64), default="agent")
    created_at: Mapped[str] = mapped_column(String(40))
    answered_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class Packet(Base):
    __tablename__ = "packets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    packet_type: Mapped[str] = mapped_column(String(24))  # submission | supplemental | appeal
    content_hash: Mapped[str] = mapped_column(String(80))
    content_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    case_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40))


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(32))  # submit_claim | add_evidence | submit_appeal
    packet_id: Mapped[str] = mapped_column(String(64), ForeignKey("packets.id"))
    payload_hash: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="proposed")  # proposed | approved | executing | succeeded | failed | uncertain | invalidated | manual_review
    approval_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    expected_case_version: Mapped[int] = mapped_column(Integer)
    review_summary_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


class ApprovalChallenge(Base):
    __tablename__ = "approval_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(64), ForeignKey("actions.id"))
    expires_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | used | expired | invalidated
    created_at: Mapped[str] = mapped_column(String(40))


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(64), ForeignKey("actions.id"))
    approver_id: Mapped[str] = mapped_column(String(64))
    action_hash: Mapped[str] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str] = mapped_column(String(40))
    revoked_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    consumed_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    consumed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))


class ClaimSubmission(Base):
    __tablename__ = "claim_submissions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    submission_kind: Mapped[str] = mapped_column(String(24))  # initial | supplemental | appeal
    packet_id: Mapped[str] = mapped_column(String(64), ForeignKey("packets.id"))
    packet_hash: Mapped[str] = mapped_column(String(80))
    approval_id: Mapped[str] = mapped_column(String(64))
    action_id: Mapped[str] = mapped_column(String(64))
    external_claim_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    external_submission_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    submitted_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="accepted")
    environment: Mapped[str] = mapped_column(String(16), default="mock")
    __table_args__ = (UniqueConstraint("case_id", "sequence", name="uq_submission_sequence"),)


class InsurerRequest(Base):
    __tablename__ = "insurer_requests"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    provider_request_id: Mapped[str] = mapped_column(String(120))
    requirement_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    due_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    deadline_source: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | satisfied | expired
    satisfied_by_submission_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("case_id", "provider_request_id", name="uq_insurer_request"),)


class ClaimDecision(Base):
    __tablename__ = "claim_decisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    decision_version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))  # approved | partially_approved | denied
    accepted_minor: Mapped[int] = mapped_column(Integer)
    rejected_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    reason_items_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    evidence_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # provider event id
    explanation_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    appeal_status: Mapped[str] = mapped_column(String(24), default="none")  # none | supported_challenge | unsupported | appealed | appeal_declined
    provider_decision_ref: Mapped[str] = mapped_column(String(120), default="")
    decided_at: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("case_id", "decision_version", name="uq_decision_version"),)


class ClaimPayment(Base):
    __tablename__ = "claim_payments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("claim_cases.id"), nullable=True, index=True)
    decision_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_payment_ref: Mapped[str] = mapped_column(String(120), unique=True)
    claim_reference: Mapped[str] = mapped_column(String(120))
    payee_id: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    payment_status: Mapped[str] = mapped_column(String(16), default="posted")  # posted | provisional
    posted_at: Mapped[str] = mapped_column(String(40))
    reconciliation_status: Mapped[str] = mapped_column(String(32), default="unreconciled")
    reconciliation_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    environment: Mapped[str] = mapped_column(String(16), default="mock")
    created_at: Mapped[str] = mapped_column(String(40))


class CaseEvent(Base):
    __tablename__ = "case_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    source_event_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    previous_state: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    next_state: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    expected_case_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    data_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("case_id", "sequence", name="uq_case_event_sequence"),)


class InboxEvent(Base):
    """Deduplication inbox for provider callbacks (at-least-once delivery)."""
    __tablename__ = "inbox_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    provider_event_id: Mapped[str] = mapped_column(String(120))
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(40), default="processed")  # processed | unmatched | ignored_out_of_order | rejected:*
    duplicate_deliveries: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("provider", "provider_event_id", name="uq_inbox_provider_event"),)


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | delivered | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    signature: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))
    delivered_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class Job(Base):
    """Persisted worker jobs with leases."""
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    run_at: Mapped[str] = mapped_column(String(40))
    lease_until: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    locked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | done | failed
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, unique=True)
    created_at: Mapped[str] = mapped_column(String(40))
    finished_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


Index("ix_jobs_pending", Job.status, Job.run_at)


class ToolRun(Base):
    __tablename__ = "tool_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    turn_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    input_ref: Mapped[str] = mapped_column(String(80))  # redacted hash of input
    output_ref: Mapped[str] = mapped_column(String(80))
    input_summary_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    source_timestamp: Mapped[str] = mapped_column(String(40))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    model_version: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    outcome: Mapped[str] = mapped_column(String(16), default="ok")  # ok | error | refused
    created_at: Mapped[str] = mapped_column(String(40))


class AgentTurn(Base):
    __tablename__ = "agent_turns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("claim_cases.id"), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    customer_message: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    planner: Mapped[str] = mapped_column(String(32), default="scripted")
    created_at: Mapped[str] = mapped_column(String(40))


class ProviderRequestLog(Base):
    """Operator-visible adapter call log (redacted)."""
    __tablename__ = "provider_request_log"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    adapter: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(32))
    request_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    outcome: Mapped[str] = mapped_column(String(24))
    environment: Mapped[str] = mapped_column(String(16))
    detail_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40))


class MockInsurerClaim(Base):
    """State of the mock insurer, persisted so a worker restart cannot 'lose' a claim the insurer already accepted."""
    __tablename__ = "mock_insurer_claims"
    claim_ref: Mapped[str] = mapped_column(String(120), primary_key=True)
    request_ref: Mapped[str] = mapped_column(String(120), unique=True)
    packet_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    scenario: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32))
    submissions_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    decision_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    open_request_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


class MockRequestRef(Base):
    """Every request_ref the mock insurer has seen (including supplemental writes), for find_submission."""
    __tablename__ = "mock_request_refs"
    request_ref: Mapped[str] = mapped_column(String(120), primary_key=True)
    claim_ref: Mapped[str] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(32))
    result_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40))


class A2ATaskMap(Base):
    __tablename__ = "a2a_task_map"
    external_task_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), index=True)
    action_id: Mapped[str] = mapped_column(String(64))
    protocol_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(40))
