"""Release gate: final recovered and outstanding amounts match every labeled fixture to the cent,
no unapproved external writes, no duplicated side effects, and no unsupported recovery claims."""
from __future__ import annotations

from backend.app.evaluation import evaluate, load_specs


def test_labeled_cases_cover_required_families():
    specs = load_specs()
    assert len(specs) >= 30
    families = {s["family"] for s in specs}
    for required in ("full_refund_exact_ref", "missing_refund", "ambiguous_credits", "missing_promise", "merchant_claims_refund_no_posting", "issuer_provisional_not_recovery",
                     "issuer_provisional_reversed", "merchant_and_issuer_overlap", "unverified_merchant_contact_refusal", "timeout_after_accept_resolved_by_lookup", "store_credit_not_card_credit"):
        assert required in families, required
    assert {s["split"] for s in specs} == {"dev", "heldout"}


def test_agent_and_rules_only_pass_every_labeled_case():
    report = evaluate()
    for mode, summary in report["summary"]["modes"].items():
        assert summary["failed"] == [], (mode, [r["diffs"] for r in report["results"][mode] if not r["passed"]])
        for split, b in summary["by_split"].items():
            assert b["unsupported_claims_emitted"] == 0, (mode, split)
            assert b["unnecessary_questions"] == 0, (mode, split)
