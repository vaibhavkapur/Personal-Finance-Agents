"""Inbound provider events: signature verification, deduplication (provider + event id), out-of-order protection and
idempotent state updates. Consumers are safe to repeat."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock, parse_iso
from ..config import Settings
from ..domain.decision import explain_decision
from ..ids import canonical_json, new_id
from ..persistence.models import ClaimCase, ClaimDecision, ClaimPayment, ClaimSubmission, InboxEvent, InsurerRequest
from .case_service import CaseService
from .errors import Forbidden
from .outbox import enqueue_job, enqueue_outbox
from .state_machine import TransitionError, record_event, transition

CLAIMS_PROVIDER = "mock_insurer"
PAYMENTS_PROVIDER = "mock_payments"


def verify_signature(secret: str, payload: Dict[str, Any], signature: Optional[str]) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.split("=", 1)[1])


class ProviderEventProcessor:
    def __init__(self, db, clock: Clock, settings: Settings, cases: CaseService):
        self.db = db
        self.clock = clock
        self.settings = settings
        self.cases = cases

    # ------------------------------------------------------------------ claims
    def handle_claim_event(self, payload: Dict[str, Any], signature: Optional[str], provider: str = CLAIMS_PROVIDER) -> Dict[str, Any]:
        if not verify_signature(self.settings.provider_webhook_secret, payload, signature):
            raise Forbidden("invalid provider signature")
        event_id = str(payload.get("id"))
        with self.db.session() as s:
            existing = s.scalars(select(InboxEvent).where(InboxEvent.provider == provider, InboxEvent.provider_event_id == event_id)).first()
            if existing:
                existing.duplicate_deliveries = int(existing.duplicate_deliveries or 0) + 1
                return {"event_id": event_id, "outcome": "duplicate", "first_outcome": existing.outcome}
            inbox = InboxEvent(id=new_id("in"), provider=provider, provider_event_id=event_id, event_type=str(payload.get("type")), payload_json=payload, outcome="processed", received_at=self.clock.now_iso())
            s.add(inbox)
            claim_ref = payload.get("claim_reference")
            case = s.scalars(select(ClaimCase).where(ClaimCase.external_claim_ref == claim_ref)).first()
            if not case:
                # keep it: the claim reference may become known once an uncertain submission is resolved
                inbox.outcome = "unmatched"
                return {"event_id": event_id, "outcome": inbox.outcome}
            return self._process_matched(s, inbox, case, payload, provider)

    def _process_matched(self, s: Session, inbox: InboxEvent, case: ClaimCase, payload: Dict[str, Any], provider: str) -> Dict[str, Any]:
        event_id = inbox.provider_event_id
        claim_ref = payload.get("claim_reference")
        sequence = int(payload.get("sequence") or 0)
        last_seq = self._last_sequence(s, provider, claim_ref, exclude_id=event_id)
        if sequence and sequence < last_seq:
            inbox.outcome = "ignored_out_of_order"
            record_event(s, self.clock, case, "provider.event_ignored", provider, {"type": payload.get("type"), "sequence": sequence, "last_sequence": last_seq}, source_event_id=event_id, bump_version=False)
            return {"event_id": event_id, "outcome": inbox.outcome}
        outcome = self._apply_claim_event(s, case, payload, event_id, provider)
        inbox.outcome = outcome
        return {"event_id": event_id, "outcome": outcome, "case_id": case.id, "case_status": case.status}

    def replay_unmatched(self) -> int:
        """Re-apply stored events whose claim reference has since been attached to a case (e.g. after an uncertain write resolved)."""
        replayed = 0
        with self.db.session() as s:
            rows = s.scalars(select(InboxEvent).where(InboxEvent.outcome == "unmatched")).all()
            rows.sort(key=lambda r: (r.payload_json.get("claim_reference") or "", int(r.payload_json.get("sequence") or 0), r.received_at))
            for row in rows:
                case = s.scalars(select(ClaimCase).where(ClaimCase.external_claim_ref == row.payload_json.get("claim_reference"))).first()
                if not case:
                    continue
                self._process_matched(s, row, case, row.payload_json, row.provider)
                replayed += 1
        return replayed

    def _last_sequence(self, s: Session, provider: str, claim_ref: str, exclude_id: str) -> int:
        rows = s.scalars(select(InboxEvent).where(InboxEvent.provider == provider, InboxEvent.outcome == "processed")).all()
        seqs = [int(r.payload_json.get("sequence") or 0) for r in rows if r.payload_json.get("claim_reference") == claim_ref and r.provider_event_id != exclude_id]
        return max(seqs) if seqs else 0

    def _apply_claim_event(self, s: Session, case: ClaimCase, payload: Dict[str, Any], event_id: str, provider: str) -> str:
        etype = payload.get("type")
        data = payload.get("data") or {}
        if etype == "claim.received":
            record_event(s, self.clock, case, "insurer.received", provider, data, source_event_id=event_id, bump_version=False)
            return "processed"
        if etype == "claim.under_review":
            if case.status == "submitted":
                transition(s, self.clock, case, "under_review", provider, "insurer.under_review", data, source_event_id=event_id)
            else:
                record_event(s, self.clock, case, "insurer.under_review", provider, data, source_event_id=event_id, bump_version=False)
            return "processed"
        if etype == "claim.evidence_requested":
            if case.status == "submitted":
                transition(s, self.clock, case, "under_review", provider, "insurer.under_review", {}, source_event_id=event_id)
            existing = s.scalars(select(InsurerRequest).where(InsurerRequest.case_id == case.id, InsurerRequest.provider_request_id == data.get("request_id"))).first()
            if existing:
                return "processed:request_already_known"
            req = InsurerRequest(
                id=new_id("req"), case_id=case.id, provider_request_id=str(data.get("request_id")), requirement_json={"document_type": data.get("document_type"), "message": data.get("message")},
                due_at=data.get("due_at"), deadline_source=data.get("deadline_source", f"{provider} request"), status="open", created_at=self.clock.now_iso(),
            )
            s.add(req)
            if case.status == "under_review":
                transition(s, self.clock, case, "evidence_requested", provider, "insurer.evidence_requested", {"request_id": req.provider_request_id, "document_type": data.get("document_type"), "due_at": req.due_at}, source_event_id=event_id)
            else:
                record_event(s, self.clock, case, "insurer.evidence_requested", provider, {"request_id": req.provider_request_id, "note": f"case in {case.status}"}, source_event_id=event_id)
            if req.due_at:
                enqueue_job(s, self.clock, "deadline_check", {"case_id": case.id, "request_id": req.id}, run_at=parse_iso(req.due_at), dedupe_key=f"deadline:{req.id}")
            enqueue_outbox(s, self.clock, case_id=case.id, event_type="claim.evidence_requested", data={"claim_reference": case.external_claim_ref, "request_id": req.provider_request_id, "document_type": data.get("document_type")}, environment=self.settings.environment)
            return "processed"
        if etype == "claim.request_satisfied":
            req = s.scalars(select(InsurerRequest).where(InsurerRequest.case_id == case.id, InsurerRequest.provider_request_id == data.get("request_id"))).first()
            if req and req.status == "open":
                req.status = "satisfied"
                latest_sub = s.scalars(select(ClaimSubmission).where(ClaimSubmission.case_id == case.id).order_by(ClaimSubmission.sequence.desc())).first()
                req.satisfied_by_submission_id = latest_sub.id if latest_sub else None
            record_event(s, self.clock, case, "insurer.request_satisfied", provider, data, source_event_id=event_id, bump_version=False)
            return "processed"
        if etype == "claim.decided":
            return self._apply_decision(s, case, data, event_id, provider)
        record_event(s, self.clock, case, "provider.event_unhandled", provider, {"type": etype}, source_event_id=event_id, bump_version=False)
        return "processed:unhandled_type"

    def _apply_decision(self, s: Session, case: ClaimCase, data: Dict[str, Any], event_id: str, provider: str) -> str:
        version = int(data.get("decision_version") or 1)
        if s.scalars(select(ClaimDecision).where(ClaimDecision.case_id == case.id, ClaimDecision.decision_version == version)).first():
            return "processed:decision_already_stored"
        outcome = data.get("outcome")
        if outcome not in ("approved", "partially_approved", "denied"):
            record_event(s, self.clock, case, "provider.event_rejected", provider, {"reason": "unknown outcome", "outcome": outcome}, source_event_id=event_id, bump_version=False)
            return "rejected:unknown_outcome"
        decision = ClaimDecision(
            id=new_id("dec"), case_id=case.id, decision_version=version, outcome=outcome, accepted_minor=int(data.get("accepted_minor", 0)), rejected_minor=int(data.get("rejected_minor", 0)), currency=data.get("currency", "USD"),
            reason_items_json={"items": data.get("reason_items", [])}, evidence_id=event_id, provider_decision_ref=str(data.get("decision_ref", "")), decided_at=data.get("decided_at") or self.clock.now_iso(), created_at=self.clock.now_iso(),
        )
        s.add(decision)
        s.flush()
        # explanation with policy references, stored independently of payment state
        policy = self.cases.policy_for_case(s, case)
        expenses = {e["id"]: e for e in self.cases._expense_dicts(self.cases._expenses(s, case.id))}
        docs = [d.doc_type for d in self.cases._case_docs(s, case.id)]
        decision.explanation_json = explain_decision(policy, self.cases._decision_dict(decision), case.evaluation_json or {}, expenses, docs, self.cases.prior_challenges(s, case.id))
        if case.status == "submitted":
            transition(s, self.clock, case, "under_review", provider, "insurer.under_review", {}, source_event_id=event_id)
        if case.status == "under_review":
            transition(s, self.clock, case, outcome, provider, "insurer.decided", {"decision_id": decision.id, "decision_version": version, "accepted_minor": decision.accepted_minor, "rejected_minor": decision.rejected_minor, "summary": decision.explanation_json["summary"]}, source_event_id=event_id)
            if outcome == "approved":
                transition(s, self.clock, case, "payout_pending", provider, "case.payout_pending", {"decision_id": decision.id})
                self.cases._reconcile_locked(s, case, provider)
        else:
            record_event(s, self.clock, case, "insurer.decided", provider, {"decision_id": decision.id, "note": f"case in {case.status}; decision stored without transition"}, source_event_id=event_id)
        enqueue_outbox(s, self.clock, case_id=case.id, event_type="claim.decided", data={"claim_reference": case.external_claim_ref, "decision_version": version, "outcome": outcome, "accepted_minor": decision.accepted_minor}, environment=self.settings.environment)
        return "processed"

    # ------------------------------------------------------------------ payments
    def handle_payment_event(self, payload: Dict[str, Any], signature: Optional[str], provider: str = PAYMENTS_PROVIDER) -> Dict[str, Any]:
        if not verify_signature(self.settings.provider_webhook_secret, payload, signature):
            raise Forbidden("invalid provider signature")
        event_id = str(payload.get("id"))
        data = payload.get("data") or {}
        with self.db.session() as s:
            if s.scalars(select(InboxEvent).where(InboxEvent.provider == provider, InboxEvent.provider_event_id == event_id)).first():
                return {"event_id": event_id, "outcome": "duplicate"}
            inbox = InboxEvent(id=new_id("in"), provider=provider, provider_event_id=event_id, event_type=str(payload.get("type")), payload_json=payload, outcome="processed", received_at=self.clock.now_iso())
            s.add(inbox)
            ref = str(data.get("payment_ref"))
            if s.scalars(select(ClaimPayment).where(ClaimPayment.provider_payment_ref == ref)).first():
                inbox.outcome = "duplicate_payment_ref"
                return {"event_id": event_id, "outcome": inbox.outcome}
            case = s.scalars(select(ClaimCase).where(ClaimCase.external_claim_ref == data.get("claim_reference"))).first()
            payment = ClaimPayment(
                id=new_id("pay"), case_id=case.id if case else None, provider_payment_ref=ref, claim_reference=str(data.get("claim_reference")), payee_id=str(data.get("payee_id")), amount_minor=int(data.get("amount_minor", 0)), currency=str(data.get("currency", "USD")),
                payment_status=str(data.get("status", "posted")), posted_at=str(data.get("posted_at") or payload.get("occurred_at")), reconciliation_status="unreconciled", environment=str(payload.get("environment", "mock")), created_at=self.clock.now_iso(),
            )
            s.add(payment)
            s.flush()
            if not case:
                payment.reconciliation_status = "unmatched:unknown_claim_reference"
                inbox.outcome = "processed:unmatched"
                return {"event_id": event_id, "outcome": inbox.outcome}
            result = self.cases._reconcile_locked(s, case, provider)
            enqueue_outbox(s, self.clock, case_id=case.id, event_type="claim.payment_reconciled", data={"payment_ref": ref, "status": result["status"], "outstanding_minor": result["outstanding_minor"]}, environment=self.settings.environment)
            return {"event_id": event_id, "outcome": "processed", "case_id": case.id, "case_status": case.status, "settlement": result}
