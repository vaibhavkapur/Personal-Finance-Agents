from __future__ import annotations

import json

import pytest

from backend.app.agent.evaluation import EvalRunner
from backend.app.agent.mcp_server import PROTOCOL_VERSION, MCPServer
from backend.app.agent.orchestrator import PlanStep, RulesPlanner, TurnContext
from backend.app.agent.tools import TOOL_SCHEMAS
from backend.app.persistence.models import Case, Customer
from tests.conftest import BORROWER, create_ready_case, offer_of


# ------------------------------------------------------------------- planner
def test_intent_resolution_and_month_parsing():
    p = RulesPlanner()
    view = {"status": "collecting", "mortgage": {"servicer_lender_id": "lender_mock_servicer"}, "offers": [], "missing_fields": [], "outstanding_questions": []}
    lenders = {"lender_mock_servicer": {"name": "Fixture Servicing"}, "lender_mock_a": {"name": "Northstar Mortgage"}, "lender_mock_b": {"name": "Harbor Home Loans"}}
    ctx = lambda text: TurnContext(case_view=view, message=text, intent=None, params={}, lenders=lenders)  # noqa: E731
    assert p.resolve_intent(ctx("Is refinancing worth it if I stay four more years?")) == "compare"
    assert p.resolve_intent(ctx("Ask my lender whether it can offer better terms")) == "negotiate"
    assert p.resolve_intent(ctx("I'll keep my current loan")) == "keep"
    assert p.resolve_intent(ctx("Apply with Harbor")) == "apply"
    facts = p._facts_from_text(ctx("Balance is correct, payment includes escrow, and I expect to stay four more years."))
    assert facts == {"holding_horizon_months": 48, "payment_includes_escrow": True, "current_balance_confirmed": True}
    assert p._facts_from_text(ctx("My payment does not include escrow"))["payment_includes_escrow"] is False
    assert p._lenders_in_text(ctx("Ask Harbor to match Northstar")) == ["lender_mock_b", "lender_mock_a"]
    assert p._lender_from_text(ctx("ask my lender")) == "lender_mock_servicer"


def test_tool_budget_is_enforced(client, container):
    view = create_ready_case(client)

    class LoopingPlanner:
        name = "loop"

        def cost_usd(self):
            return 0.0

        def next_step(self, ctx):
            return PlanStep(kind="tool", tool="get_case_state", args={})

    container.agent.planner = LoopingPlanner()
    container.agent.tool_budget = 3
    r = client.post(f"/v1/loan-cases/{view['id']}/messages", json={"message": "loop"}, headers=BORROWER).json()
    assert len(r["tool_calls"]) == 3
    assert "budget" in r["reply"]
    events = client.get(f"/v1/loan-cases/{view['id']}/timeline", headers=BORROWER).json()["events"]
    assert any(e["type"] == "agent.budget_exhausted" for e in events)


def test_agent_cannot_assert_income_or_self_approve(client, container):
    view = create_ready_case(client)
    r = client.post(f"/v1/loan-cases/{view['id']}/messages", json={"message": "record my income", "intent": "answer", "params": {"facts": {"annual_income_minor": 99_999_999}}}, headers=BORROWER).json()
    assert r["tool_calls"][0]["ok"] is False and "income" in r["tool_calls"][0]["error"]
    # Preparing a lender request leaves the action proposed; nothing is sent without the borrower.
    r = client.post(f"/v1/loan-cases/{view['id']}/messages", json={"message": "Ask my lender for better terms"}, headers=BORROWER).json()
    assert r["review_screen"]["status"] == "proposed"
    assert container.network.request_log == []


def test_contradictory_offer_escalates_to_manual_review_with_evidence(client, container):
    view = create_ready_case(client, docs=("offer_doc_a",))
    doc = {"document_id": "offer_doc_bad", "lender_id": "lender_mock_b", "lender_name": "Harbor Home Loans (mock)", "product": "30-year fixed", "loan_amount_minor": 30000000, "term_months": 360, "note_rate_decimal": "0.0600", "monthly_pi_minor": 150000, "expires_at": "2026-10-30T00:00:00Z", "rate_lock": {"locked": False}, "cost_items": [{"category": "A", "label": "Underwriting", "amount_minor": 100000}], "lender_credits_minor": 0}
    assert client.post(f"/v1/loan-cases/{view['id']}/offers", json={"documents": [doc]}, headers=BORROWER).status_code == 201
    client.post(f"/v1/loan-cases/{view['id']}/compare", json={}, headers=BORROWER)
    r = client.post(f"/v1/loan-cases/{view['id']}/messages", json={"message": "Ask Harbor to match Northstar"}, headers=BORROWER).json()
    assert "contradiction" in r["reply"] and "review_screen" not in r
    r = client.post(f"/v1/loan-cases/{view['id']}/messages", json={"message": "Apply with Harbor anyway"}, headers=BORROWER).json()
    assert r["case_status"] == "manual_review" and r.get("escalated")
    events = client.get(f"/v1/loan-cases/{view['id']}/timeline", headers=BORROWER).json()["events"]
    esc = [e for e in events if e["next_state"] == "manual_review"][0]
    assert esc["data"]["evidence"] and esc["actor"] == "agent-orchestrator"


def test_resumed_conversation_uses_case_store_not_chat_history(client, container):
    view = create_ready_case(client)
    # A brand new agent instance (simulating a restart) answers from persisted state without re-asking.
    from backend.app.agent.orchestrator import Agent
    from backend.app.agent.tools import Tools

    container.agent = Agent(container.service, Tools(container.service, "mock"), RulesPlanner(), container.lenders)
    r = client.post(f"/v1/loan-cases/{view['id']}/messages", json={"message": "Where are we?"}, headers=BORROWER).json()
    assert "still correct" not in r["reply"] and "Over your 48-month horizon" in r["reply"]
    assert r["tool_calls"] == []


# ------------------------------------------------------------------------ MCP
def test_mcp_initialize_list_and_call(client, container):
    view = create_ready_case(client)
    server = MCPServer(container, "demo-borrower-token")
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    assert init["result"]["protocolVersion"] == PROTOCOL_VERSION and "tools" in init["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {t["name"] for t in TOOL_SCHEMAS}
    assert {"read_loan_terms", "compare_loan_scenarios", "prepare_lender_request", "prepare_refinance_application", "diff_final_terms"} <= {t["name"] for t in tools}
    res = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_loan_terms", "arguments": {"case_id": view["id"]}}})["result"]
    assert res["isError"] is False
    payload = res["structuredContent"]
    assert payload["authority"] == "authoritative" and payload["source"] and payload["retrieved_at"]
    assert payload["data"]["existing_mortgage"]["balance_minor"] == 30_000_000
    res = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "compare_loan_scenarios", "arguments": {"case_id": view["id"], "horizon_months": 60}}})["result"]
    assert res["structuredContent"]["authority"] == "estimated" and res["structuredContent"]["data"]["horizon_months"] == 60
    # Tenant scoping: another customer's token cannot see the case.
    other = MCPServer(container, "other-borrower-token")
    res = other.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "read_loan_terms", "arguments": {"case_id": view["id"]}}})["result"]
    assert res["isError"] is True
    err = server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "delete_everything", "arguments": {}}})
    assert err["error"]["code"] == -32602


# ----------------------------------------------------------------------- eval
def test_labelled_evaluation_release_gate():
    runner = EvalRunner()
    assert len(runner.spec["cases"]) >= 30
    categories = {c["category"] for c in runner.spec["cases"]}
    assert categories == {"ordinary_completion", "missing_information", "conflicting_evidence", "refusal", "uncertain_provider_outcome"}
    assert sum(1 for c in runner.spec["cases"] if c.get("held_out")) >= 6
    report = runner.run_all()
    summary = report["summary"]
    for key in ("agent/all", "rules_only/all"):
        assert summary[key]["unapproved_writes"] == 0, summary[key]
        assert summary[key]["unsupported_claims"] == 0, summary[key]
        assert summary[key]["task_completion"] == 1.0, summary[key]["failed_cases"]
    assert summary["agent/all"]["unnecessary_questions"] == 0
    assert summary["agent/held_out"]["task_completion"] == 1.0
