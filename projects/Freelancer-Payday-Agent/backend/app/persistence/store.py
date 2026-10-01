import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from ..domain.engine import DomainError

ROOT = Path(__file__).resolve().parents[3]


def uid(prefix):
    return prefix + "_" + uuid4().hex[:16]


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript((ROOT / "migrations/001_initial.sql").read_text())

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            # Serializes writers across API and worker processes; no read/modify/write races.
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def get(db, kind, ident, tenant):
        row = db.execute("SELECT data FROM entities WHERE kind=? AND id=? AND tenant=?", (kind, ident, tenant)).fetchone()
        if not row:
            raise DomainError("Record not found.", 404)
        return json.loads(row[0])

    @staticmethod
    def all(db, kind, tenant, customer=None):
        rows = db.execute("SELECT data FROM entities WHERE kind=? AND tenant=?" + (" AND customer=?" if customer else "") + " ORDER BY rowid",
                          (kind, tenant, customer) if customer else (kind, tenant)).fetchall()
        return [json.loads(r[0]) for r in rows]

    @staticmethod
    def put(db, kind, record, tenant, customer):
        db.execute("INSERT INTO entities VALUES(?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data WHERE entities.tenant=excluded.tenant AND entities.customer=excluded.customer",
                   (kind, record["id"], tenant, customer, json.dumps(record)))

    @staticmethod
    def journal(db, tenant, customer, key, ref_type, ref_id, now, old, new):
        lines = [{"account": k, "amount_minor": new.get(k, 0) - old.get(k, 0), "currency": "USD"}
                 for k in sorted(set(old) | set(new)) if new.get(k, 0) != old.get(k, 0)]
        delta = sum(line["amount_minor"] for line in lines)
        if delta:
            lines.append({"account": "bank_control", "amount_minor": -delta, "currency": "USD"})
        if lines:
            assert sum(line["amount_minor"] for line in lines) == 0
            db.execute("INSERT OR IGNORE INTO bucket_journals VALUES(?,?,?,?,?,?,?,?)",
                       (uid("jnl"), tenant, customer, key, ref_type, ref_id, now, json.dumps(lines)))
