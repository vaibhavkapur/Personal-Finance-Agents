from __future__ import annotations

import asyncio
import json

from backend.app.agent.a2a import A2A_PROTOCOL_VERSION, MerchantAgentGateway
from backend.app.agent.llm import ModelDecision, ToolCall
from backend.app.agent.mcp_server import McpServer
from backend.app.agent.orchestrator import Orchestrator
from backend.app.agent.tools import ToolContext, run_tool, tool_schemas
from backend.app.domain.models import CaseStatus

from .conftest import CUSTOMER, open_case, send_merchant_message


def test_agent_drafts_only_after_matching_and_never_sends(container):
    case = open_case(container, "order_mock_499", 8499, ["receipt_mock_1", "promise_mock_1"])
    turn = Orchestrator(container.service).run(case_id=case.id, customer_id=CUSTOMER)
    assert [c["name"] for c in turn.tool_calls] == ["get_recovery_status", "find_refund_evidence", "match_refund_credits", "prepare_recovery_message"]
    assert turn.status == "awaiting_approval" and turn.proposed_action["type"] == "send_merchant_message"
    assert "Nothing is sent until you approve" in turn.summary
    assert container.merchant_mock.case_count() == 0
    assert not turn.guardrail["blocked"]
    runs = container.repos.tool_runs(case.id)
    assert len(runs) == 4 and all(r["input_ref"].startswith("sha256:") and r["prompt_version"] for r in runs)


def test_agent_explains_merchant_account_mismatch_without_claiming_recovery(container):
    case, _ = send_merchant_message(container, "order_mock_503", 12000)
    container.advance(days=1)
    container.advance(days=2)
    turn = Orchestrator(container.service).run(case_id=case.id, customer_id=CUSTOMER)
    assert turn.status == "refund_promised"
    assert "no matching credit has posted" in turn.summary and "not recovered" in turn.summary
    assert "final credits posted 0.00 USD" in turn.summary
    assert turn.proposed_action is None


def test_agent_asks_when_evidence_missing_or_ambiguous(container):
    case = open_case(container, "order_mock_505", 3000)
    turn = Orchestrator(container.service).run(case_id=case.id, customer_id=CUSTOMER)
    assert turn.questions[0]["kind"] == "missing_promise_evidence" and turn.proposed_action is None
    case = open_case(container, "order_mock_504", 4500)
    turn = Orchestrator(container.service).run(case_id=case.id, customer_id=CUSTOMER)
    assert turn.questions[0]["kind"] == "ambiguous_credit_match" and "txn_cred_504a" in turn.summary


def test_guardrail_blocks_unsupported_recovery_claims(container):
    class Overclaimer:
        version = "fake/overclaimer"

        def decide(self, *, system_prompt, transcript, tools):
            if len(transcript) == 2:
                return ModelDecision(tool_calls=[ToolCall("get_recovery_status", {"case_id": transcript[1]["case_id"]})])
            return ModelDecision(final_message="Great news: your refund has been recovered and the money is back.")

    case = open_case(container, "order_mock_499", 8499)
    turn = Orchestrator(container.service, model=Overclaimer()).run(case_id=case.id, customer_id=CUSTOMER)
    assert turn.guardrail["blocked"] and "cannot confirm recovery" in turn.summary
    assert "recovered and the money" not in turn.summary


def test_tool_budget_escalates_with_evidence(container):
    class Looper:
        version = "fake/looper"

        def decide(self, *, system_prompt, transcript, tools):
            return ModelDecision(tool_calls=[ToolCall("get_recovery_status", {"case_id": transcript[1]["case_id"]})])

    case = open_case(container, "order_mock_499", 8499)
    turn = Orchestrator(container.service, model=Looper(), max_tool_calls=3).run(case_id=case.id, customer_id=CUSTOMER)
    assert len(turn.tool_calls) == 3 and turn.escalation["reason"] == "tool_budget_exhausted"
    assert "operator should review" in turn.summary


def test_tools_are_customer_scoped_and_report_provenance(container):
    ctx = ToolContext(service=container.service, customer_id="cus_other_9")
    res = run_tool(ctx, "find_refund_evidence", {"order_id": "order_mock_499"})
    assert res["error"]["code"] == "not_found"
    case = open_case(container, "order_mock_499", 8499)
    res = run_tool(ctx, "get_recovery_status", {"case_id": case.id})
    assert res["error"]["code"] == "forbidden"
    ctx = ToolContext(service=container.service, customer_id=CUSTOMER)
    res = run_tool(ctx, "find_refund_evidence", {"order_id": "order_mock_499"})
    assert res["authority"] == "authoritative" and res["source"] == "application_records" and res["retrieved_at"]
    assert all("reply_to_found_in_email" not in d["extracted"] for d in res["data"]["documents"])
    assert run_tool(ctx, "nope", {})["error"]["code"] == "unknown_tool"
    assert run_tool(ctx, "get_recovery_status", {})["error"]["code"] == "invalid_arguments"


def test_mcp_server_lists_and_calls_tools(container):
    server = McpServer(ToolContext(service=container.service, customer_id=CUSTOMER))
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in listed["result"]["tools"]} == {t["name"] for t in tool_schemas()}
    assert all("inputSchema" in t for t in listed["result"]["tools"])
    call = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "find_refund_evidence", "arguments": {"order_id": "order_mock_499"}}})
    body = json.loads(call["result"]["content"][0]["text"])
    assert call["result"]["isError"] is False and body["data"]["purchase"]["order_ref"] == "order_mock_499"
    bad = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "prepare_recovery_message", "arguments": {"case_id": "x", "recipient": "evil@example"}}})
    assert bad["result"]["isError"] is True
    assert server.handle({"jsonrpc": "2.0", "id": 5, "method": "unknown"})["error"]["code"] == -32601


def test_a2a_gateway_maps_external_task_ids_and_pins_version(container):
    case = open_case(container, "order_mock_499", 8499)
    container.service.reconcile(case.id, actor=CUSTOMER)
    d = container.service.draft_merchant_message(case.id, actor=CUSTOMER, customer_id=CUSTOMER)
    action = container.repos.get_action(d["action_id"])
    gw = MerchantAgentGateway(container.merchant_mock)
    assert gw.agent_card()["protocolVersion"] == A2A_PROTOCOL_VERSION
    mapping = asyncio.run(gw.send_task(case_id=case.id, action_id=action.id, request_ref=action.request_ref, approved_payload=action.payload))
    assert gw.resolve(mapping.external_task_id).case_id == case.id and gw.task_for_case(case.id) is mapping
    again = asyncio.run(gw.send_task(case_id=case.id, action_id=action.id, request_ref=action.request_ref, approved_payload=action.payload))
    assert again is mapping and container.merchant_mock.case_count() == 1
