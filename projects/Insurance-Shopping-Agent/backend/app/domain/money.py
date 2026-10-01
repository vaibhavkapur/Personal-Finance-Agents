"""Fiat amounts are integer minor units with an explicit currency."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise TypeError("amount_minor must be an int")
        if len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO code")

    def __add__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def _same_currency(self, other: "Money") -> None:
        if other.currency != self.currency:
            raise ValueError("currency mismatch: %s vs %s" % (self.currency, other.currency))

    def format(self) -> str:
        sign = "-" if self.amount_minor < 0 else ""
        major, minor = divmod(abs(self.amount_minor), 100)
        symbol = "$" if self.currency == "USD" else self.currency + " "
        return "%s%s%s.%02d" % (sign, symbol, "{:,}".format(major), minor)

    def to_dict(self) -> dict:
        return {"amount_minor": self.amount_minor, "currency": self.currency, "display": self.format()}


def fmt_minor(amount_minor: int, currency: str = "USD") -> str:
    return Money(amount_minor, currency).format()
