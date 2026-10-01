"""Claim calculator: applies the approved fixture rules to verified facts and receipts.

Output is an *estimate* of what the policy supports. It is never the insurer's decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..clock import iso, parse_iso
from .policy import PolicyVersion

SUPPORTED = "supported"
EXCLUDED = "excluded"
UNCERTAIN = "uncertain"
DUPLICATE = "duplicate"
UNKNOWN = "unknown"
UNSUPPORTED = "unsupported"


@dataclass
class FactView:
    id: str
    fact_type: str
    value: Dict[str, Any]
    confirmation_status: str
    evidence_id: Optional[str]
    source_locator: str
    confidence: str = "high"
    superseded: bool = False

    @property
    def usable(self) -> bool:
        return not self.superseded and self.confirmation_status in ("extracted", "confirmed", "customer_statement")


@dataclass
class ExpenseView:
    id: str
    receipt_id: str
    receipt_hash: str
    merchant: str
    purchased_at: Optional[str]
    amount_minor: int
    currency: str
    category: str
    description: str
    source_locator: str
    receipt_flags: List[str] = field(default_factory=list)
    receipt_confirmation_status: str = "extracted"
    confirmed_distinct: bool = False
    marked_duplicate: bool = False
    created_order: int = 0


@dataclass
class ExpenseOutcome:
    expense_id: str
    status: str
    rule_id: Optional[str]
    reason: Optional[str]
    duplicate_of: Optional[str] = None
    needs: Optional[str] = None  # field that needs confirmation

    def to_dict(self) -> Dict[str, Any]:
        return {"expense_id": self.expense_id, "status": self.status, "rule_id": self.rule_id, "reason": self.reason, "duplicate_of": self.duplicate_of, "needs": self.needs}


def _first_fact(facts: List[FactView], fact_type: str, predicate=None) -> Optional[FactView]:
    """Evidence-backed facts win over customer statements; otherwise first usable fact."""
    candidates = [f for f in facts if f.fact_type == fact_type and f.usable and (predicate is None or predicate(f))]
    candidates.sort(key=lambda f: 0 if f.evidence_id else 1)
    return candidates[0] if candidates else None


def _cite(policy: PolicyVersion, rule_name: str) -> Dict[str, Any]:
    return policy.citation(rule_name)


def evaluate_case(
    policy: PolicyVersion,
    loss_at: datetime,
    now: datetime,
    facts: List[FactView],
    expenses: List[ExpenseView],
    attached_doc_types: List[str],
    claimant_name: str,
) -> Dict[str, Any]:
    conditions: List[Dict[str, Any]] = []
    missing_fields: List[str] = []
    questions: List[Dict[str, Any]] = []
    evidence_refs: Dict[str, Any] = {}

    # --- delay start / end -------------------------------------------------------------------------
    arrival = _first_fact(facts, "scheduled_arrival_at", lambda f: f.value.get("at"))
    reported = _first_fact(facts, "delay_reported_at", lambda f: f.value.get("at"))
    delivered = _first_fact(facts, "baggage_delivered_at", lambda f: f.value.get("at"))
    delay_start: Optional[datetime] = None
    delay_start_source = None
    if arrival:
        delay_start = parse_iso(arrival.value["at"])
        delay_start_source = arrival.source_locator
    elif reported:
        delay_start = parse_iso(reported.value["at"])
        delay_start_source = reported.source_locator
    delay_end: Optional[datetime] = parse_iso(delivered.value["at"]) if delivered else None
    if delay_start:
        evidence_refs["delay_start"] = {"at": iso(delay_start), "source_locator": delay_start_source}
    if delay_end:
        evidence_refs["delay_end"] = {"at": iso(delay_end), "source_locator": delivered.source_locator}

    threshold_rule = policy.rule("delay_threshold_hours")
    threshold_hours = int(threshold_rule.value)
    delay_hours: Optional[float] = None
    if delay_start and delay_end:
        delay_hours = round((delay_end - delay_start).total_seconds() / 3600, 2)
        status = SUPPORTED if delay_hours > threshold_hours else UNSUPPORTED
        conditions.append({"id": "delay_threshold_met", "status": status, "detail": f"Baggage delayed {delay_hours} hours; policy requires more than {threshold_hours} hours.", "citation": _cite(policy, "delay_threshold_hours"), "facts": [delay_start_source, delivered.source_locator]})
    else:
        detail = "Delay start is unknown; itinerary arrival time or carrier delay report is missing." if not delay_start else "Baggage delivery time is unknown; arrival confirmation is missing."
        conditions.append({"id": "delay_threshold_met", "status": UNKNOWN, "detail": detail, "citation": _cite(policy, "delay_threshold_hours"), "facts": [s for s in [delay_start_source] if s]})
        if not delay_start:
            missing_fields.append("delay_start_time")
            questions.append({"field": "delay_start_time", "question": "When did your flight arrive and when was the baggage delay first recorded? Please upload the carrier's delay report if you have it.", "kind": "fact"})
        if not delay_end:
            if "baggage_arrival_confirmation" not in attached_doc_types:
                missing_fields.append("baggage_arrival_confirmation")
                questions.append({"field": "baggage_delivered_at", "question": "When was your bag returned to you? If you have the carrier's delivery confirmation, upload it; otherwise state the date and time.", "kind": "fact"})
            else:
                missing_fields.append("baggage_delivered_at")

    # --- stated loss date must agree with the evidenced delay start ----------------------------------------
    if delay_start:
        gap_hours = abs((delay_start - loss_at).total_seconds()) / 3600
        if gap_hours > 48:
            conditions.append({"id": "loss_date_matches_evidence", "status": UNKNOWN, "detail": f"Stated loss date {iso(loss_at)} is {gap_hours:.0f} hours away from the evidenced delay start {iso(delay_start)}. The policy version is chosen from the loss date, so it must be confirmed.", "citation": None, "facts": [delay_start_source]})
            missing_fields.append("loss_date")
            questions.append({"field": "loss_date", "question": f"Your documents show the baggage delay began {iso(delay_start)}, but the case was opened with loss date {iso(loss_at)}. Which date is correct?", "kind": "fact"})
        else:
            conditions.append({"id": "loss_date_matches_evidence", "status": SUPPORTED, "detail": "Stated loss date agrees with the evidenced delay start.", "citation": None, "facts": [delay_start_source]})

    # --- destination is not home ---------------------------------------------------------------------
    home = _first_fact(facts, "home_location")
    delivery = _first_fact(facts, "delivery_address")
    segment = _first_fact(facts, "flight_segment")
    delayed_flight = _first_fact(facts, "delayed_flight")
    if delayed_flight and segment and segment.value.get("flight") != delayed_flight.value.get("flight"):
        seg_match = _first_fact(facts, "flight_segment", lambda f: f.value.get("flight") == delayed_flight.value.get("flight"))
        segment = seg_match or segment
    home_city = (home.value.get("city") or "").split(",")[0].strip().lower() if home else None
    dest_city = (segment.value.get("destination_city") or "").strip().lower() if segment else None
    statement = _first_fact(facts, "destination_statement")
    is_home = None
    if delivery and delivery.value.get("is_home"):
        is_home = True
    elif home_city and dest_city:
        is_home = dest_city == home_city
    elif statement is not None:
        is_home = not bool(statement.value.get("not_home"))
    if is_home is None:
        conditions.append({"id": "destination_not_home", "status": UNKNOWN, "detail": "Cannot verify the delayed arrival was at a destination other than home.", "citation": _cite(policy, "excludes_home_arrival"), "facts": []})
        missing_fields.append("destination_confirmation")
        questions.append({"field": "destination_confirmation", "question": "Was the delayed bag for your arrival at a destination other than your home city?", "kind": "fact"})
    else:
        used = [f for f in [segment, home, delivery] if f] if (home_city and dest_city) or (delivery and delivery.value.get("is_home")) else [statement]
        conditions.append({"id": "destination_not_home", "status": UNSUPPORTED if is_home else SUPPORTED, "detail": "Delay occurred on arrival at home; the fixture policy excludes that." if is_home else f"Arrival at {dest_city or 'destination'} is away from home.", "citation": _cite(policy, "excludes_home_arrival"), "facts": [f.source_locator for f in used if f]})

    # --- identity ------------------------------------------------------------------------------------
    names = [f for f in facts if f.fact_type == "passenger_name" and f.usable]
    if names:
        mismatched = [f for f in names if f.value.get("name", "").strip().lower() != claimant_name.strip().lower()]
        conditions.append({"id": "claimant_identity_matches", "status": UNSUPPORTED if mismatched else SUPPORTED, "detail": "Passenger name on documents differs from the claimant." if mismatched else "Passenger name on documents matches the claimant.", "citation": None, "facts": [f.source_locator for f in names]})
    else:
        conditions.append({"id": "claimant_identity_matches", "status": UNKNOWN, "detail": "No document names the passenger.", "citation": None, "facts": []})
        missing_fields.append("passenger_identity")

    # --- filing deadline (from dated fixture policy) --------------------------------------------------
    deadline_days = int(policy.rule("filing_deadline_days").value)
    filing_deadline = loss_at + timedelta(days=deadline_days)
    conditions.append({"id": "filing_within_deadline", "status": SUPPORTED if now <= filing_deadline else UNSUPPORTED, "detail": f"Filing deadline under policy version {policy.version} is {iso(filing_deadline)}.", "citation": _cite(policy, "filing_deadline_days"), "facts": [], "deadline_at": iso(filing_deadline), "deadline_source": f"policy {policy.policy_id} v{policy.version} {policy.rule('filing_deadline_days').clause_id}"})

    # --- required evidence ---------------------------------------------------------------------------
    required = list(policy.rule("required_evidence").value)
    alternatives = dict(policy.rule("alternative_evidence").value)
    present = set(attached_doc_types)
    missing_docs = []
    for req in required:
        if req in present:
            continue
        alts = alternatives.get(req, [])
        if any(a in present for a in alts):
            continue
        missing_docs.append(req)
    conditions.append({"id": "required_evidence_present", "status": SUPPORTED if not missing_docs else UNKNOWN, "detail": "All required evidence types are attached." if not missing_docs else f"Missing evidence: {', '.join(missing_docs)}.", "citation": _cite(policy, "required_evidence"), "facts": [], "missing": missing_docs, "alternatives": alternatives})
    for d in missing_docs:
        if d not in missing_fields:
            missing_fields.append(d)

    # --- expenses ------------------------------------------------------------------------------------
    covered = set(policy.rule("covered_categories").value)
    excluded_cats = set(policy.rule("excluded_categories").value)
    outcomes: List[ExpenseOutcome] = []
    ordered = sorted(expenses, key=lambda e: (e.created_order, e.id))
    seen_hash: Dict[str, ExpenseView] = {}
    seen_signature: Dict[str, ExpenseView] = {}
    for e in ordered:
        sig_date = e.purchased_at[:10] if e.purchased_at else "?"
        signature = f"{e.merchant.strip().lower()}|{sig_date}|{e.amount_minor}|{e.category}"
        if e.marked_duplicate:
            outcomes.append(ExpenseOutcome(e.id, DUPLICATE, "R_DUPLICATE_CONFIRMED", "Customer confirmed this is the same purchase as another receipt.", duplicate_of=(seen_signature.get(signature).id if signature in seen_signature else None)))
            continue
        if e.receipt_hash in seen_hash and seen_hash[e.receipt_hash].receipt_id != e.receipt_id and seen_hash[e.receipt_hash].category == e.category:
            outcomes.append(ExpenseOutcome(e.id, DUPLICATE, "R_DUPLICATE_CONTENT", "Identical receipt content already claimed.", duplicate_of=seen_hash[e.receipt_hash].id))
            continue
        if signature in seen_signature and seen_signature[signature].receipt_id != e.receipt_id and not e.confirmed_distinct:
            outcomes.append(ExpenseOutcome(e.id, UNCERTAIN, "R_POSSIBLE_DUPLICATE", "Same merchant, date and amount as another receipt; confirm this is a separate purchase.", duplicate_of=seen_signature[signature].id, needs="distinct_purchase"))
            questions.append({"field": "distinct_purchase", "expense_id": e.id, "question": f"Receipt {e.receipt_id} ({e.merchant}, {e.amount_minor / 100:.2f} {e.currency}) looks like a repeat of {seen_signature[signature].receipt_id}. Is it a separate purchase?", "kind": "expense"})
            continue
        seen_hash.setdefault(e.receipt_hash, e)
        seen_signature.setdefault(signature, e)

        if e.receipt_confirmation_status == "needs_confirmation" or "items_total_mismatch" in e.receipt_flags:
            outcomes.append(ExpenseOutcome(e.id, UNCERTAIN, None, "Receipt fields need customer confirmation.", needs="receipt_confirmation"))
            questions.append({"field": "receipt_confirmation", "expense_id": e.id, "question": f"Please confirm the details of receipt {e.receipt_id} ({e.merchant}): amount {e.amount_minor / 100:.2f} {e.currency}, date {e.purchased_at or 'unknown'}.", "kind": "expense"})
            continue
        if e.category in excluded_cats:
            outcomes.append(ExpenseOutcome(e.id, EXCLUDED, policy.rule("excluded_categories").rule_id, f"Category '{e.category}' is excluded by {policy.rule('excluded_categories').clause_id}."))
            continue
        if e.category not in covered:
            outcomes.append(ExpenseOutcome(e.id, UNCERTAIN, policy.rule("covered_categories").rule_id, "Item category could not be determined from the receipt.", needs="category"))
            questions.append({"field": "category", "expense_id": e.id, "question": f"What kind of item is '{e.description}' from {e.merchant}? (clothing, toiletries, essentials, or something else)", "kind": "expense"})
            continue
        if not e.purchased_at:
            outcomes.append(ExpenseOutcome(e.id, UNCERTAIN, policy.rule("purchase_window").rule_id, "Purchase date is missing from the receipt.", needs="purchased_at"))
            questions.append({"field": "purchased_at", "expense_id": e.id, "question": f"When did you buy the items on receipt {e.receipt_id} ({e.merchant})? The receipt shows no date.", "kind": "expense"})
            continue
        purchased = parse_iso(e.purchased_at)
        if delay_start and purchased < delay_start:
            outcomes.append(ExpenseOutcome(e.id, EXCLUDED, policy.rule("purchase_window").rule_id, "Purchased before the baggage delay began."))
            continue
        if delay_end and purchased > delay_end:
            outcomes.append(ExpenseOutcome(e.id, EXCLUDED, policy.rule("purchase_window").rule_id, "Purchased after the baggage was returned."))
            continue
        outcomes.append(ExpenseOutcome(e.id, SUPPORTED, policy.rule("covered_categories").rule_id, None))

    by_status: Dict[str, int] = {SUPPORTED: 0, EXCLUDED: 0, UNCERTAIN: 0, DUPLICATE: 0}
    amounts = {e.id: e.amount_minor for e in expenses}
    for o in outcomes:
        by_status[o.status] += amounts[o.expense_id]
    cap_rule = policy.rule("aggregate_cap_minor")
    cap_minor = int(cap_rule.value)
    supported_total = by_status[SUPPORTED]
    capped = min(supported_total, cap_minor)

    blocking = [c["id"] for c in conditions if c["status"] in (UNKNOWN, UNSUPPORTED)]
    unsupported_conditions = [c["id"] for c in conditions if c["status"] == UNSUPPORTED]
    ready = not blocking and supported_total > 0 and not [o for o in outcomes if o.status == UNCERTAIN]

    # de-duplicate questions by (field, expense_id)
    seen_q = set()
    unique_questions = []
    for q in questions:
        key = (q["field"], q.get("expense_id"))
        if key in seen_q:
            continue
        seen_q.add(key)
        unique_questions.append(q)

    return {
        "authority": "estimated",
        "binding": False,
        "disclaimer": "Estimate under the approved fixture rule set. The insurer decides the claim.",
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "currency": policy.currency,
        "evaluated_at": iso(now),
        "delay_hours": delay_hours,
        "delay_threshold_hours": threshold_hours,
        "evidence_refs": evidence_refs,
        "conditions": conditions,
        "expenses": [o.to_dict() for o in outcomes],
        "totals": {
            "supported_minor": supported_total,
            "excluded_minor": by_status[EXCLUDED],
            "uncertain_minor": by_status[UNCERTAIN],
            "duplicate_minor": by_status[DUPLICATE],
            "cap_minor": cap_minor,
            "cap_citation": _cite(policy, "aggregate_cap_minor"),
            "estimated_payable_minor": capped if not unsupported_conditions else 0,
            "estimate_if_conditions_met_minor": capped,
            "cap_applied": supported_total > cap_minor,
        },
        "blocking_conditions": blocking,
        "unsupported_conditions": unsupported_conditions,
        "missing_fields": missing_fields,
        "questions": unique_questions,
        "ready_for_packet": ready,
    }
