from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import clock
from app.api.deps import Principal, get_principal, get_session
from app.api.schemas import ApproveRequest, ApproveResponse, ChallengeResponse
from app.workflows import case_service
from app.workflows.errors import CaseError

router = APIRouter(prefix="/v1/actions", tags=["actions"])


@router.get("/{action_id}")
def get_action(action_id: str, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    action, case = case_service.get_action(session, action_id, None if principal.role == "operator" else principal.subject)
    return case_service.review_screen(session, case, action)


@router.post("/{action_id}/challenge", response_model=ChallengeResponse)
def challenge(action_id: str, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    if principal.role != "customer":
        raise CaseError(403, "customer_only", "only the customer can request an approval challenge")
    action, case = case_service.get_action(session, action_id, principal.subject)
    ch = case_service.create_challenge(session, case, action, principal.subject)
    return ChallengeResponse(
        approval_challenge_id=ch.id,
        action_id=action.id,
        action_payload_hash=action.payload_hash,
        expected_case_version=case.version,
        expires_at=clock.iso(ch.expires_at),
    )


@router.post("/{action_id}/approve", response_model=ApproveResponse)
def approve(action_id: str, body: ApproveRequest, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)):
    if principal.role != "customer":
        raise CaseError(403, "customer_only", "the approver comes from the authenticated customer session")
    action, case = case_service.get_action(session, action_id, principal.subject)
    approval = case_service.approve_action(
        session,
        case,
        action,
        approver_id=principal.subject,
        expected_case_version=body.expected_case_version,
        action_payload_hash=body.action_payload_hash,
        approval_challenge_id=body.approval_challenge_id,
    )
    return ApproveResponse(
        approval_id=approval.id,
        action_id=action.id,
        case_id=case.id,
        case_status=case.state,
        case_version=case.version,
        expires_at=clock.iso(approval.expires_at),
    )
