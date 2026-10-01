"""Final-term verification against the accepted offer.

A lender's final terms are compared field by field with the offer version that
the borrower approved. Any *material* change (rate, term, principal, financed
fees, incremental closing costs, payment, or new conditions) marks the review as
``requires_reapproval``; the earlier approval must not carry over.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .amortization import monthly_payment_minor
from .money import normalize_rate

MATERIAL_FIELDS = (
    "principal_minor",
    "note_rate_decimal",
    "term_months",
    "financed_costs_minor",
    "incremental_costs_net_minor",
    "monthly_pi_minor",
)
CASH_CATEGORIES = ("A", "B", "C", "E", "F", "G", "H", "lender_credits")
PAYMENT_TOLERANCE_MINOR = 1  # one cent of rounding tolerance on the payment


@dataclass
class TermDifference:
    field: str
    earlier: Any
    final: Any
    material: bool
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "earlier": self.earlier, "final": self.final, "material": self.material, "note": self.note}


@dataclass
class TermReview:
    differences: List[TermDifference] = field(default_factory=list)
    requires_reapproval: bool = False
    summary: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "differences": [d.as_dict() for d in self.differences],
            "requires_reapproval": self.requires_reapproval,
            "summary": self.summary,
        }


def _rate(value: Any) -> Optional[Decimal]:
    return normalize_rate(value) if value is not None else None


def _cat_totals(terms: Dict[str, Any]) -> Dict[str, int]:
    totals = {c: 0 for c in CASH_CATEGORIES}
    for item in terms.get("cost_items", []) or []:
        cat = str(item.get("category", "")).upper()
        if cat in totals:
            totals[cat] += int(item.get("amount_minor", 0))
    totals["lender_credits"] = int(terms.get("lender_credits_minor") or 0)
    return totals


def _incremental_net(terms: Dict[str, Any]) -> int:
    totals = _cat_totals(terms)
    return sum(totals[c] for c in ("A", "B", "C", "E", "H")) - totals["lender_credits"]


def diff_terms(earlier: Dict[str, Any], final: Dict[str, Any]) -> TermReview:
    """Compare two term dictionaries (accepted offer vs. final terms).

    Both dictionaries use the loan-offer schema: ``principal_minor``,
    ``note_rate_decimal``, ``term_months``, ``cost_items``, ``lender_credits_minor``,
    ``financed_costs_minor`` and optional ``conditions`` (list of ids).
    """
    review = TermReview()

    def add(field_name: str, e: Any, f: Any, material: bool, note: str = "") -> None:
        if e != f:
            review.differences.append(TermDifference(field_name, e, f, material, note))

    e_principal, f_principal = int(earlier.get("principal_minor", 0)), int(final.get("principal_minor", 0))
    add("principal_minor", e_principal, f_principal, True)

    e_rate, f_rate = _rate(earlier.get("note_rate_decimal")), _rate(final.get("note_rate_decimal"))
    add("note_rate_decimal", str(e_rate), str(f_rate), True)

    e_term, f_term = int(earlier.get("term_months", 0)), int(final.get("term_months", 0))
    add("term_months", e_term, f_term, True)

    e_fin, f_fin = int(earlier.get("financed_costs_minor") or 0), int(final.get("financed_costs_minor") or 0)
    add("financed_costs_minor", e_fin, f_fin, True, "Financed fees change the principal the borrower repays.")

    e_inc, f_inc = _incremental_net(earlier), _incremental_net(final)
    add("incremental_costs_net_minor", e_inc, f_inc, True, "Incremental closing costs (A+B+C+E+H minus credits).")

    e_tot, f_tot = _cat_totals(earlier), _cat_totals(final)
    for cat in CASH_CATEGORIES:
        if e_tot[cat] != f_tot[cat]:
            material = cat not in ("F", "G")
            note = "Prepaid/escrow change; cash-to-close only." if not material else ""
            review.differences.append(TermDifference(f"cost_category_{cat}_minor", e_tot[cat], f_tot[cat], material, note))

    if f_rate is not None and f_term > 0:
        e_pay = earlier.get("monthly_pi_minor")
        if e_pay is None and e_rate is not None and e_term > 0:
            e_pay = monthly_payment_minor(e_principal, e_rate, e_term)
        f_pay = final.get("monthly_pi_minor")
        if f_pay is None:
            f_pay = monthly_payment_minor(f_principal, f_rate, f_term)
        if e_pay is not None and abs(int(e_pay) - int(f_pay)) > PAYMENT_TOLERANCE_MINOR:
            review.differences.append(TermDifference("monthly_pi_minor", int(e_pay), int(f_pay), True))

    e_cond = sorted(str(c) for c in (earlier.get("conditions") or []))
    f_cond = sorted(str(c) for c in (final.get("conditions") or []))
    new_conditions = [c for c in f_cond if c not in e_cond]
    if e_cond != f_cond:
        review.differences.append(
            TermDifference("conditions", e_cond, f_cond, bool(new_conditions), "New conditions require review." if new_conditions else "Conditions cleared.")
        )

    e_status, f_status = earlier.get("status"), final.get("status")
    if e_status != f_status:
        review.differences.append(TermDifference("status", e_status, f_status, False, "Quote vs. commitment status."))

    review.requires_reapproval = any(d.material for d in review.differences)
    material = [d.field for d in review.differences if d.material]
    if not review.differences:
        review.summary = "Final terms match the approved offer."
    elif review.requires_reapproval:
        review.summary = "Material changes detected: " + ", ".join(material) + ". Earlier approval is invalid."
    else:
        review.summary = "Only non-material changes detected (prepaids/escrow/status); approval remains valid."
    return review
