"""Action executor and reconciliation.

Rules:

* Persist a pending action first, call the provider *outside* any database
  transaction, then reconcile the result in a new transaction.
* Authority is verified again immediately before the side effect.
* A timeout or malformed response means the outcome is unknown: the action is
  marked ``uncertain`` and looked up by its original client request reference.
  Nothing is ever re-submitted.
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select

from ..adapters.base import LenderAdapter, ProviderDeclined, ProviderMalformedResponse, ProviderResult, ProviderTimeout
from ..persistence.db import Database
from ..persistence.models import Action, Case, LenderRequest, LoanOffer, RefinanceApplication, ToolRun
from ..persistence.db import new_id
from . import states as st
from .approvals import ApprovalError, consume_approval, verify_authority
from .case_service import CaseService
from .states import record_event, transition

RECONCILE_BACKOFF_SECONDS = (30, 120, 600)
POLL_INTERVAL = timedelta(days=1)
MAX_POLLS = 10


class Executor:
    def __init__(self, db: Database, service: CaseService, adapter: LenderAdapter):
        self.db = db
        self.service = service
        self.adapter = adapter

    # ----------------------------------------------------------------- helpers
    def _record_provider_call(self, session, case_id: Optional[str], op: str, request_ref: str, result: Optional[ProviderResult], error: Optional[str], latency_ms: int) -> None:
        now = self.service.now()
        session.add(
            ToolRun(
                id=new_id("trun"),
                case_id=case_id,
                tool_name=f"adapter.{op}",
                input_ref=f"request_ref={request_ref}",
                output_ref=f"provider_reference={result.provider_reference}" if result else f"error={error}",
                source=result.source if result else self.adapter.capabilities.environment,
                authority=result.authority if result else "simulated",
                source_timestamp=result.retrieved_at if result else now,
                latency_ms=latency_ms,
                model_version="n/a",
                prompt_version="n/a",
                outcome="ok" if result and result.ok else (error or "error"),
                created_at=now,
            )
        )

    async def _call(self, op: str, case_id: str, request_ref: str, coro):
        started = time.monotonic()
        result: Optional[ProviderResult] = None
        error: Optional[str] = None
        try:
            result = await coro
            return result
        except ProviderTimeout as exc:
            error = f"timeout:{exc}"
            raise
        except ProviderMalformedResponse as exc:
            error = f"malformed:{exc}"
            raise
        except ProviderDeclined as exc:
            error = f"declined:{exc}"
            raise
        finally:
            latency = int((time.monotonic() - started) * 1000)
            with self.db.session() as session:
                self._record_provider_call(session, case_id, op, request_ref, result, error, latency)

    # ---------------------------------------------------------------- execute
    async def execute_action(self, action_id: str, job_id: str) -> Dict[str, Any]:
        with self.db.session() as session:
            action = session.get(Action, action_id)
            if action is None:
                return {"outcome": "missing_action"}
            case = session.get(Case, action.case_id)
            if action.status in ("completed", "failed", "cancelled", "invalidated"):
                return {"outcome": f"already_{action.status}"}
            if action.status == "uncertain":
                return await self.reconcile_action(action_id, job_id)
            now = self.service.now()
            try:
                approval = verify_authority(session, action, case, now)
            except ApprovalError as exc:
                action.status = "failed"
                action.result_json = {"error": str(exc)}
                action.updated_at = now
                session.add(action)
                record_event(session, case, "action.rejected_at_execution", "executor", now, {"action_id": action.id, "reason": str(exc)})
                return {"outcome": "authority_failed", "reason": str(exc)}
            if action.status == "approved":
                consume_approval(session, approval, job_id, now)
                action.status = "executing"
                action.updated_at = now
                session.add(action)
                record_event(session, case, "action.executing", "executor", now, {"action_id": action.id, "client_request_ref": action.client_request_ref})
            payload = dict(action.payload_json)
            request_ref = action.client_request_ref
            action_type = action.type
            case_id = case.id
            # Build provider-facing payloads inside the transaction (read-only), send outside.
            provider_payload = self._provider_payload(session, case, action, payload)

        try:
            result = await self._dispatch(action_type, case_id, request_ref, provider_payload)
        except (ProviderTimeout, ProviderMalformedResponse) as exc:
            return self._mark_uncertain(action_id, job_id, str(exc))
        except ProviderDeclined as exc:
            return self._mark_declined(action_id, str(exc))
        return self._reconcile_success(action_id, result)

    def _provider_payload(self, session, case: Case, action: Action, payload: Dict[str, Any]) -> Dict[str, Any]:
        if action.type == "send_negotiation":
            lr = session.get(LenderRequest, payload["lender_request_id"])
            return {"message": lr.message_json}
        if action.type == "submit_application":
            app = session.get(RefinanceApplication, payload["application_id"])
            return {"packet": app.packet_json}
        if action.type == "provide_documents":
            app = session.get(RefinanceApplication, payload["application_id"])
            return {"application_ref": app.external_application_ref, "documents": {"documents": payload["documents"]}}
        if action.type == "request_closing":
            app = session.get(RefinanceApplication, payload["application_id"])
            return {"application_ref": app.external_application_ref}
        raise ValueError(f"unknown action type {action.type}")

    async def _dispatch(self, action_type: str, case_id: str, request_ref: str, p: Dict[str, Any]) -> ProviderResult:
        if action_type == "send_negotiation":
            return await self._call("send_negotiation", case_id, request_ref, self.adapter.send_negotiation(p["message"], request_ref))
        if action_type == "submit_application":
            return await self._call("submit_application", case_id, request_ref, self.adapter.submit_application(p["packet"], request_ref))
        if action_type == "provide_documents":
            return await self._call("provide_documents", case_id, request_ref, self.adapter.provide_documents(p["application_ref"], p["documents"], request_ref))
        if action_type == "request_closing":
            return await self._call("request_closing", case_id, request_ref, self.adapter.request_closing(p["application_ref"], request_ref))
        raise ValueError(action_type)

    # -------------------------------------------------------------- outcomes
    def _mark_uncertain(self, action_id: str, job_id: str, reason: str) -> Dict[str, Any]:
        with self.db.session() as session:
            action = session.get(Action, action_id)
            case = session.get(Case, action.case_id)
            now = self.service.now()
            action.status = "uncertain"
            attempts = int(action.result_json.get("reconcile_attempts", 0)) if action.result_json else 0
            action.result_json = {"error": reason, "outcome_known": False, "reconcile_attempts": attempts}
            action.updated_at = now
            session.add(action)
            record_event(session, case, "action.outcome_uncertain", "executor", now, {"action_id": action.id, "reason": reason, "client_request_ref": action.client_request_ref})
            delay = RECONCILE_BACKOFF_SECONDS[min(attempts, len(RECONCILE_BACKOFF_SECONDS) - 1)]
            self.service.enqueue(session, "reconcile_action", case.id, {"action_id": action.id}, run_at=now + timedelta(seconds=delay), dedupe_key=f"reconcile:{action.id}:{attempts}")
            return {"outcome": "uncertain", "reason": reason}

    def _mark_declined(self, action_id: str, reason: str) -> Dict[str, Any]:
        with self.db.session() as session:
            action = session.get(Action, action_id)
            case = session.get(Case, action.case_id)
            now = self.service.now()
            action.status = "failed"
            action.result_json = {"error": reason, "outcome_known": True, "declined": True}
            action.updated_at = now
            session.add(action)
            record_event(session, case, "action.declined_by_provider", "executor", now, {"action_id": action.id, "reason": reason})
            if action.type == "send_negotiation":
                lr = session.get(LenderRequest, action.payload_json["lender_request_id"])
                lr.status = "failed"
                lr.response_json = {"outcome": "declined", "reason": reason}
                session.add(lr)
                if case.state == st.NEGOTIATION_PENDING:
                    transition(session, case, st.AWAITING_DECISION, "executor", now, data={"reason": reason})
            elif action.type == "submit_application":
                app = session.get(RefinanceApplication, action.payload_json["application_id"])
                app.status = "declined"
                app.updated_at = now
                session.add(app)
                if case.state == st.AWAITING_APPROVAL:
                    transition(session, case, st.SUBMITTED, "executor", now, data={"reason": "provider processed submission", "application_id": app.id})
                    transition(session, case, st.DECLINED, "executor", now, data={"reason": reason, "application_id": app.id})
            elif action.type in ("provide_documents", "request_closing"):
                transition(session, case, st.MANUAL_REVIEW, "executor", now, data={"reason": reason, "action_id": action.id})
            self.service.add_message(session, case, "system", f"The lender declined: {reason}")
            return {"outcome": "declined", "reason": reason}

    def _reconcile_success(self, action_id: str, result: ProviderResult) -> Dict[str, Any]:
        with self.db.session() as session:
            action = session.get(Action, action_id)
            case = session.get(Case, action.case_id)
            now = self.service.now()
            action.status = "completed"
            action.provider_reference = result.provider_reference
            action.result_json = {"outcome_known": True, "provider": result.as_dict()}
            action.updated_at = now
            session.add(action)
            record_event(session, case, "action.completed", "executor", now, {"action_id": action.id, "provider_reference": result.provider_reference, "environment": result.environment, "authority": result.authority})
            handler = {
                "send_negotiation": self._reconcile_negotiation,
                "submit_application": self._reconcile_submission,
                "provide_documents": self._reconcile_documents,
                "request_closing": self._reconcile_closing,
            }[action.type]
            handler(session, case, action, result)
            return {"outcome": "completed", "provider_reference": result.provider_reference}

    def _reconcile_negotiation(self, session, case: Case, action: Action, result: ProviderResult) -> None:
        now = self.service.now()
        lr = session.get(LenderRequest, action.payload_json["lender_request_id"])
        data = result.data
        lr.external_request_ref = result.provider_reference
        lr.response_json = data
        lr.updated_at = now
        outcome = data.get("outcome")
        target = session.get(LoanOffer, lr.target_offer_id) if lr.target_offer_id else None
        if outcome == "counteroffer" and data.get("offer"):
            lr.status = "answered"
            if target is None:
                # Lender without a prior offer: create an offer family from the counteroffer.
                target = self._offer_from_counter(session, case, lr.lender_id, data["offer"])
            else:
                new_offer = self.service._create_offer_version(session, case, target, data["offer"], provider_reference=result.provider_reference)
                record_event(session, case, "offer.revised", "executor", now, {"offer_id": new_offer.id, "version": new_offer.version, "previous_offer_id": target.id, "lender_id": lr.lender_id})
            self.service.add_message(session, case, "agent", f"{self.service.lenders[lr.lender_id]['name']} replied with a revised offer (version {target.version + 1}): {data.get('reason', '')}")
            if case.state == st.NEGOTIATION_PENDING:
                transition(session, case, st.REVISED_OFFER, "executor", now, data={"lender_request_id": lr.id})
                self.service.run_comparison(session, case, "executor")
        elif outcome == "refused":
            lr.status = "refused"
            self.service.add_message(session, case, "agent", f"{self.service.lenders[lr.lender_id]['name']} declined to reprice: {data.get('reason', '')}")
            if case.state == st.NEGOTIATION_PENDING:
                transition(session, case, st.AWAITING_DECISION, "executor", now, data={"lender_request_id": lr.id, "outcome": "refused"})
        elif outcome == "facts_requested":
            lr.status = "answered"
            questions = list(case.outstanding_questions_json or [])
            for fact in data.get("requested_facts", []):
                questions.append({"id": f"q_lender_{lr.lender_id}_{fact}", "field": fact, "lender_id": lr.lender_id, "question": f"{self.service.lenders[lr.lender_id]['name']} asks for: {fact}. Do you want to share it?", "kind": "lender_fact_request"})
            case.outstanding_questions_json = questions
            session.add(case)
            self.service.add_message(session, case, "agent", f"{self.service.lenders[lr.lender_id]['name']} asked for more information before repricing: {', '.join(data.get('requested_facts', []))}.")
            if case.state == st.NEGOTIATION_PENDING:
                transition(session, case, st.AWAITING_DECISION, "executor", now, data={"lender_request_id": lr.id, "outcome": "facts_requested"})
        elif outcome == "quote_expired":
            lr.status = "answered"
            if target is not None:
                target.status = "expired"
                session.add(target)
            self.service.add_message(session, case, "agent", f"{self.service.lenders[lr.lender_id]['name']} reports the quote has expired; a refreshed Loan Estimate is required before it can be used.")
            if case.state == st.NEGOTIATION_PENDING:
                transition(session, case, st.AWAITING_DECISION, "executor", now, data={"lender_request_id": lr.id, "outcome": "quote_expired"})
        else:
            lr.status = "failed"
            transition(session, case, st.MANUAL_REVIEW, "executor", now, data={"reason": f"unrecognised negotiation outcome {outcome}", "lender_request_id": lr.id})
        session.add(lr)

    def _offer_from_counter(self, session, case: Case, lender_id: str, offer: Dict[str, Any]) -> LoanOffer:
        from ..persistence.models import Document

        doc = {
            "document_id": new_id("counter_doc"),
            "lender_id": lender_id,
            "lender_name": self.service.lenders[lender_id]["name"],
            "product": offer.get("product", "fixed-rate refinance"),
            "loan_amount_minor": offer.get("principal_minor"),
            "term_months": offer.get("term_months"),
            "note_rate_decimal": offer.get("note_rate_decimal"),
            "cost_items": offer.get("cost_items", []),
            "lender_credits_minor": offer.get("lender_credits_minor", 0),
            "expires_at": offer.get("expires_at"),
            "rate_lock": offer.get("rate_lock"),
            "issued_at": offer.get("issued_at"),
        }
        document = Document(id=doc["document_id"], owner_customer_id=case.customer_id, kind="loan_estimate", object_key=f"provider/{doc['document_id']}.json", content_hash="sha256:provider", source="provider", captured_at=self.service.now(), extraction_version="provider-1", content_json=doc)
        session.add(document)
        session.flush()
        return self.service._create_offer_from_document(session, case, document)

    def _apply_application_view(self, session, case: Case, app: RefinanceApplication, view: Dict[str, Any]) -> None:
        """Apply an authoritative (simulated) application view to local state."""
        now = self.service.now()
        app.external_application_ref = view.get("application_ref") or app.external_application_ref
        app.conditions_json = view.get("conditions", [])
        app.updated_at = now
        status = view.get("status")
        open_conditions = [c for c in app.conditions_json if c.get("status") == "open"]
        if status == "declined":
            app.status = "declined"
            if st.can_transition(case.state, st.DECLINED):
                transition(session, case, st.DECLINED, "executor", now, data={"application_id": app.id})
        elif status == "withdrawn":
            app.status = "withdrawn"
            if st.can_transition(case.state, st.WITHDRAWN):
                transition(session, case, st.WITHDRAWN, "executor", now, data={"application_id": app.id, "confirmation": view.get("withdrawal")})
        elif status == "approved_offer" and view.get("final_terms"):
            app.status = "approved_offer"
            app.final_terms_json = view["final_terms"]
            if case.state == st.CONDITIONS_OUTSTANDING:
                transition(session, case, st.SUBMITTED, "executor", now, data={"application_id": app.id, "reason": "conditions cleared"})
            if case.state == st.SUBMITTED:
                transition(session, case, st.APPROVED_OFFER, "executor", now, data={"application_id": app.id, "final_terms_id": view["final_terms"].get("final_terms_id")})
            session.add(app)
            self.service.review_final_terms(session, case, app, "executor")
            self.service.add_message(session, case, "agent", "Final terms received from the lender and compared with the approved offer. This is an approved offer, not a funded or closed loan.")
        elif open_conditions:
            app.status = "conditions_outstanding"
            if case.state == st.SUBMITTED:
                transition(session, case, st.CONDITIONS_OUTSTANDING, "executor", now, data={"application_id": app.id, "condition_ids": [c["id"] for c in open_conditions]})
            self.service.enqueue(session, "poll_application", case.id, {"application_id": app.id, "polls": 0}, run_at=now + POLL_INTERVAL, dedupe_key=f"poll:{app.id}:{app.revision}")
        else:
            app.status = "submitted"
            if case.state == st.CONDITIONS_OUTSTANDING:
                transition(session, case, st.SUBMITTED, "executor", now, data={"application_id": app.id, "reason": "conditions cleared"})
            self.service.enqueue(session, "poll_application", case.id, {"application_id": app.id, "polls": 0}, run_at=now + POLL_INTERVAL, dedupe_key=f"poll:{app.id}:{app.revision}:{now.isoformat()}")
        session.add(app)

    def _reconcile_submission(self, session, case: Case, action: Action, result: ProviderResult) -> None:
        now = self.service.now()
        app = session.get(RefinanceApplication, action.payload_json["application_id"])
        app.client_request_ref = action.client_request_ref
        app.external_application_ref = result.provider_reference
        if case.state == st.AWAITING_APPROVAL:
            transition(session, case, st.SUBMITTED, "executor", now, data={"application_id": app.id, "external_application_ref": result.provider_reference})
        self.service.add_message(session, case, "agent", f"Mock application submitted to {self.service.lenders[app.lender_id]['name']} (reference {result.provider_reference}). This is a submission, not an approval.")
        self._apply_application_view(session, case, app, result.data)
        self.service.outbox(session, "loan.application.submitted", {"case_id": case.id, "application_id": app.id, "provider_reference": result.provider_reference, "environment": result.environment})

    def _reconcile_documents(self, session, case: Case, action: Action, result: ProviderResult) -> None:
        app = session.get(RefinanceApplication, action.payload_json["application_id"])
        app.revision += 1
        manifest = list(app.document_manifest_json)
        for d in action.payload_json.get("documents", []):
            if d["id"] not in [m["id"] for m in manifest]:
                manifest.append(d)
        app.document_manifest_json = manifest
        self._apply_application_view(session, case, app, result.data)

    def _reconcile_closing(self, session, case: Case, action: Action, result: ProviderResult) -> None:
        now = self.service.now()
        app = session.get(RefinanceApplication, action.payload_json["application_id"])
        closing = result.data.get("closing")
        if not closing or result.data.get("status") != "mock_closed":
            transition(session, case, st.MANUAL_REVIEW, "executor", now, data={"reason": "closing requested but simulator returned no closing evidence", "application_id": app.id})
            return
        app.closing_evidence_json = closing
        app.status = "mock_closed"
        app.updated_at = now
        session.add(app)
        if case.state == st.AWAITING_APPROVAL:
            transition(session, case, st.FINAL_REVIEW, "executor", now, data={"application_id": app.id, "reason": "re-approved final terms"})
        if case.state == st.FINAL_REVIEW:
            transition(session, case, st.MOCK_CLOSED, "executor", now, data={"application_id": app.id, "closing_record_id": closing.get("closing_record_id"), "payoff_record_id": closing.get("payoff_record_id")})
        case.completion_evidence_ref = closing.get("closing_record_id")
        session.add(case)
        self.service.add_message(session, case, "agent", f"Mock closing recorded ({closing.get('closing_record_id')}). The existing loan is only marked repaid by a separate payoff record ({closing.get('payoff_record_id')}); no real funds moved.")
        self.service.outbox(session, "loan.case.completed", {"case_id": case.id, "state": case.state, "closing_record_id": closing.get("closing_record_id")})

    # ------------------------------------------------------------- reconcile
    async def reconcile_action(self, action_id: str, job_id: str) -> Dict[str, Any]:
        with self.db.session() as session:
            action = session.get(Action, action_id)
            if action is None or action.status != "uncertain":
                return {"outcome": "nothing_to_reconcile"}
            case_id = action.case_id
            request_ref = action.client_request_ref
            action_type = action.type
            attempts = int((action.result_json or {}).get("reconcile_attempts", 0))
        caps = self.adapter.capabilities
        lookup = None
        if action_type in ("submit_application", "provide_documents", "request_closing") and caps.get_application_by_request_ref:
            lookup = self.adapter.get_application(request_ref)
        elif action_type == "send_negotiation" and caps.get_negotiation_by_request_ref:
            lookup = self.adapter.get_negotiation(request_ref)
        if lookup is None:
            return self._hold_for_review(action_id, "adapter cannot resolve uncertain outcomes by request reference")
        try:
            result = await self._call(f"lookup_{action_type}", case_id, request_ref, lookup)
        except (ProviderTimeout, ProviderMalformedResponse) as exc:
            result = None
            reason = str(exc)
        else:
            reason = result.error or ""
        if result is not None and result.ok:
            if action_type == "provide_documents" or action_type == "request_closing":
                # Lookup returns the application; treat as the operation result.
                pass
            return self._reconcile_success(action_id, result)
        attempts += 1
        if attempts >= len(RECONCILE_BACKOFF_SECONDS):
            return self._hold_for_review(action_id, f"outcome still unknown after {attempts} lookups: {reason}")
        with self.db.session() as session:
            action = session.get(Action, action_id)
            case = session.get(Case, action.case_id)
            now = self.service.now()
            rj = dict(action.result_json or {})
            rj["reconcile_attempts"] = attempts
            rj["last_lookup"] = reason
            action.result_json = rj
            action.updated_at = now
            session.add(action)
            self.service.enqueue(session, "reconcile_action", case.id, {"action_id": action.id}, run_at=now + timedelta(seconds=RECONCILE_BACKOFF_SECONDS[min(attempts, len(RECONCILE_BACKOFF_SECONDS) - 1)]), dedupe_key=f"reconcile:{action.id}:{attempts}")
        return {"outcome": "still_uncertain", "attempts": attempts}

    def _hold_for_review(self, action_id: str, reason: str) -> Dict[str, Any]:
        with self.db.session() as session:
            action = session.get(Action, action_id)
            case = session.get(Case, action.case_id)
            now = self.service.now()
            rj = dict(action.result_json or {})
            rj["held_for_review"] = reason
            action.result_json = rj
            session.add(action)
            if st.can_transition(case.state, st.MANUAL_REVIEW):
                transition(session, case, st.MANUAL_REVIEW, "executor", now, data={"action_id": action.id, "reason": reason})
            return {"outcome": "manual_review", "reason": reason}

    # ------------------------------------------------------------------ polls
    async def poll_application(self, application_id: str, polls: int) -> Dict[str, Any]:
        with self.db.session() as session:
            app = session.get(RefinanceApplication, application_id)
            if app is None or app.status in ("mock_closed", "declined", "withdrawn", "draft"):
                return {"outcome": "no_poll_needed"}
            if not app.client_request_ref:
                return {"outcome": "not_submitted"}
            case_id = app.case_id
            request_ref = app.client_request_ref
        try:
            result = await self._call("get_application", case_id, request_ref, self.adapter.get_application(request_ref))
        except (ProviderTimeout, ProviderMalformedResponse) as exc:
            with self.db.session() as session:
                case = session.get(Case, case_id)
                self.service.enqueue(session, "poll_application", case_id, {"application_id": application_id, "polls": polls + 1}, run_at=self.service.now() + timedelta(hours=1), dedupe_key=f"poll-retry:{application_id}:{polls + 1}")
            return {"outcome": "poll_failed", "reason": str(exc)}
        with self.db.session() as session:
            app = session.get(RefinanceApplication, application_id)
            case = session.get(Case, case_id)
            if not result.ok:
                return self._hold_for_review_case(session, case, f"application {request_ref} not found at provider")
            before = (app.status, case.state)
            self._apply_application_view(session, case, app, result.data)
            if app.status in ("submitted",) and polls + 1 < MAX_POLLS and case.state == st.SUBMITTED:
                self.service.enqueue(session, "poll_application", case_id, {"application_id": application_id, "polls": polls + 1}, run_at=self.service.now() + POLL_INTERVAL, dedupe_key=f"poll:{application_id}:{polls + 1}")
            return {"outcome": "polled", "before": before, "after": (app.status, case.state)}

    def _hold_for_review_case(self, session, case: Case, reason: str) -> Dict[str, Any]:
        now = self.service.now()
        if st.can_transition(case.state, st.MANUAL_REVIEW):
            transition(session, case, st.MANUAL_REVIEW, "executor", now, data={"reason": reason})
        return {"outcome": "manual_review", "reason": reason}

    async def process_provider_event(self, provider_event_id: str) -> Dict[str, Any]:
        from ..persistence.models import ProviderEvent

        with self.db.session() as session:
            evt = session.get(ProviderEvent, provider_event_id)
            if evt is None or evt.status == "processed":
                return {"outcome": "skipped"}
            data = evt.payload_json.get("data", {})
            request_ref = data.get("request_ref")
            app = session.execute(select(RefinanceApplication).where(RefinanceApplication.client_request_ref == request_ref)).scalar_one_or_none() if request_ref else None
            if app is None:
                evt.status = "ignored"
                evt.processed_at = self.service.now()
                session.add(evt)
                return {"outcome": "ignored", "reason": "no matching application"}
            evt.case_id = app.case_id
            session.add(evt)
            application_id = app.id
        # Never trust the callback body for state: look the application up by reference.
        outcome = await self.poll_application(application_id, polls=0)
        with self.db.session() as session:
            evt = session.get(ProviderEvent, provider_event_id)
            evt.status = "processed"
            evt.processed_at = self.service.now()
            session.add(evt)
            case = session.get(Case, evt.case_id)
            record_event(session, case, "provider_event.processed", "worker", self.service.now(), {"provider_event_id": evt.id, "type": evt.event_type}, source_event_id=evt.provider_event_id)
        return {"outcome": "processed", "poll": outcome}

    def expire_offers(self) -> int:
        count = 0
        with self.db.session() as session:
            now = self.service.now()
            offers = session.execute(select(LoanOffer).where(LoanOffer.status.in_(["indicative_quote", "revised_quote"]))).scalars().all()
            for offer in offers:
                if offer.expires_at and now >= offer.expires_at:
                    offer.status = "expired"
                    session.add(offer)
                    case = session.get(Case, offer.case_id)
                    record_event(session, case, "offer.expired", "worker", now, {"offer_id": offer.id, "lender_id": offer.lender_id})
                    count += 1
        return count
