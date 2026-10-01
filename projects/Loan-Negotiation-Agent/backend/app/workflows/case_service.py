"""Case service: durable state, offers, comparisons, drafts and approvals.

All calculations, authorization, eligibility rules and state transitions live
here in ordinary application code. The agent only chooses which of these
operations to call.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock, parse_iso
from ..config import Settings
from ..domain import comparison as cmp
from ..domain.amortization import monthly_payment_minor
from ..domain.final_terms import diff_terms
from ..domain.loan_terms import normalize_offer_document
from ..domain.money import format_minor, normalize_rate
from ..persistence.db import new_id
from ..persistence.models import (
    Action,
    Case,
    CaseEvent,
    Comparison,
    Customer,
    Document,
    Job,
    LenderRequest,
    LoanOffer,
    Message,
    Mortgage,
    OutboxMessage,
    RefinanceApplication,
    TermReviewRecord,
)
from . import states as st
from .approvals import ForbiddenError, canonical_hash, invalidate_pending_authority, propose_action, review_screen
from .states import WorkflowError, record_event, transition

OFFER_SCHEMA_VERSION = "loan-offer/v1"


class NotFoundError(WorkflowError):
    status_code = 404


class ValidationError(WorkflowError):
    status_code = 422


class CaseService:
    def __init__(self, clock: Clock, settings: Settings, lenders: Dict[str, Dict[str, Any]]):
        self.clock = clock
        self.settings = settings
        self.lenders = lenders

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self.clock.now()

    def get_case(self, session: Session, case_id: str, customer: Optional[Customer] = None) -> Case:
        case = session.get(Case, case_id)
        if case is None:
            raise NotFoundError(f"case {case_id} not found")
        if customer is not None and (case.customer_id != customer.id or case.tenant_id != customer.tenant_id):
            # Do not reveal existence across tenants.
            raise NotFoundError(f"case {case_id} not found")
        return case

    def enqueue(self, session: Session, job_type: str, case_id: Optional[str], payload: Dict[str, Any], run_at: Optional[datetime] = None, dedupe_key: Optional[str] = None) -> Job:
        if dedupe_key:
            existing = session.execute(select(Job).where(Job.dedupe_key == dedupe_key)).scalar_one_or_none()
            if existing is not None:
                return existing
        job = Job(
            id=new_id("job"),
            type=job_type,
            case_id=case_id,
            payload_json=payload,
            status="pending",
            run_at=run_at or self.now(),
            dedupe_key=dedupe_key,
            created_at=self.now(),
            updated_at=self.now(),
        )
        session.add(job)
        session.flush()
        return job

    def outbox(self, session: Session, topic: str, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        import hmac

        signature = "sha256=" + hmac.new(self.settings.webhook_signing_secret.encode(), body, hashlib.sha256).hexdigest()
        session.add(OutboxMessage(id=new_id("out"), topic=topic, payload_json=payload, signature=signature, status="pending", created_at=self.now()))

    def add_message(self, session: Session, case: Case, role: str, content: str, data: Optional[Dict[str, Any]] = None) -> Message:
        msg = Message(id=new_id("msg"), case_id=case.id, role=role, content=content, data_json=data or {}, created_at=self.now())
        session.add(msg)
        return msg

    # ---------------------------------------------------------- offer helpers
    def _offer_family(self, case_id: str, lender_id: str, document_id: Optional[str]) -> str:
        return f"fam_{hashlib.sha1(f'{case_id}|{lender_id}|{document_id}'.encode()).hexdigest()[:12]}"

    def offers_for_case(self, session: Session, case: Case, active_only: bool = True) -> List[LoanOffer]:
        q = select(LoanOffer).where(LoanOffer.case_id == case.id).order_by(LoanOffer.created_at, LoanOffer.version)
        offers = session.execute(q).scalars().all()
        if active_only:
            offers = [o for o in offers if o.status not in ("superseded", "refused")]
        return offers

    def get_offer(self, session: Session, case: Case, offer_id: str) -> LoanOffer:
        offer = session.get(LoanOffer, offer_id)
        if offer is None or offer.case_id != case.id:
            raise NotFoundError(f"offer {offer_id} not found")
        return offer

    def _normalized(self, offer: LoanOffer):
        doc = copy.deepcopy(offer.normalized_json.get("source_document", {}))
        for k, v in (offer.normalized_json.get("supplements") or {}).items():
            doc[k] = v
        norm = normalize_offer_document(doc)
        norm.document_id = offer.id  # rank by offer record id
        return norm

    def offer_terms(self, session: Session, case: Case, offer: LoanOffer, mortgage: Mortgage) -> Dict[str, Any]:
        """Versioned loan-offer schema with explicit quote/commitment status."""
        norm = self._normalized(offer)
        finance = bool((case.finance_costs_json or {}).get(offer.id, False))
        incremental = max(0, norm.incremental_costs_net_minor)
        principal = mortgage.balance_minor + (incremental if finance else 0)
        rate = norm.note_rate
        terms = {
            "schema_version": OFFER_SCHEMA_VERSION,
            "offer_id": offer.id,
            "offer_family_id": offer.offer_family_id,
            "version": offer.version,
            "lender_id": offer.lender_id,
            "lender_name": norm.lender_name,
            "document_id": offer.document_id,
            "status": offer.status,
            "commitment": "none" if offer.status in ("indicative_quote", "revised_quote") else "final_offer",
            "currency": offer.currency,
            "principal_minor": principal,
            "term_months": norm.term_months,
            "note_rate_decimal": str(rate) if rate is not None else None,
            "apr_disclosed_decimal": str(norm.apr_disclosed) if norm.apr_disclosed is not None else None,
            "cost_items": [c.as_dict() for c in norm.cost_items],
            "lender_credits_minor": norm.lender_credits_minor,
            "incremental_costs_net_minor": norm.incremental_costs_net_minor,
            "financed_costs_minor": incremental if finance else 0,
            "expires_at": norm.expires_at.isoformat() if norm.expires_at else None,
            "rate_lock": norm.rate_lock,
            "monthly_pi_minor": monthly_payment_minor(principal, rate, norm.term_months) if rate is not None and norm.term_months else None,
            "missing_fields": norm.missing_fields,
            "contradictions": norm.contradictions,
        }
        return terms

    def _create_offer_from_document(self, session: Session, case: Case, document: Document) -> LoanOffer:
        doc = document.content_json
        norm = normalize_offer_document(doc)
        offer = LoanOffer(
            id=new_id("offer"),
            case_id=case.id,
            lender_id=norm.lender_id,
            document_id=document.id,
            offer_family_id=self._offer_family(case.id, norm.lender_id, document.id),
            version=1,
            currency="USD",
            principal_minor=norm.loan_amount_minor,
            term_months=norm.term_months,
            note_rate_decimal=str(norm.note_rate) if norm.note_rate is not None else None,
            apr_disclosed=str(norm.apr_disclosed) if norm.apr_disclosed is not None else None,
            cost_items_json=[c.as_dict() for c in norm.cost_items],
            credits_minor=norm.lender_credits_minor,
            expires_at=norm.expires_at,
            status="indicative_quote",
            normalized_json={"source_document": doc, "supplements": {}, "normalized": norm.as_dict()},
            created_at=self.now(),
        )
        session.add(offer)
        session.flush()
        return offer

    def _create_offer_version(self, session: Session, case: Case, previous: LoanOffer, revised_doc: Dict[str, Any], provider_reference: Optional[str], status: str = "revised_quote") -> LoanOffer:
        base_doc = copy.deepcopy(previous.normalized_json.get("source_document", {}))
        for k, v in (previous.normalized_json.get("supplements") or {}).items():
            base_doc[k] = v
        # Translate revised terms (loan-offer schema) back into a document for normalization.
        for key in ("term_months", "note_rate_decimal", "apr_disclosed_decimal", "cost_items", "lender_credits_minor", "expires_at", "rate_lock", "issued_at", "monthly_pi_minor"):
            if key in revised_doc and revised_doc[key] is not None:
                base_doc[key] = revised_doc[key]
        if "principal_minor" in revised_doc and not revised_doc.get("financed_costs_minor"):
            base_doc["loan_amount_minor"] = revised_doc["principal_minor"]
        base_doc["document_id"] = previous.document_id or previous.id
        base_doc["cost_items"] = [
            {"category": c["category"], "label": c["label"], "amount_minor": c["amount_minor"], "source": c.get("source", {})}
            for c in base_doc.get("cost_items", [])
        ]
        norm = normalize_offer_document(base_doc)
        previous.status = "superseded"
        session.add(previous)
        offer = LoanOffer(
            id=new_id("offer"),
            case_id=case.id,
            lender_id=previous.lender_id,
            document_id=previous.document_id,
            offer_family_id=previous.offer_family_id,
            version=previous.version + 1,
            currency="USD",
            principal_minor=norm.loan_amount_minor,
            term_months=norm.term_months,
            note_rate_decimal=str(norm.note_rate) if norm.note_rate is not None else None,
            apr_disclosed=str(norm.apr_disclosed) if norm.apr_disclosed is not None else None,
            cost_items_json=[c.as_dict() for c in norm.cost_items],
            credits_minor=norm.lender_credits_minor,
            expires_at=norm.expires_at,
            status=status,
            normalized_json={"source_document": base_doc, "supplements": {}, "normalized": norm.as_dict(), "revised_from": previous.id},
            provider_reference=provider_reference,
            created_at=self.now(),
        )
        session.add(offer)
        if case.selected_offer_id == previous.id:
            case.selected_offer_id = offer.id
        session.flush()
        return offer

    # ------------------------------------------------------------ missing facts
    def compute_missing(self, session: Session, case: Case, mortgage: Mortgage) -> List[str]:
        missing: List[str] = []
        if mortgage.balance_confirmed_at is None:
            missing.append("current_balance_as_of")
        if mortgage.payment_includes_escrow is None:
            missing.append("payment_includes_escrow")
        if case.holding_horizon_months is None:
            missing.append("holding_horizon_months")
        return missing

    def outstanding_questions(self, session: Session, case: Case, mortgage: Mortgage) -> List[Dict[str, Any]]:
        questions: List[Dict[str, Any]] = []
        for field in self.compute_missing(session, case, mortgage):
            if field == "current_balance_as_of":
                questions.append({
                    "id": "q_balance",
                    "field": field,
                    "question": f"Your statement shows a balance of {format_minor(mortgage.balance_minor)} with {mortgage.remaining_months} months remaining as of {mortgage.as_of.date() if mortgage.as_of else 'an unknown date'}. Is that still correct?",
                    "kind": "confirm",
                })
            elif field == "payment_includes_escrow":
                questions.append({
                    "id": "q_escrow",
                    "field": field,
                    "question": f"Does the payment you make each month include escrow for taxes and insurance? (Statement principal-and-interest is {format_minor(mortgage.monthly_pi_minor or 0)}; escrow {format_minor(mortgage.escrow_minor)}.)",
                    "kind": "yes_no",
                })
            elif field == "holding_horizon_months":
                questions.append({"id": "q_horizon", "field": field, "question": "How many more months do you expect to keep this home and loan?", "kind": "integer"})
        if case.maximum_cash_to_close_minor is None:
            questions.append({"id": "q_cash", "field": "maximum_cash_to_close_minor", "question": "Is there a maximum amount of cash you are willing to bring to closing? (Optional; costs can be financed.)", "kind": "integer_optional"})
        for offer in self.offers_for_case(session, case):
            norm = self._normalized(offer)
            for f in norm.missing_fields:
                questions.append({
                    "id": f"q_offer_{offer.id}_{f}",
                    "field": f"offers.{offer.id}.{f}",
                    "offer_id": offer.id,
                    "lender_id": offer.lender_id,
                    "question": f"The {norm.lender_name} offer is missing '{f}'. Can you supply it from the Loan Estimate, or should I ask the lender?",
                    "kind": "offer_field",
                })
            for c in norm.contradictions:
                questions.append({
                    "id": f"q_offer_{offer.id}_contradiction",
                    "field": f"offers.{offer.id}",
                    "offer_id": offer.id,
                    "question": f"The {norm.lender_name} offer has a contradiction: {c}",
                    "kind": "contradiction",
                })
        return questions

    def _refresh_missing(self, session: Session, case: Case, mortgage: Mortgage) -> None:
        case.missing_fields_json = self.compute_missing(session, case, mortgage)
        case.outstanding_questions_json = self.outstanding_questions(session, case, mortgage)
        case.updated_at = self.now()
        session.add(case)

    # ------------------------------------------------------------- create case
    def create_case(
        self,
        session: Session,
        customer: Customer,
        mortgage_id: str,
        holding_horizon_months: Optional[int],
        maximum_cash_to_close_minor: Optional[int],
        offer_document_ids: List[str],
    ) -> Case:
        mortgage = session.get(Mortgage, mortgage_id)
        if mortgage is None or mortgage.customer_id != customer.id:
            raise NotFoundError(f"mortgage {mortgage_id} not found")
        if holding_horizon_months is not None and holding_horizon_months <= 0:
            raise ValidationError("holding_horizon_months must be positive")
        now = self.now()
        case = Case(
            id=new_id("loancase"),
            tenant_id=customer.tenant_id,
            customer_id=customer.id,
            mortgage_id=mortgage.id,
            state=st.COLLECTING,
            version=1,
            holding_horizon_months=holding_horizon_months,
            maximum_cash_to_close_minor=maximum_cash_to_close_minor,
            finance_costs_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(case)
        session.flush()
        record_event(session, case, "case.created", f"customer:{customer.id}", now, {"mortgage_id": mortgage.id, "offer_document_ids": offer_document_ids}, next_state=st.COLLECTING)
        for doc_id in offer_document_ids:
            self._attach_document(session, case, customer, doc_id)
        self._refresh_missing(session, case, mortgage)
        self.outbox(session, "loan.case.created", {"case_id": case.id, "state": case.state})
        return case

    def _attach_document(self, session: Session, case: Case, customer: Customer, doc_id: str) -> LoanOffer:
        document = session.get(Document, doc_id)
        if document is None or document.owner_customer_id != customer.id:
            raise NotFoundError(f"document {doc_id} not found")
        if document.kind != "loan_estimate":
            raise ValidationError(f"document {doc_id} is not a Loan Estimate")
        existing = session.execute(select(LoanOffer).where(LoanOffer.case_id == case.id, LoanOffer.document_id == doc_id)).scalars().first()
        if existing is not None:
            return existing
        offer = self._create_offer_from_document(session, case, document)
        record_event(session, case, "offer.ingested", "system", self.now(), {"offer_id": offer.id, "document_id": doc_id, "lender_id": offer.lender_id, "missing_fields": offer.normalized_json["normalized"]["missing_fields"], "contradictions": offer.normalized_json["normalized"]["contradictions"]})
        return offer

    def add_offer_documents(self, session: Session, case: Case, customer: Customer, document_ids: List[str], inline_documents: Optional[List[Dict[str, Any]]] = None) -> List[LoanOffer]:
        if case.state in st.TERMINAL_STATES:
            raise st.IllegalTransitionError(f"case is {case.state}")
        offers: List[LoanOffer] = []
        for doc in inline_documents or []:
            doc = dict(doc)
            doc.setdefault("document_id", new_id("offer_doc"))
            doc["owner_customer_id"] = customer.id
            doc.setdefault("kind", "loan_estimate")
            doc.setdefault("source", "customer_upload")
            document = Document(
                id=doc["document_id"],
                owner_customer_id=customer.id,
                kind="loan_estimate",
                object_key=f"uploads/{doc['document_id']}.json",
                content_hash="sha256:" + hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest(),
                source=doc["source"],
                captured_at=self.now(),
                extraction_version="inline-1",
                content_json=doc,
            )
            session.add(document)
            session.flush()
            document_ids = list(document_ids) + [document.id]
        for doc_id in document_ids:
            offers.append(self._attach_document(session, case, customer, doc_id))
        mortgage = session.get(Mortgage, case.mortgage_id)
        invalidate_pending_authority(session, case, "offers changed", self.now())
        st.bump_version(session, case, self.now())
        if case.state not in (st.COLLECTING,):
            # New evidence means the comparison is stale; go back to comparing.
            if st.can_transition(case.state, st.COMPARING):
                transition(session, case, st.COMPARING, "system", self.now(), data={"reason": "offers added"})
        self._refresh_missing(session, case, mortgage)
        return offers

    def confirm_facts(self, session: Session, case: Case, facts: Dict[str, Any], actor: str) -> Case:
        """Record borrower confirmations for the existing loan and horizon."""
        mortgage = session.get(Mortgage, case.mortgage_id)
        now = self.now()
        changed: Dict[str, Any] = {}
        if facts.get("current_balance_confirmed") is True:
            mortgage.balance_confirmed_at = now
            changed["current_balance_as_of"] = mortgage.as_of.isoformat() if mortgage.as_of else now.isoformat()
        if facts.get("current_balance_minor") is not None:
            new_balance = int(facts["current_balance_minor"])
            if new_balance <= 0:
                raise ValidationError("balance must be positive")
            mortgage.balance_minor = new_balance
            mortgage.balance_confirmed_at = now
            mortgage.as_of = now
            changed["balance_minor"] = new_balance
        if facts.get("remaining_months") is not None:
            mortgage.remaining_months = int(facts["remaining_months"])
            changed["remaining_months"] = mortgage.remaining_months
        if facts.get("payment_includes_escrow") is not None:
            mortgage.payment_includes_escrow = bool(facts["payment_includes_escrow"])
            changed["payment_includes_escrow"] = mortgage.payment_includes_escrow
        if facts.get("holding_horizon_months") is not None:
            h = int(facts["holding_horizon_months"])
            if h <= 0:
                raise ValidationError("holding_horizon_months must be positive")
            case.holding_horizon_months = h
            changed["holding_horizon_months"] = h
        if "maximum_cash_to_close_minor" in facts:
            v = facts["maximum_cash_to_close_minor"]
            case.maximum_cash_to_close_minor = int(v) if v is not None else None
            changed["maximum_cash_to_close_minor"] = case.maximum_cash_to_close_minor
        if facts.get("finance_costs") is not None:
            fc = dict(case.finance_costs_json or {})
            for offer_id, flag in dict(facts["finance_costs"]).items():
                fc[offer_id] = bool(flag)
            case.finance_costs_json = fc
            changed["finance_costs"] = fc
        if "income_evidence_document_id" in facts:
            customer = session.get(Customer, case.customer_id)
            doc_id = facts["income_evidence_document_id"]
            doc = session.get(Document, doc_id) if doc_id else None
            if doc is None or doc.owner_customer_id != customer.id:
                raise NotFoundError(f"document {doc_id} not found")
            vf = dict(customer.verified_facts_json or {})
            vf["income_evidence_document_id"] = doc_id
            customer.verified_facts_json = vf
            session.add(customer)
            changed["income_evidence_document_id"] = doc_id
        if not changed:
            raise ValidationError("no recognised facts supplied")
        session.add(mortgage)
        record_event(session, case, "facts.confirmed", actor, now, changed)
        if any(k in changed for k in ("balance_minor", "remaining_months", "holding_horizon_months", "finance_costs")):
            invalidate_pending_authority(session, case, "material inputs changed", now)
            st.bump_version(session, case, now)
            if case.state in (st.AWAITING_DECISION, st.AWAITING_APPROVAL, st.APPLICATION_REVIEW) and st.can_transition(case.state, st.COMPARING):
                transition(session, case, st.COMPARING, "system", now, data={"reason": "inputs changed"})
        self._refresh_missing(session, case, mortgage)
        return case

    def supply_offer_field(self, session: Session, case: Case, offer_id: str, field: str, value: Any, source: str, actor: str) -> LoanOffer:
        """Record a revised interpretation for a missing offer field without altering the original document."""
        offer = self.get_offer(session, case, offer_id)
        allowed = {"rate_lock", "expires_at", "note_rate_decimal", "term_months", "loan_amount_minor", "apr_disclosed_decimal"}
        if field not in allowed:
            raise ValidationError(f"field {field} cannot be supplemented")
        nj = copy.deepcopy(offer.normalized_json)
        nj.setdefault("supplements", {})[field] = value
        nj.setdefault("supplement_sources", {})[field] = {"source": source, "actor": actor, "at": self.now().isoformat()}
        offer.normalized_json = nj
        norm = self._normalized(offer)
        nj["normalized"] = norm.as_dict()
        offer.normalized_json = nj
        if field == "note_rate_decimal":
            offer.note_rate_decimal = str(normalize_rate(value))
        if field == "term_months":
            offer.term_months = int(value)
        if field == "expires_at":
            offer.expires_at = parse_iso(value)
        session.add(offer)
        record_event(session, case, "offer.field_supplied", actor, self.now(), {"offer_id": offer.id, "field": field, "source": source})
        invalidate_pending_authority(session, case, f"offer {offer.id} changed", self.now())
        st.bump_version(session, case, self.now())
        mortgage = session.get(Mortgage, case.mortgage_id)
        self._refresh_missing(session, case, mortgage)
        return offer

    # -------------------------------------------------------------- comparison
    def run_comparison(self, session: Session, case: Case, actor: str, horizon_override: Optional[int] = None) -> Comparison:
        mortgage = session.get(Mortgage, case.mortgage_id)
        missing = self.compute_missing(session, case, mortgage)
        if horizon_override is not None:
            missing = [m for m in missing if m != "holding_horizon_months"]
        if missing:
            raise ValidationError("cannot compare until confirmed: " + ", ".join(missing))
        if case.state not in (st.COLLECTING, st.COMPARING, st.AWAITING_DECISION, st.REVISED_OFFER):
            raise st.IllegalTransitionError(f"cannot run a comparison while case is {case.state}")
        now = self.now()
        if case.state == st.COLLECTING or case.state == st.REVISED_OFFER or case.state == st.AWAITING_DECISION:
            transition(session, case, st.COMPARING, actor, now)
        horizon = horizon_override or case.holding_horizon_months
        current = cmp.CurrentLoan(
            mortgage_id=mortgage.id,
            balance_minor=mortgage.balance_minor,
            note_rate=normalize_rate(mortgage.note_rate_decimal),
            remaining_months=mortgage.remaining_months,
            monthly_pi_minor=mortgage.monthly_pi_minor,
            escrow_minor=mortgage.escrow_minor,
            as_of=mortgage.as_of,
        )
        offers = self.offers_for_case(session, case)
        normalized = [self._normalized(o) for o in offers]
        result = cmp.compare_scenarios(
            current,
            normalized,
            horizon,
            now,
            finance_costs=dict(case.finance_costs_json or {}),
            maximum_cash_to_close_minor=case.maximum_cash_to_close_minor,
        )
        for o in offers:
            if o.expires_at and now >= o.expires_at and o.status in ("indicative_quote", "revised_quote"):
                o.status = "expired"
                session.add(o)
        result_dict = result.as_dict()
        comparison = Comparison(
            id=new_id("cmp"),
            case_id=case.id,
            mortgage_id=mortgage.id,
            offer_ids_json=[o.id for o in offers],
            horizon_months=horizon,
            calculation_version=result.calculation_version,
            schedule_refs_json={s["scenario_id"]: s["schedule_fingerprint"] for s in [result_dict["keep"]] + result_dict["offers"]},
            cash_to_close_json={s["scenario_id"]: s["cash_to_close"] for s in result_dict["offers"]},
            economic_cost_json={s["scenario_id"]: s["economic_cost_at_horizon_minor"] for s in [result_dict["keep"]] + result_dict["offers"]},
            result_json=result_dict,
            created_at=now,
        )
        session.add(comparison)
        case.latest_comparison_id = comparison.id
        session.add(case)
        record_event(session, case, "comparison.completed", actor, now, {"comparison_id": comparison.id, "horizon_months": horizon, "decision": result.recommendation["decision"], "best_offer_id": result.recommendation["best_offer_id"]})
        transition(session, case, st.AWAITING_DECISION, "system", now, data={"comparison_id": comparison.id})
        self._refresh_missing(session, case, mortgage)
        self.outbox(session, "loan.comparison.completed", {"case_id": case.id, "comparison_id": comparison.id})
        return comparison

    def latest_comparison(self, session: Session, case: Case) -> Optional[Comparison]:
        if not case.latest_comparison_id:
            return None
        return session.get(Comparison, case.latest_comparison_id)

    def decide_keep(self, session: Session, case: Case, actor: str, expected_version: Optional[int] = None) -> Case:
        transition(session, case, st.KEEP_CURRENT, actor, self.now(), expected_version=expected_version, data={"comparison_id": case.latest_comparison_id})
        case.completion_evidence_ref = case.latest_comparison_id
        session.add(case)
        self.outbox(session, "loan.case.completed", {"case_id": case.id, "state": case.state})
        return case

    # ---------------------------------------------------------- lender request
    def draft_lender_request(
        self,
        session: Session,
        case: Case,
        lender_id: str,
        actor: str,
        target_offer_id: Optional[str] = None,
        competing_offer_id: Optional[str] = None,
        disclosed_documents: Optional[List[str]] = None,
        request_type: str = "reprice",
    ) -> Dict[str, Any]:
        """Draft a truthful repricing/comparable-offer request and propose it for approval."""
        if case.state not in (st.AWAITING_DECISION,):
            raise st.IllegalTransitionError(f"a lender request can only be drafted from awaiting_decision (case is {case.state})")
        lender = self.lenders.get(lender_id)
        if lender is None:
            raise NotFoundError(f"lender {lender_id} not known")
        mortgage = session.get(Mortgage, case.mortgage_id)
        customer = session.get(Customer, case.customer_id)
        comparison = self.latest_comparison(session, case)
        if comparison is None:
            raise ValidationError("run a comparison before contacting a lender")
        offers = self.offers_for_case(session, case)
        by_id = {o.id: o for o in offers}

        target = by_id.get(target_offer_id) if target_offer_id else next((o for o in offers if o.lender_id == lender_id), None)
        if target is not None and target.lender_id != lender_id:
            raise ValidationError("target offer does not belong to the lender")
        # Competing offer defaults to the best-ranked offer from a different lender.
        competing = by_id.get(competing_offer_id) if competing_offer_id else None
        if competing is None:
            for oid in comparison.result_json["recommendation"]["ranked_offer_ids"]:
                o = by_id.get(oid)
                if o is not None and o.lender_id != lender_id:
                    competing = o
                    break
        if competing is not None and competing.lender_id == lender_id:
            raise ValidationError("competing offer must come from a different lender")
        if competing is not None:
            comp_norm = self._normalized(competing)
            if comp_norm.missing_fields or comp_norm.contradictions:
                raise ValidationError("competing offer is incomplete; only evidenced terms may be quoted to a lender")
            if comp_norm.is_expired(self.now()):
                raise ValidationError("competing offer has expired and cannot be cited")

        disclosed_documents = list(disclosed_documents or [])
        for doc_id in disclosed_documents:
            doc = session.get(Document, doc_id)
            if doc is None or doc.owner_customer_id != customer.id:
                raise NotFoundError(f"document {doc_id} not found")

        target_terms = self.offer_terms(session, case, target, mortgage) if target else None
        competing_terms = self.offer_terms(session, case, competing, mortgage) if competing else None
        current_terms = {
            "balance_minor": mortgage.balance_minor,
            "note_rate_decimal": mortgage.note_rate_decimal,
            "remaining_months": mortgage.remaining_months,
            "servicer_lender_id": mortgage.servicer_lender_id,
        }
        message_text = self._compose_lender_message(lender, target_terms, competing_terms, current_terms, case.holding_horizon_months, request_type)
        message = {
            "type": request_type,
            "lender_id": lender_id,
            "case_ref": case.id,
            "text": message_text,
            "target_offer": target_terms,
            "competing_offer": self._public_competing_terms(competing_terms) if competing_terms else None,
            "current_loan": current_terms if lender.get("is_current_servicer") else {"note_rate_decimal": mortgage.note_rate_decimal, "remaining_months": mortgage.remaining_months},
            "disclosed_documents": [{"id": d, "kind": session.get(Document, d).kind} for d in disclosed_documents],
            "borrower_facts": {"occupancy": customer.property_json.get("occupancy"), "credit_score_band": customer.verified_facts_json.get("credit_score_band")},
        }
        now = self.now()
        lender_request = LenderRequest(
            id=new_id("lreq"),
            case_id=case.id,
            lender_id=lender_id,
            target_offer_id=target.id if target else None,
            competing_offer_id=competing.id if competing else None,
            message_json=message,
            disclosed_documents_json=disclosed_documents,
            status="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(lender_request)
        session.flush()
        payload = {
            "action": "send_negotiation",
            "lender_id": lender_id,
            "lender_request_id": lender_request.id,
            "offer_id": target.id if target else None,
            "offer_version": target.version if target else None,
            "competing_offer_id": competing.id if competing else None,
            "message_hash": canonical_hash(message),
            "disclosed_documents": disclosed_documents,
            "environment": lender.get("environment", "mock"),
        }
        review = {
            "title": f"Send a {request_type.replace('_', ' ')} request to {lender['name']}",
            "destination": {"lender_id": lender_id, "lender_name": lender["name"], "environment": lender.get("environment", "mock"), "channel": "mock lender network"},
            "message_preview": message_text,
            "terms_shared": {"your_current_rate": mortgage.note_rate_decimal, "competing_offer": self._public_competing_terms(competing_terms) if competing_terms else None},
            "documents_shared": message["disclosed_documents"],
            "irreversible_effect": "The lender will receive this message and the listed documents. Nothing is applied for, locked or committed.",
            "does_not_authorize": ["credit inquiry", "rate lock", "application submission", "closing"],
        }
        action = propose_action(session, case, "send_negotiation", payload, review, idempotency_key=f"neg:{lender_id}:{target.id if target else 'none'}:{competing.id if competing else 'none'}:{case.version}", now=now, ttl_seconds=self.settings.approval_ttl_seconds, actor=actor)
        lender_request.action_id = action.id
        lender_request.approved_message_hash = payload["message_hash"]
        session.add(lender_request)
        transition(session, case, st.AWAITING_APPROVAL, actor, now, data={"action_id": action.id, "lender_request_id": lender_request.id})
        screen = review_screen(session, action, case)
        screen["lender_request_id"] = lender_request.id
        screen["expected_case_version"] = case.version
        action.bound_case_version = case.version
        session.add(action)
        return screen

    @staticmethod
    def _public_competing_terms(terms: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not terms:
            return None
        return {
            "lender_name": terms["lender_name"],
            "note_rate_decimal": terms["note_rate_decimal"],
            "term_months": terms["term_months"],
            "incremental_costs_net_minor": terms["incremental_costs_net_minor"],
            "lender_credits_minor": terms["lender_credits_minor"],
            "expires_at": terms["expires_at"],
            "status": terms["status"],
        }

    @staticmethod
    def _compose_lender_message(lender, target, competing, current, horizon, request_type) -> str:
        def pct(rate: Optional[str]) -> str:
            return f"{(normalize_rate(rate) * 100).normalize()}%" if rate else "n/a"

        lines = [f"To {lender['name']}:"]
        if lender.get("is_current_servicer"):
            lines.append(
                f"I currently hold a fixed-rate mortgage with you at {pct(current['note_rate_decimal'])} with {current['remaining_months']} months remaining and a balance of {format_minor(current['balance_minor'])}."
            )
        elif target:
            lines.append(
                f"Thank you for your Loan Estimate (version {target['version']}) at {pct(target['note_rate_decimal'])} for {target['term_months']} months with net incremental closing costs of {format_minor(target['incremental_costs_net_minor'])}."
            )
        if competing:
            lines.append(
                f"I have a written competing offer from {competing['lender_name']} at {pct(competing['note_rate_decimal'])} for {competing['term_months']} months with net incremental closing costs of {format_minor(competing['incremental_costs_net_minor'])} (status: {competing['status']}, expires {competing['expires_at']})."
            )
        if horizon:
            lines.append(f"I expect to keep the loan for about {horizon} months, so I am comparing total cost over that horizon rather than monthly payment alone.")
        if request_type == "reprice":
            lines.append("Could you offer improved pricing (rate, points or lender credits) on these terms? Please reply with a revised Loan Estimate or confirm your current terms are final.")
        else:
            lines.append("Could you provide a comparable offer on the same loan amount and term so I can compare like for like?")
        lines.append("This is a request for pricing information only. It is not an application and does not authorise a credit inquiry or rate lock.")
        return "\n".join(lines)

    # ------------------------------------------------------------ application
    def draft_application(self, session: Session, case: Case, offer_id: str, actor: str, document_ids: Optional[List[str]] = None, income_assertion_minor: Optional[int] = None) -> Dict[str, Any]:
        if case.state not in (st.AWAITING_DECISION, st.APPLICATION_REVIEW):
            raise st.IllegalTransitionError(f"an application can only be drafted from awaiting_decision (case is {case.state})")
        offer = self.get_offer(session, case, offer_id)
        mortgage = session.get(Mortgage, case.mortgage_id)
        customer = session.get(Customer, case.customer_id)
        lender = self.lenders.get(offer.lender_id)
        if lender is None:
            raise NotFoundError(f"lender {offer.lender_id} not known")
        norm = self._normalized(offer)
        now = self.now()
        if offer.status in ("expired",) or norm.is_expired(now):
            raise ValidationError("offer has expired; request a refreshed quote before applying")
        if norm.missing_fields or norm.contradictions:
            raise ValidationError("offer is incomplete or contradictory: " + ", ".join(norm.missing_fields + norm.contradictions))
        comparison = self.latest_comparison(session, case)
        if comparison is None or offer.id not in comparison.offer_ids_json:
            raise ValidationError("the selected offer has not been compared at the stated horizon")

        verified_income = customer.verified_facts_json.get("annual_income_minor")
        if income_assertion_minor is not None and verified_income is not None and int(income_assertion_minor) != int(verified_income):
            raise ValidationError(
                "income assertion does not match verified income; the application uses confirmed facts only"
            )
        document_ids = list(document_ids or [])
        manifest: List[Dict[str, Any]] = []
        for doc_id in document_ids:
            doc = session.get(Document, doc_id)
            if doc is None or doc.owner_customer_id != customer.id:
                raise NotFoundError(f"document {doc_id} not found")
            manifest.append({"id": doc.id, "kind": doc.kind, "content_hash": doc.content_hash})
        income_doc = customer.verified_facts_json.get("income_evidence_document_id")
        if income_doc and income_doc not in document_ids:
            manifest.append({"id": income_doc, "kind": "income_evidence", "content_hash": session.get(Document, income_doc).content_hash if session.get(Document, income_doc) else None, "auto_included": True})

        terms = self.offer_terms(session, case, offer, mortgage)
        packet = {
            "schema_version": "refi-application/v1",
            "lender_id": offer.lender_id,
            "product": norm.product,
            "offer_terms": terms,
            "borrower": {
                "customer_ref": customer.id,
                "annual_income_minor": verified_income,
                "employment_status": customer.verified_facts_json.get("employment_status"),
                "credit_score_band": customer.verified_facts_json.get("credit_score_band"),
            },
            "property": customer.property_json,
            "existing_loan": {"balance_minor": mortgage.balance_minor, "note_rate_decimal": mortgage.note_rate_decimal, "remaining_months": mortgage.remaining_months},
            "documents": manifest,
            "permissions": {"credit_inquiry": False, "rate_lock": False, "closing": False},
        }
        application = RefinanceApplication(
            id=new_id("refi"),
            case_id=case.id,
            offer_id=offer.id,
            customer_id=customer.id,
            lender_id=offer.lender_id,
            document_manifest_json=manifest,
            packet_json=packet,
            status="draft",
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add(application)
        session.flush()
        case.selected_offer_id = offer.id
        if case.state == st.AWAITING_DECISION:
            transition(session, case, st.APPLICATION_REVIEW, actor, now, data={"application_id": application.id, "offer_id": offer.id})
        payload = {
            "action": "submit_application",
            "lender_id": offer.lender_id,
            "application_id": application.id,
            "offer_id": offer.id,
            "offer_version": offer.version,
            "packet_hash": canonical_hash(packet),
            "disclosed_documents": [m["id"] for m in manifest],
            "environment": lender.get("environment", "mock"),
        }
        review = {
            "title": f"Submit a mock refinance application to {lender['name']}",
            "destination": {"lender_id": offer.lender_id, "lender_name": lender["name"], "environment": lender.get("environment", "mock"), "channel": "mock lender network"},
            "requested_product": norm.product,
            "terms": {
                "principal_minor": terms["principal_minor"],
                "note_rate_decimal": terms["note_rate_decimal"],
                "term_months": terms["term_months"],
                "monthly_pi_minor": terms["monthly_pi_minor"],
                "financed_costs_minor": terms["financed_costs_minor"],
                "incremental_costs_net_minor": terms["incremental_costs_net_minor"],
                "status": terms["status"],
            },
            "documents_shared": manifest,
            "irreversible_effect": "The lender receives your application packet and documents (mock). This does not perform a credit inquiry, lock a rate, or close a loan.",
            "does_not_authorize": ["credit inquiry", "rate lock", "closing", "payoff of existing loan"],
        }
        action = propose_action(session, case, "submit_application", payload, review, idempotency_key=f"app:{application.id}", now=now, ttl_seconds=self.settings.approval_ttl_seconds, actor=actor)
        application.action_id = action.id
        session.add(application)
        transition(session, case, st.AWAITING_APPROVAL, actor, now, data={"action_id": action.id, "application_id": application.id})
        action.bound_case_version = case.version
        session.add(action)
        screen = review_screen(session, action, case)
        screen["application_id"] = application.id
        screen["expected_case_version"] = case.version
        return screen

    def draft_document_release(self, session: Session, case: Case, application_id: str, document_ids: List[str], actor: str) -> Dict[str, Any]:
        """Propose releasing documents to satisfy outstanding conditions (approval required)."""
        application = session.get(RefinanceApplication, application_id)
        if application is None or application.case_id != case.id:
            raise NotFoundError("application not found")
        if case.state != st.CONDITIONS_OUTSTANDING:
            raise st.IllegalTransitionError(f"no conditions are outstanding (case is {case.state})")
        customer = session.get(Customer, case.customer_id)
        docs = []
        for doc_id in document_ids:
            doc = session.get(Document, doc_id)
            if doc is None or doc.owner_customer_id != customer.id:
                raise NotFoundError(f"document {doc_id} not found")
            docs.append({"id": doc.id, "kind": doc.kind, "content_hash": doc.content_hash})
        now = self.now()
        lender = self.lenders[application.lender_id]
        payload = {
            "action": "provide_documents",
            "lender_id": application.lender_id,
            "application_id": application.id,
            "offer_id": application.offer_id,
            "external_application_ref": application.external_application_ref,
            "disclosed_documents": [d["id"] for d in docs],
            "documents": docs,
            "conditions": [c["id"] for c in application.conditions_json if c.get("status") == "open"],
        }
        review = {
            "title": f"Send documents to {lender['name']} for outstanding conditions",
            "destination": {"lender_id": application.lender_id, "lender_name": lender["name"], "environment": lender.get("environment", "mock")},
            "documents_shared": docs,
            "conditions": payload["conditions"],
            "irreversible_effect": "The lender receives the listed documents (mock). No credit inquiry, lock or closing.",
        }
        action = propose_action(session, case, "provide_documents", payload, review, idempotency_key=f"docs:{application.id}:{','.join(sorted(document_ids))}:{case.version}", now=now, ttl_seconds=self.settings.approval_ttl_seconds, actor=actor)
        screen = review_screen(session, action, case)
        screen["application_id"] = application.id
        return screen

    def draft_closing_request(self, session: Session, case: Case, application_id: str, actor: str) -> Dict[str, Any]:
        """Propose the mock closing instruction after final terms were reviewed."""
        application = session.get(RefinanceApplication, application_id)
        if application is None or application.case_id != case.id:
            raise NotFoundError("application not found")
        if case.state != st.FINAL_REVIEW:
            raise st.IllegalTransitionError(f"closing can only be requested from final_review (case is {case.state})")
        review_rec = session.execute(select(TermReviewRecord).where(TermReviewRecord.application_id == application.id).order_by(TermReviewRecord.reviewed_at.desc())).scalars().first()
        if review_rec is None:
            raise ValidationError("final terms have not been reviewed")
        now = self.now()
        lender = self.lenders[application.lender_id]
        final_terms = application.final_terms_json or {}
        payload = {
            "action": "request_closing",
            "lender_id": application.lender_id,
            "application_id": application.id,
            "offer_id": application.offer_id,
            "external_application_ref": application.external_application_ref,
            "final_terms_id": final_terms.get("final_terms_id"),
            "final_terms_hash": canonical_hash(final_terms),
            "term_review_id": review_rec.id,
            "accepts_material_changes": bool(review_rec.requires_reapproval),
        }
        review = {
            "title": f"Accept final terms and request mock closing with {lender['name']}",
            "destination": {"lender_id": application.lender_id, "lender_name": lender["name"], "environment": lender.get("environment", "mock")},
            "final_terms": {k: final_terms.get(k) for k in ("principal_minor", "note_rate_decimal", "term_months", "monthly_pi_minor", "financed_costs_minor", "final_terms_id")},
            "differences_from_approved_offer": review_rec.differences_json,
            "requires_reapproval": review_rec.requires_reapproval,
            "irreversible_effect": "Instructs the mock lender to close. In production this would be a binding closing instruction; here no funds move and the existing loan is not repaid until a payoff record exists.",
        }
        action = propose_action(session, case, "request_closing", payload, review, idempotency_key=f"close:{application.id}:{final_terms.get('final_terms_id')}", now=now, ttl_seconds=self.settings.approval_ttl_seconds, actor=actor)
        if review_rec.requires_reapproval and case.state == st.FINAL_REVIEW:
            transition(session, case, st.AWAITING_APPROVAL, actor, now, data={"action_id": action.id, "reason": "material term changes require re-approval"})
            action.bound_case_version = case.version
            session.add(action)
        screen = review_screen(session, action, case)
        screen["application_id"] = application.id
        screen["expected_case_version"] = case.version
        return screen

    # ------------------------------------------------------------ final terms
    def review_final_terms(self, session: Session, case: Case, application: RefinanceApplication, actor: str) -> TermReviewRecord:
        if application.final_terms_json is None:
            raise ValidationError("no final terms recorded")
        mortgage = session.get(Mortgage, case.mortgage_id)
        offer = self.get_offer(session, case, application.offer_id)
        earlier = application.packet_json["offer_terms"]
        final = application.final_terms_json
        review = diff_terms(earlier, final)
        now = self.now()
        record = TermReviewRecord(
            id=new_id("trev"),
            application_id=application.id,
            earlier_offer_id=offer.id,
            final_terms_id=str(final.get("final_terms_id")),
            differences_json=review.as_dict(),
            requires_reapproval=review.requires_reapproval,
            reviewed_at=now,
        )
        session.add(record)
        record_event(session, case, "final_terms.reviewed", actor, now, {"application_id": application.id, "term_review_id": record.id, "requires_reapproval": review.requires_reapproval, "summary": review.summary})
        if review.requires_reapproval:
            invalidate_pending_authority(session, case, "final terms changed materially", now)
            # Refresh the comparison with the final terms as a new offer version so the borrower sees honest numbers.
            self._create_offer_version(session, case, offer, final, provider_reference=final.get("final_terms_id"), status="final_offer")
            st.bump_version(session, case, now)
        if case.state == st.APPROVED_OFFER:
            transition(session, case, st.FINAL_REVIEW, "system", now, data={"term_review_id": record.id})
        return record

    # ---------------------------------------------------------------- read side
    def timeline(self, session: Session, case: Case) -> List[Dict[str, Any]]:
        events = session.execute(select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.sequence)).scalars().all()
        return [
            {
                "id": e.id,
                "sequence": e.sequence,
                "type": e.event_type,
                "actor": e.actor,
                "previous_state": e.previous_state,
                "next_state": e.next_state,
                "expected_case_version": e.expected_case_version,
                "source_event_id": e.source_event_id,
                "occurred_at": e.occurred_at.isoformat(),
                "data": e.data_json,
            }
            for e in events
        ]

    def case_view(self, session: Session, case: Case) -> Dict[str, Any]:
        mortgage = session.get(Mortgage, case.mortgage_id)
        offers = self.offers_for_case(session, case, active_only=False)
        comparison = self.latest_comparison(session, case)
        actions = session.execute(select(Action).where(Action.case_id == case.id).order_by(Action.created_at)).scalars().all()
        applications = session.execute(select(RefinanceApplication).where(RefinanceApplication.case_id == case.id)).scalars().all()
        lender_requests = session.execute(select(LenderRequest).where(LenderRequest.case_id == case.id).order_by(LenderRequest.created_at)).scalars().all()
        reviews = []
        for app in applications:
            for r in session.execute(select(TermReviewRecord).where(TermReviewRecord.application_id == app.id)).scalars().all():
                reviews.append({"id": r.id, "application_id": app.id, "requires_reapproval": r.requires_reapproval, "differences": r.differences_json, "reviewed_at": r.reviewed_at.isoformat()})
        pending_actions = [a for a in actions if a.status in ("proposed", "approved", "executing", "uncertain")]
        return {
            "id": case.id,
            "status": case.state,
            "version": case.version,
            "customer_id": case.customer_id,
            "mortgage": {
                "id": mortgage.id,
                "balance_minor": mortgage.balance_minor,
                "note_rate_decimal": mortgage.note_rate_decimal,
                "remaining_months": mortgage.remaining_months,
                "monthly_pi_minor": mortgage.monthly_pi_minor,
                "escrow_minor": mortgage.escrow_minor,
                "payment_includes_escrow": mortgage.payment_includes_escrow,
                "as_of": mortgage.as_of.isoformat() if mortgage.as_of else None,
                "balance_confirmed_at": mortgage.balance_confirmed_at.isoformat() if mortgage.balance_confirmed_at else None,
                "servicer_lender_id": mortgage.servicer_lender_id,
                "still_active": True,
            },
            "holding_horizon_months": case.holding_horizon_months,
            "maximum_cash_to_close_minor": case.maximum_cash_to_close_minor,
            "finance_costs": case.finance_costs_json,
            "missing_fields": case.missing_fields_json,
            "outstanding_questions": case.outstanding_questions_json,
            "selected_offer_id": case.selected_offer_id,
            "offers": [
                {
                    "id": o.id,
                    "lender_id": o.lender_id,
                    "lender_name": self.lenders.get(o.lender_id, {}).get("name", o.lender_id),
                    "document_id": o.document_id,
                    "family_id": o.offer_family_id,
                    "version": o.version,
                    "status": o.status,
                    "note_rate_decimal": o.note_rate_decimal,
                    "term_months": o.term_months,
                    "credits_minor": o.credits_minor,
                    "expires_at": o.expires_at.isoformat() if o.expires_at else None,
                    "provider_reference": o.provider_reference,
                    "normalized": o.normalized_json.get("normalized"),
                }
                for o in offers
            ],
            "comparison": comparison.result_json if comparison else None,
            "comparison_id": comparison.id if comparison else None,
            "pending_actions": [review_screen(session, a, case) for a in pending_actions],
            "actions": [{"id": a.id, "type": a.type, "status": a.status, "payload_hash": a.payload_hash, "provider_reference": a.provider_reference, "client_request_ref": a.client_request_ref, "result": a.result_json, "created_at": a.created_at.isoformat()} for a in actions],
            "lender_requests": [{"id": r.id, "lender_id": r.lender_id, "status": r.status, "message": r.message_json.get("text"), "response": r.response_json, "external_request_ref": r.external_request_ref, "approved_message_hash": r.approved_message_hash, "disclosed_documents": r.disclosed_documents_json} for r in lender_requests],
            "applications": [
                {
                    "id": a.id,
                    "offer_id": a.offer_id,
                    "lender_id": a.lender_id,
                    "status": a.status,
                    "external_application_ref": a.external_application_ref,
                    "client_request_ref": a.client_request_ref,
                    "conditions": a.conditions_json,
                    "document_manifest": a.document_manifest_json,
                    "final_terms": a.final_terms_json,
                    "closing_evidence": a.closing_evidence_json,
                    "revision": a.revision,
                    "is_funded": False,
                    "is_completed_refinance": bool(a.closing_evidence_json),
                }
                for a in applications
            ],
            "term_reviews": reviews,
            "completion_evidence_ref": case.completion_evidence_ref,
            "next_decision": self.next_decision(session, case),
            "environment": self.settings.provider_environment,
            "updated_at": case.updated_at.isoformat(),
        }

    def next_decision(self, session: Session, case: Case) -> Dict[str, Any]:
        state = case.state
        if state == st.COLLECTING:
            return {"kind": "answer_questions", "text": "Confirm your current loan facts and holding horizon so the comparison can run."}
        if state == st.COMPARING:
            return {"kind": "wait", "text": "Comparison in progress."}
        if state == st.AWAITING_DECISION:
            comparison = self.latest_comparison(session, case)
            rec = comparison.result_json["recommendation"] if comparison else {}
            return {"kind": "choose", "text": rec.get("reason", "Review the comparison."), "options": ["keep_current", "ask_lender", "apply_with_offer"], "recommendation": rec}
        if state == st.AWAITING_APPROVAL:
            return {"kind": "approve", "text": "Review the exact action on the approval screen and approve or decline it."}
        if state == st.NEGOTIATION_PENDING:
            return {"kind": "wait", "text": "Waiting for the lender's reply."}
        if state == st.CONDITIONS_OUTSTANDING:
            return {"kind": "provide_documents", "text": "The lender has requested documents. Choose which to release."}
        if state == st.SUBMITTED:
            return {"kind": "wait", "text": "Application submitted; waiting for lender decision."}
        if state == st.APPROVED_OFFER:
            return {"kind": "wait", "text": "Final terms received; review pending."}
        if state == st.FINAL_REVIEW:
            return {"kind": "review_final_terms", "text": "Compare the final terms with the offer you approved, then decide whether to proceed to mock closing."}
        if state == st.MANUAL_REVIEW:
            return {"kind": "operator", "text": "An operator must resolve an uncertain provider outcome or contradiction."}
        if state in st.TERMINAL_STATES:
            return {"kind": "done", "text": f"Case finished with outcome '{state}'."}
        return {"kind": "unknown", "text": state}
