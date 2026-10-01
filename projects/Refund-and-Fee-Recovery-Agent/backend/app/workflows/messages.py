"""Factual message and packet templates.

Only verified facts from our own records are interpolated. Text extracted from
untrusted documents is never copied into outbound messages; documents are
attached by reference (id + content hash) instead.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..domain.models import Document, Merchant, PurchaseRecord, RecoveryCase, RefundPromise
from ..domain.money import format_minor
from ..ids import payload_hash


def attachment_manifest(docs: List[Document]) -> List[Dict[str, Any]]:
    return [{"document_id": d.id, "kind": d.kind, "content_hash": d.content_hash, "captured_at": d.captured_at, "source": d.source} for d in docs]


def merchant_message(*, case: RecoveryCase, purchase: PurchaseRecord, promise: Optional[RefundPromise], merchant: Merchant, customer_name: str, outstanding_minor: int, followup_number: int = 0) -> Dict[str, str]:
    amount = format_minor(outstanding_minor, case.currency)
    promised = format_minor(promise.promised_minor, promise.currency) if promise else None
    if followup_number:
        subject = f"Follow-up {followup_number}: refund for order {purchase.order_ref} not yet received"
    else:
        subject = f"Refund for order {purchase.order_ref} not yet received"
    lines = [
        f"Hello {merchant.name} support,",
        "",
        f"I am following up on order {purchase.order_ref}, paid on {purchase.purchased_at[:10]} for {format_minor(purchase.amount_minor, purchase.currency)} "
        f"using my card ending in the instrument on file for this order.",
    ]
    if promise:
        lines.append(
            f"On {promise.promised_at[:10]} {promise.promised_by} confirmed a refund of {promised} to my original payment method"
            + (f", expected by {promise.expected_by[:10]}" if promise.expected_by else "") + "."
        )
    lines += [
        f"As of today my card statement shows no refund credit for this order. The outstanding amount is {amount}.",
        "",
        "Could you confirm whether the refund was processed, share the refund reference and processing date, and if it has not been processed, issue it to the original payment method?",
        "",
        "I have attached the receipt and the cancellation/refund confirmation for reference.",
        "",
        f"Thank you,\n{customer_name}",
    ]
    if followup_number:
        lines.insert(2, f"This is follow-up number {followup_number}; I have not yet received a resolution on the case referenced below.")
    body = "\n".join(lines)
    return {"subject": subject, "body": body, "message_hash": payload_hash({"subject": subject, "body": body})}


def dispute_packet(*, case: RecoveryCase, purchase: PurchaseRecord, promise: Optional[RefundPromise], dispute_reason: str, outstanding_minor: int, merchant_history: List[Dict[str, Any]], attachments: List[Dict[str, Any]], deadline: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "reason_code": dispute_reason,
        "case_reason_code": case.reason_code.value,
        "order_ref": purchase.order_ref,
        "original_transaction_id": purchase.original_transaction_id,
        "original_amount_minor": purchase.amount_minor,
        "amount_minor": outstanding_minor,
        "currency": case.currency,
        "payment_instrument_ref": purchase.payment_instrument_ref,
        "promised_refund": None if promise is None else {"promised_minor": promise.promised_minor, "promised_at": promise.promised_at, "promised_by": promise.promised_by, "provider_refund_ref": promise.provider_refund_ref},
        "merchant_contact_history": merchant_history,
        "attachments": attachments,
        "deadline": deadline,
        "statement": (
            "The merchant confirmed a refund that has not posted to the account. "
            "This is a credit-not-processed dispute; it is not a claim that the original transaction was unauthorized."
        ),
    }
