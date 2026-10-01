"""Phase 1 exit criterion: every sample allocation agrees with the hand-checked
projection in fixtures/expected_allocations.json."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.liquidity import (
    DatedFlow,
    ProjectionInputs,
    allocation_flow,
    effective_buffer,
    inputs_hash,
    max_lockable,
    project,
)
from app.domain.money import Money, format_minor, parse_rate, to_minor
from app.domain.offers import compare_offers, earnings_minor, normalize_offer


def base_inputs(buffer_minor: int = 100000) -> ProjectionInputs:
    return ProjectionInputs(
        as_of=date(2026, 9, 26),
        horizon_end=date(2026, 11, 2),
        opening_available_minor=120000,
        currency="USD",
        buffer_minor=buffer_minor,
        effective_date=date(2026, 10, 3),
        flows=[
            DatedFlow(date(2026, 10, 1), -120000, "obligation", "Tuition installment", "bill_demo_2"),
            DatedFlow(date(2026, 10, 5), -200000, "obligation", "Rent", "bill_demo_1"),
            DatedFlow(date(2026, 10, 3), 1000000, "maturity_proceeds", "CD principal", "cd_demo_1"),
        ],
        pending_flows=[DatedFlow(date(2026, 9, 30), 50000, "pending_credit", "Payroll (pending)", "acct_checking_1", confirmed=False)],
        account_ids=["acct_checking_1", "acct_savings_1"],
    )


def test_projection_matches_hand_checked_days(expected):
    proj = project(base_inputs())
    for day, balance in expected["base_case"]["projection"].items():
        assert proj.balance_on(date.fromisoformat(day)) == balance, day
    assert proj.lowest_from_effective_minor == expected["base_case"]["lowest_balance_from_maturity_minor"]


def test_pre_maturity_shortfall_is_reported_not_attributed_to_allocation(expected):
    proj = project(base_inputs())
    shortfalls = expected["base_case"]["pre_maturity_shortfalls"]
    assert [b.to_dict()["date"] for b in proj.pre_effective_breaches] == [s["date"] for s in shortfalls]
    assert proj.pre_effective_breaches[0].balance_minor == 0
    # The obligation before maturity cannot be funded by the still-locked CD,
    # but the base case (no allocation) has no breach on or after maturity.
    assert proj.feasible


def test_pending_deposits_do_not_increase_available_cash(expected):
    base = project(base_inputs())
    with_pending = project(base_inputs(), include_pending=True)
    assert base.balance_on(date(2026, 10, 1)) == 0
    assert with_pending.balance_on(date(2026, 10, 1)) == expected["base_case"]["pending_scenario_2026_10_01_balance_minor"]
    assert with_pending.includes_pending and not base.includes_pending


def test_max_lockable_matches_hand_check(expected):
    assert max_lockable(base_inputs(), cap_minor=1000000) == expected["base_case"]["max_lockable_minor"]


@pytest.mark.parametrize("amount, feasible", [(700000, True), (700001, False), (800000, False)])
def test_allocation_feasibility(amount, feasible):
    proj = project(base_inputs(), allocations=[allocation_flow(amount, date(2026, 10, 3), "lock")])
    assert proj.feasible is feasible
    if not feasible:
        assert proj.effective_breaches[0].date == date(2026, 10, 5)
        assert proj.effective_breaches[0].shortfall_minor == amount - 700000


def test_reserve_including_obligations_is_not_double_counted(expected):
    case = expected["reserve_already_includes_obligations"]
    eff = effective_buffer(case["inputs"]["minimum_buffer_minor"], 200000, True)
    assert eff == case["effective_buffer_minor"]
    assert max_lockable(base_inputs(eff), cap_minor=1000000) == case["max_lockable_minor"]
    other = expected["double_count_if_not_confirmed"]
    eff2 = effective_buffer(other["inputs"]["minimum_buffer_minor"], 200000, False)
    assert max_lockable(base_inputs(eff2), cap_minor=1000000) == other["max_lockable_minor"]


def test_inputs_hash_is_stable_and_sensitive():
    a = inputs_hash(base_inputs())
    b = inputs_hash(base_inputs())
    c = inputs_hash(base_inputs(buffer_minor=100001))
    assert a == b and a != c and a.startswith("sha256:")


def test_money_helpers():
    assert format_minor(700000) == "$7,000.00"
    assert format_minor(-1) == "-$0.01"
    assert (Money(100) + Money(250)).minor == 350
    with pytest.raises(ValueError):
        Money(1, "USD") + Money(1, "EUR")
    assert to_minor(Decimal("287.005"), "ROUND_HALF_EVEN") == 28700
    assert to_minor(Decimal("287.005"), "ROUND_HALF_UP") == 28701
    assert parse_rate("0.0410") == Decimal("0.041000")


# --------------------------------------------------------------------------- #
# Offers
# --------------------------------------------------------------------------- #


def normalized(fixture):
    return [normalize_offer({**o, "retrieved_at": "2026-09-26T09:00:00Z", "environment": "mock"}) for o in fixture["offers"]]


def test_offer_comparison_matches_hand_check(fixture, expected):
    offers = normalized(fixture)
    results = compare_offers(
        offers,
        principal_minor=700000,
        start_date=date(2026, 10, 3),
        as_of=date(2026, 9, 26),
        horizon_days=365,
        verified_destination_accounts={"acct_cd_1", "acct_savings_1", "acct_checking_1"},
    )
    by_id = {r.offer.id: r for r in results}
    for offer_id, exp in expected["base_case"]["offers"].items():
        r = by_id[offer_id]
        assert r.comparable is exp["comparable"], offer_id
        if exp["comparable"]:
            assert r.earnings_to_term_minor == exp["earnings_to_term_minor"], offer_id
            assert r.earnings_at_horizon_minor == exp["earnings_at_horizon_minor"], offer_id
            assert r.net_at_horizon_minor == exp["net_at_horizon_minor"], offer_id
            assert (r.locked_until.isoformat() if r.locked_until else None) == exp["locked_until"], offer_id
        else:
            assert r.exclusion_reasons == exp["exclusion_reasons"]
            assert r.net_at_horizon_minor is None
    # Higher advertised yield alone never wins: the 5.00% offer is excluded.
    assert results[0].offer.id != "off_meridian_9m"
    assert results[-1].offer.id == "off_meridian_9m"


def test_every_displayed_offer_cites_source_and_version(fixture):
    for offer in normalized(fixture):
        d = offer.to_dict()
        assert d["source"]["provider_id"] and d["source"]["product_version"] and d["source"]["environment"] == "mock"


def test_matching_horizons_and_accrual_rules():
    # APY compounding over a full year equals principal * APY exactly.
    assert earnings_minor(700000, Decimal("0.041"), 365, "apy_compound", "ROUND_HALF_EVEN") == 28700
    # Half-year compounding is less than half the annual figure (compounding), simple is proportional.
    half = earnings_minor(700000, Decimal("0.041"), 182, "apy_compound", "ROUND_HALF_EVEN")
    assert half < 28700 * 182 / 365
    assert earnings_minor(700000, Decimal("0.0385"), 182, "simple_contractual", "ROUND_HALF_EVEN") == 13438
    with pytest.raises(ValueError):
        earnings_minor(1, Decimal("0.01"), 10, "unknown", "ROUND_HALF_EVEN")


def test_expired_offer_and_lockup_overlap_are_flagged(fixture):
    offers = normalized(fixture)
    results = compare_offers(
        offers,
        principal_minor=700000,
        start_date=date(2026, 10, 3),
        as_of=date(2026, 10, 3),  # after Harbor offers' valid_until 2026-10-02
        obligations_after_start=[(date(2026, 10, 20), 45000, "Car insurance")],
        preferred_lockup_days=90,
    )
    by_id = {r.offer.id: r for r in results}
    assert "offer_expired" in by_id["off_harbor_12m"].exclusion_reasons
    assert "offer_expired" in by_id["off_harbor_6m"].exclusion_reasons
    assert by_id["off_northwind_hysa"].comparable
    assert any("preferred lock-up" in w for w in by_id["off_meridian_9m"].warnings)
    assert any("locks funds past Car insurance" in w for w in by_id["off_meridian_9m"].warnings)


def test_missing_conditions_are_recorded_not_filled():
    offer = normalize_offer(
        {"id": "x", "provider_id": "p", "product_version": "1", "apy_decimal": "0.05", "valid_until": "2027-01-01", "offer_kind": "cd_new", "term_days": 90}
    )
    assert "accrual_method" in offer.missing_conditions
    assert "early_withdrawal_penalty" in offer.missing_conditions
    assert offer.eligibility_status == "unknown"
    [r] = compare_offers([offer], 100000, date(2026, 10, 3), date(2026, 9, 26))
    assert not r.comparable and "accrual_method_unknown" in r.exclusion_reasons
