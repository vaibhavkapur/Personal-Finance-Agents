"""Restart the worker mid-case and recover without losing approvals, timers or evidence."""
from __future__ import annotations

import pytest

from app.persistence import models as m
from app.persistence.jobs import claim_jobs
from app.workflows.worker import Worker
from tests.conftest import Flow


@pytest.mark.asyncio
async def test_worker_crash_after_claim_is_recovered_by_another_worker(client, ctx, clock):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    prepared = flow.prepare("ins_northwind_a")
    flow.approve(prepared)

    # Worker A claims the execute_action job and then dies before finishing.
    with ctx.db.session() as session:
        claimed = claim_jobs(session, "worker-A", ctx.now(), ctx.settings.worker_lease_seconds)
    assert [j.type for j in claimed] == ["execute_action"]

    # Worker B starts while the lease is still held: nothing to do.
    worker_b = Worker(ctx, owner="worker-B")
    summary = await worker_b.run_once()
    assert summary["jobs"] == []
    assert flow.view()["status"] == "awaiting_approval"

    # Lease expires -> worker B picks the job up. The approval was never lost.
    clock.advance(seconds=ctx.settings.worker_lease_seconds + 1)
    summary = await worker_b.run_once()
    assert [j["type"] for j in summary["jobs"]] == ["execute_action"]
    view = flow.view()
    assert view["status"] == "underwriting"
    assert view["actions"][-1]["approval"]["consumed_at"] is not None
    with ctx.db.session() as session:
        approvals = session.query(m.Approval).all()
        assert len(approvals) == 1
        submits = [p for p in session.query(m.ProviderRequestLog).all() if p.operation == "submit_application"]
        assert len(submits) == 1

    # Timers (underwriting poll) survive the restart too.
    clock.advance(hours=2)
    worker_c = Worker(ctx, owner="worker-C")
    await worker_c.run_until_idle()
    final = flow.view()
    assert final["status"] == "completed"
    assert final["policy"]["verified"] is True
    events = [t["event_type"] for t in final["timeline"]]
    assert "insurance.action.approved" in events and "insurance.policy.verified" in events


@pytest.mark.asyncio
async def test_duplicate_execution_is_prevented_when_two_workers_race(client, ctx, clock):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    flow.approve(flow.prepare("ins_harborline_b"))
    a, b = Worker(ctx, owner="A"), Worker(ctx, owner="B")
    sa = await a.run_once()
    sb = await b.run_once()
    executed = [j for j in sa["jobs"] + sb["jobs"] if j["type"] == "execute_action"]
    assert len(executed) == 1
    with ctx.db.session() as session:
        submits = [p for p in session.query(m.ProviderRequestLog).all() if p.operation == "submit_application"]
    assert len(submits) == 1
