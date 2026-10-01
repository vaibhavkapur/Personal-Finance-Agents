"""Horizon comparison of the existing mortgage against candidate offers.

Economic cost at horizon ``H`` for any scenario:

    cumulative P&I payments through H
  + remaining principal after H
  + upfront incremental costs paid in cash (0 when financed)
  - the borrower's starting balance (the same for every scenario)

Financed costs are added to the new principal and *not* counted again as an
upfront cost. Prepaids (F) and initial escrow (G) are reported in cash-to-close
but excluded from economic cost because the borrower owes them under any loan.

Nothing here is a financing commitment; results are labelled as estimates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .amortization import CALCULATION_VERSION, Schedule, build_schedule
from .loan_terms import NormalizedOffer
from .money import format_minor, normalize_rate

DEFAULT_SENSITIVITY_HORIZONS = (24, 36, 48, 60, 84, 120)
DEFAULT_COST_SHIFTS_MINOR = (-100_000, 100_000)  # +/- $1,000
KEEP_SCENARIO_ID = "keep_current"


@dataclass(frozen=True)
class CurrentLoan:
    mortgage_id: str
    balance_minor: int
    note_rate: Decimal
    remaining_months: int
    monthly_pi_minor: Optional[int] = None
    escrow_minor: int = 0
    as_of: Optional[datetime] = None
    currency: str = "USD"

    def schedule(self) -> Schedule:
        return build_schedule(self.balance_minor, self.note_rate, self.remaining_months)


@dataclass
class ScenarioResult:
    scenario_id: str
    label: str
    lender_id: Optional[str]
    offer_document_id: Optional[str]
    principal_minor: int
    note_rate: Decimal
    term_months: int
    monthly_pi_minor: int
    monthly_change_vs_keep_minor: int
    cumulative_pi_at_horizon_minor: int
    cumulative_interest_at_horizon_minor: int
    remaining_balance_at_horizon_minor: int
    upfront_incremental_costs_minor: int
    financed_costs_minor: int
    economic_cost_at_horizon_minor: int
    economic_difference_vs_keep_minor: int
    total_interest_over_life_minor: int
    cash_to_close: Dict[str, Any]
    economic_break_even_month: Optional[int]
    simple_break_even_months: Optional[Decimal]
    rankable: bool
    expired: bool
    missing_fields: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    sensitivity: Dict[str, Any] = field(default_factory=dict)
    schedule_fingerprint: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["note_rate_decimal"] = str(self.note_rate)
        del d["note_rate"]
        d["simple_break_even_months"] = (
            str(self.simple_break_even_months) if self.simple_break_even_months is not None else None
        )
        return d


@dataclass
class ComparisonResult:
    mortgage_id: str
    horizon_months: int
    starting_balance_minor: int
    calculation_version: str
    computed_at: datetime
    keep: ScenarioResult
    offers: List[ScenarioResult]
    recommendation: Dict[str, Any]
    assumptions: List[str]
    currency: str = "USD"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mortgage_id": self.mortgage_id,
            "horizon_months": self.horizon_months,
            "starting_balance_minor": self.starting_balance_minor,
            "currency": self.currency,
            "calculation_version": self.calculation_version,
            "computed_at": self.computed_at.isoformat(),
            "keep": self.keep.as_dict(),
            "offers": [o.as_dict() for o in self.offers],
            "recommendation": self.recommendation,
            "assumptions": list(self.assumptions),
            "authority": "estimated",
            "is_financing_commitment": False,
        }


def _economic_cost(schedule: Schedule, horizon: int, upfront_minor: int, starting_balance_minor: int) -> int:
    return (
        schedule.cumulative_payments_minor(horizon)
        + schedule.balance_after_minor(horizon)
        + upfront_minor
        - starting_balance_minor
    )


def _keep_scenario(current: CurrentLoan, horizon: int) -> ScenarioResult:
    sched = current.schedule()
    cost = _economic_cost(sched, horizon, 0, current.balance_minor)
    notes = []
    if current.monthly_pi_minor is not None and abs(current.monthly_pi_minor - sched.scheduled_payment_minor) > 1:
        notes.append(
            "Stated current payment differs from the computed principal-and-interest payment; "
            "confirm whether the stated payment includes escrow."
        )
    return ScenarioResult(
        scenario_id=KEEP_SCENARIO_ID,
        label="Keep current mortgage",
        lender_id=None,
        offer_document_id=None,
        principal_minor=current.balance_minor,
        note_rate=current.note_rate,
        term_months=current.remaining_months,
        monthly_pi_minor=sched.scheduled_payment_minor,
        monthly_change_vs_keep_minor=0,
        cumulative_pi_at_horizon_minor=sched.cumulative_payments_minor(horizon),
        cumulative_interest_at_horizon_minor=sched.cumulative_interest_minor(horizon),
        remaining_balance_at_horizon_minor=sched.balance_after_minor(horizon),
        upfront_incremental_costs_minor=0,
        financed_costs_minor=0,
        economic_cost_at_horizon_minor=cost,
        economic_difference_vs_keep_minor=0,
        total_interest_over_life_minor=sched.total_interest_minor,
        cash_to_close={"total_minor": 0, "incremental_net_minor": 0, "prepaids_minor": 0, "escrow_minor": 0, "lender_credits_minor": 0},
        economic_break_even_month=None,
        simple_break_even_months=None,
        rankable=True,
        expired=False,
        notes=notes,
        schedule_fingerprint=sched.fingerprint(),
    )


def _offer_scenario(
    current: CurrentLoan,
    keep: ScenarioResult,
    keep_sched: Schedule,
    offer: NormalizedOffer,
    horizon: int,
    finance_costs: bool,
    now: datetime,
    sensitivity_horizons: tuple,
    cost_shifts: tuple,
) -> ScenarioResult:
    notes: List[str] = []
    expired = offer.is_expired(now)
    if expired:
        notes.append("Offer has expired; it must be refreshed by the lender before it can be selected.")

    if offer.term_months is None or offer.note_rate is None:
        # Cannot compute anything meaningful; return an unranked placeholder.
        return ScenarioResult(
            scenario_id=offer.document_id,
            label=f"{offer.lender_name} offer (incomplete)",
            lender_id=offer.lender_id,
            offer_document_id=offer.document_id,
            principal_minor=current.balance_minor,
            note_rate=offer.note_rate or Decimal("0"),
            term_months=offer.term_months or 0,
            monthly_pi_minor=0,
            monthly_change_vs_keep_minor=0,
            cumulative_pi_at_horizon_minor=0,
            cumulative_interest_at_horizon_minor=0,
            remaining_balance_at_horizon_minor=0,
            upfront_incremental_costs_minor=0,
            financed_costs_minor=0,
            economic_cost_at_horizon_minor=0,
            economic_difference_vs_keep_minor=0,
            total_interest_over_life_minor=0,
            cash_to_close={},
            economic_break_even_month=None,
            simple_break_even_months=None,
            rankable=False,
            expired=expired,
            missing_fields=list(offer.missing_fields),
            contradictions=list(offer.contradictions),
            notes=notes + ["Offer cannot be ranked until the missing fields are supplied."],
        )

    incremental_net = max(0, offer.incremental_costs_net_minor)
    if offer.incremental_costs_net_minor < 0:
        notes.append("Lender credits exceed incremental costs; excess credit is not modelled as a cash rebate.")
    if offer.loan_amount_minor is not None and offer.loan_amount_minor != current.balance_minor:
        notes.append(
            f"Offer document states loan amount {offer.loan_amount_minor}; compared like-for-like at the current balance {current.balance_minor}."
        )

    financed = incremental_net if finance_costs else 0
    upfront = 0 if finance_costs else incremental_net
    principal = current.balance_minor + financed

    sched = build_schedule(principal, offer.note_rate, offer.term_months)
    cost = _economic_cost(sched, horizon, upfront, current.balance_minor)
    diff = cost - keep.economic_cost_at_horizon_minor

    # Economic break-even: first horizon where the offer is no worse than keeping.
    max_h = max(len(keep_sched.periods), len(sched.periods))
    break_even: Optional[int] = None
    for h in range(1, max_h + 1):
        if _economic_cost(sched, h, upfront, current.balance_minor) <= _economic_cost(keep_sched, h, 0, current.balance_minor):
            break_even = h
            break

    monthly_change = sched.scheduled_payment_minor - keep.monthly_pi_minor
    simple_be: Optional[Decimal] = None
    if monthly_change < 0 and incremental_net > 0:
        simple_be = (Decimal(incremental_net) / Decimal(-monthly_change)).quantize(Decimal("0.1"))
    elif monthly_change < 0 and incremental_net == 0:
        simple_be = Decimal("0")

    totals = offer.category_totals_minor()
    cash_to_close = {
        "incremental_gross_minor": offer.incremental_costs_gross_minor,
        "lender_credits_minor": offer.lender_credits_minor,
        "incremental_net_minor": offer.incremental_costs_net_minor,
        "financed_minor": financed,
        "prepaids_minor": totals.get("F", 0),
        "escrow_minor": totals.get("G", 0),
        "total_minor": upfront + totals.get("F", 0) + totals.get("G", 0),
        "note": "Prepaids and escrow deposits are borrower funds held or applied on the borrower's behalf; they are not lender profit or a permanent cost.",
    }

    sens_h: Dict[str, int] = {}
    for h in sensitivity_horizons:
        sens_h[str(h)] = _economic_cost(sched, h, upfront, current.balance_minor) - _economic_cost(keep_sched, h, 0, current.balance_minor)
    sens_cost: Dict[str, int] = {}
    for shift in cost_shifts:
        shifted_net = max(0, incremental_net + shift)
        if finance_costs:
            s2 = build_schedule(current.balance_minor + shifted_net, offer.note_rate, offer.term_months)
            sens_cost[str(shift)] = _economic_cost(s2, horizon, 0, current.balance_minor) - keep.economic_cost_at_horizon_minor
        else:
            sens_cost[str(shift)] = _economic_cost(sched, horizon, shifted_net, current.balance_minor) - keep.economic_cost_at_horizon_minor
    alt_financed = build_schedule(current.balance_minor + incremental_net, offer.note_rate, offer.term_months)
    alt_cash = build_schedule(current.balance_minor, offer.note_rate, offer.term_months)
    sensitivity = {
        "horizon_difference_minor": sens_h,
        "cost_shift_difference_minor": sens_cost,
        "financed_costs_difference_minor": _economic_cost(alt_financed, horizon, 0, current.balance_minor) - keep.economic_cost_at_horizon_minor,
        "cash_costs_difference_minor": _economic_cost(alt_cash, horizon, incremental_net, current.balance_minor) - keep.economic_cost_at_horizon_minor,
    }

    if offer.term_months > current.remaining_months and monthly_change < 0:
        notes.append(
            "Lower monthly payment partly reflects a longer term; remaining principal at the horizon is reported separately."
        )

    label = f"{offer.lender_name}: {str(offer.note_rate * 100).rstrip('0').rstrip('.')}% / {offer.term_months} months"
    return ScenarioResult(
        scenario_id=offer.document_id,
        label=label,
        lender_id=offer.lender_id,
        offer_document_id=offer.document_id,
        principal_minor=principal,
        note_rate=offer.note_rate,
        term_months=offer.term_months,
        monthly_pi_minor=sched.scheduled_payment_minor,
        monthly_change_vs_keep_minor=monthly_change,
        cumulative_pi_at_horizon_minor=sched.cumulative_payments_minor(horizon),
        cumulative_interest_at_horizon_minor=sched.cumulative_interest_minor(horizon),
        remaining_balance_at_horizon_minor=sched.balance_after_minor(horizon),
        upfront_incremental_costs_minor=upfront,
        financed_costs_minor=financed,
        economic_cost_at_horizon_minor=cost,
        economic_difference_vs_keep_minor=diff,
        total_interest_over_life_minor=sched.total_interest_minor,
        cash_to_close=cash_to_close,
        economic_break_even_month=break_even,
        simple_break_even_months=simple_be,
        rankable=offer.rankable and not expired,
        expired=expired,
        missing_fields=list(offer.missing_fields),
        contradictions=list(offer.contradictions),
        notes=notes,
        sensitivity=sensitivity,
        schedule_fingerprint=sched.fingerprint(),
    )


def compare_scenarios(
    current: CurrentLoan,
    offers: List[NormalizedOffer],
    horizon_months: int,
    now: datetime,
    finance_costs: Optional[Dict[str, bool]] = None,
    maximum_cash_to_close_minor: Optional[int] = None,
    sensitivity_horizons: tuple = DEFAULT_SENSITIVITY_HORIZONS,
    cost_shifts: tuple = DEFAULT_COST_SHIFTS_MINOR,
) -> ComparisonResult:
    if horizon_months <= 0:
        raise ValueError("horizon_months must be positive")
    finance_costs = finance_costs or {}
    keep_sched = current.schedule()
    keep = _keep_scenario(current, horizon_months)
    results: List[ScenarioResult] = []
    for offer in offers:
        res = _offer_scenario(
            current,
            keep,
            keep_sched,
            offer,
            horizon_months,
            bool(finance_costs.get(offer.document_id, False)),
            now,
            sensitivity_horizons,
            cost_shifts,
        )
        if maximum_cash_to_close_minor is not None and res.rankable:
            total = res.cash_to_close.get("total_minor", 0)
            if total > maximum_cash_to_close_minor:
                res.notes.append(
                    f"Cash to close {format_minor(total)} exceeds the borrower's stated maximum {format_minor(maximum_cash_to_close_minor)}; consider financing costs."
                )
                res.cash_to_close["exceeds_maximum"] = True
        results.append(res)

    ranked = sorted(
        [r for r in results if r.rankable],
        key=lambda r: (r.economic_cost_at_horizon_minor, r.remaining_balance_at_horizon_minor),
    )
    unranked = [r.scenario_id for r in results if not r.rankable]
    best = ranked[0] if ranked else None
    if best is not None and best.economic_difference_vs_keep_minor < 0:
        decision = "refinance_candidate"
        reason = (
            f"{best.label} is estimated to cost {format_minor(abs(best.economic_difference_vs_keep_minor))} less than keeping "
            f"the current loan over {horizon_months} months, including {format_minor(best.upfront_incremental_costs_minor)} of upfront costs"
            + (f" and {format_minor(best.financed_costs_minor)} of financed costs" if best.financed_costs_minor else "")
            + f"; remaining principal at month {horizon_months} would be {format_minor(best.remaining_balance_at_horizon_minor)} versus {format_minor(keep.remaining_balance_at_horizon_minor)}."
        )
    else:
        decision = "keep_current"
        if best is None:
            reason = "No offer is complete enough to rank; keep the current loan until missing fields are supplied."
        else:
            reason = (
                f"Over {horizon_months} months no ranked offer recovers its costs; the best candidate ({best.label}) "
                f"would cost {format_minor(best.economic_difference_vs_keep_minor)} more than keeping the current loan"
                + (f" and breaks even only at month {best.economic_break_even_month}" if best.economic_break_even_month else " and never breaks even")
                + "."
            )

    lowest_rate = min((r for r in ranked if r.note_rate is not None), key=lambda r: r.note_rate, default=None)
    recommendation = {
        "decision": decision,
        "reason": reason,
        "best_offer_id": best.scenario_id if best else None,
        "ranked_offer_ids": [r.scenario_id for r in ranked],
        "unranked_offer_ids": unranked,
        "lowest_rate_offer_id": lowest_rate.scenario_id if lowest_rate else None,
        "horizon_months": horizon_months,
        "fee_assumption": "incremental costs = categories A+B+C+E+H minus lender credits; F and G excluded",
    }

    assumptions = [
        f"Calculation version {CALCULATION_VERSION}: interest rounded half-up to the cent each period; level payment rounded to the cent; final payment adjusted to clear the balance.",
        "Offers are compared like-for-like at the current outstanding balance; financed costs are added to principal and not counted as upfront cash.",
        "Prepaids (F) and initial escrow (G) are shown in cash-to-close and excluded from economic cost.",
        "Disclosed APR is carried as a separate field; a compliant APR is not computed.",
        "Results are estimates for decision support and are not a lender commitment.",
    ]
    return ComparisonResult(
        mortgage_id=current.mortgage_id,
        horizon_months=horizon_months,
        starting_balance_minor=current.balance_minor,
        calculation_version=CALCULATION_VERSION,
        computed_at=now,
        keep=keep,
        offers=results,
        recommendation=recommendation,
        assumptions=assumptions,
        currency=current.currency,
    )


def current_loan_from_record(record: Dict[str, Any]) -> CurrentLoan:
    as_of = record.get("as_of")
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    return CurrentLoan(
        mortgage_id=str(record["id"]),
        balance_minor=int(record["balance_minor"]),
        note_rate=normalize_rate(record["note_rate_decimal"]),
        remaining_months=int(record["remaining_months"]),
        monthly_pi_minor=record.get("monthly_pi_minor"),
        escrow_minor=int(record.get("escrow_minor") or 0),
        as_of=as_of,
        currency=record.get("currency", "USD"),
    )
