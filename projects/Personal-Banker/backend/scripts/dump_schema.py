"""Emit the DDL for the current SQLAlchemy metadata into migrations/.

The prototype creates its schema with ``create_all`` at startup; these files
are the reviewable, versioned equivalent for PostgreSQL and SQLite. Re-run
after changing ``app/persistence/models.py`` and add a new numbered file for
incremental changes once the schema is in use.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_mock_engine  # noqa: E402
from sqlalchemy.dialects import postgresql, sqlite  # noqa: E402

from app.config import REPO_ROOT  # noqa: E402
from app.persistence.models import Base  # noqa: E402

OUT = REPO_ROOT / "migrations"


def dump(dialect, filename: str) -> None:
    statements: list[str] = []

    def executor(sql, *multiparams, **params):
        statements.append(str(sql.compile(dialect=dialect)).strip() + ";")

    engine = create_mock_engine(f"{dialect.name}://", executor)
    Base.metadata.create_all(engine, checkfirst=False)
    header = f"-- Generated from app/persistence/models.py for {dialect.name}. Do not edit by hand; re-run scripts/dump_schema.py.\n\n"
    (OUT / filename).write_text(header + "\n\n".join(statements) + "\n")
    print(f"wrote {OUT / filename} ({len(statements)} statements)")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    dump(postgresql.dialect(), "0001_initial_postgres.sql")
    dump(sqlite.dialect(), "0001_initial_sqlite.sql")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
