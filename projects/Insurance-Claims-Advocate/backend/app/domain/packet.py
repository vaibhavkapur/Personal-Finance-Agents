"""Claim packet construction. A packet is an itemized claim plus an evidence manifest and disclosure manifest.
Building a packet never submits anything."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..ids import payload_hash

PACKET_SCHEMA_VERSION = "claim-packet/1.0"


class PacketError(ValueError):
    pass


def _statement(text: str, locators: List[str], kind: str = "evidence") -> Dict[str, Any]:
    return {"statement": text, "provenance": [{"kind": kind, "locator": loc} for loc in locators]}


def build_submission_packet(
    *,
    case: Dict[str, Any],
    policy_public: Dict[str, Any],
    evaluation: Dict[str, Any],
    expenses: List[Dict[str, Any]],
    facts: List[Dict[str, Any]],
    documents: List[Dict[str, Any]],
    claimant: Dict[str, Any],
    recipient: str,
    packet_type: str = "submission",
    original_claim_ref: Optional[str] = None,
    insurer_request: Optional[Dict[str, Any]] = None,
    only_document_ids: Optional[List[str]] = None,
    challenges: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    outcome_by_id = {o["expense_id"]: o for o in evaluation["expenses"]}
    claimed: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for e in expenses:
        outcome = outcome_by_id.get(e["id"])
        if not outcome:
            continue
        item = {
            "expense_id": e["id"],
            "receipt_id": e["receipt_id"],
            "merchant": e["merchant"],
            "purchased_at": e["purchased_at"],
            "description": e["description"],
            "category": e["category"],
            "amount_minor": e["amount_minor"],
            "currency": e["currency"],
            "provenance": [{"kind": "evidence", "locator": e["source_locator"]}],
        }
        if outcome["status"] == "supported":
            item["rule_id"] = outcome["rule_id"]
            claimed.append(item)
        else:
            item["status"] = outcome["status"]
            item["rule_id"] = outcome["rule_id"]
            item["reason"] = outcome["reason"]
            excluded.append(item)

    if packet_type == "submission" and not claimed:
        raise PacketError("no supported expenses to claim")

    facts_statements: List[Dict[str, Any]] = []
    for cond in evaluation["conditions"]:
        if cond["status"] == "supported":
            locs = cond.get("facts") or []
            kind = "evidence" if locs else "policy"
            facts_statements.append(_statement(cond["detail"], locs or [f"policy:{policy_public['policy_id']}@{policy_public['version']}"], kind))
    for f in facts:
        if f["confirmation_status"] == "customer_statement":
            facts_statements.append(_statement(f"Customer statement: {f['value']}", [f["source_locator"]], "customer_statement"))

    manifest_docs = documents if only_document_ids is None else [d for d in documents if d["id"] in only_document_ids]
    evidence_manifest = [
        {"document_id": d["id"], "doc_type": d["doc_type"], "content_hash": d["content_hash"], "captured_at": d["captured_at"], "purpose": d.get("purpose", "supporting evidence")}
        for d in manifest_docs
    ]
    total = sum(i["amount_minor"] for i in claimed)
    cap = evaluation["totals"]["cap_minor"]
    packet: Dict[str, Any] = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "packet_type": packet_type,
        "case_id": case["id"],
        "policy": {"policy_id": policy_public["policy_id"], "version": policy_public["version"], "insurer_name": policy_public["insurer_name"]},
        "claimant": {"customer_id": claimant["id"], "full_name": claimant["full_name"]},
        "loss": {
            "loss_type": case["loss_type"],
            "loss_at": case["loss_at"],
            "delay_start_at": (evaluation.get("evidence_refs", {}).get("delay_start") or {}).get("at"),
            "baggage_returned_at": (evaluation.get("evidence_refs", {}).get("delay_end") or {}).get("at"),
            "delay_hours": evaluation.get("delay_hours"),
        },
        "recipient": recipient,
        "statement_of_facts": facts_statements,
        "claimed_expenses": claimed,
        "excluded_items": excluded,
        "requested_total_minor": total,
        "policy_cap_minor": cap,
        "expected_maximum_minor": min(total, cap),
        "currency": evaluation["currency"],
        "evidence_manifest": evidence_manifest,
        "disclosure_manifest": {"recipient": recipient, "document_ids": [d["document_id"] for d in evidence_manifest], "fields_shared": ["claimant name", "policy id", "itinerary", "baggage report", "receipts"]},
        "disclaimer": "Prepared by the claimant's advocate from source-linked documents. Coverage estimate is not the insurer's determination.",
    }
    if original_claim_ref:
        packet["original_claim_ref"] = original_claim_ref
    if insurer_request:
        packet["responds_to_request"] = {"provider_request_id": insurer_request["provider_request_id"], "requirement": insurer_request["requirement"]}
    if challenges is not None:
        packet["challenges"] = challenges
    packet["content_hash"] = payload_hash({k: v for k, v in packet.items() if k != "content_hash"})
    return packet


def verify_provenance(packet: Dict[str, Any]) -> List[str]:
    """Returns a list of statements lacking provenance (release gate: must be empty)."""
    missing = []
    for s in packet.get("statement_of_facts", []):
        if not s.get("provenance"):
            missing.append(s["statement"])
    for e in packet.get("claimed_expenses", []):
        if not e.get("provenance"):
            missing.append(f"expense {e['expense_id']}")
    for c in packet.get("challenges", []) or []:
        if not c.get("fact_locators") or not c.get("clause_id"):
            missing.append(f"challenge {c.get('reason_code')}")
    return missing
