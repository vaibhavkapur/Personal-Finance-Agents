"""HTTP contract, MCP stdio server and A2A insurer agent endpoint."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import COMPLETE_DOCS, CUSTOMER, LOSS_AT, ROOT, Flow, make_ctx
from app.agent.mcp_server import MCPServer, MCP_PROTOCOL_VERSION
from app.main import create_app

CUST = {"Authorization": "Bearer tok_customer_demo_3"}
OTHER = {"Authorization": "Bearer tok_customer_demo_4"}
OPS = {"Authorization": "Bearer tok_operator_demo"}


@pytest.fixture
def client():
    ctx = make_ctx()
    app = create_app(ctx)
    with TestClient(app) as c:
        c.ctx = ctx
        yield c


def test_full_journey_over_http(client):
    r = client.get("/v1/claims", headers=CUST)
    assert r.status_code == 200
    assert client.post("/v1/claims", json={"customer_id": "cus_demo_3", "policy_id": "travel_policy_demo_1", "loss_type": "baggage_delay", "loss_at": LOSS_AT}).status_code == 401
    r = client.post("/v1/claims", headers=CUST, json={"customer_id": "cus_demo_3", "policy_id": "travel_policy_demo_1", "loss_type": "baggage_delay", "loss_at": LOSS_AT, "document_ids": ["itinerary_demo", "receipt_demo_1"]})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "collecting" and "baggage_arrival_confirmation" in body["missing_fields"] and "baggage_delay_report" in body["missing_fields"]
    cid = body["id"]
    assert client.get(f"/v1/claims/{cid}", headers=OTHER).status_code == 403
    r = client.post(f"/v1/claims/{cid}/documents", headers=CUST, json={"document_ids": ["baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_2", "receipt_demo_3_early", "receipt_demo_4"]})
    assert r.status_code == 200 and r.json()["status"] == "evaluating"
    r = client.post(f"/v1/claims/{cid}/evaluate", headers=CUST)
    assert r.json()["evaluation"]["totals"]["estimated_payable_minor"] == 12000
    r = client.post(f"/v1/claims/{cid}/submission-drafts", headers=CUST, json={})
    assert r.status_code == 201
    draft = r.json()
    assert draft["content_hash"].startswith("sha256:") and draft["disclosure_manifest"]["document_ids"]
    # stale version -> 409 ; wrong hash -> 422 ; operator -> 403
    r = client.post(f"/v1/actions/{draft['action_id']}/approve", headers=CUST, json={"expected_case_version": draft["expected_case_version"] + 5, "action_payload_hash": draft["content_hash"], "approval_challenge_id": draft["approval_challenge_id"]})
    assert r.status_code == 409 and r.json()["code"] == "stale_case_version"
    r = client.post(f"/v1/actions/{draft['action_id']}/approve", headers=OPS, json={"expected_case_version": draft["expected_case_version"], "action_payload_hash": draft["content_hash"], "approval_challenge_id": draft["approval_challenge_id"]})
    assert r.status_code == 403
    r = client.post(f"/v1/actions/{draft['action_id']}/approve", headers=CUST, json={"expected_case_version": draft["expected_case_version"], "action_payload_hash": draft["content_hash"], "approval_challenge_id": draft["approval_challenge_id"]})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = client.post("/v1/dev/worker/run-until-idle", headers=OPS)
    assert r.status_code == 200
    view = client.get(f"/v1/claims/{cid}", headers=CUST).json()
    assert view["case"]["status"] == "payout_pending" and view["case"]["external_claim_ref"]
    r = client.post("/v1/dev/mock-payments/emit", headers=OPS, json={"case_id": cid, "mode": "exact"})
    assert r.status_code == 200
    view = client.get(f"/v1/claims/{cid}", headers=CUST).json()
    assert view["case"]["status"] == "closed" and view["settlement"]["status"] == "paid_in_full"
    timeline = client.get(f"/v1/claims/{cid}/timeline", headers=CUST).json()
    assert timeline["events"][-1]["type"] == "case.closed"
    export = client.get(f"/v1/claims/{cid}/export", headers=CUST).json()
    assert export["export_version"] == "case-history/1.0" and export["packets"] and export["actions"]
    ops = client.get(f"/v1/claims/{cid}/operator", headers=OPS)
    assert ops.status_code == 200 and ops.json()["adapter_requests"]
    assert client.get(f"/v1/claims/{cid}/operator", headers=CUST).status_code == 403
    metrics = client.get("/v1/dev/metrics", headers=OPS).json()
    assert metrics["cases_total"] == 1
    assert client.get("/health").json()["adapter"]["environment"] == "mock"


def test_provider_event_endpoint_requires_valid_signature(client):
    payload = {"id": "evt_1", "type": "claim.received", "claim_reference": "x", "sequence": 1, "data": {}}
    r = client.post("/v1/provider-events/claims", json=payload, headers={"X-Mock-Signature": "sha256=nope"})
    assert r.status_code == 403
    from app.adapters.mock_insurer import sign_payload

    r = client.post("/v1/provider-events/claims", json=payload, headers={"X-Mock-Signature": sign_payload(client.ctx.settings.provider_webhook_secret, payload)})
    assert r.status_code == 200 and r.json()["outcome"] == "unmatched"


def test_agent_turn_endpoint_prepares_review_and_asks_only_missing(client):
    r = client.post("/v1/claims", headers=CUST, json={"customer_id": "cus_demo_3", "policy_id": "travel_policy_demo_1", "loss_type": "baggage_delay", "loss_at": LOSS_AT, "document_ids": ["itinerary_demo", "baggage_report_demo", "receipt_demo_1", "receipt_demo_2"]})
    cid = r.json()["id"]
    turn = client.post(f"/v1/claims/{cid}/agent/turns", headers=CUST, json={"message": "My bag arrived two days late. Help me claim the essential purchases."}).json()
    assert turn["planner"] == "scripted"
    assert turn["data"]["questions"] and all(q["field"] == "baggage_delivered_at" for q in turn["data"]["questions"])
    assert "estimate" in turn["message"]
    qid = turn["data"]["questions"][0]["id"]
    client.post(f"/v1/claims/{cid}/questions/{qid}/answer", headers=CUST, json={"answer": {"delivered_at": "2026-09-12T16:30:00Z"}})
    turn = client.post(f"/v1/claims/{cid}/agent/turns", headers=CUST, json={"message": "go ahead"}).json()
    assert turn["data"]["pending_action"]["status"] == "proposed"
    assert "approve action" in turn["message"]
    # LLM planner is not configured in tests -> explicit refusal, no silent fallback
    r = client.post(f"/v1/claims/{cid}/agent/turns", headers=CUST, json={"message": "hi", "planner": "llm"})
    assert r.status_code == 403


def test_a2a_agent_card_and_rpc(client):
    card = client.get("/insurer-agent/.well-known/agent.json").json()
    assert card["protocolVersion"] == "0.3" and any(s["id"] == "submit_claim" for s in card["skills"])
    r = client.post("/insurer-agent/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "missing"}}).json()
    assert r["error"]["code"] == -32001
    r = client.post("/insurer-agent/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {"message": {"role": "user", "parts": [{"kind": "data", "data": {"schema": "wrong/0", "operation": "get_claim"}}]}}}).json()
    assert r["error"]["data"]["kind"] == "declined"


def test_mcp_server_in_process_handshake_and_tool_call():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    server = MCPServer(ctx, CUSTOMER)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
    assert init["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} >= {"get_policy", "extract_claim_evidence", "build_claim_packet", "get_case_status"}
    out = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_policy", "arguments": {"policy_id": "travel_policy_demo_1", "loss_at": LOSS_AT}}})["result"]
    assert out["isError"] is False and out["structuredContent"]["data"]["version"] == "2026-09" and out["structuredContent"]["authority"] == "authoritative"
    out = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "build_claim_packet", "arguments": {"case_id": flow.case_id}}})["result"]
    assert out["structuredContent"]["data"]["expected_maximum_minor"] == 12000 and out["structuredContent"]["authority"] == "estimated"
    out = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "extract_claim_evidence", "arguments": {"document_ids": ["receipt_other_customer"]}}})["result"]
    assert out["isError"] is True  # cross-customer document refused


def test_mcp_server_over_stdio(tmp_path):
    db = tmp_path / "mcp.db"
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db}", MCP_PRINCIPAL_TOKEN="tok_customer_demo_3", PYTHONPATH=str(ROOT / "backend"))
    proc = subprocess.Popen([sys.executable, "-m", "app.agent.mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(ROOT))
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "extract_claim_evidence", "arguments": {"document_ids": ["receipt_demo_1"]}}},
    ]
    out, err = proc.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=60)
    assert proc.returncode == 0, err
    lines = [json.loads(l) for l in out.strip().splitlines()]
    assert lines[0]["result"]["serverInfo"]["name"] == "insurance-claims-advocate"
    assert any(t["name"] == "extract_claim_evidence" for t in lines[1]["result"]["tools"])
    data = lines[2]["result"]["structuredContent"]["data"]
    assert data["documents"][0]["receipt"]["total_minor"] == 3500
