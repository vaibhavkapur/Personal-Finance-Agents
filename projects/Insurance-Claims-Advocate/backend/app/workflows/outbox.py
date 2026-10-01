"""Transactional outbox and persisted jobs. Both are written in the same transaction as the state change."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock, iso
from ..ids import canonical_json, new_id
from ..persistence.models import Job, OutboxMessage


def enqueue_outbox(session: Session, clock: Clock, *, case_id: Optional[str], event_type: str, data: Dict[str, Any], environment: str) -> OutboxMessage:
    payload = {
        "id": new_id("evt"),
        "type": event_type,
        "case_id": case_id,
        "occurred_at": clock.now_iso(),
        "environment": environment,
        "data": data,
    }
    msg = OutboxMessage(id=payload["id"], case_id=case_id, event_type=event_type, payload_json=payload, status="pending", created_at=clock.now_iso())
    session.add(msg)
    return msg


def sign_outbound(secret: str, payload: Dict[str, Any]) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def enqueue_job(session: Session, clock: Clock, job_type: str, payload: Dict[str, Any], *, run_at=None, dedupe_key: Optional[str] = None, max_attempts: int = 5) -> Optional[Job]:
    if dedupe_key:
        existing = session.scalars(select(Job).where(Job.dedupe_key == dedupe_key)).first()
        if existing:
            return existing
    job = Job(
        id=new_id("job"),
        job_type=job_type,
        payload_json=payload,
        run_at=iso(run_at) if run_at else clock.now_iso(),
        status="pending",
        attempts=0,
        max_attempts=max_attempts,
        dedupe_key=dedupe_key,
        created_at=clock.now_iso(),
    )
    session.add(job)
    return job
