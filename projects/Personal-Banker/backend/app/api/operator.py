"""Operator view and simulation controls (plan §15).

Shows adapter requests, state transitions, pending actions and redacted
evidence references. Nothing here can approve or execute an action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.adapters.registry import capability_matrix
from app.api.deps import Principal, get_session, require_operator
from app.api.schemas import ClockAdvanceRequest, ModeRequest, RevokeRequest
from app.persistence.models import (
    Action,
    AdapterRequest,
    Case,
    Job,
    MockBankAccount,
    MockBankConfig,
    MockBankDeposit,
    MockBankInstruction,
    OutboxMessage,
    ToolRun,
)
from app.workflows import simulation, worker
from app.workflows.errors import CaseError

router = APIRouter(prefix="/v1/operator", tags=["operator"], dependencies=[Depends(require_operator)])


@router.get("/overview")
def overview(session: Session = Depends(get_session)):
    now = clock.now(session)
    cases = session.scalars(select(Case).order_by(Case.updated_at.desc())).all()
    by_state: dict[str, int] = {}
    for c in cases:
        by_state[c.state] = by_state.get(c.state, 0) + 1
    pending_actions = session.scalars(select(Action).where(Action.status.in_(["proposed", "approved", "submitting", "submitted", "outcome_unknown", "effective"]))).all()
    jobs = session.scalars(select(Job).order_by(Job.run_at)).all()
    requests = session.scalars(select(AdapterRequest).order_by(AdapterRequest.created_at.desc()).limit(50)).all()
    tool_runs = session.scalars(select(ToolRun).order_by(ToolRun.created_at.desc()).limit(50)).all()
    cfg = session.get(MockBankConfig, 1)
    oldest_pending = session.scalar(select(func.min(Job.run_at)).where(Job.status == "pending"))
    submit_requests = session.scalars(select(AdapterRequest).where(AdapterRequest.operation == "submit_instruction")).all()
    duplicates_prevented = sum(1 for r in submit_requests if (r.response_redacted_json or {}).get("duplicate_of_original"))
    return {
        "now": clock.iso(now),
        "environment": "mock",
        "cases_by_state": by_state,
        "cases_waiting_on_customer": sum(1 for c in cases if c.state in ("collecting", "needs_information", "awaiting_approval", "needs_requote")),
        "cases_waiting_on_provider": sum(1 for c in cases if c.state in ("submitted", "outcome_unknown", "verifying")),
        "cases_in_manual_review": by_state.get("manual_review", 0),
        "queue": {
            "pending": sum(1 for j in jobs if j.status == "pending"),
            "leased": sum(1 for j in jobs if j.status == "leased"),
            "failed": sum(1 for j in jobs if j.status == "failed"),
            "oldest_pending_run_at": oldest_pending if isinstance(oldest_pending, str) else (clock.iso(oldest_pending) if oldest_pending else None),
        },
        "duplicate_actions_prevented": duplicates_prevented,
        "tool_errors": sum(1 for t in tool_runs if t.outcome == "error"),
        "mock_bank": {"next_submit_mode": cfg.next_submit_mode if cfg else "normal", "offer_mode": cfg.offer_mode if cfg else "normal"},
        "pending_actions": [
            {"id": a.id, "case_id": a.case_id, "type": a.type, "status": a.status, "payload_hash": a.payload_hash, "provider_reference": a.provider_reference, "request_ref": a.request_ref, "idempotency_key": a.idempotency_key, "last_error": a.last_error, "updated_at": clock.iso(a.updated_at)}
            for a in pending_actions
        ],
        "jobs": [
            {"id": j.id, "type": j.type, "case_id": j.case_id, "status": j.status, "run_at": clock.iso(j.run_at), "attempts": j.attempts, "lease_owner": j.lease_owner, "last_error": j.last_error}
            for j in jobs
        ],
        "adapter_requests": [
            {"id": r.id, "provider_id": r.provider_id, "operation": r.operation, "request_ref": r.request_ref, "case_id": r.case_id, "environment": r.environment, "latency_ms": r.latency_ms, "outcome": r.outcome, "created_at": clock.iso(r.created_at), "request": r.request_redacted_json, "response": r.response_redacted_json}
            for r in requests
        ],
        "tool_runs": [
            {"id": t.id, "case_id": t.case_id, "tool_name": t.tool_name, "outcome": t.outcome, "latency_ms": t.latency_ms, "model_version": t.model_version, "prompt_version": t.prompt_version, "input": t.input_redacted_json, "output_summary": t.output_summary_json, "created_at": clock.iso(t.created_at)}
            for t in tool_runs
        ],
        "cases": [
            {"id": c.id, "customer_id": c.customer_id, "state": c.state, "version": c.version, "deposit_id": c.deposit_id, "review_reason": c.review_reason, "updated_at": clock.iso(c.updated_at)}
            for c in cases
        ],
    }


@router.get("/outbox")
def outbox_messages(session: Session = Depends(get_session)):
    rows = session.scalars(select(OutboxMessage).order_by(OutboxMessage.created_at)).all()
    return [{"id": m.id, "topic": m.topic, "case_id": m.case_id, "payload": m.payload_json, "signature": m.signature, "published_at": clock.iso(m.published_at) if m.published_at else None} for m in rows]


@router.get("/inbox")
def inbox_events():
    return simulation.inbox_events()


@router.post("/inbox/{inbox_id}/replay")
def replay(inbox_id: str):
    try:
        return simulation.replay_inbox_event(inbox_id)
    except KeyError:
        raise CaseError(404, "inbox_event_not_found", inbox_id)


@router.get("/capabilities")
def capabilities():
    return capability_matrix()


@router.get("/mock-bank/ledger")
def ledger(session: Session = Depends(get_session)):
    accounts = session.scalars(select(MockBankAccount).order_by(MockBankAccount.id)).all()
    deposits = session.scalars(select(MockBankDeposit).order_by(MockBankDeposit.opened_on)).all()
    instructions = session.scalars(select(MockBankInstruction).order_by(MockBankInstruction.accepted_at)).all()
    return {
        "accounts": [{"id": a.id, "provider_id": a.provider_id, "kind": a.kind, "currency": a.currency, "available_minor": a.available_minor, "current_minor": a.current_minor, "pending": a.pending_json, "access_revoked": a.access_revoked} for a in accounts],
        "deposits": [{"id": d.id, "account_id": d.account_id, "principal_minor": d.principal_minor, "apy_decimal": d.apy_decimal, "term_days": d.term_days, "opened_on": d.opened_on.isoformat(), "maturity_date": d.maturity_date.isoformat(), "product_version": d.product_version, "status": d.status, "matured_from_id": d.matured_from_id} for d in deposits],
        "instructions": [{"request_ref": i.request_ref, "provider_reference": i.provider_reference, "status": i.status, "decline_reason": i.decline_reason, "effective_on": i.effective_on.isoformat() if i.effective_on else None, "applied": i.applied, "callback_sent": i.callback_sent, "credited_amount_minor": i.credited_amount_minor, "amount_minor": i.payload_json.get("amount_minor"), "instruction_type": i.payload_json.get("instruction_type")} for i in instructions],
    }


@router.post("/clock/advance")
def advance(body: ClockAdvanceRequest):
    return simulation.advance_clock(days=body.days, hours=body.hours, minutes=body.minutes)


@router.post("/worker/run")
async def run_worker():
    results = await worker.drain()
    return {"ran": len(results), "results": results}


@router.post("/mock-bank/submit-mode")
def submit_mode(body: ModeRequest):
    try:
        simulation.set_submit_mode(body.mode)
    except ValueError as exc:
        raise CaseError(422, "invalid_mode", str(exc))
    return {"next_submit_mode": body.mode}


@router.post("/mock-bank/offer-mode")
def offer_mode(body: ModeRequest):
    try:
        payload = simulation.set_offer_mode(body.mode, body.offer_id or "off_harbor_12m")
    except ValueError as exc:
        raise CaseError(422, "invalid_mode", str(exc))
    return {"offer_mode": body.mode, "offer": payload}


@router.post("/mock-bank/revoke-access")
def revoke(body: RevokeRequest):
    try:
        simulation.revoke_access(body.account_id, body.revoked)
    except KeyError:
        raise CaseError(404, "account_not_found", body.account_id)
    return {"account_id": body.account_id, "access_revoked": body.revoked}


@router.post("/reset")
def reset():
    from app.persistence import db
    from app.persistence.seed import seed

    db.drop_schema()
    db.create_schema()
    with db.session_scope() as session:
        seed(session)
    return {"reset": True}
