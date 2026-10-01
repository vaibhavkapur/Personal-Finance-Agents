from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.container import Container, build_container  # noqa: E402

CUSTOMER = "cus_demo_4"


@pytest.fixture
def container() -> Container:
    c = build_container(database_path=":memory:")
    c.seed()
    return c


@pytest.fixture
def client(container: Container):
    from fastapi.testclient import TestClient

    from backend.app.api.app import create_app

    return TestClient(create_app(container, seed=False))


CUSTOMER_HEADERS = {"Authorization": "Bearer tok_demo_customer"}
OTHER_HEADERS = {"Authorization": "Bearer tok_demo_other_customer"}
OPERATOR_HEADERS = {"Authorization": "Bearer tok_demo_operator"}


def open_case(c: Container, order_ref: str, target_minor: int, evidence=()):
    return c.service.create_case(customer_id=CUSTOMER, order_ref=order_ref, reason_code="promised_refund_missing", target_minor=target_minor, currency="USD", evidence_ids=list(evidence), actor=CUSTOMER)


def draft_and_approve(c: Container, case_id: str) -> Dict[str, Any]:
    d = c.service.draft_merchant_message(case_id, actor=CUSTOMER, customer_id=CUSTOMER)
    c.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"], action_payload_hash=d["payload_hash"], approval_challenge_id=d["approval_challenge_id"])
    return d


def send_merchant_message(c: Container, order_ref: str, target_minor: int, evidence=()):
    """Create, reconcile, draft, approve and execute the first merchant message."""
    case = open_case(c, order_ref, target_minor, evidence)
    c.service.reconcile(case.id, actor=CUSTOMER)
    d = draft_and_approve(c, case.id)
    c.worker.run_until_idle_sync()
    return case, d


def amounts(c: Container, case_id: str) -> Dict[str, Any]:
    return c.service.get_status(case_id)["amounts"]
