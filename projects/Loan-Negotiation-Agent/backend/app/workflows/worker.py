"""Durable worker with persisted jobs and leases.

Jobs live in the ``jobs`` table. A worker leases due jobs for a bounded time,
runs the handler and marks the job done or reschedules it with backoff. A
worker that dies mid-job leaves an expired lease that any other worker can
pick up, so a restart never loses approvals, timers or evidence.
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select

from ..adapters.base import LenderAdapter
from ..persistence.db import Database, new_id
from ..persistence.models import Case, Job, OutboxMessage
from . import states as st
from .case_service import CaseService
from .executor import Executor
from .states import transition

LEASE_SECONDS = 60
RETRY_BACKOFF_SECONDS = (5, 30, 120, 600, 1800)


class Worker:
    def __init__(self, db: Database, service: CaseService, adapter: LenderAdapter, owner: Optional[str] = None):
        self.db = db
        self.service = service
        self.executor = Executor(db, service, adapter)
        self.owner = owner or new_id("worker")
        self.delivered_outbox: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ leases
    def lease_jobs(self, limit: int = 10) -> List[str]:
        now = self.service.now()
        with self.db.session() as session:
            jobs = session.execute(
                select(Job)
                .where(
                    Job.run_at <= now,
                    or_(Job.status == "pending", (Job.status == "leased") & (Job.lease_until < now)),
                )
                .order_by(Job.run_at)
                .limit(limit)
            ).scalars().all()
            ids = []
            for job in jobs:
                job.status = "leased"
                job.lease_owner = self.owner
                job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
                job.attempts += 1
                job.updated_at = now
                session.add(job)
                ids.append(job.id)
            return ids

    async def run_job(self, job_id: str) -> Dict[str, Any]:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "leased" or job.lease_owner != self.owner:
                return {"outcome": "lost_lease"}
            job_type, payload, case_id, attempts, max_attempts = job.type, dict(job.payload_json), job.case_id, job.attempts, job.max_attempts
        try:
            result = await self._dispatch(job_type, payload, job_id)
            with self.db.session() as session:
                job = session.get(Job, job_id)
                job.status = "done"
                job.updated_at = self.service.now()
                job.last_error = None
                job.payload_json = {**payload, "result": result}
                session.add(job)
            return result
        except Exception as exc:  # noqa: BLE001 - worker must survive handler errors
            err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}"
            with self.db.session() as session:
                job = session.get(Job, job_id)
                now = self.service.now()
                job.last_error = err
                job.updated_at = now
                if attempts >= max_attempts:
                    job.status = "dead"
                    if case_id:
                        case = session.get(Case, case_id)
                        if case is not None and st.can_transition(case.state, st.MANUAL_REVIEW):
                            transition(session, case, st.MANUAL_REVIEW, "worker", now, data={"reason": f"job {job.type} failed permanently", "job_id": job.id})
                else:
                    job.status = "pending"
                    job.lease_owner = None
                    job.lease_until = None
                    job.run_at = now + timedelta(seconds=RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)])
                session.add(job)
            return {"outcome": "error", "error": str(exc)}

    async def _dispatch(self, job_type: str, payload: Dict[str, Any], job_id: str) -> Dict[str, Any]:
        if job_type == "execute_action":
            return await self.executor.execute_action(payload["action_id"], job_id)
        if job_type == "reconcile_action":
            return await self.executor.reconcile_action(payload["action_id"], job_id)
        if job_type == "poll_application":
            return await self.executor.poll_application(payload["application_id"], int(payload.get("polls", 0)))
        if job_type == "process_provider_event":
            return await self.executor.process_provider_event(payload["provider_event_id"])
        if job_type == "expire_offers":
            return {"expired": self.executor.expire_offers()}
        if job_type == "deliver_outbox":
            return {"delivered": self.deliver_outbox()}
        raise ValueError(f"unknown job type {job_type}")

    def deliver_outbox(self, limit: int = 50) -> int:
        """Deliver signed outbox messages. The prototype's subscriber is an in-memory sink."""
        count = 0
        with self.db.session() as session:
            pending = session.execute(select(OutboxMessage).where(OutboxMessage.status == "pending").order_by(OutboxMessage.created_at).limit(limit)).scalars().all()
            for msg in pending:
                self.delivered_outbox.append({"id": msg.id, "topic": msg.topic, "payload": msg.payload_json, "signature": msg.signature})
                msg.status = "delivered"
                msg.attempts += 1
                msg.delivered_at = self.service.now()
                session.add(msg)
                count += 1
        return count

    async def run_once(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Lease and run due jobs once; also run housekeeping (expiry, outbox)."""
        results = []
        self.executor.expire_offers()
        self.deliver_outbox()
        for job_id in self.lease_jobs(limit):
            results.append({"job_id": job_id, **(await self.run_job(job_id))})
        return results

    async def drain(self, max_rounds: int = 20) -> List[Dict[str, Any]]:
        """Run until no due jobs remain (used by tests and the demo)."""
        all_results: List[Dict[str, Any]] = []
        for _ in range(max_rounds):
            results = await self.run_once()
            all_results.extend(results)
            if not results:
                break
        return all_results

    async def run_forever(self, interval_seconds: float = 2.0) -> None:  # pragma: no cover - long-running
        while True:
            await self.run_once()
            await asyncio.sleep(interval_seconds)
