"""Applications, exact-action approvals, the executor and underwriting reconciliation.

Approval binds to insurer, product, premium, limits, deductible and effective date via
the payload hash. The executor re-verifies authority immediately before the side effect.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..adapters.base import ProviderError, ProviderMalformedResponse, ProviderTimeout
from ..context import AppContext
from ..domain.application import IncompleteApplication, StaleQuote, application_payload_hash, build_application
from ..domain.coverage import compare_coverage
from ..domain.hashing import sha256_hash
from ..domain.issuance import verify_policy
from ..domain.money import fmt_minor
from ..domain.needs import InsuranceNeeds
from ..domain.quotes import RentersQuote
from ..fixtures import household_by_customer
from ..persistence import models as m
from ..persistence import repositories as repo
from ..persistence.db import new_id
from ..persistence.jobs import enqueue_job
from . import states as st
from .case_service import CaseError, CaseService
from .states import IllegalTransition, StaleVersion, record_event, transition

CHALLENGE_TTL_MINUTES = 30
MAX_UNCERTAIN_RECONCILE_ATTEMPTS = 5


class ApplicationService:
    def __init__(self, ctx: AppContext, cases: Optional[CaseService] = None) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.registry = ctx.registry
        self.cases = cases or CaseService(ctx)

    def now(self) -> datetime:
        return self.ctx.now()

    def _env(self) -> str:
        return self.ctx.environment

    # ------------------------------------------------------------------ prepare
    def answers_version(self, session: Session, case_id: str) -> int:
        return sum(row.answer_version for row in repo.latest_answers(session, case_id).values())

    def prepare_application(self, case_id: str, customer_id: Optional[str], quote_id: str, answers_version: Optional[int], actor: str,
                            idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            case = repo.get_case_for_customer(session, case_id, customer_id) if customer_id else repo.get_case(session, case_id)
            needs = repo.current_needs(session, case)
            if case.state not in (st.AWAITING_SELECTION, st.AWAITING_APPROVAL, st.COMPARING):
                raise CaseError("an application can only be prepared while awaiting selection (case is %s)" % case.state, 409)
            quote_row = repo.get_quote(session, quote_id)
            if quote_row.case_id != case.id or quote_row.needs_version != needs.version or quote_row.superseded_by:
                raise CaseError("quote does not belong to the current needs version", 409)
            quote = RentersQuote.model_validate(quote_row.quote_json)
            comparison = compare_coverage(needs, [quote], now)
            if not comparison["suitable"]:
                reason = (comparison["excluded"] or comparison["undetermined"])[0]["reason"]
                raise CaseError("quote is not suitable and cannot be applied for: %s" % reason, 409, {"reason": reason})
            current_answers_version = self.answers_version(session, case.id)
            if answers_version is not None and answers_version != current_answers_version:
                raise CaseError("answers_version is stale (current %d)" % current_answers_version, 409, {"answers_version": current_answers_version})
            answers = repo.answers_as_records(session, case.id)
            household = household_by_customer(case.customer_id)
            existing_apps = [a for a in repo.applications_for_case(session, case.id) if a.quote_id == quote_row.id]
            revision = len(existing_apps) + 1
            existing_action = session.query(m.Action).filter(m.Action.idempotency_key == idempotency_key).first() if idempotency_key else None
            if existing_action is not None and existing_action.status in ("proposed", "approved"):
                # Same key: rebuild at the original revision and compare content.
                existing_app = session.get(m.Application, existing_action.application_id)
                revision = existing_app.revision if existing_app else revision

            def _build(rev: int):
                try:
                    return build_application(case.id, case.customer_id, household, needs, quote, self.registry.questions(quote.insurer_id), answers, now, revision=rev)
                except IncompleteApplication as exc:
                    raise CaseError("application incomplete: unanswered insurer questions", 409, {"missing_questions": exc.missing_questions})
                except StaleQuote as exc:
                    raise CaseError(str(exc), 409)

            payload = _build(revision)
            payload_hash = application_payload_hash(payload)
            key = idempotency_key or "%s:submit_application:%s:%s:r%d" % (case.id, quote_row.id, payload["answers_hash"], revision)
            if existing_action is None and not idempotency_key:
                existing_action = session.query(m.Action).filter(m.Action.idempotency_key == key).first()
            if existing_action is not None:
                if existing_action.payload_hash != payload_hash:
                    raise CaseError("idempotency key reused with different content", 409)
                if existing_action.status in ("proposed", "approved"):
                    app = session.get(m.Application, existing_action.application_id)
                    return self._prepared_response(session, case, app, existing_action)
                # A closed action with the same key: create a new revision under a derived key.
                revision = len(existing_apps) + 1
                payload = _build(revision)
                payload_hash = application_payload_hash(payload)
                key = key + ":r%d" % revision

            app = m.Application(
                id=new_id("application"),
                case_id=case.id,
                quote_id=quote_row.id,
                answers_hash=payload["answers_hash"],
                payload_json=payload,
                payload_hash=payload_hash,
                status="prepared",
                revision=revision,
                created_at=now,
                updated_at=now,
            )
            session.add(app)
            session.flush()
            self.cases._invalidate_open_actions(session, case, "superseded by a new application revision", now)
            for old in existing_apps:
                if old.status in ("prepared", "approved"):
                    old.status = "superseded"
            action = self._propose_action(session, case, "submit_application", app, payload, payload_hash, key, now)
            case.selected_quote_id = quote_row.id
            case.current_application_id = app.id
            if case.state != st.AWAITING_APPROVAL:
                if case.state == st.COMPARING:
                    transition(session, case, st.AWAITING_SELECTION, actor, "insurance.case.awaiting_selection", now, self._env())
                transition(session, case, st.AWAITING_APPROVAL, actor, "insurance.application.prepared", now, self._env(),
                           {"application_id": app.id, "action_id": action.id, "quote_id": quote_row.id, "revision": revision})
            else:
                record_event(session, case, "insurance.application.prepared", actor, now, self._env(), {"application_id": app.id, "action_id": action.id, "revision": revision})
            action.expected_case_version = case.version
            session.flush()
            return self._prepared_response(session, case, app, action)

    def _propose_action(self, session: Session, case: m.Case, action_type: str, app: m.Application, payload: Dict[str, Any], payload_hash: str,
                        idempotency_key: str, now: datetime, extra: Optional[Dict[str, Any]] = None) -> m.Action:
        terms = payload["terms"]
        insurer = self.registry.config(payload["insurer_id"])
        review = {
            "action": action_type,
            "destination": {"insurer_id": payload["insurer_id"], "insurer_name": insurer["display_name"], "environment": self._env(), "protocol": self.registry.get(payload["insurer_id"]).capabilities().protocol},
            "amount": {"annual_premium_minor": terms["annual_premium_minor"], "currency": terms["currency"], "display": fmt_minor(terms["annual_premium_minor"], terms["currency"]) + " per year"},
            "terms": {
                "property_limit": fmt_minor(terms["property_limit_minor"]),
                "liability_limit": fmt_minor(terms["liability_limit_minor"]),
                "deductible": fmt_minor(terms["deductible_minor"]),
                "replacement_cost": terms["replacement_cost"],
                "effective_date": terms["effective_date"],
                "policy_form_version": terms["policy_form_version"],
                "exclusions": terms["exclusions"],
                "endorsements": terms["endorsements"],
            },
            "answers": payload["answers"],
            "documents_disclosed": payload["disclosed_document_ids"],
            "irreversible_effects": payload["irreversible_effects"],
            "expires_at": (now + timedelta(minutes=CHALLENGE_TTL_MINUTES)).isoformat(),
            **(extra or {}),
        }
        action = m.Action(
            id=new_id("action"),
            case_id=case.id,
            type=action_type,
            payload_json={"application_id": app.id, "application": payload, "review": review},
            payload_hash=payload_hash,
            status="proposed",
            idempotency_key=idempotency_key,
            challenge_id=new_id("challenge"),
            challenge_expires_at=now + timedelta(minutes=CHALLENGE_TTL_MINUTES),
            expected_case_version=case.version,
            application_id=app.id,
            created_at=now,
            updated_at=now,
        )
        session.add(action)
        session.flush()
        return action

    def _prepared_response(self, session: Session, case: m.Case, app: m.Application, action: m.Action) -> Dict[str, Any]:
        return {
            "case_id": case.id,
            "status": case.state,
            "version": case.version,
            "expected_case_version": case.version,
            "application": self.cases._application_view(app),
            "action": self.cases._action_view(action, session),
            "approval_challenge_id": action.challenge_id,
            "action_payload_hash": action.payload_hash,
            "review": action.payload_json["review"],
        }

    # ------------------------------------------------------------------ approve / reject
    def approve_action(self, action_id: str, approver_id: str, expected_case_version: int, action_payload_hash: str, approval_challenge_id: str) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            if action is None:
                raise CaseError("action not found", 404)
            case = repo.get_case(session, action.case_id)
            if case.customer_id != approver_id:
                raise CaseError("action not found", 404)
            if action.status != "proposed":
                raise CaseError("action is %s and cannot be approved" % action.status, 409)
            if action.challenge_id != approval_challenge_id:
                raise CaseError("approval challenge does not match this action", 409)
            expired = now > action.challenge_expires_at
        if expired:
            # Persist the invalidation in its own transaction before rejecting the request.
            with self.db.session() as session:
                action = session.get(m.Action, action_id)
                case = repo.get_case(session, action.case_id)
                action.status = "invalidated"
                action.result_json = {"reason": "approval challenge expired"}
                action.updated_at = now
                record_event(session, case, "insurance.action.invalidated", "system", now, self._env(), {"action_id": action.id, "reason": "challenge expired"})
            raise CaseError("approval challenge expired; prepare the application again", 410)
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            case = repo.get_case(session, action.case_id)
            if action.payload_hash != action_payload_hash:
                raise CaseError("action payload has changed since it was reviewed", 409, {"expected_hash": action.payload_hash})
            if expected_case_version != case.version:
                raise CaseError("case version is stale", 409, {"current_version": case.version})
            if case.state != st.AWAITING_APPROVAL or case.current_application_id != action.application_id:
                raise CaseError("case is no longer awaiting approval for this action", 409)
            terms = action.payload_json["application"]["terms"]
            approval = m.Approval(
                id=new_id("approval"),
                action_id=action.id,
                approver_id=approver_id,
                action_hash=action.payload_hash,
                scope_json={
                    "action_type": action.type,
                    "insurer_id": terms["insurer_id"],
                    "product": terms["product"],
                    "annual_premium_minor": terms["annual_premium_minor"],
                    "currency": terms["currency"],
                    "property_limit_minor": terms["property_limit_minor"],
                    "liability_limit_minor": terms["liability_limit_minor"],
                    "deductible_minor": terms["deductible_minor"],
                    "effective_date": terms["effective_date"],
                    "document_disclosure": action.payload_json["application"]["disclosed_document_ids"],
                },
                expires_at=now + timedelta(minutes=self.ctx.settings.approval_ttl_minutes),
                created_at=now,
            )
            session.add(approval)
            session.flush()
            action.status = "approved"
            action.approval_id = approval.id
            action.updated_at = now
            app = session.get(m.Application, action.application_id)
            app.status = "approved"
            app.updated_at = now
            record_event(session, case, "insurance.action.approved", "customer:" + approver_id, now, self._env(),
                         {"action_id": action.id, "approval_id": approval.id, "action_type": action.type, "payload_hash": action.payload_hash})
            enqueue_job(session, "execute_action", {"action_id": action.id}, now, dedupe_key="execute_action:" + action.id)
            session.flush()
            return {"action_id": action.id, "status": action.status, "approval_id": approval.id, "case_version": case.version, "expires_at": approval.expires_at.isoformat()}

    def reject_action(self, action_id: str, approver_id: str, reason: Optional[str]) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            if action is None:
                raise CaseError("action not found", 404)
            case = repo.get_case(session, action.case_id)
            if case.customer_id != approver_id:
                raise CaseError("action not found", 404)
            if action.status not in ("proposed", "approved"):
                raise CaseError("action is %s" % action.status, 409)
            action.status = "rejected"
            action.result_json = {"reason": reason or "rejected by customer"}
            action.updated_at = now
            if action.approval_id:
                approval = session.get(m.Approval, action.approval_id)
                if approval and approval.consumed_at is None:
                    approval.revoked_at = now
            app = session.get(m.Application, action.application_id)
            if app:
                app.status = "rejected"
                app.updated_at = now
            record_event(session, case, "insurance.action.rejected", "customer:" + approver_id, now, self._env(), {"action_id": action.id, "reason": reason})
            if case.state in (st.AWAITING_APPROVAL, st.REVISED_OFFER):
                transition(session, case, st.AWAITING_SELECTION, "customer:" + approver_id, "insurance.case.awaiting_selection", now, self._env())
            return {"action_id": action.id, "status": action.status, "case_status": case.state}

    # ------------------------------------------------------------------ execute (worker)
    def _verify_authority(self, session: Session, action: m.Action, case: m.Case, now: datetime) -> Optional[str]:
        if action.status != "approved":
            return "action is %s" % action.status
        approval = session.get(m.Approval, action.approval_id) if action.approval_id else None
        if approval is None:
            return "no approval bound to action"
        if approval.revoked_at is not None:
            return "approval revoked"
        if approval.consumed_at is not None:
            return "approval already consumed"
        if now > approval.expires_at:
            return "approval expired"
        if approval.action_hash != action.payload_hash:
            return "approval hash does not match action payload"
        if approval.approver_id != case.customer_id:
            return "approver is not the case owner"
        if case.state != st.AWAITING_APPROVAL or case.current_application_id != action.application_id:
            return "case is no longer awaiting approval for this action"
        app = session.get(m.Application, action.application_id)
        if app is None or app.status != "approved" or app.payload_hash != action.payload_hash:
            return "application changed since approval"
        return None

    async def execute_action(self, action_id: str) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            if action is None:
                return {"skipped": "missing"}
            case = repo.get_case(session, action.case_id)
            if action.status in ("executed", "executing", "uncertain"):
                return {"skipped": action.status}
            problem = self._verify_authority(session, action, case, now)
            if problem:
                action.status = "invalidated"
                action.result_json = {"reason": problem}
                action.updated_at = now
                app = session.get(m.Application, action.application_id)
                if app and app.status == "approved":
                    app.status = "prepared"
                record_event(session, case, "insurance.action.invalidated", "executor", now, self._env(), {"action_id": action.id, "reason": problem})
                if case.state == st.AWAITING_APPROVAL:
                    transition(session, case, st.AWAITING_SELECTION, "executor", "insurance.case.awaiting_selection", now, self._env(), {"reason": problem})
                return {"invalidated": problem}
            approval = session.get(m.Approval, action.approval_id)
            approval.consumed_at = now
            approval.consumed_by = "executor"
            action.status = "executing"
            action.updated_at = now
            app = session.get(m.Application, action.application_id)
            payload = dict(action.payload_json["application"])
            action_type, insurer_id, case_id = action.type, payload["insurer_id"], case.id
            submission_ref = app.submission_ref
            record_event(session, case, "insurance.action.executing", "executor", now, self._env(), {"action_id": action.id, "action_type": action.type})

        adapter = self.registry.get(insurer_id)
        try:
            if action_type == "submit_application":
                result = await self.ctx.call_provider(case_id, insurer_id, "submit_application", adapter.submit_application(payload, action_id),
                                                      request_ref=action_id, request={"quote_ref": payload["quote_ref"], "revision": payload["revision"]})
            elif action_type == "accept_revised_offer":
                result = await self.ctx.call_provider(
                    case_id, insurer_id, "accept_revised_offer",
                    adapter.accept_revised_offer(submission_ref, payload["quote_ref"], payload["quote_version"], action_id),
                    request_ref=action_id, request={"submission_ref": submission_ref, "quote_version": payload["quote_version"]},
                )
            else:
                raise ValueError("unsupported action type %s" % action_type)
        except ProviderTimeout as exc:
            return self._action_uncertain(action_id, str(exc))
        except (ProviderError, ProviderMalformedResponse, KeyError, ValueError) as exc:
            return self._action_failed(action_id, str(exc))
        return self._action_executed(action_id, result)

    def _action_executed(self, action_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            case = repo.get_case(session, action.case_id)
            app = session.get(m.Application, action.application_id)
            action.status = "executed"
            action.provider_ref = result.get("submission_ref")
            action.result_json = {"status": result.get("status"), "source": result.get("source")}
            action.updated_at = now
            app.submission_ref = result.get("submission_ref")
            app.status = "submitted"
            app.updated_at = now
            event = "insurance.application.submitted" if action.type == "submit_application" else "insurance.application.revised_offer_accepted"
            if case.state == st.AWAITING_APPROVAL:
                transition(session, case, st.SUBMITTED, "executor", event, now, self._env(),
                           {"action_id": action.id, "application_id": app.id, "submission_ref": app.submission_ref, "source": result.get("source")})
            if result.get("status") == "underwriting" and case.state == st.SUBMITTED:
                transition(session, case, st.UNDERWRITING, "provider:" + app.payload_json["insurer_id"], "insurance.application.underwriting", now, self._env(),
                           {"submission_ref": app.submission_ref, "underwriting_completes_at": result.get("underwriting_completes_at")})
                app.status = "underwriting"
            enqueue_job(session, "poll_underwriting", {"case_id": case.id}, now + timedelta(seconds=1), dedupe_key="poll_underwriting:" + case.id)
            return {"executed": True, "submission_ref": app.submission_ref, "case_status": case.state}

    def _action_uncertain(self, action_id: str, error: str) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            case = repo.get_case(session, action.case_id)
            action.status = "uncertain"
            action.result_json = {"error": error, "outcome": "unknown", "reconcile_attempts": 0}
            action.updated_at = now
            record_event(session, case, "insurance.action.uncertain", "executor", now, self._env(), {"action_id": action.id, "error": error[:200]})
            if case.state == st.AWAITING_APPROVAL:
                transition(session, case, st.SUBMITTED, "executor", "insurance.application.submitted_unconfirmed", now, self._env(), {"action_id": action.id, "outcome": "unknown"})
            caps = self.registry.get(action.payload_json["application"]["insurer_id"]).capabilities()
            if caps.status_lookup_by_request_ref:
                enqueue_job(session, "reconcile_action", {"action_id": action.id}, now + timedelta(seconds=2), dedupe_key="reconcile_action:" + action.id)
            else:
                case.review_reason = "Provider write outcome unknown and the adapter cannot look up status by request reference."
                transition(session, case, st.MANUAL_REVIEW, "executor", "insurance.case.manual_review", now, self._env(), {"reason": case.review_reason, "action_id": action.id})
            return {"uncertain": True}

    def _action_failed(self, action_id: str, error: str) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            case = repo.get_case(session, action.case_id)
            action.status = "failed"
            action.result_json = {"error": error}
            action.updated_at = now
            app = session.get(m.Application, action.application_id)
            if app:
                app.status = "rejected"
            case.review_reason = "Provider rejected the approved action: %s" % error[:200]
            record_event(session, case, "insurance.action.failed", "executor", now, self._env(), {"action_id": action.id, "error": error[:200]})
            transition(session, case, st.MANUAL_REVIEW, "executor", "insurance.case.manual_review", now, self._env(), {"reason": case.review_reason})
            return {"failed": error}

    async def reconcile_action(self, action_id: str) -> Dict[str, Any]:
        """Before retrying an uncertain write, ask the provider what happened to the original request."""
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            if action is None or action.status != "uncertain":
                return {"skipped": True}
            case = repo.get_case(session, action.case_id)
            insurer_id, case_id = action.payload_json["application"]["insurer_id"], case.id
            attempts = int((action.result_json or {}).get("reconcile_attempts", 0)) + 1
            action.result_json = {**(action.result_json or {}), "reconcile_attempts": attempts}
        adapter = self.registry.get(insurer_id)
        try:
            status = await self.ctx.call_provider(case_id, insurer_id, "get_policy_status", adapter.get_policy_status(action_id), request_ref=action_id)
        except (ProviderError, KeyError, ValueError) as exc:
            return self._reconcile_retry_or_review(action_id, attempts, str(exc))
        if status.get("status") == "not_found":
            # The provider never recorded the write: re-issuing with the SAME request reference is safe.
            with self.db.session() as session:
                action = session.get(m.Action, action_id)
                action.status = "approved"
                approval = session.get(m.Approval, action.approval_id)
                approval.consumed_at = None
                case = repo.get_case(session, action.case_id)
                if case.state == st.SUBMITTED:
                    # Back to awaiting approval so the executor's authority check passes; approval is still valid.
                    case.state = st.AWAITING_APPROVAL
                record_event(session, case, "insurance.action.reconciled_not_found", "executor", self.now(), self._env(), {"action_id": action.id, "attempt": attempts})
                enqueue_job(session, "execute_action", {"action_id": action.id}, self.now(), dedupe_key="execute_action:%s:retry%d" % (action.id, attempts))
            return {"retry": True}
        # The provider did record it: mark executed and let the underwriting poller take over.
        self._action_executed(action_id, {"submission_ref": status.get("submission_ref"), "status": "underwriting" if status.get("status") == "underwriting" else status.get("status"), "source": status.get("source")})
        return {"reconciled": status.get("status")}

    def _reconcile_retry_or_review(self, action_id: str, attempts: int, error: str) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            action = session.get(m.Action, action_id)
            case = repo.get_case(session, action.case_id)
            if attempts >= MAX_UNCERTAIN_RECONCILE_ATTEMPTS:
                case.review_reason = "Provider write outcome could not be resolved after %d checks: %s" % (attempts, error[:160])
                if case.state != st.MANUAL_REVIEW:
                    transition(session, case, st.MANUAL_REVIEW, "executor", "insurance.case.manual_review", now, self._env(), {"reason": case.review_reason, "action_id": action.id})
                return {"manual_review": True}
            enqueue_job(session, "reconcile_action", {"action_id": action.id}, now + timedelta(seconds=min(60, 2 ** attempts)), dedupe_key="reconcile_action:%s:%d" % (action.id, attempts))
            return {"retry_later": attempts}

    # ------------------------------------------------------------------ underwriting polling (worker)
    async def poll_underwriting(self, case_id: str) -> Dict[str, Any]:
        with self.db.session() as session:
            case = repo.get_case(session, case_id)
            if case.state not in (st.SUBMITTED, st.UNDERWRITING):
                return {"skipped": case.state}
            app = session.get(m.Application, case.current_application_id)
            submit_action = next((a for a in repo.actions_for_case(session, case.id) if a.type == "submit_application" and a.status == "executed" and a.application_id in
                                  {x.id for x in repo.applications_for_case(session, case.id)}), None)
            if submit_action is None:
                return {"skipped": "no executed submission"}
            request_ref, insurer_id = submit_action.id, app.payload_json["insurer_id"]
        adapter = self.registry.get(insurer_id)
        try:
            status = await self.ctx.call_provider(case_id, insurer_id, "get_policy_status", adapter.get_policy_status(request_ref), request_ref=request_ref)
        except (ProviderError, KeyError, ValueError) as exc:
            with self.db.session() as session:
                enqueue_job(session, "poll_underwriting", {"case_id": case_id}, self.now() + timedelta(seconds=10), dedupe_key="poll_underwriting:%s:%s" % (case_id, self.now().isoformat()))
            return {"retry": str(exc)}
        return self._apply_underwriting_status(case_id, status)

    def _apply_underwriting_status(self, case_id: str, status: Dict[str, Any]) -> Dict[str, Any]:
        now = self.now()
        state = status.get("status")
        with self.db.session() as session:
            case = repo.get_case(session, case_id)
            app = session.get(m.Application, case.current_application_id)
            insurer_id = app.payload_json["insurer_id"]
            actor = "provider:" + insurer_id
            if state == "underwriting":
                if case.state == st.SUBMITTED:
                    transition(session, case, st.UNDERWRITING, actor, "insurance.application.underwriting", now, self._env(), {"submission_ref": app.submission_ref})
                    app.status = "underwriting"
                enqueue_job(session, "poll_underwriting", {"case_id": case.id}, now + timedelta(seconds=30), dedupe_key="poll_underwriting:%s:%s" % (case.id, now.isoformat()))
                return {"status": "underwriting", "completes_at": status.get("underwriting_completes_at")}
            if state == "declined":
                app.status = "declined"
                app.updated_at = now
                if case.state == st.SUBMITTED:
                    transition(session, case, st.UNDERWRITING, actor, "insurance.application.underwriting", now, self._env())
                transition(session, case, st.DECLINED, actor, "insurance.application.declined", now, self._env(), {"reason": status.get("decline_reason"), "application_id": app.id})
                return {"status": "declined"}
            if state == "revised_offer":
                return self._handle_revised_offer(session, case, app, status, now)
            if state == "issued":
                return self._handle_issued(session, case, app, status, now)
            case.review_reason = "Unexpected underwriting status %r" % state
            transition(session, case, st.MANUAL_REVIEW, "system", "insurance.case.manual_review", now, self._env(), {"reason": case.review_reason})
            return {"status": "manual_review"}

    def _handle_revised_offer(self, session: Session, case: m.Case, app: m.Application, status: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        revised = RentersQuote.model_validate(status["revised_quote"])
        insurer_id = revised.insurer_id
        old_quote = session.get(m.InsuranceQuote, app.quote_id)
        task = session.get(m.QuoteTask, old_quote.quote_task_id)
        if case.state == st.SUBMITTED:
            transition(session, case, st.UNDERWRITING, "provider:" + insurer_id, "insurance.application.underwriting", now, self._env())
        existing = next((q for q in repo.current_quotes(session, case.id, old_quote.needs_version) if q.insurer_id == insurer_id and q.quote_version == revised.quote_version), None)
        if existing is not None:
            return {"status": "revised_offer", "already_recorded": True}
        new_quote = self.cases._store_quote(session, case, task, revised, now)
        old_quote.superseded_by = new_quote.id
        needs = repo.current_needs(session, case)
        household = household_by_customer(case.customer_id)
        answers = repo.answers_as_records(session, case.id)
        payload = build_application(case.id, case.customer_id, household, needs, revised, self.registry.questions(insurer_id), answers, now, revision=app.revision + 1)
        payload_hash = application_payload_hash(payload)
        new_app = m.Application(
            id=new_id("application"), case_id=case.id, quote_id=new_quote.id, answers_hash=payload["answers_hash"], payload_json=payload, payload_hash=payload_hash,
            submission_ref=app.submission_ref, status="prepared", revision=app.revision + 1, created_at=now, updated_at=now,
        )
        session.add(new_app)
        session.flush()
        app.status = "revised"
        app.updated_at = now
        self.cases._invalidate_open_actions(session, case, "underwriting revised the offer", now)
        premium_delta = revised.annual_premium_minor - old_quote.annual_premium_minor
        action = self._propose_action(
            session, case, "accept_revised_offer", new_app, payload, payload_hash,
            "%s:accept_revised_offer:%s:v%d" % (case.id, revised.quote_ref, revised.quote_version), now,
            extra={"revision_reason": revised.revision_reason, "previous_premium_display": fmt_minor(old_quote.annual_premium_minor), "premium_change_display": ("+" if premium_delta >= 0 else "-") + fmt_minor(abs(premium_delta))},
        )
        case.selected_quote_id = new_quote.id
        case.current_application_id = new_app.id
        transition(session, case, st.REVISED_OFFER, "provider:" + insurer_id, "insurance.application.revised_offer", now, self._env(),
                   {"application_id": new_app.id, "quote_id": new_quote.id, "previous_quote_id": old_quote.id, "requires_new_approval": True,
                    "premium_delta_minor": premium_delta, "reason": revised.revision_reason})
        transition(session, case, st.AWAITING_APPROVAL, "system", "insurance.application.prepared", now, self._env(), {"application_id": new_app.id, "action_id": action.id, "revision": new_app.revision})
        action.expected_case_version = case.version
        return {"status": "revised_offer", "action_id": action.id, "application_id": new_app.id}

    def _handle_issued(self, session: Session, case: m.Case, app: m.Application, status: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        declarations = status.get("policy") or {}
        insurer_id = app.payload_json["insurer_id"]
        actor = "provider:" + insurer_id
        if case.state == st.SUBMITTED:
            transition(session, case, st.UNDERWRITING, actor, "insurance.application.underwriting", now, self._env())
        existing = [p for p in repo.policies_for_case(session, case.id) if p.application_id == app.id]
        if existing:
            return {"status": "issued", "already_recorded": True}
        document = m.Document(
            id=new_id("doc"), owner_id=case.customer_id, case_id=case.id, object_key="declarations/%s/%s.json" % (case.id, declarations.get("insurer_policy_ref")),
            content_hash=sha256_hash(declarations), source="provider:" + insurer_id, captured_at=now, extraction_version="declarations/v1", content_json=declarations,
        )
        session.add(document)
        verification = verify_policy(app.payload_json, declarations, today=now.date())
        effective = _parse_date(declarations.get("effective_at"))
        policy = m.IssuedPolicy(
            id=new_id("policy"), case_id=case.id, application_id=app.id, insurer_policy_ref=declarations.get("insurer_policy_ref"), effective_at=effective,
            expires_at=_parse_date(declarations.get("expires_at")), declarations_document_id=document.id, declarations_json=declarations,
            verified_against_quote_id=app.quote_id, verification_json=verification, verified=verification["verified"], created_at=now,
        )
        session.add(policy)
        session.flush()
        app.status = "bound"
        app.updated_at = now
        transition(session, case, st.BOUND, actor, "insurance.policy.bound", now, self._env(), {"insurer_policy_ref": policy.insurer_policy_ref, "source": status.get("source")})
        transition(session, case, st.ISSUED, actor, "insurance.policy.issued", now, self._env(),
                   {"policy_id": policy.id, "insurer_policy_ref": policy.insurer_policy_ref, "effective_at": declarations.get("effective_at"), "document_id": document.id,
                    "coverage_starts_in_future": verification["coverage_starts_in_future"]})
        if verification["verified"]:
            case.completion_evidence_ref = policy.id
            transition(session, case, st.COMPLETED, "verifier", "insurance.policy.verified", now, self._env(), {"policy_id": policy.id, "verified_against_quote_id": app.quote_id})
            transition(session, case, st.COMPLETED, "verifier", "insurance.case.completed", now, self._env(), {"completion_evidence_ref": policy.id})
            return {"status": "completed", "policy_id": policy.id}
        case.review_reason = "Issued policy does not match approved terms: " + "; ".join(mm["detail"] for mm in verification["mismatches"])
        transition(session, case, st.MANUAL_REVIEW, "verifier", "insurance.policy.mismatch", now, self._env(), {"policy_id": policy.id, "mismatches": verification["mismatches"]})
        record_event(session, case, "insurance.case.manual_review", "verifier", now, self._env(), {"reason": case.review_reason})
        return {"status": "manual_review", "mismatches": verification["mismatches"]}

    # ------------------------------------------------------------------ verification (agent tool)
    def verify_policy(self, application_id: str, customer_id: Optional[str]) -> Dict[str, Any]:
        with self.db.session() as session:
            app = session.get(m.Application, application_id)
            if app is None:
                raise CaseError("application not found", 404)
            case = repo.get_case(session, app.case_id)
            if customer_id and case.customer_id != customer_id:
                raise CaseError("application not found", 404)
            policies = [p for p in repo.policies_for_case(session, case.id) if p.application_id == app.id]
            if not policies:
                return {
                    "application_id": app.id,
                    "application_status": app.status,
                    "case_status": case.state,
                    "issued": False,
                    "verified": False,
                    "message": "No issued policy has been received for this application. A quote or a submitted application is not an issued policy.",
                }
            policy = policies[-1]
            return {
                "application_id": app.id,
                "application_status": app.status,
                "case_status": case.state,
                "issued": True,
                "verified": policy.verified,
                "insurer_policy_ref": policy.insurer_policy_ref,
                "effective_at": policy.effective_at.isoformat() if policy.effective_at else None,
                "verification": policy.verification_json,
                "declarations_document_id": policy.declarations_document_id,
                "source": policy.declarations_json.get("source") or {"environment": self._env(), "authority": "simulated"},
            }

    # ------------------------------------------------------------------ expiry (worker)
    def expire_stale_quotes(self) -> int:
        now = self.now()
        expired_cases = 0
        with self.db.session() as session:
            for case in repo.all_cases(session):
                if case.state not in (st.AWAITING_SELECTION, st.AWAITING_APPROVAL, st.COMPARING):
                    continue
                needs = repo.current_needs(session, case)
                quotes = repo.current_quotes(session, case.id, needs.version)
                if not quotes:
                    continue
                live = [q for q in quotes if q.valid_until >= now]
                for q in quotes:
                    if q.valid_until < now and q.status != "expired":
                        q.status = "expired"
                        record_event(session, case, "insurance.quote.expired", "timer", now, self._env(), {"quote_id": q.id, "insurer_id": q.insurer_id})
                if not live:
                    self.cases._invalidate_open_actions(session, case, "all quotes expired", now)
                    transition(session, case, st.EXPIRED, "timer", "insurance.case.expired", now, self._env())
                    expired_cases += 1
        return expired_cases


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
