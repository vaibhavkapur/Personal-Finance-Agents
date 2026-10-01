"""Case lifecycle. Transitions are enforced server-side; every transition is an event with previous/next state,
actor, timestamp and the case version it expected."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..ids import new_id
from ..persistence.models import CaseEvent, ClaimCase

TRANSITIONS: Dict[str, set] = {
    "collecting": {"evaluating"},
    "evaluating": {"collecting", "awaiting_approval"},
    "awaiting_approval": {"submitted", "evaluating", "collecting", "appeal_review", "evidence_requested", "manual_review"},
    "submitted": {"under_review", "manual_review"},
    "under_review": {"evidence_requested", "partially_approved", "approved", "denied", "manual_review"},
    "evidence_requested": {"awaiting_approval", "manual_review"},
    "approved": {"payout_pending"},
    "partially_approved": {"payout_pending", "appeal_review"},
    "payout_pending": {"paid", "appeal_review", "manual_review"},
    "paid": {"closed"},
    "denied": {"appeal_review", "closed_unpaid"},
    "appeal_review": {"awaiting_approval", "closed_unpaid", "payout_pending"},
    "manual_review": {"under_review", "submitted", "collecting", "evaluating", "closed_unpaid"},
    "closed": set(),
    "closed_unpaid": set(),
}

TERMINAL = {"closed", "closed_unpaid"}
WAITING_ON_CUSTOMER = {"collecting", "awaiting_approval", "appeal_review", "evidence_requested"}
WAITING_ON_PROVIDER = {"submitted", "under_review", "payout_pending"}


class TransitionError(ValueError):
    pass


class StaleVersionError(ValueError):
    pass


def next_sequence(session: Session, case_id: str) -> int:
    current = session.execute(select(func.max(CaseEvent.sequence)).where(CaseEvent.case_id == case_id)).scalar()
    return int(current or 0) + 1


def record_event(
    session: Session,
    clock: Clock,
    case: ClaimCase,
    event_type: str,
    actor: str,
    data: Optional[Dict[str, Any]] = None,
    *,
    source_event_id: Optional[str] = None,
    previous_state: Optional[str] = None,
    next_state: Optional[str] = None,
    bump_version: bool = True,
) -> CaseEvent:
    expected = case.version
    if bump_version:
        case.version = case.version + 1
    case.updated_at = clock.now_iso()
    event = CaseEvent(
        id=new_id("evt"),
        case_id=case.id,
        sequence=next_sequence(session, case.id),
        event_type=event_type,
        source_event_id=source_event_id,
        actor=actor,
        previous_state=previous_state,
        next_state=next_state,
        expected_case_version=expected,
        data_json=data or {},
        occurred_at=clock.now_iso(),
    )
    session.add(event)
    session.flush()
    return event


def transition(
    session: Session,
    clock: Clock,
    case: ClaimCase,
    next_state: str,
    actor: str,
    event_type: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    *,
    source_event_id: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> CaseEvent:
    if expected_version is not None and expected_version != case.version:
        raise StaleVersionError(f"case {case.id} is at version {case.version}, expected {expected_version}")
    allowed = TRANSITIONS.get(case.status, set())
    if next_state not in allowed:
        raise TransitionError(f"cannot move case {case.id} from {case.status} to {next_state}")
    previous = case.status
    case.status = next_state
    return record_event(session, clock, case, event_type or f"case.{next_state}", actor, data, source_event_id=source_event_id, previous_state=previous, next_state=next_state)
