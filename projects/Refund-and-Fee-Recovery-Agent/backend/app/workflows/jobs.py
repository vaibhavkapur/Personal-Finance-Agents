"""Persisted jobs with leases. The single worker polls this queue."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..clock import Clock, format_ts
from ..domain.models import Job
from ..ids import new_id
from ..persistence.repositories import Repositories


class JobQueue:
    def __init__(self, repos: Repositories, clock: Clock) -> None:
        self.repos = repos
        self.clock = clock

    def enqueue(self, job_type: str, *, case_id: Optional[str], payload: Dict[str, Any], run_at: Optional[datetime] = None, dedupe_key: Optional[str] = None) -> Optional[Job]:
        job = Job(
            id=new_id("job"), type=job_type, case_id=case_id, payload=payload,
            run_at=format_ts(run_at or self.clock.now()), status="pending", dedupe_key=dedupe_key,
            created_at=format_ts(self.clock.now()),
        )
        return job if self.repos.add_job(job) else None

    def lease(self, job: Job, owner: str, seconds: int) -> Optional[Job]:
        """Take the lease if nobody else holds it. Uses a conditional update so two workers cannot both win."""
        now = self.clock.now()
        until = format_ts(now + timedelta(seconds=seconds))
        n = self.repos.db.execute(
            "UPDATE jobs SET lease_owner = ?, lease_until = ?, status = 'running', attempts = attempts + 1 "
            "WHERE id = ? AND status IN ('pending','running') AND (lease_until IS NULL OR lease_until < ?)",
            (owner, until, job.id, format_ts(now)),
        ).rowcount
        if n != 1:
            return None
        return self.repos.get_job(job.id)

    def complete(self, job: Job) -> None:
        self.repos.save_job(job.model_copy(update={"status": "done", "completed_at": format_ts(self.clock.now()), "lease_owner": None, "lease_until": None}))

    def fail(self, job: Job, error: str, *, retry_in_seconds: Optional[int]) -> None:
        if retry_in_seconds is None:
            self.repos.save_job(job.model_copy(update={"status": "failed", "last_error": error, "lease_owner": None, "lease_until": None, "completed_at": format_ts(self.clock.now())}))
        else:
            run_at = format_ts(self.clock.now() + timedelta(seconds=retry_in_seconds))
            self.repos.save_job(job.model_copy(update={"status": "pending", "last_error": error, "lease_owner": None, "lease_until": None, "run_at": run_at}))

    def due(self) -> List[Job]:
        return self.repos.due_jobs(format_ts(self.clock.now()))

    def all(self) -> List[Job]:
        return self.repos.all_jobs()
