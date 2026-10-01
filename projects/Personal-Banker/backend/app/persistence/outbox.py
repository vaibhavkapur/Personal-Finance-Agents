"""Transactional outbox and provider event inbox (plan §14).

* ``emit`` writes a signed application event in the same transaction as the
  state change that produced it. A publisher (the worker) marks it published.
* ``receive`` deduplicates incoming provider events by (provider, event id).
  Consumers are idempotent so at-least-once delivery is safe.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.adapters.mock_bank import sign_payload
from app.config import settings
from app.ids import new_id
from app.persistence.models import InboxEvent, OutboxMessage


def emit(session: Session, topic: str, case_id: str | None, data: dict) -> OutboxMessage:
    now = clock.now(session)
    payload = {
        "id": new_id("evt"),
        "type": topic,
        "case_id": case_id,
        "occurred_at": clock.iso(now),
        "environment": settings.environment,
        "data": data,
    }
    msg = OutboxMessage(
        id=payload["id"],
        topic=topic,
        case_id=case_id,
        payload_json=payload,
        signature=sign_payload(payload),
        created_at=now,
    )
    session.add(msg)
    return msg


def pending_outbox(session: Session, limit: int = 100) -> list[OutboxMessage]:
    return list(
        session.scalars(
            select(OutboxMessage).where(OutboxMessage.published_at.is_(None)).order_by(OutboxMessage.created_at).limit(limit)
        )
    )


def mark_published(session: Session, msg: OutboxMessage) -> None:
    msg.published_at = clock.now(session)


def verify_signature(payload: dict, signature: str | None) -> bool:
    if not signature:
        return False
    expected = sign_payload(payload)
    import hmac

    return hmac.compare_digest(expected, signature)


def receive(session: Session, provider_id: str, payload: dict, signature: str | None) -> tuple[InboxEvent, bool]:
    """Store an incoming provider event. Returns (event, is_new)."""
    event_id = str(payload.get("id") or "")
    if not event_id:
        raise ValueError("provider event has no id")
    existing = session.scalars(
        select(InboxEvent).where(InboxEvent.provider_id == provider_id, InboxEvent.event_id == event_id)
    ).first()
    if existing is not None:
        return existing, False
    event = InboxEvent(
        id=new_id("inbox"),
        provider_id=provider_id,
        event_id=event_id,
        event_type=str(payload.get("type") or "unknown"),
        payload_json=payload,
        signature_valid=verify_signature(payload, signature),
        received_at=clock.now(session),
    )
    session.add(event)
    session.flush()
    return event, True
