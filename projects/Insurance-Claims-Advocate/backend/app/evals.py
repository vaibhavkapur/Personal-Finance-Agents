"""Agent evaluation harness.

Runs labeled fixture cases through (a) the agent (planner + tools) and (b) a rules-only workflow that calls the
application services directly. Both share the same simulated customer and mock insurer. Evaluation looks at state and
evidence (final status, questions asked, provenance, submissions, amounts), not at response wording.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from .clock import Clock, parse_iso
from .config import FIXTURES_DIR, Settings
from .context import AppContext
from .domain.packet import verify_provenance
from .persistence.models import InboxEvent, Packet, ProviderRequestLog
from .workflows.approvals import PrincipalView
from .workflows.errors import DomainError, Forbidden, Invalid

CUSTOMER = PrincipalView("prn_customer_demo_3", "customer", "cus_demo_3", "Jordan Rivera")
TERMINAL = {"closed", "closed_unpaid"}
MAX_ITERATIONS = 16


def load_cases(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    data = json.loads((path or FIXTURES_DIR / "eval_cases.json").read_text())
    return data["cases"]


class CaseRunner:
    def __init__(self, case: Dict[str, Any], mode: str):
        self.case = case
        self.mode = mode
        settings = Settings(database_url="sqlite:///:memory:")
        self.ctx = AppContext(settings, Clock(parse_iso(case["now"])))
        for fault in case.get("faults", []):
            self.ctx.mock_insurer.inject_fault(fault)
        if case.get("chaos"):
            self.ctx.mock_insurer.set_chaos(**case["chaos"])
        if case.get("advance_hours_after_submit"):
            self.ctx.mock_insurer.set_callback_delay(hours=48)
        self.case_id: Optional[str] = None
        self.asked_fields: List[str] = []
        self.steps = 0
        self.tool_calls = 0
        self.later_attached = False
        self.paid = False
        self.advanced = False
        self.first_decision: Optional[str] = None
        self.status_after_submit: Optional[str] = None
        self.status_before_advance: Optional[str] = None
        self.escalation: Optional[str] = None
        self.error: Optional[str] = None
        self.appeal_challenged_minor: Optional[int] = None

    # ------------------------------------------------------------------ helpers
    def view(self) -> Dict[str, Any]:
        return self.ctx.cases.get_case(CUSTOMER, self.case_id)

    def work(self) -> None:
        asyncio.run(self.ctx.worker.run_until_idle())

    def approve(self, action: Dict[str, Any]) -> None:
        self.ctx.cases.approve_action(CUSTOMER, action["action_id"], expected_case_version=action["expected_case_version"], action_payload_hash=action["content_hash"], approval_challenge_id=action["approval_challenge_id"])
        self.work()

    # ------------------------------------------------------------------ run
    def run(self) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            res = self.ctx.cases.create_case(CUSTOMER, customer_id="cus_demo_3", policy_id="travel_policy_demo_1", loss_type="baggage_delay", loss_at=self.case["loss_at"], document_ids=self.case["documents"], mock_scenario=self.case.get("scenario"))
            self.case_id = res["id"]
        except Forbidden as exc:
            self.error = "forbidden"
            return self._result(started, error=str(exc))
        last = None
        stale_rounds = 0
        for _ in range(MAX_ITERATIONS):
            view = self.view()
            status = view["case"]["status"]
            if status in TERMINAL:
                break
            self._agent_or_rules_step(view)
            progressed = self._environment_reacts()
            view = self.view()
            marker = (view["case"]["status"], view["case"]["version"])
            if marker == last and not progressed:
                stale_rounds += 1
                if stale_rounds >= 2:
                    break
            else:
                stale_rounds = 0
            last = marker
        return self._result(started)

    def _agent_or_rules_step(self, view: Dict[str, Any]) -> None:
        self.steps += 1
        status = view["case"]["status"]
        if self.mode == "agent":
            turn = self.ctx.agent.run_turn(CUSTOMER, self.case_id, self.case.get("customer_message", "My bag arrived two days late. Help me claim the essential purchases."))
            self.tool_calls += turn["tool_calls"]
            if turn["data"].get("escalation"):
                self.escalation = turn["data"]["escalation"]
            return
        # rules-only workflow
        try:
            self.tool_calls += 1
            if status in ("collecting", "evaluating"):
                ev = self.ctx.cases.evaluate(CUSTOMER, self.case_id)
                if ev["evaluation"].get("unsupported_conditions"):
                    self.escalation = "unsupported_conditions"
                    return
                if ev["evaluation"]["ready_for_packet"] and not ev["open_questions"]:
                    self.tool_calls += 1
                    self.ctx.cases.create_submission_draft(CUSTOMER, self.case_id)
            elif status == "evidence_requested":
                reqs = [r for r in view["insurer_requests"] if r["status"] == "open"]
                if reqs and any(d["doc_type"] == reqs[-1]["requirement"]["document_type"] for d in view["documents"]):
                    self.ctx.cases.create_submission_draft(CUSTOMER, self.case_id)
            elif status in ("partially_approved", "denied"):
                try:
                    self.ctx.cases.create_appeal_draft(CUSTOMER, self.case_id)
                except Invalid:
                    if self.case.get("accept_decision"):
                        self.ctx.cases.accept_decision(CUSTOMER, self.case_id)
            elif status == "payout_pending":
                self.ctx.cases.reconcile(CUSTOMER, self.case_id)
        except DomainError:
            pass

    def _environment_reacts(self) -> bool:
        """The simulated customer, worker and payment feed respond to the current case state."""
        progressed = False
        view = self.view()
        status = view["case"]["status"]
        answers = self.case.get("customer_answers", {})
        for q in view["open_questions"]:
            if q["field"] not in self.asked_fields:
                self.asked_fields.append(q["field"])
            if q["field"] in answers:
                self.ctx.cases.answer_question(CUSTOMER, self.case_id, q["id"], answers[q["field"]])
                progressed = True
        view = self.view()
        status = view["case"]["status"]
        pending = view.get("pending_action")
        if pending and pending["status"] == "proposed":
            if pending["action_type"] == "submit_appeal":
                self.appeal_challenged_minor = pending["review_summary"]["requested_total_minor"]
            if self.case.get("auto_approve", True):
                before_subs = len(view["submissions"])
                self.approve(pending)
                progressed = True
                view = self.view()
                if before_subs == 0 and view["submissions"]:
                    self.status_after_submit = view["case"]["status"]
        status = view["case"]["status"]
        if status == "submitted" and self.case.get("advance_hours_after_submit") and not self.advanced:
            self.status_before_advance = status
            self.ctx.clock.advance(hours=self.case["advance_hours_after_submit"])
            self.advanced = True
            self.work()
            progressed = True
            view = self.view()
            status = view["case"]["status"]
        if view["decisions"] and self.first_decision is None:
            self.first_decision = view["decisions"][0]["outcome"]
        if status == "evidence_requested" and self.case.get("later_documents") and not self.later_attached:
            self.ctx.cases.attach_documents(CUSTOMER, self.case_id, self.case["later_documents"])
            self.later_attached = True
            progressed = True
        if status in ("partially_approved", "denied") and self.mode == "agent" and self.case.get("accept_decision"):
            decision = view["decisions"][-1]
            if not (decision.get("explanation") or {}).get("has_supported_challenge") or decision.get("appeal_status") == "unsupported":
                self.ctx.cases.accept_decision(CUSTOMER, self.case_id)
                progressed = True
        if status == "payout_pending" and self.case.get("payment_mode") and not self.paid:
            decision = view["decisions"][-1]
            events = self.ctx.payment_feed.generate(claim_reference=view["case"]["external_claim_ref"], payee_id="cus_demo_3", approved_minor=decision["accepted_minor"], currency=decision["currency"], mode=self.case["payment_mode"])
            for ev in events:
                self.ctx.events.handle_payment_event(ev["payload"], ev["signature"])
            self.paid = True
            progressed = True
        return progressed

    # ------------------------------------------------------------------ result + comparison
    def _result(self, started: float, error: Optional[str] = None) -> Dict[str, Any]:
        expected = self.case["expected"]
        observed: Dict[str, Any] = {"error": self.error}
        mismatches: List[str] = []
        unsupported_statements = 0
        if self.case_id:
            view = self.view()
            with self.ctx.db.session() as s:
                packets = s.scalars(select(Packet).where(Packet.case_id == self.case_id)).all()
                for p in packets:
                    unsupported_statements += len(verify_provenance(p.content_json))
                adapter_outcomes = [r.outcome for r in s.scalars(select(ProviderRequestLog).where(ProviderRequestLog.case_id == self.case_id).order_by(ProviderRequestLog.created_at)).all()]
                duplicate_events = sum(int(r.duplicate_deliveries or 0) for r in s.scalars(select(InboxEvent)).all())
            settlement = view.get("settlement") or {}
            latest = view["decisions"][-1] if view["decisions"] else None
            observed.update({
                "final_status": view["case"]["status"],
                "policy_version": view["case"]["policy_version"],
                "questions": sorted(set(self.asked_fields)),
                "supported_minor": view["totals"]["supported_minor"],
                "excluded_minor": view["totals"]["excluded_minor"],
                "duplicate_minor": view["totals"]["duplicate_minor"],
                "estimated_payable_minor": view["totals"]["estimated_payable_minor"],
                "accepted_minor": latest["accepted_minor"] if latest else 0,
                "paid_minor": settlement.get("paid_minor", 0),
                "outstanding_minor": settlement.get("outstanding_minor", 0),
                "submissions": len(view["submissions"]),
                "insurer_claims": len(self.ctx.mock_insurer.snapshot()),
                "decisions": len(view["decisions"]),
                "open_requests": sum(1 for r in view["insurer_requests"] if r["status"] == "open"),
                "first_decision": self.first_decision,
                "first_case_status_after_submit": self.status_after_submit,
                "status_before_clock_advance": self.status_before_advance,
                "escalation": self.escalation,
                "pending_action": (view.get("pending_action") or {}).get("status"),
                "payout_last4": view["claimant"]["payout_destination"].get("account_last4"),
                "adapter_outcomes": adapter_outcomes,
                "duplicate_events": duplicate_events,
                "appeal_challenged_minor": self.appeal_challenged_minor,
                "unsupported_statements": unsupported_statements,
            })
        for key, want in expected.items():
            got = observed.get(key)
            if key == "questions":
                got = sorted(set(got or []))
                want = sorted(set(want))
            if got != want:
                mismatches.append(f"{key}: expected {want!r}, got {got!r}")
        expected_q = set(expected.get("questions", []))
        asked = set(self.asked_fields)
        return {
            "id": self.case["id"],
            "category": self.case["category"],
            "held_out": self.case.get("held_out", False),
            "mode": self.mode,
            "passed": not mismatches and unsupported_statements == 0,
            "mismatches": mismatches,
            "unnecessary_questions": sorted(asked - expected_q) if "questions" in expected else [],
            "missed_questions": sorted(expected_q - asked),
            "unsupported_statements": unsupported_statements,
            "tool_calls": self.tool_calls,
            "steps": self.steps,
            "seconds": round(time.perf_counter() - started, 3),
            "observed": observed,
        }


def run_all(cases: Optional[List[Dict[str, Any]]] = None, modes=("agent", "rules")) -> Dict[str, Any]:
    cases = cases or load_cases()
    results: List[Dict[str, Any]] = []
    for case in cases:
        for mode in modes:
            results.append(CaseRunner(case, mode).run())
    summary: Dict[str, Any] = {"fixture_version": cases[0].get("fixture_version") if cases else None, "cases": len(cases), "modes": {}}
    for mode in modes:
        rows = [r for r in results if r["mode"] == mode]
        held = [r for r in rows if r["held_out"]]
        summary["modes"][mode] = {
            "passed": sum(1 for r in rows if r["passed"]),
            "total": len(rows),
            "held_out_passed": sum(1 for r in held if r["passed"]),
            "held_out_total": len(held),
            "unnecessary_questions": sum(len(r["unnecessary_questions"]) for r in rows),
            "missed_questions": sum(len(r["missed_questions"]) for r in rows),
            "unsupported_statements": sum(r["unsupported_statements"] for r in rows),
            "tool_calls_total": sum(r["tool_calls"] for r in rows),
            "tool_calls_per_case": round(sum(r["tool_calls"] for r in rows) / max(len(rows), 1), 2),
            "seconds_total": round(sum(r["seconds"] for r in rows), 2),
            "by_category": {},
        }
        for cat in sorted({r["category"] for r in rows}):
            cat_rows = [r for r in rows if r["category"] == cat]
            summary["modes"][mode]["by_category"][cat] = {"passed": sum(1 for r in cat_rows if r["passed"]), "total": len(cat_rows)}
    return {"summary": summary, "results": results}
