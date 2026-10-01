"""Money helpers. All fiat values are integer minor units with a currency."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import DomainError

_MINOR_DIGITS = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if self.amount_minor < 0:
            raise DomainError("Money amounts are non-negative; direction is explicit elsewhere")
        if not self.currency or len(self.currency) != 3:
            raise DomainError(f"Invalid currency: {self.currency!r}")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def minus_floor_zero(self, other: "Money") -> "Money":
        self._same(other)
        return Money(max(0, self.amount_minor - other.amount_minor), self.currency)

    def _same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise DomainError(f"Currency mismatch: {self.currency} vs {other.currency}")

    def format(self) -> str:
        return format_minor(self.amount_minor, self.currency)


def format_minor(amount_minor: int, currency: str) -> str:
    digits = _MINOR_DIGITS.get(currency, 2)
    if digits == 0:
        return f"{amount_minor} {currency}"
    whole, frac = divmod(abs(amount_minor), 10 ** digits)
    sign = "-" if amount_minor < 0 else ""
    return f"{sign}{whole}.{frac:0{digits}d} {currency}"
