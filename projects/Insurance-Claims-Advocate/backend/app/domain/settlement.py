"""Payout reconciliation: accepted, paid and outstanding are tracked separately.
A provisional or unrelated credit never closes a case."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def reconcile(
    *,
    accepted_minor: int,
    currency: str,
    external_claim_ref: Optional[str],
    payee_id: str,
    payments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    matched: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    paid = 0
    for p in payments:
        reasons = []
        if not external_claim_ref or p["claim_reference"] != external_claim_ref:
            reasons.append("claim_reference_mismatch")
        if p["payee_id"] != payee_id:
            reasons.append("payee_mismatch")
        if p["currency"] != currency:
            reasons.append("currency_mismatch")
        if p.get("payment_status") != "posted":
            reasons.append("provisional")
        if reasons:
            ignored.append({"payment_ref": p["provider_payment_ref"], "amount_minor": p["amount_minor"], "currency": p["currency"], "reasons": reasons})
            continue
        matched.append({"payment_ref": p["provider_payment_ref"], "amount_minor": p["amount_minor"], "posted_at": p["posted_at"]})
        paid += p["amount_minor"]
    outstanding = accepted_minor - paid
    if accepted_minor == 0:
        status = "nothing_accepted" if not matched else "unexpected_credit"
    elif paid == 0:
        status = "unpaid"
    elif outstanding > 0:
        status = "partially_paid"
    elif outstanding == 0:
        status = "paid_in_full"
    else:
        status = "overpaid"
    return {
        "currency": currency,
        "accepted_minor": accepted_minor,
        "paid_minor": paid,
        "outstanding_minor": max(outstanding, 0),
        "overpaid_minor": max(-outstanding, 0),
        "status": status,
        "matched_payments": matched,
        "ignored_payments": ignored,
        "can_close_as_paid": status == "paid_in_full",
    }
