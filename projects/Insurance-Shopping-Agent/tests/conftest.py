from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from app.clock import FixtureClock
from app.config import Settings
from app.context import AppContext
from app.fixtures import household_by_customer
from app.main import create_app
from app.workflows.approvals import ApplicationService
from app.workflows.case_service import CaseService
from app.workflows.worker import Worker

FIXTURE_NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
TOKENS = {"cus_demo_1": "tok_cus_demo_1", "cus_demo_2": "tok_cus_demo_2", "cus_demo_3": "tok_cus_demo_3", "operator": "tok_operator"}

BASE_NEEDS = {
    "state_code": "CA",
    "product": "renters",
    "desired_effective_date": "2026-11-01",
    "property_limit_minor": 3000000,
    "liability_limit_minor": 10000000,
    "replacement_cost_required": True,
}

# Answers to each insurer's questions derived from household facts (never guessed by the app).
QUESTION_FACTS = {
    "nw_q_dog": "owns_dog",
    "nw_q_claims": "prior_claims_5y",
    "nw_q_smoke": "smoke_detectors",
    "nw_q_home_business": "home_business",
    "hl_q_animals": "animals_in_household",
    "hl_q_building": "building_type",
    "hl_q_claims": "prior_claims_3y",
    "cp_q_high_value": "high_value_items_over_1500",
    "cp_q_claims": "prior_claims_5y",
}


def household_answers(customer_id: str, question_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    hh = household_by_customer(customer_id)
    out = []
    for qid, fact in QUESTION_FACTS.items():
        if question_ids is not None and qid not in question_ids:
            continue
        value = hh["facts"].get(fact) if fact != "building_type" else hh["building_type"]
        out.append({"question_id": qid, "value": value})
    return out


def interview_answers(customer_id: str, deductible_cap: int = 100000, item_classes=("jewelry", "bicycles"), preference: str = "lower_premium") -> List[Dict[str, Any]]:
    hh = household_by_customer(customer_id)
    return [
        {"field": "address", "value": hh["address"]},
        {"field": "deductible_cap_minor", "value": deductible_cap},
        {"field": "required_item_classes", "value": list(item_classes)},
        {"field": "deductible_preference", "value": preference},
    ]


@pytest.fixture
def clock() -> FixtureClock:
    return FixtureClock(FIXTURE_NOW, frozen=True)


@pytest.fixture
def ctx(tmp_path, clock) -> AppContext:
    settings = Settings(database_url="sqlite:///%s/test.db" % tmp_path, fixture_clock=True, environment="mock", adapter_mode="direct", tool_call_budget=8)
    return AppContext(settings, clock=clock)


@pytest.fixture
def services(ctx):
    cases = CaseService(ctx)
    return cases, ApplicationService(ctx, cases), Worker(ctx, owner="test-worker")


@pytest.fixture
def client(ctx) -> TestClient:
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c


def auth(customer: str) -> Dict[str, str]:
    return {"Authorization": "Bearer " + TOKENS[customer]}


class Flow:
    """Drives a case through the HTTP API the way the frontend would."""

    def __init__(self, client: TestClient, ctx: AppContext, customer_id: str = "cus_demo_1") -> None:
        self.client = client
        self.ctx = ctx
        self.customer_id = customer_id
        self.headers = auth(customer_id)
        self.case_id: Optional[str] = None

    def create(self, **overrides) -> Dict[str, Any]:
        body = {**BASE_NEEDS, **overrides}
        r = self.client.post("/v1/insurance-shopping-cases", json=body, headers=self.headers)
        assert r.status_code == 201, r.text
        self.case_id = r.json()["id"]
        return r.json()

    def answers(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        r = self.client.post("/v1/insurance-shopping-cases/%s/answers" % self.case_id, json={"answers": items}, headers=self.headers)
        assert r.status_code == 200, r.text
        return r.json()

    def interview(self, **kwargs) -> Dict[str, Any]:
        return self.answers(interview_answers(self.customer_id, **kwargs))

    def request_quotes(self) -> Dict[str, Any]:
        r = self.client.post("/v1/insurance-shopping-cases/%s/quote-requests" % self.case_id, headers=self.headers)
        assert r.status_code == 202, r.text
        return r.json()

    def answer_questions(self, question_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        return self.answers(household_answers(self.customer_id, question_ids))

    def view(self) -> Dict[str, Any]:
        r = self.client.get("/v1/insurance-shopping-cases/%s" % self.case_id, headers=self.headers)
        assert r.status_code == 200, r.text
        return r.json()

    def comparison(self) -> Dict[str, Any]:
        r = self.client.get("/v1/insurance-shopping-cases/%s/comparison" % self.case_id, headers=self.headers)
        assert r.status_code == 200, r.text
        return r.json()

    def quote_id_for(self, insurer_id: str) -> str:
        for q in self.view()["quotes"]:
            if q["insurer_id"] == insurer_id:
                return q["quote_id"]
        raise AssertionError("no quote for %s" % insurer_id)

    def prepare(self, insurer_id: str, expect: int = 201, **body) -> Dict[str, Any]:
        r = self.client.post("/v1/insurance-shopping-cases/%s/applications" % self.case_id, json={"quote_id": self.quote_id_for(insurer_id), **body}, headers=self.headers)
        assert r.status_code == expect, r.text
        return r.json()

    def approve(self, prepared: Dict[str, Any], expect: int = 200, **overrides) -> Dict[str, Any]:
        body = {
            "expected_case_version": prepared["expected_case_version"],
            "action_payload_hash": prepared["action_payload_hash"],
            "approval_challenge_id": prepared["approval_challenge_id"],
            **overrides,
        }
        r = self.client.post("/v1/actions/%s/approve" % prepared["action"]["action_id"], json=body, headers=self.headers)
        assert r.status_code == expect, r.text
        return r.json()

    def run_worker(self) -> Dict[str, Any]:
        r = self.client.post("/v1/operator/worker/run-once", headers=auth("operator"))
        assert r.status_code == 200, r.text
        return r.json()

    def advance(self, **kwargs) -> None:
        r = self.client.post("/v1/operator/clock/advance", json=kwargs, headers=auth("operator"))
        assert r.status_code == 200, r.text

    def inject(self, insurer_id: str, kind: str, operation: Optional[str] = None, **params) -> None:
        r = self.client.post("/v1/operator/faults", json={"insurer_id": insurer_id, "kind": kind, "operation": operation, "params": params}, headers=auth("operator"))
        assert r.status_code == 200, r.text

    def settle(self, max_rounds: int = 6, hours: int = 5) -> Dict[str, Any]:
        """Run worker, advance the clock past underwriting and run again until nothing is due."""
        for _ in range(max_rounds):
            summary = self.run_worker()
            if not summary["jobs"]:
                self.advance(hours=hours)
                summary = self.run_worker()
                if not summary["jobs"]:
                    break
        return self.view()

    def until_awaiting_selection(self) -> Dict[str, Any]:
        self.create()
        self.interview()
        self.request_quotes()
        self.answer_questions()
        view = self.view()
        assert view["status"] == "awaiting_selection", view["status"]
        return view
