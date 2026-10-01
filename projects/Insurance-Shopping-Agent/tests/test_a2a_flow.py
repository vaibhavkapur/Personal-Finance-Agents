"""Phase 3 exit criterion: the customer agent completes a multi-turn quote exchange over A2A."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.adapters.a2a.inprocess import build_inprocess_a2a_registry
from app.agent.llm import ScriptedProvider
from app.agent.orchestrator import AgentOrchestrator
from app.config import Settings
from app.context import AppContext
from app.main import create_app
from app.persistence import models as m
from tests.conftest import Flow


@pytest.fixture
def a2a_ctx(tmp_path, clock):
    settings = Settings(database_url="sqlite:///%s/a2a.db" % tmp_path, fixture_clock=True, environment="mock", adapter_mode="a2a")
    return AppContext(settings, clock=clock, registry=build_inprocess_a2a_registry(clock))


@pytest.fixture
def a2a_client(a2a_ctx):
    with TestClient(create_app(a2a_ctx)) as c:
        yield c


@pytest.mark.asyncio
async def test_full_journey_over_a2a_with_agent_turns(a2a_client, a2a_ctx):
    flow = Flow(a2a_client, a2a_ctx, "cus_demo_1")
    flow.create()
    flow.interview()
    agent = AgentOrchestrator(a2a_ctx, ScriptedProvider())
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "Please get quotes.")
    assert "Do you own jewelry" in turn["reply"]
    view = flow.view()
    tasks = {t["insurer_id"]: t for t in view["quote_tasks"]}
    assert tasks["ins_cedar_c"]["status"] == "input_required"
    assert all(t["external_task_id"] for t in tasks.values())  # external A2A task ids are mapped to internal tasks
    flow.answer_questions()
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "options?")
    assert "Excluded: Cedar & Pine" in turn["reply"]
    a_quote = flow.quote_id_for("ins_northwind_a")
    turn = await agent.handle_message(flow.case_id, "cus_demo_1", "select %s" % a_quote)
    view = flow.view()
    assert view["status"] == "awaiting_approval"
    assert view["pending_action"]["review"]["destination"]["protocol"] == "a2a/0.3.0"
    prepared = {"action": view["pending_action"], "expected_case_version": view["version"], "action_payload_hash": view["pending_action"]["payload_hash"], "approval_challenge_id": view["pending_action"]["challenge_id"]}
    flow.approve(prepared)
    flow.run_worker()
    assert flow.view()["status"] == "underwriting"
    final = flow.settle()
    assert final["status"] == "completed"
    assert final["policy"]["verified"] is True
    assert final["quotes"][0]["source"]["protocol"] == "a2a/0.3.0"
    with a2a_ctx.db.session() as session:
        ops = {p.operation for p in session.query(m.ProviderRequestLog).all()}
    assert {"request_quote", "answer_question", "submit_application", "get_policy_status"} <= ops


@pytest.mark.asyncio
async def test_a2a_timeout_and_reconciliation_end_to_end(a2a_client, a2a_ctx):
    flow = Flow(a2a_client, a2a_ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    a2a_ctx.registry.insurers["ins_northwind_a"].inject_fault("timeout", "submit_application")
    flow.approve(flow.prepare("ins_northwind_a"))
    flow.run_worker()
    assert flow.view()["actions"][-1]["status"] == "uncertain"
    flow.advance(minutes=1)
    flow.run_worker()
    assert flow.view()["actions"][-1]["status"] == "executed"
    final = flow.settle()
    assert final["status"] == "completed"
