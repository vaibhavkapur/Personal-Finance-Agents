from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.api.main import create_app  # noqa: E402
from backend.app.clock import FixtureClock, parse_iso  # noqa: E402
from backend.app.config import Settings  # noqa: E402
from backend.app.container import Container  # noqa: E402

BORROWER = {"Authorization": "Bearer demo-borrower-token"}
OTHER = {"Authorization": "Bearer other-borrower-token"}
OPERATOR = {"Authorization": "Bearer demo-operator-token"}
FIXTURE_NOW = "2026-09-26T12:00:00+00:00"


def make_container(**overrides: Any) -> Container:
    settings = Settings()
    settings.database_url = "sqlite:///:memory:"
    settings.use_fixture_clock = True
    for k, v in overrides.items():
        setattr(settings, k, v)
    container = Container(settings=settings, clock=FixtureClock(parse_iso(FIXTURE_NOW)))
    container.seed()
    return container


@pytest.fixture
def container() -> Container:
    return make_container()


@pytest.fixture
def client(container: Container) -> TestClient:
    app = create_app(container, run_worker=False)
    return TestClient(app)


@pytest.fixture
def run_worker(container: Container):
    def _run():
        return asyncio.get_event_loop().run_until_complete(container.worker.drain())

    return _run


def approve_screen(client: TestClient, screen: Dict[str, Any], headers=BORROWER):
    return client.post(
        f"/v1/actions/{screen['action_id']}/approve",
        json={
            "expected_case_version": screen["expected_case_version"],
            "action_payload_hash": screen["action_payload_hash"],
            "approval_challenge_id": screen["approval_challenge_id"],
        },
        headers=headers,
    )


def create_ready_case(client: TestClient, horizon: int = 48, docs=("offer_doc_a", "offer_doc_b", "offer_doc_c"), max_cash=None) -> Dict[str, Any]:
    """Create a case, confirm facts and run the comparison; return the case view."""
    r = client.post(
        "/v1/loan-cases",
        json={"customer_id": "cus_demo_5", "mortgage_id": "mortgage_demo_1", "holding_horizon_months": horizon, "maximum_cash_to_close_minor": max_cash, "offer_document_ids": list(docs)},
        headers=BORROWER,
    )
    assert r.status_code == 201, r.text
    case_id = r.json()["id"]
    r = client.post(f"/v1/loan-cases/{case_id}/facts", json={"current_balance_confirmed": True, "payment_includes_escrow": True}, headers=BORROWER)
    assert r.status_code == 200, r.text
    r = client.post(f"/v1/loan-cases/{case_id}/compare", json={}, headers=BORROWER)
    assert r.status_code == 200, r.text
    return client.get(f"/v1/loan-cases/{case_id}", headers=BORROWER).json()


def offer_of(view: Dict[str, Any], lender_id: str, status=("indicative_quote", "revised_quote", "final_offer")) -> Dict[str, Any]:
    cands = [o for o in view["offers"] if o["lender_id"] == lender_id and o["status"] in status]
    return sorted(cands, key=lambda o: -o["version"])[0]
