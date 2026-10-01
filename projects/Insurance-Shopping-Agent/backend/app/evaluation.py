"""Evaluation driver: runs labeled fixture cases through the rules-only workflow or the agent.

Evaluates state and evidence, not response wording. Used by tests/test_eval.py and
scripts/evaluate.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .agent.llm import ScriptedProvider
from .agent.orchestrator import AgentOrchestrator
from .clock import FixtureClock
from .config import Settings
from .context import AppContext
from .fixtures import household_by_customer
from .persistence import models as m
from .workflows.approvals import ApplicationService
from .workflows.case_service import CaseError, CaseService
from .workflows.worker import Worker

BASE_NEEDS = {
    "state_code": "CA",
    "product": "renters",
    "desired_effective_date": "2026-11-01",
    "property_limit_minor": 3000000,
    "liability_limit_minor": 10000000,
    "replacement_cost_required": True,
}
QUESTION_FACTS = {
    "nw_q_dog": "owns_dog", "nw_q_claims": "prior_claims_5y", "nw_q_smoke": "smoke_detectors", "nw_q_home_business": "home_business",
    "hl_q_animals": "animals_in_household", "hl_q_building": "building_type", "hl_q_claims": "prior_claims_3y",
    "cp_q_high_value": "high_value_items_over_1500", "cp_q_claims": "prior_claims_5y",
}
CITED_FIELDS = {"deductible", "property_limit", "liability_limit", "replacement_cost"}


def household_question_answers(customer_id: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    hh = household_by_customer(customer_id)
    out = {}
    for qid, fact in QUESTION_FACTS.items():
        out[qid] = hh["building_type"] if fact == "building_type" else hh["facts"].get(fact)
    out.update(overrides or {})
    return out


def build_context(tmp_dir: str) -> AppContext:
    clock = FixtureClock(datetime(2026, 10, 1, 9, tzinfo=timezone.utc), frozen=True)
    settings = Settings(database_url="sqlite:///%s/eval.db" % tmp_dir, fixture_clock=True, environment="mock", adapter_mode="direct")
    return AppContext(settings, clock=clock)


class EvalRun:
    def __init__(self, ctx: AppContext, via_agent: bool = False) -> None:
        self.ctx = ctx
        self.cases = CaseService(ctx)
        self.apps = ApplicationService(ctx, self.cases)
        self.worker = Worker(ctx, owner="eval-worker")
        self.via_agent = via_agent
        self.agent = AgentOrchestrator(ctx, ScriptedProvider()) if via_agent else None

    WAITING_ON_CUSTOMER = {"awaiting_approval", "awaiting_selection", "needs_information", "collecting", "completed", "declined", "manual_review", "expired"}

    async def _settle(self, case_id: str, customer_id: str, hours: int = 5, rounds: int = 6) -> None:
        """Run the worker and move the fixture clock while the case waits on a provider."""
        for _ in range(rounds):
            summary = await self.worker.run_once()
            if self.cases.case_view(case_id, customer_id)["status"] in self.WAITING_ON_CUSTOMER:
                return
            if not summary["jobs"]:
                self.ctx.clock.advance(hours=hours)
                summary = await self.worker.run_once()
                if not summary["jobs"]:
                    return

    async def run(self, case: Dict[str, Any]) -> Dict[str, Any]:
        customer_id = case["customer_id"]
        actor = "eval"
        expected = case.get("expected", {})
        observed: Dict[str, Any] = {"id": case["id"], "errors": []}
        hh = household_by_customer(customer_id)

        # 1. create
        try:
            created = self.cases.create_case(customer_id, {**BASE_NEEDS, **case.get("needs_overrides", {})})
        except CaseError as exc:
            observed["create_error"] = str(exc)
            observed["final_state"] = None
            return self._score(case, observed)
        case_id = created["id"]
        observed["case_id"] = case_id

        # 2. interview
        interview = case.get("interview", {})
        answers = []
        if interview.get("address", True):
            answers.append({"field": "address", "value": hh["address"]})
        for field in ("deductible_cap_minor", "required_item_classes", "deductible_preference"):
            if field in interview:
                answers.append({"field": field, "value": interview[field]})
        if answers:
            await self.cases.record_answers(case_id, customer_id, answers, actor)

        # 3. faults
        for fault in case.get("faults", []):
            self.ctx.registry.insurers[fault["insurer_id"]].inject_fault(fault["kind"], fault.get("operation"), **fault.get("params", {}))

        # 4. quotes
        try:
            if self.via_agent:
                turn = await self.agent.handle_message(case_id, customer_id, "Please get quotes.")
                requested = self.cases.case_view(case_id, customer_id)
                if requested["status"] == "collecting":
                    raise CaseError("needs are incomplete", 409, {"missing_fields": requested["missing_fields"]})
            else:
                requested = await self.cases.request_quotes(case_id, customer_id, actor)
        except CaseError as exc:
            observed["quote_request_error"] = str(exc)
            observed["final_state"] = self.cases.case_view(case_id, customer_id)["status"]
            return self._score(case, observed)
        observed["after_quotes_status"] = requested["status"]
        surfaced_before_answers = self.cases.case_view(case_id, customer_id)["outstanding_questions"]

        if case.get("scenario") == "compare_before_retry":
            comparison_early = self.cases.comparison(case_id, customer_id)
            observed["early_missing_responses"] = sorted(mr["insurer_id"] for mr in comparison_early["missing_responses"])

        # 5. question answers (customer statements; unknown stays unknown)
        qa = household_question_answers(customer_id, case.get("question_answers"))
        await self.cases.record_answers(case_id, customer_id, [{"question_id": k, "value": v} for k, v in qa.items()], actor)
        if case.get("scenario") in ("compare_before_retry", "recover_timeouts"):
            self.ctx.clock.advance(minutes=1)
            await self.worker.run_until_idle()
        view = self.cases.case_view(case_id, customer_id)
        observed["contradictions"] = _contradictions(view["confirmed_answers"])

        # 6. comparison
        comparison = self.cases.comparison(case_id, customer_id)
        observed["suitable"] = [s["insurer_id"] for s in comparison["suitable"]]
        observed["excluded"] = sorted(e["insurer_id"] for e in comparison["excluded"])
        observed["undetermined"] = sorted(u["insurer_id"] for u in comparison["undetermined"])
        observed["missing_responses"] = sorted(mr["insurer_id"] for mr in comparison["missing_responses"])
        observed["unsupported_claims"] = _unsupported_claims(comparison)
        observed["questions_surfaced"] = len(surfaced_before_answers)
        observed["unnecessary_questions"] = _unnecessary_questions(surfaced_before_answers, observed["excluded"])

        # 7. selection / application
        select = case.get("select_insurer")
        if select and case.get("scenario") != "expire_quotes":
            quote_id = next((q["quote_id"] for q in view["quotes"] if q["insurer_id"] == select), None)
            if quote_id is None:
                observed["prepare_error"] = "no quote from %s" % select
            else:
                try:
                    if self.via_agent:
                        turn = await self.agent.handle_message(case_id, customer_id, "select %s" % quote_id)
                        v2 = self.cases.case_view(case_id, customer_id)
                        if v2["status"] != "awaiting_approval":
                            raise CaseError(turn["reply"], 409)
                        prepared = {"action": v2["pending_action"], "expected_case_version": v2["version"], "action_payload_hash": v2["pending_action"]["payload_hash"], "approval_challenge_id": v2["pending_action"]["challenge_id"]}
                    else:
                        prepared = self.apps.prepare_application(case_id, customer_id, quote_id, None, actor)
                except CaseError as exc:
                    observed["prepare_error"] = str(exc)
                    prepared = None
                if prepared is not None:
                    await self._approve_and_settle(case, case_id, customer_id, prepared, observed)
        if case.get("scenario") == "expire_quotes":
            self.ctx.clock.advance(days=31)
            await self.worker.run_once()
            observed["expired_state"] = self.cases.case_view(case_id, customer_id)["status"]
            await self.cases.request_quotes(case_id, customer_id, actor)
            await self.cases.record_answers(case_id, customer_id, [{"question_id": "cp_q_high_value", "value": qa["cp_q_high_value"]}], actor)

        final = self.cases.case_view(case_id, customer_id)
        observed["final_state"] = final["status"]
        observed["verified"] = bool(final["policy"] and final["policy"]["verified"])
        observed["policy_premium_minor"] = final["policy"]["declarations"]["annual_premium_minor"] if final["policy"] else None
        with self.ctx.db.session() as session:
            observed["tool_runs"] = session.query(m.ToolRun).filter(m.ToolRun.case_id == case_id).count()
            observed["provider_calls"] = session.query(m.ProviderRequestLog).filter(m.ProviderRequestLog.case_id == case_id).count()
            observed["executed_actions"] = session.query(m.Action).filter(m.Action.case_id == case_id, m.Action.status == "executed").count()
            observed["submit_calls"] = session.query(m.ProviderRequestLog).filter(m.ProviderRequestLog.case_id == case_id, m.ProviderRequestLog.operation == "submit_application").count()
        return self._score(case, observed)

    async def _approve_and_settle(self, case, case_id, customer_id, prepared, observed) -> None:
        action = prepared["action"]
        scenario = case.get("scenario")
        if scenario == "approve_wrong_hash":
            try:
                self.apps.approve_action(action["action_id"], customer_id, prepared["expected_case_version"], "sha256:tampered", prepared["approval_challenge_id"])
            except CaseError as exc:
                observed["approval_error"] = str(exc)
            return
        if scenario == "approve_stale_version":
            try:
                self.apps.approve_action(action["action_id"], customer_id, prepared["expected_case_version"] - 1, prepared["action_payload_hash"], prepared["approval_challenge_id"])
            except CaseError as exc:
                observed["approval_error"] = str(exc)
            return
        self.apps.approve_action(action["action_id"], customer_id, prepared["expected_case_version"], prepared["action_payload_hash"], prepared["approval_challenge_id"])
        await self._settle(case_id, customer_id, hours=case.get("underwriting_hours", 5))
        view = self.cases.case_view(case_id, customer_id)
        observed["revised_offer"] = any(t["event_type"] == "insurance.application.revised_offer" for t in view["timeline"])
        if view["status"] == "awaiting_approval" and view["pending_action"] and view["pending_action"]["type"] == "accept_revised_offer":
            pa = view["pending_action"]
            observed["revised_premium_minor"] = pa["review"]["amount"]["annual_premium_minor"]
            if case.get("accept_revision", True):
                self.apps.approve_action(pa["action_id"], customer_id, view["version"], pa["payload_hash"], pa["challenge_id"])
                await self._settle(case_id, customer_id, hours=1)
            else:
                self.apps.reject_action(pa["action_id"], customer_id, "customer declined revised terms")

    def _score(self, case: Dict[str, Any], observed: Dict[str, Any]) -> Dict[str, Any]:
        expected = case.get("expected", {})
        mismatches = []
        for key, want in expected.items():
            got = observed.get(key)
            if key.endswith("_error") or key == "reply_contains":
                ok = want is None and got is None or (want is not None and got is not None and want in got)
            elif key == "max_unnecessary_questions":
                ok = (observed.get("unnecessary_questions") or 0) <= want
            else:
                ok = got == want
            if not ok:
                mismatches.append({"field": key, "expected": want, "observed": got})
        observed["mismatches"] = mismatches
        observed["passed"] = not mismatches
        observed["category"] = case.get("category")
        observed["held_out"] = case.get("held_out", False)
        return observed


def _contradictions(answers: Dict[str, Dict[str, Any]]) -> int:
    from .agent.tools import detect_contradictions

    return len(detect_contradictions(answers))


def _unsupported_claims(comparison: Dict[str, Any]) -> int:
    count = 0
    for row in comparison["differences"]:
        if row["field"] in CITED_FIELDS or row["field"].startswith("item_class:"):
            for value in row["values"].values():
                if value["citation"] is None and value["value"] != "not_stated":
                    count += 1
    for bucket in ("suitable", "excluded"):
        for entry in comparison[bucket]:
            for check in entry["checks"]:
                if check["result"] == "fail" and check["citation"] is None and check["field"] not in ("quote_validity", "effective_date", "property_limit", "liability_limit", "deductible"):
                    count += 1
    return count


def _unnecessary_questions(surfaced: List[Dict[str, Any]], excluded: List[str]) -> int:
    """Application questions surfaced for insurers whose quote is excluded cannot change the outcome."""
    return sum(1 for q in surfaced if q["reason"] == "required_for_application" and q["insurer_id"] in excluded)


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    by_cat: Dict[str, Dict[str, int]] = {}
    for r in results:
        cat = by_cat.setdefault(r["category"], {"total": 0, "passed": 0})
        cat["total"] += 1
        cat["passed"] += 1 if r["passed"] else 0
    held = [r for r in results if r["held_out"]]
    return {
        "total": total,
        "passed": passed,
        "held_out_total": len(held),
        "held_out_passed": sum(1 for r in held if r["passed"]),
        "by_category": by_cat,
        "unsupported_claims_total": sum(r.get("unsupported_claims", 0) or 0 for r in results),
        "unnecessary_questions_total": sum(r.get("unnecessary_questions", 0) or 0 for r in results),
        "tool_runs_total": sum(r.get("tool_runs", 0) or 0 for r in results),
        "provider_calls_total": sum(r.get("provider_calls", 0) or 0 for r in results),
        "duplicate_submits": sum(max(0, (r.get("submit_calls") or 0) - (r.get("executed_actions") or 0)) for r in results),
        "completed_cases": sum(1 for r in results if r.get("final_state") == "completed"),
    }
