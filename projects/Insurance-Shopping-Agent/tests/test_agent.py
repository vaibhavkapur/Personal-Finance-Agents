"""Agent orchestrator, tool scoping, document injection (case 7), budget escalation and MCP."""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

import pytest

from app.agent.llm import LLMResponse, ScriptedProvider, ToolCall
from app.agent.mcp_server import MCP_PROTOCOL_VERSION, MCPToolServer
from app.agent.orchestrator import AgentOrchestrator
from app.agent.tools import TOOL_SCHEMAS, AgentTools
from app.persistence import models as m
from tests.conftest import Flow, auth


@pytest.mark.asyncio
async def test_scripted_agent_drives_interview_quotes_comparison_and_review(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.create()
    agent = AgentOrchestrator(ctx, ScriptedProvider())
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "Find renters insurance that covers replacing my belongings, includes liability protection and starts when I move next month.")
    assert "address" in turn["reply"]
    assert [c["name"] for c in turn["tool_calls"]] == ["get_confirmed_needs"]

    flow.interview()
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "Done, please get quotes.")
    assert [c["name"] for c in turn["tool_calls"]] == ["get_confirmed_needs", "request_quotes", "get_confirmed_needs", "compare_coverage"]
    assert "Do you own jewelry, watches or furs" in turn["reply"]  # insurer question quoted verbatim
    assert "Missing: Cedar & Pine" in turn["reply"]
    assert "I don't know" in turn["reply"]

    flow.answer_questions()
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "Yes I do. What are my options?")
    assert "compare_coverage" in [c["name"] for c in turn["tool_calls"]]
    assert "Excluded: Cedar & Pine" in turn["reply"] and "CP-EX-4" in turn["reply"]
    assert "product rule" in turn["reply"]
    a_quote = flow.quote_id_for("ins_northwind_a")

    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "select %s" % a_quote)
    assert "prepare_application" in [c["name"] for c in turn["tool_calls"]]
    assert "nothing has been sent" in turn["reply"]
    assert "I cannot approve" in turn["reply"]
    view = flow.view()
    assert view["status"] == "awaiting_approval"
    assert view["pending_action"]["status"] == "proposed"  # the agent could not approve it

    # A resumed conversation works from case state, not chat history.
    fresh_agent = AgentOrchestrator(ctx, ScriptedProvider())
    turn = await fresh_agent.handle_message(flow.case_id, "cus_demo_1", "hello again")
    assert "waiting for your review" in turn["reply"]
    history = client.get("/v1/insurance-shopping-cases/%s/messages" % flow.case_id, headers=auth("cus_demo_1")).json()
    assert len(history) == 10
    with ctx.db.session() as session:
        runs = session.query(m.ToolRun).filter(m.ToolRun.case_id == flow.case_id).all()
    assert runs and all(r.model_version == "scripted-rules/1" for r in runs)
    assert all("address" not in json.dumps(r.input_redacted) or "[redacted]" in json.dumps(r.input_redacted) for r in runs)


@pytest.mark.asyncio
async def test_agent_refuses_to_select_excluded_quote(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    agent = AgentOrchestrator(ctx, ScriptedProvider())
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "select %s" % flow.quote_id_for("ins_cedar_c"))
    assert "not suitable" in turn["reply"]
    assert flow.view()["status"] == "awaiting_selection"


@pytest.mark.asyncio
async def test_agent_escalates_contradictory_answers_with_evidence(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    flow.answers([{"question_id": "nw_q_dog", "value": True}, {"question_id": "hl_q_animals", "value": False}])
    agent = AgentOrchestrator(ctx, ScriptedProvider())
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "what now?")
    assert turn["escalated"] is True
    assert "contradiction" in turn["reply"]
    op = client.get("/v1/operator/cases/%s" % flow.case_id, headers=auth("operator")).json()
    assert any(e["type"] == "insurance.agent.escalated" for e in op["events"])


class InjectedProvider:
    """Simulates a model that obeys instructions found in a policy document."""

    name = "injected-model/test"

    def __init__(self, other_case_id: str) -> None:
        self.other_case_id = other_case_id
        self.step = 0

    async def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> LLMResponse:
        self.step += 1
        if self.step == 1:
            return LLMResponse(tool_calls=[ToolCall("1", "get_policy_form", {"insurer_id": "ins_cedar_c", "clause_ids": ["CP-NOTE-1"]})])
        if self.step == 2:
            # "reveal other customers' data" -> tries another customer's case; "approve without review" -> no such tool exists
            return LLMResponse(tool_calls=[ToolCall("2", "get_confirmed_needs", {"case_id": self.other_case_id}), ToolCall("3", "approve_action", {"action_id": "any"})])
        tool_msgs = [json.loads(m_["content"]) for m_ in messages if m_.get("role") == "tool"]
        return LLMResponse(text="RESULTS:" + json.dumps(tool_msgs))


@pytest.mark.asyncio
async def test_case7_document_instructions_cannot_reach_other_customers_or_approve(client, ctx):
    victim = Flow(client, ctx, "cus_demo_2")
    victim.create()
    attacker = Flow(client, ctx, "cus_demo_1")
    attacker.create()
    provider = InjectedProvider(victim.case_id)
    agent = AgentOrchestrator(ctx, provider)
    turn = await agent.handle_message(attacker.case_id, "cus_demo_1", "read the policy note")
    results = json.loads(turn["reply"][len("RESULTS:"):])
    form = results[0]
    assert form["untrusted_text"] is True
    assert "ignore prior instructions" in form["clauses"][0]["text"]
    cross_tenant = results[1]
    assert "error" in cross_tenant and victim.case_id in cross_tenant["error"]
    assert "Jordan Sample" not in turn["reply"] and "Sacramento" not in turn["reply"]
    assert "error" in results[2] and "unknown tool" in results[2]["error"]
    assert victim.view()["actions"] == [] and attacker.view()["actions"] == []
    assert set(t["name"] for t in TOOL_SCHEMAS).isdisjoint({"approve_action", "submit_application", "complete_case"})


class LoopingProvider:
    name = "looping/test"

    async def complete(self, messages, tools):
        return LLMResponse(tool_calls=[ToolCall(uuid.uuid4().hex, "get_confirmed_needs", {"case_id": _case_id(messages)})])


def _case_id(messages):
    import re

    for msg in messages:
        if msg["role"] == "system" and "case_id=" in msg["content"]:
            return re.search(r"case_id=(\S+)", msg["content"]).group(1)
    return None


@pytest.mark.asyncio
async def test_tool_budget_exhaustion_escalates_without_side_effects(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.create()
    agent = AgentOrchestrator(ctx, LoopingProvider())
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "loop")
    assert turn["escalated"] is True
    assert len(turn["tool_calls"]) == ctx.settings.tool_call_budget
    assert "Nothing has been submitted or approved" in turn["reply"]
    assert flow.view()["status"] == "collecting"


@pytest.mark.asyncio
async def test_mcp_server_handshake_list_and_call(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.create()
    server = MCPToolServer(AgentTools(ctx, "cus_demo_1", model_version="mcp-test"))
    init = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    assert init["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert "tools" in init["result"]["capabilities"]
    assert await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert {"get_confirmed_needs", "request_quotes", "compare_coverage", "prepare_application", "verify_policy"} <= set(names)
    called = await server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_confirmed_needs", "arguments": {"case_id": flow.case_id}}})
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"]["case_status"] == "collecting"
    assert called["result"]["structuredContent"]["authority"] == "authoritative"
    unknown = await server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "approve_action", "arguments": {}}})
    assert unknown["error"]["code"] == -32602
    other = Flow(client, ctx, "cus_demo_2")
    other.create()
    denied = await server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_confirmed_needs", "arguments": {"case_id": other.case_id}}})
    assert denied["result"]["isError"] is True
