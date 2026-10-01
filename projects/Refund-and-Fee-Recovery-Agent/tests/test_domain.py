"""Unit tests for the deterministic domain engine (no database)."""
from __future__ import annotations

import pytest

from backend.app.clock import FixtureClock
from backend.app.config import FIXTURES_DIR
from backend.app.domain.deadlines import DeadlineConfig, compute_dispute_deadline
from backend.app.domain.errors import DomainError, IllegalTransition
from backend.app.domain.models import (
    CaseStatus,
    ChannelType,
    CreditKind,
    CreditMatch,
    DestinationType,
    Direction,
    MatchingMethod,
    PurchaseRecord,
    RefundPromise,
    Transaction,
    TransactionKind,
)
from backend.app.domain.money import Money, format_minor
from backend.app.domain.reconciliation import find_reversal_targets, match_credits
from backend.app.domain.state_machine import assert_transition, can_transition
from backend.app.domain.totals import compute_totals

PURCHASE = PurchaseRecord(id="pur", customer_id="cus", merchant_id="m1", order_ref="o1", original_transaction_id="t0", amount_minor=8499, currency="USD", payment_instrument_ref="pi", purchased_at="2026-08-15T18:00:00Z")
PROMISE = RefundPromise(id="prm", purchase_id="pur", promised_minor=8499, currency="USD", destination_type=DestinationType.original_payment, promised_by="merchant", promised_at="2026-09-01T10:00:00Z", provider_refund_ref="rf_1", verified_at="2026-09-01T10:00:00Z")


def credit(id_: str, amount: int, *, ref=None, merchant="m1", posted="2026-09-04T09:00:00Z", currency="USD", instrument="pi", kind=TransactionKind.refund, direction=Direction.credit, reverses=None) -> Transaction:
    return Transaction(id=id_, customer_id="cus", payment_instrument_ref=instrument, merchant_id=merchant, direction=direction, kind=kind, amount_minor=amount, currency=currency, posted_at=posted, description="x", provider_ref=ref, reverses_transaction_id=reverses, source="statement_mock")


def run(txns, **kw):
    defaults = dict(purchase=PURCHASE, promise=PROMISE, outstanding_minor=8499, currency="USD", transactions=txns, existing_matches=[], known_refund_refs={"rf_1"}, issuer_dispute_refs=set())
    defaults.update(kw)
    return match_credits(**defaults)


def test_exact_match_by_provider_reference():
    r = run([credit("c1", 8499, ref="rf_1")])
    assert len(r.exact) == 1 and r.exact[0].matching_method == MatchingMethod.provider_reference and r.exact[0].confidence == 1.0
    assert not r.ambiguous and r.auto_confirmable


def test_single_amount_candidate_is_auto_confirmable():
    r = run([credit("c1", 8499)])
    assert not r.exact and len(r.candidates) == 1 and r.candidates[0].confidence == 0.9 and r.auto_confirmable


def test_multiple_candidates_are_ambiguous_and_not_auto_confirmable():
    r = run([credit("c1", 8499), credit("c2", 8499, posted="2026-09-06T09:00:00Z")])
    assert r.ambiguous and len(r.candidates) == 2 and r.auto_confirmable == []


@pytest.mark.parametrize("txn,reason", [
    (credit("d", 8499, direction=Direction.debit, kind=TransactionKind.purchase), "not_a_credit"),
    (credit("e", 8499, currency="EUR"), "currency_mismatch"),
    (credit("i", 8499, instrument="other"), "different_payment_instrument"),
    (credit("m", 8499, merchant="m2"), "merchant_mismatch"),
    (credit("w", 8499, posted="2026-08-20T09:00:00Z"), "outside_time_window"),
    (credit("b", 8499, posted="2026-08-01T09:00:00Z"), "posted_before_purchase"),
    (credit("o", 9000), "amount_exceeds_outstanding"),
    (credit("p", 1234), "amount_does_not_match_promise_or_claim"),
    (credit("r", 8499, kind=TransactionKind.reversal), "reversal_not_a_credit"),
    (credit("v", 8499, kind=TransactionKind.provisional_credit, merchant=None), "provisional_credit_without_dispute_reference"),
])
def test_rejections_carry_reasons(txn, reason):
    r = run([txn])
    assert not r.exact and not r.candidates
    assert reason in r.rejected[0].reasons


def test_reference_of_other_order_and_foreign_matches_are_rejected():
    r = run([credit("c1", 8499, ref="rf_other")], foreign_refund_refs={"rf_other"})
    assert r.rejected[0].reasons == ["reference_belongs_to_other_order"]
    r = run([credit("c1", 8499)], excluded_transaction_ids={"c1"})
    assert r.rejected[0].reasons == ["matched_to_other_case"]


def test_exact_reference_over_outstanding_is_still_rejected():
    r = run([credit("c1", 9000, ref="rf_1")])
    assert not r.exact and "provider_reference_matches_but_amount_exceeds_outstanding" in r.rejected[0].reasons


def test_claimed_partial_amount_is_a_candidate():
    r = run([credit("c1", 5000)], claimed_amounts={5000})
    assert len(r.candidates) == 1 and r.candidates[0].confidence == 0.7


def test_issuer_provisional_matched_by_dispute_reference():
    r = run([credit("p1", 8499, kind=TransactionKind.provisional_credit, merchant=None, ref="dsp_1:prov")], issuer_dispute_refs={"dsp_1:prov"})
    assert r.exact[0].credit_kind == CreditKind.provisional and r.exact[0].channel_type == ChannelType.issuer


def test_reversal_targets_matched_credit():
    m = CreditMatch(id="m", case_id="c", transaction_id="p1", amount_minor=8499, currency="USD", credit_kind=CreditKind.provisional, channel_type="issuer", matching_method=MatchingMethod.provider_reference, confidence=1, confirmed_by="system", confirmed_at="x")
    rev = credit("r1", 8499, kind=TransactionKind.reversal, direction=Direction.debit, reverses="p1", merchant=None)
    assert [(t.id, mm.id) for t, mm in find_reversal_targets([rev], [m])] == [("r1", "m")]


def _match(kind: CreditKind, amount: int, channel="merchant", reversed_at=None, currency="USD") -> CreditMatch:
    return CreditMatch(id=f"m{amount}{kind}{channel}", case_id="c", transaction_id=f"t{amount}{channel}{kind}", amount_minor=amount, currency=currency, credit_kind=kind, channel_type=channel, matching_method=MatchingMethod.provider_reference, confidence=1, confirmed_by="system", confirmed_at="x", reversed_at=reversed_at)


def test_totals_partial_leaves_outstanding():
    t = compute_totals(currency="USD", requested_minor=8499, promised_minor=8499, target_minor=8499, matches=[_match(CreditKind.final, 5000)])
    assert (t.final_recovered_minor, t.outstanding_minor) == (5000, 3499)


def test_totals_provisional_is_not_recovery_and_store_credit_is_separate():
    t = compute_totals(currency="USD", requested_minor=8499, promised_minor=8499, target_minor=8499, matches=[_match(CreditKind.provisional, 8499, "issuer"), _match(CreditKind.store_credit, 8499)])
    assert t.final_recovered_minor == 0 and t.outstanding_minor == 8499
    assert t.provisional_minor == 8499 and t.store_credit_minor == 8499


def test_totals_reversal_reopens_and_overlap_flagged():
    t = compute_totals(currency="USD", requested_minor=8499, promised_minor=8499, target_minor=8499, matches=[_match(CreditKind.final, 8499, "issuer", reversed_at="x")])
    assert t.final_recovered_minor == 0 and t.reversed_minor == 8499 and t.outstanding_minor == 8499
    t = compute_totals(currency="USD", requested_minor=8499, promised_minor=8499, target_minor=8499, matches=[_match(CreditKind.final, 8499, "merchant"), _match(CreditKind.final, 8499, "issuer")])
    assert t.overlap_flagged and t.overlap_minor == 8499 and t.final_by_channel == {"merchant": 8499, "issuer": 8499}


def test_totals_never_mix_currencies():
    t = compute_totals(currency="USD", requested_minor=8499, promised_minor=8499, target_minor=8499, matches=[_match(CreditKind.final, 8499, currency="EUR")])
    assert t.final_recovered_minor == 0


def test_money_helpers():
    assert format_minor(8499, "USD") == "84.99 USD" and format_minor(5, "USD") == "0.05 USD"
    assert (Money(100, "USD") + Money(50, "USD")).amount_minor == 150
    with pytest.raises(DomainError):
        Money(1, "USD") + Money(1, "EUR")
    with pytest.raises(DomainError):
        Money(-1, "USD")


def test_state_machine_follows_plan_graph():
    assert can_transition(CaseStatus.detected, CaseStatus.investigating)
    assert can_transition(CaseStatus.merchant_pending, CaseStatus.refund_promised)
    assert can_transition(CaseStatus.provisional_credit, CaseStatus.credit_reversed)
    assert can_transition(CaseStatus.credit_reversed, CaseStatus.issuer_pending)
    assert can_transition(CaseStatus.issuer_pending, CaseStatus.manual_review)
    assert not can_transition(CaseStatus.recovered, CaseStatus.investigating)
    assert not can_transition(CaseStatus.detected, CaseStatus.recovered)
    assert not can_transition(CaseStatus.provisional_credit, CaseStatus.recovered), "a provisional credit can never close a case"
    with pytest.raises(IllegalTransition):
        assert_transition(CaseStatus.investigating, CaseStatus.merchant_pending)


def test_deadline_from_versioned_fixture_config():
    cfg = DeadlineConfig.load(FIXTURES_DIR / "issuer_config.json")
    d = compute_dispute_deadline(cfg, "issuer_mock", "2026-08-15T18:00:00Z", "promised_refund_missing")
    assert d is not None and d.deadline_at == "2026-10-14T18:00:00Z" and d.source.startswith("issuer-config-v1:issuer_mock")
    assert d.alert(FixtureClock("2026-09-20T12:00:00Z").now()) is None
    assert d.alert(FixtureClock("2026-10-05T12:00:00Z").now()) == "deadline_within_9_days"
    assert d.alert(FixtureClock("2026-10-20T12:00:00Z").now()) == "deadline_passed"
    assert compute_dispute_deadline(cfg, "unknown_issuer", "2026-08-15T18:00:00Z", "promised_refund_missing") is None
