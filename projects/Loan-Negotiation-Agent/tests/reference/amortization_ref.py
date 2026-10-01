"""Independent reference amortization using exact rational arithmetic.

This implementation shares *only* the documented rounding rule with the
production engine (half-up to the cent per period, level payment rounded to
the cent, final payment clears the balance). It uses ``fractions.Fraction``
instead of ``Decimal`` and integer cents throughout, so agreement to the cent
between the two is a meaningful cross-check.
"""
from __future__ import annotations

from fractions import Fraction
from typing import List, Tuple


def round_half_up_cents(value: Fraction) -> int:
    """Round a Fraction expressed in cents to the nearest integer cent, half-up."""
    sign = 1 if value >= 0 else -1
    value = abs(value)
    floor = value.numerator // value.denominator
    remainder = value - floor
    if remainder >= Fraction(1, 2):
        floor += 1
    return sign * floor


def ref_payment_cents(principal_cents: int, annual_rate: str, months: int) -> int:
    rate = Fraction(annual_rate)
    if rate == 0:
        return round_half_up_cents(Fraction(principal_cents, months))
    r = rate / 12
    growth = (1 + r) ** months
    payment = Fraction(principal_cents) * r * growth / (growth - 1)
    return round_half_up_cents(payment)


def ref_schedule(principal_cents: int, annual_rate: str, months: int) -> List[Tuple[int, int, int, int]]:
    """Return (payment, interest, principal, ending_balance) tuples in cents."""
    rate = Fraction(annual_rate)
    r = rate / 12
    payment = ref_payment_cents(principal_cents, annual_rate, months)
    balance = principal_cents
    rows = []
    for k in range(1, months + 1):
        if balance <= 0:
            break
        interest = round_half_up_cents(Fraction(balance) * r) if rate != 0 else 0
        principal_part = payment - interest
        if k == months or principal_part >= balance:
            principal_part = balance
            pay = principal_part + interest
        else:
            pay = payment
        balance -= principal_part
        rows.append((pay, interest, principal_part, balance))
    return rows
