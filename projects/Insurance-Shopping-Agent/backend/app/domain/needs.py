"""Typed renter requirements. Missing information is `unknown`, never a favorable default."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

UNKNOWN = "unknown"

MATERIAL_NEEDS_FIELDS = (
    "state_code",
    "effective_date",
    "property_limit_minor",
    "liability_limit_minor",
    "deductible_cap_minor",
    "replacement_cost_required",
    "required_item_classes",
    "address",
)

# Fields the interview must confirm before quotes can be requested.
REQUIRED_FOR_QUOTING = (
    "address",
    "effective_date",
    "property_limit_minor",
    "liability_limit_minor",
    "replacement_cost_required",
)

# Fields required before a suitability shortlist can be produced.
REQUIRED_FOR_COMPARISON = REQUIRED_FOR_QUOTING + ("deductible_cap_minor", "required_item_classes")


class Address(BaseModel):
    line1: str
    city: str
    state_code: str
    postal_code: str


class InsuranceNeeds(BaseModel):
    """Versioned requirements bundle shared with every insurer."""

    customer_id: str
    state_code: str
    product: str = "renters"
    version: int = 1
    effective_date: Optional[date] = None
    property_limit_minor: Optional[int] = None
    liability_limit_minor: Optional[int] = None
    deductible_cap_minor: Optional[int] = None
    replacement_cost_required: Optional[bool] = None
    required_item_classes: Optional[List[str]] = None
    address: Optional[Address] = None
    deductible_preference: Optional[str] = None  # lower_premium | lower_deductible
    currency: str = "USD"

    @field_validator("product")
    @classmethod
    def _only_renters(cls, v: str) -> str:
        if v != "renters":
            raise ValueError("only the renters product class is supported in this release")
        return v

    @field_validator("state_code")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("deductible_preference")
    @classmethod
    def _pref(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("lower_premium", "lower_deductible"):
            raise ValueError("deductible_preference must be lower_premium or lower_deductible")
        return v

    def missing_fields(self, for_comparison: bool = False) -> List[str]:
        fields = REQUIRED_FOR_COMPARISON if for_comparison else REQUIRED_FOR_QUOTING
        missing = [f for f in fields if getattr(self, f) is None]
        if not for_comparison and self.deductible_cap_minor is None:
            # Surface deductible preference early so the interview asks about it.
            missing.append("deductible_preference")
        return missing

    def is_ready_for_quoting(self) -> bool:
        return not [f for f in REQUIRED_FOR_QUOTING if getattr(self, f) is None]

    def to_requirements(self) -> Dict[str, Any]:
        """Normalized requirements sent to insurers. Unknown values stay unknown."""
        return {
            "schema": "renters-quote-request/v1",
            "needs_version": self.version,
            "product": self.product,
            "state_code": self.state_code,
            "effective_date": self.effective_date.isoformat() if self.effective_date else UNKNOWN,
            "property_limit_minor": self.property_limit_minor if self.property_limit_minor is not None else UNKNOWN,
            "liability_limit_minor": self.liability_limit_minor if self.liability_limit_minor is not None else UNKNOWN,
            "deductible_cap_minor": self.deductible_cap_minor if self.deductible_cap_minor is not None else UNKNOWN,
            "replacement_cost_required": self.replacement_cost_required
            if self.replacement_cost_required is not None
            else UNKNOWN,
            "required_item_classes": list(self.required_item_classes or []),
            "address": self.address.model_dump() if self.address else UNKNOWN,
            "currency": self.currency,
        }


def apply_needs_update(needs: InsuranceNeeds, updates: Dict[str, Any]) -> InsuranceNeeds:
    """Return a new needs version when a material field changes; unknown clears a field."""
    data = needs.model_dump()
    material_change = False
    for key, value in updates.items():
        if key not in data:
            raise KeyError("unknown needs field: %s" % key)
        new_value = None if value == UNKNOWN else value
        if key == "address" and isinstance(new_value, dict):
            new_value = Address(**new_value).model_dump()
        if data[key] != new_value:
            data[key] = new_value
            if key in MATERIAL_NEEDS_FIELDS:
                material_change = True
    if material_change:
        data["version"] = needs.version + 1
    return InsuranceNeeds(**data)
