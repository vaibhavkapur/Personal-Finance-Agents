"""Recovery case workflow.

All calculations, authorization, eligibility rules and state transitions live
here in ordinary application code. The agent only calls the typed tools that
wrap these methods; it cannot set a case to recovered or approve anything.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from ..adapters.base import (
    ProviderDeclined,
    ProviderError,
    ProviderNotFound,
    ProviderResult,
    RecoveryProviderAdapter,
)
from ..adapters.commerce_events import refund_refs_from_documents
from ..clock import Clock, format_ts, parse_ts
from ..config import Settings
from ..domain import policy
from ..domain.deadlines import Deadline, DeadlineConfig, compute_dispute_deadline
from ..domain.errors import Conflict, DomainError, Forbidden, NotFound, PolicyViolation
from ..domain.models import (
    Action,
    ActionStatus,
    ActionType,
    Approval,
    CaseEvent,
    CaseStatus,
    ChannelStatus,
    ChannelType,
    CreditKind,
    CreditMatch,
    DestinationType,
    Document,
    MatchingMethod,
    OutboundPacket,
    PendingQuestion,
    PurchaseRecord,
    ReasonCode,
    RecoveryCase,
    RecoveryChannel,
    RefundPromise,
    SUPPORTED_REASON_CODES,
    Transaction,
)
from ..domain.reconciliation import MatchResult, find_reversal_targets, match_credits
from ..domain.state_machine import assert_transition, is_terminal
from ..domain.totals import Totals, compute_totals
from ..ids import new_id, payload_hash
from ..persistence.outbox import Outbox
from ..persistence.repositories import Repositories
from . import approvals as approval_rules
from .jobs import JobQueue
from .messages import attachment_manifest, dispute_packet, merchant_message

CREDIT_WAIT_DAYS_AFTER_MERCHANT_CLAIM = 10
MIN_MERCHANT_WAIT_DAYS_FOR_DISPUTE = 10
IRREVERSIBLE_MESSAGE = "Sends a message to the merchant's verified support contact with the listed attachments. The message cannot be recalled once sent."
IRREVERSIBLE_DISPUTE = "Opens a dispute with the card issuer for the outstanding amount. Issuers may restrict merchant refunds once a dispute is open; a duplicate recovery will be flagged and must be corrected through the issuer."


class CaseService:
    def __init__(
        self,
        *,
        repos: Repositories,
        clock: Clock,
        outbox: Outbox,
        jobs: JobQueue,
        settings: Settings,
        deadline_config: DeadlineConfig,
        adapters: Dict[str, RecoveryProviderAdapter],
    ) -> None:
        self.repos = repos
        self.clock = clock
        self.outbox = outbox
        self.jobs = jobs
        self.settings = settings
        self.deadline_config = deadline_config
        self.adapters = adapters  # provider name -> adapter
        self.merchant_provider = "merchant_mock"
        self.issuer_provider = "issuer_mock"

    # ------------------------------------------------------------------ utils
    def _now(self) -> str:
        return format_ts(self.clock.now())

    def _event(self, case: RecoveryCase, event_type: str, actor: str, data: Optional[Dict[str, Any]] = None, *, previous: Optional[str] = None, next_: Optional[str] = None, source_event_id: Optional[str] = None, expected_version: Optional[int] = None) -> CaseEvent:
        ev = CaseEvent(
            id=new_id("cev"), case_id=case.id, sequence=self.repos.next_sequence(case.id), event_type=event_type,
            source_event_id=source_event_id, actor=actor, occurred_at=self._now(), previous_state=previous, next_state=next_,
            expected_version=expected_version, data=data or {},
        )
        self.repos.add_case_event(ev)
        return ev

    def _save(self, case: RecoveryCase, actor: str, event_type: str, data: Optional[Dict[str, Any]] = None, *, source_event_id: Optional[str] = None) -> RecoveryCase:
        """Persist a material change (no state change) with an event."""
        updated = self.repos.save_case(case.model_copy(update={"updated_at": self._now()}), expected_version=case.version)
        self._event(updated, event_type, actor, data, source_event_id=source_event_id, expected_version=case.version)
        return updated

    def _transition(self, case: RecoveryCase, target: CaseStatus, actor: str, data: Optional[Dict[str, Any]] = None, *, source_event_id: Optional[str] = None) -> RecoveryCase:
        assert_transition(case.status, target)
        previous = case.status
        updated = self.repos.save_case(case.model_copy(update={"status": target, "updated_at": self._now()}), expected_version=case.version)
        self._event(updated, "case.transitioned", actor, data, previous=previous.value, next_=target.value, source_event_id=source_event_id, expected_version=case.version)
        self.outbox.publish("recovery.state_changed", case.id, {"previous": previous.value, "next": target.value, "version": updated.version})
        return updated

    def _owned_case(self, case_id: str, customer_id: Optional[str]) -> RecoveryCase:
        case = self.repos.get_case(case_id)
        if customer_id is not None and case.customer_id != customer_id:
            raise Forbidden("case belongs to a different customer")
        return case

    def _verified_promise(self, case: RecoveryCase) -> Optional[RefundPromise]:
        if case.promise_id:
            p = self.repos.get_promise(case.promise_id)
            return p if p.verified_at else None
        return None

    def _evidence_docs(self, case: RecoveryCase, purchase: PurchaseRecord) -> List[Document]:
        docs = self.repos.documents_for_customer(case.customer_id)
        ids: Set[str] = set()
        created = next((e for e in self.repos.events_for_case(case.id) if e.event_type == "case.created"), None)
        if created:
            ids.update(created.data.get("evidence_ids", []))
        promise = self._verified_promise(case)
        if promise and promise.evidence_id:
            ids.add(promise.evidence_id)
        out = [d for d in docs if d.id in ids or d.extracted.get("order_ref") == purchase.order_ref]
        seen: Set[str] = set()
        uniq: List[Document] = []
        for d in out:
            if d.id not in seen:
                seen.add(d.id)
                uniq.append(d)
        return uniq

    def totals_for(self, case: RecoveryCase) -> Totals:
        purchase = self.repos.get_purchase(case.purchase_id)
        promise = self._verified_promise(case)
        return compute_totals(
            currency=case.currency, requested_minor=purchase.amount_minor,
            promised_minor=promise.promised_minor if promise else 0, target_minor=case.target_minor,
            matches=self.repos.matches_for_case(case.id), store_credit_minor=case.store_credit_minor,
        )

    # --------------------------------------------------------------- creation
    def create_case(self, *, customer_id: str, order_ref: str, reason_code: str, target_minor: int, currency: str, evidence_ids: List[str], actor: str) -> RecoveryCase:
        purchase = self.repos.find_purchase_by_order(customer_id, order_ref)
        if purchase is None:
            raise NotFound(f"No purchase for order {order_ref} belongs to this customer")
        if currency != purchase.currency:
            raise DomainError(f"Case currency {currency} differs from purchase currency {purchase.currency}", code="currency_mismatch")
        if target_minor <= 0 or target_minor > purchase.amount_minor:
            raise DomainError("target_minor must be positive and not exceed the original purchase", code="invalid_target")
        for doc_id in evidence_ids:
            doc = self.repos.get_document(doc_id)
            if doc.customer_id != customer_id:
                raise Forbidden("evidence belongs to a different customer")
        for existing in self.repos.cases_for_purchase(purchase.id):
            if not is_terminal(existing.status):
                raise Conflict(f"An open case already exists for this purchase: {existing.id}", code="case_exists")
        try:
            reason = ReasonCode(reason_code)
        except ValueError:
            raise DomainError(f"Unknown reason code {reason_code}", code="invalid_reason_code")

        promise = self._pick_promise(purchase, evidence_ids)
        now = self._now()
        case = RecoveryCase(
            id=new_id("recovery"), customer_id=customer_id, purchase_id=purchase.id, promise_id=promise.id if promise else None,
            reason_code=reason, target_minor=target_minor, currency=currency, status=CaseStatus.detected, version=1,
            created_at=now, updated_at=now,
        )
        with self.repos.db.transaction():
            self.repos.add_case(case)
            self._event(case, "case.created", actor, {"order_ref": order_ref, "evidence_ids": evidence_ids, "promise_id": case.promise_id, "target_minor": target_minor, "currency": currency})
            self.outbox.publish("recovery.case_created", case.id, {"order_ref": order_ref, "target_minor": target_minor, "currency": currency})
            if reason not in SUPPORTED_REASON_CODES:
                case = self._transition(case, CaseStatus.investigating, actor)
                case = self._transition(case, CaseStatus.not_supported, actor, {"reason": f"{reason.value} is not supported in this release"})
                case = self._save(case.model_copy(update={"outcome_note": f"reason code {reason.value} not supported"}), actor, "case.closed_not_supported")
        return case

    def _pick_promise(self, purchase: PurchaseRecord, evidence_ids: List[str]) -> Optional[RefundPromise]:
        promises = self.repos.promises_for_purchase(purchase.id)
        verified = [p for p in promises if p.verified_at]
        for p in verified:
            if p.evidence_id and p.evidence_id in evidence_ids:
                return p
        return verified[-1] if verified else None

    # -------------------------------------------------------------- reconcile
    def reconcile(self, case_id: str, *, actor: str = "system", source_event_id: Optional[str] = None) -> Dict[str, Any]:
        case = self.repos.get_case(case_id)
        with self.repos.db.transaction():
            if case.status == CaseStatus.detected:
                case = self._transition(case, CaseStatus.investigating, actor)
            purchase = self.repos.get_purchase(case.purchase_id)
            promise = self._verified_promise(case)
            transactions = self.repos.transactions_for_instrument(case.customer_id, purchase.payment_instrument_ref)
            existing = self.repos.matches_for_case(case.id)
            channels = self.repos.channels_for_case(case.id)
            totals_before = self.totals_for(case)
            result = match_credits(
                purchase=purchase, promise=promise, outstanding_minor=totals_before.outstanding_minor, currency=case.currency,
                transactions=transactions, existing_matches=existing,
                known_refund_refs=self._known_refund_refs(case, purchase),
                issuer_dispute_refs=self._issuer_refs(channels, transactions),
                claimed_amounts={p.promised_minor for p in self.repos.promises_for_purchase(purchase.id)},
                excluded_transaction_ids=self._transactions_matched_elsewhere(case),
                foreign_refund_refs=self._foreign_refund_refs(case, purchase),
            )
            new_final = new_prov = new_rev = False
            new_matches: List[CreditMatch] = []
            for cand in result.auto_confirmable:
                if cand.amount_minor > totals_before.outstanding_minor and cand.credit_kind == CreditKind.final:
                    continue
                m = CreditMatch(
                    id=new_id("match"), case_id=case.id, transaction_id=cand.transaction_id, amount_minor=cand.amount_minor, currency=cand.currency,
                    credit_kind=cand.credit_kind, channel_type=cand.channel_type.value, matching_method=cand.matching_method,
                    confidence=cand.confidence, confirmed_by=actor if actor != "system" else "system", confirmed_at=self._now(),
                )
                self.repos.add_match(m)
                existing.append(m)
                new_matches.append(m)
                self._event(case, "credit.matched", actor, {"match_id": m.id, "transaction_id": m.transaction_id, "amount_minor": m.amount_minor, "credit_kind": m.credit_kind.value, "channel_type": m.channel_type, "method": m.matching_method.value, "confidence": m.confidence, "reasons": cand.reasons}, source_event_id=source_event_id)
                if m.credit_kind == CreditKind.final:
                    new_final = True
                elif m.credit_kind == CreditKind.provisional:
                    new_prov = True
                if m.channel_type == ChannelType.issuer.value:
                    self._mark_issuer_channel(channels, ChannelStatus.provisional if m.credit_kind == CreditKind.provisional else ChannelStatus.credited)
                elif m.channel_type == ChannelType.merchant.value:
                    self._mark_merchant_channel(channels, ChannelStatus.credited)
            for rev_txn, match in find_reversal_targets(transactions, existing):
                match.reversed_at = rev_txn.posted_at
                match.reversal_transaction_id = rev_txn.id
                self.repos.save_match(match)
                new_rev = True
                self._event(case, "credit.reversed", actor, {"match_id": match.id, "transaction_id": match.transaction_id, "reversal_transaction_id": rev_txn.id, "amount_minor": match.amount_minor, "credit_kind": match.credit_kind.value}, source_event_id=source_event_id)
                if match.channel_type == ChannelType.issuer.value:
                    self._mark_issuer_channel(channels, ChannelStatus.reversed)

            totals = compute_totals(currency=case.currency, requested_minor=purchase.amount_minor, promised_minor=promise.promised_minor if promise else 0,
                                    target_minor=case.target_minor, matches=existing, store_credit_minor=case.store_credit_minor)
            case = case.model_copy(update={
                "final_recovered_minor": totals.final_recovered_minor, "provisional_minor": totals.provisional_minor,
                "reversed_minor": totals.reversed_minor, "store_credit_minor": totals.store_credit_minor,
            })
            question = case.pending_question
            if result.ambiguous and (question is None or question.kind != "ambiguous_credit_match"):
                question = PendingQuestion(kind="ambiguous_credit_match", prompt="Several credits on your statement could be this refund. Which one is it?",
                                           options=[{"transaction_id": c.transaction_id, "amount_minor": c.amount_minor, "posted_at": c.posted_at, "confidence": c.confidence} for c in result.candidates] + [{"transaction_id": None, "label": "None of these"}],
                                           asked_at=self._now())
                self._event(case, "customer.question_asked", actor, {"kind": question.kind, "options": len(question.options)})
            elif not result.ambiguous and question is not None and question.kind == "ambiguous_credit_match":
                question = None
            case = case.model_copy(update={"pending_question": question})
            case = self._save(case, actor, "case.reconciled", {"summary": result.summary, "totals": totals.as_dict(), "exact": len(result.exact), "candidates": len(result.candidates), "rejected": len(result.rejected)}, source_event_id=source_event_id)
            for m in new_matches:
                if m.reversed_at is None:
                    self.outbox.publish("recovery.credit_posted", case.id, {"transaction_id": m.transaction_id, "amount_minor": m.amount_minor, "credit_kind": m.credit_kind.value, "remaining_minor": totals.outstanding_minor})
            case = self._derive_state(case, totals, channels, new_final=new_final, new_provisional=new_prov, new_reversal=new_rev, actor=actor, source_event_id=source_event_id)
        return self._reconcile_report(case, totals, result)

    def _known_refund_refs(self, case: RecoveryCase, purchase: PurchaseRecord) -> Set[str]:
        refs = {p.provider_refund_ref for p in self.repos.promises_for_purchase(purchase.id) if p.provider_refund_ref}
        docs = [d for d in self.repos.documents_for_customer(case.customer_id) if d.extracted.get("order_ref") == purchase.order_ref]
        refs |= refund_refs_from_documents(docs)
        return refs

    def _foreign_refund_refs(self, case: RecoveryCase, purchase: PurchaseRecord) -> Set[str]:
        """Refund references known to belong to other orders of this customer."""
        refs: Set[str] = set()
        for other in self.repos.purchases_for_customer(case.customer_id):
            if other.id == purchase.id:
                continue
            refs |= {p.provider_refund_ref for p in self.repos.promises_for_purchase(other.id) if p.provider_refund_ref}
        docs = [d for d in self.repos.documents_for_customer(case.customer_id) if d.extracted.get("order_ref") not in (None, purchase.order_ref)]
        refs |= refund_refs_from_documents(docs)
        return refs

    def _transactions_matched_elsewhere(self, case: RecoveryCase) -> Set[str]:
        rows = self.repos.db.fetch_all(
            "SELECT cm.transaction_id AS tid FROM credit_matches cm JOIN recovery_cases rc ON rc.id = cm.case_id WHERE rc.customer_id = ? AND cm.case_id != ? AND cm.reversed_at IS NULL",
            (case.customer_id, case.id),
        )
        return {r["tid"] for r in rows}

    def _issuer_refs(self, channels: Iterable[RecoveryChannel], transactions: Iterable[Transaction]) -> Set[str]:
        prefixes = [c.provider_case_ref for c in channels if c.channel_type == ChannelType.issuer and c.provider_case_ref]
        return {t.provider_ref for t in transactions if t.provider_ref and any(t.provider_ref.startswith(p) for p in prefixes)}

    def _mark_issuer_channel(self, channels: List[RecoveryChannel], status: ChannelStatus) -> None:
        for ch in channels:
            if ch.channel_type == ChannelType.issuer and ch.provider_case_ref:
                ch.status = status
                self.repos.save_channel(ch)

    def _mark_merchant_channel(self, channels: List[RecoveryChannel], status: ChannelStatus) -> None:
        for ch in channels:
            if ch.channel_type == ChannelType.merchant and ch.provider_case_ref:
                ch.status = status
                self.repos.save_channel(ch)

    def _derive_state(self, case: RecoveryCase, totals: Totals, channels: List[RecoveryChannel], *, new_final: bool, new_provisional: bool, new_reversal: bool, actor: str, source_event_id: Optional[str]) -> RecoveryCase:
        s = case.status
        if is_terminal(s):
            if new_final or new_provisional or new_reversal:
                self._event(case, "credit.after_closure", actor, {"note": "credit activity after case closure recorded for operator review"}, source_event_id=source_event_id)
            return case
        if totals.overlap_flagged:
            if s != CaseStatus.manual_review:
                self._revoke_open_approvals(case, "possible duplicate recovery", actor)
                case = self._transition(case, CaseStatus.manual_review, actor, {"reason": "possible_duplicate_recovery", "overlap_minor": totals.overlap_minor, "final_by_channel": totals.final_by_channel}, source_event_id=source_event_id)
                self.outbox.publish("recovery.duplicate_recovery_flagged", case.id, {"overlap_minor": totals.overlap_minor})
            return case
        if totals.outstanding_minor == 0 and totals.final_recovered_minor >= case.target_minor:
            issuer_open = any(c.channel_type == ChannelType.issuer and c.provider_case_ref and c.status in (ChannelStatus.pending, ChannelStatus.provisional, ChannelStatus.reversed) for c in channels)
            if issuer_open and totals.provisional_minor > 0 and s != CaseStatus.manual_review:
                # Fully covered by final credits while an issuer provisional credit is still open: hold, do not close.
                self._revoke_open_approvals(case, "possible duplicate recovery pending issuer decision", actor)
                case = self._transition(case, CaseStatus.manual_review, actor, {"reason": "final_credit_posted_while_issuer_provisional_open", "provisional_minor": totals.provisional_minor, "final_by_channel": totals.final_by_channel}, source_event_id=source_event_id)
                self.outbox.publish("recovery.duplicate_recovery_flagged", case.id, {"provisional_minor": totals.provisional_minor, "final_recovered_minor": totals.final_recovered_minor})
                return case
            evidence = "credit_matches:" + ",".join(m.id for m in self.repos.matches_for_case(case.id) if m.credit_kind == CreditKind.final and m.reversed_at is None)
            contacted = any(c.provider_case_ref for c in channels)
            if s == CaseStatus.investigating and not contacted:
                case = self._save(case.model_copy(update={"completion_evidence_ref": evidence, "outcome_note": "refund already posted before any external request"}), actor, "case.completion_evidence_recorded", {"evidence": evidence})
                return self._transition(case, CaseStatus.already_refunded, actor, {"evidence": evidence}, source_event_id=source_event_id)
            if s == CaseStatus.awaiting_approval:
                self._revoke_open_approvals(case, "credit posted; nothing outstanding", actor)
            path = {
                CaseStatus.investigating: [CaseStatus.credit_pending, CaseStatus.recovered],
                CaseStatus.awaiting_approval: [CaseStatus.credit_pending, CaseStatus.recovered],
                CaseStatus.merchant_pending: [CaseStatus.credit_pending, CaseStatus.recovered],
                CaseStatus.refund_promised: [CaseStatus.credit_pending, CaseStatus.recovered],
                CaseStatus.credit_pending: [CaseStatus.recovered],
                CaseStatus.issuer_review: [CaseStatus.recovered],
                CaseStatus.issuer_pending: [CaseStatus.final_credit, CaseStatus.recovered],
                CaseStatus.provisional_credit: [CaseStatus.final_credit, CaseStatus.recovered],
                CaseStatus.credit_reversed: [CaseStatus.issuer_pending, CaseStatus.final_credit, CaseStatus.recovered],
                CaseStatus.manual_review: [],
            }.get(s, [])
            if path:
                case = self._save(case.model_copy(update={"completion_evidence_ref": evidence}), actor, "case.completion_evidence_recorded", {"evidence": evidence})
                for target in path:
                    case = self._transition(case, target, actor, {"evidence": evidence, "final_recovered_minor": totals.final_recovered_minor}, source_event_id=source_event_id)
                self.outbox.publish("recovery.recovered", case.id, {"final_recovered_minor": totals.final_recovered_minor, "evidence": evidence})
                self._close_channels(case)
            return case
        # outstanding > 0
        if new_reversal and s == CaseStatus.provisional_credit:
            case = self._transition(case, CaseStatus.credit_reversed, actor, {"reversed_minor": totals.reversed_minor}, source_event_id=source_event_id)
            case = self._transition(case, CaseStatus.issuer_pending, actor, {"note": "provisional credit reversed; dispute still open"}, source_event_id=source_event_id)
        elif new_provisional and s == CaseStatus.issuer_pending:
            case = self._transition(case, CaseStatus.provisional_credit, actor, {"provisional_minor": totals.provisional_minor, "note": "provisional credit is not final recovery"}, source_event_id=source_event_id)
        elif new_final and s in (CaseStatus.merchant_pending, CaseStatus.refund_promised):
            case = self._transition(case, CaseStatus.credit_pending, actor, {"partial_final_minor": totals.final_recovered_minor, "outstanding_minor": totals.outstanding_minor}, source_event_id=source_event_id)
        elif new_final and s == CaseStatus.awaiting_approval:
            self._revoke_open_approvals(case, "partial credit posted; outstanding amount changed", actor)
            case = self._transition(case, CaseStatus.credit_pending, actor, {"partial_final_minor": totals.final_recovered_minor, "outstanding_minor": totals.outstanding_minor}, source_event_id=source_event_id)
        return case

    def _close_channels(self, case: RecoveryCase) -> None:
        for ch in self.repos.channels_for_case(case.id):
            if ch.status not in (ChannelStatus.closed, ChannelStatus.declined):
                ch.status = ChannelStatus.closed if ch.status != ChannelStatus.credited else ChannelStatus.credited
                self.repos.save_channel(ch)

    def _reconcile_report(self, case: RecoveryCase, totals: Totals, result: MatchResult) -> Dict[str, Any]:
        return {
            "case_id": case.id, "status": case.status.value, "version": case.version,
            "amounts": totals.as_dict(),
            "exact_matches": [c.__dict__ for c in result.exact],
            "candidate_matches": [c.__dict__ for c in result.candidates],
            "rejected": [r.__dict__ for r in result.rejected],
            "already_matched": result.already_matched,
            "ambiguous": result.ambiguous,
            "summary": result.summary,
            "pending_question": case.pending_question.model_dump() if case.pending_question else None,
            "next_step": self.next_step(case),
            "labels": {"final_recovered_is_cash": True, "provisional_is_not_recovery": True, "store_credit_is_not_card_credit": True},
        }

    # --------------------------------------------------------------- questions
    def answer_question(self, case_id: str, answer: Dict[str, Any], *, actor: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        case = self._owned_case(case_id, customer_id)
        q = case.pending_question
        if q is None:
            raise DomainError("No question is pending", code="no_question")
        with self.repos.db.transaction():
            if q.kind == "ambiguous_credit_match":
                txn_id = answer.get("transaction_id")
                if txn_id:
                    option = next((o for o in q.options if o.get("transaction_id") == txn_id), None)
                    if option is None:
                        raise DomainError("transaction_id is not one of the offered options", code="invalid_option")
                    txn = self.repos.get_transaction(txn_id)
                    if txn.customer_id != case.customer_id:
                        raise Forbidden("transaction belongs to a different customer")
                    m = CreditMatch(id=new_id("match"), case_id=case.id, transaction_id=txn.id, amount_minor=txn.amount_minor, currency=txn.currency,
                                    credit_kind=CreditKind.final, channel_type=ChannelType.merchant.value, matching_method=MatchingMethod.customer_confirmed,
                                    confidence=1.0, confirmed_by=actor, confirmed_at=self._now())
                    self.repos.add_match(m)
                    self._event(case, "credit.matched", actor, {"match_id": m.id, "transaction_id": txn.id, "amount_minor": txn.amount_minor, "method": "customer_confirmed"})
                case = self._save(case.model_copy(update={"pending_question": None}), actor, "customer.question_answered", {"kind": q.kind, "answer": answer})
                if not txn_id:
                    self._event(case, "customer.no_match_selected", actor, {"note": "customer confirmed none of the candidate credits is the refund"})
                    case = self._save(case.model_copy(update={"outcome_note": "customer_rejected_candidate_credits"}), actor, "case.note")
                return self.reconcile(case.id, actor=actor)
            if q.kind == "store_credit_preference":
                accept = bool(answer.get("accept_store_credit"))
                case = self._save(case.model_copy(update={"pending_question": None}), actor, "customer.question_answered", {"kind": q.kind, "answer": answer})
                if accept:
                    self._revoke_open_approvals(case, "customer accepted store credit", actor)
                    case = self._save(case.model_copy(update={"outcome_note": f"closed_with_store_credit:{case.store_credit_minor}"}), actor, "case.store_credit_accepted", {"store_credit_minor": case.store_credit_minor})
                    if case.status not in (CaseStatus.unresolved,):
                        if case.status in (CaseStatus.awaiting_approval, CaseStatus.investigating):
                            pass
                        case = self._transition(case, CaseStatus.unresolved, actor, {"outcome": "store_credit_accepted", "note": "no card credit recovered; customer accepted store credit"})
                    self._close_channels(case)
                else:
                    self._event(case, "customer.store_credit_declined", actor, {"note": "customer wants refund to original payment method"})
                return self.get_status(case.id)
            if q.kind == "missing_promise_evidence":
                purchase = self.repos.get_purchase(case.purchase_id)
                promised_minor = int(answer["promised_minor"])
                if promised_minor <= 0 or promised_minor > purchase.amount_minor:
                    raise DomainError("promised amount must be positive and not exceed the purchase", code="invalid_promise")
                evidence_id = answer.get("evidence_id")
                if evidence_id:
                    doc = self.repos.get_document(evidence_id)
                    if doc.customer_id != case.customer_id:
                        raise Forbidden("evidence belongs to a different customer")
                promise = RefundPromise(id=new_id("promise"), purchase_id=purchase.id, promised_minor=promised_minor, currency=purchase.currency,
                                        destination_type=DestinationType(answer.get("destination_type", "original_payment")), promised_by=str(answer.get("promised_by", "merchant (customer reported)")),
                                        promised_at=str(answer.get("promised_at", self._now())), expected_by=answer.get("expected_by"), evidence_id=evidence_id, verified_at=self._now())
                self.repos.add_promise(promise)
                case = self._save(case.model_copy(update={"pending_question": None, "promise_id": promise.id, "target_minor": min(case.target_minor, promised_minor)}), actor, "customer.question_answered", {"kind": q.kind, "promise_id": promise.id})
                return self.reconcile(case.id, actor=actor)
        raise DomainError(f"Unknown question kind {q.kind}", code="unknown_question")

    def ask_for_promise_evidence(self, case: RecoveryCase, actor: str) -> RecoveryCase:
        if case.pending_question is not None:
            return case
        q = PendingQuestion(kind="missing_promise_evidence", prompt="I could not find a verified refund promise for this order. What amount was promised, when, and by whom? Attach the confirmation if you have it.",
                            options=[], asked_at=self._now())
        self._event(case, "customer.question_asked", actor, {"kind": q.kind})
        return self._save(case.model_copy(update={"pending_question": q}), actor, "case.question_recorded")

    # ----------------------------------------------------------------- drafts
    def draft_merchant_message(self, case_id: str, *, actor: str, customer_id: Optional[str] = None, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        case = self._owned_case(case_id, customer_id)
        purchase = self.repos.get_purchase(case.purchase_id)
        merchant = self.repos.get_merchant(purchase.merchant_id)
        promise = self._verified_promise(case)
        totals = self.totals_for(case)
        channels = self.repos.channels_for_case(case.id)
        actions = self.repos.actions_for_case(case.id)
        customer = self.repos.get_customer(case.customer_id)

        if idempotency_key:
            existing = self.repos.action_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._draft_view(existing)
        if case.status == CaseStatus.detected:
            raise PolicyViolation("Reconcile account credits before contacting the merchant", code="reconcile_first")
        if promise is None:
            raise PolicyViolation("A verified refund promise is required before contacting the merchant", code="promise_required")
        policy.check_before_merchant_request(case=case, totals=totals, merchant=merchant, channels=channels, actions=actions, max_followups=self.settings.max_followups)
        recipient = policy.verified_recipient(merchant)
        docs = self._evidence_docs(case, purchase)
        manifest = attachment_manifest(docs)
        msg = merchant_message(case=case, purchase=purchase, promise=promise, merchant=merchant, customer_name=customer.display_name, outstanding_minor=totals.outstanding_minor)
        payload = {
            "type": ActionType.send_merchant_message.value, "case_id": case.id, "order_ref": purchase.order_ref, "reason_code": case.reason_code.value,
            "recipient": recipient, "subject": msg["subject"], "body": msg["body"], "message_hash": msg["message_hash"],
            "attachments": manifest, "attachments_hash": payload_hash(manifest), "amount_minor": totals.outstanding_minor, "currency": case.currency,
            "environment": self.settings.environment, "provider": self.merchant_provider,
        }
        if idempotency_key:
            existing = self.repos.action_by_idempotency_key(idempotency_key)
            if existing is not None and existing.payload_hash != payload_hash(payload):
                raise Conflict("Idempotency key reused with different content", code="idempotency_key_reuse")
        with self.repos.db.transaction():
            channel = next((c for c in channels if c.channel_type == ChannelType.merchant and c.status in (ChannelStatus.draft, ChannelStatus.awaiting_approval)), None)
            if channel is None:
                channel = RecoveryChannel(id=new_id("chan"), case_id=case.id, channel_type=ChannelType.merchant, provider=self.merchant_provider, status=ChannelStatus.awaiting_approval, created_at=self._now())
                self.repos.add_channel(channel)
            else:
                channel.status = ChannelStatus.awaiting_approval
                self.repos.save_channel(channel)
            if case.status in (CaseStatus.investigating, CaseStatus.credit_pending, CaseStatus.issuer_review):
                case = self._transition(case, CaseStatus.awaiting_approval, actor, {"action_type": payload["type"]})
            elif case.status != CaseStatus.awaiting_approval:
                raise PolicyViolation(f"Cannot draft a merchant message while the case is {case.status.value}", code="wrong_state")
            action, approval = self._create_action_with_challenge(case, channel, ActionType.send_merchant_message, payload, idempotency_key, actor,
                                                                   scope={"recipient_ref": recipient["recipient_ref"], "attachments_hash": payload["attachments_hash"], "reminders_allowed": self.settings.max_followups, "kind": "merchant_message"})
            packet = OutboundPacket(id=new_id("pkt"), channel_id=channel.id, action_id=action.id, recipient_ref=recipient["recipient_ref"], recipient_address=recipient["address"],
                                    subject=msg["subject"], body=msg["body"], message_hash=msg["message_hash"], attachment_manifest=manifest, created_at=self._now())
            self.repos.add_packet(packet)
        return self._draft_view(action)

    def draft_issuer_dispute(self, case_id: str, *, actor: str, customer_id: Optional[str] = None, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        case = self._owned_case(case_id, customer_id)
        purchase = self.repos.get_purchase(case.purchase_id)
        instrument = self.repos.get_instrument(purchase.payment_instrument_ref)
        promise = self._verified_promise(case)
        totals = self.totals_for(case)
        channels = self.repos.channels_for_case(case.id)
        actions = self.repos.actions_for_case(case.id)
        if idempotency_key:
            existing = self.repos.action_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._draft_view(existing)
        deadline = self.dispute_deadline(case, purchase, instrument)
        findings = policy.check_dispute_eligibility(case=case, totals=totals, channels=channels, actions=actions, now=self.clock.now(),
                                                    deadline_at=deadline.deadline_at if deadline else None, min_merchant_wait_days=MIN_MERCHANT_WAIT_DAYS_FOR_DISPUTE)
        merchant_channel = next(c for c in channels if c.channel_type == ChannelType.merchant)
        history = [{"event": e.event_type, "at": e.occurred_at, "data": e.data} for e in self.repos.events_for_case(case.id) if e.event_type.startswith("merchant.") or e.event_type == "action.submitted"]
        docs = self._evidence_docs(case, purchase)
        manifest = attachment_manifest(docs)
        packet = dispute_packet(case=case, purchase=purchase, promise=promise, dispute_reason=policy.dispute_reason_for(case), outstanding_minor=totals.outstanding_minor,
                                merchant_history=history, attachments=manifest, deadline=None if deadline is None else {"deadline_at": deadline.deadline_at, "source": deadline.source, "fixture": True})
        payload = {
            "type": ActionType.submit_issuer_dispute.value, "case_id": case.id, "order_ref": purchase.order_ref, "reason_code": packet["reason_code"], "case_reason_code": case.reason_code.value,
            "recipient": {"recipient_ref": f"issuer:{instrument.issuer_id}", "channel": "issuer_dispute_api", "source": "issuer_registry"},
            "packet": packet, "attachments_hash": payload_hash(manifest), "amount_minor": totals.outstanding_minor, "currency": case.currency,
            "merchant_channel_ref": merchant_channel.provider_case_ref, "eligibility_findings": findings, "environment": self.settings.environment, "provider": self.issuer_provider,
        }
        if idempotency_key:
            existing = self.repos.action_by_idempotency_key(idempotency_key)
            if existing is not None and existing.payload_hash != payload_hash(payload):
                raise Conflict("Idempotency key reused with different content", code="idempotency_key_reuse")
        with self.repos.db.transaction():
            channel = next((c for c in channels if c.channel_type == ChannelType.issuer and c.status in (ChannelStatus.draft, ChannelStatus.awaiting_approval)), None)
            if channel is None:
                channel = RecoveryChannel(id=new_id("chan"), case_id=case.id, channel_type=ChannelType.issuer, provider=self.issuer_provider, status=ChannelStatus.awaiting_approval,
                                          deadline_at=deadline.deadline_at if deadline else None, deadline_source=deadline.source if deadline else None, created_at=self._now())
                self.repos.add_channel(channel)
            if case.status in (CaseStatus.merchant_pending, CaseStatus.refund_promised, CaseStatus.credit_pending):
                case = self._transition(case, CaseStatus.issuer_review, actor, {"findings": findings})
            if case.status == CaseStatus.issuer_review:
                case = self._transition(case, CaseStatus.awaiting_approval, actor, {"action_type": payload["type"]})
            elif case.status != CaseStatus.awaiting_approval:
                raise PolicyViolation(f"Cannot draft a dispute while the case is {case.status.value}", code="wrong_state")
            action, approval = self._create_action_with_challenge(case, channel, ActionType.submit_issuer_dispute, payload, idempotency_key, actor,
                                                                   scope={"recipient_ref": payload["recipient"]["recipient_ref"], "attachments_hash": payload["attachments_hash"], "reminders_allowed": 0, "kind": "issuer_dispute"})
        return self._draft_view(action)

    def _create_action_with_challenge(self, case: RecoveryCase, channel: RecoveryChannel, action_type: ActionType, payload: Dict[str, Any], idempotency_key: Optional[str], actor: str, *, scope: Dict[str, Any]):
        now = self._now()
        action = Action(id=new_id("act"), case_id=case.id, channel_id=channel.id, type=action_type, payload=payload, payload_hash=payload_hash(payload),
                        status=ActionStatus.awaiting_approval, idempotency_key=idempotency_key, request_ref=new_id("req"), expected_case_version=case.version, created_at=now, updated_at=now)
        self.repos.add_action(action)
        approval = Approval(id=new_id("apr"), action_id=action.id, action_hash=action.payload_hash, scope=scope, challenge_id=new_id("challenge"),
                            challenge_expires_at=format_ts(self.clock.now() + timedelta(hours=self.settings.approval_ttl_hours)), expected_case_version=case.version, created_at=now)
        self.repos.add_approval(approval)
        self._event(case, "action.drafted", actor, {"action_id": action.id, "type": action_type.value, "payload_hash": action.payload_hash, "challenge_id": approval.challenge_id, "expires_at": approval.challenge_expires_at})
        return action, approval

    def _draft_view(self, action: Action) -> Dict[str, Any]:
        approval = self.repos.approval_for_action(action.id)
        case = self.repos.get_case(action.case_id)
        p = action.payload
        review = {
            "destination": p.get("recipient"),
            "documents": p.get("attachments", p.get("packet", {}).get("attachments", [])),
            "amount_minor": p.get("amount_minor"), "currency": p.get("currency"),
            "terms": {"reason_code": p.get("reason_code"), "reminders_covered": approval.scope.get("reminders_allowed", 0) if approval else 0},
            "irreversible_effect": IRREVERSIBLE_MESSAGE if action.type != ActionType.submit_issuer_dispute else IRREVERSIBLE_DISPUTE,
            "environment": p.get("environment"),
        }
        if action.type == ActionType.submit_issuer_dispute:
            review["deadline"] = p.get("packet", {}).get("deadline")
            review["lane"] = "issuer"
        else:
            review["subject"] = p.get("subject")
            review["body"] = p.get("body")
            review["lane"] = "merchant"
        return {
            "action_id": action.id, "case_id": action.case_id, "type": action.type.value, "status": action.status.value,
            "payload_hash": action.payload_hash, "expected_case_version": case.version,
            "approval_challenge_id": approval.challenge_id if approval else None, "challenge_expires_at": approval.challenge_expires_at if approval else None,
            "review": review, "next_step": "approve_pending_action" if action.status == ActionStatus.awaiting_approval else self.next_step(case),
        }

    def dispute_deadline(self, case: RecoveryCase, purchase: PurchaseRecord, instrument) -> Optional[Deadline]:
        return compute_dispute_deadline(self.deadline_config, instrument.issuer_id, purchase.purchased_at, case.reason_code.value)

    # -------------------------------------------------------------- approvals
    def approve_action(self, action_id: str, *, approver_id: str, customer_id: Optional[str], expected_case_version: int, action_payload_hash: str, approval_challenge_id: str) -> Dict[str, Any]:
        action = self.repos.get_action(action_id)
        case = self._owned_case(action.case_id, customer_id)
        approval = self.repos.approval_for_action(action.id)
        if approval is None:
            raise PolicyViolation("No approval challenge exists for this action", code="no_challenge")
        try:
            approval_rules.check_approval_request(action=action, approval=approval, case=case, expected_case_version=expected_case_version,
                                                  action_payload_hash=action_payload_hash, approval_challenge_id=approval_challenge_id, now=self.clock.now())
        except PolicyViolation as exc:
            if exc.code == "challenge_expired":
                with self.repos.db.transaction():
                    self.repos.save_action(action.model_copy(update={"status": ActionStatus.expired, "updated_at": self._now(), "failure_reason": "approval challenge expired"}))
                    self.repos.save_approval(approval.model_copy(update={"revoked_at": self._now(), "revoke_reason": "expired"}))
                    self._event(case, "action.expired", approver_id, {"action_id": action.id})
            raise
        with self.repos.db.transaction():
            approval = approval.model_copy(update={"approver_id": approver_id, "approved_at": self._now()})
            self.repos.save_approval(approval)
            action = action.model_copy(update={"status": ActionStatus.approved, "approval_id": approval.id, "updated_at": self._now()})
            self.repos.save_action(action)
            packet = self.repos.packet_for_action(action.id)
            if packet:
                self.repos.save_packet(packet.model_copy(update={"approval_id": approval.id}))
            self._event(case, "action.approved", approver_id, {"action_id": action.id, "approval_id": approval.id, "payload_hash": action.payload_hash, "expected_case_version": expected_case_version})
            self.jobs.enqueue("execute_action", case_id=case.id, payload={"action_id": action.id}, dedupe_key=f"execute:{action.id}")
        return {"action_id": action.id, "status": action.status.value, "approval_id": approval.id, "case_version": case.version, "next_step": "worker_executes_action"}

    def _revoke_open_approvals(self, case: RecoveryCase, reason: str, actor: str) -> None:
        for a in self.repos.actions_for_case(case.id):
            if a.status in (ActionStatus.awaiting_approval, ActionStatus.approved):
                self.repos.save_action(a.model_copy(update={"status": ActionStatus.superseded, "failure_reason": reason, "updated_at": self._now()}))
                apr = self.repos.approval_for_action(a.id)
                if apr and not apr.revoked_at:
                    self.repos.save_approval(apr.model_copy(update={"revoked_at": self._now(), "revoke_reason": reason}))
                self._event(case, "action.superseded", actor, {"action_id": a.id, "reason": reason})
                if a.channel_id:
                    ch = self.repos.get_channel(a.channel_id)
                    if ch.status == ChannelStatus.awaiting_approval:
                        ch.status = ChannelStatus.draft
                        self.repos.save_channel(ch)

    # -------------------------------------------------------------- execution
    async def execute_action(self, action_id: str, *, worker_id: str) -> Dict[str, Any]:
        action = self.repos.get_action(action_id)
        case = self.repos.get_case(action.case_id)
        if action.status != ActionStatus.approved:
            return {"action_id": action.id, "skipped": True, "status": action.status.value}
        approval = self.repos.get_approval(action.approval_id) if action.approval_id else None
        try:
            approval_rules.verify_authority_before_side_effect(action=action, approval=approval, case_status=case.status.value, current_payload_hash=action.payload_hash)
        except PolicyViolation as exc:
            with self.repos.db.transaction():
                self.repos.save_action(action.model_copy(update={"status": ActionStatus.failed, "failure_reason": exc.message, "updated_at": self._now()}))
                self._event(case, "action.blocked", worker_id, {"action_id": action.id, "reason": exc.message, "code": exc.code})
            return {"action_id": action.id, "status": "failed", "reason": exc.message}
        adapter = self.adapters[action.payload["provider"]]
        with self.repos.db.transaction():
            action = action.model_copy(update={"status": ActionStatus.submitting, "updated_at": self._now()})
            self.repos.save_action(action)
            self._event(case, "action.submitting", worker_id, {"action_id": action.id, "request_ref": action.request_ref, "provider": adapter.provider})
        # ---- provider call happens outside any transaction ----
        result: Optional[ProviderResult] = None
        error: Optional[ProviderError] = None
        req_id = self._log_provider_request(adapter, action, case)
        try:
            result = await self._call_provider(adapter, action)
        except ProviderError as exc:
            error = exc
        self.repos.finish_provider_request(req_id, "ok" if result else f"error:{error.code}", result.as_dict() if result else {"error": str(error), "outcome_known": error.outcome_known}, self._now())
        if result is not None:
            return self._record_submitted(action, case, result, worker_id)
        assert error is not None
        return self._record_provider_error(action, case, error, adapter, worker_id)

    async def _call_provider(self, adapter: RecoveryProviderAdapter, action: Action) -> ProviderResult:
        if action.type == ActionType.send_merchant_message or action.type == ActionType.submit_issuer_dispute:
            packet = {k: v for k, v in action.payload.items() if k not in ("recipient",)}
            packet["recipient_ref"] = action.payload["recipient"]["recipient_ref"]
            return await adapter.open_case(packet, action.request_ref)
        if action.type == ActionType.send_merchant_followup:
            channel = self.repos.get_channel(action.channel_id)
            return await adapter.send_followup(channel.provider_case_ref, {"subject": action.payload["subject"], "body": action.payload["body"], "message_hash": action.payload["message_hash"]}, action.request_ref)
        raise ProviderError(f"unsupported action type {action.type}")

    def _log_provider_request(self, adapter: RecoveryProviderAdapter, action: Action, case: RecoveryCase) -> str:
        req_id = new_id("preq")
        caps = adapter.capabilities()
        self.repos.add_provider_request({
            "id": req_id, "provider": adapter.provider, "environment": caps.environment, "operation": action.type.value, "request_ref": action.request_ref, "case_id": case.id,
            "request_summary_json": __import__("json").dumps({"payload_hash": action.payload_hash, "amount_minor": action.payload.get("amount_minor"), "currency": action.payload.get("currency"), "recipient_ref": action.payload.get("recipient", {}).get("recipient_ref")}),
            "response_summary_json": None, "status": "in_flight", "started_at": self._now(), "finished_at": None,
        })
        return req_id

    def _record_submitted(self, action: Action, case: RecoveryCase, result: ProviderResult, worker_id: str) -> Dict[str, Any]:
        case = self.repos.get_case(case.id)
        provider_ref = result.data.get("case_ref") or result.data.get("message_ref")
        if not provider_ref:
            return self._record_provider_error(action, case, ProviderError("provider result lacks a reference", code="malformed_response"), self.adapters[action.payload["provider"]], worker_id, force_unknown=True)
        with self.repos.db.transaction():
            channel = self.repos.get_channel(action.channel_id)
            if action.type in (ActionType.send_merchant_message, ActionType.submit_issuer_dispute):
                if channel.provider_case_ref and channel.provider_case_ref != provider_ref:
                    raise Conflict("channel already bound to a different provider case", code="channel_ref_conflict")
                channel.provider_case_ref = provider_ref
                channel.status = ChannelStatus.pending
                channel.last_contact_at = self._now()
                channel.last_provider_status = result.data.get("status")
                if action.type == ActionType.send_merchant_message:
                    channel.next_followup_at = format_ts(self.clock.now() + timedelta(days=self.settings.followup_cadence_days))
                self.repos.save_channel(channel)
            else:
                channel.followup_count += 1
                channel.last_contact_at = self._now()
                channel.next_followup_at = format_ts(self.clock.now() + timedelta(days=self.settings.followup_cadence_days))
                self.repos.save_channel(channel)
            action = action.model_copy(update={"status": ActionStatus.submitted, "provider_ref": provider_ref, "updated_at": self._now()})
            self.repos.save_action(action)
            packet = self.repos.packet_for_action(action.id)
            if packet:
                self.repos.save_packet(packet.model_copy(update={"provider_message_ref": provider_ref, "sent_at": self._now()}))
            approval = self.repos.get_approval(action.approval_id)
            if not approval.consumed_at:
                self.repos.save_approval(approval.model_copy(update={"consumed_at": self._now(), "consumed_by_action_id": action.id}))
            self._event(case, "action.submitted", worker_id, {"action_id": action.id, "type": action.type.value, "provider": result.provider, "environment": result.environment, "provider_ref": provider_ref, "authority": result.authority.value, "retrieved_at": result.retrieved_at})
            self.outbox.publish("recovery.action_submitted", case.id, {"action_id": action.id, "type": action.type.value, "provider_ref": provider_ref, "environment": result.environment})
            if action.type == ActionType.send_merchant_message:
                if case.status == CaseStatus.awaiting_approval:
                    case = self._transition(case, CaseStatus.merchant_pending, worker_id, {"provider_ref": provider_ref})
                self.jobs.enqueue("merchant_followup_check", case_id=case.id, payload={"channel_id": channel.id}, run_at=parse_ts(channel.next_followup_at), dedupe_key=f"followup:{channel.id}:{channel.followup_count}")
            elif action.type == ActionType.send_merchant_followup:
                self.jobs.enqueue("merchant_followup_check", case_id=case.id, payload={"channel_id": channel.id}, run_at=parse_ts(channel.next_followup_at), dedupe_key=f"followup:{channel.id}:{channel.followup_count}")
            elif action.type == ActionType.submit_issuer_dispute:
                if case.status == CaseStatus.awaiting_approval:
                    case = self._transition(case, CaseStatus.issuer_pending, worker_id, {"provider_ref": provider_ref})
        return {"action_id": action.id, "status": action.status.value, "provider_ref": provider_ref}

    def _record_provider_error(self, action: Action, case: RecoveryCase, error: ProviderError, adapter: RecoveryProviderAdapter, worker_id: str, *, force_unknown: bool = False) -> Dict[str, Any]:
        case = self.repos.get_case(case.id)
        with self.repos.db.transaction():
            if isinstance(error, ProviderDeclined):
                action = action.model_copy(update={"status": ActionStatus.declined, "failure_reason": str(error), "updated_at": self._now()})
                self.repos.save_action(action)
                channel = self.repos.get_channel(action.channel_id)
                channel.status = ChannelStatus.declined
                channel.last_provider_status = "declined"
                self.repos.save_channel(channel)
                approval = self.repos.get_approval(action.approval_id)
                self.repos.save_approval(approval.model_copy(update={"consumed_at": self._now(), "consumed_by_action_id": action.id}))
                self._event(case, "action.declined", worker_id, {"action_id": action.id, "reason": str(error)})
                if action.type == ActionType.send_merchant_message:
                    case = self._transition(case, CaseStatus.merchant_pending, worker_id, {"note": "merchant declined at intake"})
                    case = self._transition(case, CaseStatus.issuer_review, worker_id, {"reason": "merchant_declined"})
                elif action.type == ActionType.submit_issuer_dispute:
                    case = self._transition(case, CaseStatus.issuer_pending, worker_id, {"note": "issuer declined at intake"})
                    case = self._save(case.model_copy(update={"outcome_note": f"issuer declined: {error}"}), worker_id, "case.note")
                    case = self._transition(case, CaseStatus.unresolved, worker_id, {"reason": "issuer_declined"})
                return {"action_id": action.id, "status": "declined"}
            if not error.outcome_known or force_unknown:
                previous_unknowns = sum(1 for e in self.repos.events_for_case(case.id) if e.event_type == "action.outcome_unknown" and e.data.get("action_id") == action.id)
                action = action.model_copy(update={"status": ActionStatus.unknown, "failure_reason": str(error), "updated_at": self._now()})
                self.repos.save_action(action)
                self._event(case, "action.outcome_unknown", worker_id, {"action_id": action.id, "reason": str(error), "code": error.code, "occurrence": previous_unknowns + 1})
                if adapter.capabilities().find_action and previous_unknowns == 0:
                    self.jobs.enqueue("resolve_uncertain_write", case_id=case.id, payload={"action_id": action.id, "attempt": 1}, run_at=self.clock.now() + timedelta(minutes=1), dedupe_key=f"resolve:{action.id}:1")
                else:
                    # Either no lookup capability, or the safe retry also ended uncertain: stop generating side effects.
                    reason = "uncertain_write_without_lookup" if not adapter.capabilities().find_action else "uncertain_write_after_retry"
                    case = self._transition(case, CaseStatus.manual_review, worker_id, {"reason": reason, "action_id": action.id})
                return {"action_id": action.id, "status": "unknown"}
            action = action.model_copy(update={"status": ActionStatus.failed, "failure_reason": str(error), "updated_at": self._now()})
            self.repos.save_action(action)
            self._event(case, "action.failed", worker_id, {"action_id": action.id, "reason": str(error), "code": error.code})
            case = self._transition(case, CaseStatus.manual_review, worker_id, {"reason": "provider_error", "action_id": action.id})
        return {"action_id": action.id, "status": "failed"}

    async def resolve_uncertain_write(self, action_id: str, attempt: int, *, worker_id: str) -> Dict[str, Any]:
        action = self.repos.get_action(action_id)
        case = self.repos.get_case(action.case_id)
        if action.status != ActionStatus.unknown:
            return {"action_id": action.id, "skipped": True, "status": action.status.value}
        adapter = self.adapters[action.payload["provider"]]
        req_id = self._log_provider_request(adapter, action, case)
        try:
            found = await adapter.find_action(action.request_ref)
        except ProviderNotFound:
            self.repos.finish_provider_request(req_id, "not_found", {"attempt": attempt}, self._now())
            if attempt < 2:
                self.jobs.enqueue("resolve_uncertain_write", case_id=case.id, payload={"action_id": action.id, "attempt": attempt + 1}, run_at=self.clock.now() + timedelta(minutes=5 * attempt), dedupe_key=f"resolve:{action.id}:{attempt + 1}")
                return {"action_id": action.id, "status": "unknown", "retry": attempt + 1}
            # The provider authoritatively has no record: safe to retry the very same request_ref once.
            with self.repos.db.transaction():
                self.repos.save_action(action.model_copy(update={"status": ActionStatus.approved, "updated_at": self._now(), "failure_reason": "no provider record; retrying with the same request reference"}))
                self._event(case, "action.retry_same_request_ref", worker_id, {"action_id": action.id, "request_ref": action.request_ref})
            return await self.execute_action(action.id, worker_id=worker_id)
        except ProviderError as exc:
            self.repos.finish_provider_request(req_id, f"error:{exc.code}", {"error": str(exc)}, self._now())
            with self.repos.db.transaction():
                self._event(case, "action.lookup_failed", worker_id, {"action_id": action.id, "reason": str(exc)})
                case = self._transition(case, CaseStatus.manual_review, worker_id, {"reason": "uncertain_write_unresolvable", "action_id": action.id})
            return {"action_id": action.id, "status": "unknown", "case_status": case.status.value}
        self.repos.finish_provider_request(req_id, "ok", found.as_dict(), self._now())
        self._event(case, "action.resolved_by_lookup", worker_id, {"action_id": action.id, "provider_ref": found.data.get("case_ref") or found.data.get("message_ref")})
        return self._record_submitted(action, case, found, worker_id)

    # -------------------------------------------------------------- follow-ups
    def merchant_followup_check(self, case_id: str, channel_id: str, *, worker_id: str) -> Dict[str, Any]:
        case = self.repos.get_case(case_id)
        channel = self.repos.get_channel(channel_id)
        if is_terminal(case.status) or case.status == CaseStatus.manual_review:
            return {"skipped": True, "reason": case.status.value}
        if channel.status != ChannelStatus.pending or case.status != CaseStatus.merchant_pending:
            return {"skipped": True, "reason": f"channel {channel.status.value} / case {case.status.value}"}
        if channel.followup_count >= self.settings.max_followups:
            with self.repos.db.transaction():
                self._event(case, "merchant.followup_cap_reached", worker_id, {"channel_id": channel.id, "followups": channel.followup_count})
                case = self._transition(case, CaseStatus.issuer_review, worker_id, {"reason": "merchant_unresponsive_after_followups"})
            return {"transitioned": case.status.value}
        original = next((a for a in self.repos.actions_for_case(case.id) if a.type == ActionType.send_merchant_message and a.status == ActionStatus.submitted), None)
        if original is None or not original.approval_id:
            return {"skipped": True, "reason": "no approved original message"}
        purchase = self.repos.get_purchase(case.purchase_id)
        merchant = self.repos.get_merchant(purchase.merchant_id)
        customer = self.repos.get_customer(case.customer_id)
        promise = self._verified_promise(case)
        totals = self.totals_for(case)
        try:
            policy.check_before_merchant_request(case=case, totals=totals, merchant=merchant, channels=[channel], actions=[a for a in self.repos.actions_for_case(case.id) if a.id != original.id], max_followups=self.settings.max_followups, is_followup=True)
        except PolicyViolation as exc:
            self._event(case, "merchant.followup_blocked", worker_id, {"reason": exc.message, "code": exc.code})
            return {"skipped": True, "reason": exc.message}
        msg = merchant_message(case=case, purchase=purchase, promise=promise, merchant=merchant, customer_name=customer.display_name, outstanding_minor=totals.outstanding_minor, followup_number=channel.followup_count + 1)
        payload = dict(original.payload, type=ActionType.send_merchant_followup.value, subject=msg["subject"], body=msg["body"], message_hash=msg["message_hash"], amount_minor=totals.outstanding_minor, followup_number=channel.followup_count + 1)
        now = self._now()
        with self.repos.db.transaction():
            action = Action(id=new_id("act"), case_id=case.id, channel_id=channel.id, type=ActionType.send_merchant_followup, payload=payload, payload_hash=payload_hash(payload),
                            status=ActionStatus.approved, approval_id=original.approval_id, request_ref=new_id("req"), expected_case_version=case.version, created_at=now, updated_at=now)
            self.repos.add_action(action)
            self.repos.add_packet(OutboundPacket(id=new_id("pkt"), channel_id=channel.id, action_id=action.id, recipient_ref=payload["recipient"]["recipient_ref"], recipient_address=payload["recipient"]["address"],
                                                 subject=msg["subject"], body=msg["body"], message_hash=msg["message_hash"], attachment_manifest=payload["attachments"], approval_id=original.approval_id, created_at=now))
            self._event(case, "action.reminder_created", worker_id, {"action_id": action.id, "under_approval": original.approval_id, "followup_number": payload["followup_number"]})
            self.jobs.enqueue("execute_action", case_id=case.id, payload={"action_id": action.id}, dedupe_key=f"execute:{action.id}")
        return {"action_id": action.id, "status": "approved_under_scope"}

    def credit_wait_check(self, case_id: str, *, worker_id: str) -> Dict[str, Any]:
        case = self.repos.get_case(case_id)
        if case.status not in (CaseStatus.refund_promised, CaseStatus.credit_pending):
            return {"skipped": True, "reason": case.status.value}
        totals = self.totals_for(case)
        if totals.outstanding_minor <= 0:
            return {"skipped": True, "reason": "nothing outstanding"}
        with self.repos.db.transaction():
            self._event(case, "merchant.claim_unverified", worker_id, {"outstanding_minor": totals.outstanding_minor, "note": "merchant reported the refund as issued but no matching credit has posted within the wait window"})
            case = self._transition(case, CaseStatus.issuer_review, worker_id, {"reason": "merchant_claims_completed_but_no_credit_posted"})
            self.outbox.publish("recovery.merchant_account_mismatch", case.id, {"outstanding_minor": totals.outstanding_minor})
        return {"transitioned": case.status.value}

    # --------------------------------------------------------------- operator
    def operator_release(self, case_id: str, target: str, *, actor: str, note: str) -> RecoveryCase:
        case = self.repos.get_case(case_id)
        if case.status != CaseStatus.manual_review:
            raise PolicyViolation("Only cases in manual_review can be released", code="not_in_review")
        target_status = CaseStatus(target)
        if target_status == CaseStatus.recovered:
            totals = self.totals_for(case)
            if totals.outstanding_minor > 0 or totals.overlap_flagged:
                raise PolicyViolation("Cannot mark recovered: outstanding balance or unresolved overlap", code="no_completion_evidence")
        with self.repos.db.transaction():
            case = self._save(case.model_copy(update={"outcome_note": note}), actor, "operator.note", {"note": note})
            case = self._transition(case, target_status, actor, {"operator_release": True, "note": note})
        return case

    def record_unresolved(self, case_id: str, *, actor: str, reason: str, customer_id: Optional[str] = None) -> RecoveryCase:
        case = self._owned_case(case_id, customer_id)
        with self.repos.db.transaction():
            self._revoke_open_approvals(case, f"case closed unresolved: {reason}", actor)
            case = self._save(case.model_copy(update={"outcome_note": f"unresolved:{reason}"}), actor, "case.unresolved_reason", {"reason": reason})
            if case.status in (CaseStatus.awaiting_approval, CaseStatus.detected):
                if case.status == CaseStatus.detected:
                    case = self._transition(case, CaseStatus.investigating, actor)
                else:
                    case = self._transition(case, CaseStatus.investigating, actor, {"note": "approval withdrawn"})
            case = self._transition(case, CaseStatus.unresolved, actor, {"reason": reason})
            self._close_channels(case)
        return case

    # ----------------------------------------------------------------- status
    def next_step(self, case: RecoveryCase) -> str:
        if is_terminal(case.status):
            return "none"
        if case.pending_question is not None:
            return f"answer_customer_question:{case.pending_question.kind}"
        s = case.status
        if s == CaseStatus.detected:
            return "reconcile_account_credits"
        if s == CaseStatus.investigating:
            return "draft_merchant_message" if self._verified_promise(case) else "provide_refund_promise_evidence"
        if s == CaseStatus.awaiting_approval:
            statuses = {a.status for a in self.repos.actions_for_case(case.id)}
            if statuses & {ActionStatus.submitting, ActionStatus.unknown}:
                return "wait_for_provider_confirmation"
            if ActionStatus.approved in statuses:
                return "wait_for_worker_to_execute"
            if ActionStatus.awaiting_approval in statuses:
                return "approve_pending_action"
            return "draft_merchant_message"
        if s == CaseStatus.merchant_pending:
            return "wait_for_merchant_response"
        if s in (CaseStatus.refund_promised, CaseStatus.credit_pending):
            return "wait_for_credit_to_post"
        if s == CaseStatus.issuer_review:
            return "consider_issuer_dispute"
        if s in (CaseStatus.issuer_pending, CaseStatus.provisional_credit, CaseStatus.credit_reversed):
            return "wait_for_issuer_decision"
        if s == CaseStatus.final_credit:
            return "verify_final_credit"
        if s == CaseStatus.manual_review:
            return "operator_review"
        return "none"

    def get_status(self, case_id: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        case = self._owned_case(case_id, customer_id)
        purchase = self.repos.get_purchase(case.purchase_id)
        instrument = self.repos.get_instrument(purchase.payment_instrument_ref)
        totals = self.totals_for(case)
        channels = self.repos.channels_for_case(case.id)
        deadline = self.dispute_deadline(case, purchase, instrument)
        alert = deadline.alert(self.clock.now()) if deadline else None
        promise = self._verified_promise(case)
        return {
            "case_id": case.id, "status": case.status.value, "version": case.version, "reason_code": case.reason_code.value,
            "order_ref": purchase.order_ref, "merchant_id": purchase.merchant_id,
            "amounts": totals.as_dict(),
            "promise": None if promise is None else {"promised_minor": promise.promised_minor, "promised_at": promise.promised_at, "promised_by": promise.promised_by, "destination_type": promise.destination_type.value, "provider_refund_ref": promise.provider_refund_ref, "verified_at": promise.verified_at},
            "channels": [c.model_dump() for c in channels],
            "pending_question": case.pending_question.model_dump() if case.pending_question else None,
            "deadline": None if deadline is None else {"deadline_at": deadline.deadline_at, "source": deadline.source, "alert": alert, "fixture": True},
            "completion_evidence_ref": case.completion_evidence_ref, "outcome_note": case.outcome_note,
            "next_step": self.next_step(case),
            "as_of": self._now(), "environment": self.settings.environment,
        }

    def get_timeline(self, case_id: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        case = self._owned_case(case_id, customer_id)
        return {
            "case_id": case.id, "status": case.status.value, "version": case.version,
            "events": [e.model_dump() for e in self.repos.events_for_case(case.id)],
            "channels": [c.model_dump() for c in self.repos.channels_for_case(case.id)],
            "credit_matches": [m.model_dump() for m in self.repos.matches_for_case(case.id)],
            "actions": [{k: v for k, v in a.model_dump().items() if k != "payload"} for a in self.repos.actions_for_case(case.id)],
            "amounts": self.totals_for(case).as_dict(),
        }

    def find_refund_evidence(self, customer_id: str, order_ref: str) -> Dict[str, Any]:
        purchase = self.repos.find_purchase_by_order(customer_id, order_ref)
        if purchase is None:
            raise NotFound(f"No purchase for order {order_ref}")
        promises = self.repos.promises_for_purchase(purchase.id)
        txns = self.repos.transactions_for_instrument(customer_id, purchase.payment_instrument_ref)
        docs = [d for d in self.repos.documents_for_customer(customer_id) if d.extracted.get("order_ref") == order_ref]
        cases = self.repos.cases_for_purchase(purchase.id)
        credits = [t.model_dump() for t in txns if t.direction.value == "credit" and t.id != purchase.original_transaction_id]
        return {
            "purchase": purchase.model_dump(),
            "original_payment": next((t.model_dump() for t in txns if t.id == purchase.original_transaction_id), None),
            "promises": [p.model_dump() for p in promises],
            "verified_promise": next((p.model_dump() for p in reversed(promises) if p.verified_at), None),
            "documents": [{"id": d.id, "kind": d.kind, "source": d.source, "content_hash": d.content_hash, "captured_at": d.captured_at, "extracted": {k: v for k, v in d.extracted.items() if k not in ("raw", "body_text")}} for d in docs],
            "posted_credits": credits,
            "existing_cases": [{"id": c.id, "status": c.status.value} for c in cases],
        }
