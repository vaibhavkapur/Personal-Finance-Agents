"""Engine and session management.

The engine is created lazily from ``settings.database_url`` so tests can point
the application at a throwaway database before the first session is opened.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def configure(url: str | None = None) -> Engine:
    """(Re)configure the global engine. Used by tests and the CLI."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url or settings.database_url)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        configure()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_schema() -> None:
    from app.persistence import models  # noqa: F401  (register tables)

    models.Base.metadata.create_all(get_engine())


def drop_schema() -> None:
    from app.persistence import models  # noqa: F401

    models.Base.metadata.drop_all(get_engine())
