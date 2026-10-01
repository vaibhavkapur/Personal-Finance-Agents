"""Deterministic credit matching.

Given the original purchase, the verified promise and the account feed, produce
exact, candidate and rejected matches with reasons. Confidence is a ranking
signal only; multiple plausible candidates are flagged ambiguous and require
review. Nothing in this module changes case state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, List, Optional, Set

from ..clock import parse_ts
from .models import (
    ChannelType,
    CreditKind,
    CreditMatch,
    Direction,
    MatchingMethod,
    PurchaseRecord,
    RefundPromise,
    Transaction,
    TransactionKind,
)

WINDOW_BEFORE_DAYS = 2
WINDOW_AFTER_DAYS = 45
NO_PROMISE_WINDOW_DAYS = 60


@dataclass
class MatchCandidate:
    transaction_id: str
    amount_minor: int
    currency: str
    credit_kind: CreditKind
    channel_type: ChannelType
    matching_method: MatchingMethod
    confidence: float
    reasons: List[str] = field(default_factory=list)
    posted_at: str = ""
    provider_ref: Optional[str] = None


@dataclass
class RejectedTransaction:
    transaction_id: str
    amount_minor: int
    currency: str
    reasons: List[str]


@dataclass
class MatchResult:
    exact: List[MatchCandidate] = field(default_factory=list)
    candidates: List[MatchCandidate] = field(default_factory=list)
    rejected: List[RejectedTransaction] = field(default_factory=list)
    already_matched: List[str] = field(default_factory=list)
    ambiguous: bool = False
    summary: str = ""

    @property
    def auto_confirmable(self) -> List[MatchCandidate]:
        """Matches the system may confirm without a human: exact provider references,
        or a single unambiguous candidate from the same merchant."""
        if self.exact:
            return list(self.exact)
        if len(self.candidates) == 1 and not self.ambiguous:
            return list(self.candidates)
        return []


def match_credits(
    *,
    purchase: PurchaseRecord,
    promise: Optional[RefundPromise],
    outstanding_minor: int,
    currency: str,
    transactions: Iterable[Transaction],
    existing_matches: Iterable[CreditMatch],
    known_refund_refs: Set[str],
    issuer_dispute_refs: Set[str],
    claimed_amounts: Optional[Set[int]] = None,
    excluded_transaction_ids: Optional[Set[str]] = None,
    foreign_refund_refs: Optional[Set[str]] = None,
) -> MatchResult:
    """
    known_refund_refs        provider refund references tied to THIS purchase (promise, merchant claim, UCP/ACP evidence)
    issuer_dispute_refs      statement references produced by THIS case's issuer dispute
    claimed_amounts          amounts the merchant has promised/claimed for this purchase (partial refunds)
    excluded_transaction_ids credits already matched to other cases of the same customer
    foreign_refund_refs      refund references known to belong to OTHER orders
    """
    claimed_amounts = claimed_amounts or set()
    excluded_transaction_ids = excluded_transaction_ids or set()
    foreign_refund_refs = foreign_refund_refs or set()
    result = MatchResult()
    matched_ids = {m.transaction_id for m in existing_matches}
    purchased_at = parse_ts(purchase.purchased_at)
    if promise is not None:
        anchor = parse_ts(promise.promised_at)
        window_start = anchor - timedelta(days=WINDOW_BEFORE_DAYS)
        window_end = anchor + timedelta(days=WINDOW_AFTER_DAYS)
    else:
        window_start = purchased_at
        window_end = purchased_at + timedelta(days=NO_PROMISE_WINDOW_DAYS)

    for txn in transactions:
        if txn.id in matched_ids:
            result.already_matched.append(txn.id)
            continue
        if txn.id == purchase.original_transaction_id:
            continue
        if txn.id in excluded_transaction_ids:
            result.rejected.append(RejectedTransaction(txn.id, txn.amount_minor, txn.currency, ["matched_to_other_case"]))
            continue
        reasons: List[str] = []
        if txn.direction != Direction.credit:
            reasons.append("not_a_credit")
        if txn.kind == TransactionKind.reversal:
            reasons.append("reversal_not_a_credit")
        if txn.currency != currency:
            reasons.append("currency_mismatch")
        if txn.payment_instrument_ref != purchase.payment_instrument_ref:
            reasons.append("different_payment_instrument")
        if reasons:
            result.rejected.append(RejectedTransaction(txn.id, txn.amount_minor, txn.currency, reasons))
            continue

        posted = parse_ts(txn.posted_at)
        if posted < purchased_at:
            result.rejected.append(RejectedTransaction(txn.id, txn.amount_minor, txn.currency, ["posted_before_purchase"]))
            continue

        # Exact matches by provider reference win regardless of window/amount ranking,
        # but an over-target amount is still rejected so a stranger's refund cannot close a case.
        if txn.provider_ref and txn.provider_ref in known_refund_refs:
            if txn.amount_minor > outstanding_minor:
                result.rejected.append(RejectedTransaction(txn.id, txn.amount_minor, txn.currency, ["provider_reference_matches_but_amount_exceeds_outstanding"]))
                continue
            result.exact.append(MatchCandidate(
                transaction_id=txn.id, amount_minor=txn.amount_minor, currency=txn.currency,
                credit_kind=CreditKind.final, channel_type=ChannelType.merchant,
                matching_method=MatchingMethod.provider_reference, confidence=1.0,
                reasons=["provider_reference_match"], posted_at=txn.posted_at, provider_ref=txn.provider_ref,
            ))
            continue
        if txn.provider_ref and txn.provider_ref in issuer_dispute_refs:
            kind = CreditKind.provisional if txn.kind == TransactionKind.provisional_credit else CreditKind.final
            result.exact.append(MatchCandidate(
                transaction_id=txn.id, amount_minor=txn.amount_minor, currency=txn.currency,
                credit_kind=kind, channel_type=ChannelType.issuer,
                matching_method=MatchingMethod.provider_reference, confidence=1.0,
                reasons=["issuer_dispute_reference_match"], posted_at=txn.posted_at, provider_ref=txn.provider_ref,
            ))
            continue

        if txn.provider_ref and txn.provider_ref in foreign_refund_refs:
            result.rejected.append(RejectedTransaction(txn.id, txn.amount_minor, txn.currency, ["reference_belongs_to_other_order"]))
            continue

        # Candidate matching on amount, merchant and time window.
        if txn.kind == TransactionKind.provisional_credit:
            # Provisional credits without our dispute reference are not ours to claim.
            result.rejected.append(RejectedTransaction(txn.id, txn.amount_minor, txn.currency, ["provisional_credit_without_dispute_reference"]))
            continue
        if txn.merchant_id != purchase.merchant_id:
            reasons.append("merchant_mismatch")
        if not (window_start <= posted <= window_end):
            reasons.append("outside_time_window")
        if txn.amount_minor > outstanding_minor:
            reasons.append("amount_exceeds_outstanding")
        amount_is_expected = (
            txn.amount_minor == outstanding_minor
            or (promise is not None and txn.amount_minor == promise.promised_minor)
            or txn.amount_minor in claimed_amounts
        )
        if not amount_is_expected:
            # A same-merchant credit of an unexpected amount is more likely another order's refund.
            reasons.append("amount_does_not_match_promise_or_claim")
        if reasons:
            result.rejected.append(RejectedTransaction(txn.id, txn.amount_minor, txn.currency, reasons))
            continue

        if txn.amount_minor == outstanding_minor:
            confidence, why = 0.9, "amount_equals_outstanding"
        elif promise is not None and txn.amount_minor == promise.promised_minor:
            confidence, why = 0.85, "amount_equals_promise"
        else:
            confidence, why = 0.7, "amount_equals_merchant_claim"
        result.candidates.append(MatchCandidate(
            transaction_id=txn.id, amount_minor=txn.amount_minor, currency=txn.currency,
            credit_kind=CreditKind.final, channel_type=ChannelType.merchant,
            matching_method=MatchingMethod.amount_merchant_window, confidence=confidence,
            reasons=[why, "same_merchant", "within_window"], posted_at=txn.posted_at, provider_ref=txn.provider_ref,
        ))

    result.candidates.sort(key=lambda c: (-c.confidence, c.posted_at))
    if not result.exact and len(result.candidates) > 1:
        result.ambiguous = True
        result.summary = f"{len(result.candidates)} plausible credits found; customer review required"
    elif result.exact:
        result.summary = f"{len(result.exact)} credit(s) matched by provider reference"
    elif result.candidates:
        result.summary = "1 plausible credit found by amount, merchant and time window"
    else:
        result.summary = "no matching credit found on the account feed"
    return result


def find_reversal_targets(transactions: Iterable[Transaction], matches: Iterable[CreditMatch]) -> List[tuple]:
    """Return (reversal_txn, match) pairs for reversals that point at a matched credit."""
    by_txn = {m.transaction_id: m for m in matches if m.reversed_at is None}
    out = []
    for txn in transactions:
        if txn.kind == TransactionKind.reversal and txn.reverses_transaction_id in by_txn:
            out.append((txn, by_txn[txn.reverses_transaction_id]))
    return out
