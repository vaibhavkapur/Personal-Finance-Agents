"""Phase 3 exit criterion: the agent completes the maturity journey from the
user story, asks only for missing information and never self-approves."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.llm import FinalAnswer, RulesPolicy, ToolCall, Turn, parse_amounts_minor, parse_choice, parse_lockup_days, parse_yes_no
from app.agent.orchestrator import run_turn
from tests.conftest import CUSTOMER, OPERATOR
from tests.helpers import advance, approve, case, run_worker

STORY = "My $10,000 CD matures next week. Keep $3,000 available for my upcoming expenses and compare what I can do with the rest."


def turn(client, message, case_id=None):
    r = client.post("/v1/agent/turns", json={"message": message, "case_id": case_id}, headers=CUSTOMER)
    assert r.status_code == 200, r.text
    return r.json()


def test_parsers():
    assert parse_amounts_minor("Keep $3,000 available and $10,000 CD") == [300000, 1000000]
    assert parse_amounts_minor("$2.5k") == [250000]
    assert parse_lockup_days("I'm fine with 12 months") == 360
    assert parse_lockup_days("keep it liquid") == 0
    assert parse_lockup_days("up to a year") == 365
    assert parse_yes_no("Yes, that includes the rent") is True
    assert parse_yes_no("No, the rent is separate") is False
    assert parse_yes_no("hmm") is None


def test_agent_completes_user_story_journey(client):
    # Turn 1: the story. The agent reads the snapshot, opens the case, evaluates
    # and asks for what the records cannot supply.
    out = turn(client, STORY)
    assert out["case_id"] and out["state"] == "needs_information"
    tools = [c["tool"] for c in out["tool_calls"]]
    assert tools == ["read_cash_snapshot", "open_maturity_case", "evaluate_case"]
    assert set(out["questions"]) == {"preferred_lockup_days", "buffer_includes_obligations"}
    assert "still-locked CD cannot fund it" in out["reply"]
    case_id = out["case_id"]

    # Turn 2: answers. The agent records them, re-evaluates and presents options with citations.
    out = turn(client, "Yes, the $3,000 includes the rent. I can lock money up for a year.", case_id)
    assert [c["tool"] for c in out["tool_calls"]] == ["record_customer_answers", "evaluate_case"]
    assert out["state"] == "evaluating"
    reply = out["reply"]
    assert "$7,000.00 can be placed" in reply
    assert "$287.00" in reply and "$273.00" in reply and "$134.38" in reply
    assert "Not comparable: 9-Month Promotional CD" in reply and "eligibility_unknown" in reply
    assert "[bank_harbor v2026-09-r2 retrieved 2026-09-26T09:00:00Z (mock)]" in reply
    detail = case(client, case_id)
    assert detail["buffer_includes_obligations"] is True and detail["preferred_lockup_days"] == 365
    assert detail["plan"]["max_lockable_minor"] == 700000

    # Turn 3: the highest advertised yield is refused with the reason.
    out = turn(client, "Go with the Meridian 9-month promo, it pays the most.", case_id)
    assert out["tool_calls"] == [] or all(c["tool"] != "prepare_bank_instruction" for c in out["tool_calls"])
    assert "can't be selected" in out["reply"] and "eligibility" in out["reply"]
    assert out["state"] == "evaluating"

    # Turn 4: a feasible choice becomes a proposed action, not an execution.
    out = turn(client, "Renew into the 12-month CD.", case_id)
    assert [c["tool"] for c in out["tool_calls"]][-1] == "prepare_bank_instruction"
    assert out["state"] == "awaiting_approval"
    assert "Nothing is sent until you approve it" in out["reply"]
    detail = case(client, case_id)
    action_id = detail["current_action_id"]
    assert detail["review"]["instruction"]["amount_minor"] == 700000

    # Turn 5: the agent refuses to approve.
    out = turn(client, "Approve it and submit now.", case_id)
    assert out["refused"] is True and case(client, case_id)["state"] == "awaiting_approval"

    # The customer approves through the review screen; the worker executes; the bank confirms.
    assert approve(client, action_id).status_code == 200
    run_worker(client)
    out = turn(client, "What's the status?", case_id)
    assert out["state"] == "submitted" and "accepted the instruction" in out["reply"] and "bankref_" in out["reply"]
    advance(client, days=7)
    run_worker(client)
    out = turn(client, "Is it done?", case_id)
    assert out["state"] == "completed"
    assert "verified" in out["reply"] and "reconciliation checks matched" in out["reply"]
    assert "provider_reference:bankref_" in out["reply"]


def test_agent_asks_for_reserve_when_story_has_no_amount(client):
    out = turn(client, "My CD is maturing, what should I do?")
    assert out["case_id"] is None
    assert out["questions"] == ["minimum_buffer_minor"]
    assert "How much cash do you want to keep available" in out["reply"]


def test_agent_rejects_liquidity_violation_with_evidence(client):
    out = turn(client, "Keep $1,000 available from my CD. No need to ask about lockup, 12 months is fine. The $1,000 is separate from rent.")
    case_id = out["case_id"]
    out = turn(client, "Put $9,000 in the 12-month CD.", case_id)
    assert "breaches your cash buffer" in out["reply"]
    assert "$7,000.00" in out["reply"]
    assert case(client, case_id)["state"] == "evaluating"


def test_tool_scope_and_budget(fresh_db):
    from app.agent.tools import ToolContext, run_tool

    ctx = ToolContext(customer_id="cus_other", budget=2)
    result = asyncio.run(run_tool(ctx, "read_cash_snapshot", {"customer_id": "cus_demo_1"}))
    assert result["error"] == "refused"
    result = asyncio.run(run_tool(ctx, "list_maturity_options", {"account_id": "acct_cd_1"}))
    assert result["error"] == "refused"
    result = asyncio.run(run_tool(ctx, "read_cash_snapshot", {}))
    assert result["error"] == "refused" and "budget" in result["message"]
    with pytest.raises(Exception):
        asyncio.run(run_tool(ToolContext(customer_id="cus_demo_1"), "approve_action", {}))


def test_budget_exhaustion_escalates(fresh_db, monkeypatch):
    class LoopingPolicy:
        model_version = "loop-test"

        async def decide(self, turn, tool_schemas):
            return ToolCall("read_cash_snapshot", {})

    monkeypatch.setattr("app.agent.orchestrator.settings.agent_tool_budget", 3)
    out = asyncio.run(run_turn("cus_demo_1", "hello", policy=LoopingPolicy()))
    assert out.budget_exhausted and out.escalated and len(out.tool_calls) == 3


def test_tool_runs_are_recorded_and_redacted(client):
    turn(client, STORY)
    ov = client.get("/v1/operator/overview", headers=OPERATOR).json()
    runs = ov["tool_runs"]
    assert {r["tool_name"] for r in runs} == {"read_cash_snapshot", "open_maturity_case", "evaluate_case"}
    for r in runs:
        assert r["model_version"] == "rules-v1" and r["prompt_version"]
        assert "cus_demo_1" not in str(r["input"])


def test_mcp_server_exposes_scoped_tools(fresh_db):
    from mcp.client.client import Client

    from app.agent.mcp_server import build_server

    async def scenario():
        server = build_server("cus_demo_1")
        async with Client(server) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            assert {"read_cash_snapshot", "list_maturity_options", "project_cash", "prepare_bank_instruction", "get_instruction_status"} <= names
            assert not any("approve" in n for n in names)
            snap = await client.call_tool("read_cash_snapshot", {})
            data = snap.structured_content or {}
            assert data["deposits"][0]["id"] == "cd_demo_1"
            assert data["_meta"]["authority"]
            opened = await client.call_tool("open_maturity_case", {"deposit_id": "cd_demo_1", "minimum_buffer_minor": 100000, "obligation_ids": ["bill_demo_1"], "preferred_lockup_days": 365, "buffer_includes_obligations": False})
            case_id = opened.structured_content["id"]
            projection = await client.call_tool("project_cash", {"case_id": case_id, "allocation_minor": 700000})
            assert projection.structured_content["feasible"] is True
            assert projection.structured_content["max_lockable_minor"] == 700000

    asyncio.run(scenario())
