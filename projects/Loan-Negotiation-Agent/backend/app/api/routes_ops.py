"""Operator endpoints: adapter requests, jobs, clock, faults, replay and metrics.

Nothing here can approve an action or mark a case completed; replay goes
through the same job handlers and the same authorization checks.
"""
from __future__ import annotations

from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..container import Container
from ..persistence.models import Action, Case, Job, OutboxMessage, ProviderEvent, ToolRun
from ..workflows import states as st
from ..workflows.states import IllegalTransitionError, transition
from . import schemas
from .deps import get_container, get_session, require_operator

router = APIRouter(prefix="/v1/ops", tags=["operations"], dependencies=[Depends(require_operator)])


def _redact(payload: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    def scrub(value):
        if isinstance(value, dict):
            return {k: ("[redacted]" if any(f in k.lower() for f in fields) else scrub(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(payload)


@router.get("/cases")
def list_cases(session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    cases = session.execute(select(Case).order_by(Case.updated_at.desc())).scalars().all()
    return {"cases": [{"id": c.id, "tenant_id": c.tenant_id, "customer_id": c.customer_id, "status": c.state, "version": c.version, "updated_at": c.updated_at.isoformat()} for c in cases]}


@router.get("/cases/{case_id}")
def case_detail(case_id: str, session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    view = container.service.case_view(session, case)
    view["timeline"] = container.service.timeline(session, case)
    tool_runs = session.execute(select(ToolRun).where(ToolRun.case_id == case.id).order_by(ToolRun.created_at)).scalars().all()
    view["tool_runs"] = [{"id": t.id, "tool": t.tool_name, "input_ref": t.input_ref, "output_ref": t.output_ref, "source": t.source, "authority": t.authority, "latency_ms": t.latency_ms, "outcome": t.outcome, "at": t.created_at.isoformat()} for t in tool_runs]
    return _redact(view, container.settings.log_redaction_fields)


@router.post("/cases/{case_id}/resume")
def resume_case(case_id: str, body: schemas.ResumeCaseRequest, session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    if case.state != st.MANUAL_REVIEW:
        raise HTTPException(status_code=409, detail="only cases in manual_review can be resumed by an operator")
    if body.next_state in (st.MOCK_CLOSED, st.KEEP_CURRENT):
        raise HTTPException(status_code=403, detail="operators cannot mark a case completed; completion requires provider evidence")
    try:
        transition(session, case, body.next_state, "operator", container.clock.now(), data={"reason": body.reason})
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"case_id": case.id, "status": case.state, "version": case.version}


@router.get("/jobs")
def list_jobs(session: Session = Depends(get_session)) -> Dict[str, Any]:
    jobs = session.execute(select(Job).order_by(Job.run_at.desc()).limit(200)).scalars().all()
    return {"jobs": [{"id": j.id, "type": j.type, "case_id": j.case_id, "status": j.status, "run_at": j.run_at.isoformat(), "attempts": j.attempts, "lease_owner": j.lease_owner, "last_error": j.last_error} for j in jobs]}


@router.post("/worker/run-once")
async def worker_run_once(container: Container = Depends(get_container)) -> Dict[str, Any]:
    results = await container.worker.run_once()
    return {"results": results, "clock_now": container.clock.now().isoformat()}


@router.post("/worker/drain")
async def worker_drain(container: Container = Depends(get_container)) -> Dict[str, Any]:
    results = await container.worker.drain()
    return {"results": results, "clock_now": container.clock.now().isoformat()}


@router.post("/clock/advance")
def clock_advance(body: schemas.ClockAdvanceRequest, container: Container = Depends(get_container)) -> Dict[str, Any]:
    clock = container.clock
    if not hasattr(clock, "advance"):
        raise HTTPException(status_code=409, detail="system clock cannot be advanced")
    now = clock.advance(days=body.days, hours=body.hours, minutes=body.minutes)
    return {"clock_now": now.isoformat()}


@router.get("/clock")
def clock_now(container: Container = Depends(get_container)) -> Dict[str, Any]:
    return {"clock_now": container.clock.now().isoformat(), "fixture_clock": hasattr(container.clock, "advance")}


@router.get("/adapter")
def adapter_info(container: Container = Depends(get_container)) -> Dict[str, Any]:
    return {
        "capabilities": container.adapter.capabilities.as_dict(),
        "requests": container.network.request_log[-200:],
        "pending_callbacks": [{k: v for k, v in c.items() if k != "signature"} for c in container.network.callbacks if not c.get("delivered")],
        "faults": container.network.faults,
    }


@router.post("/faults", status_code=201)
def inject_fault(body: schemas.FaultInjectionRequest, container: Container = Depends(get_container)) -> Dict[str, Any]:
    container.network.inject_fault(body.kind, body.lender_id, body.operation, body.once, **body.params)
    return {"faults": container.network.faults}


@router.post("/mock/deliver-callbacks")
async def deliver_callbacks(container: Container = Depends(get_container)) -> Dict[str, Any]:
    """Deliver due simulator callbacks to our own signed webhook endpoint."""
    from .main import app_for_container  # local import to avoid cycle

    app = app_for_container(container)
    delivered = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock-delivery") as client:
        for event in container.network.due_callbacks():
            body = {k: v for k, v in event.items() if k not in ("signature", "deliver_at", "delivered")}
            resp = await client.post("/v1/provider-events/lenders", json=body, headers={"X-Lender-Id": event["provider"], "X-Signature": event["signature"]})
            container.network.mark_delivered(event["id"])
            delivered.append({"event_id": event["id"], "type": event["type"], "status_code": resp.status_code, "response": resp.json()})
    return {"delivered": delivered}


@router.post("/replay/{provider_event_id}")
def replay_event(provider_event_id: str, session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    evt = session.get(ProviderEvent, provider_event_id)
    if evt is None:
        raise HTTPException(status_code=404, detail="event not found")
    evt.status = "received"
    session.add(evt)
    job = container.service.enqueue(session, "process_provider_event", evt.case_id, {"provider_event_id": evt.id, "replay": True})
    return {"job_id": job.id, "note": "replay reuses the normal handler; it cannot bypass action authorization"}


@router.get("/metrics")
def metrics(session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    now = container.clock.now()
    cases = session.execute(select(Case)).scalars().all()
    by_state: Dict[str, int] = {}
    for c in cases:
        by_state[c.state] = by_state.get(c.state, 0) + 1
    pending_jobs = session.execute(select(Job).where(Job.status.in_(["pending", "leased"]))).scalars().all()
    oldest = min((j.run_at for j in pending_jobs), default=None)
    tool_errors = session.execute(select(func.count()).select_from(ToolRun).where(ToolRun.outcome != "ok")).scalar()
    actions = session.execute(select(Action)).scalars().all()
    abandoned = sum(1 for a in actions if a.status in ("cancelled", "invalidated"))
    duplicates_prevented = sum(1 for r in container.network.request_log if r["outcome"] == "duplicate_returned")
    outbox_pending = session.execute(select(func.count()).select_from(OutboxMessage).where(OutboxMessage.status == "pending")).scalar()
    return {
        "cases_by_state": by_state,
        "waiting_on_customer": sum(v for k, v in by_state.items() if k in st.WAITING_ON_CUSTOMER),
        "waiting_on_provider": sum(v for k, v in by_state.items() if k in st.WAITING_ON_PROVIDER),
        "queue_depth": len(pending_jobs),
        "queue_age_seconds": (now - oldest).total_seconds() if oldest else 0,
        "tool_errors": tool_errors,
        "approvals_abandoned_or_invalidated": abandoned,
        "duplicate_actions_prevented": duplicates_prevented,
        "outbox_pending": outbox_pending,
        "model_cost_per_completed_case_usd": container.agent.cost_summary() if hasattr(container, "agent") else None,
        "clock_now": now.isoformat(),
    }
