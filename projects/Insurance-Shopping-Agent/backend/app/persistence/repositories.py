"""Thin query helpers over the ORM models."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domain.needs import InsuranceNeeds
from . import models as m
from .db import new_id


class NotFound(Exception):
    pass


def get_case(session: Session, case_id: str) -> m.Case:
    case = session.get(m.Case, case_id)
    if case is None:
        raise NotFound("case %s not found" % case_id)
    return case


def get_case_for_customer(session: Session, case_id: str, customer_id: str) -> m.Case:
    case = get_case(session, case_id)
    if case.customer_id != customer_id:
        # Tenant ownership: do not reveal that the case exists.
        raise NotFound("case %s not found" % case_id)
    return case


def current_needs(session: Session, case: m.Case) -> InsuranceNeeds:
    row = session.get(m.InsuranceNeedsRow, case.current_needs_id)
    if row is None:
        raise NotFound("needs for case %s not found" % case.id)
    return InsuranceNeeds(**row.data_json)


def current_needs_row(session: Session, case: m.Case) -> m.InsuranceNeedsRow:
    row = session.get(m.InsuranceNeedsRow, case.current_needs_id)
    if row is None:
        raise NotFound("needs for case %s not found" % case.id)
    return row


def save_needs_version(session: Session, case: m.Case, needs: InsuranceNeeds, now: datetime) -> m.InsuranceNeedsRow:
    row = m.InsuranceNeedsRow(
        id=new_id("needs"),
        case_id=case.id,
        customer_id=case.customer_id,
        state_code=needs.state_code,
        effective_date=needs.effective_date,
        version=needs.version,
        property_limit_minor=needs.property_limit_minor,
        liability_limit_minor=needs.liability_limit_minor,
        deductible_cap_minor=needs.deductible_cap_minor,
        replacement_cost_required=needs.replacement_cost_required,
        data_json=needs.model_dump(mode="json"),
        created_at=now,
    )
    session.add(row)
    case.current_needs_id = row.id
    return row


def latest_answers(session: Session, case_id: str) -> Dict[str, m.UnderwritingAnswer]:
    """Latest version of every confirmed answer keyed by question_id."""
    rows = session.scalars(
        select(m.UnderwritingAnswer).where(m.UnderwritingAnswer.case_id == case_id).order_by(m.UnderwritingAnswer.answer_version)
    ).all()
    latest: Dict[str, m.UnderwritingAnswer] = {}
    for row in rows:
        latest[row.question_id] = row
    return latest


def answers_as_records(session: Session, case_id: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for qid, row in latest_answers(session, case_id).items():
        out[qid] = {
            "value": row.answer_json.get("value"),
            "confirmed_at": row.confirmed_at.isoformat(),
            "answer_version": row.answer_version,
            "evidence_id": row.evidence_id,
            "provider_id": row.provider_id,
        }
    return out


def quote_tasks_for_case(session: Session, case_id: str, needs_version: Optional[int] = None) -> List[m.QuoteTask]:
    stmt = select(m.QuoteTask).where(m.QuoteTask.case_id == case_id)
    if needs_version is not None:
        stmt = stmt.where(m.QuoteTask.needs_version == needs_version)
    return list(session.scalars(stmt.order_by(m.QuoteTask.created_at)).all())


def task_by_external_id(session: Session, insurer_id: str, external_task_id: str) -> Optional[m.QuoteTask]:
    return session.scalars(
        select(m.QuoteTask).where(m.QuoteTask.insurer_id == insurer_id, m.QuoteTask.external_task_id == external_task_id)
    ).first()


def current_quotes(session: Session, case_id: str, needs_version: int) -> List[m.InsuranceQuote]:
    stmt = (
        select(m.InsuranceQuote)
        .where(
            m.InsuranceQuote.case_id == case_id,
            m.InsuranceQuote.needs_version == needs_version,
            m.InsuranceQuote.superseded_by.is_(None),
        )
        .order_by(m.InsuranceQuote.created_at)
    )
    return list(session.scalars(stmt).all())


def get_quote(session: Session, quote_id: str) -> m.InsuranceQuote:
    quote = session.get(m.InsuranceQuote, quote_id)
    if quote is None:
        raise NotFound("quote %s not found" % quote_id)
    return quote


def next_event_sequence(session: Session, case_id: str) -> int:
    current = session.scalar(select(func.max(m.CaseEvent.sequence)).where(m.CaseEvent.case_id == case_id))
    return (current or 0) + 1


def events_for_case(session: Session, case_id: str) -> List[m.CaseEvent]:
    return list(session.scalars(select(m.CaseEvent).where(m.CaseEvent.case_id == case_id).order_by(m.CaseEvent.sequence)).all())


def actions_for_case(session: Session, case_id: str) -> List[m.Action]:
    return list(session.scalars(select(m.Action).where(m.Action.case_id == case_id).order_by(m.Action.created_at)).all())


def open_actions(session: Session, case_id: str) -> List[m.Action]:
    return list(
        session.scalars(
            select(m.Action).where(m.Action.case_id == case_id, m.Action.status.in_(("proposed", "approved")))
        ).all()
    )


def applications_for_case(session: Session, case_id: str) -> List[m.Application]:
    return list(session.scalars(select(m.Application).where(m.Application.case_id == case_id).order_by(m.Application.revision)).all())


def policies_for_case(session: Session, case_id: str) -> List[m.IssuedPolicy]:
    return list(session.scalars(select(m.IssuedPolicy).where(m.IssuedPolicy.case_id == case_id).order_by(m.IssuedPolicy.created_at)).all())


def messages_for_case(session: Session, case_id: str) -> List[m.ConversationMessage]:
    return list(
        session.scalars(select(m.ConversationMessage).where(m.ConversationMessage.case_id == case_id).order_by(m.ConversationMessage.created_at)).all()
    )


def tool_runs_for_case(session: Session, case_id: str) -> List[m.ToolRun]:
    return list(session.scalars(select(m.ToolRun).where(m.ToolRun.case_id == case_id).order_by(m.ToolRun.started_at)).all())


def provider_requests_for_case(session: Session, case_id: str) -> List[m.ProviderRequestLog]:
    return list(
        session.scalars(select(m.ProviderRequestLog).where(m.ProviderRequestLog.case_id == case_id).order_by(m.ProviderRequestLog.started_at)).all()
    )


def cases_for_customer(session: Session, customer_id: str) -> List[m.Case]:
    return list(session.scalars(select(m.Case).where(m.Case.customer_id == customer_id).order_by(m.Case.created_at)).all())


def all_cases(session: Session) -> List[m.Case]:
    return list(session.scalars(select(m.Case).order_by(m.Case.created_at)).all())
