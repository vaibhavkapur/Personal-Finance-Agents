"""Offer normalization and like-for-like comparison (plan §9).

* Terms, APY, fees and restrictions are normalized into ``NormalizedOffer``.
  Missing conditions are recorded, never silently filled in.
* ``compare`` evaluates every offer at a common horizon. An offer is
  *comparable* only if it is unexpired, eligibility is known, the accrual
  method is documented and its lock-up does not overlap required liquidity.
* Earnings use ``principal * ((1 + APY) ** (days / 365) - 1)`` only for
  ``apy_compound`` products. ``simple_contractual`` products use the quoted
  simple-interest payout. Unknown accrual methods are not comparable.
* Early-withdrawal effects are surfaced separately from the headline figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.domain.money import parse_rate, to_minor

ELIGIBILITY_VALUES = {"eligible", "conditional", "unknown", "ineligible"}
ACCRUAL_METHODS = {"apy_compound", "simple_contractual"}


@dataclass
class OfferSource:
    evidence_id: str | None
    retrieved_at: str
    environment: str
    authoritative: bool
    provider_id: str
    product_version: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class NormalizedOffer:
    id: str
    provider_id: str
    product_code: str
    product_name: str
    product_version: str
    offer_kind: str  # cd_renewal | cd_new | savings_transfer
    apy: Decimal
    rate_type: str
    term_days: int | None
    fees_minor: int
    fee_description: str | None
    restrictions: dict
    accrual_method: str
    rounding: str
    valid_until: date
    eligibility_status: str
    eligibility_notes: str | None
    destination_account_id: str | None
    source: OfferSource
    missing_conditions: list[str] = field(default_factory=list)

    @property
    def liquid(self) -> bool:
        return self.term_days is None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "product_code": self.product_code,
            "product_name": self.product_name,
            "product_version": self.product_version,
            "offer_kind": self.offer_kind,
            "apy_decimal": str(self.apy),
            "rate_type": self.rate_type,
            "term_days": self.term_days,
            "fees_minor": self.fees_minor,
            "fee_description": self.fee_description,
            "restrictions": self.restrictions,
            "accrual_method": self.accrual_method,
            "rounding": self.rounding,
            "valid_until": self.valid_until.isoformat(),
            "eligibility_status": self.eligibility_status,
            "eligibility_notes": self.eligibility_notes,
            "destination_account_id": self.destination_account_id,
            "liquid": self.liquid,
            "missing_conditions": list(self.missing_conditions),
            "source": self.source.to_dict(),
        }


def normalize_offer(raw: dict) -> NormalizedOffer:
    """Normalize a provider offer record. Raises ``ValueError`` only for fields
    without which the offer cannot be identified; softer gaps become
    ``missing_conditions``."""
    missing: list[str] = []
    for required in ("id", "provider_id", "product_version", "apy_decimal", "valid_until"):
        if raw.get(required) in (None, ""):
            raise ValueError(f"offer missing required field {required}")

    eligibility = raw.get("eligibility_status") or "unknown"
    if eligibility not in ELIGIBILITY_VALUES:
        eligibility = "unknown"
        missing.append("eligibility_status")

    accrual = raw.get("accrual_method") or "unknown"
    if accrual not in ACCRUAL_METHODS:
        accrual = "unknown"
        missing.append("accrual_method")

    term_days = raw.get("term_days")
    offer_kind = raw.get("offer_kind") or "unknown"
    if offer_kind in ("cd_renewal", "cd_new") and not term_days:
        missing.append("term_days")
    restrictions = raw.get("restrictions") or raw.get("restrictions_json") or {}
    if offer_kind in ("cd_renewal", "cd_new") and "early_withdrawal_penalty" not in restrictions:
        missing.append("early_withdrawal_penalty")
    if raw.get("fees_minor") is None:
        missing.append("fees_minor")
    if offer_kind in ("cd_renewal", "savings_transfer") and not raw.get("destination_account_id"):
        missing.append("destination_account_id")

    valid_until = raw["valid_until"]
    if isinstance(valid_until, str):
        valid_until = date.fromisoformat(valid_until)

    retrieved_at = raw.get("retrieved_at")
    if hasattr(retrieved_at, "isoformat"):
        retrieved_at = retrieved_at.isoformat()

    return NormalizedOffer(
        id=raw["id"],
        provider_id=raw["provider_id"],
        product_code=raw.get("product_code") or raw["id"],
        product_name=raw.get("product_name") or raw.get("product_code") or raw["id"],
        product_version=str(raw["product_version"]),
        offer_kind=offer_kind,
        apy=parse_rate(raw["apy_decimal"]),
        rate_type=raw.get("rate_type") or "fixed",
        term_days=int(term_days) if term_days else None,
        fees_minor=int(raw.get("fees_minor") or 0),
        fee_description=raw.get("fee_description"),
        restrictions=dict(restrictions),
        accrual_method=accrual,
        rounding=raw.get("rounding") or "ROUND_HALF_EVEN",
        valid_until=valid_until,
        eligibility_status=eligibility,
        eligibility_notes=raw.get("eligibility_notes"),
        destination_account_id=raw.get("destination_account_id"),
        source=OfferSource(
            evidence_id=raw.get("evidence_id"),
            retrieved_at=str(retrieved_at or ""),
            environment=raw.get("environment") or "mock",
            authoritative=bool(raw.get("authoritative", True)),
            provider_id=raw["provider_id"],
            product_version=str(raw["product_version"]),
        ),
        missing_conditions=missing,
    )


def earnings_minor(principal_minor: int, apy: Decimal, days: int, accrual_method: str, rounding: str) -> int:
    """Interest earned on ``principal_minor`` over ``days`` under the product's
    documented accrual rule."""
    if days <= 0:
        return 0
    principal = Decimal(principal_minor) / Decimal(100)
    if accrual_method == "apy_compound":
        growth = (Decimal(1) + apy) ** (Decimal(days) / Decimal(365))
        interest = principal * (growth - Decimal(1))
    elif accrual_method == "simple_contractual":
        interest = principal * apy * Decimal(days) / Decimal(365)
    else:
        raise ValueError(f"cannot compute earnings for accrual method {accrual_method!r}")
    return to_minor(interest, rounding)


@dataclass
class OfferComparison:
    offer: NormalizedOffer
    principal_minor: int
    horizon_days: int
    start_date: date
    comparable: bool
    exclusion_reasons: list[str]
    warnings: list[str]
    earnings_to_term_minor: int | None
    earnings_at_horizon_minor: int | None
    net_at_horizon_minor: int | None
    locked_until: date | None
    early_withdrawal_note: str | None
    assumptions: list[str]

    def to_dict(self) -> dict:
        return {
            "offer": self.offer.to_dict(),
            "offer_id": self.offer.id,
            "product_version": self.offer.product_version,
            "principal_minor": self.principal_minor,
            "horizon_days": self.horizon_days,
            "start_date": self.start_date.isoformat(),
            "comparable": self.comparable,
            "exclusion_reasons": self.exclusion_reasons,
            "warnings": self.warnings,
            "earnings_to_term_minor": self.earnings_to_term_minor,
            "earnings_at_horizon_minor": self.earnings_at_horizon_minor,
            "fees_minor": self.offer.fees_minor,
            "net_at_horizon_minor": self.net_at_horizon_minor,
            "locked_until": self.locked_until.isoformat() if self.locked_until else None,
            "liquid": self.offer.liquid,
            "early_withdrawal_note": self.early_withdrawal_note,
            "assumptions": self.assumptions,
            "source": self.offer.source.to_dict(),
        }


def compare_offers(
    offers: list[NormalizedOffer],
    principal_minor: int,
    start_date: date,
    as_of: date,
    horizon_days: int = 365,
    obligations_after_start: list[tuple[date, int, str]] | None = None,
    preferred_lockup_days: int | None = None,
    verified_destination_accounts: set[str] | None = None,
) -> list[OfferComparison]:
    """Evaluate all offers at a common horizon. ``obligations_after_start`` are
    (due_date, amount_minor, label) tuples that must be covered by liquidity the
    projection has *not* already reserved; an offer whose lock-up overlaps one of
    them is flagged. The projection engine remains the authority on feasibility."""
    results: list[OfferComparison] = []
    obligations_after_start = obligations_after_start or []
    for offer in offers:
        reasons: list[str] = []
        warnings: list[str] = []
        assumptions: list[str] = []

        if offer.valid_until < as_of:
            reasons.append("offer_expired")
        if offer.eligibility_status == "unknown":
            reasons.append("eligibility_unknown")
        elif offer.eligibility_status == "ineligible":
            reasons.append("ineligible")
        elif offer.eligibility_status == "conditional":
            warnings.append(f"eligibility conditional: {offer.eligibility_notes or 'conditions not recorded'}")
        if offer.accrual_method == "unknown":
            reasons.append("accrual_method_unknown")
        if offer.offer_kind in ("cd_renewal", "cd_new") and offer.term_days is None:
            reasons.append("term_missing")
        if offer.offer_kind == "unknown":
            reasons.append("offer_kind_unknown")
        min_deposit = offer.restrictions.get("minimum_deposit_minor")
        if isinstance(min_deposit, int) and principal_minor < min_deposit:
            reasons.append("below_minimum_deposit")
        if offer.offer_kind in ("cd_renewal", "savings_transfer") and offer.destination_account_id is None:
            reasons.append("destination_missing")
        if (
            verified_destination_accounts is not None
            and offer.destination_account_id is not None
            and offer.destination_account_id not in verified_destination_accounts
        ):
            reasons.append("destination_not_verified_same_owner")
        for cond in offer.missing_conditions:
            if cond not in ("eligibility_status", "accrual_method", "term_days", "destination_account_id"):
                warnings.append(f"missing condition: {cond}")

        locked_until = start_date + timedelta(days=offer.term_days) if offer.term_days else None
        if locked_until:
            for due, amount, label in obligations_after_start:
                if start_date <= due < locked_until:
                    warnings.append(
                        f"locks funds past {label} ({due.isoformat()}); the projection must cover it from other cash"
                    )
        if preferred_lockup_days is not None and offer.term_days and offer.term_days > preferred_lockup_days:
            warnings.append(
                f"term of {offer.term_days} days exceeds preferred lock-up of {preferred_lockup_days} days"
            )

        earnings_term = earnings_horizon = net = None
        if not reasons:
            term_days = offer.term_days or horizon_days
            earnings_term = earnings_minor(principal_minor, offer.apy, term_days, offer.accrual_method, offer.rounding)
            if offer.term_days is None:
                assumptions.append(
                    f"variable-rate product: {offer.apy} APY assumed constant over the {horizon_days}-day horizon"
                    if offer.rate_type == "variable"
                    else f"rate assumed constant over the {horizon_days}-day horizon"
                )
                earnings_horizon = earnings_term
            elif offer.term_days >= horizon_days:
                earnings_horizon = earnings_minor(
                    principal_minor, offer.apy, horizon_days, offer.accrual_method, offer.rounding
                )
                if offer.term_days > horizon_days:
                    assumptions.append(
                        f"horizon figure covers the first {horizon_days} of {offer.term_days} days; funds stay locked until {locked_until.isoformat()}"
                    )
            else:
                earnings_horizon = earnings_term
                assumptions.append(
                    f"term ends after {offer.term_days} days; no reinvestment assumed for the remaining {horizon_days - offer.term_days} days of the horizon"
                )
            if offer.accrual_method == "apy_compound":
                assumptions.append("APY compounding formula principal * ((1 + APY) ** (days / 365) - 1)")
            else:
                assumptions.append("contractual simple interest principal * rate * days / 365 as quoted by the provider")
            net = earnings_horizon - offer.fees_minor
            if offer.fees_minor:
                assumptions.append(f"fees of {offer.fees_minor} minor units deducted ({offer.fee_description or 'per offer'})")

        early = offer.restrictions.get("early_withdrawal_penalty")
        early_note = None
        if offer.term_days:
            early_note = (
                f"Early withdrawal penalty: {early}. Shown separately; not included in projected earnings."
                if early
                else "Early withdrawal penalty not recorded for this product."
            )

        results.append(
            OfferComparison(
                offer=offer,
                principal_minor=principal_minor,
                horizon_days=horizon_days,
                start_date=start_date,
                comparable=not reasons,
                exclusion_reasons=reasons,
                warnings=warnings,
                earnings_to_term_minor=earnings_term,
                earnings_at_horizon_minor=earnings_horizon,
                net_at_horizon_minor=net,
                locked_until=locked_until,
                early_withdrawal_note=early_note,
                assumptions=assumptions,
            )
        )
    # Stable ordering: comparable first, then by net earnings; excluded offers last.
    results.sort(key=lambda r: (not r.comparable, -(r.net_at_horizon_minor or 0), r.offer.id))
    return results


def material_terms(offer: NormalizedOffer) -> dict:
    """The fields whose change invalidates an approval (plan §13)."""
    return {
        "offer_id": offer.id,
        "product_version": offer.product_version,
        "apy_decimal": str(offer.apy),
        "term_days": offer.term_days,
        "fees_minor": offer.fees_minor,
        "destination_account_id": offer.destination_account_id,
    }
