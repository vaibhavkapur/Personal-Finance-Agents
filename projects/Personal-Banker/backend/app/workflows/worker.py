"""Durable worker: persisted jobs with leases (plan §16).

``run_once`` leases one due job, runs its handler and records the outcome.
A crashed worker leaves an expired lease that another worker picks up; the
handlers are written to be safe to repeat (they re-check state and look up
uncertain writes by request reference before acting).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import timedelta

from sqlalchemy import or_, select

from app import clock
from app.config import settings
from app.ids import new_id
from app.persistence import outbox
from app.persistence.db import session_scope
from app.persistence.models import Case, Job
from app.workflows import executor, states

log = logging.getLogger("personal_banker.worker")

HANDLERS = {
    "execute_action": lambda p: executor.execute_action(p["action_id"]),
    "reconcile_action": lambda p: executor.reconcile_action(p["action_id"], int(p.get("lookups", 0))),
    "verify_action": lambda p: executor.verify_action(p["action_id"]),
}


def lease_next_job(owner: str) -> Job | None:
    with session_scope() as session:
        now = clock.now(session)
        job = session.scalars(
            select(Job)
            .where(
                Job.run_at <= now,
                or_(Job.status == "pending", (Job.status == "leased") & (Job.lease_until <= now)),
            )
            .order_by(Job.run_at, Job.created_at)
            .limit(1)
        ).first()
        if job is None:
            return None
        job.status = "leased"
        job.lease_owner = owner
        job.lease_until = now + timedelta(seconds=settings.worker_lease_seconds)
        job.attempts += 1
        session.flush()
        session.expunge(job)
        return job


async def run_job(job: Job) -> str:
    handler = HANDLERS.get(job.type)
    if handler is None:
        raise ValueError(f"unknown job type {job.type}")
    return await handler(job.payload_json)


async def run_once(owner: str | None = None) -> dict | None:
    owner = owner or f"{socket.gethostname()}-{new_id('w')}"
    job = lease_next_job(owner)
    if job is None:
        publish_outbox()
        return None
    started = time.perf_counter()
    try:
        outcome = await run_job(job)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
        log.exception("job %s failed", job.id)
        with session_scope() as session:
            row = session.get(Job, job.id)
            now = clock.now(session)
            row.last_error = f"{type(exc).__name__}: {exc}"
            if row.attempts >= settings.worker_max_attempts:
                row.status = "failed"
                row.finished_at = now
                case = session.get(Case, row.case_id) if row.case_id else None
                if case is not None and states.can_transition(case.state, states.MANUAL_REVIEW):
                    states.transition(session, case, states.MANUAL_REVIEW, "worker", reason=f"job {row.type} failed after {row.attempts} attempts: {row.last_error}")
            else:
                row.status = "pending"
                row.lease_owner = None
                row.lease_until = None
                row.run_at = now + timedelta(minutes=min(2 ** row.attempts, 60))
        return {"job_id": job.id, "type": job.type, "outcome": f"error: {exc}"}
    with session_scope() as session:
        row = session.get(Job, job.id)
        row.status = "done"
        row.finished_at = clock.now(session)
        row.last_error = None
    publish_outbox()
    return {"job_id": job.id, "type": job.type, "outcome": outcome, "ms": int((time.perf_counter() - started) * 1000)}


async def drain(max_jobs: int = 50, owner: str | None = None) -> list[dict]:
    """Run due jobs until none remain (used by demos and tests)."""
    results = []
    for _ in range(max_jobs):
        result = await run_once(owner)
        if result is None:
            break
        results.append(result)
    return results


def publish_outbox() -> int:
    """Mark outbox messages as published. In this prototype "publishing" means
    the signed event is available to the operator timeline; a real deployment
    would deliver it to subscribers here."""
    count = 0
    with session_scope() as session:
        for msg in outbox.pending_outbox(session):
            outbox.mark_published(session, msg)
            count += 1
    return count


async def main_loop(poll_seconds: float = 1.0) -> None:  # pragma: no cover - long running
    logging.basicConfig(level=logging.INFO)
    owner = f"{socket.gethostname()}-{new_id('w')}"
    log.info("worker %s started", owner)
    while True:
        result = await run_once(owner)
        if result is None:
            await asyncio.sleep(poll_seconds)
        else:
            log.info("%s", result)


if __name__ == "__main__":  # pragma: no cover
    from app.persistence.db import create_schema

    create_schema()
    asyncio.run(main_loop())
