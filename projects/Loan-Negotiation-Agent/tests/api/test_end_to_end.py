from __future__ import annotations

import asyncio

from backend.app.adapters.mock_lender import canonical_body, sign_payload
from tests.conftest import BORROWER, OPERATOR, approve_screen, create_ready_case, offer_of


def _drain(container):
    return asyncio.get_event_loop().run_until_complete(container.worker.drain())


def _chat(client, case_id, text, **params):
    r = client.post(f"/v1/loan-cases/{case_id}/messages", json={"message": text, "params": params}, headers=BORROWER)
    assert r.status_code == 200, r.text
    return r.json()


def test_demo_1_keep_current_when_horizon_is_short(client, container):
    r = client.post("/v1/loan-cases", json={"customer_id": "cus_demo_5", "mortgage_id": "mortgage_demo_1", "holding_horizon_months": 24, "maximum_cash_to_close_minor": 600000, "offer_document_ids": ["offer_doc_a", "offer_doc_b"]}, headers=BORROWER)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "collecting" and body["version"] == 1
    assert "current_balance_as_of" in body["missing_fields"]
    cid = body["id"]
    turn = _chat(client, cid, "Is refinancing worth it if I stay two more years?")
    assert "?" in turn["reply"]  # asks for confirmations before computing
    assert [t["tool"] for t in turn["tool_calls"]] == ["record_borrower_facts"]  # horizon from the question is recorded
    assert turn["tool_calls"][0]["args"] == {"facts": {"holding_horizon_months": 24}}
    assert turn["case_status"] == "collecting"
    turn = _chat(client, cid, "Yes, the balance is correct and my payment includes escrow.")
    assert [t["tool"] for t in turn["tool_calls"]] == ["record_borrower_facts", "compare_loan_scenarios"]
    assert turn["case_status"] == "awaiting_decision"
    assert "keep" in turn["reply"].lower()
    view = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert view["comparison"]["recommendation"]["decision"] == "keep_current"
    for o in view["comparison"]["offers"]:
        assert "remaining_balance_at_horizon_minor" in o and "monthly_change_vs_keep_minor" in o
    turn = _chat(client, cid, "I'll keep my current loan.")
    assert turn["case_status"] == "keep_current"
    view = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert view["completion_evidence_ref"] == view["comparison_id"]
    tl = client.get(f"/v1/loan-cases/{cid}/timeline", headers=BORROWER).json()["events"]
    assert [e["next_state"] for e in tl if e["next_state"]] == ["collecting", "comparing", "awaiting_decision", "keep_current"]


def test_demo_2_negotiate_with_competing_offer_and_versioned_counteroffer(client, container):
    view = create_ready_case(client, horizon=48)
    cid = view["id"]
    # Current servicer refuses.
    turn = _chat(client, cid, "Ask my lender whether it can offer better terms.")
    screen = turn["review_screen"]
    assert screen["review"]["destination"]["lender_id"] == "lender_mock_servicer"
    assert "not an application" in screen["review"]["message_preview"]
    assert "Northstar" in screen["review"]["message_preview"]  # competing terms are cited factually
    assert approve_screen(client, screen).status_code == 200
    _drain(container)
    v = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert v["status"] == "awaiting_decision" and v["lender_requests"][0]["status"] == "refused"
    # Harbor matches the rate but adds fees -> honest recalculation still prefers Northstar.
    turn = _chat(client, cid, "Ask Harbor to match Northstar.")
    screen = turn["review_screen"]
    assert screen["payload"]["competing_offer_id"] == offer_of(view, "lender_mock_a")["id"]
    assert approve_screen(client, screen).status_code == 200
    _drain(container)
    v = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    harbor_v2 = offer_of(v, "lender_mock_b")
    assert harbor_v2["version"] == 2 and harbor_v2["status"] == "revised_quote"
    assert harbor_v2["note_rate_decimal"] == "0.065000" and harbor_v2["term_months"] == 300
    assert harbor_v2["normalized"]["incremental_costs_net_minor"] == 800_000
    assert [o["status"] for o in v["offers"] if o["lender_id"] == "lender_mock_b" and o["version"] == 1] == ["superseded"]
    assert v["comparison"]["recommendation"]["best_offer_id"] == offer_of(v, "lender_mock_a")["id"]
    assert v["status"] == "awaiting_decision"
    # Northstar gives a small credit; comparison refreshes.
    turn = _chat(client, cid, "Ask Northstar to beat Harbor.")
    assert approve_screen(client, turn["review_screen"]).status_code == 200
    _drain(container)
    v = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert offer_of(v, "lender_mock_a")["credits_minor"] == 25_000
    assert offer_of(v, "lender_mock_a")["version"] == 2
    # Summit asks for facts; it does not get them without a borrower decision.
    turn = _chat(client, cid, "Ask Summit for better terms.")
    assert approve_screen(client, turn["review_screen"]).status_code == 200
    _drain(container)
    v = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert any(q["kind"] == "lender_fact_request" for q in v["outstanding_questions"])
    assert v["lender_requests"][-1]["response"]["outcome"] == "facts_requested"


def test_demo_3_conditions_documents_and_final_review_with_northstar(client, container):
    view = create_ready_case(client, horizon=48)
    cid = view["id"]
    turn = _chat(client, cid, "Apply with the best offer.")
    screen = turn["review_screen"]
    assert screen["review"]["destination"]["lender_id"] == "lender_mock_a"
    assert screen["review"]["does_not_authorize"] == ["credit inquiry", "rate lock", "closing", "payoff of existing loan"]
    assert approve_screen(client, screen).status_code == 200
    _drain(container)
    v = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert v["status"] == "conditions_outstanding"
    assert [c["id"] for c in v["applications"][0]["conditions"]] == ["income_doc_missing"]
    # Signed provider callback arrives (at-least-once: deliver twice).
    r = client.post("/v1/ops/mock/deliver-callbacks", headers=OPERATOR)
    delivered = r.json()["delivered"]
    assert delivered and delivered[0]["response"]["status"] == "accepted"
    event = [c for c in container.network.callbacks if c["id"] == delivered[0]["event_id"]][0]
    body = {k: v_ for k, v_ in event.items() if k not in ("signature", "deliver_at", "delivered")}
    r = client.post("/v1/provider-events/lenders", json=body, headers={"X-Lender-Id": event["provider"], "X-Signature": event["signature"]})
    assert r.json()["status"] == "duplicate"
    r = client.post("/v1/provider-events/lenders", json=body, headers={"X-Lender-Id": event["provider"], "X-Signature": "sha256=bad"})
    assert r.status_code == 401
    _drain(container)
    # Release the income document: needs approval, then conditions clear.
    turn = _chat(client, cid, "Send them my pay stub.", document_ids=["doc_paystub_2026_09"])
    screen = turn["review_screen"]
    assert screen["review"]["documents_shared"][0]["id"] == "doc_paystub_2026_09"
    assert approve_screen(client, screen).status_code == 200
    _drain(container)
    v = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert v["status"] == "submitted"
    assert v["applications"][0]["conditions"][0]["status"] == "satisfied"
    # Lender issues final terms after processing delay (prepaid interest changed slightly: non-material).
    container.clock.advance(days=3)
    _drain(container)
    v = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert v["status"] == "final_review"
    assert v["term_reviews"][-1]["requires_reapproval"] is False
    assert v["applications"][0]["is_funded"] is False and v["applications"][0]["is_completed_refinance"] is False
    fr = client.get(f"/v1/loan-cases/{cid}/applications/{v['applications'][0]['id']}/final-review", headers=BORROWER).json()
    assert fr["review"]["requires_reapproval"] is False and fr["is_funded"] is False
    turn = _chat(client, cid, "Accept the final terms and close.")
    screen = turn["review_screen"]
    assert screen["action_type"] == "request_closing"
    assert approve_screen(client, screen).status_code == 200
    _drain(container)
    v = client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()
    assert v["status"] == "mock_closed"
    assert v["applications"][0]["closing_evidence"]["payoff_record_id"].startswith("payoff_mock")
    assert v["mortgage"]["still_active"] is True  # old loan not marked repaid by this record alone
    metrics = client.get("/v1/ops/metrics", headers=OPERATOR).json()
    assert metrics["cases_by_state"]["mock_closed"] == 1


def test_declining_a_review_returns_to_decision(client):
    view = create_ready_case(client)
    screen = client.post(f"/v1/loan-cases/{view['id']}/lender-request-drafts", json={"lender_id": "lender_mock_servicer"}, headers=BORROWER).json()
    r = client.post(f"/v1/actions/{screen['action_id']}/decline", headers=BORROWER)
    assert r.json()["case_status"] == "awaiting_decision"
    assert approve_screen(client, screen).status_code == 409


def test_stale_version_returns_409(client):
    view = create_ready_case(client)
    screen = client.post(f"/v1/loan-cases/{view['id']}/lender-request-drafts", json={"lender_id": "lender_mock_servicer"}, headers=BORROWER).json()
    screen["expected_case_version"] -= 1
    assert approve_screen(client, screen).status_code == 409


def test_adding_offers_after_comparison_invalidates_pending_review(client):
    view = create_ready_case(client, docs=("offer_doc_a", "offer_doc_b"))
    screen = client.post(f"/v1/loan-cases/{view['id']}/lender-request-drafts", json={"lender_id": "lender_mock_servicer"}, headers=BORROWER).json()
    r = client.post(f"/v1/loan-cases/{view['id']}/offers", json={"document_ids": ["offer_doc_c"]}, headers=BORROWER)
    assert r.status_code == 201
    assert approve_screen(client, screen).status_code == 409
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert [a["status"] for a in v["actions"]] == ["invalidated"]


def test_supplying_missing_rate_lock_makes_offer_rankable(client):
    view = create_ready_case(client)
    summit = offer_of(view, "lender_mock_c")
    assert summit["id"] in view["comparison"]["recommendation"]["unranked_offer_ids"]
    r = client.post(f"/v1/loan-cases/{view['id']}/offers/{summit['id']}/fields", json={"field": "rate_lock", "value": {"locked": False, "lock_period_days": 45}, "source": "lender email 2026-09-24"}, headers=BORROWER)
    assert r.status_code == 200 and r.json()["normalized"]["missing_fields"] == []
    client.post(f"/v1/loan-cases/{view['id']}/compare", json={}, headers=BORROWER)
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert summit["id"] in v["comparison"]["recommendation"]["ranked_offer_ids"]
    # Original evidence is preserved separately from the revised interpretation.
    assert v["offers"][2]["document_id"] == "offer_doc_c"


def test_a2a_lender_agent_card_and_negotiation(client, container):
    card = client.get("/a2a/lenders/lender_mock_b/.well-known/agent-card.json").json()
    assert card["protocolVersion"] == "0.3" and card["domainPayloadSchema"] == "loan-offer/v1"
    rpc = {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {"message": {"role": "user", "messageId": "req_a2a_1", "taskId": "req_a2a_1", "parts": [{"kind": "data", "data": {"type": "reprice", "target_offer": {"note_rate_decimal": "0.06625", "term_months": 360, "principal_minor": 30000000, "cost_items": [], "lender_credits_minor": 0}, "competing_offer": {"note_rate_decimal": "0.0650", "term_months": 300}}}]}}}
    task = client.post("/a2a/lenders/lender_mock_b", json=rpc).json()["result"]
    assert task["status"]["state"] == "completed"
    offer = task["artifacts"][0]["parts"][0]["data"]
    assert offer["schema"] == "loan-offer/v1" and offer["note_rate_decimal"] == "0.065000" and offer["version"] == 2
    again = client.post("/a2a/lenders/lender_mock_b", json={**rpc, "id": "2", "method": "tasks/get", "params": {"id": "req_a2a_1"}}).json()["result"]
    assert again["id"] == "req_a2a_1"
    # Summit asks for facts -> input-required.
    rpc["params"]["message"]["taskId"] = rpc["params"]["message"]["messageId"] = "req_a2a_2"
    task = client.post("/a2a/lenders/lender_mock_c", json=rpc).json()["result"]
    assert task["status"]["state"] == "input-required"
