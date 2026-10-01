"""Clock abstraction.

The simulator and worker must run against a controllable clock so that
multi-day follow-up cadences, approval expiry and deadline alerts can be
exercised deterministically in tests and demos.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def parse_ts(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixtureClock:
    """A settable clock. `advance` moves time forward; `set` jumps to an instant."""

    def __init__(self, start: str = "2026-09-20T12:00:00Z") -> None:
        self._now = parse_ts(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, days: float = 0, hours: float = 0, minutes: float = 0, seconds: float = 0) -> datetime:
        self._now = self._now + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        return self._now

    def set(self, value: str) -> datetime:
        self._now = parse_ts(value)
        return self._now
