"""Controllable clock shared by the application, worker and mock insurers."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Optional


class Clock:
    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError

    def today(self):
        return self.now().date()


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixtureClock(Clock):
    """Controllable clock.

    frozen=True  -> time only moves when advance()/set() is called (tests).
    frozen=False -> time flows normally from `start`, and advance() shifts it forward
                    (local demo servers, so scheduled jobs still become due).
    """

    def __init__(self, start: datetime, frozen: bool = True) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._frozen = frozen
        self._now = start
        self._offset = start - datetime.now(timezone.utc)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            if self._frozen:
                return self._now
            return datetime.now(timezone.utc) + self._offset

    def advance(self, **kwargs) -> datetime:
        delta = timedelta(**kwargs)
        with self._lock:
            self._now = self._now + delta
            self._offset = self._offset + delta
        return self.now()

    def set(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        with self._lock:
            self._now = value
            self._offset = value - datetime.now(timezone.utc)
        return self.now()


def build_clock(fixture: bool, start_iso: Optional[str], frozen: bool = False) -> Clock:
    if fixture:
        start = datetime.fromisoformat(start_iso) if start_iso else datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        return FixtureClock(start, frozen=frozen)
    return SystemClock()
