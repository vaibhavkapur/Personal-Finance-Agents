"""Deterministic date-by-date cash projection (plan §9).

Rules implemented here:
* only dated, *available* funds are projected; ``current`` balances and
  pending credits never enter the base case;
* for each day: opening spendable cash + confirmed inflows - obligations -
  proposed allocations = closing balance;
* the configured buffer is a floor on the closing balance. A candidate
  allocation is feasible only if no day on or after its effective date closes
  below the buffer. Shortfalls *before* the effective date are reported as
  pre-existing warnings: the allocation cannot cause or cure them (e.g. an
  obligation due before maturity cannot be funded by the still-locked CD).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class DatedFlow:
    date: date
    amount_minor: int  # positive inflow, negative outflow
    kind: str  # obligation | maturity_proceeds | allocation | pending_credit | inflow
    label: str
    ref: str | None = None
    confirmed: bool = True
    source: str | None = None


@dataclass
class ProjectionInputs:
    as_of: date
    horizon_end: date
    opening_available_minor: int
    currency: str
    buffer_minor: int
    effective_date: date
    flows: list[DatedFlow] = field(default_factory=list)
    pending_flows: list[DatedFlow] = field(default_factory=list)
    account_ids: list[str] = field(default_factory=list)

    def to_hashable(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "horizon_end": self.horizon_end.isoformat(),
            "opening_available_minor": self.opening_available_minor,
            "currency": self.currency,
            "buffer_minor": self.buffer_minor,
            "effective_date": self.effective_date.isoformat(),
            "account_ids": sorted(self.account_ids),
            "flows": sorted(
                (
                    {**asdict(f), "date": f.date.isoformat()}
                    for f in self.flows
                ),
                key=lambda f: (f["date"], f["kind"], f["label"], f["amount_minor"]),
            ),
            "pending_flows": sorted(
                ({**asdict(f), "date": f.date.isoformat()} for f in self.pending_flows),
                key=lambda f: (f["date"], f["label"]),
            ),
        }


def inputs_hash(inputs: ProjectionInputs, allocations: list[DatedFlow] | None = None) -> str:
    payload = inputs.to_hashable()
    payload["allocations"] = [
        {**asdict(a), "date": a.date.isoformat()} for a in (allocations or [])
    ]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"sha256:{digest}"


@dataclass
class ProjectionDay:
    date: date
    opening_minor: int
    inflows_minor: int
    obligations_minor: int
    allocations_minor: int
    closing_minor: int
    events: list[dict]

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "opening_minor": self.opening_minor,
            "inflows_minor": self.inflows_minor,
            "obligations_minor": self.obligations_minor,
            "allocations_minor": self.allocations_minor,
            "closing_minor": self.closing_minor,
            "events": self.events,
        }


@dataclass
class Breach:
    date: date
    balance_minor: int
    shortfall_minor: int
    before_effective: bool

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "balance_minor": self.balance_minor,
            "shortfall_minor": self.shortfall_minor,
            "before_effective": self.before_effective,
        }


@dataclass
class Projection:
    currency: str
    buffer_minor: int
    effective_date: date
    days: list[ProjectionDay]
    lowest_balance_minor: int
    lowest_balance_date: date
    lowest_from_effective_minor: int
    lowest_from_effective_date: date
    breaches: list[Breach]
    includes_pending: bool
    allocations_minor: int

    @property
    def pre_effective_breaches(self) -> list[Breach]:
        return [b for b in self.breaches if b.before_effective]

    @property
    def effective_breaches(self) -> list[Breach]:
        return [b for b in self.breaches if not b.before_effective]

    @property
    def feasible(self) -> bool:
        return not self.effective_breaches

    def balance_on(self, day: date) -> int:
        for d in self.days:
            if d.date == day:
                return d.closing_minor
        raise KeyError(day)

    def to_dict(self, include_days: bool = True) -> dict:
        data = {
            "currency": self.currency,
            "buffer_minor": self.buffer_minor,
            "effective_date": self.effective_date.isoformat(),
            "lowest_balance_minor": self.lowest_balance_minor,
            "lowest_balance_date": self.lowest_balance_date.isoformat(),
            "lowest_from_effective_minor": self.lowest_from_effective_minor,
            "lowest_from_effective_date": self.lowest_from_effective_date.isoformat(),
            "feasible": self.feasible,
            "includes_pending": self.includes_pending,
            "allocations_minor": self.allocations_minor,
            "breaches": [b.to_dict() for b in self.breaches],
            "pre_effective_breaches": [b.to_dict() for b in self.pre_effective_breaches],
        }
        if include_days:
            data["days"] = [d.to_dict() for d in self.days]
        return data


def _daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def project(
    inputs: ProjectionInputs,
    allocations: list[DatedFlow] | None = None,
    include_pending: bool = False,
) -> Projection:
    allocations = allocations or []
    for a in allocations:
        if a.amount_minor > 0:
            raise ValueError("allocations must be non-positive (they lock funds)")
    if inputs.horizon_end < inputs.as_of:
        raise ValueError("horizon_end must be on or after as_of")

    flows: list[DatedFlow] = list(inputs.flows)
    if include_pending:
        flows.extend(inputs.pending_flows)
    flows.extend(allocations)

    by_day: dict[date, list[DatedFlow]] = {}
    for f in flows:
        # Flows dated before as_of are treated as occurring today (already known).
        key = max(f.date, inputs.as_of)
        by_day.setdefault(key, []).append(f)

    days: list[ProjectionDay] = []
    balance = inputs.opening_available_minor
    lowest = balance
    lowest_date = inputs.as_of
    lowest_eff: int | None = None
    lowest_eff_date: date | None = None
    breaches: list[Breach] = []
    total_alloc = 0

    for day in _daterange(inputs.as_of, inputs.horizon_end):
        opening = balance
        inflow = 0
        obligations = 0
        alloc = 0
        events = []
        for f in sorted(by_day.get(day, []), key=lambda x: (-x.amount_minor, x.kind, x.label)):
            if f.kind == "allocation":
                alloc += f.amount_minor
            elif f.amount_minor < 0:
                obligations += f.amount_minor
            else:
                inflow += f.amount_minor
            events.append(
                {
                    "kind": f.kind,
                    "label": f.label,
                    "amount_minor": f.amount_minor,
                    "ref": f.ref,
                    "confirmed": f.confirmed,
                    "source": f.source,
                }
            )
        balance = opening + inflow + obligations + alloc
        total_alloc += alloc
        days.append(ProjectionDay(day, opening, inflow, obligations, alloc, balance, events))
        if balance < lowest:
            lowest, lowest_date = balance, day
        if day >= inputs.effective_date and (lowest_eff is None or balance < lowest_eff):
            lowest_eff, lowest_eff_date = balance, day
        if balance < inputs.buffer_minor:
            breaches.append(
                Breach(
                    date=day,
                    balance_minor=balance,
                    shortfall_minor=inputs.buffer_minor - balance,
                    before_effective=day < inputs.effective_date,
                )
            )

    if lowest_eff is None:  # effective date beyond horizon: constraint binds on last day
        lowest_eff, lowest_eff_date = balance, inputs.horizon_end

    return Projection(
        currency=inputs.currency,
        buffer_minor=inputs.buffer_minor,
        effective_date=inputs.effective_date,
        days=days,
        lowest_balance_minor=lowest,
        lowest_balance_date=lowest_date,
        lowest_from_effective_minor=lowest_eff,
        lowest_from_effective_date=lowest_eff_date,
        breaches=breaches,
        includes_pending=include_pending,
        allocations_minor=-total_alloc,
    )


def max_lockable(inputs: ProjectionInputs, cap_minor: int | None = None) -> int:
    """Largest amount that can be locked on ``effective_date`` while every day
    from that date onward closes at or above the buffer."""
    base = project(inputs)
    headroom = base.lowest_from_effective_minor - inputs.buffer_minor
    headroom = max(headroom, 0)
    if cap_minor is not None:
        headroom = min(headroom, cap_minor)
    return headroom


def allocation_flow(amount_minor: int, effective_date: date, label: str, ref: str | None = None) -> DatedFlow:
    if amount_minor < 0:
        raise ValueError("amount_minor must be non-negative")
    return DatedFlow(
        date=effective_date,
        amount_minor=-amount_minor,
        kind="allocation",
        label=label,
        ref=ref,
        confirmed=True,
        source="proposed",
    )


def effective_buffer(stated_buffer_minor: int, obligations_total_minor: int, buffer_includes_obligations: bool) -> int:
    """Avoid double counting: if the customer's stated reserve already covers the
    dated obligations, only the remainder is enforced as a floor (plan §9)."""
    if stated_buffer_minor < 0:
        raise ValueError("buffer must be non-negative")
    if not buffer_includes_obligations:
        return stated_buffer_minor
    return max(stated_buffer_minor - obligations_total_minor, 0)
