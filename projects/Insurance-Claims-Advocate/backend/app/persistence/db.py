from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        kwargs = {}
        if ":memory:" in database_url:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        engine = create_engine(database_url, connect_args=connect_args, future=True, **kwargs)

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(database_url, future=True, pool_pre_ping=True)


def init_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


class Database:
    def __init__(self, database_url: str):
        self.url = database_url
        self.engine = make_engine(database_url)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        init_schema(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def new_session(self) -> Session:
        return self._factory()
