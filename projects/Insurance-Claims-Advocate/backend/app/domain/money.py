"""Integer minor-unit money helpers. Decimal is used only at the parsing boundary."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

MINOR_UNITS = {"USD": 100}


def parse_amount_to_minor(text: str, currency: str = "USD") -> Optional[int]:
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    factor = MINOR_UNITS.get(currency, 100)
    return int((value * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_minor(amount_minor: int, currency: str = "USD") -> str:
    factor = MINOR_UNITS.get(currency, 100)
    sign = "-" if amount_minor < 0 else ""
    amount_minor = abs(amount_minor)
    major, minor = divmod(amount_minor, factor)
    return f"{sign}{major}.{minor:02d} {currency}"
