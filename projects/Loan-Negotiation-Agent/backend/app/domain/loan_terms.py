"""Loan Estimate normalization.

Synthetic Loan Estimates arrive as structured documents (see ``fixtures/offers``).
The normalizer extracts principal, rate, term and cost categories with source
references, classifies which cost items are *incremental* (a real cost of
refinancing) versus *pass-through* (prepaids and escrow deposits the borrower
would owe under any loan), and flags missing or contradictory values.

Cost categories follow the CFPB Loan Estimate page 2 layout:

    A  Origination charges (points, underwriting)      incremental
    B  Services you cannot shop for                    incremental
    C  Services you can shop for                       incremental
    E  Taxes and other government fees                 incremental
    F  Prepaids (interest, insurance, taxes)           pass-through
    G  Initial escrow payment at closing               pass-through
    H  Other                                           incremental
    Lender credits                                     reduce incremental cost
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .amortization import monthly_payment_minor
from .money import normalize_rate

INCREMENTAL_CATEGORIES = ("A", "B", "C", "E", "H")
PASS_THROUGH_CATEGORIES = ("F", "G")
ALL_CATEGORIES = INCREMENTAL_CATEGORIES + PASS_THROUGH_CATEGORIES

CATEGORY_LABELS = {
    "A": "Origination charges",
    "B": "Services you cannot shop for",
    "C": "Services you can shop for",
    "E": "Taxes and other government fees",
    "F": "Prepaids",
    "G": "Initial escrow payment at closing",
    "H": "Other",
}

REQUIRED_FIELDS = ("loan_amount_minor", "term_months", "note_rate_decimal", "cost_items", "expires_at")
# Fields that are needed before an offer can be ranked against others.
RANKING_FIELDS = ("loan_amount_minor", "term_months", "note_rate_decimal", "cost_items", "expires_at", "rate_lock")


@dataclass(frozen=True)
class SourceRef:
    document_id: str
    page: Optional[int] = None
    section: Optional[str] = None
    label: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"document_id": self.document_id, "page": self.page, "section": self.section, "label": self.label}


@dataclass(frozen=True)
class CostItem:
    category: str
    label: str
    amount_minor: int
    source: SourceRef

    @property
    def incremental(self) -> bool:
        return self.category in INCREMENTAL_CATEGORIES

    def as_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, self.category),
            "label": self.label,
            "amount_minor": self.amount_minor,
            "incremental": self.incremental,
            "source": self.source.as_dict(),
        }


@dataclass
class NormalizedOffer:
    document_id: str
    lender_id: str
    lender_name: str
    product: str
    loan_amount_minor: Optional[int]
    term_months: Optional[int]
    note_rate: Optional[Decimal]
    apr_disclosed: Optional[Decimal]
    disclosed_monthly_pi_minor: Optional[int]
    cost_items: List[CostItem]
    lender_credits_minor: int
    rate_lock: Optional[Dict[str, Any]]
    expires_at: Optional[datetime]
    issued_at: Optional[datetime]
    missing_fields: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    source_refs: List[SourceRef] = field(default_factory=list)
    currency: str = "USD"

    # Derived totals ------------------------------------------------------------------
    def category_totals_minor(self) -> Dict[str, int]:
        totals = {c: 0 for c in ALL_CATEGORIES}
        for item in self.cost_items:
            totals[item.category] = totals.get(item.category, 0) + item.amount_minor
        return totals

    @property
    def incremental_costs_gross_minor(self) -> int:
        return sum(i.amount_minor for i in self.cost_items if i.incremental)

    @property
    def incremental_costs_net_minor(self) -> int:
        """Incremental costs after lender credits; never below zero for economic purposes."""
        return self.incremental_costs_gross_minor - self.lender_credits_minor

    @property
    def pass_through_minor(self) -> int:
        return sum(i.amount_minor for i in self.cost_items if not i.incremental)

    @property
    def total_closing_costs_minor(self) -> int:
        return self.incremental_costs_gross_minor + self.pass_through_minor - self.lender_credits_minor

    @property
    def computed_monthly_pi_minor(self) -> Optional[int]:
        if self.loan_amount_minor is None or self.term_months is None or self.note_rate is None:
            return None
        return monthly_payment_minor(self.loan_amount_minor, self.note_rate, self.term_months)

    @property
    def rankable(self) -> bool:
        return not self.missing_fields and not self.contradictions

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def as_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "lender_id": self.lender_id,
            "lender_name": self.lender_name,
            "product": self.product,
            "currency": self.currency,
            "loan_amount_minor": self.loan_amount_minor,
            "term_months": self.term_months,
            "note_rate_decimal": str(self.note_rate) if self.note_rate is not None else None,
            "apr_disclosed_decimal": str(self.apr_disclosed) if self.apr_disclosed is not None else None,
            "disclosed_monthly_pi_minor": self.disclosed_monthly_pi_minor,
            "computed_monthly_pi_minor": self.computed_monthly_pi_minor,
            "cost_items": [c.as_dict() for c in self.cost_items],
            "category_totals_minor": self.category_totals_minor(),
            "incremental_costs_gross_minor": self.incremental_costs_gross_minor,
            "lender_credits_minor": self.lender_credits_minor,
            "incremental_costs_net_minor": self.incremental_costs_net_minor,
            "pass_through_minor": self.pass_through_minor,
            "total_closing_costs_minor": self.total_closing_costs_minor,
            "rate_lock": self.rate_lock,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "missing_fields": list(self.missing_fields),
            "contradictions": list(self.contradictions),
            "rankable": self.rankable,
            "source_refs": [s.as_dict() for s in self.source_refs],
        }


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def normalize_offer_document(doc: Dict[str, Any]) -> NormalizedOffer:
    """Normalize one synthetic Loan Estimate document.

    Instructions embedded in the document are treated as data only; nothing in the
    document changes tool behaviour or permissions.
    """
    document_id = str(doc.get("document_id") or doc.get("id") or "unknown_document")
    missing: List[str] = []
    contradictions: List[str] = []
    refs: List[SourceRef] = []

    def ref(section: str, label: Optional[str] = None, page: Optional[int] = None) -> SourceRef:
        r = SourceRef(document_id=document_id, page=page, section=section, label=label)
        refs.append(r)
        return r

    loan_amount = doc.get("loan_amount_minor")
    if loan_amount is None:
        missing.append("loan_amount_minor")
    else:
        loan_amount = int(loan_amount)
        ref("Loan Terms", "Loan Amount", page=1)

    term_months = doc.get("term_months")
    if term_months is None:
        missing.append("term_months")
    else:
        term_months = int(term_months)
        ref("Loan Terms", "Loan Term", page=1)

    note_rate = doc.get("note_rate_decimal")
    if note_rate is None:
        missing.append("note_rate_decimal")
    else:
        note_rate = normalize_rate(note_rate)
        ref("Loan Terms", "Interest Rate", page=1)

    apr = doc.get("apr_disclosed_decimal")
    apr_dec = normalize_rate(apr) if apr is not None else None
    if apr_dec is not None:
        ref("Comparisons", "APR", page=3)

    disclosed_pi = doc.get("monthly_pi_minor")
    disclosed_pi = int(disclosed_pi) if disclosed_pi is not None else None

    raw_items = doc.get("cost_items")
    items: List[CostItem] = []
    if raw_items is None:
        missing.append("cost_items")
    else:
        for raw in raw_items:
            category = str(raw.get("category", "")).upper()
            if category not in ALL_CATEGORIES:
                contradictions.append(f"cost item '{raw.get('label')}' has unknown category '{category}'")
                continue
            amount = raw.get("amount_minor")
            if amount is None:
                missing.append(f"cost_items[{raw.get('label')}].amount_minor")
                continue
            src = raw.get("source") or {}
            item = CostItem(
                category=category,
                label=str(raw.get("label", CATEGORY_LABELS[category])),
                amount_minor=int(amount),
                source=SourceRef(document_id=document_id, page=src.get("page", 2), section=src.get("section", category), label=raw.get("label")),
            )
            items.append(item)
            refs.append(item.source)

    credits = int(doc.get("lender_credits_minor") or 0)
    if credits < 0:
        contradictions.append("lender_credits_minor must be non-negative (credits are stored as positive amounts)")
        credits = abs(credits)

    rate_lock = doc.get("rate_lock")
    if rate_lock is None:
        missing.append("rate_lock")

    expires_at = _parse_dt(doc.get("expires_at"))
    if expires_at is None:
        missing.append("expires_at")
    issued_at = _parse_dt(doc.get("issued_at"))

    offer = NormalizedOffer(
        document_id=document_id,
        lender_id=str(doc.get("lender_id", "unknown_lender")),
        lender_name=str(doc.get("lender_name", doc.get("lender_id", "Unknown lender"))),
        product=str(doc.get("product", "fixed-rate")),
        loan_amount_minor=loan_amount,
        term_months=term_months,
        note_rate=note_rate,
        apr_disclosed=apr_dec,
        disclosed_monthly_pi_minor=disclosed_pi,
        cost_items=items,
        lender_credits_minor=credits,
        rate_lock=rate_lock,
        expires_at=expires_at,
        issued_at=issued_at,
        missing_fields=missing,
        contradictions=contradictions,
        source_refs=refs,
    )

    # Consistency checks between disclosed and computed values.
    computed = offer.computed_monthly_pi_minor
    if computed is not None and disclosed_pi is not None and abs(computed - disclosed_pi) > 1:
        contradictions.append(
            f"disclosed monthly P&I {disclosed_pi} differs from computed {computed} for the stated rate/term/amount"
        )
    if term_months is not None and term_months <= 0:
        contradictions.append("term_months must be positive")
    if note_rate is not None and (note_rate < 0 or note_rate > Decimal("0.30")):
        contradictions.append("note_rate_decimal is outside a plausible range")
    if doc.get("product", "").lower().find("adjustable") >= 0 or doc.get("product", "").upper().find("ARM") >= 0:
        contradictions.append("adjustable-rate products are out of scope for this MVP")
    offer.contradictions = contradictions
    return offer
