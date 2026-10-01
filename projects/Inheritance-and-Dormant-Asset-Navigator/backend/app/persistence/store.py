"""Encrypted case aggregates with optimistic concurrency and a transactional job outbox."""
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, UniqueConstraint, create_engine, select, update

ROOT = Path(__file__).resolve().parents[3]
metadata = MetaData()
cases = Table('cases', metadata, Column('id', String, primary_key=True), Column('tenant', String, nullable=False), Column('version', Integer, nullable=False), Column('sealed', Text, nullable=False))
jobs = Table('jobs', metadata, Column('id', String, primary_key=True), Column('case_id', String, nullable=False), Column('tenant', String, nullable=False), Column('action_id', String, nullable=False, unique=True), Column('status', String, nullable=False), Column('lease_until', String), Column('attempts', Integer, nullable=False), Column('error', String))
provider_requests = Table('provider_requests', metadata, Column('request_ref', String, primary_key=True), Column('payload_hash', String, nullable=False), Column('case_ref', String, unique=True, nullable=False), Column('sealed', Text, nullable=False))
inbox = Table('event_inbox', metadata, Column('provider', String, primary_key=True), Column('event_id', String, primary_key=True), Column('case_id', String, nullable=False))
events = Table('case_events', metadata, Column('id', String, primary_key=True), Column('case_id', String, nullable=False), Column('sequence', Integer, nullable=False), Column('sealed', Text, nullable=False), UniqueConstraint('case_id', 'sequence'))
settings = Table('settings', metadata, Column('id', String, primary_key=True), Column('value', Text, nullable=False))


class DomainError(Exception):
    def __init__(self, message, status=409):
        self.message, self.status = message, status
        super().__init__(message)


def uid(prefix):
    return f'{prefix}_{secrets.token_hex(8)}'


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, url=None, key=None):
        data = ROOT / 'data'
        data.mkdir(exist_ok=True, mode=0o700)
        url = url or os.getenv('DATABASE_URL', f'sqlite:///{data / "estate.db"}')
        key = key or os.getenv('ESTATE_ENCRYPTION_KEY')
        if not key:
            keyfile = data / 'encryption.key'
            try:
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(Fernet.generate_key())
            except FileExistsError:
                pass
            key = keyfile.read_bytes()
        self.cipher = Fernet(key.encode() if isinstance(key, str) else key)
        self.engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 30} if url.startswith('sqlite') else {}, pool_pre_ping=True)
        metadata.create_all(self.engine)

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value, sort_keys=True).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def now(self, con=None):
        if con is None:
            with self.engine.connect() as connection:
                return self.now(connection)
        value = con.execute(select(settings.c.value).where(settings.c.id == 'clock')).scalar_one_or_none()
        return value or utcnow()

    def create(self, case):
        with self.engine.begin() as con:
            con.execute(cases.insert().values(id=case['id'], tenant=case['tenant'], version=case['version'], sealed=self.seal(case)))
        return case

    def load(self, case_id, tenant, con=None):
        if con is None:
            with self.engine.connect() as connection:
                return self.load(case_id, tenant, connection)
        row = con.execute(select(cases).where(cases.c.id == case_id, cases.c.tenant == tenant)).mappings().first()
        if row is None:
            raise DomainError('Estate workspace not found.', 404)
        return self.unseal(row['sealed'])

    def list_cases(self, tenant):
        with self.engine.connect() as con:
            return [self.unseal(row.sealed) for row in con.execute(select(cases.c.sealed).where(cases.c.tenant == tenant))]

    def save(self, con, case, expected):
        case['version'] = expected + 1
        result = con.execute(update(cases).where(cases.c.id == case['id'], cases.c.tenant == case['tenant'], cases.c.version == expected).values(version=case['version'], sealed=self.seal(case)))
        if result.rowcount != 1:
            raise DomainError('The workspace changed. Refresh and review the latest version.')

    @contextmanager
    def edit(self, case_id, tenant, expected=None):
        with self.engine.begin() as con:
            case = self.load(case_id, tenant, con)
            version = case['version']
            if expected is not None and version != expected:
                raise DomainError('The workspace changed. Refresh and review the latest version.')
            yield con, case
            self.save(con, case, version)

    def event(self, con, case, kind, actor, asset_id=None, previous=None, next_state=None, detail=''):
        event = {'id': uid('evt'), 'sequence': len(case['events']) + 1, 'type': kind, 'actor': actor, 'timestamp': self.now(con), 'asset_id': asset_id, 'previous_state': previous, 'next_state': next_state, 'expected_case_version': case['version'], 'detail': detail, 'environment': 'mock'}
        case['events'].append(event)
        con.execute(events.insert().values(id=event['id'], case_id=case['id'], sequence=event['sequence'], sealed=self.seal(event)))
        return event
