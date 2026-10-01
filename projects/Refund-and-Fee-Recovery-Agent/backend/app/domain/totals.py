"""Amount categories for a case.

requested   original debit
promised    verified merchant promise
target      what the case is trying to recover (may be < requested)
final       posted final credits net of reversals (this is the only "recovered" money)
provisional issuer provisional credits, shown separately, never added to final
store       store credit, kept distinct from card credits
reversed    credits that were later reversed
outstanding target - final, floored at zero
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable

from .models import ChannelType, CreditKind, CreditMatch


@dataclass
class Totals:
    currency: str
    requested_minor: int
    promised_minor: int
    target_minor: int
    final_recovered_minor: int = 0
    provisional_minor: int = 0
    store_credit_minor: int = 0
    reversed_minor: int = 0
    outstanding_minor: int = 0
    final_by_channel: Dict[str, int] = field(default_factory=dict)
    overlap_flagged: bool = False
    overlap_minor: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "currency": self.currency,
            "requested_minor": self.requested_minor,
            "promised_minor": self.promised_minor,
            "target_minor": self.target_minor,
            "final_recovered_minor": self.final_recovered_minor,
            "provisional_minor": self.provisional_minor,
            "store_credit_minor": self.store_credit_minor,
            "reversed_minor": self.reversed_minor,
            "outstanding_minor": self.outstanding_minor,
            "final_by_channel": dict(self.final_by_channel),
            "overlap_flagged": self.overlap_flagged,
            "overlap_minor": self.overlap_minor,
        }


def compute_totals(
    *,
    currency: str,
    requested_minor: int,
    promised_minor: int,
    target_minor: int,
    matches: Iterable[CreditMatch],
    store_credit_minor: int = 0,
) -> Totals:
    t = Totals(currency=currency, requested_minor=requested_minor, promised_minor=promised_minor,
               target_minor=target_minor, store_credit_minor=store_credit_minor)
    by_channel: Dict[str, int] = {ChannelType.merchant.value: 0, ChannelType.issuer.value: 0}
    for m in matches:
        if m.currency != currency:
            continue  # never mix currencies into a total
        if m.reversed_at is not None:
            t.reversed_minor += m.amount_minor
            continue
        if m.credit_kind == CreditKind.final:
            t.final_recovered_minor += m.amount_minor
            by_channel[m.channel_type] = by_channel.get(m.channel_type, 0) + m.amount_minor
        elif m.credit_kind == CreditKind.provisional:
            t.provisional_minor += m.amount_minor
        elif m.credit_kind == CreditKind.store_credit:
            # Store credit is tracked separately from posted card credits.
            t.store_credit_minor += m.amount_minor
    t.final_by_channel = by_channel
    t.outstanding_minor = max(0, target_minor - t.final_recovered_minor)
    merchant_final = by_channel.get(ChannelType.merchant.value, 0)
    issuer_final = by_channel.get(ChannelType.issuer.value, 0)
    if merchant_final > 0 and issuer_final > 0 and t.final_recovered_minor > target_minor:
        t.overlap_flagged = True
        t.overlap_minor = t.final_recovered_minor - target_minor
    elif t.final_recovered_minor > target_minor:
        # Over-recovery from a single channel is still worth surfacing.
        t.overlap_flagged = True
        t.overlap_minor = t.final_recovered_minor - target_minor
    return t
