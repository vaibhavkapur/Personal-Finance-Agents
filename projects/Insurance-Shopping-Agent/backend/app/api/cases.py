"""Customer-facing case endpoints."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request

from ..agent.orchestrator import AgentOrchestrator
from ..context import AppContext
from ..fixtures import household_by_customer, load_insurers, load_state_profile
from ..persistence import repositories as repo
from ..workflows.approvals import ApplicationService
from ..workflows.case_service import CaseService
from .deps import Principal, current_customer, get_ctx
from .schemas import AnswersRequest, ApplicationRequest, CreateCaseRequest, CreateCaseResponse, MessageRequest, SelectQuoteRequest

router = APIRouter(prefix="/v1", tags=["cases"])


def _services(ctx: AppContext):
    cases = CaseService(ctx)
    return cases, ApplicationService(ctx, cases)


@router.get("/me")
def me(principal: Principal = Depends(current_customer)) -> Dict[str, Any]:
    household = household_by_customer(principal.id)
    return {"customer_id": principal.id, "display_name": household["display_name"], "address": household["address"], "building_type": household["building_type"]}


@router.get("/catalog")
def catalog(ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    profile = load_state_profile()
    return {
        "state_profile": profile,
        "insurers": [{"insurer_id": c["insurer_id"], "display_name": c["display_name"], "label": c["label"], "questions": c["questions"], "policy_form_version": c["policy_form"]["policy_form_version"]} for c in load_insurers()],
        "environment": ctx.environment,
        "adapter_mode": ctx.registry.mode,
        "now": ctx.now().isoformat(),
    }


@router.get("/insurance-shopping-cases")
def list_cases(principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> List[Dict[str, Any]]:
    with ctx.db.session() as session:
        return [{"id": c.id, "status": c.state, "version": c.version, "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat()} for c in repo.cases_for_customer(session, principal.id)]


@router.post("/insurance-shopping-cases", status_code=201, response_model=CreateCaseResponse)
def create_case(body: CreateCaseRequest, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    from ..workflows.case_service import CaseError

    if body.customer_id and body.customer_id != principal.id:
        raise CaseError("customer_id does not match the authenticated session", 403)
    cases, _ = _services(ctx)
    return cases.create_case(principal.id, body.model_dump(mode="json"))


@router.get("/insurance-shopping-cases/{case_id}")
def get_case(case_id: str, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    cases, _ = _services(ctx)
    return cases.case_view(case_id, principal.id)


@router.post("/insurance-shopping-cases/{case_id}/answers")
async def post_answers(case_id: str, body: AnswersRequest, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    cases, _ = _services(ctx)
    return await cases.record_answers(case_id, principal.id, [a.model_dump() for a in body.answers], actor="customer:" + principal.id)


@router.post("/insurance-shopping-cases/{case_id}/quote-requests", status_code=202)
async def request_quotes(case_id: str, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    cases, _ = _services(ctx)
    return await cases.request_quotes(case_id, principal.id, actor="customer:" + principal.id)


@router.get("/insurance-shopping-cases/{case_id}/comparison")
def comparison(case_id: str, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    cases, _ = _services(ctx)
    return cases.comparison(case_id, principal.id)


@router.post("/insurance-shopping-cases/{case_id}/selection")
def select_quote(case_id: str, body: SelectQuoteRequest, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    cases, _ = _services(ctx)
    return cases.select_quote(case_id, principal.id, body.quote_id, actor="customer:" + principal.id)


@router.post("/insurance-shopping-cases/{case_id}/applications", status_code=201)
def create_application(case_id: str, body: ApplicationRequest, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    _, applications = _services(ctx)
    return applications.prepare_application(case_id, principal.id, body.quote_id, body.answers_version, actor="customer:" + principal.id, idempotency_key=body.idempotency_key)


@router.get("/insurance-shopping-cases/{case_id}/messages")
def get_messages(case_id: str, request: Request, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> List[Dict[str, Any]]:
    return AgentOrchestrator(ctx, request.app.state.llm).history(case_id, principal.id)


@router.post("/insurance-shopping-cases/{case_id}/messages")
async def post_message(case_id: str, body: MessageRequest, request: Request, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    return await AgentOrchestrator(ctx, request.app.state.llm).handle_message(case_id, principal.id, body.text)
