"""Money and rate helpers.

All fiat values are integer minor units (cents for USD). Rates are ``Decimal``
values with explicit precision (``0.0700`` means 7.00% per year).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Union

CURRENCY_USD = "USD"
CENT = Decimal("0.01")
RATE_PRECISION = Decimal("0.000001")

Number = Union[int, str, float, Decimal]


def to_decimal(value: Number) -> Decimal:
    """Convert a scalar to ``Decimal`` without float artefacts."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def round_cents(value: Decimal) -> Decimal:
    """Round a Decimal amount in major units to the cent, half-up.

    This is the single rounding rule used by every schedule in the project.
    """
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def to_minor(value: Decimal) -> int:
    """Convert a major-unit Decimal to integer minor units (cents)."""
    return int(round_cents(value) * 100)


def from_minor(minor: int) -> Decimal:
    """Convert integer minor units to a major-unit Decimal."""
    return (Decimal(minor) / 100).quantize(CENT)


def normalize_rate(rate: Number) -> Decimal:
    """Pin a rate to six decimal places (e.g. ``Decimal('0.070000')``)."""
    return to_decimal(rate).quantize(RATE_PRECISION)


def format_minor(minor: int, currency: str = CURRENCY_USD) -> str:
    sign = "-" if minor < 0 else ""
    major, cents = divmod(abs(minor), 100)
    return f"{sign}{currency} {major:,}.{cents:02d}"
