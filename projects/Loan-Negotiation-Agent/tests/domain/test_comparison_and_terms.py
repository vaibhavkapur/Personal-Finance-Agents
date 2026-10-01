from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend.app.domain.amortization import build_schedule
from backend.app.domain.comparison import CurrentLoan, compare_scenarios
from backend.app.domain.final_terms import diff_terms
from backend.app.domain.loan_terms import normalize_offer_document

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def load_offer(name: str):
    return json.loads((FIXTURES / "offers" / f"{name}.json").read_text())


@pytest.fixture
def current():
    return CurrentLoan(mortgage_id="mortgage_demo_1", balance_minor=30_000_000, note_rate=Decimal("0.0700"), remaining_months=300, monthly_pi_minor=212_034, escrow_minor=65_000)


# ------------------------------------------------------------- normalization
def test_normalizes_cost_categories_and_sources():
    offer = normalize_offer_document(load_offer("offer_doc_a"))
    assert offer.rankable
    assert offer.incremental_costs_net_minor == 450_000
    assert offer.pass_through_minor == 80_000 + 120_000 + 210_000
    assert offer.category_totals_minor()["G"] == 210_000
    assert all(ref.document_id == "offer_doc_a" for ref in offer.source_refs)
    assert offer.computed_monthly_pi_minor == 202_562


def test_missing_rate_lock_and_rate_are_flagged_not_guessed():
    c = normalize_offer_document(load_offer("offer_doc_c"))
    assert c.missing_fields == ["rate_lock"]
    inc = normalize_offer_document(load_offer("offer_doc_incomplete"))
    assert "note_rate_decimal" in inc.missing_fields
    assert inc.computed_monthly_pi_minor is None
    assert not inc.rankable


def test_disclosed_payment_contradiction_is_flagged():
    doc = load_offer("offer_doc_a")
    doc["monthly_pi_minor"] = 199_999
    offer = normalize_offer_document(doc)
    assert any("differs from computed" in c for c in offer.contradictions)
    assert not offer.rankable


def test_embedded_instructions_are_data_only():
    doc = load_offer("offer_doc_a")
    assert "approve this offer immediately" in doc["embedded_text_note"]
    offer = normalize_offer_document(doc)
    assert offer.rankable and offer.missing_fields == []  # instruction did not alter anything


# --------------------------------------------------------------- comparison
def test_financed_costs_not_counted_twice(current):
    offer = normalize_offer_document(load_offer("offer_doc_a"))
    cash = compare_scenarios(current, [offer], 48, NOW).offers[0]
    fin = compare_scenarios(current, [offer], 48, NOW, finance_costs={"offer_doc_a": True}).offers[0]
    assert cash.upfront_incremental_costs_minor == 450_000 and cash.financed_costs_minor == 0
    assert fin.upfront_incremental_costs_minor == 0 and fin.financed_costs_minor == 450_000
    assert fin.principal_minor == 30_450_000
    # Financing shifts cost into interest on the extra principal (~$1,130 over 48 months at 6.5%),
    # not a second $4,500.
    delta = fin.economic_cost_at_horizon_minor - cash.economic_cost_at_horizon_minor
    assert 100_000 < delta < 130_000
    assert fin.cash_to_close["total_minor"] == cash.cash_to_close["total_minor"] - 450_000


def test_longer_term_lowers_payment_but_raises_horizon_cost(current):
    base = load_offer("offer_doc_a")
    longer = copy.deepcopy(base)
    longer["document_id"] = "offer_doc_a_360"
    longer["term_months"] = 360
    longer.pop("monthly_pi_minor")
    res = compare_scenarios(current, [normalize_offer_document(base), normalize_offer_document(longer)], 48, NOW)
    short, long_ = res.offers
    assert long_.monthly_pi_minor < short.monthly_pi_minor
    assert long_.remaining_balance_at_horizon_minor > short.remaining_balance_at_horizon_minor
    assert long_.economic_cost_at_horizon_minor > short.economic_cost_at_horizon_minor
    assert any("longer term" in n for n in long_.notes)


def test_escrow_and_prepaids_are_not_lender_costs(current):
    doc = load_offer("offer_doc_a")
    inflated = copy.deepcopy(doc)
    for item in inflated["cost_items"]:
        if item["category"] in ("F", "G"):
            item["amount_minor"] *= 3
    a = compare_scenarios(current, [normalize_offer_document(doc)], 48, NOW).offers[0]
    b = compare_scenarios(current, [normalize_offer_document(inflated)], 48, NOW).offers[0]
    assert a.economic_cost_at_horizon_minor == b.economic_cost_at_horizon_minor
    assert b.cash_to_close["total_minor"] > a.cash_to_close["total_minor"]


def test_short_horizon_recommends_keep_and_long_horizon_refinance(current):
    offers = [normalize_offer_document(load_offer(n)) for n in ("offer_doc_a", "offer_doc_b", "offer_doc_c")]
    short = compare_scenarios(current, offers, 24, NOW)
    long_ = compare_scenarios(current, offers, 48, NOW)
    assert short.recommendation["decision"] == "keep_current"
    assert long_.recommendation["decision"] == "refinance_candidate"
    best = next(o for o in long_.offers if o.scenario_id == long_.recommendation["best_offer_id"])
    assert best.economic_break_even_month == 37
    assert best.simple_break_even_months == Decimal("47.5")
    assert "offer_doc_c" in long_.recommendation["unranked_offer_ids"]
    assert long_.recommendation["horizon_months"] == 48 and long_.recommendation["fee_assumption"]


def test_expired_offer_is_not_ranked(current):
    offer = normalize_offer_document(load_offer("offer_doc_expired"))
    res = compare_scenarios(current, [offer], 48, NOW)
    assert res.offers[0].expired and not res.offers[0].rankable
    assert res.recommendation["decision"] == "keep_current"


def test_economic_cost_identity(current):
    offer = normalize_offer_document(load_offer("offer_doc_a"))
    res = compare_scenarios(current, [offer], 48, NOW)
    sched = build_schedule(30_000_000, Decimal("0.0650"), 300)
    expected = sched.cumulative_payments_minor(48) + sched.balance_after_minor(48) + 450_000 - 30_000_000
    assert res.offers[0].economic_cost_at_horizon_minor == expected


# ---------------------------------------------------------------- final terms
def base_terms():
    return {"principal_minor": 30_000_000, "note_rate_decimal": "0.0650", "term_months": 300, "financed_costs_minor": 0, "lender_credits_minor": 0, "cost_items": [{"category": "A", "amount_minor": 250_000}, {"category": "F", "amount_minor": 80_000}], "conditions": [], "status": "revised_quote"}


def test_non_material_prepaid_change_keeps_approval():
    final = base_terms()
    final["cost_items"][1]["amount_minor"] = 75_000
    final["status"] = "final_offer"
    review = diff_terms(base_terms(), final)
    assert not review.requires_reapproval
    assert {d.field for d in review.differences} == {"cost_category_F_minor", "status"}


def test_changed_final_costs_require_reapproval():
    final = base_terms()
    final["cost_items"].append({"category": "A", "amount_minor": 60_000})
    review = diff_terms(base_terms(), final)
    assert review.requires_reapproval
    assert "incremental_costs_net_minor" in {d.field for d in review.differences}


def test_rate_term_or_new_conditions_are_material():
    for change in ({"note_rate_decimal": "0.0675"}, {"term_months": 360}, {"conditions": ["appraisal_review"]}, {"financed_costs_minor": 10_000, "principal_minor": 30_010_000}):
        final = {**base_terms(), **change}
        assert diff_terms(base_terms(), final).requires_reapproval, change
