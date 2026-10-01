"""Decision explanation and appeal support.

Each insurer reason is compared with the applicable policy version and the verified facts. A challenge is produced only
when a fact and a policy clause contradict the reason. Supported rejections are explained, not fought.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..clock import iso, parse_iso
from .policy import PolicyVersion

SUPPORTED_BY_POLICY = "supported_by_policy"
CONTRADICTED = "contradicted_by_evidence"
CANNOT_ASSESS = "cannot_assess"
UPHELD_ON_REVIEW = "upheld_on_review"


def explain_decision(
    policy: PolicyVersion,
    decision: Dict[str, Any],
    evaluation: Dict[str, Any],
    expenses_by_id: Dict[str, Dict[str, Any]],
    attached_doc_types: List[str],
    prior_challenges: Optional[set] = None,
) -> Dict[str, Any]:
    """`prior_challenges` holds (expense_id, reason_code) pairs already appealed. A reason the insurer upheld after a
    challenge is not challenged again; the customer is told to use the remaining options instead."""
    items_out: List[Dict[str, Any]] = []
    challenges: List[Dict[str, Any]] = []
    challengeable_minor = 0
    delay_hours = evaluation.get("delay_hours")
    threshold = evaluation.get("delay_threshold_hours")
    refs = evaluation.get("evidence_refs", {})
    prior_challenges = prior_challenges or set()

    for item in decision.get("reason_items", []):
        expense = expenses_by_id.get(item.get("expense_id") or "")
        code = item.get("reason_code", "")
        rejected = int(item.get("rejected_minor", 0))
        out: Dict[str, Any] = {
            "expense_id": item.get("expense_id"),
            "receipt_id": item.get("receipt_id"),
            "accepted_minor": int(item.get("accepted_minor", 0)),
            "rejected_minor": rejected,
            "insurer_reason_code": code,
            "insurer_reason_text": item.get("reason_text", ""),
        }
        if rejected == 0 or code in ("", "RC_ACCEPTED"):
            out["policy_view"] = "accepted"
            items_out.append(out)
            continue

        if (out["expense_id"], code) in prior_challenges:
            out.update({"policy_view": UPHELD_ON_REVIEW, "explanation": "This reason was already challenged and the insurer's review upheld it. No further appeal is drafted inside this process; remaining options are a human insurance professional or a state insurance department complaint."})
            items_out.append(out)
            continue

        if code == "RC_CAP_APPLIED":
            out.update({"policy_view": SUPPORTED_BY_POLICY, "citation": policy.citation("aggregate_cap_minor"), "explanation": "The aggregate benefit cap applies under the policy; this reduction is consistent with the contract."})
        elif code == "RC_EXCLUDED_CATEGORY":
            excluded = set(policy.rule("excluded_categories").value)
            if expense and expense["category"] in excluded:
                out.update({"policy_view": SUPPORTED_BY_POLICY, "citation": policy.citation("excluded_categories"), "explanation": f"'{expense['category']}' is an excluded category under {policy.rule('excluded_categories').clause_id}."})
            elif expense and expense["category"] in set(policy.rule("covered_categories").value):
                out.update({"policy_view": CONTRADICTED, "citation": policy.citation("covered_categories"), "fact_locators": [expense["source_locator"]], "explanation": f"The receipt shows a '{expense['category']}' purchase, which {policy.rule('covered_categories').clause_id} covers."})
            else:
                out.update({"policy_view": CANNOT_ASSESS, "explanation": "Category could not be verified from evidence."})
        elif code == "RC_LATE_PURCHASE":
            window_rule = policy.rule("purchase_window")
            if expense and expense.get("purchased_at") and refs.get("delay_start") and refs.get("delay_end"):
                purchased = parse_iso(expense["purchased_at"])
                start = parse_iso(refs["delay_start"]["at"])
                end = parse_iso(refs["delay_end"]["at"])
                if start <= purchased <= end:
                    out.update({
                        "policy_view": CONTRADICTED,
                        "citation": policy.citation("purchase_window"),
                        "fact_locators": [expense["source_locator"], refs["delay_start"]["source_locator"], refs["delay_end"]["source_locator"]],
                        "explanation": f"{window_rule.clause_id} allows purchases from the start of the delay until the baggage is returned. This purchase at {expense['purchased_at']} falls between delay start {refs['delay_start']['at']} and return {refs['delay_end']['at']}. The policy sets no 24-hour purchase limit.",
                    })
                else:
                    out.update({"policy_view": SUPPORTED_BY_POLICY, "citation": policy.citation("purchase_window"), "explanation": "The purchase falls outside the policy purchase window."})
            else:
                out.update({"policy_view": CANNOT_ASSESS, "citation": policy.citation("purchase_window"), "explanation": "Purchase time or delay window is not fully evidenced."})
        elif code == "RC_NO_RECEIPT":
            if expense and expense.get("source_locator"):
                out.update({"policy_view": CONTRADICTED, "citation": policy.citation("required_evidence"), "fact_locators": [expense["source_locator"]], "explanation": "An itemized receipt for this expense is in the evidence manifest."})
            elif "card_statement" in attached_doc_types and "card_statement" in policy.rule("alternative_evidence").value.get("receipt", []):
                out.update({"policy_view": CONTRADICTED, "citation": policy.citation("alternative_evidence"), "fact_locators": ["doc:card_statement"], "explanation": "A card statement line is accepted as alternative evidence under the policy."})
            else:
                out.update({"policy_view": SUPPORTED_BY_POLICY, "citation": policy.citation("required_evidence"), "explanation": "No receipt or permitted alternative evidence is on file."})
        elif code == "RC_DELAY_THRESHOLD_NOT_MET":
            if delay_hours is not None and threshold is not None and delay_hours > threshold:
                out.update({"policy_view": CONTRADICTED, "citation": policy.citation("delay_threshold_hours"), "fact_locators": [refs["delay_start"]["source_locator"], refs["delay_end"]["source_locator"]], "explanation": f"Evidence shows a {delay_hours}-hour delay, above the {threshold}-hour threshold."})
            elif delay_hours is not None:
                out.update({"policy_view": SUPPORTED_BY_POLICY, "citation": policy.citation("delay_threshold_hours"), "explanation": "The evidenced delay does not exceed the policy threshold."})
            else:
                out.update({"policy_view": CANNOT_ASSESS, "explanation": "Delay duration is not fully evidenced."})
        else:
            out.update({"policy_view": CANNOT_ASSESS, "explanation": f"Reason code '{code}' has no approved rule mapping; manual review required."})

        if out["policy_view"] == CONTRADICTED:
            challengeable_minor += rejected
            challenges.append({
                "expense_id": out["expense_id"],
                "reason_code": code,
                "challenged_amount_minor": rejected,
                "currency": decision.get("currency", policy.currency),
                "clause_id": out["citation"]["clause_id"],
                "policy_version": policy.version,
                "fact_locators": out["fact_locators"],
                "argument": out["explanation"],
            })
        items_out.append(out)

    return {
        "decision_version": decision.get("decision_version"),
        "outcome": decision.get("outcome"),
        "accepted_minor": int(decision.get("accepted_minor", 0)),
        "rejected_minor": int(decision.get("rejected_minor", 0)),
        "currency": decision.get("currency", policy.currency),
        "items": items_out,
        "challengeable_minor": challengeable_minor,
        "challenges": challenges,
        "has_supported_challenge": bool(challenges),
        "summary": _summary(items_out, challengeable_minor, int(decision.get("accepted_minor", 0)), int(decision.get("rejected_minor", 0)), decision.get("currency", policy.currency)),
    }


def _summary(items: List[Dict[str, Any]], challengeable_minor: int, accepted: int, rejected: int, currency: str) -> str:
    supported = [i for i in items if i.get("policy_view") == SUPPORTED_BY_POLICY]
    contradicted = [i for i in items if i.get("policy_view") == CONTRADICTED]
    upheld = [i for i in items if i.get("policy_view") == UPHELD_ON_REVIEW]
    parts = [f"Insurer accepted {accepted / 100:.2f} {currency} and did not pay {rejected / 100:.2f} {currency}."]
    if supported:
        parts.append(f"{len(supported)} reduction(s) are consistent with the policy and are not worth appealing.")
    if upheld:
        parts.append(f"{len(upheld)} rejection(s) were already appealed and upheld; this process offers no further appeal.")
    if contradicted:
        parts.append(f"{len(contradicted)} rejection(s) totaling {challengeable_minor / 100:.2f} are contradicted by evidence and policy text; an appeal is supported.")
    if not supported and not contradicted and rejected:
        parts.append("Rejection reasons could not be assessed against the approved rules; manual review is needed.")
    return " ".join(parts)


def appeal_window(policy: PolicyVersion, decided_at: datetime) -> Dict[str, Any]:
    spec = policy.rule("appeal").value
    deadline = decided_at + timedelta(days=int(spec["deadline_days_after_decision"]))
    return {"process": spec["process"], "channel": spec["channel"], "deadline_at": iso(deadline), "deadline_source": f"policy {policy.policy_id} v{policy.version} {policy.rule('appeal').clause_id}", "citation": policy.citation("appeal")}


def appeal_permitted(policy: PolicyVersion, decided_at: datetime, now: datetime) -> bool:
    window = appeal_window(policy, decided_at)
    return now <= parse_iso(window["deadline_at"])
