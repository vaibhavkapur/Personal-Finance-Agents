from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.clock import Clock, parse_iso  # noqa: E402
from app.config import Settings  # noqa: E402
from app.context import AppContext  # noqa: E402
from app.workflows.approvals import PrincipalView  # noqa: E402

FROZEN_NOW = "2026-09-14T10:00:00Z"
LOSS_AT = "2026-09-10T14:00:00Z"
POLICY = "travel_policy_demo_1"

CUSTOMER = PrincipalView("prn_customer_demo_3", "customer", "cus_demo_3", "Jordan Rivera")
OTHER_CUSTOMER = PrincipalView("prn_customer_demo_4", "customer", "cus_demo_4", "Priya Natarajan")
REPRESENTATIVE = PrincipalView("prn_rep_demo_3", "representative", "cus_demo_3", "Representative")
OPERATOR = PrincipalView("prn_operator_demo", "operator", None, "Operations reviewer")

COMPLETE_DOCS = ["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3_early", "receipt_demo_4"]


def make_ctx(adapter: str = "mock", now: str = FROZEN_NOW, **overrides) -> AppContext:
    settings = Settings(database_url="sqlite:///:memory:", claims_adapter=adapter, **overrides)
    return AppContext(settings, Clock(parse_iso(now)))


@pytest.fixture
def ctx() -> AppContext:
    return make_ctx()


def run(coro):
    return asyncio.run(coro)


class Flow:
    """Small driver around the service layer for tests."""

    def __init__(self, ctx: AppContext, principal: PrincipalView = CUSTOMER):
        self.ctx = ctx
        self.p = principal
        self.case_id: Optional[str] = None

    def open(self, docs: List[str], loss_at: str = LOSS_AT, scenario: Optional[str] = None) -> Dict[str, Any]:
        res = self.ctx.cases.create_case(self.p, customer_id=self.p.customer_id, policy_id=POLICY, loss_type="baggage_delay", loss_at=loss_at, document_ids=docs, mock_scenario=scenario)
        self.case_id = res["id"]
        return res

    def view(self) -> Dict[str, Any]:
        return self.ctx.cases.get_case(self.p, self.case_id)

    def status(self) -> str:
        return self.view()["case"]["status"]

    def answer(self, field: str, answer: Dict[str, Any]) -> Dict[str, Any]:
        for q in self.view()["open_questions"]:
            if q["field"] == field:
                return self.ctx.cases.answer_question(self.p, self.case_id, q["id"], answer)
        raise AssertionError(f"no open question for field {field}")

    def draft(self) -> Dict[str, Any]:
        return self.ctx.cases.create_submission_draft(self.p, self.case_id)

    def appeal_draft(self) -> Dict[str, Any]:
        return self.ctx.cases.create_appeal_draft(self.p, self.case_id)

    def approve(self, draft: Dict[str, Any], principal: Optional[PrincipalView] = None) -> Dict[str, Any]:
        return self.ctx.cases.approve_action(principal or self.p, draft["action_id"], expected_case_version=draft["expected_case_version"], action_payload_hash=draft["content_hash"], approval_challenge_id=draft["approval_challenge_id"])

    def work(self, worker=None) -> List[Dict[str, Any]]:
        return run((worker or self.ctx.worker).run_until_idle())

    def submit(self) -> Dict[str, Any]:
        d = self.draft()
        self.approve(d)
        self.work()
        return d

    def pay(self, mode: str = "exact") -> List[Dict[str, Any]]:
        v = self.view()
        decision = v["decisions"][-1]
        events = self.ctx.payment_feed.generate(claim_reference=v["case"]["external_claim_ref"], payee_id=self.p.customer_id, approved_minor=decision["accepted_minor"], currency=decision["currency"], mode=mode)
        return [self.ctx.events.handle_payment_event(ev["payload"], ev["signature"]) for ev in events]

    def agent(self, message: str = "") -> Dict[str, Any]:
        return self.ctx.agent.run_turn(self.p, self.case_id, message)
