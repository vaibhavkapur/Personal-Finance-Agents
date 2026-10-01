"""Money helpers: integer minor units, explicit currency, decimal arithmetic."""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal

decimal.getcontext().prec = 28

ROUNDING_MODES = {
    "ROUND_HALF_EVEN": decimal.ROUND_HALF_EVEN,
    "ROUND_HALF_UP": decimal.ROUND_HALF_UP,
    "ROUND_DOWN": decimal.ROUND_DOWN,
    "ROUND_FLOOR": decimal.ROUND_FLOOR,
}


@dataclass(frozen=True)
class Money:
    minor: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int):
            raise TypeError("Money.minor must be an int (minor units)")
        if len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter code")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def _check(self, other: Money) -> None:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def as_decimal(self) -> Decimal:
        return Decimal(self.minor) / Decimal(100)

    def format(self) -> str:
        sign = "-" if self.minor < 0 else ""
        whole, cents = divmod(abs(self.minor), 100)
        return f"{sign}${whole:,}.{cents:02d}" if self.currency == "USD" else f"{sign}{whole:,}.{cents:02d} {self.currency}"


def format_minor(minor: int, currency: str = "USD") -> str:
    return Money(minor, currency).format()


def to_minor(amount: Decimal, rounding: str = "ROUND_HALF_EVEN") -> int:
    mode = ROUNDING_MODES.get(rounding)
    if mode is None:
        raise ValueError(f"unsupported rounding convention: {rounding}")
    return int((amount * 100).quantize(Decimal("1"), rounding=mode))


def parse_rate(value: str | Decimal | float) -> Decimal:
    """Parse a rate such as ``"0.0410"`` with fixed precision (6 dp)."""
    if isinstance(value, float):
        value = repr(value)
    rate = Decimal(str(value)).quantize(Decimal("0.000001"))
    if rate < 0 or rate > 1:
        raise ValueError(f"rate out of range: {rate}")
    return rate
