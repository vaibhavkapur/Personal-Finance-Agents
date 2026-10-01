"""Exact-action approval endpoints. The approver comes from the authenticated session."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from ..context import AppContext
from ..workflows.approvals import ApplicationService
from .deps import Principal, current_customer, get_ctx
from .schemas import ApproveActionRequest, RejectActionRequest

router = APIRouter(prefix="/v1/actions", tags=["actions"])


@router.post("/{action_id}/approve")
def approve(action_id: str, body: ApproveActionRequest, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    return ApplicationService(ctx).approve_action(action_id, principal.id, body.expected_case_version, body.action_payload_hash, body.approval_challenge_id)


@router.post("/{action_id}/reject")
def reject(action_id: str, body: RejectActionRequest, principal: Principal = Depends(current_customer), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    return ApplicationService(ctx).reject_action(action_id, principal.id, body.reason)
