"""Engine and session management.

SQLite is the zero-dependency default for local runs and tests; PostgreSQL is
selected by ``DATABASE_URL``. Provider calls must never run inside a session
transaction (see ``workflows.executor``).
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import DateTime, TypeDecorator, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: Optional[datetime], dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    kwargs = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        path = database_url.replace("sqlite:///", "")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        else:
            # One shared connection so every thread sees the same in-memory database.
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, future=True, connect_args=connect_args, **kwargs)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL") if not database_url.endswith(":memory:") else None
            cursor.close()

    return engine


class Database:
    def __init__(self, database_url: str):
        self.url = database_url
        self.engine = make_engine(database_url)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session, future=True)

    def create_all(self) -> None:
        from . import models  # noqa: F401  (register tables)

        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        from . import models  # noqa: F401

        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def open_session(self) -> Session:
        return self._sessions()
