"""Case lifecycle (plan §8). Transitions are enforced server-side; every
transition records previous state, next state, event id, actor, timestamp and
the expected case version."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.ids import new_id
from app.persistence.models import Case, CaseEvent

DISCOVERED = "discovered"
COLLECTING = "collecting"
EVALUATING = "evaluating"
NEEDS_INFORMATION = "needs_information"
AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
NEEDS_REQUOTE = "needs_requote"
SUBMITTED = "submitted"
OUTCOME_UNKNOWN = "outcome_unknown"
VERIFYING = "verifying"
COMPLETED = "completed"
REJECTED = "rejected"
CANCELLED = "cancelled"
MANUAL_REVIEW = "manual_review"

TERMINAL = {COMPLETED, REJECTED, CANCELLED}

ALLOWED: dict[str, set[str]] = {
    DISCOVERED: {COLLECTING},
    COLLECTING: {EVALUATING, NEEDS_INFORMATION, CANCELLED},
    EVALUATING: {AWAITING_APPROVAL, NEEDS_INFORMATION, EVALUATING, CANCELLED, MANUAL_REVIEW},
    NEEDS_INFORMATION: {EVALUATING, CANCELLED},
    AWAITING_APPROVAL: {APPROVED, CANCELLED, EVALUATING, NEEDS_REQUOTE},
    APPROVED: {SUBMITTED, NEEDS_REQUOTE, OUTCOME_UNKNOWN, REJECTED, MANUAL_REVIEW},
    NEEDS_REQUOTE: {EVALUATING, CANCELLED},
    SUBMITTED: {VERIFYING, OUTCOME_UNKNOWN, REJECTED, MANUAL_REVIEW},
    OUTCOME_UNKNOWN: {VERIFYING, SUBMITTED, REJECTED, MANUAL_REVIEW},
    VERIFYING: {COMPLETED, MANUAL_REVIEW},
    MANUAL_REVIEW: {VERIFYING, SUBMITTED, EVALUATING, REJECTED, CANCELLED},
    COMPLETED: set(),
    REJECTED: set(),
    CANCELLED: set(),
}


class IllegalTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"illegal transition {current} -> {target}")
        self.current = current
        self.target = target


class StaleVersion(Exception):
    def __init__(self, expected: int, actual: int):
        super().__init__(f"stale case version: expected {expected}, actual {actual}")
        self.expected = expected
        self.actual = actual


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def next_sequence(session: Session, case_id: str) -> int:
    current = session.scalar(select(func.max(CaseEvent.sequence)).where(CaseEvent.case_id == case_id))
    return (current or 0) + 1


def record_event(
    session: Session,
    case: Case,
    event_type: str,
    actor: str,
    data: dict | None = None,
    previous_state: str | None = None,
    next_state: str | None = None,
    source_event_id: str | None = None,
    expected_case_version: int | None = None,
) -> CaseEvent:
    event = CaseEvent(
        id=new_id("cevt"),
        case_id=case.id,
        sequence=next_sequence(session, case.id),
        event_type=event_type,
        source_event_id=source_event_id,
        actor=actor,
        previous_state=previous_state,
        next_state=next_state,
        expected_case_version=expected_case_version,
        data_json=data or {},
        occurred_at=clock.now(session),
    )
    session.add(event)
    session.flush()
    return event


def transition(
    session: Session,
    case: Case,
    target: str,
    actor: str,
    reason: str | None = None,
    data: dict | None = None,
    expected_version: int | None = None,
    source_event_id: str | None = None,
) -> CaseEvent:
    """Move ``case`` to ``target``. Raises on illegal transitions and stale
    versions. Bumps the case version and records the event atomically with the
    caller's transaction."""
    if expected_version is not None and case.version != expected_version:
        raise StaleVersion(expected_version, case.version)
    if not can_transition(case.state, target):
        raise IllegalTransition(case.state, target)
    previous = case.state
    case.state = target
    case.version += 1
    case.updated_at = clock.now(session)
    if reason is not None:
        case.review_reason = reason
    elif target in (EVALUATING, AWAITING_APPROVAL, APPROVED, SUBMITTED, VERIFYING, COMPLETED):
        # Moving forward clears a stale hold/requote reason.
        case.review_reason = None
    payload = dict(data or {})
    if reason:
        payload.setdefault("reason", reason)
    return record_event(
        session,
        case,
        event_type="case.transitioned",
        actor=actor,
        data=payload,
        previous_state=previous,
        next_state=target,
        source_event_id=source_event_id,
        expected_case_version=expected_version if expected_version is not None else previous_version(case),
    )


def previous_version(case: Case) -> int:
    return case.version - 1
