"""Serialized customer aggregates, immutable audit history, durable jobs and outbox."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, case_id TEXT, event_type TEXT NOT NULL, actor TEXT NOT NULL, occurred_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_customer ON events(customer_id, id);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER UNIQUE NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, proposal_id TEXT NOT NULL, status TEXT NOT NULL, lease_until REAL NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS inbox (provider TEXT NOT NULL, event_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(provider,event_id));
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_timers (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, name TEXT NOT NULL, at TEXT NOT NULL, outcome TEXT NOT NULL);
'''

class Store:
    def __init__(self, path=None):
        self.path = path or os.getenv('WEALTH_DB', 'var/wealth.db')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def load(self, db, customer='cus_demo_7'):
        row = db.execute('SELECT data FROM customers WHERE id=?', (customer,)).fetchone()
        return json.loads(row['data']) if row else None

    def save(self, db, state):
        db.execute('INSERT INTO customers VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,data=excluded.data',
                   (state['customer_id'], state['version'], json.dumps(state)))

    def event(self, db, state, event_type, data=None, actor='customer'):
        event = db.execute('INSERT INTO events(customer_id,case_id,event_type,actor,occurred_at,data) VALUES(?,?,?,?,?,?)',
                          (state['customer_id'], state['case']['id'], event_type, actor, state['clock'], json.dumps(data or {})))
        db.execute('INSERT INTO outbox(event_id) VALUES(?)', (event.lastrowid,))

    def claim(self):
        with self.transaction() as db:
            now = time.time()
            row = db.execute("SELECT * FROM jobs WHERE (status='pending' OR (status='running' AND lease_until<?)) AND available_at<=? ORDER BY available_at LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',lease_until=?,attempts=attempts+1 WHERE id=?", (now+30, row['id']))
            return dict(row)

    def finish_job(self, job_id, retry=False):
        with self.transaction() as db:
            db.execute('UPDATE jobs SET status=?,lease_until=0,available_at=? WHERE id=?', ('pending' if retry else 'done', time.time()+2, job_id))

    def flush_outbox(self):
        # Local notification destination is the persisted activity timeline.
        with self.transaction() as db:
            db.execute('UPDATE outbox SET delivered=1 WHERE delivered=0')
