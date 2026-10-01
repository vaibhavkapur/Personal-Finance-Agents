"""Versioned renters quote schema (renters-quote/v1).

A2A does not supply insurance semantics, so every insurer adapter must return this
structure. Exclusions and endorsements are source-linked objects, not a score.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

QUOTE_SCHEMA = "renters-quote/v1"
QUOTE_REQUEST_SCHEMA = "renters-quote-request/v1"


class Clause(BaseModel):
    clause_id: str
    title: str
    text: str


class ItemClassTerm(BaseModel):
    status: str  # covered | covered_with_sublimit | excluded
    sublimit_minor: Optional[int] = None
    clause_id: str


class QuoteCoverage(BaseModel):
    property_limit_minor: int
    liability_limit_minor: int
    deductible_minor: int
    replacement_cost: bool
    item_classes: Dict[str, ItemClassTerm]
    endorsements: List[Clause] = Field(default_factory=list)
    # field name -> clause id supporting the value
    citations: Dict[str, str] = Field(default_factory=dict)


class QuoteSource(BaseModel):
    environment: str  # mock | sandbox | production
    authority: str  # authoritative | estimated | simulated
    retrieved_at: datetime
    provider: str
    protocol: Optional[str] = None  # direct | a2a/<version>


class RentersQuote(BaseModel):
    schema_id: str = Field(default=QUOTE_SCHEMA, alias="schema")
    quote_ref: str
    insurer_id: str
    insurer_name: str
    needs_version: int
    quote_version: int = 1
    annual_premium_minor: int
    currency: str = "USD"
    coverage: QuoteCoverage
    exclusions: List[Clause]
    policy_form_version: str
    quoted_at: datetime
    valid_until: datetime
    effective_date: date
    answers_hash: str
    status: str = "quoted"  # quoted | input_required | declined
    source: QuoteSource
    revision_reason: Optional[str] = None
    clauses: List[Clause] = Field(default_factory=list)  # cited passages

    model_config = {"populate_by_name": True}

    def clause_text(self, clause_id: str) -> Optional[Clause]:
        for c in self.clauses:
            if c.clause_id == clause_id:
                return c
        for c in self.exclusions:
            if c.clause_id == clause_id:
                return c
        for c in self.coverage.endorsements:
            if c.clause_id == clause_id:
                return c
        return None

    def material_terms(self) -> Dict[str, Any]:
        """The terms an approval binds to."""
        return {
            "insurer_id": self.insurer_id,
            "product": "renters",
            "annual_premium_minor": self.annual_premium_minor,
            "currency": self.currency,
            "property_limit_minor": self.coverage.property_limit_minor,
            "liability_limit_minor": self.coverage.liability_limit_minor,
            "deductible_minor": self.coverage.deductible_minor,
            "replacement_cost": self.coverage.replacement_cost,
            "effective_date": self.effective_date.isoformat(),
            "policy_form_version": self.policy_form_version,
            "exclusions": sorted(c.clause_id for c in self.exclusions),
            "endorsements": sorted(c.clause_id for c in self.coverage.endorsements),
            "quote_ref": self.quote_ref,
            "quote_version": self.quote_version,
        }
