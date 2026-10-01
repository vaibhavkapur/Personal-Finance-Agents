"""Coverage comparison engine.

Hard requirements are checked first; only suitable quotes are ranked. Unknown needs
never pass a check, and a cheaper quote missing a required coverage is excluded.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .money import fmt_minor
from .needs import InsuranceNeeds
from .quotes import RentersQuote

RANKING_RULE = (
    "Suitable quotes are sorted by annual premium (lowest first); when the customer "
    "prefers a lower deductible, deductible is sorted first and premium second."
)
RANKING_DISCLOSURE = (
    "This ordering is a product rule applied after suitability filtering. It is not an "
    "actuarial evaluation and no sample policy guarantees coverage for a specific future loss."
)


def _citation(quote: RentersQuote, clause_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not clause_id:
        return None
    clause = quote.clause_text(clause_id)
    return {
        "clause_id": clause_id,
        "policy_form_version": quote.policy_form_version,
        "title": clause.title if clause else None,
        "text": clause.text if clause else None,
        "quote_ref": quote.quote_ref,
        "source": quote.source.model_dump(mode="json"),
    }


def hard_checks(needs: InsuranceNeeds, quote: RentersQuote, now: datetime) -> List[Dict[str, Any]]:
    cov = quote.coverage
    checks: List[Dict[str, Any]] = []

    def add(field: str, requirement: Any, value: Any, passed: Optional[bool], clause: Optional[str], why: str) -> None:
        checks.append(
            {
                "field": field,
                "requirement": requirement,
                "quote_value": value,
                "result": "pass" if passed else ("unknown" if passed is None else "fail"),
                "citation": _citation(quote, clause),
                "explanation": why,
            }
        )

    # 1. property limit
    if needs.property_limit_minor is None:
        add("property_limit", "unknown", cov.property_limit_minor, None, cov.citations.get("property_limit"),
            "Requested personal property limit has not been confirmed.")
    else:
        ok = cov.property_limit_minor >= needs.property_limit_minor
        add("property_limit", needs.property_limit_minor, cov.property_limit_minor, ok, cov.citations.get("property_limit"),
            "Quoted personal property limit %s %s requested minimum %s."
            % (fmt_minor(cov.property_limit_minor), "meets" if ok else "is below", fmt_minor(needs.property_limit_minor)))

    # 2. liability limit
    if needs.liability_limit_minor is None:
        add("liability_limit", "unknown", cov.liability_limit_minor, None, cov.citations.get("liability_limit"),
            "Requested liability limit has not been confirmed.")
    else:
        ok = cov.liability_limit_minor >= needs.liability_limit_minor
        add("liability_limit", needs.liability_limit_minor, cov.liability_limit_minor, ok, cov.citations.get("liability_limit"),
            "Quoted liability limit %s %s requested minimum %s."
            % (fmt_minor(cov.liability_limit_minor), "meets" if ok else "is below", fmt_minor(needs.liability_limit_minor)))

    # 3. deductible cap
    if needs.deductible_cap_minor is None:
        add("deductible", "unknown", cov.deductible_minor, None, cov.citations.get("deductible"),
            "Maximum acceptable deductible has not been confirmed; this quote cannot be shortlisted until it is.")
    else:
        ok = cov.deductible_minor <= needs.deductible_cap_minor
        add("deductible", needs.deductible_cap_minor, cov.deductible_minor, ok, cov.citations.get("deductible"),
            "Deductible %s %s the customer's cap of %s."
            % (fmt_minor(cov.deductible_minor), "is within" if ok else "exceeds", fmt_minor(needs.deductible_cap_minor)))

    # 4. replacement cost basis
    if needs.replacement_cost_required is None:
        add("replacement_cost", "unknown", cov.replacement_cost, None, cov.citations.get("replacement_cost"),
            "Replacement-cost preference has not been confirmed.")
    elif needs.replacement_cost_required:
        add("replacement_cost", True, cov.replacement_cost, cov.replacement_cost, cov.citations.get("replacement_cost"),
            "Replacement-cost settlement is %s." % ("included" if cov.replacement_cost else "not included"))
    else:
        add("replacement_cost", False, cov.replacement_cost, True, cov.citations.get("replacement_cost"),
            "Replacement cost was not required.")

    # 5. required item classes
    if needs.required_item_classes is None:
        add("required_item_classes", "unknown", None, None, None,
            "Item classes that must be covered have not been confirmed.")
    else:
        for item_class in needs.required_item_classes:
            term = cov.item_classes.get(item_class)
            if term is None:
                add("item_class:" + item_class, "covered", "not_stated", None, None,
                    "The quote does not state terms for %s; treat as unknown, not covered." % item_class)
            elif term.status == "excluded":
                add("item_class:" + item_class, "covered", term.status, False, term.clause_id,
                    "%s is excluded by the policy form." % item_class.replace("_", " ").capitalize())
            else:
                note = ""
                if term.status == "covered_with_sublimit" and term.sublimit_minor is not None:
                    note = " with a %s special limit" % fmt_minor(term.sublimit_minor)
                add("item_class:" + item_class, "covered", term.status, True, term.clause_id,
                    "%s is covered%s." % (item_class.replace("_", " ").capitalize(), note))

    # 6. effective date
    if needs.effective_date is None:
        add("effective_date", "unknown", quote.effective_date.isoformat(), None, None, "Desired start date not confirmed.")
    else:
        ok = quote.effective_date == needs.effective_date
        add("effective_date", needs.effective_date.isoformat(), quote.effective_date.isoformat(), ok, None,
            "Quote effective date %s the requested start date." % ("matches" if ok else "differs from"))

    # 7. quote validity
    valid = quote.valid_until >= now
    add("quote_validity", "not expired at %s" % now.isoformat(), quote.valid_until.isoformat(), valid, None,
        "Quote %s." % ("is current" if valid else "has expired and must be refreshed"))

    return checks


def _difference_rows(needs: InsuranceNeeds, quotes: List[RentersQuote]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def row(field: str, label: str, getter, clause_key: Optional[str] = None, note: Optional[str] = None) -> None:
        values: Dict[str, Any] = {}
        for q in quotes:
            value, display, clause_id = getter(q)
            values[q.quote_ref] = {
                "insurer_id": q.insurer_id,
                "value": value,
                "display": display,
                "citation": _citation(q, clause_id),
            }
        distinct = {str(v["value"]) for v in values.values()}
        rows.append({"field": field, "label": label, "values": values, "differs": len(distinct) > 1, "note": note})

    row("annual_premium", "Annual premium",
        lambda q: (q.annual_premium_minor, fmt_minor(q.annual_premium_minor, q.currency), None),
        note="Premium is a quote figure, not a policy-form clause.")
    row("deductible", "Deductible per occurrence",
        lambda q: (q.coverage.deductible_minor, fmt_minor(q.coverage.deductible_minor), q.coverage.citations.get("deductible")))
    row("property_limit", "Personal property limit",
        lambda q: (q.coverage.property_limit_minor, fmt_minor(q.coverage.property_limit_minor), q.coverage.citations.get("property_limit")))
    row("liability_limit", "Personal liability limit",
        lambda q: (q.coverage.liability_limit_minor, fmt_minor(q.coverage.liability_limit_minor), q.coverage.citations.get("liability_limit")))
    row("replacement_cost", "Replacement-cost settlement",
        lambda q: (q.coverage.replacement_cost, "yes" if q.coverage.replacement_cost else "no", q.coverage.citations.get("replacement_cost")))
    all_classes = sorted({c for q in quotes for c in q.coverage.item_classes})
    for item_class in all_classes:
        def getter(q: RentersQuote, ic: str = item_class):
            term = q.coverage.item_classes.get(ic)
            if term is None:
                return ("not_stated", "not stated", None)
            display = term.status.replace("_", " ")
            if term.sublimit_minor is not None:
                display += " (%s)" % fmt_minor(term.sublimit_minor)
            return (term.status, display, term.clause_id)
        row("item_class:" + item_class, "Item class: " + item_class.replace("_", " "), getter)
    row("endorsements", "Included endorsements",
        lambda q: (sorted(e.clause_id for e in q.coverage.endorsements),
                   ", ".join(e.title for e in q.coverage.endorsements) or "none",
                   q.coverage.endorsements[0].clause_id if q.coverage.endorsements else None))
    row("exclusions", "Declared exclusions",
        lambda q: (sorted(e.clause_id for e in q.exclusions), "; ".join(e.title for e in q.exclusions), None),
        note="Each exclusion is listed with its clause reference in the quote detail.")
    row("valid_until", "Quote expires", lambda q: (q.valid_until.isoformat(), q.valid_until.date().isoformat(), None))
    row("policy_form_version", "Policy form version", lambda q: (q.policy_form_version, q.policy_form_version, None))
    return rows


def compare_coverage(
    needs: InsuranceNeeds,
    quotes: List[RentersQuote],
    now: datetime,
    missing_responses: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return suitable / excluded / undetermined quotes with citations and unknowns."""
    unknown_fields = needs.missing_fields(for_comparison=True)
    suitable: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    undetermined: List[Dict[str, Any]] = []
    quoted: List[RentersQuote] = []
    disclosed_missing: List[Dict[str, Any]] = list(missing_responses or [])

    for quote in quotes:
        if quote.status != "quoted":
            disclosed_missing.append(
                {
                    "insurer_id": quote.insurer_id,
                    "quote_ref": quote.quote_ref,
                    "status": quote.status,
                    "disclosure": "Insurer %s has not returned a comparable quote (status: %s)."
                    % (quote.insurer_name, quote.status),
                }
            )
            continue
        quoted.append(quote)
        checks = hard_checks(needs, quote, now)
        failed = [c for c in checks if c["result"] == "fail"]
        unknown = [c for c in checks if c["result"] == "unknown"]
        entry = {
            "quote_ref": quote.quote_ref,
            "insurer_id": quote.insurer_id,
            "insurer_name": quote.insurer_name,
            "quote_version": quote.quote_version,
            "annual_premium_minor": quote.annual_premium_minor,
            "annual_premium_display": fmt_minor(quote.annual_premium_minor, quote.currency),
            "deductible_minor": quote.coverage.deductible_minor,
            "policy_form_version": quote.policy_form_version,
            "valid_until": quote.valid_until.isoformat(),
            "checks": checks,
            "source": quote.source.model_dump(mode="json"),
        }
        if failed:
            entry["failed_checks"] = failed
            entry["reason"] = " ".join(c["explanation"] for c in failed)
            excluded.append(entry)
        elif unknown:
            entry["unknown_checks"] = unknown
            entry["reason"] = "Cannot be shortlisted until: " + "; ".join(c["explanation"] for c in unknown)
            undetermined.append(entry)
        else:
            suitable.append(entry)

    preference = needs.deductible_preference or "lower_premium"
    if preference == "lower_deductible":
        suitable.sort(key=lambda e: (e["deductible_minor"], e["annual_premium_minor"]))
    else:
        suitable.sort(key=lambda e: (e["annual_premium_minor"], e["deductible_minor"]))
    for idx, entry in enumerate(suitable, start=1):
        entry["rank"] = idx

    trade_offs: List[str] = []
    if len(suitable) >= 2:
        a, b = suitable[0], suitable[1]
        if a["deductible_minor"] != b["deductible_minor"]:
            cheaper, dearer = (a, b) if a["annual_premium_minor"] <= b["annual_premium_minor"] else (b, a)
            trade_offs.append(
                "%s costs %s less per year than %s but carries a %s deductible versus %s."
                % (
                    cheaper["insurer_name"],
                    fmt_minor(dearer["annual_premium_minor"] - cheaper["annual_premium_minor"]),
                    dearer["insurer_name"],
                    fmt_minor(cheaper["deductible_minor"]),
                    fmt_minor(dearer["deductible_minor"]),
                )
            )
    for entry in excluded:
        cheapest_suitable = min((e["annual_premium_minor"] for e in suitable), default=None)
        if cheapest_suitable is not None and entry["annual_premium_minor"] < cheapest_suitable:
            trade_offs.append(
                "%s is the cheapest offer (%s) but is excluded: %s"
                % (entry["insurer_name"], entry["annual_premium_display"], entry["reason"])
            )

    return {
        "schema": "renters-comparison/v1",
        "needs_version": needs.version,
        "generated_at": now.isoformat(),
        "unknown_needs_fields": unknown_fields,
        "suitable": suitable,
        "excluded": excluded,
        "undetermined": undetermined,
        "missing_responses": disclosed_missing,
        "differences": _difference_rows(needs, quoted),
        "trade_offs": trade_offs,
        "ranking": {"preference": preference, "rule": RANKING_RULE, "disclosure": RANKING_DISCLOSURE},
        "complete": not disclosed_missing,
    }
