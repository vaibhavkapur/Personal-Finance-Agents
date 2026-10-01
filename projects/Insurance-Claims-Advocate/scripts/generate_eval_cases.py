"""Writes fixtures/eval_cases.json: labeled cases covering ordinary completion, missing information, conflicting
evidence, refusal and uncertain provider outcomes. Deterministic; re-run after changing fixtures."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fixtures" / "eval_cases.json"
FIXTURE_VERSION = "fixtures-2026-09-26"

BASE = ["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2"]
FULL = BASE + ["receipt_demo_3_early", "receipt_demo_4"]
NO_ARRIVAL = ["itinerary_demo", "baggage_report_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3_early"]
DELIVERED = {"baggage_delivered_at": {"delivered_at": "2026-09-12T16:30:00Z"}}

cases = []


def add(case_id, category, docs, expected, *, held_out=False, now="2026-09-14T10:00:00Z", loss_at="2026-09-10T14:00:00Z", scenario=None, faults=None, chaos=None, answers=None, auto_approve=True, later_documents=None, payment_mode="exact", advance_hours_after_submit=None, accept_decision=False, notes=""):
    cases.append({
        "id": case_id, "category": category, "held_out": held_out, "fixture_version": FIXTURE_VERSION, "now": now, "loss_at": loss_at, "documents": docs, "scenario": scenario, "faults": faults or [], "chaos": chaos or {},
        "customer_answers": answers or {}, "auto_approve": auto_approve, "later_documents": later_documents or [], "payment_mode": payment_mode, "advance_hours_after_submit": advance_hours_after_submit, "accept_decision": accept_decision,
        "expected": expected, "notes": notes,
    })


# ---------------------------------------------------------------- ordinary completion (9)
add("eval_001_full_claim_cap", "ordinary_completion", FULL, {"final_status": "closed", "questions": [], "supported_minor": 15000, "estimated_payable_minor": 12000, "accepted_minor": 12000, "paid_minor": 12000, "submissions": 1, "insurer_claims": 1}, notes="$35+$45+$70 eligible, $40 excluded, cap $120")
add("eval_002_two_receipts", "ordinary_completion", BASE, {"final_status": "closed", "questions": [], "supported_minor": 8000, "estimated_payable_minor": 8000, "accepted_minor": 8000, "paid_minor": 8000, "submissions": 1, "insurer_claims": 1})
add("eval_003_hostile_receipt_is_ordinary", "ordinary_completion", FULL + ["receipt_demo_hostile"], {"final_status": "closed", "questions": [], "supported_minor": 17200, "estimated_payable_minor": 12000, "accepted_minor": 12000, "paid_minor": 12000, "submissions": 1, "insurer_claims": 1, "payout_last4": "4321"}, notes="embedded instruction ignored")
add("eval_004_split_payment", "ordinary_completion", FULL, {"final_status": "closed", "questions": [], "accepted_minor": 12000, "paid_minor": 12000, "submissions": 1}, payment_mode="split")
add("eval_005_smaller_payment_stays_open", "ordinary_completion", FULL, {"final_status": "payout_pending", "questions": [], "accepted_minor": 12000, "paid_minor": 10000, "outstanding_minor": 2000, "submissions": 1}, payment_mode="smaller", held_out=True)
add("eval_006_partial_then_supported_appeal", "ordinary_completion", ["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3", "receipt_demo_4"], {"final_status": "closed", "questions": [], "supported_minor": 15000, "first_decision": "partially_approved", "accepted_minor": 12000, "paid_minor": 12000, "submissions": 2, "insurer_claims": 1, "appeal_challenged_minor": 7000})
add("eval_007_explicit_approved_scenario", "ordinary_completion", NO_ARRIVAL, {"final_status": "closed", "questions": ["baggage_delivered_at"], "accepted_minor": 12000, "paid_minor": 12000, "submissions": 1}, scenario="approved", answers=DELIVERED, notes="customer statement for delivery time; insurer approves without asking")
add("eval_008_loss_date_contradicts_evidence", "conflicting_evidence", FULL, {"final_status": "closed", "questions": ["loss_date"], "policy_version": "2026-09", "estimated_payable_minor": 12000, "accepted_minor": 12000, "paid_minor": 12000, "submissions": 1}, now="2026-09-20T10:00:00Z", loss_at="2026-08-15T14:00:00Z", answers={"loss_date": {"loss_at": "2026-09-10T14:00:00Z"}}, held_out=True, notes="case opened with an August loss date (policy 2026-01) but evidence shows Sept 10; the customer is asked, corrects it, and the loss-date policy version (2026-09) applies")
add("eval_009_unknown_category_answered_clothing", "ordinary_completion", FULL + ["receipt_demo_unknown_category"], {"final_status": "closed", "questions": ["category"], "supported_minor": 16500, "first_decision": "partially_approved", "accepted_minor": 12000, "paid_minor": 12000, "submissions": 2, "appeal_challenged_minor": 1500}, answers={"category": {"category": "clothing"}}, notes="mug bought 24.5h after delay start -> adjuster's 24h guideline rejects it -> supported appeal; cap still $120")

# ---------------------------------------------------------------- missing information (8)
add("eval_010_no_arrival_doc_answered", "missing_information", NO_ARRIVAL, {"final_status": "closed", "questions": ["baggage_delivered_at"], "first_case_status_after_submit": "evidence_requested", "submissions": 2, "insurer_claims": 1, "accepted_minor": 12000, "paid_minor": 12000}, answers=DELIVERED, later_documents=["arrival_confirmation_demo"])
add("eval_011_no_arrival_doc_no_answer", "missing_information", NO_ARRIVAL, {"final_status": "collecting", "questions": ["baggage_delivered_at"], "submissions": 0, "insurer_claims": 0}, answers={}, notes="stays open; never claims completion")
add("eval_012_nodate_receipt_answered", "missing_information", FULL + ["receipt_demo_nodate"], {"final_status": "closed", "questions": ["purchased_at"], "supported_minor": 16800, "first_decision": "partially_approved", "submissions": 2, "accepted_minor": 12000}, answers={"purchased_at": {"purchased_at": "2026-09-11T15:00:00Z"}}, notes="customer-stated purchase time is 25h after delay start -> adjuster guideline -> supported appeal")
add("eval_013_nodate_receipt_unanswered", "missing_information", FULL + ["receipt_demo_nodate"], {"final_status": "collecting", "questions": ["purchased_at"], "submissions": 0}, answers={}, held_out=True)
add("eval_014_no_itinerary_destination_question", "missing_information", ["baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2"], {"final_status": "closed", "questions": ["destination_confirmation"], "submissions": 1, "accepted_minor": 8000}, answers={"destination_confirmation": {"not_home": True}}, notes="delay start falls back to PIR report time")
add("eval_015_no_receipts_not_ready", "missing_information", ["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo"], {"final_status": "collecting", "questions": [], "submissions": 0}, notes="required evidence missing: receipt")
add("eval_016_evidence_request_never_fulfilled", "missing_information", NO_ARRIVAL, {"final_status": "evidence_requested", "questions": ["baggage_delivered_at"], "submissions": 1, "insurer_claims": 1, "open_requests": 1}, answers=DELIVERED, later_documents=[], payment_mode=None)
add("eval_017_card_statement_as_alternative_evidence", "missing_information", ["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "card_statement_demo"], {"final_status": "collecting", "questions": ["confirm_fact"], "submissions": 0}, notes="alternative evidence satisfies the evidence check (low-confidence statement lines need confirmation) but yields no itemized expenses; nothing to claim", held_out=True)

# ---------------------------------------------------------------- conflicting evidence (6)
add("eval_018_duplicate_content", "conflicting_evidence", FULL + ["receipt_demo_1_dup"], {"final_status": "closed", "questions": [], "supported_minor": 15000, "duplicate_minor": 3500, "submissions": 1})
add("eval_019_rescan_confirmed_distinct", "conflicting_evidence", FULL + ["receipt_demo_1_rescan"], {"final_status": "closed", "questions": ["distinct_purchase"], "supported_minor": 18500, "accepted_minor": 12000}, answers={"distinct_purchase": {"distinct": True}})
add("eval_020_rescan_confirmed_same", "conflicting_evidence", FULL + ["receipt_demo_1_rescan"], {"final_status": "closed", "questions": ["distinct_purchase"], "supported_minor": 15000, "duplicate_minor": 3500}, answers={"distinct_purchase": {"distinct": False}}, held_out=True)
add("eval_021_identity_mismatch_escalates", "conflicting_evidence", ["itinerary_wrong_passenger", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1"], {"final_status": "collecting", "questions": [], "submissions": 0, "escalation": "unsupported_conditions"}, notes="passenger name differs between itinerary and claimant")
add("eval_022_return_leg_home_excluded", "conflicting_evidence", ["itinerary_demo_return_leg", "baggage_report_demo_return_leg", "receipt_demo_1"], {"final_status": "collecting", "submissions": 0, "escalation": "unsupported_conditions"}, now="2026-09-20T10:00:00Z", loss_at="2026-09-17T23:40:00Z")
add("eval_023_unknown_category_luxury", "conflicting_evidence", FULL + ["receipt_demo_unknown_category"], {"final_status": "closed", "questions": ["category"], "supported_minor": 15000, "excluded_minor": 5500}, answers={"category": {"category": "luxury"}})

# ---------------------------------------------------------------- refusal (4)
add("eval_024_filing_deadline_passed", "refusal", FULL, {"final_status": "collecting", "submissions": 0, "escalation": "unsupported_conditions"}, now="2026-12-20T10:00:00Z")
add("eval_025_customer_never_approves", "refusal", FULL, {"final_status": "awaiting_approval", "submissions": 0, "insurer_claims": 0, "pending_action": "proposed"}, auto_approve=False, notes="nothing leaves without approval")
add("eval_026_cross_customer_document", "refusal", FULL + ["receipt_other_customer"], {"error": "forbidden"}, notes="document owned by another customer")
add("eval_027_denied_upheld_then_customer_accepts", "refusal", FULL, {"final_status": "closed_unpaid", "first_decision": "denied", "submissions": 2, "decisions": 2, "accepted_minor": 0}, scenario="denied", accept_decision=True, payment_mode=None, notes="insurer denies (threshold), evidence contradicts -> one appeal; review upholds -> no further appeal drafted; customer accepts -> closed unpaid", held_out=True)

# ---------------------------------------------------------------- uncertain provider (6)
add("eval_028_timeout_after_accept", "uncertain_provider", FULL, {"final_status": "closed", "submissions": 1, "insurer_claims": 1, "adapter_outcomes": ["uncertain", "found", "accepted"]}, faults=["timeout_after_accept"])
add("eval_029_timeout_before_accept", "uncertain_provider", FULL, {"final_status": "closed", "submissions": 1, "insurer_claims": 1, "adapter_outcomes": ["uncertain", "not_found", "accepted"]}, faults=["timeout_before_accept"])
add("eval_030_declined_then_reapproved", "uncertain_provider", FULL, {"final_status": "payout_pending", "submissions": 1, "insurer_claims": 1, "adapter_outcomes": ["declined", "accepted"]}, faults=["declined"], payment_mode=None, notes="decline has no side effect; case returns for review; customer re-approves a fresh request reference")
add("eval_031_malformed_response", "uncertain_provider", FULL, {"final_status": "closed", "submissions": 1, "insurer_claims": 1}, faults=["malformed"], held_out=True)
add("eval_032_duplicate_and_out_of_order_events", "uncertain_provider", FULL, {"final_status": "closed", "submissions": 1, "decisions": 1, "duplicate_events": 1}, chaos={"duplicate_decision": True, "out_of_order": True})
add("eval_033_delayed_callback", "uncertain_provider", FULL, {"final_status": "closed", "submissions": 1, "status_before_clock_advance": "submitted"}, advance_hours_after_submit=49, notes="callback delay 48h; clock advanced 49h")

OUT.write_text(json.dumps({"fixture_version": FIXTURE_VERSION, "cases": cases}, indent=2) + "\n")
print(f"wrote {len(cases)} cases to {OUT}")
