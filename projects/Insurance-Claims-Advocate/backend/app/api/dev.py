"""Operator/dev controls: fixture clock, worker ticks, simulator faults and the mock payment feed.
Disabled with DEV_ENDPOINTS=0. Nothing here can bypass action authorization."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..clock import parse_iso
from ..context import AppContext
from ..persistence.models import ClaimCase, ClaimDecision, InboxEvent, Job, OutboxMessage
from ..workflows.approvals import PrincipalView
from .deps import get_ctx, require_operator

router = APIRouter(prefix="/v1/dev", tags=["dev"])


class ClockRequest(BaseModel):
    freeze_at: Optional[str] = None
    advance_hours: Optional[float] = None
    advance_days: Optional[float] = None
    unfreeze: bool = False


class FaultRequest(BaseModel):
    fault: Optional[str] = None  # timeout_after_accept | timeout_before_accept | malformed | declined
    duplicate_decision: Optional[bool] = None
    out_of_order: Optional[bool] = None
    callback_delay_hours: Optional[float] = None


class PaymentRequest(BaseModel):
    case_id: str
    mode: str = "exact"  # exact | smaller | remainder | delayed | provisional | unrelated | wrong_payee | split


@router.get("/clock")
def get_clock(ctx: AppContext = Depends(get_ctx)):
    return {"now": ctx.clock.now_iso(), "frozen": ctx.clock.is_frozen}


@router.post("/clock")
def set_clock(body: ClockRequest, principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    if body.unfreeze:
        ctx.clock.unfreeze()
    if body.freeze_at:
        ctx.clock.freeze(parse_iso(body.freeze_at))
    if body.advance_hours:
        ctx.clock.advance(hours=body.advance_hours)
    if body.advance_days:
        ctx.clock.advance(days=body.advance_days)
    return {"now": ctx.clock.now_iso(), "frozen": ctx.clock.is_frozen}


@router.post("/worker/run-once")
async def worker_run_once(principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    return await ctx.worker.run_once()


@router.post("/worker/run-until-idle")
async def worker_run_until_idle(principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    rounds = await ctx.worker.run_until_idle()
    return {"rounds": len(rounds), "stats": rounds}


@router.post("/mock-insurer/faults")
def mock_faults(body: FaultRequest, principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    if body.fault:
        ctx.mock_insurer.inject_fault(body.fault)
    flags = {}
    if body.duplicate_decision is not None:
        flags["duplicate_decision"] = body.duplicate_decision
    if body.out_of_order is not None:
        flags["out_of_order"] = body.out_of_order
    if flags:
        ctx.mock_insurer.set_chaos(**flags)
    if body.callback_delay_hours is not None:
        if body.callback_delay_hours > 0:
            ctx.mock_insurer.set_callback_delay(hours=body.callback_delay_hours)
        else:
            ctx.mock_insurer.set_callback_delay()
    return {"pending_faults": list(ctx.mock_insurer.faults), "chaos": ctx.mock_insurer.chaos, "callback_delay": str(ctx.mock_insurer.callback_delay)}


@router.get("/mock-insurer/claims")
def mock_claims(principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    return {"items": ctx.mock_insurer.snapshot()}


@router.post("/mock-payments/emit")
def emit_payment(body: PaymentRequest, principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        case = s.get(ClaimCase, body.case_id)
        if not case:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "case not found"})
        decision = s.scalars(select(ClaimDecision).where(ClaimDecision.case_id == case.id).order_by(ClaimDecision.decision_version.desc())).first()
        if not decision or not case.external_claim_ref:
            raise HTTPException(status_code=409, detail={"code": "no_decision", "message": "case has no insurer decision to pay"})
        events = ctx.payment_feed.generate(claim_reference=case.external_claim_ref, payee_id=case.customer_id, approved_minor=decision.accepted_minor, currency=decision.currency, mode=body.mode)
    results = [ctx.events.handle_payment_event(ev["payload"], ev["signature"]) for ev in events]
    return {"emitted": len(events), "results": results}


@router.get("/jobs")
def jobs(principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        rows = s.scalars(select(Job).order_by(Job.created_at.desc())).all()
        return {"items": [{"id": j.id, "type": j.job_type, "status": j.status, "run_at": j.run_at, "attempts": j.attempts, "lease_until": j.lease_until, "locked_by": j.locked_by, "last_error": (j.last_error or "")[:200], "payload": j.payload_json} for j in rows]}


@router.get("/inbox")
def inbox(principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        rows = s.scalars(select(InboxEvent).order_by(InboxEvent.received_at.desc())).all()
        return {"items": [{"id": r.id, "provider": r.provider, "provider_event_id": r.provider_event_id, "type": r.event_type, "outcome": r.outcome, "received_at": r.received_at, "sequence": r.payload_json.get("sequence"), "claim_reference": r.payload_json.get("claim_reference")} for r in rows]}


@router.get("/outbox")
def outbox(principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        rows = s.scalars(select(OutboxMessage).order_by(OutboxMessage.created_at.desc())).all()
        return {"items": [{"id": r.id, "case_id": r.case_id, "type": r.event_type, "status": r.status, "attempts": r.attempts, "created_at": r.created_at, "delivered_at": r.delivered_at} for r in rows]}


@router.post("/replay/inbox/{inbox_id}")
def replay_inbox(inbox_id: str, principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    """Operator replay of a provider event. Goes through the normal processor; cannot approve or submit anything."""
    with ctx.db.session() as s:
        row = s.get(InboxEvent, inbox_id)
        if not row:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "inbox event not found"})
        payload = row.payload_json
        provider = row.provider
        s.delete(row)
    from ..adapters.mock_insurer import sign_payload

    signature = sign_payload(ctx.settings.provider_webhook_secret, payload)
    if provider == "mock_payments":
        return ctx.events.handle_payment_event(payload, signature)
    return ctx.events.handle_claim_event(payload, signature)


@router.get("/metrics")
def metrics(principal: PrincipalView = Depends(require_operator), ctx: AppContext = Depends(get_ctx)):
    from ..persistence.models import Action, ToolRun
    from ..workflows.state_machine import WAITING_ON_CUSTOMER, WAITING_ON_PROVIDER

    with ctx.db.session() as s:
        cases = s.scalars(select(ClaimCase)).all()
        jobs_rows = s.scalars(select(Job).where(Job.status == "pending")).all()
        tool_runs = s.scalars(select(ToolRun)).all()
        actions = s.scalars(select(Action)).all()
        now = ctx.clock.now()
        queue_age = max([(now - parse_iso(j.run_at)).total_seconds() for j in jobs_rows] + [0])
        return {
            "cases_total": len(cases),
            "cases_waiting_on_customer": sum(1 for c in cases if c.status in WAITING_ON_CUSTOMER),
            "cases_waiting_on_provider": sum(1 for c in cases if c.status in WAITING_ON_PROVIDER),
            "cases_manual_review": sum(1 for c in cases if c.status == "manual_review"),
            "queue_age_seconds": queue_age,
            "tool_runs": len(tool_runs),
            "tool_errors": sum(1 for t in tool_runs if t.outcome != "ok"),
            "approvals_abandoned": sum(1 for a in actions if a.status == "invalidated"),
            "duplicate_events_suppressed": sum(int(r.duplicate_deliveries or 0) for r in s.scalars(select(InboxEvent)).all()),
            "duplicate_actions_prevented": sum(1 for a in actions if (a.result_json or {}).get("idempotent_replay") or (a.result_json or {}).get("found")),
        }
