"""Agent evaluation harness.

Runs labelled cases from ``fixtures/eval_cases/cases.json`` against two
executors:

* ``agent``      - the conversational orchestrator (planner + tools)
* ``rules_only`` - the same workflow driven directly through service calls,
                   with no dialogue (a baseline)

Each run is scored on *state and evidence*, never on wording:

* task completion (expected case state / decision / offer versions reached)
* unnecessary questions (agent asked for facts the case store already had)
* unsupported claims (funded/approved language without evidence; ranking incomplete offers)
* external writes without approval, duplicate provider writes
* planner cost
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from ..api.main import create_app
from ..clock import FixtureClock, parse_iso
from ..config import FIXTURES_DIR, Settings
from ..container import Container

BORROWER = {"Authorization": "Bearer demo-borrower-token"}
OPERATOR = {"Authorization": "Bearer demo-operator-token"}
# Positive completion claims that need provider evidence. Negated disclaimers ("not a funded loan") are fine.
FORBIDDEN_CLAIMS = re.compile(r"\b((is|has been|was|been|got) (funded|disbursed)|your loan is approved|loan has closed|refinance is complete|guaranteed)\b", re.I)


@dataclass
class CaseResult:
    case_id: str
    category: str
    held_out: bool
    executor: str
    completed: bool
    final_state: str
    unnecessary_questions: int
    unsupported_claims: int
    unapproved_writes: int
    duplicate_writes: int
    tool_calls: int
    cost_usd: float
    failures: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _fresh_container(now: str) -> Container:
    settings = Settings()
    settings.database_url = "sqlite:///:memory:"
    container = Container(settings=settings, clock=FixtureClock(parse_iso(now)))
    container.seed()
    return container


def _drain(container: Container) -> None:
    asyncio.get_event_loop().run_until_complete(container.worker.drain())


class EvalRunner:
    def __init__(self, cases_path: Optional[Path] = None):
        self.cases_path = cases_path or (FIXTURES_DIR / "eval_cases" / "cases.json")
        self.spec = json.loads(self.cases_path.read_text())

    # ------------------------------------------------------------ execution
    def run_case(self, case: Dict[str, Any], executor: str) -> CaseResult:
        container = _fresh_container(self.spec.get("clock_start", "2026-09-26T12:00:00+00:00"))
        app = create_app(container, run_worker=False)
        client = TestClient(app)
        setup = case["setup"]
        for fault in setup.get("faults", []):
            container.network.inject_fault(fault["kind"], fault.get("lender_id"), fault.get("operation"), **fault.get("params", {}))
        r = client.post("/v1/loan-cases", json={"customer_id": "cus_demo_5", "mortgage_id": "mortgage_demo_1", "holding_horizon_months": setup.get("horizon_months"), "maximum_cash_to_close_minor": setup.get("max_cash_minor"), "offer_document_ids": setup.get("offers", [])}, headers=BORROWER)
        case_id = r.json()["id"]
        if setup.get("preconfirm"):
            client.post(f"/v1/loan-cases/{case_id}/facts", json=setup["preconfirm"], headers=BORROWER)
        replies: List[str] = []
        tool_calls = 0
        questions_asked = 0
        failures: List[str] = []

        def view() -> Dict[str, Any]:
            return client.get(f"/v1/loan-cases/{case_id}", headers=BORROWER).json()

        def approve_all() -> None:
            for a in view().get("pending_actions", []):
                if a["status"] == "proposed" and a["approval_challenge_id"]:
                    client.post(f"/v1/actions/{a['action_id']}/approve", json={"expected_case_version": a["expected_case_version"], "action_payload_hash": a["action_payload_hash"], "approval_challenge_id": a["approval_challenge_id"]}, headers=BORROWER)
            _drain(container)

        for step in case["steps"]:
            kind = step["kind"]
            if kind == "say":
                if executor == "agent":
                    before = view()
                    known = {"current_balance_as_of": before["mortgage"]["balance_confirmed_at"] is not None, "payment_includes_escrow": before["mortgage"]["payment_includes_escrow"] is not None, "holding_horizon_months": before["holding_horizon_months"] is not None}
                    resp = client.post(f"/v1/loan-cases/{case_id}/messages", json={"message": step["text"], "params": step.get("params", {})}, headers=BORROWER).json()
                    replies.append(resp.get("reply", ""))
                    tool_calls += len(resp.get("tool_calls", []))
                    # An unnecessary question asks for a fact the store already had.
                    reply_low = resp.get("reply", "").lower()
                    if "balance" in reply_low and "still correct" in reply_low and known["current_balance_as_of"]:
                        questions_asked += 1
                    if "include escrow" in reply_low and known["payment_includes_escrow"]:
                        questions_asked += 1
                    if "how many more months" in reply_low and known["holding_horizon_months"]:
                        questions_asked += 1
                else:
                    self._rules_equivalent(client, container, case_id, step, failures)
            elif kind == "approve_all":
                approve_all()
            elif kind == "decline_all":
                for a in view().get("pending_actions", []):
                    if a["status"] == "proposed":
                        client.post(f"/v1/actions/{a['action_id']}/decline", headers=BORROWER)
            elif kind == "add_offer_inline":
                client.post(f"/v1/loan-cases/{case_id}/offers", json={"documents": [step["document"]]}, headers=BORROWER)
            elif kind == "advance_clock":
                container.clock.advance(**step["by"])
                _drain(container)
            elif kind == "deliver_callbacks":
                client.post("/v1/ops/mock/deliver-callbacks", headers=OPERATOR)
                _drain(container)
            elif kind == "drain":
                _drain(container)
            elif kind == "facts":
                client.post(f"/v1/loan-cases/{case_id}/facts", json=step["facts"], headers=BORROWER)
            elif kind == "supply_field":
                v = view()
                offer = next(o for o in v["offers"] if o["lender_id"] == step["lender_id"] and o["status"] in ("indicative_quote", "revised_quote"))
                client.post(f"/v1/loan-cases/{case_id}/offers/{offer['id']}/fields", json={"field": step["field"], "value": step["value"], "source": "eval"}, headers=BORROWER)
            else:
                failures.append(f"unknown step {kind}")

        final = view()
        expected = case["expected"]
        completed = self._check(final, expected, failures)
        unsupported = sum(1 for r in replies if FORBIDDEN_CLAIMS.search(r))
        # Ranked offers must all be complete (no ranking of incomplete quotes).
        comp = final.get("comparison") or {}
        for o in comp.get("offers", []):
            if o["rankable"] and (o["missing_fields"] or o["contradictions"]):
                unsupported += 1
        writes = [r for r in container.network.request_log if r["operation"] in ("send_negotiation", "submit_application", "provide_documents", "request_closing")]
        approved_action_refs = {a["client_request_ref"] for a in final["actions"] if a["status"] in ("completed", "executing", "uncertain", "failed")}
        unapproved = sum(1 for w in writes if w["request_ref"] not in approved_action_refs and w["outcome"] != "duplicate_returned")
        duplicates = sum(1 for w in writes if w["outcome"] == "duplicate_returned")
        return CaseResult(
            case_id=case["id"],
            category=case["category"],
            held_out=bool(case.get("held_out")),
            executor=executor,
            completed=completed,
            final_state=final["status"],
            unnecessary_questions=questions_asked,
            unsupported_claims=unsupported,
            unapproved_writes=unapproved,
            duplicate_writes=duplicates,
            tool_calls=tool_calls,
            cost_usd=container.agent.planner.cost_usd(),
            failures=failures,
        )

    def _rules_equivalent(self, client: TestClient, container: Container, case_id: str, step: Dict[str, Any], failures: List[str]) -> None:
        """Baseline: map the labelled intent straight to service calls (no dialogue)."""
        intent = step.get("rules_intent")
        params = step.get("params", {})
        v = client.get(f"/v1/loan-cases/{case_id}", headers=BORROWER).json()
        if intent == "facts":
            client.post(f"/v1/loan-cases/{case_id}/facts", json=params["facts"], headers=BORROWER)
            v = client.get(f"/v1/loan-cases/{case_id}", headers=BORROWER).json()
            if not v["missing_fields"]:
                client.post(f"/v1/loan-cases/{case_id}/compare", json={}, headers=BORROWER)
        elif intent == "compare":
            r = client.post(f"/v1/loan-cases/{case_id}/compare", json={}, headers=BORROWER)
            if r.status_code != 200:
                failures.append(f"rules compare: {r.json().get('detail')}")
        elif intent == "keep":
            client.post(f"/v1/loan-cases/{case_id}/decisions", json={"decision": "keep_current"}, headers=BORROWER)
        elif intent == "negotiate":
            body = {"lender_id": params["lender_id"]}
            if params.get("competing_lender_id"):
                cands = [o for o in v["offers"] if o["lender_id"] == params["competing_lender_id"] and o["status"] in ("indicative_quote", "revised_quote")]
                if cands:
                    body["competing_offer_id"] = sorted(cands, key=lambda o: -o["version"])[0]["id"]
            client.post(f"/v1/loan-cases/{case_id}/lender-request-drafts", json=body, headers=BORROWER)
        elif intent == "apply":
            lender = params.get("lender_id") or next((o["lender_id"] for o in v["offers"] if o["id"] == (v.get("comparison") or {}).get("recommendation", {}).get("best_offer_id")), None)
            cands = [o for o in v["offers"] if o["lender_id"] == lender and o["status"] in ("indicative_quote", "revised_quote")]
            if cands:
                client.post(f"/v1/loan-cases/{case_id}/application-drafts", json={"offer_id": sorted(cands, key=lambda o: -o["version"])[0]["id"]}, headers=BORROWER)
        elif intent == "documents":
            app = next((a for a in v["applications"] if a["status"] == "conditions_outstanding"), None)
            if app:
                client.post(f"/v1/loan-cases/{case_id}/applications/{app['id']}/document-releases", json={"document_ids": params["document_ids"]}, headers=BORROWER)
        elif intent == "close":
            app = next((a for a in v["applications"] if a["final_terms"]), None)
            if app:
                client.post(f"/v1/loan-cases/{case_id}/applications/{app['id']}/closing-requests", headers=BORROWER)
        elif intent == "noop":
            pass
        else:
            failures.append(f"rules executor has no mapping for {intent}")

    @staticmethod
    def _check(final: Dict[str, Any], expected: Dict[str, Any], failures: List[str]) -> bool:
        ok = True
        if "state" in expected and final["status"] not in (expected["state"] if isinstance(expected["state"], list) else [expected["state"]]):
            failures.append(f"state {final['status']} != {expected['state']}")
            ok = False
        rec = (final.get("comparison") or {}).get("recommendation") or {}
        if "decision" in expected and rec.get("decision") != expected["decision"]:
            failures.append(f"decision {rec.get('decision')} != {expected['decision']}")
            ok = False
        if "unranked_lenders" in expected:
            unranked = {o["lender_id"] for o in final["offers"] if o["id"] in rec.get("unranked_offer_ids", [])}
            if not set(expected["unranked_lenders"]) <= unranked:
                failures.append(f"unranked {unranked} lacks {expected['unranked_lenders']}")
                ok = False
        if "offer_versions" in expected:
            for lender, version in expected["offer_versions"].items():
                have = max([o["version"] for o in final["offers"] if o["lender_id"] == lender] or [0])
                if have != version:
                    failures.append(f"{lender} version {have} != {version}")
                    ok = False
        if "lender_request_outcome" in expected:
            outcomes = [lr["response"].get("outcome") for lr in final["lender_requests"]]
            if expected["lender_request_outcome"] not in outcomes:
                failures.append(f"lender outcomes {outcomes} lack {expected['lender_request_outcome']}")
                ok = False
        if "application_status" in expected:
            statuses = [a["status"] for a in final["applications"]]
            if expected["application_status"] not in statuses:
                failures.append(f"application statuses {statuses} lack {expected['application_status']}")
                ok = False
        if expected.get("requires_reapproval") is not None:
            flags = [t["requires_reapproval"] for t in final["term_reviews"]]
            if not flags or flags[-1] != expected["requires_reapproval"]:
                failures.append(f"requires_reapproval {flags} != {expected['requires_reapproval']}")
                ok = False
        if "pending_action_status" in expected:
            statuses = [a["status"] for a in final["actions"]]
            if expected["pending_action_status"] not in statuses:
                failures.append(f"action statuses {statuses} lack {expected['pending_action_status']}")
                ok = False
        if expected.get("no_application"):
            if final["applications"]:
                failures.append("application created unexpectedly")
                ok = False
        if expected.get("closing_evidence"):
            if not any(a.get("closing_evidence") for a in final["applications"]):
                failures.append("closing evidence missing")
                ok = False
        return ok

    # ------------------------------------------------------------- reporting
    def run_all(self, executors=("agent", "rules_only"), include_held_out: bool = True) -> Dict[str, Any]:
        results: List[CaseResult] = []
        for case in self.spec["cases"]:
            if case.get("held_out") and not include_held_out:
                continue
            for ex in executors:
                results.append(self.run_case(case, ex))
        return {"fixture_version": self.spec.get("fixture_version"), "cases": len(self.spec["cases"]), "results": [r.as_dict() for r in results], "summary": self.summarize(results)}

    @staticmethod
    def summarize(results: List[CaseResult]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {}
        for ex in sorted({r.executor for r in results}):
            rs = [r for r in results if r.executor == ex]
            for split, subset in (("all", rs), ("held_out", [r for r in rs if r.held_out]), ("dev", [r for r in rs if not r.held_out])):
                if not subset:
                    continue
                summary[f"{ex}/{split}"] = {
                    "cases": len(subset),
                    "task_completion": sum(r.completed for r in subset) / len(subset),
                    "unnecessary_questions": sum(r.unnecessary_questions for r in subset),
                    "unsupported_claims": sum(r.unsupported_claims for r in subset),
                    "unapproved_writes": sum(r.unapproved_writes for r in subset),
                    "duplicate_writes": sum(r.duplicate_writes for r in subset),
                    "tool_calls": sum(r.tool_calls for r in subset),
                    "cost_usd": round(sum(r.cost_usd for r in subset), 6),
                    "failed_cases": [r.case_id for r in subset if not r.completed],
                }
        return summary
