from __future__ import annotations
import os, uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer, Text, JSON, select, update, UniqueConstraint, event
from sqlalchemy.pool import StaticPool

metadata = MetaData()
cases = Table("financial_incidents", metadata, Column("id", String, primary_key=True), Column("customer_id", String, nullable=False), Column("status", String, nullable=False), Column("version", Integer, nullable=False), Column("discovered_at", String), Column("data", JSON, nullable=False))
actions = Table("actions", metadata, Column("id", String, primary_key=True), Column("case_id", String, nullable=False), Column("customer_id", String, nullable=False), Column("kind", String), Column("payload", JSON), Column("payload_hash", String), Column("status", String), Column("idempotency_key", String), Column("challenge_id", String), Column("expires_at", String), Column("approval_id", String), Column("provider_ref", String), Column("result", JSON), UniqueConstraint("customer_id", "idempotency_key"))
approvals = Table("approvals", metadata, Column("id", String, primary_key=True), Column("action_id", String, unique=True), Column("approver", String), Column("payload_hash", String), Column("expires_at", String), Column("revoked_at", String), Column("consumed_at", String))
events = Table("case_events", metadata, Column("id", String, primary_key=True), Column("case_id", String), Column("sequence", Integer), Column("event_type", String), Column("actor", String), Column("occurred_at", String), Column("previous_state", String), Column("next_state", String), Column("data", JSON), UniqueConstraint("case_id", "sequence"))
jobs = Table("outbox_jobs", metadata, Column("id", String, primary_key=True), Column("action_id", String, unique=True), Column("status", String), Column("priority", Integer), Column("lease_until", String), Column("attempts", Integer), Column("last_error", String))
inbox = Table("provider_event_inbox", metadata, Column("id", String, primary_key=True), Column("provider_id", String), Column("event_id", String), Column("case_id", String), Column("payload", JSON), UniqueConstraint("provider_id", "event_id"))
provider_actions = Table("mock_provider_actions", metadata, Column("request_ref", String, primary_key=True), Column("provider_id", String), Column("payload_hash", String), Column("result", JSON))
settings = Table("settings", metadata, Column("key", String, primary_key=True), Column("value", JSON))
sessions = Table("sessions", metadata, Column("token_hash", String, primary_key=True), Column("customer_id", String), Column("expires_at", String))
tool_runs = Table("tool_runs", metadata, Column("id", String, primary_key=True), Column("case_id", String), Column("name", String), Column("occurred_at", String), Column("outcome", String), Column("metadata", JSON))

def uid(prefix): return prefix + "_" + uuid.uuid4().hex[:16]
def iso(dt): return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
def parse(value): return datetime.fromisoformat(value.replace("Z", "+00:00"))

def row(conn, table, condition):
    r = conn.execute(select(table).where(condition)).mappings().first()
    return dict(r) if r else None

def rows(conn, table, condition=None):
    stmt = select(table)
    if condition is not None: stmt = stmt.where(condition)
    return [dict(r) for r in conn.execute(stmt).mappings()]

class Store:
    def __init__(self, url=None):
        url = url or os.getenv("DATABASE_URL", "sqlite:///./concierge.db")
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
        if url == "sqlite://": kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def pragmas(db, _):
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA journal_mode=WAL")
        metadata.create_all(self.engine)
        with self.engine.begin() as c:
            if not row(c, settings, settings.c.key == "clock"):
                c.execute(settings.insert().values(key="clock", value={"now": "2026-09-25T13:30:00Z"}))
    @contextmanager
    def tx(self):
        with self.engine.begin() as c: yield c
    def now(self):
        with self.tx() as c: return row(c, settings, settings.c.key == "clock")["value"]["now"]
