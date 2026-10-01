"""Evaluation harness for labeled cases (fixtures/eval_cases.json).

Each spec builds an isolated environment (fresh in-memory database, fixture
clock), scripts a timeline of steps, and compares the resulting *state and
evidence* — not wording — against labels. The same steps run against the
agent (planner + tools) and a rules-only pipeline (reconcile + drafts without
a planner) so completion, unnecessary questions, unsupported claims and cost
can be compared.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent.orchestrator import Orchestrator, RECOVERY_CLAIM_PATTERNS
from .clock import format_ts, parse_ts
from .config import FIXTURES_DIR
from .container import Container, build_container
from .domain.errors import DomainError
from .domain.models import Customer, Direction, Document, Merchant, PaymentInstrument, PurchaseRecord, RefundPromise, Transaction, TransactionKind

PURCHASED_AT = "2026-08-15T18:00:00Z"
PROMISED_AT = "2026-09-01T10:15:00Z"
CUSTOMER_ID = "cus_demo_4"
INSTRUMENT = "pi_visa_4242"
OTHER_INSTRUMENT = "pi_amex_0005"
MERCHANT = "mrc_mock_streaming"
OTHER_MERCHANT = "mrc_mock_gadgets"
UNVERIFIED_MERCHANT = "mrc_mock_unverified"


def load_specs(path: Path = FIXTURES_DIR / "eval_cases.json") -> List[Dict[str, Any]]:
    return json.loads(path.read_text())["cases"]


def seed_reference_data(c: Container) -> None:
    data = json.loads((c.fixtures_dir / "customer.json").read_text())
    with c.db.transaction():
        c.repos.upsert_customer(Customer(**data["customer"]))
        for pi in data["payment_instruments"]:
            c.repos.upsert_instrument(PaymentInstrument(**pi))
        c.repos.upsert_instrument(PaymentInstrument(id=OTHER_INSTRUMENT, customer_id=CUSTOMER_ID, network="amex", last4="0005", issuer_id="issuer_mock"))
        for m in data["merchants"]:
            c.repos.upsert_merchant(Merchant(**m))


def build_environment(spec: Dict[str, Any]) -> Dict[str, Any]:
    c = build_container(database_path=":memory:")
    seed_reference_data(c)
    order_ref = f"order_{spec['id']}"
    merchant_id = {"same": MERCHANT, "unverified": UNVERIFIED_MERCHANT}.get(spec.get("merchant", "same"), MERCHANT)
    purchase = PurchaseRecord(id=f"pur_{spec['id']}", customer_id=CUSTOMER_ID, merchant_id=merchant_id, order_ref=order_ref, original_transaction_id=f"txn_{spec['id']}_pur",
                              amount_minor=spec["purchase_minor"], currency=spec.get("currency", "USD"), payment_instrument_ref=INSTRUMENT, purchased_at=PURCHASED_AT)
    evidence_ids: List[str] = []
    with c.db.transaction():
        c.repos.add_purchase(purchase)
        c.repos.add_transaction(Transaction(id=purchase.original_transaction_id, customer_id=CUSTOMER_ID, payment_instrument_ref=INSTRUMENT, merchant_id=merchant_id, direction=Direction.debit,
                                            kind=TransactionKind.purchase, amount_minor=purchase.amount_minor, currency=purchase.currency, posted_at=PURCHASED_AT, description="PURCHASE", provider_ref=f"auth_{spec['id']}", source="statement_mock"))
        receipt = Document(id=f"doc_{spec['id']}_receipt", customer_id=CUSTOMER_ID, kind="receipt", object_key="x", content_hash=f"sha256:{spec['id']}r", source="email_import", captured_at=PURCHASED_AT, extraction_version="extract-v1",
                           extracted={"order_ref": order_ref, "amount_minor": purchase.amount_minor})
        c.repos.add_document(receipt)
        evidence_ids.append(receipt.id)
        if spec.get("promise"):
            p = spec["promise"]
            doc = Document(id=f"doc_{spec['id']}_promise", customer_id=CUSTOMER_ID, kind="cancellation_confirmation", object_key="x", content_hash=f"sha256:{spec['id']}p", source="email_import", captured_at=PROMISED_AT, extraction_version="extract-v1",
                           extracted={"order_ref": order_ref, "promised_minor": p["minor"]})
            c.repos.add_document(doc)
            evidence_ids.append(doc.id)
            c.repos.add_promise(RefundPromise(id=f"prm_{spec['id']}", purchase_id=purchase.id, promised_minor=p["minor"], currency=purchase.currency, destination_type="original_payment", promised_by="merchant email",
                                              promised_at=PROMISED_AT, expected_by=None, evidence_id=doc.id, provider_refund_ref=p.get("refund_ref"), verified_at=PROMISED_AT if p.get("verified", True) else None))
        if spec.get("other_order_claim_ref"):
            other = PurchaseRecord(id=f"pur_{spec['id']}_other", customer_id=CUSTOMER_ID, merchant_id=MERCHANT, order_ref=f"order_{spec['id']}_other", original_transaction_id=f"txn_{spec['id']}_other_pur",
                                   amount_minor=spec["purchase_minor"], currency=purchase.currency, payment_instrument_ref=INSTRUMENT, purchased_at=PURCHASED_AT)
            c.repos.add_purchase(other)
            c.repos.add_promise(RefundPromise(id=f"prm_{spec['id']}_other", purchase_id=other.id, promised_minor=other.amount_minor, currency=other.currency, destination_type="original_payment", promised_by="merchant",
                                              promised_at=PROMISED_AT, provider_refund_ref=spec["other_order_claim_ref"], verified_at=PROMISED_AT))
        for i, cr in enumerate(spec.get("credits", []), start=1):
            posted = parse_ts(PROMISED_AT) + timedelta(days=cr.get("days_after_promise", 3))
            c.repos.add_transaction(Transaction(
                id=f"txn_{spec['id']}_c{i}", customer_id=CUSTOMER_ID, payment_instrument_ref=OTHER_INSTRUMENT if cr.get("instrument") == "other" else INSTRUMENT,
                merchant_id=OTHER_MERCHANT if cr.get("merchant") == "other" else merchant_id, direction=Direction.credit, kind=TransactionKind(cr.get("kind", "refund")),
                amount_minor=cr["minor"], currency=cr.get("currency", purchase.currency), posted_at=format_ts(posted), description="CREDIT", provider_ref=cr.get("ref") or f"cr_{spec['id']}_{i}", source="statement_mock"))
    if spec.get("merchant_scenario"):
        c.merchant_mock.scenarios[order_ref] = spec["merchant_scenario"]
    if spec.get("issuer_scenario"):
        c.issuer_mock.scenarios[order_ref] = spec["issuer_scenario"]
    case = c.service.create_case(customer_id=CUSTOMER_ID, order_ref=order_ref, reason_code=spec.get("reason_code", "promised_refund_missing"), target_minor=spec["target_minor"],
                                 currency=purchase.currency, evidence_ids=evidence_ids, actor=CUSTOMER_ID)
    return {"container": c, "case_id": case.id, "order_ref": order_ref}


class Runner:
    def __init__(self, mode: str) -> None:
        assert mode in ("agent", "rules_only")
        self.mode = mode

    def run(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        env = build_environment(spec)
        c: Container = env["container"]
        case_id = env["case_id"]
        orchestrator = Orchestrator(c.service)
        metrics = {"tool_calls": 0, "questions_asked": 0, "guardrail_blocks": 0, "claims_in_output": 0, "escalations": 0, "turns": 0, "mismatch_explained": False}
        last_draft: Optional[Dict[str, Any]] = None
        last_question: Optional[str] = None
        last_escalation: Optional[Dict[str, Any]] = None
        for step in spec.get("steps", ["agent"]):
            if step == "agent":
                metrics["turns"] += 1
                if self.mode == "agent":
                    turn = orchestrator.run(case_id=case_id, customer_id=CUSTOMER_ID)
                    metrics["tool_calls"] += len(turn.tool_calls)
                    metrics["questions_asked"] += len(turn.questions)
                    metrics["guardrail_blocks"] += 1 if turn.guardrail.get("blocked") else 0
                    metrics["claims_in_output"] += sum(1 for p in RECOVERY_CLAIM_PATTERNS if p.search(turn.summary)) if turn.status not in ("recovered", "already_refunded") else 0
                    metrics["escalations"] += 1 if turn.escalation else 0
                    metrics["mismatch_explained"] = metrics["mismatch_explained"] or ("no matching credit has posted" in turn.summary)
                    last_draft = turn.proposed_action
                    last_question = turn.questions[0]["kind"] if turn.questions else None
                    last_escalation = turn.escalation
                else:
                    result = self._rules_only_turn(c, case_id)
                    metrics["tool_calls"] += result["calls"]
                    metrics["questions_asked"] += 1 if result["question"] else 0
                    metrics["escalations"] += 1 if result["escalation"] else 0
                    last_draft = result["draft"] or last_draft
                    last_question = result["question"]
                    last_escalation = result["escalation"]
            elif step == "approve":
                if last_draft and last_draft.get("status") == "awaiting_approval":
                    c.service.approve_action(last_draft["action_id"], approver_id=CUSTOMER_ID, customer_id=CUSTOMER_ID, expected_case_version=last_draft["expected_case_version"],
                                             action_payload_hash=last_draft["payload_hash"], approval_challenge_id=last_draft["approval_challenge_id"])
                    c.worker.run_until_idle_sync()
                    last_draft = None
            elif step.startswith("advance:"):
                c.advance(days=float(step.split(":", 1)[1]))
            elif step.startswith("advance_minutes:"):
                c.advance(minutes=float(step.split(":", 1)[1]))
            elif step.startswith("answer:"):
                c.service.answer_question(case_id, json.loads(step.split(":", 1)[1]), actor=CUSTOMER_ID, customer_id=CUSTOMER_ID)
            elif step == "duplicates:on":
                c.pump.duplicate_deliveries = True
            else:
                raise ValueError(f"unknown step {step}")
        status = c.service.get_status(case_id)
        observed = {
            "status": status["status"],
            "final_minor": status["amounts"]["final_recovered_minor"],
            "outstanding_minor": status["amounts"]["outstanding_minor"],
            "provisional_minor": status["amounts"]["provisional_minor"],
            "store_credit_minor": status["amounts"]["store_credit_minor"],
            "reversed_minor": status["amounts"]["reversed_minor"],
            "overlap_flagged": status["amounts"]["overlap_flagged"],
            "question_kind": (status["pending_question"] or {}).get("kind") or last_question,
            "proposed_action": last_draft["type"] if last_draft else None,
            "proposed_amount_minor": (last_draft or {}).get("review", {}).get("amount_minor") if last_draft else None,
            "match_count": len([m for m in c.repos.matches_for_case(case_id) if m.reversed_at is None]),
            "provider_case_count": c.merchant_mock.case_count(),
            "escalation_reason": (last_escalation or {}).get("reason"),
            "recovery_claimed": status["status"] in ("recovered", "already_refunded"),
        }
        expected = spec["expected"]
        diffs = {k: {"expected": v, "observed": observed.get(k)} for k, v in expected.items() if k != "recovery_claim_allowed" and observed.get(k) != v}
        if not expected.get("recovery_claim_allowed", False) and observed["recovery_claimed"]:
            diffs["recovery_claim_allowed"] = {"expected": False, "observed": True}
        return {"id": spec["id"], "split": spec.get("split", "dev"), "family": spec["family"], "mode": self.mode, "passed": not diffs, "diffs": diffs, "observed": observed, "metrics": metrics,
                "unnecessary_question": bool(last_question) and expected.get("question_kind") is None}

    def _rules_only_turn(self, c: Container, case_id: str) -> Dict[str, Any]:
        """Fixed pipeline without a planner: reconcile, then draft whatever next_step names."""
        calls = 1
        report = c.service.reconcile(case_id, actor="rules_only")
        draft = None
        escalation = None
        question = (report.get("pending_question") or {}).get("kind")
        if report["next_step"] == "provide_refund_promise_evidence":
            question = "missing_promise_evidence"
        try:
            if report["next_step"] == "draft_merchant_message":
                calls += 1
                d = c.service.draft_merchant_message(case_id, actor="rules_only", customer_id=CUSTOMER_ID)
                draft = {"action_id": d["action_id"], "type": d["type"], "status": d["status"], "review": d["review"], "approval_challenge_id": d["approval_challenge_id"], "payload_hash": d["payload_hash"], "expected_case_version": d["expected_case_version"]}
            elif report["next_step"] == "consider_issuer_dispute":
                calls += 1
                d = c.service.draft_issuer_dispute(case_id, actor="rules_only", customer_id=CUSTOMER_ID)
                draft = {"action_id": d["action_id"], "type": d["type"], "status": d["status"], "review": d["review"], "approval_challenge_id": d["approval_challenge_id"], "payload_hash": d["payload_hash"], "expected_case_version": d["expected_case_version"]}
        except DomainError as exc:
            escalation = {"reason": "draft_blocked", "error": {"code": exc.code, "message": exc.message}}
        return {"calls": calls, "draft": draft, "question": question, "escalation": escalation}


def evaluate(specs: Optional[List[Dict[str, Any]]] = None, modes: Optional[List[str]] = None) -> Dict[str, Any]:
    specs = specs or load_specs()
    modes = modes or ["agent", "rules_only"]
    results = {mode: [Runner(mode).run(s) for s in specs] for mode in modes}
    summary: Dict[str, Any] = {"fixture_version": json.loads((FIXTURES_DIR / "customer.json").read_text())["fixture_version"], "cases": len(specs), "modes": {}}
    for mode, rs in results.items():
        by_split: Dict[str, Dict[str, Any]] = {}
        for r in rs:
            b = by_split.setdefault(r["split"], {"cases": 0, "passed": 0, "unnecessary_questions": 0, "unsupported_claims_emitted": 0, "claims_prevented_by_guardrail": 0, "tool_calls": 0, "escalations": 0})
            b["cases"] += 1
            b["passed"] += 1 if r["passed"] else 0
            b["unnecessary_questions"] += 1 if r["unnecessary_question"] else 0
            b["unsupported_claims_emitted"] += r["metrics"]["claims_in_output"]
            b["claims_prevented_by_guardrail"] += r["metrics"]["guardrail_blocks"]
            b["tool_calls"] += r["metrics"]["tool_calls"]
            b["escalations"] += r["metrics"]["escalations"]
        summary["modes"][mode] = {"by_split": by_split, "passed": sum(1 for r in rs if r["passed"]), "failed": [r["id"] for r in rs if not r["passed"]]}
    return {"summary": summary, "results": results}
