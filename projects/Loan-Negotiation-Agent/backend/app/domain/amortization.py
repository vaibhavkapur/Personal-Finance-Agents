"""Deterministic fixed-rate amortization.

Rounding rule (``CALCULATION_VERSION`` documents it for every stored comparison):

1. Monthly rate ``r = annual_rate / 12`` (exact Decimal, no rounding).
2. Level payment ``M = P*r / (1 - (1+r)**-n)`` rounded half-up to the cent.
   For a zero rate ``M = P / n`` rounded half-up to the cent.
3. Each period: ``interest = round_half_up(balance * r)``,
   ``principal = M - interest``.
4. Final-payment adjustment: in the last scheduled period, or whenever the
   computed principal would exceed the outstanding balance, the principal
   portion becomes the outstanding balance and the payment becomes
   ``principal + interest``. The schedule therefore always ends at zero.

Everything here works in integer minor units and ``Decimal``; the LLM never
computes payments.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import List, Optional

from .money import from_minor, normalize_rate, round_cents, to_minor

getcontext().prec = 40

CALCULATION_VERSION = "amort-v1.0-halfup-cents-final-adjust"


@dataclass(frozen=True)
class SchedulePeriod:
    period: int
    payment_minor: int
    interest_minor: int
    principal_minor: int
    ending_balance_minor: int


@dataclass(frozen=True)
class Schedule:
    principal_minor: int
    annual_rate: Decimal
    term_months: int
    scheduled_payment_minor: int
    periods: List[SchedulePeriod]
    calculation_version: str = CALCULATION_VERSION

    @property
    def total_paid_minor(self) -> int:
        return sum(p.payment_minor for p in self.periods)

    @property
    def total_interest_minor(self) -> int:
        return sum(p.interest_minor for p in self.periods)

    def cumulative_payments_minor(self, months: int) -> int:
        """Sum of payments made in the first ``months`` periods (capped at payoff)."""
        months = max(0, min(months, len(self.periods)))
        return sum(p.payment_minor for p in self.periods[:months])

    def cumulative_interest_minor(self, months: int) -> int:
        months = max(0, min(months, len(self.periods)))
        return sum(p.interest_minor for p in self.periods[:months])

    def balance_after_minor(self, months: int) -> int:
        """Outstanding balance after ``months`` payments (0 once paid off)."""
        if months <= 0:
            return self.principal_minor
        if months >= len(self.periods):
            return 0
        return self.periods[months - 1].ending_balance_minor

    def fingerprint(self) -> str:
        import hashlib

        h = hashlib.sha256()
        h.update(f"{self.principal_minor}|{self.annual_rate}|{self.term_months}|{self.scheduled_payment_minor}|".encode())
        for p in self.periods:
            h.update(f"{p.period}:{p.payment_minor}:{p.interest_minor}:{p.principal_minor}:{p.ending_balance_minor};".encode())
        return "sha256:" + h.hexdigest()


def monthly_payment_minor(principal_minor: int, annual_rate: Decimal, term_months: int) -> int:
    """Level monthly principal-and-interest payment in minor units."""
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if principal_minor < 0:
        raise ValueError("principal_minor must be non-negative")
    rate = normalize_rate(annual_rate)
    principal = from_minor(principal_minor)
    if rate == 0:
        return to_minor(principal / term_months)
    r = rate / Decimal(12)
    factor = (Decimal(1) + r) ** (-term_months)
    payment = principal * r / (Decimal(1) - factor)
    return to_minor(payment)


def build_schedule(
    principal_minor: int,
    annual_rate: Decimal,
    term_months: int,
    scheduled_payment_minor: Optional[int] = None,
) -> Schedule:
    """Build the full amortization schedule following the documented rounding rule."""
    rate = normalize_rate(annual_rate)
    payment_minor = (
        scheduled_payment_minor
        if scheduled_payment_minor is not None
        else monthly_payment_minor(principal_minor, rate, term_months)
    )
    r = rate / Decimal(12)
    balance_minor = principal_minor
    periods: List[SchedulePeriod] = []
    for k in range(1, term_months + 1):
        if balance_minor <= 0:
            break
        interest_minor = to_minor(from_minor(balance_minor) * r) if rate != 0 else 0
        principal_part = payment_minor - interest_minor
        if k == term_months or principal_part >= balance_minor:
            principal_part = balance_minor
            pay = principal_part + interest_minor
        else:
            pay = payment_minor
        if principal_part <= 0 and k < term_months:
            # Payment does not cover interest: negative amortization is out of scope.
            raise ValueError("scheduled payment does not cover interest; negative amortization unsupported")
        balance_minor -= principal_part
        periods.append(
            SchedulePeriod(
                period=k,
                payment_minor=pay,
                interest_minor=interest_minor,
                principal_minor=principal_part,
                ending_balance_minor=balance_minor,
            )
        )
    return Schedule(
        principal_minor=principal_minor,
        annual_rate=rate,
        term_months=term_months,
        scheduled_payment_minor=payment_minor,
        periods=periods,
    )


def closed_form_balance(principal_minor: int, annual_rate: Decimal, term_months: int, after_months: int) -> Decimal:
    """Unrounded closed-form balance ``B_k = P(1+r)^k - M((1+r)^k - 1)/r``.

    Used only as a tolerance cross-check for the rounded schedule.
    """
    rate = normalize_rate(annual_rate)
    P = from_minor(principal_minor)
    if after_months >= term_months:
        return Decimal(0)
    if rate == 0:
        return round_cents(P - P * after_months / term_months)
    r = rate / Decimal(12)
    M = from_minor(monthly_payment_minor(principal_minor, rate, term_months))
    growth = (Decimal(1) + r) ** after_months
    return P * growth - M * (growth - Decimal(1)) / r
