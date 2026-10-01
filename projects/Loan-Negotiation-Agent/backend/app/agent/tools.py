"""Typed, scoped tools exposed to the model.

Every response records its source, retrieval time and whether the result is
authoritative, estimated or simulated. Tools never perform an external write:
they only *prepare* actions that the borrower must approve.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..domain.final_terms import diff_terms
from ..persistence.db import new_id
from ..persistence.models import Case, Customer, Document, RefinanceApplication, ToolRun
from ..workflows.case_service import CaseService, NotFoundError, ValidationError

MODEL_VERSION = "rules-planner-v1"
PROMPT_VERSION = "prompt-v1"


@dataclass
class ToolResponse:
    tool: str
    data: Dict[str, Any]
    source: str
    retrieved_at: str
    authority: str  # authoritative|estimated|simulated
    environment: str
    ok: bool = True
    error: Optional[str] = None
    latency_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "read_loan_terms",
        "description": "Return confirmed loan terms, cost categories and source locations for the case's existing mortgage and offer documents. Flags missing or contradictory values.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "document_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["case_id"]},
    },
    {
        "name": "record_borrower_facts",
        "description": "Record facts the borrower confirmed (balance, escrow, holding horizon, cash constraint, financing choice). Never alters income.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "facts": {"type": "object"}}, "required": ["case_id", "facts"]},
    },
    {
        "name": "compare_loan_scenarios",
        "description": "Run deterministic amortization schedules and return cash flow, remaining principal, fee and horizon sensitivity results for the current loan and every offer.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "horizon_months": {"type": "integer"}}, "required": ["case_id"]},
    },
    {
        "name": "prepare_lender_request",
        "description": "Draft a truthful repricing or comparable-offer request to a lender for borrower approval. Does not send anything.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "lender_id": {"type": "string"}, "target_offer_id": {"type": "string"}, "competing_offer_id": {"type": "string"}, "disclosed_document_ids": {"type": "array", "items": {"type": "string"}}, "request_type": {"type": "string", "enum": ["reprice", "comparable_offer"]}}, "required": ["case_id", "lender_id"]},
    },
    {
        "name": "prepare_refinance_application",
        "description": "Build a mock refinance application package for the selected offer with outstanding conditions, for borrower approval. Does not submit.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "offer_id": {"type": "string"}, "document_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["case_id", "offer_id"]},
    },
    {
        "name": "diff_final_terms",
        "description": "Report rate, term, fee, payment and condition changes between the approved offer and the lender's final terms.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "application_id": {"type": "string"}}, "required": ["case_id", "application_id"]},
    },
    {
        "name": "get_case_state",
        "description": "Load the current case version, state, outstanding questions, pending approvals and provider references.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]},
    },
]


class ToolBudgetExceeded(Exception):
    pass


class Tools:
    def __init__(self, service: CaseService, environment: str):
        self.service = service
        self.environment = environment

    # ------------------------------------------------------------- plumbing
    def _run(self, session: Session, case: Optional[Case], name: str, args: Dict[str, Any], fn: Callable[[], Dict[str, Any]], source: str, authority: str) -> ToolResponse:
        started = time.monotonic()
        now = self.service.now()
        ok, error, data = True, None, {}
        try:
            data = fn()
        except (ValidationError, NotFoundError) as exc:
            ok, error = False, str(exc)
        except Exception as exc:  # workflow errors are surfaced to the planner, not hidden
            ok, error = False, f"{type(exc).__name__}: {exc}"
        latency = int((time.monotonic() - started) * 1000)
        redacted_in = json.dumps({k: v for k, v in args.items() if k not in ("facts",)}, default=str)[:500]
        session.add(
            ToolRun(
                id=new_id("trun"),
                case_id=case.id if case else None,
                tool_name=name,
                input_ref=redacted_in,
                output_ref=(error or f"keys={sorted(data.keys())}")[:500],
                source=source,
                authority=authority,
                source_timestamp=now,
                latency_ms=latency,
                model_version=MODEL_VERSION,
                prompt_version=PROMPT_VERSION,
                outcome="ok" if ok else "error",
                created_at=now,
            )
        )
        return ToolResponse(tool=name, data=data, source=source, retrieved_at=now.isoformat(), authority=authority, environment=self.environment, ok=ok, error=error, latency_ms=latency)

    # ---------------------------------------------------------------- tools
    def read_loan_terms(self, session: Session, case: Case, customer: Customer, document_ids: Optional[List[str]] = None) -> ToolResponse:
        def fn() -> Dict[str, Any]:
            from ..persistence.models import Mortgage

            mortgage = session.get(Mortgage, case.mortgage_id)
            offers = self.service.offers_for_case(session, case)
            wanted = set(document_ids or [])
            out_offers = []
            for o in offers:
                if wanted and o.document_id not in wanted and o.id not in wanted:
                    continue
                norm = o.normalized_json.get("normalized", {})
                out_offers.append({"offer_id": o.id, "document_id": o.document_id, "lender_id": o.lender_id, "version": o.version, "status": o.status, "terms": {k: norm.get(k) for k in ("loan_amount_minor", "term_months", "note_rate_decimal", "apr_disclosed_decimal", "expires_at", "rate_lock")}, "category_totals_minor": norm.get("category_totals_minor"), "incremental_costs_net_minor": norm.get("incremental_costs_net_minor"), "pass_through_minor": norm.get("pass_through_minor"), "missing_fields": norm.get("missing_fields"), "contradictions": norm.get("contradictions"), "source_refs": norm.get("source_refs")})
            return {
                "existing_mortgage": {
                    "id": mortgage.id,
                    "balance_minor": mortgage.balance_minor,
                    "note_rate_decimal": mortgage.note_rate_decimal,
                    "remaining_months": mortgage.remaining_months,
                    "monthly_pi_minor": mortgage.monthly_pi_minor,
                    "escrow_minor": mortgage.escrow_minor,
                    "payment_includes_escrow": mortgage.payment_includes_escrow,
                    "as_of": mortgage.as_of.isoformat() if mortgage.as_of else None,
                    "confirmed": mortgage.balance_confirmed_at is not None,
                    "evidence_id": mortgage.evidence_id,
                },
                "offers": out_offers,
                "missing_fields": case.missing_fields_json,
            }

        return self._run(session, case, "read_loan_terms", {"case_id": case.id, "document_ids": document_ids}, fn, source="documents+case_store", authority="authoritative")

    def record_borrower_facts(self, session: Session, case: Case, customer: Customer, facts: Dict[str, Any]) -> ToolResponse:
        if "annual_income_minor" in facts or "income" in facts:
            return ToolResponse(tool="record_borrower_facts", data={}, source="policy", retrieved_at=self.service.now().isoformat(), authority="authoritative", environment=self.environment, ok=False, error="income cannot be asserted through the agent; it must come from verified evidence")

        def fn() -> Dict[str, Any]:
            self.service.confirm_facts(session, case, facts, f"customer:{customer.id}")
            return {"missing_fields": case.missing_fields_json, "outstanding_questions": case.outstanding_questions_json, "case_version": case.version, "status": case.state}

        return self._run(session, case, "record_borrower_facts", {"case_id": case.id, "facts": facts}, fn, source="borrower", authority="authoritative")

    def compare_loan_scenarios(self, session: Session, case: Case, customer: Customer, horizon_months: Optional[int] = None) -> ToolResponse:
        def fn() -> Dict[str, Any]:
            comparison = self.service.run_comparison(session, case, "agent", horizon_override=horizon_months)
            return {"comparison_id": comparison.id, **comparison.result_json}

        return self._run(session, case, "compare_loan_scenarios", {"case_id": case.id, "horizon_months": horizon_months}, fn, source="amortization_engine", authority="estimated")

    def prepare_lender_request(self, session: Session, case: Case, customer: Customer, lender_id: str, target_offer_id: Optional[str] = None, competing_offer_id: Optional[str] = None, disclosed_document_ids: Optional[List[str]] = None, request_type: str = "reprice") -> ToolResponse:
        def fn() -> Dict[str, Any]:
            return self.service.draft_lender_request(session, case, lender_id, "agent", target_offer_id, competing_offer_id, disclosed_document_ids or [], request_type)

        return self._run(session, case, "prepare_lender_request", {"case_id": case.id, "lender_id": lender_id, "target_offer_id": target_offer_id, "competing_offer_id": competing_offer_id}, fn, source="case_store", authority="authoritative")

    def prepare_refinance_application(self, session: Session, case: Case, customer: Customer, offer_id: str, document_ids: Optional[List[str]] = None) -> ToolResponse:
        def fn() -> Dict[str, Any]:
            return self.service.draft_application(session, case, offer_id, "agent", document_ids or [])

        return self._run(session, case, "prepare_refinance_application", {"case_id": case.id, "offer_id": offer_id, "document_ids": document_ids}, fn, source="case_store", authority="authoritative")

    def diff_final_terms(self, session: Session, case: Case, customer: Customer, application_id: str) -> ToolResponse:
        def fn() -> Dict[str, Any]:
            app = session.get(RefinanceApplication, application_id)
            if app is None or app.case_id != case.id:
                raise NotFoundError("application not found")
            if app.final_terms_json is None:
                return {"application_id": app.id, "status": app.status, "review": None, "note": "no final terms yet"}
            review = diff_terms(app.packet_json["offer_terms"], app.final_terms_json)
            return {"application_id": app.id, "status": app.status, "review": review.as_dict(), "final_terms_id": app.final_terms_json.get("final_terms_id"), "is_funded": False}

        return self._run(session, case, "diff_final_terms", {"case_id": case.id, "application_id": application_id}, fn, source="case_store+provider_final_terms", authority="simulated" if self.environment == "mock" else "authoritative")

    def get_case_state(self, session: Session, case: Case, customer: Customer) -> ToolResponse:
        return self._run(session, case, "get_case_state", {"case_id": case.id}, lambda: self.service.case_view(session, case), source="case_store", authority="authoritative")

    def call(self, name: str, session: Session, case: Case, customer: Customer, args: Dict[str, Any]) -> ToolResponse:
        args = {k: v for k, v in args.items() if k != "case_id"}
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_") or name in ("call",):
            return ToolResponse(tool=name, data={}, source="tools", retrieved_at=self.service.now().isoformat(), authority="authoritative", environment=self.environment, ok=False, error=f"unknown tool {name}")
        return fn(session, case, customer, **args)
