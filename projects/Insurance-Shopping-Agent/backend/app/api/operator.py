"""Operator view: adapter requests, transitions, pending actions, redacted evidence, controls."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from ..clock import FixtureClock
from ..context import AppContext, redact
from ..persistence import models as m
from ..persistence import repositories as repo
from ..workflows import states as st
from ..workflows.case_service import CaseError, CaseService
from ..workflows.events import ProviderEventService
from ..workflows.states import transition
from ..workflows.worker import Worker
from .deps import Principal, current_operator, get_ctx
from .schemas import ClockAdvanceRequest, FaultRequest, ResolveReviewRequest

router = APIRouter(prefix="/v1/operator", tags=["operator"])


@router.get("/cases")
def list_cases(_: Principal = Depends(current_operator), ctx: AppContext = Depends(get_ctx)) -> List[Dict[str, Any]]:
    with ctx.db.session() as session:
        return [{"id": c.id, "customer_id": c.customer_id, "status": c.state, "version": c.version, "review_reason": c.review_reason, "updated_at": c.updated_at.isoformat()} for c in repo.all_cases(session)]


@router.get("/cases/{case_id}")
def case_detail(case_id: str, _: Principal = Depends(current_operator), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    cases = CaseService(ctx)
    view = cases.case_view(case_id, None)
    # Operators see redacted evidence references, not identity data.
    view["needs"]["address"] = "[redacted]" if view["needs"].get("address") else None
    if view.get("application"):
        view["application"]["payload"] = redact(view["application"]["payload"])
    if view.get("policy"):
        view["policy"]["declarations"] = redact(view["policy"]["declarations"])
    with ctx.db.session() as session:
        view["events"] = [
            {"sequence": e.sequence, "type": e.event_type, "actor": e.actor, "from": e.from_state, "to": e.to_state, "expected_version": e.expected_version, "at": e.occurred_at.isoformat(), "data": redact(e.data_json)}
            for e in repo.events_for_case(session, case_id)
        ]
        view["provider_requests"] = [
            {"id": p.id, "insurer_id": p.insurer_id, "operation": p.operation, "request_ref": p.request_ref, "environment": p.environment, "latency_ms": p.latency_ms, "outcome": p.outcome, "at": p.started_at.isoformat(), "request": p.request_redacted, "response": p.response_redacted}
            for p in repo.provider_requests_for_case(session, case_id)
        ]
        view["tool_runs"] = [
            {"id": t.id, "tool": t.tool_name, "input": t.input_redacted, "output_ref": t.output_ref, "latency_ms": t.latency_ms, "model_version": t.model_version, "prompt_version": t.prompt_version, "outcome": t.outcome, "error": t.error, "at": t.started_at.isoformat()}
            for t in repo.tool_runs_for_case(session, case_id)
        ]
        view["jobs"] = [
            {"id": j.id, "type": j.type, "status": j.status, "attempts": j.attempts, "run_at": j.run_at.isoformat(), "lease_until": j.lease_until.isoformat() if j.lease_until else None, "last_error": j.last_error}
            for j in session.scalars(select(m.Job).order_by(m.Job.created_at)).all()
            if (j.payload_json.get("case_id") == case_id) or _job_touches_case(session, j, case_id)
        ]
    return view


def _job_touches_case(session, job: m.Job, case_id: str) -> bool:
    p = job.payload_json
    if "action_id" in p:
        a = session.get(m.Action, p["action_id"])
        return bool(a and a.case_id == case_id)
    if "task_id" in p:
        t = session.get(m.QuoteTask, p["task_id"])
        return bool(t and t.case_id == case_id)
    return False


@router.get("/metrics")
def metrics(_: Principal = Depends(current_operator), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    now = ctx.now()
    with ctx.db.session() as session:
        states = dict(session.execute(select(m.Case.state, func.count()).group_by(m.Case.state)).all())
        tool_errors = session.scalar(select(func.count()).select_from(m.ToolRun).where(m.ToolRun.outcome != "ok")) or 0
        tool_total = session.scalar(select(func.count()).select_from(m.ToolRun)) or 0
        oldest_job = session.scalar(select(func.min(m.Job.run_at)).where(m.Job.status.in_(("queued", "running"))))
        queue_depth = session.scalar(select(func.count()).select_from(m.Job).where(m.Job.status.in_(("queued", "running")))) or 0
        dead_jobs = session.scalar(select(func.count()).select_from(m.Job).where(m.Job.status == "dead")) or 0
        actions = dict(session.execute(select(m.Action.status, func.count()).group_by(m.Action.status)).all())
        approvals_total = session.scalar(select(func.count()).select_from(m.Approval)) or 0
        duplicates_prevented = session.scalar(select(func.count()).select_from(m.InboxEvent)) or 0
        provider_outcomes = dict(session.execute(select(m.ProviderRequestLog.outcome, func.count()).group_by(m.ProviderRequestLog.outcome)).all())
        pending_outbox = session.scalar(select(func.count()).select_from(m.OutboxEvent).where(m.OutboxEvent.status == "pending")) or 0
        completed = session.scalar(select(func.count()).select_from(m.Case).where(m.Case.state == st.COMPLETED)) or 0
    waiting_on_customer = sum(states.get(s, 0) for s in (st.COLLECTING, st.NEEDS_INFORMATION, st.AWAITING_SELECTION, st.AWAITING_APPROVAL))
    waiting_on_provider = sum(states.get(s, 0) for s in (st.QUOTING, st.SUBMITTED, st.UNDERWRITING, st.BOUND))
    return {
        "now": now.isoformat(),
        "environment": ctx.environment,
        "adapter_mode": ctx.registry.mode,
        "cases_by_state": states,
        "cases_waiting_on_customer": waiting_on_customer,
        "cases_waiting_on_provider": waiting_on_provider,
        "tool_runs": {"total": tool_total, "errors": tool_errors},
        "queue": {"depth": queue_depth, "oldest_due_at": oldest_job.isoformat() if oldest_job else None, "age_seconds": (now - oldest_job).total_seconds() if oldest_job else 0, "dead": dead_jobs},
        "actions_by_status": actions,
        "approvals_recorded": approvals_total,
        "approval_abandonment": {"proposed_open": actions.get("proposed", 0), "rejected": actions.get("rejected", 0), "invalidated": actions.get("invalidated", 0)},
        "duplicate_provider_events_seen": duplicates_prevented,
        "provider_requests_by_outcome": provider_outcomes,
        "outbox_pending": pending_outbox,
        "model_cost_per_completed_case": {"tool_runs_per_completed_case": (tool_total / completed) if completed else None, "note": "token cost is not measured for the scripted provider"},
        "capability_matrix": ctx.registry.capability_matrix(),
    }


@router.post("/worker/run-once")
async def worker_run_once(_: Principal = Depends(current_operator), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    return await Worker(ctx, owner="operator-run-once").run_once()


@router.post("/clock/advance")
async def clock_advance(body: ClockAdvanceRequest, _: Principal = Depends(current_operator), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    if not isinstance(ctx.clock, FixtureClock):
        raise CaseError("the system clock cannot be advanced", 409)
    delta = {"minutes": body.minutes, "hours": body.hours, "days": body.days}
    now = ctx.clock.advance(**delta)
    remote: Dict[str, Any] = {}
    if ctx.environment == "mock":
        # Remote mock insurer agents keep their own fixture clocks; keep them in step.
        for insurer_id, adapter in ctx.registry.adapters.items():
            advance = getattr(adapter, "mock_advance_clock", None)
            if advance is not None and insurer_id not in ctx.registry.insurers:
                remote[insurer_id] = await advance(**delta)
    return {"now": now.isoformat(), "remote_insurers": remote}


@router.post("/faults")
async def inject_fault(body: FaultRequest, _: Principal = Depends(current_operator), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    insurer = ctx.registry.insurers.get(body.insurer_id)
    if insurer is not None:
        insurer.inject_fault(body.kind, body.operation, **body.params)
        return {"ok": True, "insurer_id": body.insurer_id, "kind": body.kind, "target": "in-process"}
    adapter = ctx.registry.adapters.get(body.insurer_id)
    inject = getattr(adapter, "mock_inject_fault", None)
    if inject is not None and await inject(body.kind, body.operation, body.params):
        return {"ok": True, "insurer_id": body.insurer_id, "kind": body.kind, "target": "remote-mock"}
    raise CaseError("fault injection is only available for mock insurers", 409)


@router.post("/replay/{inbox_id}")
def replay(inbox_id: str, _: Principal = Depends(current_operator), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    return ProviderEventService(ctx).replay(inbox_id)


@router.post("/cases/{case_id}/resolve-review")
def resolve_review(case_id: str, body: ResolveReviewRequest, principal: Principal = Depends(current_operator), ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    """Operators can move a case out of manual_review; they cannot approve actions or fabricate completion."""
    targets = {"resume_underwriting": st.UNDERWRITING, "back_to_selection": st.AWAITING_SELECTION, "decline": st.DECLINED, "back_to_collecting": st.COLLECTING}
    target = targets.get(body.resolution)
    if target is None:
        raise CaseError("unsupported resolution; allowed: %s" % ", ".join(sorted(targets)), 422)
    now = ctx.now()
    with ctx.db.session() as session:
        case = repo.get_case(session, case_id)
        if case.state != st.MANUAL_REVIEW:
            raise CaseError("case is not in manual_review", 409)
        transition(session, case, target, "operator:" + principal.id, "insurance.case.review_resolved", now, ctx.environment, {"resolution": body.resolution, "note": body.note})
        case.review_reason = None
        if target == st.UNDERWRITING:
            from ..persistence.jobs import enqueue_job

            enqueue_job(session, "poll_underwriting", {"case_id": case.id}, now, dedupe_key="poll_underwriting:%s:review" % case.id)
        return {"case_id": case.id, "status": case.state, "version": case.version}
