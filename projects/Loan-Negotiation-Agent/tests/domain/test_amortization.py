from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.app.domain.amortization import CALCULATION_VERSION, build_schedule, closed_form_balance, monthly_payment_minor
from backend.app.domain.money import from_minor
from tests.reference.amortization_ref import ref_payment_cents, ref_schedule

FIXTURE_CASES = [
    (30_000_000, "0.0700", 300),  # existing loan
    (30_000_000, "0.0650", 300),  # offer A
    (30_000_000, "0.06625", 360),  # offer B
    (30_000_000, "0.0675", 300),  # offer C
    (30_450_000, "0.0650", 300),  # offer A with financed costs
    (100_000, "0.0000", 7),  # zero rate, uneven division
    (12_345_678, "0.0999", 17),  # awkward amounts
    (500, "0.1200", 3),  # tiny loan, rounding dominates
]


@pytest.mark.parametrize("principal,rate,months", FIXTURE_CASES)
def test_schedule_matches_independent_reference_to_the_cent(principal, rate, months):
    ours = build_schedule(principal, Decimal(rate), months)
    ref = ref_schedule(principal, rate, months)
    assert ours.scheduled_payment_minor == ref_payment_cents(principal, rate, months)
    assert len(ours.periods) == len(ref)
    for period, (pay, interest, princ, bal) in zip(ours.periods, ref):
        assert (period.payment_minor, period.interest_minor, period.principal_minor, period.ending_balance_minor) == (pay, interest, princ, bal), f"period {period.period}"


@pytest.mark.parametrize("principal,rate,months", FIXTURE_CASES)
def test_schedule_clears_balance_and_sums_to_principal(principal, rate, months):
    sched = build_schedule(principal, Decimal(rate), months)
    assert sched.periods[-1].ending_balance_minor == 0
    assert sum(p.principal_minor for p in sched.periods) == principal
    assert sched.total_paid_minor == principal + sched.total_interest_minor


def test_closed_form_balance_agrees_within_rounding_drift():
    sched = build_schedule(30_000_000, Decimal("0.0700"), 300)
    for k in (1, 12, 48, 120, 299):
        exact = closed_form_balance(30_000_000, Decimal("0.0700"), 300, k)
        assert abs(from_minor(sched.balance_after_minor(k)) - exact) < Decimal("0.60"), k


def test_fixture_payment_values():
    assert monthly_payment_minor(30_000_000, Decimal("0.0700"), 300) == 212_034
    assert monthly_payment_minor(30_000_000, Decimal("0.0650"), 300) == 202_562
    assert monthly_payment_minor(30_000_000, Decimal("0.06625"), 360) == 192_093


def test_zero_rate_uses_p_over_n_with_final_adjustment():
    sched = build_schedule(100_000, Decimal("0"), 7)
    assert sched.scheduled_payment_minor == 14_286
    assert sched.total_interest_minor == 0
    assert sched.periods[-1].payment_minor == 100_000 - 14_286 * 6
    assert sched.periods[-1].ending_balance_minor == 0


def test_final_payment_adjustment_absorbs_rounding():
    sched = build_schedule(30_000_000, Decimal("0.0700"), 300)
    regular = sched.scheduled_payment_minor
    assert all(p.payment_minor == regular for p in sched.periods[:-1])
    assert sched.periods[-1].payment_minor != regular
    assert abs(sched.periods[-1].payment_minor - regular) < regular  # small adjustment only


def test_fingerprint_is_stable_and_versioned():
    a = build_schedule(30_000_000, Decimal("0.0700"), 300)
    b = build_schedule(30_000_000, Decimal("0.0700"), 300)
    assert a.fingerprint() == b.fingerprint()
    assert a.calculation_version == CALCULATION_VERSION


def test_expected_schedule_fixture_file_matches():
    path = Path(__file__).resolve().parents[2] / "fixtures" / "expected" / "schedules.json"
    expected = json.loads(path.read_text())
    for case in expected["schedules"]:
        sched = build_schedule(case["principal_minor"], Decimal(case["note_rate_decimal"]), case["term_months"])
        assert sched.scheduled_payment_minor == case["scheduled_payment_minor"], case["id"]
        assert sched.total_interest_minor == case["total_interest_minor"], case["id"]
        assert sched.balance_after_minor(48) == case["balance_after_48_minor"], case["id"]
        assert sched.fingerprint() == case["fingerprint"], case["id"]
