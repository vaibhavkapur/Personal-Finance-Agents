"""Exact-action approvals.

An external write (lender message, application submission, document release)
is first persisted as a *proposed* ``Action`` with an immutable payload hash and
a review screen. The authenticated borrower approves that exact hash together
with the case version and a short-lived challenge. The executor re-verifies the
approval immediately before the side effect and consumes it once.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.db import new_id
from ..persistence.models import Action, Approval, ApprovalChallenge, Case
from .states import StaleVersionError, WorkflowError, record_event


class ApprovalError(WorkflowError):
    status_code = 409


class ForbiddenError(WorkflowError):
    status_code = 403


def canonical_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def propose_action(
    session: Session,
    case: Case,
    action_type: str,
    payload: Dict[str, Any],
    review: Dict[str, Any],
    idempotency_key: str,
    now,
    ttl_seconds: int,
    actor: str = "system",
) -> Action:
    """Persist a proposed action plus its approval challenge (idempotent per case + key)."""
    payload_hash = canonical_hash(payload)
    existing = session.execute(
        select(Action).where(Action.case_id == case.id, Action.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ApprovalError("idempotency key reused with different content")
        return existing

    action = Action(
        id=new_id("act"),
        case_id=case.id,
        type=action_type,
        payload_json=payload,
        payload_hash=payload_hash,
        status="proposed",
        idempotency_key=idempotency_key,
        client_request_ref=new_id("req"),
        review_json=review,
        bound_case_version=case.version,
        created_at=now,
        updated_at=now,
    )
    session.add(action)
    session.flush()
    challenge = ApprovalChallenge(
        id=new_id("challenge"),
        action_id=action.id,
        expires_at=now + timedelta(seconds=ttl_seconds),
        created_at=now,
    )
    session.add(challenge)
    session.flush()
    record_event(session, case, "action.proposed", actor, now, {"action_id": action.id, "type": action_type, "payload_hash": payload_hash})
    return action


def current_challenge(session: Session, action: Action) -> Optional[ApprovalChallenge]:
    return session.execute(
        select(ApprovalChallenge)
        .where(ApprovalChallenge.action_id == action.id, ApprovalChallenge.consumed_at.is_(None))
        .order_by(ApprovalChallenge.created_at.desc())
    ).scalars().first()


def review_screen(session: Session, action: Action, case: Case) -> Dict[str, Any]:
    challenge = current_challenge(session, action)
    return {
        "action_id": action.id,
        "action_type": action.type,
        "status": action.status,
        "action_payload_hash": action.payload_hash,
        "expected_case_version": action.bound_case_version,
        "approval_challenge_id": challenge.id if challenge else None,
        "challenge_expires_at": challenge.expires_at.isoformat() if challenge else None,
        "review": action.review_json,
        "payload": action.payload_json,
    }


def approve_action(
    session: Session,
    case: Case,
    action: Action,
    approver_customer_id: str,
    expected_case_version: int,
    action_payload_hash: str,
    approval_challenge_id: str,
    now,
    ttl_seconds: int,
) -> Approval:
    if case.customer_id != approver_customer_id:
        raise ForbiddenError("approver does not own this case")
    if action.case_id != case.id:
        raise ForbiddenError("action does not belong to this case")
    if action.status != "proposed":
        raise ApprovalError(f"action is {action.status}, not awaiting approval")
    if expected_case_version != case.version:
        raise StaleVersionError(f"case version is {case.version}, expected {expected_case_version}")
    if action.bound_case_version != case.version:
        raise StaleVersionError("material inputs changed since the action was proposed; request a new review")
    if action_payload_hash != action.payload_hash:
        raise ApprovalError("payload hash does not match the proposed action")
    challenge = session.get(ApprovalChallenge, approval_challenge_id)
    if challenge is None or challenge.action_id != action.id:
        raise ApprovalError("unknown approval challenge")
    if challenge.consumed_at is not None:
        raise ApprovalError("approval challenge already used")
    if now >= challenge.expires_at:
        raise ApprovalError("approval challenge expired")

    challenge.consumed_at = now
    approval = Approval(
        id=new_id("apr"),
        action_id=action.id,
        approver_customer_id=approver_customer_id,
        action_hash=action.payload_hash,
        scope_json={
            "case_id": case.id,
            "action_type": action.type,
            "lender_id": action.payload_json.get("lender_id"),
            "offer_id": action.payload_json.get("offer_id"),
            "offer_version": action.payload_json.get("offer_version"),
            "disclosed_documents": action.payload_json.get("disclosed_documents", []),
            "case_version": case.version,
        },
        expires_at=now + timedelta(seconds=ttl_seconds),
        created_at=now,
    )
    session.add(approval)
    action.status = "approved"
    action.approval_id = approval.id
    action.updated_at = now
    session.add(action)
    session.flush()
    record_event(
        session,
        case,
        "action.approved",
        f"customer:{approver_customer_id}",
        now,
        {"action_id": action.id, "approval_id": approval.id, "payload_hash": action.payload_hash},
        expected_case_version=expected_case_version,
    )
    return approval


def verify_authority(session: Session, action: Action, case: Case, now) -> Approval:
    """Return the valid approval for ``action`` or raise. Called right before any side effect."""
    if action.status not in ("approved", "executing"):
        raise ApprovalError(f"action {action.id} is {action.status}; execution requires an approved action")
    approval = session.get(Approval, action.approval_id) if action.approval_id else None
    if approval is None:
        raise ApprovalError("no approval bound to action")
    if approval.action_hash != action.payload_hash:
        raise ApprovalError("approval hash does not match action payload")
    if approval.revoked_at is not None:
        raise ApprovalError(f"approval revoked: {approval.revocation_reason}")
    if approval.consumed_at is not None and action.status != "executing":
        raise ApprovalError("approval already consumed")
    if now >= approval.expires_at and action.status != "executing":
        raise ApprovalError("approval expired")
    if approval.scope_json.get("case_version") != action.bound_case_version:
        raise ApprovalError("approval scope does not match bound case version")
    return approval


def consume_approval(session: Session, approval: Approval, job_id: str, now) -> None:
    approval.consumed_at = now
    approval.consumed_by_job_id = job_id
    session.add(approval)


def invalidate_pending_authority(session: Session, case: Case, reason: str, now, actor: str = "system") -> int:
    """Revoke unconsumed approvals and cancel proposed actions after material inputs change."""
    count = 0
    actions = session.execute(
        select(Action).where(Action.case_id == case.id, Action.status.in_(["proposed", "approved"]))
    ).scalars().all()
    for action in actions:
        if action.approval_id:
            approval = session.get(Approval, action.approval_id)
            if approval and approval.consumed_at is None and approval.revoked_at is None:
                approval.revoked_at = now
                approval.revocation_reason = reason
                session.add(approval)
        action.status = "invalidated"
        action.result_json = {"invalidated_reason": reason}
        action.updated_at = now
        session.add(action)
        count += 1
        record_event(session, case, "action.invalidated", actor, now, {"action_id": action.id, "reason": reason})
    return count
