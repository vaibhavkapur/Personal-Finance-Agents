"""Verify an issued policy against the approved application before presenting coverage as active."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .money import fmt_minor

MATCHED_FIELDS = (
    "property_limit_minor",
    "liability_limit_minor",
    "deductible_minor",
    "annual_premium_minor",
    "currency",
    "replacement_cost",
    "policy_form_version",
)


def verify_policy(approved_application: Dict[str, Any], declarations: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """Compare declarations with approved terms. Any mismatch fails verification."""
    terms = approved_application["terms"]
    applicant = approved_application["applicant"]
    mismatches: List[Dict[str, Any]] = []

    def mismatch(field: str, expected: Any, actual: Any, detail: str) -> None:
        mismatches.append({"field": field, "expected": expected, "actual": actual, "detail": detail})

    if not declarations.get("insurer_policy_ref"):
        mismatch("insurer_policy_ref", "present", None, "Declarations do not carry an insurer policy reference.")
    if declarations.get("insurer_id") and declarations["insurer_id"] != terms["insurer_id"]:
        mismatch("insurer_id", terms["insurer_id"], declarations.get("insurer_id"), "Policy issued by a different insurer.")

    for field in MATCHED_FIELDS:
        expected = terms.get(field)
        actual = declarations.get(field)
        if expected != actual:
            if field.endswith("_minor") and isinstance(expected, int) and isinstance(actual, int):
                detail = "Approved %s, issued %s." % (fmt_minor(expected), fmt_minor(actual))
            else:
                detail = "Approved %r, issued %r." % (expected, actual)
            mismatch(field, expected, actual, detail)

    expected_effective = terms.get("effective_date")
    actual_effective = declarations.get("effective_at")
    if isinstance(actual_effective, (datetime, date)):
        actual_effective = actual_effective.isoformat()[:10]
    if expected_effective != actual_effective:
        mismatch("effective_date", expected_effective, actual_effective,
                 "Issued effective date differs from the approved start date.")

    if sorted(declarations.get("exclusions", [])) != sorted(terms.get("exclusions", [])):
        mismatch("exclusions", terms.get("exclusions"), declarations.get("exclusions"), "Exclusion set differs from the approved quote.")
    if sorted(declarations.get("endorsements", [])) != sorted(terms.get("endorsements", [])):
        mismatch("endorsements", terms.get("endorsements"), declarations.get("endorsements"), "Endorsement set differs from the approved quote.")

    if applicant.get("display_name") and declarations.get("insured_name") != applicant.get("display_name"):
        mismatch("insured_name", applicant.get("display_name"), declarations.get("insured_name"), "Named insured differs.")
    approved_address = applicant.get("address")
    if isinstance(approved_address, dict) and declarations.get("address") != approved_address:
        mismatch("address", approved_address, declarations.get("address"), "Covered address differs.")

    coverage_starts_in_future = False
    if today is not None and actual_effective:
        try:
            coverage_starts_in_future = date.fromisoformat(actual_effective) > today
        except ValueError:
            pass

    return {
        "schema": "policy-verification/v1",
        "verified": not mismatches,
        "mismatches": mismatches,
        "insurer_policy_ref": declarations.get("insurer_policy_ref"),
        "effective_at": actual_effective,
        "expires_at": declarations.get("expires_at"),
        "coverage_starts_in_future": coverage_starts_in_future,
        "display_label": "issued (coverage starts %s)" % actual_effective if coverage_starts_in_future else "issued",
        "verified_against_quote_ref": terms.get("quote_ref"),
        "declarations_document_id": declarations.get("document_id"),
    }
