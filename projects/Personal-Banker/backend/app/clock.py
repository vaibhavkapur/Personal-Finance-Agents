"""Controllable simulation clock shared by the application and the mock bank.

The clock lives in the database so the API process, the worker and the
simulator agree on "now". Tests and demos advance it explicitly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.persistence.models import SimClock


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_clock(session: Session, start: str | None = None) -> SimClock:
    clock = session.get(SimClock, 1)
    if clock is None:
        clock = SimClock(id=1, now=parse_iso(start or settings.clock_start))
        session.add(clock)
        session.flush()
    return clock


def now(session: Session) -> datetime:
    return ensure_clock(session).now


def today(session: Session) -> date:
    return now(session).date()


def set_now(session: Session, value: datetime) -> datetime:
    clock = ensure_clock(session)
    if value < clock.now:
        raise ValueError("the simulation clock cannot move backwards")
    clock.now = value
    session.flush()
    return clock.now


def advance(session: Session, *, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    clock = ensure_clock(session)
    clock.now = clock.now + timedelta(days=days, hours=hours, minutes=minutes)
    session.flush()
    return clock.now
