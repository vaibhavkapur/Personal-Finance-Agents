"""Case lifecycle: states, allowed transitions and the transition recorder.

Transitions are enforced server-side. Every transition records previous state,
next state, event id, actor, timestamp and the expected case version; a stale
version raises ``StaleVersionError`` (mapped to HTTP 409).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..persistence.db import new_id
from ..persistence.models import Case, CaseEvent


class WorkflowError(Exception):
    status_code = 400


class IllegalTransitionError(WorkflowError):
    status_code = 409


class StaleVersionError(WorkflowError):
    status_code = 409


# Terminal states never leave; manual_review is operational and non-terminal.
COLLECTING = "collecting"
COMPARING = "comparing"
AWAITING_DECISION = "awaiting_decision"
KEEP_CURRENT = "keep_current"
AWAITING_APPROVAL = "awaiting_approval"
NEGOTIATION_PENDING = "negotiation_pending"
REVISED_OFFER = "revised_offer"
APPLICATION_REVIEW = "application_review"
SUBMITTED = "submitted"
CONDITIONS_OUTSTANDING = "conditions_outstanding"
APPROVED_OFFER = "approved_offer"
FINAL_REVIEW = "final_review"
MOCK_CLOSED = "mock_closed"
DECLINED = "declined"
WITHDRAWN = "withdrawn"
MANUAL_REVIEW = "manual_review"

TERMINAL_STATES = {KEEP_CURRENT, MOCK_CLOSED, DECLINED, WITHDRAWN}

ALLOWED_TRANSITIONS: Dict[str, set] = {
    COLLECTING: {COLLECTING, COMPARING, MANUAL_REVIEW},
    COMPARING: {AWAITING_DECISION, COLLECTING, MANUAL_REVIEW},
    AWAITING_DECISION: {KEEP_CURRENT, AWAITING_APPROVAL, APPLICATION_REVIEW, COMPARING, COLLECTING, MANUAL_REVIEW},
    AWAITING_APPROVAL: {NEGOTIATION_PENDING, SUBMITTED, AWAITING_DECISION, APPLICATION_REVIEW, FINAL_REVIEW, MANUAL_REVIEW},
    NEGOTIATION_PENDING: {REVISED_OFFER, AWAITING_DECISION, MANUAL_REVIEW},
    REVISED_OFFER: {COMPARING, MANUAL_REVIEW},
    APPLICATION_REVIEW: {AWAITING_APPROVAL, AWAITING_DECISION, MANUAL_REVIEW},
    SUBMITTED: {CONDITIONS_OUTSTANDING, APPROVED_OFFER, DECLINED, WITHDRAWN, MANUAL_REVIEW},
    CONDITIONS_OUTSTANDING: {SUBMITTED, WITHDRAWN, DECLINED, MANUAL_REVIEW},
    APPROVED_OFFER: {FINAL_REVIEW, WITHDRAWN, MANUAL_REVIEW},
    FINAL_REVIEW: {MOCK_CLOSED, AWAITING_APPROVAL, WITHDRAWN, MANUAL_REVIEW},
    MANUAL_REVIEW: {COLLECTING, COMPARING, AWAITING_DECISION, NEGOTIATION_PENDING, SUBMITTED, CONDITIONS_OUTSTANDING, APPROVED_OFFER, FINAL_REVIEW, DECLINED, WITHDRAWN},
    KEEP_CURRENT: set(),
    MOCK_CLOSED: set(),
    DECLINED: set(),
    WITHDRAWN: set(),
}

# States in which the case is waiting on the customer vs. a provider (for metrics).
WAITING_ON_CUSTOMER = {COLLECTING, AWAITING_DECISION, AWAITING_APPROVAL, APPLICATION_REVIEW, CONDITIONS_OUTSTANDING, FINAL_REVIEW}
WAITING_ON_PROVIDER = {NEGOTIATION_PENDING, SUBMITTED}


def can_transition(current: str, nxt: str) -> bool:
    return nxt in ALLOWED_TRANSITIONS.get(current, set())


def next_sequence(session: Session, case_id: str) -> int:
    current = session.execute(select(func.max(CaseEvent.sequence)).where(CaseEvent.case_id == case_id)).scalar()
    return (current or 0) + 1


def record_event(
    session: Session,
    case: Case,
    event_type: str,
    actor: str,
    now,
    data: Optional[Dict[str, Any]] = None,
    source_event_id: Optional[str] = None,
    previous_state: Optional[str] = None,
    next_state: Optional[str] = None,
    expected_case_version: Optional[int] = None,
) -> CaseEvent:
    event = CaseEvent(
        id=new_id("evt"),
        case_id=case.id,
        sequence=next_sequence(session, case.id),
        event_type=event_type,
        source_event_id=source_event_id,
        actor=actor,
        previous_state=previous_state,
        next_state=next_state,
        expected_case_version=expected_case_version,
        data_json=data or {},
        occurred_at=now,
    )
    session.add(event)
    session.flush()
    return event


def transition(
    session: Session,
    case: Case,
    nxt: str,
    actor: str,
    now,
    expected_version: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    source_event_id: Optional[str] = None,
) -> CaseEvent:
    """Move ``case`` to ``nxt`` if allowed, bumping the version and recording an event."""
    if actor == "model":
        raise IllegalTransitionError("the model cannot change case state directly")
    if expected_version is not None and expected_version != case.version:
        raise StaleVersionError(f"case version is {case.version}, expected {expected_version}")
    if not can_transition(case.state, nxt):
        raise IllegalTransitionError(f"cannot move case from {case.state} to {nxt}")
    prev = case.state
    case.state = nxt
    case.version += 1
    case.updated_at = now
    session.add(case)
    return record_event(
        session,
        case,
        event_type=f"case.transition.{nxt}",
        actor=actor,
        now=now,
        data=data,
        source_event_id=source_event_id,
        previous_state=prev,
        next_state=nxt,
        expected_case_version=expected_version if expected_version is not None else case.version - 1,
    )


def bump_version(session: Session, case: Case, now, expected_version: Optional[int] = None) -> None:
    """Material inputs changed without a state change: bump the version so bound approvals become stale."""
    if expected_version is not None and expected_version != case.version:
        raise StaleVersionError(f"case version is {case.version}, expected {expected_version}")
    case.version += 1
    case.updated_at = now
    session.add(case)
