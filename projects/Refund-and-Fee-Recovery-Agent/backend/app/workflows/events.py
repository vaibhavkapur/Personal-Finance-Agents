"""Provider event handling.

Incoming callbacks are verified, deduplicated by (provider, event_id) and then
applied. Handlers are idempotent so an operator replay cannot create a second
side effect or approve anything.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from ..adapters.mock_issuer import sign_callback
from ..clock import Clock, parse_ts
from ..domain.errors import Forbidden, NotFound
from ..domain.models import (
    CaseStatus,
    ChannelStatus,
    ChannelType,
    CreditKind,
    DestinationType,
    Direction,
    PendingQuestion,
    RefundPromise,
    Transaction,
    TransactionKind,
)
from ..domain.state_machine import is_terminal
from ..ids import new_id
from ..persistence.inbox import EventInbox
from .case_service import CREDIT_WAIT_DAYS_AFTER_MERCHANT_CLAIM, CaseService


class ProviderEventHandler:
    def __init__(self, service: CaseService, inbox: EventInbox, clock: Clock, provider_secret: str) -> None:
        self.service = service
        self.inbox = inbox
        self.clock = clock
        self.provider_secret = provider_secret
        self.repos = service.repos

    # -------------------------------------------------------------- entry
    def handle(self, payload: Dict[str, Any], signature: Optional[str], *, actor: str = "provider_event") -> Dict[str, Any]:
        provider = payload.get("provider")
        event_id = payload.get("event_id")
        event_type = payload.get("type")
        if not provider or not event_id or not event_type:
            raise NotFound("provider, event_id and type are required")
        if signature is None or sign_callback(self.provider_secret, {k: v for k, v in payload.items() if k != "signature"}) != signature:
            raise Forbidden("invalid provider callback signature")
        first = self.inbox.record(provider, event_id, event_type, payload.get("environment", "unknown"), payload)
        if not first:
            return {"provider": provider, "event_id": event_id, "outcome": "duplicate_ignored"}
        outcome = self._apply(payload, actor=actor)
        self.inbox.mark_processed(provider, event_id, outcome)
        return {"provider": provider, "event_id": event_id, "outcome": outcome}

    def replay(self, provider: str, event_id: str, *, actor: str) -> Dict[str, Any]:
        """Operator replay of a stored event. Idempotent handlers make this safe; it never touches approvals."""
        stored = self.inbox.get(provider, event_id)
        if stored is None:
            raise NotFound("event not in inbox")
        outcome = self._apply(stored["payload"], actor=f"replay:{actor}")
        self.inbox.mark_processed(provider, event_id, f"replayed:{outcome}")
        return {"provider": provider, "event_id": event_id, "outcome": outcome, "replayed": True}

    # -------------------------------------------------------------- dispatch
    def _apply(self, payload: Dict[str, Any], *, actor: str) -> str:
        t = payload["type"]
        data = payload.get("data", {})
        source_event_id = f"{payload['provider']}:{payload['event_id']}"
        if t == "statement.transaction_posted":
            return self._statement_posted(data, source_event_id, actor)
        if t == "merchant.case_updated":
            return self._merchant_updated(data, source_event_id, actor)
        if t == "merchant.store_credit_issued":
            return self._merchant_store_credit(data, source_event_id, actor)
        if t == "issuer.dispute_updated":
            return self._issuer_updated(data, source_event_id, actor)
        return "ignored_unknown_type"

    # -------------------------------------------------------------- handlers
    def _statement_posted(self, data: Dict[str, Any], source_event_id: str, actor: str) -> str:
        reverses_id = None
        if data.get("reverses_provider_ref"):
            row = self.repos.db.fetch_one("SELECT id FROM transactions WHERE source = ? AND provider_ref = ?", ("statement_mock", data["reverses_provider_ref"]))
            reverses_id = row["id"] if row else None
        txn = Transaction(
            id=data["transaction_id"], customer_id=data["customer_id"], payment_instrument_ref=data["payment_instrument_ref"], merchant_id=data.get("merchant_id"),
            direction=Direction(data["direction"]), kind=TransactionKind(data["kind"]), amount_minor=int(data["amount_minor"]), currency=data["currency"],
            posted_at=data["posted_at"], description=data.get("description", ""), provider_ref=data.get("provider_ref"), reverses_transaction_id=reverses_id, source="statement_mock",
        )
        inserted = self.repos.add_transaction(txn)
        touched = 0
        for case in self.repos.cases_for_customer(txn.customer_id):
            if is_terminal(case.status):
                continue
            purchase = self.repos.get_purchase(case.purchase_id)
            if purchase.payment_instrument_ref != txn.payment_instrument_ref:
                continue
            self.service.reconcile(case.id, actor=actor, source_event_id=source_event_id)
            touched += 1
        return f"transaction_{'inserted' if inserted else 'already_known'};cases_reconciled={touched}"

    def _merchant_updated(self, data: Dict[str, Any], source_event_id: str, actor: str) -> str:
        channel = self.repos.channel_by_provider_ref("merchant_mock", data["case_ref"])
        if channel is None:
            return "unknown_channel_ref"
        case = self.repos.get_case(channel.case_id)
        status = data.get("status")
        with self.repos.db.transaction():
            channel.last_provider_status = status
            self.repos.save_channel(channel)
            self.service._event(case, "merchant.status_reported", actor, {"status": status, "provider_ref": data["case_ref"], "authority": "simulated", "note": "merchant status is a claim, not money movement"}, source_event_id=source_event_id)
            if status == "refund_issued" and not is_terminal(case.status):
                purchase = self.repos.get_purchase(case.purchase_id)
                refund_ref = data.get("refund_ref")
                existing = [p for p in self.repos.promises_for_purchase(purchase.id) if refund_ref and p.provider_refund_ref == refund_ref]
                if not existing:
                    promise = RefundPromise(id=new_id("promise"), purchase_id=purchase.id, promised_minor=int(data.get("amount_minor", case.outstanding_minor)), currency=data.get("currency", case.currency),
                                            destination_type=DestinationType(data.get("destination", "original_payment")), promised_by=f"merchant_mock:{data['case_ref']}", promised_at=self.service._now(),
                                            provider_refund_ref=refund_ref, verified_at=self.service._now())
                    self.repos.add_promise(promise)
                    self.service._event(case, "merchant.refund_claimed", actor, {"promise_id": promise.id, "refund_ref": refund_ref, "amount_minor": promise.promised_minor, "destination": promise.destination_type.value}, source_event_id=source_event_id)
                    self.service.outbox.publish("recovery.merchant_refund_claimed", case.id, {"refund_ref": refund_ref, "amount_minor": promise.promised_minor})
                channel.status = ChannelStatus.promised
                self.repos.save_channel(channel)
                if case.status == CaseStatus.merchant_pending:
                    case = self.service._transition(case, CaseStatus.refund_promised, actor, {"refund_ref": refund_ref, "note": "promise changes evidence, not the balance"}, source_event_id=source_event_id)
                    self.service.jobs.enqueue("credit_wait_check", case_id=case.id, payload={}, run_at=self.clock.now() + timedelta(days=CREDIT_WAIT_DAYS_AFTER_MERCHANT_CLAIM), dedupe_key=f"creditwait:{case.id}:{refund_ref}")
            elif status == "declined" and not is_terminal(case.status):
                channel.status = ChannelStatus.declined
                self.repos.save_channel(channel)
                if case.status in (CaseStatus.merchant_pending, CaseStatus.refund_promised, CaseStatus.credit_pending):
                    case = self.service._transition(case, CaseStatus.issuer_review, actor, {"reason": "merchant_declined", "detail": data.get("reason")}, source_event_id=source_event_id)
        if status == "refund_issued":
            self.service.reconcile(case.id, actor=actor, source_event_id=source_event_id)
        return f"merchant_status:{status}"

    def _merchant_store_credit(self, data: Dict[str, Any], source_event_id: str, actor: str) -> str:
        channel = self.repos.channel_by_provider_ref("merchant_mock", data["case_ref"])
        if channel is None:
            return "unknown_channel_ref"
        case = self.repos.get_case(channel.case_id)
        if is_terminal(case.status):
            return "case_closed"
        already = any(e.source_event_id == source_event_id for e in self.repos.events_for_case(case.id))
        if already:
            return "already_applied"
        with self.repos.db.transaction():
            channel.last_provider_status = "store_credit_issued"
            channel.status = ChannelStatus.promised
            self.repos.save_channel(channel)
            amount = int(data["amount_minor"])
            q = PendingQuestion(kind="store_credit_preference",
                                prompt=f"The merchant issued store credit of {amount / 100:.2f} {data.get('currency', case.currency)} instead of refunding your card. Accept the store credit, or keep pursuing a refund to the original payment method?",
                                options=[{"accept_store_credit": True, "label": "Accept store credit and close"}, {"accept_store_credit": False, "label": "Keep pursuing card refund"}], asked_at=self.service._now())
            self.service._event(case, "merchant.store_credit_issued", actor, {"store_credit_ref": data.get("store_credit_ref"), "amount_minor": amount, "currency": data.get("currency"), "note": "store credit is not a card credit and does not reduce the outstanding card amount"}, source_event_id=source_event_id)
            self.service._event(case, "customer.question_asked", actor, {"kind": q.kind})
            case = self.service._save(case.model_copy(update={"store_credit_minor": case.store_credit_minor + amount, "pending_question": q}), actor, "case.store_credit_recorded", {"store_credit_minor": case.store_credit_minor + amount}, source_event_id=source_event_id)
            self.service.outbox.publish("recovery.store_credit_issued", case.id, {"amount_minor": amount, "counts_toward_recovery": False})
        return "store_credit_recorded"

    def _issuer_updated(self, data: Dict[str, Any], source_event_id: str, actor: str) -> str:
        channel = self.repos.channel_by_provider_ref("issuer_mock", data["dispute_ref"])
        if channel is None:
            return "unknown_channel_ref"
        case = self.repos.get_case(channel.case_id)
        status = data.get("status")
        with self.repos.db.transaction():
            channel.last_provider_status = status
            self.repos.save_channel(channel)
            self.service._event(case, "issuer.status_reported", actor, {"status": status, "provider_ref": data["dispute_ref"], "authority": "simulated"}, source_event_id=source_event_id)
            if status == "resolved_in_favor" and not data.get("final_credit_is_new_transaction", False):
                # The provisional credit becomes permanent: revise the interpretation of the existing match.
                for m in self.repos.matches_for_case(case.id):
                    if m.credit_kind == CreditKind.provisional and m.reversed_at is None and m.channel_type == ChannelType.issuer.value:
                        m.credit_kind = CreditKind.final
                        self.repos.save_match(m)
                        self.service._event(case, "credit.interpretation_revised", actor, {"match_id": m.id, "from": "provisional", "to": "final", "issuer_status": status}, source_event_id=source_event_id)
                channel.status = ChannelStatus.credited
                self.repos.save_channel(channel)
            elif status == "declined" and not is_terminal(case.status):
                channel.status = ChannelStatus.declined
                self.repos.save_channel(channel)
        if status in ("resolved_in_favor", "provisional_credit_posted", "provisional_credit_reversed"):
            self.service.reconcile(case.id, actor=actor, source_event_id=source_event_id)
        if status == "declined":
            case = self.repos.get_case(case.id)
            if not is_terminal(case.status) and case.status != CaseStatus.manual_review:
                with self.repos.db.transaction():
                    totals = self.service.totals_for(case)
                    case = self.service._save(case.model_copy(update={"outcome_note": f"issuer declined dispute; outstanding {totals.outstanding_minor}"}), actor, "case.note", source_event_id=source_event_id)
                    if case.status == CaseStatus.provisional_credit:
                        case = self.service._transition(case, CaseStatus.credit_reversed, actor, {"reason": "issuer_declined"}, source_event_id=source_event_id)
                    if case.status == CaseStatus.credit_reversed:
                        case = self.service._transition(case, CaseStatus.unresolved, actor, {"reason": "issuer_declined"}, source_event_id=source_event_id)
                    elif case.status == CaseStatus.issuer_pending:
                        case = self.service._transition(case, CaseStatus.unresolved, actor, {"reason": "issuer_declined"}, source_event_id=source_event_id)
        return f"issuer_status:{status}"
