"""Single durable worker.

Jobs are persisted with leases, so a worker restart mid-case loses nothing:
approvals, timers (jobs with a future run_at) and evidence all live in the
database. Provider calls happen outside transactions; a timeout leaves the
action `unknown` until a lookup by request reference resolves it.
"""
from __future__ import annotations

import asyncio
import traceback
from typing import Any, Dict, List

from ..clock import Clock
from ..domain.models import Job
from ..persistence.outbox import Outbox
from .case_service import CaseService
from .jobs import JobQueue

MAX_ATTEMPTS = 5


class Worker:
    def __init__(self, *, jobs: JobQueue, service: CaseService, outbox: Outbox, clock: Clock, worker_id: str = "worker-1", lease_seconds: int = 60) -> None:
        self.jobs = jobs
        self.service = service
        self.outbox = outbox
        self.clock = clock
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.processed: List[Dict[str, Any]] = []

    async def run_once(self) -> int:
        """Process every due job once and dispatch the outbox. Returns jobs handled."""
        handled = 0
        for job in self.jobs.due():
            leased = self.jobs.lease(job, self.worker_id, self.lease_seconds)
            if leased is None:
                continue
            handled += 1
            try:
                result = await self._dispatch(leased)
                self.jobs.complete(leased)
                self.processed.append({"job_id": leased.id, "type": leased.type, "result": result})
            except Exception as exc:  # noqa: BLE001 - the worker must survive any job failure
                retry = None if leased.attempts >= MAX_ATTEMPTS else min(600, 30 * (2 ** (leased.attempts - 1)))
                self.jobs.fail(leased, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}", retry_in_seconds=retry)
                self.processed.append({"job_id": leased.id, "type": leased.type, "error": str(exc)})
        self.outbox.dispatch_pending()
        return handled

    def run_once_sync(self) -> int:
        return asyncio.run(self.run_once())

    async def run_until_idle(self, max_rounds: int = 20) -> int:
        total = 0
        for _ in range(max_rounds):
            n = await self.run_once()
            total += n
            if n == 0:
                break
        return total

    def run_until_idle_sync(self, max_rounds: int = 20) -> int:
        return asyncio.run(self.run_until_idle(max_rounds))

    async def _dispatch(self, job: Job) -> Dict[str, Any]:
        p = job.payload
        if job.type == "execute_action":
            return await self.service.execute_action(p["action_id"], worker_id=self.worker_id)
        if job.type == "resolve_uncertain_write":
            return await self.service.resolve_uncertain_write(p["action_id"], int(p.get("attempt", 1)), worker_id=self.worker_id)
        if job.type == "merchant_followup_check":
            return self.service.merchant_followup_check(job.case_id, p["channel_id"], worker_id=self.worker_id)
        if job.type == "credit_wait_check":
            return self.service.credit_wait_check(job.case_id, worker_id=self.worker_id)
        raise ValueError(f"unknown job type {job.type}")
