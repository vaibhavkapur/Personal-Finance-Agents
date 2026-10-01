import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

class Conflict(Exception):
    pass

class DB:
    def __init__(self, conn, postgres=False):
        self.conn, self.postgres = conn, postgres
    def execute(self, sql, args=()):
        return self.conn.execute(sql.replace("?", "%s") if self.postgres else sql, args)

class Store:
    def __init__(self, url=None):
        self.url = url or os.getenv("DATABASE_URL", str(ROOT / ".data/tax.db"))
        self.postgres = self.url.startswith("postgres")
        if not self.postgres:
            Path(self.url).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            for statement in (ROOT / "migrations/001_initial.sql").read_text().split(";"):
                if statement.strip():
                    db.execute(statement)
    @contextmanager
    def transaction(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, row_factory=dict_row)
            conn.execute("SELECT pg_advisory_xact_lock(10402025)")
        else:
            conn = sqlite3.connect(self.url, timeout=20)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield DB(conn, self.postgres)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_case(db, case_id, tenant_id=None):
        row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row or (tenant_id is not None and row["tenant_id"] != tenant_id):
            raise KeyError("Case not found")
        return json.loads(row["data"])

    @staticmethod
    def get_action(db, action_id, tenant_id=None):
        row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if not row or (tenant_id is not None and row["tenant_id"] != tenant_id):
            raise KeyError("Action not found")
        return json.loads(row["data"])

    @staticmethod
    def save_action(db, action):
        db.execute("UPDATE actions SET status=?,data=? WHERE id=?", (action["status"], json.dumps(action), action["id"]))

    @staticmethod
    def save_case(db, case, expected):
        count = db.execute("UPDATE cases SET version=?,state=?,data=? WHERE id=? AND version=?", (case["version"], case["state"], json.dumps(case), case["id"], expected)).rowcount
        if count != 1:
            raise Conflict("Case changed. Refresh and review the latest version.")
