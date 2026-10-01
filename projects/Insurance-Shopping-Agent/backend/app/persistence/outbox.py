"""Transactional outbox for application events and signed webhook delivery."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.hashing import canonical_json
from . import models as m
from .db import new_id


def enqueue_event(
    session: Session,
    case_id: Optional[str],
    event_type: str,
    data: Dict[str, Any],
    now: datetime,
    environment: str,
) -> m.OutboxEvent:
    """Write an event in the same transaction as the state change that caused it."""
    event_id = new_id("evt")
    payload = {
        "id": event_id,
        "type": event_type,
        "case_id": case_id,
        "occurred_at": now.isoformat(),
        "environment": environment,
        "data": data,
    }
    row = m.OutboxEvent(id=event_id, case_id=case_id, event_type=event_type, payload_json=payload, status="pending", attempts=0, created_at=now)
    session.add(row)
    return row


def pending_events(session: Session, limit: int = 50) -> List[m.OutboxEvent]:
    return list(session.scalars(select(m.OutboxEvent).where(m.OutboxEvent.status == "pending").order_by(m.OutboxEvent.created_at).limit(limit)).all())


def sign_payload(secret: str, body: bytes, timestamp: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), (timestamp + ".").encode("utf-8") + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def verify_signature(secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    expected = sign_payload(secret, body, timestamp)
    return hmac.compare_digest(expected, signature or "")


def serialize_event(payload: Dict[str, Any]) -> bytes:
    return canonical_json(payload).encode("utf-8")
