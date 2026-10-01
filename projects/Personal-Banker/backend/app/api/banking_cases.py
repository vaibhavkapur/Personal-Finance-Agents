from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal, get_session
from app.api.schemas import (
    AnswersRequest,
    CreateCaseRequest,
    CreateCaseResponse,
    PrepareInstructionRequest,
)
from app.persistence.models import Case
from app.workflows import case_service
from app.workflows.errors import CaseError

router = APIRouter(prefix="/v1", tags=["banking-cases"])


def _owner(principal: Principal, customer_id: str | None = None) -> str:
    if principal.role == "operator":
        return customer_id or principal.subject
    if customer_id is not None and customer_id != principal.subject:
        raise CaseError(403, "not_owner", "customer_id does not match the authenticated session")
    return principal.subject


@router.get("/me/inbox")
def inbox(principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    return case_service.maturity_inbox(session, _owner(principal))


@router.get("/banking-cases")
def list_cases(principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    query = select(Case).order_by(Case.created_at.desc())
    if principal.role != "operator":
        query = query.where(Case.customer_id == principal.subject)
    return [case_service.case_summary(session, c) for c in session.scalars(query)]


@router.post("/banking-cases", response_model=CreateCaseResponse, status_code=201)
def create_case(body: CreateCaseRequest, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    owner = _owner(principal, body.customer_id)
    case = case_service.create_case(
        session,
        customer_id=owner,
        deposit_id=body.deposit_id,
        currency=body.currency,
        minimum_buffer_minor=body.minimum_buffer_minor,
        obligation_ids=body.obligation_ids,
        preferred_lockup_days=body.preferred_lockup_days,
        buffer_includes_obligations=body.buffer_includes_obligations,
        concentration_limit_minor=body.concentration_limit_minor,
        provider_mode=body.provider_mode,
        actor=principal.subject,
    )
    return CreateCaseResponse(id=case.id, status=case.state, version=case.version, missing_fields=case.missing_fields_json, outstanding_questions=case.outstanding_questions_json)


@router.get("/banking-cases/{case_id}")
def get_case(case_id: str, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    case = case_service.get_case(session, case_id, None if principal.role == "operator" else principal.subject)
    return case_service.case_detail(session, case)


@router.post("/banking-cases/{case_id}/answers")
def answer(case_id: str, body: AnswersRequest, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    case = case_service.get_case(session, case_id, None if principal.role == "operator" else principal.subject)
    answers = body.model_dump(exclude_none=True)
    if body.obligations is not None:
        answers["obligations"] = [o.model_dump() for o in body.obligations]
    case_service.answer_questions(session, case, answers, actor=principal.subject)
    return case_service.case_summary(session, case)


@router.post("/banking-cases/{case_id}/evaluate")
async def evaluate(case_id: str, principal: Principal = Depends(get_principal)):
    result = await case_service.evaluate_case(case_id, actor=principal.subject, customer_id=None if principal.role == "operator" else principal.subject)
    return result


@router.get("/banking-cases/{case_id}/options")
def options(case_id: str, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    case = case_service.get_case(session, case_id, None if principal.role == "operator" else principal.subject)
    return case_service.get_options(session, case)


@router.post("/banking-cases/{case_id}/instructions", status_code=201)
def prepare(case_id: str, body: PrepareInstructionRequest, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    case = case_service.get_case(session, case_id, None if principal.role == "operator" else principal.subject)
    if principal.role != "customer":
        raise CaseError(403, "customer_only", "only the customer can prepare an instruction for approval")
    return case_service.prepare_instruction(session, case, body.option_id, body.amount_minor, actor=principal.subject)


@router.get("/banking-cases/{case_id}/timeline")
def timeline(case_id: str, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    case = case_service.get_case(session, case_id, None if principal.role == "operator" else principal.subject)
    return case_service.timeline(session, case)


@router.post("/banking-cases/{case_id}/cancel")
def cancel(case_id: str, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    case = case_service.get_case(session, case_id, None if principal.role == "operator" else principal.subject)
    case_service.cancel_case(session, case, actor=principal.subject)
    return case_service.case_summary(session, case)
