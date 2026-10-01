"""Domain and failure cases 1, 2, 3, 8 from plan section 19."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.adapters.mock.insurers import MockInsurer
from app.clock import FixtureClock
from app.domain.application import IncompleteApplication, StaleQuote, application_payload_hash, build_application
from app.domain.coverage import compare_coverage
from app.domain.hashing import sha256_hash
from app.domain.issuance import verify_policy
from app.domain.money import Money, fmt_minor
from app.domain.needs import UNKNOWN, InsuranceNeeds, apply_needs_update
from app.domain.quotes import RentersQuote
from app.fixtures import load_insurers

NOW = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)


def _needs(**overrides) -> InsuranceNeeds:
    data = dict(
        customer_id="cus_demo_1", state_code="CA", effective_date=date(2026, 11, 1), property_limit_minor=3000000, liability_limit_minor=10000000,
        deductible_cap_minor=100000, replacement_cost_required=True, required_item_classes=["jewelry", "bicycles"],
        address={"line1": "1 Test St", "city": "Oakland", "state_code": "CA", "postal_code": "94607"},
    )
    data.update(overrides)
    return InsuranceNeeds(**data)


def _quotes(needs: InsuranceNeeds, answers=None):
    clock = FixtureClock(NOW)
    quotes = []
    for cfg in load_insurers():
        insurer = MockInsurer(cfg, clock)
        reqs = needs.to_requirements()
        view = insurer.request_quote(reqs, "req-" + cfg["label"], answers or {"cp_q_high_value": True})
        assert view["status"] == "quoted", (cfg["label"], view)
        quotes.append(RentersQuote.model_validate(view["quote"]))
    return quotes


def test_money_and_hashing():
    assert fmt_minor(18000) == "$180.00"
    assert (Money(100) + Money(250)).amount_minor == 350
    with pytest.raises(ValueError):
        Money(1, "USD") + Money(1, "EUR")
    assert sha256_hash({"b": 1, "a": 2}) == sha256_hash({"a": 2, "b": 1})
    assert sha256_hash({"a": 1}) != sha256_hash({"a": 2})


def test_fixture_premiums_match_plan_illustration():
    quotes = {q.insurer_id: q for q in _quotes(_needs())}
    assert quotes["ins_northwind_a"].annual_premium_minor == 18000
    assert quotes["ins_harborline_b"].annual_premium_minor == 21000
    assert quotes["ins_cedar_c"].annual_premium_minor == 16500
    assert quotes["ins_harborline_b"].coverage.deductible_minor < quotes["ins_northwind_a"].coverage.deductible_minor


def test_case2_cheaper_quote_missing_required_coverage_is_excluded():
    needs = _needs()
    result = compare_coverage(needs, _quotes(needs), NOW)
    suitable = [s["insurer_id"] for s in result["suitable"]]
    excluded = {e["insurer_id"]: e for e in result["excluded"]}
    assert suitable == ["ins_northwind_a", "ins_harborline_b"]
    assert "ins_cedar_c" in excluded
    failed = excluded["ins_cedar_c"]["failed_checks"]
    assert failed[0]["field"] == "item_class:jewelry"
    assert failed[0]["citation"]["clause_id"] == "CP-EX-4"
    assert "cheapest" in " ".join(result["trade_offs"])
    assert result["ranking"]["disclosure"].startswith("This ordering is a product rule")


def test_case3_deductible_difference_is_displayed_and_explained():
    needs = _needs()
    result = compare_coverage(needs, _quotes(needs), NOW)
    ded_row = next(r for r in result["differences"] if r["field"] == "deductible")
    assert ded_row["differs"] is True
    values = {v["insurer_id"]: v for v in ded_row["values"].values()}
    assert values["ins_northwind_a"]["display"] == "$1,000.00"
    assert values["ins_harborline_b"]["display"] == "$500.00"
    assert values["ins_harborline_b"]["citation"]["clause_id"] == "HL-DED"
    assert any("deductible" in t for t in result["trade_offs"])


def test_ranking_respects_lower_deductible_preference_after_suitability():
    needs = _needs(deductible_preference="lower_deductible")
    result = compare_coverage(needs, _quotes(needs), NOW)
    assert [s["insurer_id"] for s in result["suitable"]] == ["ins_harborline_b", "ins_northwind_a"]
    assert "ins_cedar_c" not in [s["insurer_id"] for s in result["suitable"]]


def test_case1_unknown_needs_never_become_a_favorable_default():
    needs = _needs(replacement_cost_required=None, deductible_cap_minor=None)
    assert needs.to_requirements()["replacement_cost_required"] == UNKNOWN
    result = compare_coverage(needs, _quotes(_needs()), NOW)
    assert result["suitable"] == []
    assert len(result["undetermined"]) == 2  # A and B are complete but cannot be shortlisted
    assert "ins_cedar_c" in [e["insurer_id"] for e in result["excluded"]]  # a hard failure still excludes
    assert set(result["unknown_needs_fields"]) >= {"deductible_cap_minor", "replacement_cost_required"}


def test_case1_unknown_underwriting_answer_blocks_application_instead_of_defaulting():
    needs = _needs()
    quote = next(q for q in _quotes(needs) if q.insurer_id == "ins_northwind_a")
    questions = next(c for c in load_insurers() if c["insurer_id"] == "ins_northwind_a")["questions"]
    answers = {
        "nw_q_dog": {"value": UNKNOWN},
        "nw_q_claims": {"value": False},
        "nw_q_smoke": {"value": True},
        "nw_q_home_business": {"value": False},
    }
    with pytest.raises(IncompleteApplication) as exc:
        build_application("case", "cus_demo_1", {"display_name": "Avery Demo"}, needs, quote, questions, answers, NOW)
    assert exc.value.missing_questions[0]["question_id"] == "nw_q_dog"


def test_application_hash_binds_terms_and_changes_with_premium():
    needs = _needs()
    quote = next(q for q in _quotes(needs) if q.insurer_id == "ins_northwind_a")
    questions = next(c for c in load_insurers() if c["insurer_id"] == "ins_northwind_a")["questions"]
    answers = {q["id"]: {"value": False if q["id"] != "nw_q_smoke" else True} for q in questions}
    payload = build_application("case", "cus_demo_1", {"display_name": "Avery Demo"}, needs, quote, questions, answers, NOW)
    h1 = application_payload_hash(payload)
    revised = quote.model_copy(update={"annual_premium_minor": quote.annual_premium_minor + 3600, "quote_version": 2})
    payload2 = build_application("case", "cus_demo_1", {"display_name": "Avery Demo"}, needs, revised, questions, answers, NOW, revision=2)
    assert application_payload_hash(payload2) != h1
    with pytest.raises(StaleQuote):
        build_application("case", "cus_demo_1", {}, needs, quote, questions, answers, NOW + timedelta(days=60))


def test_case8_issued_effective_date_mismatch_fails_verification():
    needs = _needs()
    quote = next(q for q in _quotes(needs) if q.insurer_id == "ins_northwind_a")
    questions = next(c for c in load_insurers() if c["insurer_id"] == "ins_northwind_a")["questions"]
    answers = {q["id"]: {"value": False if q["id"] != "nw_q_smoke" else True} for q in questions}
    payload = build_application("case", "cus_demo_1", {"display_name": "Avery Demo"}, needs, quote, questions, answers, NOW)
    declarations = {
        "insurer_policy_ref": "A-POL-1", "insurer_id": "ins_northwind_a", "insured_name": "Avery Demo", "address": needs.address.model_dump(),
        "property_limit_minor": 3000000, "liability_limit_minor": 10000000, "deductible_minor": 100000, "annual_premium_minor": 18000, "currency": "USD",
        "replacement_cost": True, "policy_form_version": "NW-HO4-2026.1", "effective_at": "2026-11-01", "expires_at": "2027-11-01",
        "exclusions": ["NW-EX-1", "NW-EX-2", "NW-EX-3"], "endorsements": [],
    }
    ok = verify_policy(payload, declarations, today=NOW.date())
    assert ok["verified"] and ok["coverage_starts_in_future"] and "coverage starts 2026-11-01" in ok["display_label"]
    shifted = dict(declarations, effective_at="2026-11-08")
    bad = verify_policy(payload, shifted, today=NOW.date())
    assert not bad["verified"]
    assert bad["mismatches"][0]["field"] == "effective_date"


def test_needs_update_versions_only_on_material_change():
    needs = _needs()
    same = apply_needs_update(needs, {"deductible_preference": "lower_deductible"})
    assert same.version == needs.version
    bumped = apply_needs_update(needs, {"property_limit_minor": 4000000})
    assert bumped.version == needs.version + 1
    cleared = apply_needs_update(needs, {"deductible_cap_minor": UNKNOWN})
    assert cleared.deductible_cap_minor is None and cleared.version == needs.version + 1
