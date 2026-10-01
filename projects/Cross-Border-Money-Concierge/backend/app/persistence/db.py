"""Portable SQL storage. All state mutations use a short transaction; provider I/O does not."""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Integer, Text,
                        UniqueConstraint, select, event)

metadata = MetaData()

def table(name, *columns, constraints=()):
    return Table(name, metadata, Column('id', String, primary_key=True), *columns,
                 Column('data', Text, nullable=False), *constraints)

cases = table('cases', Column('customer_id', String, nullable=False), Column('status', String), Column('version', Integer))
beneficiaries = table('beneficiaries', Column('customer_id', String, nullable=False))
quotes = table('remittance_quotes', Column('case_id', String, nullable=False))
transfers = table('remittance_transfers', Column('case_id', String, unique=True, nullable=False), Column('request_ref', String, unique=True))
actions = table('actions', Column('case_id', String), Column('customer_id', String), Column('status', String), Column('idem_key', String), constraints=(UniqueConstraint('customer_id', 'idem_key'),))
approvals = table('approvals', Column('action_id', String, unique=True))
events = table('case_events', Column('case_id', String), Column('sequence', Integer), constraints=(UniqueConstraint('case_id', 'sequence'),))
jobs = table('outbox_jobs', Column('case_id', String), Column('status', String), Column('due_at', String), Column('lease_until', String))
inbox = table('provider_event_inbox', Column('provider_id', String), Column('event_id', String), constraints=(UniqueConstraint('provider_id', 'event_id'),))
receipts = table('delivery_receipts', Column('transfer_id', String, unique=True))
requirements = table('provider_requirements', Column('transfer_id', String), Column('provider_request_id', String, unique=True))
documents = table('documents', Column('customer_id', String))
tool_runs = table('tool_runs', Column('customer_id', String), Column('case_id', String))
provider_transfers = table('mock_provider_transfers', Column('request_ref', String, unique=True))
provider_operations = table('mock_provider_operations', Column('request_ref', String, unique=True))
settings = table('settings')
sessions = table('sessions', Column('customer_id', String))


def pack(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def get(conn, t, identifier):
    row = conn.execute(select(t.c.data).where(t.c.id == identifier)).scalar_one_or_none()
    return json.loads(row) if row is not None else None


def rows(conn, t, *conditions):
    return [json.loads(v) for v in conn.execute(select(t.c.data).where(*conditions)).scalars()]


def put(conn, t, value):
    fields = {c.name: value.get(c.name) for c in t.columns if c.name != 'data'}
    fields['data'] = pack(value)
    if get(conn, t, value['id']) is None:
        conn.execute(t.insert().values(**fields))
    else:
        conn.execute(t.update().where(t.c.id == value['id']).values(**fields))
    return value


class Database:
    def __init__(self, url=None):
        self.url = url or os.getenv('DATABASE_URL', 'sqlite:///./concierge.db')
        kw = {'connect_args': {'check_same_thread': False, 'timeout': 30}} if self.url.startswith('sqlite') else {}
        self.engine = create_engine(self.url, **kw)
        if self.url.startswith('sqlite'):
            @event.listens_for(self.engine, 'connect')
            def configure(dbapi, _):
                dbapi.execute('PRAGMA journal_mode=WAL')
                dbapi.execute('PRAGMA foreign_keys=ON')
        metadata.create_all(self.engine)

    @contextmanager
    def tx(self):
        with self.engine.connect() as conn:
            if self.url.startswith('sqlite'):
                conn.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                conn.begin()
                # One prototype coordinator lock also serializes jobs, approval consumption and clock.
                conn.exec_driver_sql('SELECT pg_advisory_xact_lock(808319)')
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def utcnow():
    return datetime.now(timezone.utc).isoformat()
