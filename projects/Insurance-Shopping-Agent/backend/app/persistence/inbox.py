"""Provider event inbox with (provider, event_id) deduplication."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .db import new_id


def record_inbound(session: Session, provider: str, external_event_id: str, payload: Dict[str, Any], now: datetime) -> Tuple[m.InboxEvent, bool]:
    """Return (row, is_new). Duplicate deliveries return the original row."""
    existing = session.scalars(
        select(m.InboxEvent).where(m.InboxEvent.provider == provider, m.InboxEvent.external_event_id == external_event_id)
    ).first()
    if existing is not None:
        return existing, False
    row = m.InboxEvent(
        id=new_id("inbox"),
        provider=provider,
        external_event_id=external_event_id,
        payload_json=payload,
        received_at=now,
        status="received",
    )
    session.add(row)
    session.flush()
    return row, True


def get_inbox_event(session: Session, inbox_id: str) -> Optional[m.InboxEvent]:
    return session.get(m.InboxEvent, inbox_id)
