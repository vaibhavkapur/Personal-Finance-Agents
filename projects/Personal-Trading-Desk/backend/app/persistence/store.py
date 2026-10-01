from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from app.domain.types import DomainError, uid

TABLES = {"accounts", "trading_mandates", "market_snapshots", "signals", "previews", "orders", "execution_fills", "exposure_reservations", "position_snapshots", "cases", "actions", "approvals", "tool_runs", "settings"}


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript((Path(__file__).parents[3] / "migrations/001_initial.sql").read_text())
            for table in TABLES:
                db.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL)")
                db.execute(f"CREATE INDEX IF NOT EXISTS {table}_tenant ON {table}(tenant)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS order_client_id ON orders(json_extract(body,'$.client_order_id'))")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS fill_execution_id ON execution_fills(json_extract(body,'$.broker_execution_id'))")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_order_per_signal ON orders(tenant,json_extract(body,'$.signal_id'))")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def tx(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def put(db, table, data, tenant="demo"):
        assert table in TABLES
        db.execute(f"INSERT INTO {table}(id,tenant,body) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body WHERE {table}.tenant=excluded.tenant", (data["id"], tenant, json.dumps(data)))
        return data

    @staticmethod
    def get(db, table, id, tenant="demo"):
        assert table in TABLES
        row = db.execute(f"SELECT body FROM {table} WHERE id=? AND tenant=?", (id, tenant)).fetchone()
        if not row:
            raise DomainError(f"{table.replace('_', ' ').title()} record not found", 404)
        return json.loads(row[0])

    @staticmethod
    def all(db, table, tenant="demo"):
        assert table in TABLES
        return [json.loads(r[0]) for r in db.execute(f"SELECT body FROM {table} WHERE tenant=? ORDER BY rowid", (tenant,))]

    @staticmethod
    def event(db, kind, data, at, case_id=None, actor="engine", tenant="demo"):
        id = uid("evt")
        payload = dict(id=id, case_id=case_id, event_type=kind, actor=actor, occurred_at=at, environment="mock", data=data)
        db.execute("INSERT INTO case_events(id,tenant,case_id,event_type,actor,occurred_at,data) VALUES(?,?,?,?,?,?,?)", (id, tenant, case_id, kind, actor, at, json.dumps(data)))
        db.execute("INSERT INTO outbox(event_id,payload) VALUES(?,?)", (id, json.dumps(payload)))
        return payload
