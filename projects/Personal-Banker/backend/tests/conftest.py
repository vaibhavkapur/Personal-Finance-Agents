from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("PB_SERVE_FRONTEND", "false")

from app.config import FIXTURES_DIR  # noqa: E402
from app.persistence import db  # noqa: E402
from app.persistence.seed import seed  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path: Path):
    """A seeded, throwaway SQLite database bound to the global engine."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    db.configure(url)
    db.create_schema()
    with db.session_scope() as session:
        seed(session)
    yield url
    db.get_engine().dispose()


@pytest.fixture()
def expected() -> dict:
    with open(FIXTURES_DIR / "expected_allocations.json") as fh:
        return json.load(fh)


@pytest.fixture()
def fixture() -> dict:
    with open(FIXTURES_DIR / "demo_customer.json") as fh:
        return json.load(fh)


@pytest.fixture()
def client(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


CUSTOMER = {"X-Customer-Id": "cus_demo_1"}
OPERATOR = {"X-Customer-Id": "ops_demo", "X-Role": "operator"}
