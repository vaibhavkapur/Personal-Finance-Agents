"""Persisted jobs with leases. The worker claims a job, does the work, and releases it."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from . import models as m
from .db import new_id


def enqueue_job(
    session: Session,
    job_type: str,
    payload: Dict[str, Any],
    run_at: datetime,
    dedupe_key: Optional[str] = None,
) -> Optional[m.Job]:
    """Insert a job unless an open job with the same dedupe key already exists."""
    if dedupe_key:
        existing = session.scalars(select(m.Job).where(m.Job.dedupe_key == dedupe_key)).first()
        if existing is not None:
            if existing.status in ("queued", "running"):
                return existing
            # Completed job with the same key: free the key so the new job can be inserted.
            existing.dedupe_key = None
            session.flush()
    job = m.Job(
        id=new_id("job"),
        type=job_type,
        payload_json=payload,
        dedupe_key=dedupe_key,
        run_at=run_at,
        status="queued",
        attempts=0,
        created_at=run_at,
        updated_at=run_at,
    )
    session.add(job)
    session.flush()
    return job


def claim_jobs(session: Session, owner: str, now: datetime, lease_seconds: int, limit: int = 10) -> List[m.Job]:
    """Claim due jobs whose lease is free or expired. Safe to run from several workers."""
    candidates = session.scalars(
        select(m.Job)
        .where(
            m.Job.status.in_(("queued", "running")),
            m.Job.run_at <= now,
            or_(m.Job.lease_until.is_(None), m.Job.lease_until < now),
        )
        .order_by(m.Job.run_at)
        .limit(limit)
    ).all()
    claimed: List[m.Job] = []
    lease_until = now + timedelta(seconds=lease_seconds)
    for job in candidates:
        result = session.execute(
            update(m.Job)
            .where(
                m.Job.id == job.id,
                or_(m.Job.lease_until.is_(None), m.Job.lease_until < now),
                m.Job.status.in_(("queued", "running")),
            )
            .values(lease_until=lease_until, lease_owner=owner, status="running", attempts=m.Job.attempts + 1, updated_at=now)
        )
        if result.rowcount == 1:
            session.refresh(job)
            claimed.append(job)
    session.commit()
    return claimed


def complete_job(session: Session, job: m.Job, now: datetime) -> None:
    job.status = "done"
    job.lease_until = None
    job.lease_owner = None
    job.updated_at = now


def reschedule_job(session: Session, job: m.Job, now: datetime, delay_seconds: float, error: Optional[str] = None, max_attempts: int = 12) -> None:
    job.lease_until = None
    job.lease_owner = None
    job.last_error = error
    job.updated_at = now
    if job.attempts >= max_attempts:
        job.status = "dead"
        return
    job.status = "queued"
    job.run_at = now + timedelta(seconds=delay_seconds)


def backoff_seconds(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    return min(cap, base * (2 ** max(0, attempt - 1)))
