"""SQLite-backed database for the prototype.

The schema is written in portable SQL (see migrations/) so the same repositories
can target PostgreSQL later. Provider calls never run inside a transaction;
callers persist a pending record, commit, call the provider, then reconcile.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..config import MIGRATIONS_DIR


class _ExecResult:
    """Cursor stand-in. Rows are read while the connection lock is held."""

    def __init__(self, rows: List[sqlite3.Row], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> Optional[sqlite3.Row]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> List[sqlite3.Row]:
        return list(self._rows)


class Database:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._depth = 0

    # -- lifecycle -----------------------------------------------------------
    def migrate(self, migrations_dir: Path = MIGRATIONS_DIR) -> List[str]:
        applied: List[str] = []
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
            done = {r["name"] for r in self._conn.execute("SELECT name FROM schema_migrations")}
        for sql_file in sorted(migrations_dir.glob("*.sql")):
            if sql_file.name in done:
                continue
            # executescript issues its own COMMIT, so it cannot run inside transaction().
            with self._lock:
                self._conn.executescript(sql_file.read_text())
                self._conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (sql_file.name,))
            applied.append(sql_file.name)
        return applied

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- transactions --------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant transaction. Only the outermost block commits/rolls back."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    # -- helpers -------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> _ExecResult:
        # Materialize inside the lock. Returning a live cursor and fetching later
        # races uvicorn's threadpool against the shared sqlite3 connection (SIGSEGV).
        with self._lock:
            cur = self._conn.execute(sql, params)
            return _ExecResult(list(cur.fetchall()), cur.rowcount)

    def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def insert(self, table: str, row: Dict[str, Any]) -> None:
        cols = list(row.keys())
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        self.execute(sql, [row[c] for c in cols])

    def update(self, table: str, key: Dict[str, Any], values: Dict[str, Any]) -> int:
        sets = ", ".join(f"{c} = ?" for c in values)
        where = " AND ".join(f"{c} = ?" for c in key)
        cur = self.execute(f"UPDATE {table} SET {sets} WHERE {where}", [*values.values(), *key.values()])
        return cur.rowcount
