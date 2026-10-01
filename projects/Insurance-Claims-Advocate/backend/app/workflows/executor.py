"""Action executor. Persists the pending action first, calls the provider outside any transaction, then reconciles the
result. A timeout means the outcome is unknown until checked by the original request reference."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import func, select

from ..adapters.base import ProviderDeclined, ProviderMalformedResponse, ProviderNotConfigured, ProviderTimeout
from ..clock import Clock
from ..config import Settings
from ..ids import new_id
from ..persistence.models import Action, Approval, ClaimCase, ClaimSubmission, InsurerRequest, Packet, ProviderRequestLog
from . import approvals as approvals_mod
from .errors import Forbidden
from .outbox import enqueue_job, enqueue_outbox
from .state_machine import TransitionError, record_event, transition

SUBMISSION_KIND = {"submit_claim": "initial", "add_evidence": "supplemental", "submit_appeal": "appeal"}
MAX_ATTEMPTS = 3


class ActionExecutor:
    def __init__(self, db, clock: Clock, settings: Settings, adapter):
        self.db = db
        self.clock = clock
        self.settings = settings
        self.adapter = adapter

    def _log(self, s, case_id: Optional[str], operation: str, request_ref: Optional[str], outcome: str, detail: Dict[str, Any]) -> None:
        s.add(ProviderRequestLog(id=new_id("plog"), case_id=case_id, adapter=self.adapter.capabilities.name, operation=operation, request_ref=request_ref, outcome=outcome, environment=self.adapter.capabilities.environment, detail_json=detail, created_at=self.clock.now_iso()))

    async def execute(self, action_id: str) -> Dict[str, Any]:
        # 1. persist the pending action (own transaction)
        with self.db.session() as s:
            action = s.get(Action, action_id)
            if not action:
                return {"action_id": action_id, "outcome": "missing"}
            case = s.get(ClaimCase, action.case_id)
            if action.status not in ("approved",):
                return {"action_id": action_id, "outcome": f"skipped:{action.status}"}
            try:
                approval = approvals_mod.verify_for_execution(s, self.clock, action, case)
            except Forbidden as exc:
                action.status = "failed"
                action.result_json = {"error": str(exc)}
                action.updated_at = self.clock.now_iso()
                record_event(s, self.clock, case, "action.blocked", "executor", {"action_id": action.id, "reason": str(exc)})
                return {"action_id": action_id, "outcome": "blocked", "reason": str(exc)}
            approval.consumed_at = self.clock.now_iso()
            approval.consumed_by = action.id
            action.status = "executing"
            action.attempts += 1
            action.updated_at = self.clock.now_iso()
            packet = s.get(Packet, action.packet_id).content_json
            action_type = action.action_type
            request_ref = action.idempotency_key
            claim_ref = case.external_claim_ref
            case_id = case.id
            record_event(s, self.clock, case, "action.executing", "executor", {"action_id": action.id, "attempt": action.attempts, "request_ref": request_ref}, bump_version=False)

        # 2. provider call, outside the transaction
        try:
            if action_type == "submit_claim":
                result = await self.adapter.submit_claim(packet, request_ref)
            else:
                result = await self.adapter.add_evidence(claim_ref, packet, request_ref)
        except ProviderDeclined as exc:
            return self._record_declined(action_id, case_id, request_ref, str(exc))
        except (ProviderTimeout, ProviderMalformedResponse) as exc:
            return self._record_uncertain(action_id, case_id, request_ref, type(exc).__name__, str(exc))
        except ProviderNotConfigured as exc:
            return self._record_declined(action_id, case_id, request_ref, f"adapter not configured: {exc}")
        except Exception as exc:  # unknown failure => outcome unknown
            return self._record_uncertain(action_id, case_id, request_ref, type(exc).__name__, str(exc))
        return self._record_success(action_id, case_id, request_ref, result)

    # ------------------------------------------------------------------ outcomes
    def _record_success(self, action_id: str, case_id: str, request_ref: str, result: Dict[str, Any]) -> Dict[str, Any]:
        with self.db.session() as s:
            action = s.get(Action, action_id)
            case = s.get(ClaimCase, case_id)
            if action.status == "succeeded":
                return {"action_id": action_id, "outcome": "already_succeeded"}
            self._log(s, case_id, action.action_type, request_ref, "accepted", {"claim_ref": result.get("claim_ref"), "environment": result.get("environment"), "authority": result.get("authority"), "replay": result.get("idempotent_replay", False)})
            action.status = "succeeded"
            action.provider_ref = result.get("claim_ref")
            action.result_json = result
            action.updated_at = self.clock.now_iso()
            seq = int(s.execute(select(func.count(ClaimSubmission.id)).where(ClaimSubmission.case_id == case.id)).scalar() or 0) + 1
            existing = s.scalars(select(ClaimSubmission).where(ClaimSubmission.action_id == action.id)).first()
            if not existing:
                sub = ClaimSubmission(
                    id=new_id("sub"), case_id=case.id, sequence=seq, submission_kind=SUBMISSION_KIND[action.action_type], packet_id=action.packet_id, packet_hash=action.payload_hash, approval_id=action.approval_id, action_id=action.id,
                    external_claim_ref=result.get("claim_ref"), external_submission_ref=result.get("submission_ref"), submitted_at=result.get("received_at") or self.clock.now_iso(), status="accepted", environment=result.get("environment", "unknown"),
                )
                s.add(sub)
                s.flush()
                if action.action_type == "submit_claim":
                    case.external_claim_ref = result.get("claim_ref")
                if action.action_type == "add_evidence":
                    pkt = s.get(Packet, action.packet_id).content_json
                    req_id = (pkt.get("responds_to_request") or {}).get("provider_request_id")
                    if req_id:
                        req = s.scalars(select(InsurerRequest).where(InsurerRequest.case_id == case.id, InsurerRequest.provider_request_id == req_id)).first()
                        if req:
                            req.status = "satisfied"
                            req.satisfied_by_submission_id = sub.id
                if action.action_type == "submit_appeal":
                    case.followups_used = int(case.followups_used or 0) + 1
            try:
                if case.status in ("awaiting_approval", "manual_review"):
                    transition(s, self.clock, case, "submitted", "executor", "claim.submitted", {"action_id": action.id, "sequence": seq, "external_claim_ref": case.external_claim_ref, "environment": result.get("environment")})
                else:
                    record_event(s, self.clock, case, "claim.submitted", "executor", {"action_id": action.id, "sequence": seq, "note": f"case in {case.status}"})
            except TransitionError as exc:
                record_event(s, self.clock, case, "claim.submitted", "executor", {"action_id": action.id, "sequence": seq, "transition_error": str(exc)})
            enqueue_outbox(s, self.clock, case_id=case.id, event_type="claim.submitted", data={"action_id": action.id, "external_claim_ref": case.external_claim_ref, "sequence": seq}, environment=self.settings.environment)
            return {"action_id": action_id, "outcome": "succeeded", "claim_ref": case.external_claim_ref, "case_status": case.status}

    def _record_declined(self, action_id: str, case_id: str, request_ref: str, reason: str) -> Dict[str, Any]:
        with self.db.session() as s:
            action = s.get(Action, action_id)
            case = s.get(ClaimCase, case_id)
            self._log(s, case_id, action.action_type, request_ref, "declined", {"reason": reason})
            action.status = "failed"
            action.result_json = {"error": reason}
            action.updated_at = self.clock.now_iso()
            record_event(s, self.clock, case, "action.declined", "executor", {"action_id": action.id, "reason": reason})
            if case.status == "awaiting_approval":
                transition(s, self.clock, case, "evaluating", "executor", "case.returned_for_review", {"reason": reason})
            return {"action_id": action_id, "outcome": "declined", "reason": reason}

    def _record_uncertain(self, action_id: str, case_id: str, request_ref: str, kind: str, detail: str) -> Dict[str, Any]:
        with self.db.session() as s:
            action = s.get(Action, action_id)
            case = s.get(ClaimCase, case_id)
            self._log(s, case_id, action.action_type, request_ref, "uncertain", {"kind": kind, "detail": detail})
            action.status = "uncertain"
            action.result_json = {"error": detail, "kind": kind}
            action.updated_at = self.clock.now_iso()
            record_event(s, self.clock, case, "action.uncertain", "executor", {"action_id": action.id, "kind": kind, "detail": detail, "next": "lookup by request reference before any retry"})
            enqueue_job(s, self.clock, "resolve_uncertain_action", {"action_id": action.id}, dedupe_key=f"resolve:{action.id}:{action.attempts}")
            return {"action_id": action_id, "outcome": "uncertain", "kind": kind}

    # ------------------------------------------------------------------ recovery
    async def resolve_uncertain(self, action_id: str) -> Dict[str, Any]:
        """Before retrying a write, query by the original request reference. Never generate a new side effect blindly."""
        with self.db.session() as s:
            action = s.get(Action, action_id)
            if not action or action.status not in ("uncertain", "executing"):
                return {"action_id": action_id, "outcome": "nothing_to_resolve"}
            case_id = action.case_id
            request_ref = action.idempotency_key
            attempts = action.attempts
        caps = self.adapter.capabilities
        if not caps.find_submission:
            return self._hold_for_manual_review(action_id, case_id, "adapter cannot look up writes by request reference")
        try:
            found = await self.adapter.find_submission(request_ref)
        except Exception as exc:
            with self.db.session() as s:
                self._log(s, case_id, "find_submission", request_ref, "error", {"detail": str(exc)})
                enqueue_job(s, self.clock, "resolve_uncertain_action", {"action_id": action_id}, dedupe_key=f"resolve:{action_id}:{attempts}:retry")
            return {"action_id": action_id, "outcome": "lookup_failed", "detail": str(exc)}
        with self.db.session() as s:
            self._log(s, case_id, "find_submission", request_ref, "found" if found.get("found") else "not_found", {"environment": found.get("environment")})
        if found.get("found"):
            return self._record_success(action_id, case_id, request_ref, found)
        if attempts >= MAX_ATTEMPTS:
            return self._hold_for_manual_review(action_id, case_id, "provider never recorded the write after repeated attempts")
        # provider has no record: safe to retry the same idempotent request
        with self.db.session() as s:
            action = s.get(Action, action_id)
            case = s.get(ClaimCase, case_id)
            action.status = "approved"
            action.updated_at = self.clock.now_iso()
            record_event(s, self.clock, case, "action.retry_scheduled", "executor", {"action_id": action.id, "attempt": action.attempts + 1}, bump_version=False)
            enqueue_job(s, self.clock, "execute_action", {"action_id": action.id}, dedupe_key=f"execute:{action.id}:{action.attempts + 1}")
        return {"action_id": action_id, "outcome": "retry_scheduled"}

    def _hold_for_manual_review(self, action_id: str, case_id: str, reason: str) -> Dict[str, Any]:
        with self.db.session() as s:
            action = s.get(Action, action_id)
            case = s.get(ClaimCase, case_id)
            action.status = "manual_review"
            action.result_json = dict(action.result_json or {}, hold_reason=reason)
            action.updated_at = self.clock.now_iso()
            try:
                if case.status in ("awaiting_approval", "submitted", "under_review", "payout_pending"):
                    transition(s, self.clock, case, "manual_review", "executor", "case.manual_review", {"action_id": action.id, "reason": reason})
                else:
                    record_event(s, self.clock, case, "case.manual_review_requested", "executor", {"action_id": action.id, "reason": reason})
            except TransitionError:
                record_event(s, self.clock, case, "case.manual_review_requested", "executor", {"action_id": action.id, "reason": reason})
            enqueue_outbox(s, self.clock, case_id=case.id, event_type="claim.manual_review", data={"action_id": action.id, "reason": reason}, environment=self.settings.environment)
        return {"action_id": action_id, "outcome": "manual_review", "reason": reason}
