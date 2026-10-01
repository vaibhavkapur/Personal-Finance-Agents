"""HTTP routes. Endpoints are owned by this application; they are not claimed
endpoints of any bank, merchant, protocol or government service."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..adapters.commerce_events import UnsupportedCommerceEvent, ingest_commerce_event
from ..agent.orchestrator import Orchestrator
from ..clock import FixtureClock
from ..container import Container
from ..domain.errors import Forbidden
from ..domain.models import ActionStatus
from ..domain.state_machine import ALLOWED
from . import schemas
from .auth import Session, require_customer, require_operator

router = APIRouter()


def _c(request: Request) -> Container:
    return request.app.state.container


# ---------------------------------------------------------------- customer
@router.get("/v1/health")
def health(request: Request) -> Dict[str, Any]:
    c = _c(request)
    return {"ok": True, "environment": c.settings.environment, "now": c.service._now(), "fixture_clock": isinstance(c.clock, FixtureClock)}


@router.post("/v1/recovery-cases", response_model=schemas.CreateCaseResponse, status_code=201)
def create_case(body: schemas.CreateCaseRequest, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    c = _c(request)
    if body.customer_id != session.customer_id:
        raise Forbidden("customer_id does not match the authenticated session")
    case = c.service.create_case(customer_id=session.customer_id, order_ref=body.order_ref, reason_code=body.reason_code, target_minor=body.target_minor,
                                 currency=body.currency, evidence_ids=body.evidence_ids, actor=session.subject)
    return {"id": case.id, "status": case.status.value, "version": case.version, "next_step": c.service.next_step(case)}


@router.get("/v1/recovery-cases")
def list_cases(request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    c = _c(request)
    items = []
    for case in c.repos.cases_for_customer(session.customer_id):
        st = c.service.get_status(case.id, customer_id=session.customer_id)
        items.append({k: st[k] for k in ("case_id", "status", "version", "order_ref", "merchant_id", "amounts", "next_step", "pending_question", "deadline")})
    return {"items": items}


@router.get("/v1/recovery-cases/{case_id}")
def get_case(case_id: str, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    return _c(request).service.get_status(case_id, customer_id=session.customer_id)


@router.post("/v1/recovery-cases/{case_id}/reconcile")
def reconcile(case_id: str, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    c = _c(request)
    c.service._owned_case(case_id, session.customer_id)
    return c.service.reconcile(case_id, actor=session.subject)


@router.post("/v1/recovery-cases/{case_id}/merchant-message-drafts", status_code=201)
def merchant_message_draft(case_id: str, request: Request, session: Session = Depends(require_customer), idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")) -> Dict[str, Any]:
    return _c(request).service.draft_merchant_message(case_id, actor=session.subject, customer_id=session.customer_id, idempotency_key=idempotency_key)


@router.post("/v1/recovery-cases/{case_id}/dispute-drafts", status_code=201)
def dispute_draft(case_id: str, request: Request, session: Session = Depends(require_customer), idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")) -> Dict[str, Any]:
    return _c(request).service.draft_issuer_dispute(case_id, actor=session.subject, customer_id=session.customer_id, idempotency_key=idempotency_key)


@router.post("/v1/recovery-cases/{case_id}/answers")
def answer(case_id: str, body: schemas.AnswerRequest, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    return _c(request).service.answer_question(case_id, body.answer, actor=session.subject, customer_id=session.customer_id)


@router.post("/v1/recovery-cases/{case_id}/agent-turns")
def agent_turn(case_id: str, body: schemas.AgentTurnRequest, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    c = _c(request)
    orchestrator: Orchestrator = request.app.state.orchestrator
    return orchestrator.run(case_id=case_id, customer_id=session.customer_id, user_message=body.message).as_dict()


@router.post("/v1/recovery-cases/{case_id}/close-unresolved")
def close_unresolved(case_id: str, body: schemas.CloseUnresolvedRequest, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    c = _c(request)
    case = c.service.record_unresolved(case_id, actor=session.subject, reason=body.reason, customer_id=session.customer_id)
    return {"id": case.id, "status": case.status.value, "version": case.version, "outcome_note": case.outcome_note}


@router.get("/v1/recovery-cases/{case_id}/timeline")
def timeline(case_id: str, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    return _c(request).service.get_timeline(case_id, customer_id=session.customer_id)


@router.get("/v1/recovery-cases/{case_id}/actions/{action_id}")
def action_review(case_id: str, action_id: str, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    c = _c(request)
    c.service._owned_case(case_id, session.customer_id)
    action = c.repos.get_action(action_id)
    if action.case_id != case_id:
        raise Forbidden("action belongs to a different case")
    return c.service._draft_view(action)


@router.post("/v1/actions/{action_id}/approve")
def approve(action_id: str, body: schemas.ApproveActionRequest, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    return _c(request).service.approve_action(action_id, approver_id=session.subject, customer_id=session.customer_id, expected_case_version=body.expected_case_version,
                                              action_payload_hash=body.action_payload_hash, approval_challenge_id=body.approval_challenge_id)


@router.post("/v1/commerce-events", status_code=201)
def commerce_event(body: schemas.CommerceEventRequest, request: Request, session: Session = Depends(require_customer)) -> Dict[str, Any]:
    c = _c(request)
    try:
        doc = ingest_commerce_event(session.customer_id, body.payload, c.clock)
    except UnsupportedCommerceEvent as exc:
        raise HTTPException(status_code=422, detail={"code": "unsupported_commerce_event", "message": str(exc)})
    with c.db.transaction():
        if c.repos.db.fetch_one("SELECT id FROM documents WHERE content_hash = ?", (doc.content_hash,)) is None:
            c.repos.add_document(doc)
    return {"document_id": doc.id, "kind": doc.kind, "order_ref": doc.extracted.get("order_ref"), "refund_refs": doc.extracted.get("refund_refs", []), "authority": "simulated", "grants_execution_authority": False}


# ---------------------------------------------------------------- providers
@router.post("/v1/provider-events/recovery")
def provider_event(body: schemas.ProviderEventRequest, request: Request, x_provider_signature: Optional[str] = Header(default=None, alias="X-Provider-Signature")) -> Dict[str, Any]:
    c = _c(request)
    payload = body.model_dump()
    return c.events.handle(payload, x_provider_signature)


# ---------------------------------------------------------------- operator
@router.get("/v1/ops/overview")
def ops_overview(request: Request, session: Session = Depends(require_operator)) -> Dict[str, Any]:
    c = _c(request)
    cases = c.repos.all_cases()
    pending_actions = [{k: v for k, v in a.model_dump().items() if k != "payload"} for a in c.repos.actions_with_status([ActionStatus.awaiting_approval.value, ActionStatus.approved.value, ActionStatus.submitting.value, ActionStatus.unknown.value])]
    return {
        "now": c.service._now(), "environment": c.settings.environment,
        "cases": [{"id": x.id, "status": x.status.value, "version": x.version, "target_minor": x.target_minor, "final_recovered_minor": x.final_recovered_minor, "provisional_minor": x.provisional_minor, "store_credit_minor": x.store_credit_minor, "currency": x.currency, "customer_ref": x.customer_id[:8] + "…"} for x in cases],
        "pending_actions": pending_actions,
        "jobs": [j.model_dump() for j in c.jobs.all()],
        "provider_requests": c.repos.provider_requests(),
        "recent_transitions": [e.model_dump() for e in c.repos.recent_events(50) if e.event_type == "case.transitioned"],
        "inbox": c.inbox.all(),
        "outbox_pending": c.outbox.pending(),
        "capabilities": {name: a.capabilities().as_dict() for name, a in c.adapters.items()},
        "simulator_pending": {"merchant_mock": c.merchant_mock.pending(), "issuer_mock": c.issuer_mock.pending(), "statement_mock": c.statement.pending()},
        "metrics": ops_metrics(request, session),
        "allowed_transitions": {k.value: sorted(v.value for v in vs) for k, vs in ALLOWED.items()},
    }


@router.get("/v1/ops/metrics")
def ops_metrics(request: Request, session: Session = Depends(require_operator)) -> Dict[str, Any]:
    c = _c(request)
    runs = c.repos.tool_runs()
    cases = c.repos.all_cases()
    waiting_customer = sum(1 for x in cases if x.pending_question is not None)
    waiting_provider = sum(1 for x in cases if x.status.value in ("merchant_pending", "refund_promised", "credit_pending", "issuer_pending", "provisional_credit", "credit_reversed"))
    actions = c.repos.actions_with_status([s.value for s in ActionStatus])
    return {
        "tool_runs": len(runs), "tool_errors": sum(1 for r in runs if r["outcome"] != "ok"),
        "cases_total": len(cases), "cases_waiting_on_customer": waiting_customer, "cases_waiting_on_provider": waiting_provider,
        "cases_manual_review": sum(1 for x in cases if x.status.value == "manual_review"),
        "approvals_abandoned": sum(1 for a in actions if a.status in (ActionStatus.expired, ActionStatus.superseded)),
        "duplicate_events_ignored": sum(1 for e in c.inbox.all() if (e.get("outcome") or "").startswith("duplicate")),
        "queue_age_seconds": _queue_age(c),
        "model_cost_proxy_tool_calls_per_case": (len(runs) / len(cases)) if cases else 0,
    }


def _queue_age(c: Container) -> float:
    from ..clock import parse_ts
    due = c.jobs.due()
    if not due:
        return 0.0
    return max((c.clock.now() - parse_ts(j.run_at)).total_seconds() for j in due)


@router.get("/v1/ops/tool-runs")
def ops_tool_runs(request: Request, session: Session = Depends(require_operator), case_id: Optional[str] = None) -> Dict[str, Any]:
    return {"items": _c(request).repos.tool_runs(case_id)}


@router.get("/v1/ops/cases/{case_id}/timeline")
def ops_timeline(case_id: str, request: Request, session: Session = Depends(require_operator)) -> Dict[str, Any]:
    return _c(request).service.get_timeline(case_id)


@router.post("/v1/ops/worker/run")
def ops_run_worker(request: Request, session: Session = Depends(require_operator)) -> Dict[str, Any]:
    c = _c(request)
    delivered = c.pump.deliver_due()
    handled = c.worker.run_until_idle_sync()
    return {"callbacks_delivered": len(delivered), "jobs_handled": handled, "now": c.service._now()}


@router.post("/v1/ops/clock/advance")
def ops_advance_clock(body: schemas.ClockAdvanceRequest, request: Request, session: Session = Depends(require_operator)) -> Dict[str, Any]:
    c = _c(request)
    if not isinstance(c.clock, FixtureClock):
        raise HTTPException(status_code=409, detail={"code": "not_fixture_clock", "message": "clock control is only available with the fixture clock"})
    return c.advance(days=body.days, hours=body.hours, minutes=body.minutes)


@router.post("/v1/ops/events/replay")
def ops_replay(body: schemas.ReplayRequest, request: Request, session: Session = Depends(require_operator)) -> Dict[str, Any]:
    return _c(request).events.replay(body.provider, body.event_id, actor=session.subject)


@router.post("/v1/ops/cases/{case_id}/release")
def ops_release(case_id: str, body: schemas.ReleaseRequest, request: Request, session: Session = Depends(require_operator)) -> Dict[str, Any]:
    case = _c(request).service.operator_release(case_id, body.target_status, actor=session.subject, note=body.note)
    return {"id": case.id, "status": case.status.value, "version": case.version}


@router.post("/v1/ops/simulator/faults")
def ops_fault(body: schemas.FaultRequest, request: Request, session: Session = Depends(require_operator)) -> Dict[str, Any]:
    c = _c(request)
    sim = {"merchant_mock": c.merchant_mock, "issuer_mock": c.issuer_mock}.get(body.provider)
    if sim is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_simulator", "message": body.provider})
    sim.set_fault(body.order_ref, body.fault)
    return {"provider": body.provider, "order_ref": body.order_ref, "fault": body.fault}
