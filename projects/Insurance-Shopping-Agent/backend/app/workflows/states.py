"""Application case states (separate from the A2A task lifecycle)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Set

from sqlalchemy.orm import Session

from ..persistence import models as m
from ..persistence.db import new_id
from ..persistence.outbox import enqueue_event
from ..persistence.repositories import next_event_sequence

COLLECTING = "collecting"
QUOTING = "quoting"
NEEDS_INFORMATION = "needs_information"
COMPARING = "comparing"
AWAITING_SELECTION = "awaiting_selection"
AWAITING_APPROVAL = "awaiting_approval"
SUBMITTED = "submitted"
UNDERWRITING = "underwriting"
REVISED_OFFER = "revised_offer"
BOUND = "bound"
ISSUED = "issued"
COMPLETED = "completed"
DECLINED = "declined"
EXPIRED = "expired"
MANUAL_REVIEW = "manual_review"

TERMINAL = {COMPLETED}
PRE_SUBMISSION = {COLLECTING, QUOTING, NEEDS_INFORMATION, COMPARING, AWAITING_SELECTION, AWAITING_APPROVAL, EXPIRED, DECLINED}

ALLOWED: Dict[str, Set[str]] = {
    COLLECTING: {QUOTING, MANUAL_REVIEW, EXPIRED},
    QUOTING: {NEEDS_INFORMATION, COMPARING, COLLECTING, MANUAL_REVIEW},
    NEEDS_INFORMATION: {QUOTING, COMPARING, COLLECTING, MANUAL_REVIEW},
    COMPARING: {AWAITING_SELECTION, NEEDS_INFORMATION, QUOTING, COLLECTING, MANUAL_REVIEW},
    AWAITING_SELECTION: {AWAITING_APPROVAL, EXPIRED, QUOTING, COLLECTING, NEEDS_INFORMATION, MANUAL_REVIEW},
    AWAITING_APPROVAL: {SUBMITTED, AWAITING_SELECTION, QUOTING, COLLECTING, EXPIRED, MANUAL_REVIEW},
    SUBMITTED: {UNDERWRITING, MANUAL_REVIEW},
    UNDERWRITING: {REVISED_OFFER, BOUND, DECLINED, MANUAL_REVIEW},
    REVISED_OFFER: {AWAITING_APPROVAL, AWAITING_SELECTION, DECLINED, MANUAL_REVIEW},
    BOUND: {ISSUED, MANUAL_REVIEW},
    ISSUED: {COMPLETED, MANUAL_REVIEW},
    MANUAL_REVIEW: {UNDERWRITING, AWAITING_APPROVAL, AWAITING_SELECTION, ISSUED, COMPLETED, DECLINED, COLLECTING, QUOTING},
    DECLINED: {AWAITING_SELECTION, COLLECTING},
    EXPIRED: {QUOTING, COLLECTING},
    COMPLETED: set(),
}


class IllegalTransition(Exception):
    pass


class StaleVersion(Exception):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__("case version is stale: expected %d, actual %d" % (expected, actual))


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED.get(from_state, set())


def transition(
    session: Session,
    case: m.Case,
    to_state: str,
    actor: str,
    event_type: str,
    now: datetime,
    environment: str,
    data: Optional[Dict[str, Any]] = None,
    expected_version: Optional[int] = None,
    source_event_id: Optional[str] = None,
) -> m.CaseEvent:
    """Enforce allowed transitions server-side and record the event + outbox message."""
    if expected_version is not None and expected_version != case.version:
        raise StaleVersion(expected_version, case.version)
    if to_state != case.state and not can_transition(case.state, to_state):
        raise IllegalTransition("cannot move case %s from %s to %s" % (case.id, case.state, to_state))
    return record_event(session, case, event_type, actor, now, environment, data, to_state=to_state, source_event_id=source_event_id)


def record_event(
    session: Session,
    case: m.Case,
    event_type: str,
    actor: str,
    now: datetime,
    environment: str,
    data: Optional[Dict[str, Any]] = None,
    to_state: Optional[str] = None,
    source_event_id: Optional[str] = None,
) -> m.CaseEvent:
    """Record an event; bumps the case version even when the state does not change."""
    from_state = case.state
    if to_state is not None:
        case.state = to_state
    case.version += 1
    case.updated_at = now
    event = m.CaseEvent(
        id=new_id("cevt"),
        case_id=case.id,
        sequence=next_event_sequence(session, case.id),
        event_type=event_type,
        source_event_id=source_event_id,
        actor=actor,
        occurred_at=now,
        from_state=from_state,
        to_state=case.state,
        expected_version=case.version - 1,
        data_json=data or {},
    )
    session.add(event)
    enqueue_event(session, case.id, event_type, {**(data or {}), "from_state": from_state, "to_state": case.state, "case_version": case.version}, now, environment)
    return event
