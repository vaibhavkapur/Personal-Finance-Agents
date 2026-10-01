"""Phase 1 exit criterion: eligible expense calculations match labeled outcomes; extraction is source-linked."""
from __future__ import annotations

import json

from conftest import COMPLETE_DOCS, CUSTOMER, FROZEN_NOW, LOSS_AT, Flow, make_ctx
from app.clock import parse_iso
from app.config import FIXTURES_DIR
from app.domain.extraction import classify_item, extract_document, parse_datetime
from app.domain.policy import PolicyFixture
from app.persistence.seed import load_policy_fixtures


def load_doc(doc_id):
    docs = json.loads((FIXTURES_DIR / "documents.json").read_text())["documents"]
    return next(d for d in docs if d["id"] == doc_id)


def test_policy_clause_offsets_reproduce_clause_text():
    fixture = load_policy_fixtures()["travel_policy_demo_1"]
    for version in fixture.versions:
        for clause in version.clauses.values():
            assert version.source_text[clause.start:clause.end].strip() == clause.text
            assert clause.text.startswith(clause.id[1:])  # "7.1 ..." etc.


def test_policy_version_selected_by_loss_date_not_latest():
    fixture = load_policy_fixtures()["travel_policy_demo_1"]
    august = fixture.version_for_loss(parse_iso("2026-08-15T12:00:00Z"))
    september = fixture.version_for_loss(parse_iso(LOSS_AT))
    assert august.version == "2026-01" and august.rule("aggregate_cap_minor").value == 10000 and august.rule("delay_threshold_hours").value == 24
    assert september.version == "2026-09" and september.rule("aggregate_cap_minor").value == 12000 and september.rule("delay_threshold_hours").value == 12
    assert fixture.latest().version == "2026-09"


def test_receipt_extraction_has_line_level_locators_and_categories():
    d = load_doc("receipt_demo_1")
    result = extract_document(d["id"], d["doc_type"], d["pages"])
    r = result.receipt
    assert r.merchant == "DENVER OUTFITTERS"
    assert r.purchased_at == "2026-09-10T18:20:00Z"  # 12:20 MDT -> UTC
    assert r.total_minor == 3500 and r.items_total_minor == 3500
    assert [i.category for i in r.items] == ["clothing", "clothing"]
    assert r.items[0].source_locator == "doc:receipt_demo_1#p1:l4"
    assert result.facts[0].source_locator == "doc:receipt_demo_1#p1:l1"


def test_missing_date_is_flagged_not_invented():
    d = load_doc("receipt_demo_nodate")
    result = extract_document(d["id"], d["doc_type"], d["pages"])
    assert result.receipt.purchased_at is None
    assert "purchase_date_missing" in result.receipt.uncertainty_flags


def test_hostile_document_text_is_flagged_and_not_interpreted():
    d = load_doc("receipt_demo_hostile")
    result = extract_document(d["id"], d["doc_type"], d["pages"])
    assert any(w.startswith("instruction_like_text_ignored") for w in result.warnings)
    assert result.receipt.total_minor == 2200
    assert classify_item("Travel Soap + Comb") == "toiletries"


def test_timezone_parsing():
    assert parse_datetime("2026-09-12 10:30 MDT") == ("2026-09-12T16:30:00Z", [])
    ts, flags = parse_datetime("2026-09-12 10:30")
    assert ts == "2026-09-12T10:30:00Z" and "timezone_assumed_utc" in flags


def test_fixture_example_cap_and_exclusion():
    """$35 + $45 + $70 eligible, $40 excluded, cap $120 -> supported 150, payable 120, excluded disclosed."""
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    v = flow.view()
    totals = v["totals"]
    assert totals["supported_minor"] == 15000
    assert totals["excluded_minor"] == 4000
    assert totals["cap_minor"] == 12000
    assert totals["estimated_payable_minor"] == 12000
    assert v["evaluation"]["totals"]["cap_applied"] is True
    excluded = [e for e in v["expenses"] if e["eligibility_status"] == "excluded"]
    assert len(excluded) == 1 and excluded[0]["receipt_id"] == "receipt_demo_4" and excluded[0]["rule_id"] == "R_EXCLUDED_CATEGORY"
    assert v["evaluation"]["delay_hours"] == 50.5
    assert v["case"]["status"] == "evaluating"
    packet = ctx.cases.preview_packet(CUSTOMER, flow.case_id)
    assert packet["requested_total_minor"] == 15000 and packet["expected_maximum_minor"] == 12000
    assert [x["receipt_id"] for x in packet["excluded_items"]] == ["receipt_demo_4"]
    assert all(s["provenance"] for s in packet["statement_of_facts"])


def test_duplicate_receipt_content_does_not_increase_claim():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS + ["receipt_demo_1_dup"])
    v = flow.view()
    dup = [e for e in v["expenses"] if e["eligibility_status"] == "duplicate"]
    assert len(dup) == 1 and dup[0]["receipt_id"] == "receipt_demo_1_dup"
    assert v["totals"]["supported_minor"] == 15000
    assert v["totals"]["duplicate_minor"] == 3500


def test_possible_duplicate_asks_instead_of_deleting():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS + ["receipt_demo_1_rescan"])
    v = flow.view()
    q = [q for q in v["open_questions"] if q["field"] == "distinct_purchase"]
    assert len(q) == 1
    assert v["totals"]["supported_minor"] == 15000  # not counted until confirmed
    flow.answer("distinct_purchase", {"distinct": True})
    assert flow.view()["totals"]["supported_minor"] == 18500
    ctx2 = make_ctx()
    flow2 = Flow(ctx2)
    flow2.open(COMPLETE_DOCS + ["receipt_demo_1_rescan"])
    flow2.answer("distinct_purchase", {"distinct": False})
    v2 = flow2.view()
    assert v2["totals"]["supported_minor"] == 15000
    assert any(e["receipt_id"] == "receipt_demo_1_rescan" and e["eligibility_status"] == "duplicate" for e in v2["expenses"])


def test_unsupported_purchase_date_triggers_question_not_invented_date():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS + ["receipt_demo_nodate"])
    v = flow.view()
    q = [q for q in v["open_questions"] if q["field"] == "purchased_at"]
    assert len(q) == 1
    exp = next(e for e in v["expenses"] if e["receipt_id"] == "receipt_demo_nodate")
    assert exp["purchased_at"] is None and exp["eligibility_status"] == "uncertain"
    assert v["case"]["status"] == "collecting"
    flow.answer("purchased_at", {"purchased_at": "2026-09-11T15:00:00Z"})
    v = flow.view()
    exp = next(e for e in v["expenses"] if e["receipt_id"] == "receipt_demo_nodate")
    assert exp["eligibility_status"] == "supported"
    assert any(f["fact_type"] == "purchase_time_statement" and f["confirmation_status"] == "customer_statement" for f in v["facts"])
    assert v["case"]["status"] == "evaluating"


def test_unknown_category_asks_customer():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS + ["receipt_demo_unknown_category"])
    assert any(q["field"] == "category" for q in flow.view()["open_questions"])
    flow.answer("category", {"category": "luxury"})
    exp = next(e for e in flow.view()["expenses"] if e["receipt_id"] == "receipt_demo_unknown_category")
    assert exp["eligibility_status"] == "excluded"


def test_contract_version_applies_to_loss_date_in_case():
    ctx = make_ctx(now="2026-08-20T10:00:00Z")
    flow = Flow(ctx)
    res = flow.open(["receipt_demo_1"], loss_at="2026-08-15T14:00:00Z")
    assert res["policy_version"] == "2026-01"
    assert flow.view()["totals"]["cap_minor"] == 10000


def test_return_leg_to_home_is_unsupported_not_guessed():
    ctx = make_ctx(now="2026-09-20T10:00:00Z")
    flow = Flow(ctx)
    flow.open(["itinerary_demo_return_leg", "baggage_report_demo_return_leg", "receipt_demo_1"], loss_at="2026-09-17T23:40:00Z")
    v = flow.view()
    cond = next(c for c in v["checklist"] if c["id"] == "destination_not_home")
    assert cond["status"] == "unsupported"
    assert v["totals"]["estimated_payable_minor"] == 0
    turn = flow.agent("please file my claim")
    assert "not preparing a submission" in turn["message"]
    assert v["pending_action"] is None


def test_filing_deadline_from_dated_policy():
    ctx = make_ctx(now="2026-12-20T10:00:00Z")  # > 90 days after 2026-09-10
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    cond = next(c for c in flow.view()["checklist"] if c["id"] == "filing_within_deadline")
    assert cond["status"] == "unsupported"
    assert cond["id"] == "filing_within_deadline"
    deadline = next(c for c in flow.view()["evaluation"]["conditions"] if c["id"] == "filing_within_deadline")
    assert deadline["deadline_at"] == "2026-12-09T14:00:00Z" and "C7.6" in deadline["deadline_source"]


def test_loss_date_contradicted_by_evidence_asks_and_reselects_policy_version():
    ctx = make_ctx(now="2026-09-20T10:00:00Z")
    flow = Flow(ctx)
    res = flow.open(COMPLETE_DOCS, loss_at="2026-08-15T14:00:00Z")
    assert res["policy_version"] == "2026-01"
    assert any(q["field"] == "loss_date" for q in res["open_questions"])
    assert flow.view()["case"]["status"] == "collecting"
    flow.answer("loss_date", {"loss_at": "2026-09-10T14:00:00Z"})
    v = flow.view()
    assert v["case"]["policy_version"] == "2026-09" and v["totals"]["cap_minor"] == 12000
    assert v["case"]["status"] == "evaluating"
