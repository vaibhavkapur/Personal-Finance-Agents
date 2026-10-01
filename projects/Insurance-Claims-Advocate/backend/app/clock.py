"""A controllable clock. The simulator, timers and approvals all read time from here so demos and tests can advance time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Clock:
    """Wall clock by default; frozen when `freeze` is called (fixture clock)."""

    def __init__(self, frozen_at: Optional[datetime] = None):
        self._frozen: Optional[datetime] = frozen_at

    def now(self) -> datetime:
        if self._frozen is not None:
            return self._frozen
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return iso(self.now())

    def freeze(self, at: datetime) -> None:
        self._frozen = at.astimezone(timezone.utc)

    def advance(self, **kwargs) -> datetime:
        base = self.now()
        self._frozen = base + timedelta(**kwargs)
        return self._frozen

    def unfreeze(self) -> None:
        self._frozen = None

    @property
    def is_frozen(self) -> bool:
        return self._frozen is not None
