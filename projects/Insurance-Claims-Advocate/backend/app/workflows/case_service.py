"""Case coordinator: creates cases, ingests documents, evaluates, drafts packets and handles approvals.
All calculations, eligibility and state transitions live here or in the domain modules, never in the model."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import Clock, iso, parse_iso
from ..config import Settings
from ..domain.calculator import ExpenseView, FactView, evaluate_case
from ..domain.decision import appeal_permitted, appeal_window, explain_decision
from ..domain.extraction import EXTRACTION_VERSION, extract_document
from ..domain.packet import PacketError, build_submission_packet, verify_provenance
from ..domain.policy import PolicyFixture, PolicyVersion
from ..domain.settlement import reconcile
from ..ids import new_id, payload_hash
from ..persistence.models import (
    Action,
    ApprovalChallenge,
    CaseDocument,
    CaseEvent,
    CaseQuestion,
    ClaimCase,
    ClaimDecision,
    ClaimFact,
    ClaimPayment,
    ClaimSubmission,
    ClaimedExpense,
    Customer,
    Document,
    InsurerRequest,
    Packet,
    Policy,
    ProviderRequestLog,
    ToolRun,
)
from . import approvals as approvals_mod
from .approvals import PrincipalView
from .errors import Conflict, Forbidden, Invalid, NotFound
from .outbox import enqueue_job, enqueue_outbox
from .state_machine import TERMINAL, WAITING_ON_CUSTOMER, WAITING_ON_PROVIDER, TransitionError, record_event, transition

ACTION_TYPE_FOR_PACKET = {"submission": "submit_claim", "supplemental": "add_evidence", "appeal": "submit_appeal"}


class CaseService:
    def __init__(self, db, clock: Clock, settings: Settings, policies: Dict[str, PolicyFixture], adapter):
        self.db = db
        self.clock = clock
        self.settings = settings
        self.policies = policies
        self.adapter = adapter

    # ------------------------------------------------------------------ helpers
    def _load_case(self, s: Session, principal: PrincipalView, case_id: str) -> ClaimCase:
        case = s.get(ClaimCase, case_id)
        if not case:
            raise NotFound(f"case {case_id} not found")
        self._authorize(principal, case)
        return case

    @staticmethod
    def _authorize(principal: PrincipalView, case: ClaimCase) -> None:
        if principal.role == "operator":
            return
        if principal.customer_id != case.customer_id:
            raise Forbidden("case belongs to another customer")

    def policy_for_case(self, s: Session, case: ClaimCase) -> PolicyVersion:
        fixture = self.policies.get(case.policy_id)
        if not fixture:
            raise NotFound(f"policy {case.policy_id} not found")
        if case.policy_version:
            return fixture.get_version(case.policy_version)
        version = fixture.version_for_loss(parse_iso(case.loss_at))
        case.policy_version = version.version
        return version

    def _customer(self, s: Session, customer_id: str) -> Customer:
        cust = s.get(Customer, customer_id)
        if not cust:
            raise NotFound(f"customer {customer_id} not found")
        return cust

    def _case_docs(self, s: Session, case_id: str) -> List[Document]:
        rows = s.execute(select(Document).join(CaseDocument, CaseDocument.document_id == Document.id).where(CaseDocument.case_id == case_id).order_by(CaseDocument.attached_at)).scalars().all()
        return list(rows)

    def _facts(self, s: Session, case_id: str) -> List[ClaimFact]:
        return list(s.scalars(select(ClaimFact).where(ClaimFact.case_id == case_id).order_by(ClaimFact.created_at)).all())

    def _expenses(self, s: Session, case_id: str) -> List[ClaimedExpense]:
        return list(s.scalars(select(ClaimedExpense).where(ClaimedExpense.case_id == case_id).order_by(ClaimedExpense.created_order)).all())

    def _questions(self, s: Session, case_id: str, status: Optional[str] = None) -> List[CaseQuestion]:
        q = select(CaseQuestion).where(CaseQuestion.case_id == case_id)
        if status:
            q = q.where(CaseQuestion.status == status)
        return list(s.scalars(q.order_by(CaseQuestion.created_at)).all())

    def _latest_decision(self, s: Session, case_id: str) -> Optional[ClaimDecision]:
        return s.scalars(select(ClaimDecision).where(ClaimDecision.case_id == case_id).order_by(ClaimDecision.decision_version.desc())).first()

    def prior_challenges(self, s: Session, case_id: str) -> set:
        """(expense_id, reason_code) pairs already submitted in an appeal for this case."""
        out = set()
        subs = s.scalars(select(ClaimSubmission).where(ClaimSubmission.case_id == case_id, ClaimSubmission.submission_kind == "appeal")).all()
        for sub in subs:
            pkt = s.get(Packet, sub.packet_id)
            for ch in (pkt.content_json.get("challenges") if pkt else []) or []:
                out.add((ch.get("expense_id"), ch.get("reason_code")))
        return out

    def _pending_action(self, s: Session, case_id: str) -> Optional[Action]:
        return s.scalars(select(Action).where(Action.case_id == case_id, Action.status.in_(["proposed", "approved", "executing", "uncertain"])).order_by(Action.created_at.desc())).first()

    # ------------------------------------------------------------------ create / documents
    def create_case(self, principal: PrincipalView, *, customer_id: str, policy_id: str, loss_type: str, loss_at: str, document_ids: List[str], mock_scenario: Optional[str] = None) -> Dict[str, Any]:
        if principal.role != "operator" and principal.customer_id != customer_id:
            raise Forbidden("cannot open a case for another customer")
        if loss_type != "baggage_delay":
            raise Invalid("only baggage_delay claims are supported in this release", "unsupported_loss_type")
        with self.db.session() as s:
            cust = self._customer(s, customer_id)
            policy_row = s.get(Policy, policy_id)
            if not policy_row or policy_row.customer_id != customer_id:
                raise Forbidden("policy is not held by this customer")
            fixture = self.policies.get(policy_id)
            if not fixture:
                raise NotFound(f"policy {policy_id} not loaded")
            loss_dt = parse_iso(loss_at)
            version = fixture.version_for_loss(loss_dt)
            now = self.clock.now_iso()
            case = ClaimCase(
                id=new_id("claim"),
                tenant_id=cust.tenant_id,
                customer_id=customer_id,
                policy_id=policy_id,
                policy_version=version.version,
                loss_type=loss_type,
                loss_at=iso(loss_dt),
                status="collecting",
                version=1,
                mock_scenario=mock_scenario,
                created_at=now,
                updated_at=now,
            )
            s.add(case)
            s.flush()
            record_event(s, self.clock, case, "case.opened", principal.id, {"policy_version": version.version, "loss_at": case.loss_at, "claimant": cust.full_name}, bump_version=False)
            for doc_id in document_ids:
                self._attach_one(s, case, doc_id, principal.id)
            evaluation = self._evaluate_locked(s, case, principal.id)
            return {"id": case.id, "status": case.status, "version": case.version, "policy_version": version.version, "missing_fields": evaluation["missing_fields"], "open_questions": [self._question_dict(q) for q in self._questions(s, case.id, "open")]}

    def attach_documents(self, principal: PrincipalView, case_id: str, document_ids: List[str]) -> Dict[str, Any]:
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            if case.status in TERMINAL:
                raise Conflict("case is closed", "case_closed")
            attached = []
            for doc_id in document_ids:
                if self._attach_one(s, case, doc_id, principal.id):
                    attached.append(doc_id)
            if attached:
                invalidated = approvals_mod.invalidate_open_actions(s, self.clock, case, "documents_added")
                if invalidated and case.status == "awaiting_approval":
                    transition(s, self.clock, case, "evaluating", principal.id, "approval.invalidated", {"actions": invalidated, "reason": "documents_added"})
            evaluation = self._evaluate_locked(s, case, principal.id)
            return {"id": case.id, "status": case.status, "version": case.version, "attached": attached, "missing_fields": evaluation["missing_fields"], "open_questions": [self._question_dict(q) for q in self._questions(s, case.id, "open")]}

    def _attach_one(self, s: Session, case: ClaimCase, doc_id: str, actor: str) -> bool:
        doc = s.get(Document, doc_id)
        if not doc:
            raise NotFound(f"document {doc_id} not found")
        if doc.owner_customer_id != case.customer_id:
            raise Forbidden(f"document {doc_id} belongs to another customer")
        if s.get(CaseDocument, {"case_id": case.id, "document_id": doc_id}):
            return False
        s.add(CaseDocument(case_id=case.id, document_id=doc_id, attached_at=self.clock.now_iso()))
        result = extract_document(doc.id, doc.doc_type, doc.content_json.get("pages", []))
        doc.extraction_version = EXTRACTION_VERSION
        created_facts = 0
        for fact in result.facts:
            needs_confirmation = fact.confidence == "low"
            if fact.fact_type == "receipt":
                needs_confirmation = any(flag in fact.uncertainty_flags for flag in ("items_total_mismatch", "no_itemized_lines", "total_missing"))
            s.add(
                ClaimFact(
                    id=new_id("fact"),
                    case_id=case.id,
                    fact_type=fact.fact_type,
                    value_json=fact.value,
                    evidence_id=fact.document_id,
                    source_locator=fact.source_locator,
                    confidence=fact.confidence,
                    uncertainty_flags_json={"flags": fact.uncertainty_flags},
                    confirmation_status="needs_confirmation" if needs_confirmation else "extracted",
                    created_at=self.clock.now_iso(),
                )
            )
            created_facts += 1
        created_expenses = 0
        if result.receipt:
            order_base = s.execute(select(func.count(ClaimedExpense.id)).where(ClaimedExpense.case_id == case.id)).scalar() or 0
            groups: Dict[str, List] = {}
            for item in result.receipt.items:
                groups.setdefault(item.category, []).append(item)
            for idx, (category, items) in enumerate(groups.items()):
                s.add(
                    ClaimedExpense(
                        id=new_id("exp"),
                        case_id=case.id,
                        receipt_id=doc.id,
                        receipt_hash=doc.content_hash,
                        merchant=result.receipt.merchant,
                        purchased_at=result.receipt.purchased_at,
                        amount_minor=sum(i.amount_minor for i in items),
                        currency=result.receipt.currency,
                        category=category,
                        description="; ".join(f"{i.quantity} x {i.description}" for i in items),
                        eligibility_status="uncertain",
                        source_locator=items[0].source_locator,
                        receipt_flags_json={"flags": result.receipt.uncertainty_flags},
                        confirmations_json={},
                        created_order=int(order_base) + idx,
                        created_at=self.clock.now_iso(),
                    )
                )
                created_expenses += 1
        record_event(s, self.clock, case, "document.attached", actor, {"document_id": doc.id, "doc_type": doc.doc_type, "content_hash": doc.content_hash, "facts": created_facts, "expenses": created_expenses, "warnings": result.warnings})
        return True

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, principal: PrincipalView, case_id: str) -> Dict[str, Any]:
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            evaluation = self._evaluate_locked(s, case, principal.id)
            return {"id": case.id, "status": case.status, "version": case.version, "evaluation": evaluation, "open_questions": [self._question_dict(q) for q in self._questions(s, case.id, "open")]}

    def _evaluate_locked(self, s: Session, case: ClaimCase, actor: str) -> Dict[str, Any]:
        policy = self.policy_for_case(s, case)
        cust = self._customer(s, case.customer_id)
        docs = self._case_docs(s, case.id)
        facts = self._facts(s, case.id)
        expenses = self._expenses(s, case.id)
        fact_views = [FactView(f.id, f.fact_type, f.value_json, f.confirmation_status, f.evidence_id, f.source_locator, f.confidence, f.superseded) for f in facts]
        receipt_status = {f.evidence_id: f.confirmation_status for f in facts if f.fact_type == "receipt"}
        expense_views = [
            ExpenseView(
                e.id, e.receipt_id, e.receipt_hash, e.merchant, e.purchased_at, e.amount_minor, e.currency, e.category, e.description, e.source_locator,
                receipt_flags=list((e.receipt_flags_json or {}).get("flags", [])),
                receipt_confirmation_status=receipt_status.get(e.receipt_id, "extracted"),
                confirmed_distinct=bool((e.confirmations_json or {}).get("distinct_purchase")),
                marked_duplicate=bool((e.confirmations_json or {}).get("customer_marked_duplicate")),
                created_order=e.created_order,
            )
            for e in expenses
        ]
        evaluation = evaluate_case(policy, parse_iso(case.loss_at), self.clock.now(), fact_views, expense_views, [d.doc_type for d in docs], cust.full_name)
        # apply outcomes to expense rows
        by_id = {o["expense_id"]: o for o in evaluation["expenses"]}
        for e in expenses:
            o = by_id.get(e.id)
            if o:
                e.eligibility_status = o["status"]
                e.rule_id = o["rule_id"]
                e.exclusion_reason = o["reason"]
                e.duplicate_of = o["duplicate_of"]
        # questions: from evaluation + facts needing confirmation
        existing = self._questions(s, case.id)
        still_needed = {(q["field"], q.get("expense_id")) for q in evaluation["questions"]}
        facts_by_id = {f.id: f for f in facts}
        for q in existing:
            if q.status != "open":
                continue
            if q.field == "confirm_fact":
                f = facts_by_id.get(q.fact_id)
                if f is None or f.superseded or f.confirmation_status != "needs_confirmation":
                    q.status, q.answer_json, q.answered_at = "answered", {"resolved": "fact_no_longer_needs_confirmation"}, self.clock.now_iso()
            elif (q.field, q.expense_id) not in still_needed:
                q.status, q.answer_json, q.answered_at = "answered", {"resolved": "superseded_by_evidence"}, self.clock.now_iso()
        open_keys = {(q.field, q.expense_id, q.fact_id) for q in existing if q.status == "open"}
        answered_keys = {(q.field, q.expense_id, q.fact_id) for q in existing if q.status == "answered"}
        for q in evaluation["questions"]:
            key = (q["field"], q.get("expense_id"), None)
            if key in open_keys or key in answered_keys:
                continue
            s.add(CaseQuestion(id=new_id("q"), case_id=case.id, field=q["field"], question=q["question"], expense_id=q.get("expense_id"), status="open", asked_by="agent", created_at=self.clock.now_iso()))
            open_keys.add(key)
        for f in facts:
            if f.confirmation_status == "needs_confirmation" and not f.superseded:
                key = ("confirm_fact", None, f.id)
                if key in open_keys or key in answered_keys:
                    continue
                flags = ", ".join((f.uncertainty_flags_json or {}).get("flags", [])) or "low confidence"
                s.add(CaseQuestion(id=new_id("q"), case_id=case.id, field="confirm_fact", fact_id=f.id, question=f"Please confirm this extracted detail ({f.fact_type} from {f.source_locator}): {f.value_json}. Flags: {flags}.", status="open", asked_by="agent", created_at=self.clock.now_iso()))
                open_keys.add(key)
        s.flush()
        open_questions = self._questions(s, case.id, "open")
        for q in open_questions:
            label = f"question:{q.field}"
            if label not in evaluation["missing_fields"] and q.field not in evaluation["missing_fields"]:
                evaluation["missing_fields"].append(label)
        evaluation["ready_for_packet"] = evaluation["ready_for_packet"] and not open_questions
        case.evaluation_json = evaluation
        if case.status == "collecting" and evaluation["ready_for_packet"]:
            transition(s, self.clock, case, "evaluating", actor, "case.evaluated", {"totals": evaluation["totals"], "ready": True})
        elif case.status == "evaluating" and not evaluation["ready_for_packet"]:
            transition(s, self.clock, case, "collecting", actor, "case.evaluated", {"totals": evaluation["totals"], "ready": False, "missing_fields": evaluation["missing_fields"]})
        else:
            record_event(s, self.clock, case, "case.evaluated", actor, {"totals": evaluation["totals"], "ready": evaluation["ready_for_packet"], "missing_fields": evaluation["missing_fields"]})
        return evaluation

    # ------------------------------------------------------------------ customer answers
    def answer_question(self, principal: PrincipalView, case_id: str, question_id: str, answer: Dict[str, Any]) -> Dict[str, Any]:
        if principal.role == "operator":
            raise Forbidden("operators cannot answer on behalf of the customer")
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            q = s.get(CaseQuestion, question_id)
            if not q or q.case_id != case.id:
                raise NotFound("question not found")
            if q.status != "open":
                raise Conflict("question already answered", "question_closed")
            now = self.clock.now_iso()
            locator = f"customer:{principal.id}:{q.id}"
            if q.field == "confirm_fact" and q.fact_id:
                fact = s.get(ClaimFact, q.fact_id)
                if answer.get("confirm"):
                    fact.confirmation_status = "confirmed"
                    fact.confirmed_by = principal.id
                    fact.confirmed_at = now
                else:
                    fact.confirmation_status = "rejected"
                    fact.superseded = True
                    if answer.get("value") is not None:
                        s.add(ClaimFact(id=new_id("fact"), case_id=case.id, fact_type=fact.fact_type, value_json=answer["value"], evidence_id=None, source_locator=locator, confidence="high", uncertainty_flags_json={}, confirmation_status="customer_statement", confirmed_by=principal.id, confirmed_at=now, created_at=now))
            elif q.field == "receipt_confirmation" and q.expense_id:
                exp = s.get(ClaimedExpense, q.expense_id)
                for f in self._facts(s, case.id):
                    if f.fact_type == "receipt" and f.evidence_id == exp.receipt_id:
                        f.confirmation_status = "confirmed" if answer.get("confirm") else "rejected"
                        f.confirmed_by = principal.id
                        f.confirmed_at = now
                if answer.get("amount_minor") is not None:
                    exp.amount_minor = int(answer["amount_minor"])
                if answer.get("purchased_at"):
                    exp.purchased_at = iso(parse_iso(answer["purchased_at"]))
                exp.receipt_flags_json = {"flags": [f for f in (exp.receipt_flags_json or {}).get("flags", []) if f != "items_total_mismatch"]}
            elif q.field == "category" and q.expense_id:
                exp = s.get(ClaimedExpense, q.expense_id)
                category = str(answer.get("category", "")).strip().lower()
                if not category:
                    raise Invalid("category is required")
                exp.category = category
                exp.confirmations_json = dict(exp.confirmations_json or {}, category=category, by=principal.id, at=now)
            elif q.field == "purchased_at" and q.expense_id:
                exp = s.get(ClaimedExpense, q.expense_id)
                if not answer.get("purchased_at"):
                    raise Invalid("purchased_at is required")
                exp.purchased_at = iso(parse_iso(answer["purchased_at"]))
                exp.confirmations_json = dict(exp.confirmations_json or {}, purchased_at=exp.purchased_at, by=principal.id, at=now)
                s.add(ClaimFact(id=new_id("fact"), case_id=case.id, fact_type="purchase_time_statement", value_json={"expense_id": exp.id, "purchased_at": exp.purchased_at}, evidence_id=None, source_locator=locator, confidence="high", uncertainty_flags_json={}, confirmation_status="customer_statement", confirmed_by=principal.id, confirmed_at=now, created_at=now))
            elif q.field == "distinct_purchase" and q.expense_id:
                exp = s.get(ClaimedExpense, q.expense_id)
                if answer.get("distinct"):
                    exp.confirmations_json = dict(exp.confirmations_json or {}, distinct_purchase=True, by=principal.id, at=now)
                else:
                    # the record is kept (never deleted); the calculator classifies it as a duplicate
                    exp.confirmations_json = dict(exp.confirmations_json or {}, distinct_purchase=False, customer_marked_duplicate=True, by=principal.id, at=now)
            elif q.field == "destination_confirmation":
                s.add(ClaimFact(id=new_id("fact"), case_id=case.id, fact_type="destination_statement", value_json={"not_home": bool(answer.get("not_home", True))}, evidence_id=None, source_locator=locator, confidence="high", uncertainty_flags_json={}, confirmation_status="customer_statement", confirmed_by=principal.id, confirmed_at=now, created_at=now))
            elif q.field == "loss_date":
                if not answer.get("loss_at"):
                    raise Invalid("loss_at is required")
                new_loss = parse_iso(answer["loss_at"])
                fixture = self.policies[case.policy_id]
                version = fixture.version_for_loss(new_loss)
                record_event(s, self.clock, case, "case.loss_date_corrected", principal.id, {"from": case.loss_at, "to": iso(new_loss), "policy_version_from": case.policy_version, "policy_version_to": version.version}, bump_version=False)
                case.loss_at = iso(new_loss)
                case.policy_version = version.version
            elif q.field == "delay_start_time":
                if not answer.get("delay_start_at"):
                    raise Invalid("delay_start_at is required")
                s.add(ClaimFact(id=new_id("fact"), case_id=case.id, fact_type="delay_reported_at", value_json={"at": iso(parse_iso(answer["delay_start_at"]))}, evidence_id=None, source_locator=locator, confidence="high", uncertainty_flags_json={}, confirmation_status="customer_statement", confirmed_by=principal.id, confirmed_at=now, created_at=now))
            elif q.field == "baggage_delivered_at":
                if not answer.get("delivered_at"):
                    raise Invalid("delivered_at is required (or upload the carrier's delivery confirmation instead)")
                s.add(ClaimFact(id=new_id("fact"), case_id=case.id, fact_type="baggage_delivered_at", value_json={"at": iso(parse_iso(answer["delivered_at"]))}, evidence_id=None, source_locator=locator, confidence="high", uncertainty_flags_json={}, confirmation_status="customer_statement", confirmed_by=principal.id, confirmed_at=now, created_at=now))
            else:
                raise Invalid(f"unsupported question field {q.field}")
            q.status = "answered"
            q.answer_json = answer
            q.answered_at = now
            invalidated = approvals_mod.invalidate_open_actions(s, self.clock, case, "customer_answer")
            record_event(s, self.clock, case, "customer.answered", principal.id, {"question_id": q.id, "field": q.field, "invalidated_actions": invalidated})
            if invalidated and case.status == "awaiting_approval":
                transition(s, self.clock, case, "evaluating", principal.id, "approval.invalidated", {"actions": invalidated, "reason": "customer_answer"})
            evaluation = self._evaluate_locked(s, case, principal.id)
            return {"id": case.id, "status": case.status, "version": case.version, "missing_fields": evaluation["missing_fields"], "open_questions": [self._question_dict(qq) for qq in self._questions(s, case.id, "open")]}

    # ------------------------------------------------------------------ drafts
    def _packet_inputs(self, s: Session, case: ClaimCase):
        policy = self.policy_for_case(s, case)
        cust = self._customer(s, case.customer_id)
        docs = self._case_docs(s, case.id)
        expenses = self._expenses(s, case.id)
        facts = self._facts(s, case.id)
        return policy, cust, docs, expenses, facts

    def _doc_dicts(self, docs: List[Document]) -> List[Dict[str, Any]]:
        return [{"id": d.id, "doc_type": d.doc_type, "content_hash": d.content_hash, "captured_at": d.captured_at} for d in docs]

    def _expense_dicts(self, expenses: List[ClaimedExpense]) -> List[Dict[str, Any]]:
        return [
            {"id": e.id, "receipt_id": e.receipt_id, "merchant": e.merchant, "purchased_at": e.purchased_at, "amount_minor": e.amount_minor, "currency": e.currency, "category": e.category, "description": e.description, "source_locator": e.source_locator, "eligibility_status": e.eligibility_status, "rule_id": e.rule_id, "exclusion_reason": e.exclusion_reason, "duplicate_of": e.duplicate_of}
            for e in expenses
        ]

    def _fact_dicts(self, facts: List[ClaimFact]) -> List[Dict[str, Any]]:
        return [{"id": f.id, "fact_type": f.fact_type, "value": f.value_json, "evidence_id": f.evidence_id, "source_locator": f.source_locator, "confidence": f.confidence, "confirmation_status": f.confirmation_status, "confirmed_by": f.confirmed_by, "confirmed_at": f.confirmed_at, "superseded": f.superseded, "uncertainty_flags": (f.uncertainty_flags_json or {}).get("flags", [])} for f in facts]

    def create_submission_draft(self, principal: PrincipalView, case_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            if case.status in TERMINAL:
                raise Conflict("case is closed", "case_closed")
            if case.status in ("submitted", "under_review", "payout_pending", "paid", "manual_review"):
                raise Conflict(f"case is {case.status}; no new submission can be drafted now", "wrong_state")
            if case.status in ("denied", "partially_approved", "appeal_review", "approved"):
                raise Conflict("a decision exists; use appeal-drafts for a challenge", "use_appeal_draft")
            evaluation = self._evaluate_locked(s, case, principal.id)
            policy, cust, docs, expenses, facts = self._packet_inputs(s, case)
            recipient = f"{policy.insurer_name} via {self.adapter.capabilities.name} ({self.adapter.capabilities.environment})"
            open_request = s.scalars(select(InsurerRequest).where(InsurerRequest.case_id == case.id, InsurerRequest.status == "open").order_by(InsurerRequest.created_at.desc())).first()
            if case.status == "evidence_requested" or (open_request and case.external_claim_ref and case.status == "evaluating"):
                if not open_request:
                    raise Conflict("no open insurer request to respond to", "no_open_request")
                required_type = open_request.requirement_json.get("document_type")
                matching = [d for d in docs if d.doc_type == required_type]
                if not matching:
                    raise Invalid(f"insurer requested '{required_type}' but no such document is attached", "missing_requested_document")
                packet_type = "supplemental"
                try:
                    packet = build_submission_packet(
                        case=self._case_dict(case), policy_public=policy.to_public(), evaluation=evaluation, expenses=self._expense_dicts(expenses), facts=self._fact_dicts(facts), documents=self._doc_dicts(docs), claimant={"id": cust.id, "full_name": cust.full_name}, recipient=recipient,
                        packet_type=packet_type, original_claim_ref=case.external_claim_ref, insurer_request={"provider_request_id": open_request.provider_request_id, "requirement": open_request.requirement_json}, only_document_ids=[d.id for d in matching],
                    )
                except PacketError as exc:
                    raise Invalid(str(exc), "packet_error")
                packet["claimed_expenses"] = []
                packet["requested_total_minor"] = 0
                packet["expected_maximum_minor"] = 0
                packet.pop("content_hash", None)
                packet["content_hash"] = payload_hash(packet)
            else:
                if not evaluation["ready_for_packet"]:
                    raise Invalid("case is not ready: " + ", ".join(evaluation["missing_fields"]) if evaluation["missing_fields"] else "case is not ready: no supported expenses", "not_ready")
                packet_type = "submission"
                if case.external_claim_ref:
                    raise Conflict("claim already submitted; supplemental packets must reference the original claim", "already_submitted")
                try:
                    packet = build_submission_packet(case=self._case_dict(case), policy_public=policy.to_public(), evaluation=evaluation, expenses=self._expense_dicts(expenses), facts=self._fact_dicts(facts), documents=self._doc_dicts(docs), claimant={"id": cust.id, "full_name": cust.full_name}, recipient=recipient)
                except PacketError as exc:
                    raise Invalid(str(exc), "packet_error")
            return self._propose_action(s, case, principal, packet, packet_type, idempotency_key)

    def preview_packet(self, principal: PrincipalView, case_id: str) -> Dict[str, Any]:
        """Itemized claim + evidence manifest, computed but not stored and not submitted."""
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            evaluation = case.evaluation_json or self._evaluate_locked(s, case, principal.id)
            policy, cust, docs, expenses, facts = self._packet_inputs(s, case)
            recipient = f"{policy.insurer_name} via {self.adapter.capabilities.name} ({self.adapter.capabilities.environment})"
            try:
                packet = build_submission_packet(case=self._case_dict(case), policy_public=policy.to_public(), evaluation=evaluation, expenses=self._expense_dicts(expenses), facts=self._fact_dicts(facts), documents=self._doc_dicts(docs), claimant={"id": cust.id, "full_name": cust.full_name}, recipient=recipient)
                packet["ready"] = bool(evaluation.get("ready_for_packet"))
                packet["missing_fields"] = evaluation.get("missing_fields", [])
                return packet
            except PacketError as exc:
                return {"ready": False, "reason": str(exc), "missing_fields": evaluation.get("missing_fields", []), "excluded_items": [e for e in self._expense_dicts(expenses) if e["eligibility_status"] != "supported"], "case_id": case.id}

    def get_policy(self, principal: PrincipalView, policy_id: str, loss_at: Optional[str] = None) -> Dict[str, Any]:
        fixture = self.policies.get(policy_id)
        if not fixture:
            raise NotFound(f"policy {policy_id} not found")
        if principal.role != "operator" and fixture.customer_id != principal.customer_id:
            raise Forbidden("policy is not held by this customer")
        version = fixture.version_for_loss(parse_iso(loss_at)) if loss_at else fixture.latest()
        out = version.to_public()
        out["selected_by"] = "loss_date" if loss_at else "latest"
        out["available_versions"] = [v.version for v in fixture.versions]
        return out

    def create_appeal_draft(self, principal: PrincipalView, case_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            if case.status not in ("denied", "partially_approved", "appeal_review"):
                raise Conflict(f"case is {case.status}; an appeal needs a decision", "wrong_state")
            decision = self._latest_decision(s, case.id)
            if not decision:
                raise Conflict("no insurer decision on file", "no_decision")
            policy, cust, docs, expenses, facts = self._packet_inputs(s, case)
            if not appeal_permitted(policy, parse_iso(decision.decided_at), self.clock.now()):
                raise Invalid("the policy's review window for this decision has passed", "appeal_window_closed")
            if case.followups_used >= self.settings.max_auto_followups:
                raise Invalid("follow-up cap reached for this case; hand off to a human reviewer", "followup_cap")
            evaluation = case.evaluation_json or self._evaluate_locked(s, case, principal.id)
            explanation = explain_decision(policy, self._decision_dict(decision), evaluation, {e["id"]: e for e in self._expense_dicts(expenses)}, [d.doc_type for d in docs], self.prior_challenges(s, case.id))
            decision.explanation_json = explanation
            if not explanation["has_supported_challenge"]:
                decision.appeal_status = "unsupported"
                record_event(s, self.clock, case, "appeal.not_supported", principal.id, {"decision_id": decision.id, "summary": explanation["summary"]})
                raise Invalid("no supported challenge: every rejection is consistent with the policy or cannot be assessed. " + explanation["summary"], "no_supported_challenge")
            window = appeal_window(policy, parse_iso(decision.decided_at))
            recipient = f"{policy.insurer_name} Claims Review Unit via {self.adapter.capabilities.name} ({self.adapter.capabilities.environment})"
            # share only the documents the challenges actually cite
            cited_docs = sorted({loc.split("#")[0][4:] for c in explanation["challenges"] for loc in c["fact_locators"] if loc.startswith("doc:")})
            packet = build_submission_packet(
                case=self._case_dict(case), policy_public=policy.to_public(), evaluation=evaluation, expenses=self._expense_dicts(expenses), facts=self._fact_dicts(facts), documents=self._doc_dicts(docs), claimant={"id": cust.id, "full_name": cust.full_name}, recipient=recipient,
                packet_type="appeal", original_claim_ref=case.external_claim_ref, challenges=explanation["challenges"], only_document_ids=cited_docs or None,
            )
            packet["excluded_items"] = []
            packet["appeal"] = {"decision_version": decision.decision_version, "provider_decision_ref": decision.provider_decision_ref, "process": window["process"], "deadline_at": window["deadline_at"], "deadline_source": window["deadline_source"]}
            packet["claimed_expenses"] = [e for e in packet["claimed_expenses"] if e["expense_id"] in {c["expense_id"] for c in explanation["challenges"]}]
            packet["requested_total_minor"] = explanation["challengeable_minor"]
            packet["expected_maximum_minor"] = explanation["challengeable_minor"]
            packet.pop("content_hash", None)
            packet["content_hash"] = payload_hash(packet)
            decision.appeal_status = "supported_challenge"
            if case.status in ("denied", "partially_approved"):
                transition(s, self.clock, case, "appeal_review", principal.id, "appeal.drafted", {"decision_id": decision.id, "challengeable_minor": explanation["challengeable_minor"]})
            return self._propose_action(s, case, principal, packet, "appeal", idempotency_key)

    def _propose_action(self, s: Session, case: ClaimCase, principal: PrincipalView, packet: Dict[str, Any], packet_type: str, idempotency_key: Optional[str]) -> Dict[str, Any]:
        missing = verify_provenance(packet)
        if missing:
            raise Invalid("packet contains statements without provenance: " + "; ".join(missing), "provenance_missing")
        if self.adapter.capabilities.environment == "mock" and case.mock_scenario:
            packet["mock"] = {"scenario": case.mock_scenario}
            packet.pop("content_hash", None)
            packet["content_hash"] = payload_hash(packet)
        action_type = ACTION_TYPE_FOR_PACKET[packet_type]
        derived_prefix = f"{case.id}:{action_type}:{packet['content_hash']}"
        key = idempotency_key or derived_prefix
        existing = s.scalars(select(Action).where(Action.idempotency_key == key)).first()
        if existing:
            if existing.payload_hash != packet["content_hash"]:
                raise Conflict("idempotency key reused with different content", "idempotency_conflict")
            if existing.status == "proposed":
                challenge = s.scalars(select(ApprovalChallenge).where(ApprovalChallenge.action_id == existing.id, ApprovalChallenge.status == "open")).first()
                if challenge and parse_iso(challenge.expires_at) >= self.clock.now():
                    return self._action_dict(s, existing, case, challenge)
            elif existing.status in ("approved", "executing", "succeeded", "uncertain"):
                return self._action_dict(s, existing, case, None)
            if not idempotency_key:
                # same content proposed again after a failed/invalidated/expired action: a new request reference
                attempt = s.execute(select(func.count(Action.id)).where(Action.case_id == case.id, Action.action_type == action_type, Action.payload_hash == packet["content_hash"])).scalar() or 0
                key = f"{derived_prefix}:r{int(attempt)}"
        # a new proposal replaces any older open proposal for this case
        approvals_mod.invalidate_open_actions(s, self.clock, case, "superseded_by_new_draft")
        pkt = Packet(id=new_id("pkt"), case_id=case.id, packet_type=packet_type, content_hash=packet["content_hash"], content_json=packet, case_version=case.version, created_at=self.clock.now_iso())
        s.add(pkt)
        s.flush()
        review = {
            "destination": packet["recipient"],
            "environment": self.adapter.capabilities.environment,
            "action_type": action_type,
            "documents": [{"document_id": d["document_id"], "doc_type": d["doc_type"], "content_hash": d["content_hash"]} for d in packet["evidence_manifest"]],
            "requested_total_minor": packet["requested_total_minor"],
            "expected_maximum_minor": packet["expected_maximum_minor"],
            "currency": packet["currency"],
            "statements": [st["statement"] for st in packet["statement_of_facts"]],
            "excluded_items": [{"receipt_id": x["receipt_id"], "amount_minor": x["amount_minor"], "reason": x["reason"]} for x in packet["excluded_items"]],
            "challenges": packet.get("challenges", []),
            "irreversible_effect": "The insurer receives these documents and statements; a submitted packet cannot be recalled." if action_type != "submit_appeal" else "The Claims Review Unit receives the challenge and evidence; it cannot be recalled.",
            "simulator_scenario": (packet.get("mock") or {}).get("scenario"),
        }
        if case.status in ("collecting", "evaluating", "evidence_requested", "appeal_review"):
            if case.status == "collecting":
                transition(s, self.clock, case, "evaluating", principal.id, "case.evaluated", {"ready": True})
            transition(s, self.clock, case, "awaiting_approval", principal.id, f"{packet_type}.drafted", {"packet_id": pkt.id, "content_hash": pkt.content_hash})
        elif case.status == "awaiting_approval":
            record_event(s, self.clock, case, f"{packet_type}.redrafted", principal.id, {"packet_id": pkt.id, "content_hash": pkt.content_hash})
        else:
            raise TransitionError(f"cannot draft from state {case.status}")
        action = Action(
            id=new_id("act"), case_id=case.id, action_type=action_type, packet_id=pkt.id, payload_hash=pkt.content_hash, idempotency_key=key, status="proposed",
            expected_case_version=case.version, review_summary_json=review, attempts=0, created_at=self.clock.now_iso(), updated_at=self.clock.now_iso(),
        )
        s.add(action)
        s.flush()
        challenge = approvals_mod.open_challenge(s, self.clock, action, self.settings.approval_ttl_seconds)
        return self._action_dict(s, action, case, challenge)

    def _action_dict(self, s: Session, action: Action, case: ClaimCase, challenge: Optional[ApprovalChallenge]) -> Dict[str, Any]:
        pkt = s.get(Packet, action.packet_id)
        if challenge is None:
            challenge = s.scalars(select(ApprovalChallenge).where(ApprovalChallenge.action_id == action.id).order_by(ApprovalChallenge.created_at.desc())).first()
        return {
            "action_id": action.id,
            "action_type": action.action_type,
            "status": action.status,
            "case_id": case.id,
            "expected_case_version": case.version,
            "packet_id": pkt.id if pkt else None,
            "packet_type": pkt.packet_type if pkt else None,
            "content_hash": action.payload_hash,
            "idempotency_key": action.idempotency_key,
            "approval_challenge_id": challenge.id if challenge else None,
            "challenge_status": challenge.status if challenge else None,
            "challenge_expires_at": challenge.expires_at if challenge else None,
            "review_summary": action.review_summary_json,
            "disclosure_manifest": (pkt.content_json.get("disclosure_manifest") if pkt else None),
            "provider_ref": action.provider_ref,
            "result": action.result_json,
        }

    # ------------------------------------------------------------------ approvals
    def approve_action(self, principal: PrincipalView, action_id: str, *, expected_case_version: int, action_payload_hash: str, approval_challenge_id: str) -> Dict[str, Any]:
        with self.db.session() as s:
            action = s.get(Action, action_id)
            if not action:
                raise NotFound("action not found")
            case = self._load_case(s, principal, action.case_id)
            approval = approvals_mod.approve(
                s, self.clock, principal, action, case,
                expected_case_version=expected_case_version, action_payload_hash=action_payload_hash, approval_challenge_id=approval_challenge_id, ttl_seconds=self.settings.approval_ttl_seconds,
            )
            record_event(s, self.clock, case, "action.approved", principal.id, {"action_id": action.id, "approval_id": approval.id, "payload_hash": action.payload_hash, "scope": approval.scope})
            enqueue_job(s, self.clock, "execute_action", {"action_id": action.id}, dedupe_key=f"execute:{action.id}")
            enqueue_outbox(s, self.clock, case_id=case.id, event_type="claim.action_approved", data={"action_id": action.id, "action_type": action.action_type}, environment=self.settings.environment)
            return {"action_id": action.id, "status": action.status, "approval_id": approval.id, "case_version": case.version, "expires_at": approval.expires_at, "executes": "background worker"}

    def get_action(self, principal: PrincipalView, action_id: str) -> Dict[str, Any]:
        with self.db.session() as s:
            action = s.get(Action, action_id)
            if not action:
                raise NotFound("action not found")
            case = self._load_case(s, principal, action.case_id)
            return self._action_dict(s, action, case, None)

    # ------------------------------------------------------------------ decisions
    def accept_decision(self, principal: PrincipalView, case_id: str) -> Dict[str, Any]:
        """Customer has reviewed the decision and accepts it (no further challenge)."""
        if principal.role == "operator":
            raise Forbidden("only the claimant can accept a decision")
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            decision = self._latest_decision(s, case.id)
            if not decision:
                raise Conflict("no decision to accept", "no_decision")
            if case.status not in ("partially_approved", "denied", "appeal_review"):
                raise Conflict(f"case is {case.status}", "wrong_state")
            approvals_mod.invalidate_open_actions(s, self.clock, case, "decision_accepted")
            if decision.accepted_minor > 0:
                transition(s, self.clock, case, "payout_pending", principal.id, "decision.accepted", {"decision_id": decision.id, "accepted_minor": decision.accepted_minor})
                self._reconcile_locked(s, case, principal.id)
            else:
                transition(s, self.clock, case, "closed_unpaid", principal.id, "decision.accepted", {"decision_id": decision.id, "remaining_options": ["state insurance department complaint (outside this prototype)", "human insurance professional review"]})
            return self.get_case_locked(s, case)

    # ------------------------------------------------------------------ settlement
    def _reconcile_locked(self, s: Session, case: ClaimCase, actor: str) -> Dict[str, Any]:
        decision = self._latest_decision(s, case.id)
        payments = list(s.scalars(select(ClaimPayment).where(ClaimPayment.claim_reference == (case.external_claim_ref or "__none__"))).all())
        accepted = decision.accepted_minor if decision else 0
        currency = decision.currency if decision else self.policy_for_case(s, case).currency
        result = reconcile(accepted_minor=accepted, currency=currency, external_claim_ref=case.external_claim_ref, payee_id=case.customer_id, payments=[self._payment_dict(p) for p in payments])
        matched_refs = {m["payment_ref"] for m in result["matched_payments"]}
        for p in payments:
            p.case_id = case.id
            p.decision_id = decision.id if decision else None
            if p.provider_payment_ref in matched_refs:
                p.reconciliation_status = "matched"
                p.reconciliation_json = {"status": result["status"]}
            else:
                reasons = [i["reasons"] for i in result["ignored_payments"] if i["payment_ref"] == p.provider_payment_ref]
                p.reconciliation_status = "ignored:" + ",".join(reasons[0]) if reasons else "unreconciled"
        if result["can_close_as_paid"] and case.status == "payout_pending":
            transition(s, self.clock, case, "paid", actor, "payment.matched", result)
            case.completion_evidence_ref = ",".join(sorted(matched_refs))
            transition(s, self.clock, case, "closed", actor, "case.closed", {"completion_evidence_ref": case.completion_evidence_ref, "paid_minor": result["paid_minor"]})
        elif payments:
            record_event(s, self.clock, case, "payment.reconciled", actor, result, bump_version=False)
        return result

    def reconcile(self, principal: PrincipalView, case_id: str) -> Dict[str, Any]:
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            result = self._reconcile_locked(s, case, principal.id)
            return {"case_id": case.id, "status": case.status, "settlement": result}

    # ------------------------------------------------------------------ views
    def _case_dict(self, case: ClaimCase) -> Dict[str, Any]:
        return {"id": case.id, "customer_id": case.customer_id, "policy_id": case.policy_id, "policy_version": case.policy_version, "loss_type": case.loss_type, "loss_at": case.loss_at, "status": case.status, "version": case.version, "external_claim_ref": case.external_claim_ref, "mock_scenario": case.mock_scenario, "created_at": case.created_at, "updated_at": case.updated_at}

    def _question_dict(self, q: CaseQuestion) -> Dict[str, Any]:
        return {"id": q.id, "field": q.field, "question": q.question, "fact_id": q.fact_id, "expense_id": q.expense_id, "status": q.status, "answer": q.answer_json, "created_at": q.created_at, "answered_at": q.answered_at}

    def _decision_dict(self, d: ClaimDecision) -> Dict[str, Any]:
        return {"id": d.id, "decision_version": d.decision_version, "outcome": d.outcome, "accepted_minor": d.accepted_minor, "rejected_minor": d.rejected_minor, "currency": d.currency, "reason_items": d.reason_items_json.get("items", []), "evidence_id": d.evidence_id, "provider_decision_ref": d.provider_decision_ref, "appeal_status": d.appeal_status, "decided_at": d.decided_at, "explanation": d.explanation_json}

    def _payment_dict(self, p: ClaimPayment) -> Dict[str, Any]:
        return {"id": p.id, "provider_payment_ref": p.provider_payment_ref, "claim_reference": p.claim_reference, "payee_id": p.payee_id, "amount_minor": p.amount_minor, "currency": p.currency, "payment_status": p.payment_status, "posted_at": p.posted_at, "reconciliation_status": p.reconciliation_status, "environment": p.environment}

    def list_cases(self, principal: PrincipalView) -> List[Dict[str, Any]]:
        with self.db.session() as s:
            q = select(ClaimCase).order_by(ClaimCase.created_at.desc())
            if principal.role != "operator":
                q = q.where(ClaimCase.customer_id == principal.customer_id)
            return [self._case_dict(c) for c in s.scalars(q).all()]

    def get_case(self, principal: PrincipalView, case_id: str) -> Dict[str, Any]:
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            return self.get_case_locked(s, case)

    def get_case_locked(self, s: Session, case: ClaimCase) -> Dict[str, Any]:
        policy = self.policy_for_case(s, case)
        cust = self._customer(s, case.customer_id)
        docs = self._case_docs(s, case.id)
        facts = self._facts(s, case.id)
        expenses = self._expenses(s, case.id)
        questions = self._questions(s, case.id)
        decisions = list(s.scalars(select(ClaimDecision).where(ClaimDecision.case_id == case.id).order_by(ClaimDecision.decision_version)).all())
        requests = list(s.scalars(select(InsurerRequest).where(InsurerRequest.case_id == case.id).order_by(InsurerRequest.created_at)).all())
        submissions = list(s.scalars(select(ClaimSubmission).where(ClaimSubmission.case_id == case.id).order_by(ClaimSubmission.sequence)).all())
        payments = list(s.scalars(select(ClaimPayment).where(ClaimPayment.case_id == case.id).order_by(ClaimPayment.posted_at)).all())
        pending = self._pending_action(s, case.id)
        evaluation = case.evaluation_json or {}
        latest = decisions[-1] if decisions else None
        approved_by_expense: Dict[str, int] = {}
        if latest:
            for item in latest.reason_items_json.get("items", []):
                if item.get("expense_id"):
                    approved_by_expense[item["expense_id"]] = approved_by_expense.get(item["expense_id"], 0) + int(item.get("accepted_minor", 0))
        settlement = None
        if latest:
            settlement = reconcile(accepted_minor=latest.accepted_minor, currency=latest.currency, external_claim_ref=case.external_claim_ref, payee_id=case.customer_id, payments=[self._payment_dict(p) for p in payments])
        expense_rows = []
        for e in expenses:
            expense_rows.append({
                **self._expense_dicts([e])[0],
                "requested_minor": e.amount_minor,
                "supported_minor": e.amount_minor if e.eligibility_status == "supported" else 0,
                "approved_minor": approved_by_expense.get(e.id),
            })
        checklist = []
        for cond in evaluation.get("conditions", []):
            checklist.append({"id": cond["id"], "status": cond["status"], "detail": cond["detail"], "evidence": cond.get("facts", []), "clause": (cond.get("citation") or {}).get("clause_id")})
        required = list(policy.rule("required_evidence").value)
        present_types = {d.doc_type: d.id for d in docs}
        alternatives = policy.rule("alternative_evidence").value
        evidence_checklist = []
        for r in required:
            alt_present = [a for a in alternatives.get(r, []) if a in present_types]
            evidence_checklist.append({"doc_type": r, "present": r in present_types, "document_id": present_types.get(r), "alternative_present": alt_present})
        return {
            "case": self._case_dict(case),
            "claimant": {"customer_id": cust.id, "full_name": cust.full_name, "payout_destination": cust.payout_destination_json},
            "policy": {"policy_id": policy.policy_id, "version": policy.version, "insurer_name": policy.insurer_name, "effective_from": policy.effective_from.isoformat(), "effective_to": policy.effective_to.isoformat() if policy.effective_to else None},
            "waiting_on": "customer" if case.status in WAITING_ON_CUSTOMER else ("provider" if case.status in WAITING_ON_PROVIDER else ("nobody" if case.status in TERMINAL else "operator" if case.status == "manual_review" else "customer")),
            "next_step": self._next_step(case, pending, questions, latest, settlement),
            "checklist": checklist,
            "evidence_checklist": evidence_checklist,
            "documents": self._doc_dicts(docs),
            "facts": self._fact_dicts(facts),
            "expenses": expense_rows,
            "totals": {
                "requested_minor": sum(e.amount_minor for e in expenses),
                "supported_minor": evaluation.get("totals", {}).get("supported_minor", 0),
                "excluded_minor": evaluation.get("totals", {}).get("excluded_minor", 0),
                "uncertain_minor": evaluation.get("totals", {}).get("uncertain_minor", 0),
                "duplicate_minor": evaluation.get("totals", {}).get("duplicate_minor", 0),
                "cap_minor": evaluation.get("totals", {}).get("cap_minor"),
                "estimated_payable_minor": evaluation.get("totals", {}).get("estimated_payable_minor", 0),
                "approved_minor": latest.accepted_minor if latest else None,
                "paid_minor": settlement["paid_minor"] if settlement else 0,
                "outstanding_minor": settlement["outstanding_minor"] if settlement else None,
                "currency": policy.currency,
            },
            "evaluation": evaluation,
            "open_questions": [self._question_dict(q) for q in questions if q.status == "open"],
            "answered_questions": [self._question_dict(q) for q in questions if q.status == "answered"],
            "pending_action": self._action_dict(s, pending, case, None) if pending else None,
            "submissions": [{"id": x.id, "sequence": x.sequence, "kind": x.submission_kind, "packet_id": x.packet_id, "packet_hash": x.packet_hash, "external_claim_ref": x.external_claim_ref, "external_submission_ref": x.external_submission_ref, "submitted_at": x.submitted_at, "status": x.status, "environment": x.environment} for x in submissions],
            "insurer_requests": [{"id": r.id, "provider_request_id": r.provider_request_id, "requirement": r.requirement_json, "due_at": r.due_at, "deadline_source": r.deadline_source, "status": r.status, "satisfied_by_submission_id": r.satisfied_by_submission_id} for r in requests],
            "decisions": [self._decision_dict(d) for d in decisions],
            "payments": [self._payment_dict(p) for p in payments],
            "settlement": settlement,
        }

    def _next_step(self, case: ClaimCase, pending: Optional[Action], questions: List[CaseQuestion], latest: Optional[ClaimDecision], settlement: Optional[Dict[str, Any]]) -> str:
        open_q = [q for q in questions if q.status == "open"]
        if case.status == "closed":
            return "Case closed: the approved amount has been matched to a posted payment."
        if case.status == "closed_unpaid":
            return "Case closed without payment after your review of the decision."
        if pending and pending.status == "proposed":
            return "Review the exact packet and approve it to submit."
        if pending and pending.status in ("approved", "executing"):
            return "Your approved packet is being submitted by the background worker."
        if pending and pending.status == "uncertain":
            return "The insurer's response was lost; the worker is confirming whether the submission was received before doing anything else."
        if open_q:
            return f"Answer {len(open_q)} question(s) so the claim can be evaluated."
        if case.status == "collecting":
            return "Upload the missing evidence listed in the checklist."
        if case.status == "evaluating":
            return "Evidence is complete. Request a submission draft to review."
        if case.status == "evidence_requested":
            return "The insurer asked for a document. Upload it, then request a supplemental draft."
        if case.status in ("submitted", "under_review"):
            return "Waiting for the insurer."
        if case.status in ("partially_approved", "denied"):
            return "Review the decision. Draft an appeal if the explanation shows a supported challenge, or accept the decision."
        if case.status == "appeal_review":
            return "An appeal draft is ready for review, or accept the decision to stop here."
        if case.status == "payout_pending":
            if settlement and settlement["status"] == "partially_paid":
                return f"Partial payment received; {settlement['outstanding_minor'] / 100:.2f} {settlement['currency']} is still outstanding."
            return "Approved. Waiting for the payout to post."
        if case.status == "manual_review":
            return "An operator must resolve an uncertain provider outcome before the case continues."
        return "Waiting."

    def timeline(self, principal: PrincipalView, case_id: str) -> Dict[str, Any]:
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            events = list(s.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.sequence)).all())
            return {
                "case_id": case.id,
                "status": case.status,
                "version": case.version,
                "events": [
                    {"sequence": e.sequence, "id": e.id, "type": e.event_type, "actor": e.actor, "previous_state": e.previous_state, "next_state": e.next_state, "expected_case_version": e.expected_case_version, "source_event_id": e.source_event_id, "occurred_at": e.occurred_at, "data": e.data_json}
                    for e in events
                ],
            }

    def export_case(self, principal: PrincipalView, case_id: str) -> Dict[str, Any]:
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            view = self.get_case_locked(s, case)
            packets = list(s.scalars(select(Packet).where(Packet.case_id == case.id).order_by(Packet.created_at)).all())
            actions = list(s.scalars(select(Action).where(Action.case_id == case.id).order_by(Action.created_at)).all())
            tool_runs = list(s.scalars(select(ToolRun).where(ToolRun.case_id == case.id).order_by(ToolRun.created_at)).all())
            events = self.timeline(principal, case_id)["events"]
            return {
                "export_version": "case-history/1.0",
                "exported_at": self.clock.now_iso(),
                "environment": self.settings.environment,
                **view,
                "packets": [{"id": p.id, "packet_type": p.packet_type, "content_hash": p.content_hash, "case_version": p.case_version, "created_at": p.created_at, "content": p.content_json} for p in packets],
                "actions": [self._action_dict(s, a, case, None) for a in actions],
                "tool_runs": [{"id": t.id, "tool": t.tool_name, "outcome": t.outcome, "latency_ms": t.latency_ms, "input_ref": t.input_ref, "output_ref": t.output_ref, "created_at": t.created_at} for t in tool_runs],
                "events": events,
            }

    def operator_view(self, principal: PrincipalView, case_id: str) -> Dict[str, Any]:
        if principal.role != "operator":
            raise Forbidden("operator role required")
        with self.db.session() as s:
            case = self._load_case(s, principal, case_id)
            actions = list(s.scalars(select(Action).where(Action.case_id == case.id).order_by(Action.created_at)).all())
            logs = list(s.scalars(select(ProviderRequestLog).where(ProviderRequestLog.case_id == case.id).order_by(ProviderRequestLog.created_at)).all())
            tool_runs = list(s.scalars(select(ToolRun).where(ToolRun.case_id == case.id).order_by(ToolRun.created_at)).all())
            docs = self._case_docs(s, case.id)
            return {
                "case": self._case_dict(case),
                "actions": [self._action_dict(s, a, case, None) for a in actions],
                "adapter_requests": [{"id": l.id, "adapter": l.adapter, "operation": l.operation, "request_ref": l.request_ref, "outcome": l.outcome, "environment": l.environment, "detail": l.detail_json, "created_at": l.created_at} for l in logs],
                "tool_runs": [{"id": t.id, "tool": t.tool_name, "outcome": t.outcome, "latency_ms": t.latency_ms, "input_ref": t.input_ref, "output_ref": t.output_ref, "input_summary": t.input_summary_json, "created_at": t.created_at} for t in tool_runs],
                "evidence_refs": [{"document_id": d.id, "doc_type": d.doc_type, "content_hash": d.content_hash} for d in docs],
                "transitions": [e for e in self.timeline(principal, case_id)["events"] if e["next_state"]],
            }
