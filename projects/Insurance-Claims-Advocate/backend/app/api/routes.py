"""Customer/operator-facing application API. These endpoints belong to this application only."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..context import AppContext
from ..workflows.approvals import PrincipalView
from .deps import get_ctx, get_principal, require_operator

router = APIRouter(prefix="/v1", tags=["claims"])


class CreateClaimRequest(BaseModel):
    customer_id: str
    policy_id: str
    loss_type: str = "baggage_delay"
    loss_at: str
    document_ids: List[str] = Field(default_factory=list)
    mock_scenario: Optional[str] = Field(default=None, description="mock adapter only: approved | evidence_requested | partial | denied")


class DocumentsRequest(BaseModel):
    document_ids: List[str]


class AnswerRequest(BaseModel):
    answer: Dict[str, Any]


class DraftRequest(BaseModel):
    idempotency_key: Optional[str] = None


class ApproveRequest(BaseModel):
    expected_case_version: int
    action_payload_hash: str
    approval_challenge_id: str


class AgentTurnRequest(BaseModel):
    message: str = ""
    planner: Optional[str] = None


@router.get("/me")
def me(principal: PrincipalView = Depends(get_principal)):
    return {"principal_id": principal.id, "role": principal.role, "customer_id": principal.customer_id, "display_name": principal.display_name}


@router.get("/claims")
def list_claims(principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return {"items": ctx.cases.list_cases(principal)}


@router.post("/claims", status_code=201)
def create_claim(body: CreateClaimRequest, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.create_case(principal, customer_id=body.customer_id, policy_id=body.policy_id, loss_type=body.loss_type, loss_at=body.loss_at, document_ids=body.document_ids, mock_scenario=body.mock_scenario)


@router.get("/claims/{case_id}")
def get_claim(case_id: str, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.get_case(principal, case_id)


@router.post("/claims/{case_id}/documents")
def add_documents(case_id: str, body: DocumentsRequest, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.attach_documents(principal, case_id, body.document_ids)


@router.post("/claims/{case_id}/evaluate")
def evaluate(case_id: str, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.evaluate(principal, case_id)


@router.post("/claims/{case_id}/questions/{question_id}/answer")
def answer(case_id: str, question_id: str, body: AnswerRequest, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.answer_question(principal, case_id, question_id, body.answer)


@router.get("/claims/{case_id}/packet-preview")
def packet_preview(case_id: str, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.preview_packet(principal, case_id)


@router.post("/claims/{case_id}/submission-drafts", status_code=201)
def submission_draft(case_id: str, body: Optional[DraftRequest] = None, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.create_submission_draft(principal, case_id, (body.idempotency_key if body else None))


@router.post("/claims/{case_id}/appeal-drafts", status_code=201)
def appeal_draft(case_id: str, body: Optional[DraftRequest] = None, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.create_appeal_draft(principal, case_id, (body.idempotency_key if body else None))


@router.post("/claims/{case_id}/decision/accept")
def accept_decision(case_id: str, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.accept_decision(principal, case_id)


@router.post("/claims/{case_id}/reconcile")
def reconcile(case_id: str, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.reconcile(principal, case_id)


@router.get("/claims/{case_id}/timeline")
def timeline(case_id: str, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.timeline(principal, case_id)


@router.get("/claims/{case_id}/export")
def export(case_id: str, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.export_case(principal, case_id)


@router.get("/claims/{case_id}/operator")
def operator_view(case_id: str, principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.operator_view(principal, case_id)


@router.get("/actions/{action_id}")
def get_action(action_id: str, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.get_action(principal, action_id)


@router.post("/actions/{action_id}/approve")
def approve(action_id: str, body: ApproveRequest, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.approve_action(principal, action_id, expected_case_version=body.expected_case_version, action_payload_hash=body.action_payload_hash, approval_challenge_id=body.approval_challenge_id)


@router.post("/claims/{case_id}/agent/turns")
def agent_turn(case_id: str, body: AgentTurnRequest, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.agent.run_turn(principal, case_id, body.message, body.planner)


@router.get("/policies/{policy_id}")
def get_policy(policy_id: str, loss_at: Optional[str] = None, principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.cases.get_policy(principal, policy_id, loss_at)


@router.get("/documents")
def list_documents(principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    from sqlalchemy import select

    from ..persistence.models import Document

    with ctx.db.session() as s:
        q = select(Document).order_by(Document.captured_at)
        if principal.role != "operator":
            q = q.where(Document.owner_customer_id == principal.customer_id)
        return {"items": [{"id": d.id, "doc_type": d.doc_type, "content_hash": d.content_hash, "captured_at": d.captured_at, "owner_customer_id": d.owner_customer_id} for d in s.scalars(q).all()]}


@router.get("/provider/capabilities")
def capabilities(principal: PrincipalView = Depends(get_principal), ctx: AppContext = Depends(get_ctx)):
    return ctx.adapter.capabilities.to_dict()
