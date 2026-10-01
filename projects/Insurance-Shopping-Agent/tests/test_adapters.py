"""Mock insurer engine, direct adapter and A2A boundary tests (includes plan case 6)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.adapters.a2a.client import A2AInsurerAdapter
from app.adapters.base import ProviderMalformedResponse, ProviderTimeout
from app.adapters.mock.a2a_server import A2A_PROTOCOL_VERSION, create_insurer_app
from app.adapters.mock.direct import DirectMockAdapter
from app.adapters.mock.insurers import FAULT_DECLINE_APPLICATION, FAULT_MALFORMED, FAULT_TIMEOUT, FAULT_TIMEOUT_BEFORE, MockInsurer
from app.clock import FixtureClock
from app.domain.needs import UNKNOWN
from app.fixtures import insurer_by_id

NOW = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
REQUIREMENTS = {
    "schema": "renters-quote-request/v1", "needs_version": 1, "product": "renters", "state_code": "CA", "effective_date": "2026-11-01",
    "property_limit_minor": 3000000, "liability_limit_minor": 10000000, "deductible_cap_minor": 100000, "replacement_cost_required": True,
    "required_item_classes": ["jewelry"], "address": {"line1": "1 Test St", "city": "Oakland", "state_code": "CA", "postal_code": "94607"}, "currency": "USD",
}


def _application(quote, answers):
    return {
        "schema": "renters-application/v1", "quote_ref": quote["quote_ref"], "quote_version": quote["quote_version"], "insurer_id": quote["insurer_id"],
        "applicant": {"display_name": "Avery Demo", "address": REQUIREMENTS["address"]},
        "answers": [{"question_id": k, "value": v} for k, v in answers.items()],
    }


def test_request_quote_is_idempotent_by_request_ref():
    insurer = MockInsurer(insurer_by_id("ins_northwind_a"), FixtureClock(NOW))
    first = insurer.request_quote(REQUIREMENTS, "req-1")
    second = insurer.request_quote(REQUIREMENTS, "req-1")
    assert first["task_ref"] == second["task_ref"]
    assert first["quote"]["quote_ref"] == second["quote"]["quote_ref"]


def test_cedar_asks_follow_up_and_unknown_answer_does_not_resolve_it():
    insurer = MockInsurer(insurer_by_id("ins_cedar_c"), FixtureClock(NOW))
    view = insurer.request_quote(REQUIREMENTS, "req-c")
    assert view["status"] == "input_required"
    assert view["questions"][0]["id"] == "cp_q_high_value"
    still = insurer.answer_question(view["task_ref"], {"question_id": "cp_q_high_value", "value": UNKNOWN})
    assert still["status"] == "input_required"
    quoted = insurer.answer_question(view["task_ref"], {"question_id": "cp_q_high_value", "value": True})
    assert quoted["status"] == "quoted"
    assert quoted["quote"]["coverage"]["item_classes"]["jewelry"]["status"] == "excluded"


def test_underwriting_revision_decline_and_issuance():
    clock = FixtureClock(NOW)
    insurer = MockInsurer(insurer_by_id("ins_northwind_a"), clock)
    quote = insurer.request_quote(REQUIREMENTS, "req-a")["quote"]
    answers = {"nw_q_dog": False, "nw_q_claims": True, "nw_q_smoke": True, "nw_q_home_business": False}
    sub = insurer.submit_application(_application(quote, answers), "sub-1")
    assert sub["status"] == "underwriting"
    assert insurer.get_policy_status("sub-1")["status"] == "underwriting"
    clock.advance(hours=2)
    revised = insurer.get_policy_status("sub-1")
    assert revised["status"] == "revised_offer"
    assert revised["revised_quote"]["annual_premium_minor"] == 18000 + 3600
    assert revised["revised_quote"]["quote_version"] == 2
    accepted = insurer.accept_revised_offer(sub["submission_ref"], quote["quote_ref"], 2, "acc-1")
    assert accepted["status"] == "underwriting"
    clock.advance(minutes=10)
    issued = insurer.get_policy_status("sub-1")
    assert issued["status"] == "issued"
    assert issued["policy"]["annual_premium_minor"] == 21600
    assert issued["policy"]["effective_at"] == "2026-11-01"

    # decline rule: home business
    quote2 = insurer.request_quote(REQUIREMENTS, "req-a2")["quote"]
    insurer.submit_application(_application(quote2, {**answers, "nw_q_claims": False, "nw_q_home_business": True}), "sub-2")
    clock.advance(hours=2)
    assert insurer.get_policy_status("sub-2")["status"] == "declined"


def test_incomplete_application_is_rejected_by_provider():
    insurer = MockInsurer(insurer_by_id("ins_northwind_a"), FixtureClock(NOW))
    quote = insurer.request_quote(REQUIREMENTS, "req-a")["quote"]
    with pytest.raises(ValueError):
        insurer.submit_application(_application(quote, {"nw_q_dog": False}), "sub-x")


def test_faults_timeout_after_accept_and_status_lookup_by_request_ref():
    clock = FixtureClock(NOW)
    insurer = MockInsurer(insurer_by_id("ins_northwind_a"), clock)
    quote = insurer.request_quote(REQUIREMENTS, "req-a")["quote"]
    answers = {"nw_q_dog": False, "nw_q_claims": False, "nw_q_smoke": True, "nw_q_home_business": False}
    insurer.inject_fault(FAULT_TIMEOUT, "submit_application")
    with pytest.raises(ProviderTimeout):
        insurer.submit_application(_application(quote, answers), "sub-lost")
    # The write happened even though the response was lost.
    assert insurer.get_policy_status("sub-lost")["status"] == "underwriting"
    insurer.inject_fault(FAULT_TIMEOUT_BEFORE, "submit_application")
    with pytest.raises(ProviderTimeout):
        insurer.submit_application(_application(quote, answers), "sub-never")
    assert insurer.get_policy_status("sub-never")["status"] == "not_found"


@pytest.mark.asyncio
async def test_direct_adapter_flags_malformed_response():
    insurer = MockInsurer(insurer_by_id("ins_harborline_b"), FixtureClock(NOW))
    adapter = DirectMockAdapter(insurer)
    insurer.inject_fault(FAULT_MALFORMED, "request_quote")
    with pytest.raises(ProviderMalformedResponse):
        await adapter.request_quote(REQUIREMENTS, "req-b")
    ok = await adapter.request_quote(REQUIREMENTS, "req-b2")
    assert ok["status"] == "quoted"
    caps = adapter.capabilities()
    assert caps.environment == "mock" and caps.status_lookup_by_request_ref


def _a2a_pair(insurer_id: str, clock: FixtureClock):
    insurer = MockInsurer(insurer_by_id(insurer_id), clock, protocol="a2a/%s" % A2A_PROTOCOL_VERSION)
    app = create_insurer_app(insurer, base_url="http://insurer.test")
    transport = httpx.ASGITransport(app=app)
    adapter = A2AInsurerAdapter(insurer_id, "http://insurer.test", transport=transport)
    return insurer, adapter


@pytest.mark.asyncio
async def test_a2a_agent_card_and_multi_turn_quote_exchange():
    clock = FixtureClock(NOW)
    insurer, adapter = _a2a_pair("ins_cedar_c", clock)
    card = await adapter.agent_card()
    assert card["protocolVersion"] == A2A_PROTOCOL_VERSION
    assert "renters-quote/v1" in card["metadata"]["domain_schemas"]
    first = await adapter.request_quote(REQUIREMENTS, "a2a-req-1")
    assert first["status"] == "input_required"
    assert first["questions"][0]["id"] == "cp_q_high_value"
    resumed = await adapter.answer_question(first["task_ref"], {"question_id": "cp_q_high_value", "value": True})
    assert resumed["status"] == "quoted"
    assert resumed["quote"]["schema"] == "renters-quote/v1"
    fetched = await adapter.get_task(first["task_ref"])
    assert fetched["task_ref"] == first["task_ref"] and fetched["status"] == "quoted"
    assert resumed["source"]["protocol"].startswith("a2a/")


@pytest.mark.asyncio
async def test_case6_a2a_task_completed_with_quote_is_not_an_issued_policy():
    clock = FixtureClock(NOW)
    insurer, adapter = _a2a_pair("ins_northwind_a", clock)
    result = await adapter.request_quote(REQUIREMENTS, "a2a-req-2")
    assert result["status"] == "quoted"  # A2A task state: completed
    status = await adapter.get_policy_status("a2a-req-2")
    assert status["status"] == "not_found"  # no submission -> no policy
    answers = {"nw_q_dog": False, "nw_q_claims": False, "nw_q_smoke": True, "nw_q_home_business": False}
    sub = await adapter.submit_application(_application(result["quote"], answers), "a2a-sub-1")
    assert sub["status"] == "underwriting" and sub["policy"] is None
    clock.advance(hours=2)
    issued = await adapter.get_policy_status("a2a-sub-1")
    assert issued["status"] == "issued" and issued["policy"]["insurer_policy_ref"].startswith("A-POL-")


@pytest.mark.asyncio
async def test_a2a_gateway_timeout_maps_to_unknown_outcome():
    clock = FixtureClock(NOW)
    insurer, adapter = _a2a_pair("ins_northwind_a", clock)
    insurer.inject_fault(FAULT_TIMEOUT, "request_quote")
    with pytest.raises(ProviderTimeout):
        await adapter.request_quote(REQUIREMENTS, "a2a-req-3")
    # Same request reference resolves to the task the insurer already recorded.
    again = await adapter.request_quote(REQUIREMENTS, "a2a-req-3")
    assert again["status"] == "quoted"


@pytest.mark.asyncio
async def test_a2a_malformed_task_is_rejected():
    clock = FixtureClock(NOW)
    insurer, adapter = _a2a_pair("ins_harborline_b", clock)
    insurer.inject_fault(FAULT_MALFORMED, "request_quote")
    with pytest.raises(ProviderMalformedResponse):
        await adapter.request_quote(REQUIREMENTS, "a2a-req-4")
