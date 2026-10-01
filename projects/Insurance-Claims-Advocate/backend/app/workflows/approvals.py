"""Exact-action approvals. An approval binds an authenticated approver to one action payload hash, expires, and is
consumed once by the executor. Any material change to the case invalidates open challenges and unconsumed approvals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock, iso, parse_iso
from ..ids import new_id
from ..persistence.models import Action, Approval, ApprovalChallenge, ClaimCase
from .errors import Conflict, Forbidden, Invalid, NotFound


@dataclass
class PrincipalView:
    id: str
    role: str
    customer_id: Optional[str]
    display_name: str

    @property
    def can_approve(self) -> bool:
        return self.role in ("customer", "representative")


def open_challenge(session: Session, clock: Clock, action: Action, ttl_seconds: int) -> ApprovalChallenge:
    challenge = ApprovalChallenge(
        id=new_id("challenge"),
        action_id=action.id,
        expires_at=iso(clock.now() + timedelta(seconds=ttl_seconds)),
        status="open",
        created_at=clock.now_iso(),
    )
    session.add(challenge)
    session.flush()
    return challenge


def invalidate_open_actions(session: Session, clock: Clock, case: ClaimCase, reason: str) -> List[str]:
    """Called whenever material inputs change. Returns the invalidated action ids."""
    invalidated: List[str] = []
    actions = session.scalars(select(Action).where(Action.case_id == case.id, Action.status.in_(["proposed", "approved"]))).all()
    for action in actions:
        action.status = "invalidated"
        action.updated_at = clock.now_iso()
        action.result_json = {"invalidated_reason": reason}
        for ch in session.scalars(select(ApprovalChallenge).where(ApprovalChallenge.action_id == action.id, ApprovalChallenge.status == "open")).all():
            ch.status = "invalidated"
        for ap in session.scalars(select(Approval).where(Approval.action_id == action.id, Approval.consumed_at.is_(None), Approval.revoked_at.is_(None))).all():
            ap.revoked_at = clock.now_iso()
        invalidated.append(action.id)
    return invalidated


def approve(
    session: Session,
    clock: Clock,
    principal: PrincipalView,
    action: Action,
    case: ClaimCase,
    *,
    expected_case_version: int,
    action_payload_hash: str,
    approval_challenge_id: str,
    ttl_seconds: int,
) -> Approval:
    if not principal.can_approve:
        raise Forbidden("only the verified claimant or an authorized representative can approve a claim action")
    if principal.customer_id != case.customer_id:
        raise Forbidden("approver is not the claimant for this case")
    if action.status != "proposed":
        raise Conflict(f"action is {action.status}; only proposed actions can be approved", "action_not_approvable")
    if expected_case_version != case.version:
        raise Conflict(f"case version is {case.version}, approval expected {expected_case_version}", "stale_case_version")
    if action_payload_hash != action.payload_hash:
        raise Invalid("payload hash does not match the proposed action; review the current packet", "payload_changed")
    challenge = session.get(ApprovalChallenge, approval_challenge_id)
    if not challenge or challenge.action_id != action.id:
        raise NotFound("approval challenge not found for this action")
    if challenge.status != "open":
        raise Conflict(f"approval challenge is {challenge.status}", "challenge_unusable")
    if parse_iso(challenge.expires_at) < clock.now():
        challenge.status = "expired"
        raise Conflict("approval challenge expired; request a new review", "challenge_expired")
    challenge.status = "used"
    approval = Approval(
        id=new_id("apr"),
        action_id=action.id,
        approver_id=principal.id,
        action_hash=action.payload_hash,
        scope=action.action_type,
        expires_at=iso(clock.now() + timedelta(seconds=ttl_seconds)),
        created_at=clock.now_iso(),
    )
    session.add(approval)
    action.status = "approved"
    action.approval_id = approval.id
    action.updated_at = clock.now_iso()
    session.flush()
    return approval


def verify_for_execution(session: Session, clock: Clock, action: Action, case: ClaimCase) -> Approval:
    """Re-verify authority immediately before the side effect."""
    if not action.approval_id:
        raise Forbidden("action has no approval")
    approval = session.get(Approval, action.approval_id)
    if not approval:
        raise Forbidden("approval record missing")
    if approval.revoked_at:
        raise Forbidden("approval was revoked")
    if approval.consumed_at and approval.consumed_by != action.id:
        raise Forbidden("approval already consumed by another action")
    if approval.action_hash != action.payload_hash:
        raise Forbidden("approval hash does not match the action payload")
    if parse_iso(approval.expires_at) < clock.now() and not approval.consumed_at:
        raise Forbidden("approval expired before execution")
    if case.customer_id is None:
        raise Forbidden("case has no claimant")
    return approval
