"""Simulation controls: advance the shared clock, let the mock bank act, and
deliver its callbacks through the same signed inbox path a real provider
webhook would use."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app import clock
from app.adapters import mock_bank
from app.persistence import outbox
from app.persistence.db import session_scope
from app.persistence.models import InboxEvent
from app.workflows.executor import process_inbox_event


def deliver_provider_event(provider_id: str, payload: dict, signature: str | None) -> dict:
    """Store and process one provider callback (idempotent by provider/event id)."""
    with session_scope() as session:
        event, is_new = outbox.receive(session, provider_id, payload, signature)
        if not is_new:
            return {"inbox_id": event.id, "duplicate": True, "result": event.result}
        result = process_inbox_event(session, event)
        event.processed_at = clock.now(session)
        event.result = result
        return {"inbox_id": event.id, "duplicate": False, "result": result}


def replay_inbox_event(inbox_id: str) -> dict:
    """Operator replay: re-run the consumer for a stored event. It cannot bypass
    action authorization because the consumer only schedules verification."""
    with session_scope() as session:
        event = session.get(InboxEvent, inbox_id)
        if event is None:
            raise KeyError(inbox_id)
        result = process_inbox_event(session, event)
        event.replay_count += 1
        event.processed_at = clock.now(session)
        event.result = f"replayed: {result}"
        return {"inbox_id": event.id, "result": result, "replay_count": event.replay_count}


def advance_clock(days: int = 0, hours: int = 0, minutes: int = 0) -> dict:
    """Advance the simulation clock one day at a time so the bank processes
    each day's due instructions in order and callbacks arrive dated correctly."""
    delivered: list[dict] = []
    steps = [(1, 0, 0)] * days + ([(0, hours, minutes)] if (hours or minutes) else [])
    if not steps:
        steps = [(0, 0, 0)]
    for d, h, m in steps:
        with session_scope() as session:
            if d or h or m:
                clock.advance(session, days=d, hours=h, minutes=m)
            events = mock_bank.process_due_instructions(session)
        for event in events:
            provider_id = "bank_harbor"
            delivered.append(deliver_provider_event(provider_id, event, mock_bank.sign_payload(event)))
    with session_scope() as session:
        now = clock.now(session)
    return {"now": clock.iso(now), "callbacks_delivered": delivered}


def current_time() -> datetime:
    with session_scope() as session:
        return clock.now(session)


def set_submit_mode(mode: str) -> None:
    with session_scope() as session:
        mock_bank.set_submit_mode(session, mode)


def set_offer_mode(mode: str, offer_id: str = "off_harbor_12m") -> dict | None:
    with session_scope() as session:
        return mock_bank.set_offer_mode(session, mode, offer_id)


def revoke_access(account_id: str, revoked: bool = True) -> None:
    with session_scope() as session:
        mock_bank.revoke_access(session, account_id, revoked)


def inbox_events() -> list[dict]:
    with session_scope() as session:
        rows = session.scalars(select(InboxEvent).order_by(InboxEvent.received_at)).all()
        return [
            {"id": r.id, "provider_id": r.provider_id, "event_id": r.event_id, "event_type": r.event_type, "signature_valid": r.signature_valid, "received_at": clock.iso(r.received_at), "processed_at": clock.iso(r.processed_at) if r.processed_at else None, "result": r.result, "replay_count": r.replay_count}
            for r in rows
        ]
