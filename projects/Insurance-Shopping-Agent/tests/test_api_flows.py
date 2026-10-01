"""End-to-end API flows: demo scenarios 1-3 and failure cases 4, 5, 8 plus authorization rules."""
from __future__ import annotations

import json

import pytest

from app.persistence import models as m
from app.persistence.outbox import sign_payload
from tests.conftest import Flow, auth, household_answers


def test_create_case_returns_missing_fields(client, ctx):
    flow = Flow(client, ctx, "cus_demo_2")
    created = flow.create()
    assert created["status"] == "collecting"
    assert created["version"] == 1
    assert created["missing_fields"] == ["address", "deductible_preference"]


def test_unsupported_state_or_product_rejected(client, ctx):
    r = client.post("/v1/insurance-shopping-cases", json={"state_code": "NY", "product": "renters"}, headers=auth("cus_demo_1"))
    assert r.status_code == 422
    r = client.post("/v1/insurance-shopping-cases", json={"state_code": "CA", "product": "auto"}, headers=auth("cus_demo_1"))
    assert r.status_code == 422


def test_demo1_comparable_shopping_to_verified_issued_policy(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.create()
    flow.interview()
    requested = flow.request_quotes()
    statuses = {t["insurer_id"]: t["status"] for t in requested["tasks"]}
    assert statuses == {"ins_northwind_a": "quoted", "ins_harborline_b": "quoted", "ins_cedar_c": "input_required"}
    # Two quotes are already comparable; the missing insurer is disclosed rather than blocking the case.
    assert requested["status"] == "awaiting_selection"

    view = flow.view()
    blocking = [q for q in view["outstanding_questions"] if q["reason"] == "blocks_quote"]
    assert blocking[0]["question_id"] == "cp_q_high_value"
    assert blocking[0]["text"] == "Do you own jewelry, watches or furs with a combined value over $1,500?"

    flow.answer_questions()
    comparison = flow.comparison()
    assert [s["insurer_id"] for s in comparison["suitable"]] == ["ins_northwind_a", "ins_harborline_b"]
    assert comparison["excluded"][0]["insurer_id"] == "ins_cedar_c"
    assert comparison["excluded"][0]["failed_checks"][0]["citation"]["clause_id"] == "CP-EX-4"
    assert comparison["complete"] is True
    # every material comparison field carries a reference
    for row in comparison["differences"]:
        for value in row["values"].values():
            assert value["citation"] is not None or row["field"] in ("annual_premium", "exclusions", "valid_until", "policy_form_version", "endorsements")

    prepared = flow.prepare("ins_northwind_a")
    assert prepared["status"] == "awaiting_approval"
    review = prepared["review"]
    assert review["destination"]["insurer_id"] == "ins_northwind_a"
    assert review["amount"]["display"] == "$180.00 per year"
    assert review["terms"]["effective_date"] == "2026-11-01"
    assert len(review["answers"]) == 4  # only Northwind's questions are sent to Northwind
    assert {a["question_id"] for a in review["answers"]} == {"nw_q_dog", "nw_q_claims", "nw_q_smoke", "nw_q_home_business"}

    approved = flow.approve(prepared)
    assert approved["status"] == "approved"
    summary = flow.run_worker()
    assert summary["jobs"][0]["type"] == "execute_action"
    view = flow.view()
    assert view["status"] == "underwriting"
    assert view["policy"] is None  # submitted is not issued

    final = flow.settle()
    assert final["status"] == "completed"
    assert final["policy"]["verified"] is True
    assert final["policy"]["coverage_starts_in_future"] is True
    assert final["policy"]["coverage_label"].startswith("issued – coverage starts 2026-11-01")
    labels = [t["label"] for t in final["timeline"]]
    for label in ("quoted", "approved by customer", "submitted", "bound", "issued", "verified", "effective (coverage starts)"):
        assert label in labels
    assert labels.index("submitted") < labels.index("bound") < labels.index("issued")


def test_demo2_follow_up_question_resumes_insurer_task(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.create()
    flow.interview()
    flow.request_quotes()
    view = flow.view()
    cedar = next(t for t in view["quote_tasks"] if t["insurer_id"] == "ins_cedar_c")
    assert cedar["status"] == "input_required"
    external_before = cedar["external_task_id"]
    comparison = flow.comparison()
    assert comparison["complete"] is False
    assert comparison["missing_responses"][0]["insurer_id"] == "ins_cedar_c"

    # 'unknown' keeps the task blocked; it is never defaulted to no.
    flow.answers([{"question_id": "cp_q_high_value", "value": "unknown"}])
    assert next(t for t in flow.view()["quote_tasks"] if t["insurer_id"] == "ins_cedar_c")["status"] == "input_required"

    result = flow.answers([{"question_id": "cp_q_high_value", "value": True}])
    cedar_after = next(t for t in result["quote_tasks"] if t["insurer_id"] == "ins_cedar_c")
    assert cedar_after["status"] == "quoted"
    assert cedar_after["external_task_id"] == external_before  # same insurer task resumed
    assert result["status"] == "awaiting_selection"
    assert len(flow.view()["quotes"]) == 3


def test_demo3_revised_terms_require_fresh_approval(client, ctx):
    flow = Flow(client, ctx, "cus_demo_2")  # household with a prior claim -> Northwind surcharge
    flow.until_awaiting_selection()
    prepared = flow.prepare("ins_northwind_a")
    first_action = prepared["action"]["action_id"]
    flow.approve(prepared)
    flow.run_worker()
    assert flow.view()["status"] == "underwriting"
    flow.advance(hours=2)
    flow.run_worker()
    view = flow.view()
    assert view["status"] == "awaiting_approval"
    pending = view["pending_action"]
    assert pending["type"] == "accept_revised_offer"
    assert pending["status"] == "proposed"
    assert pending["review"]["amount"]["display"] == "$216.00 per year"
    assert pending["review"]["previous_premium_display"] == "$180.00"
    assert pending["review"]["premium_change_display"] == "+$36.00"
    assert "Prior-claim surcharge" in pending["review"]["revision_reason"]
    assert view["application"]["revision"] == 2
    revised_event = [t for t in view["timeline"] if t["event_type"] == "insurance.application.revised_offer"][0]
    assert revised_event["data"]["requires_new_approval"] is True
    # The original approval cannot be reused for the new terms.
    r = client.post("/v1/actions/%s/approve" % first_action, json={"expected_case_version": view["version"], "action_payload_hash": pending["payload_hash"], "approval_challenge_id": "x"}, headers=auth("cus_demo_2"))
    assert r.status_code == 409

    second = {"expected_case_version": view["version"], "action_payload_hash": pending["payload_hash"], "approval_challenge_id": pending["challenge_id"], "action": pending}
    flow.approve(second)
    flow.run_worker()
    assert flow.view()["status"] == "underwriting"
    final = flow.settle(hours=1)
    assert final["status"] == "completed"
    assert final["policy"]["declarations"]["annual_premium_minor"] == 21600
    assert final["policy"]["verified"] is True


def test_case4_one_insurer_times_out_while_two_quotes_remain(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.create()
    flow.interview()
    flow.inject("ins_harborline_b", "timeout_before", "request_quote")
    requested = flow.request_quotes()
    statuses = {t["insurer_id"]: t["status"] for t in requested["tasks"]}
    assert statuses["ins_harborline_b"] == "timeout"
    flow.answers([{"question_id": "cp_q_high_value", "value": True}])
    comparison = flow.comparison()
    assert comparison["complete"] is False
    assert [s["insurer_id"] for s in comparison["suitable"]] == ["ins_northwind_a"]
    missing = comparison["missing_responses"][0]
    assert missing["insurer_id"] == "ins_harborline_b" and "unknown" in missing["disclosure"]
    # The poller re-checks by the original request reference and the quote arrives.
    flow.advance(minutes=1)
    flow.run_worker()
    assert next(t for t in flow.view()["quote_tasks"] if t["insurer_id"] == "ins_harborline_b")["status"] == "quoted"
    assert flow.comparison()["complete"] is True


def test_case5_material_answer_change_invalidates_approval_and_requotes(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    prepared = flow.prepare("ins_northwind_a")
    flow.approve(prepared)
    # Before the executor runs, the customer changes a material fact for Northwind.
    result = flow.answers([{"question_id": "nw_q_claims", "value": True}])
    assert result["requoted_insurers"] == ["ins_northwind_a"]
    actions = {a["action_id"]: a for a in result["actions"]}
    assert actions[prepared["action"]["action_id"]]["status"] == "invalidated"
    assert result["status"] == "awaiting_selection"
    flow.run_worker()  # the executor must not submit an invalidated action
    view = flow.view()
    assert view["status"] == "awaiting_selection"
    assert all(a["status"] != "executed" for a in view["actions"])
    a_quote = next(q for q in view["quotes"] if q["insurer_id"] == "ins_northwind_a")
    assert a_quote["quote_id"] != prepared["application"]["quote_id"]


def test_case5b_needs_change_requotes_all_and_locks_after_submission(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    before = {q["insurer_id"]: q["quote_id"] for q in flow.view()["quotes"]}
    version_before = flow.view()["needs"]["version"]
    result = flow.answers([{"field": "property_limit_minor", "value": 4000000}])
    assert result["needs"]["version"] == version_before + 1
    after = {q["insurer_id"]: q["quote_id"] for q in result["quotes"]}
    assert set(after) == set(before) and all(after[k] != before[k] for k in before)
    a = next(q for q in result["quotes"] if q["insurer_id"] == "ins_northwind_a")
    assert a["annual_premium_minor"] == 18000 + 10 * 300
    prepared = flow.prepare("ins_northwind_a")
    flow.approve(prepared)
    flow.run_worker()
    r = client.post("/v1/insurance-shopping-cases/%s/answers" % flow.case_id, json={"answers": [{"field": "property_limit_minor", "value": 5000000}]}, headers=flow.headers)
    assert r.status_code == 409


def test_approval_rejects_stale_version_wrong_hash_and_expired_challenge(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    prepared = flow.prepare("ins_harborline_b")
    flow.approve(prepared, expect=409, expected_case_version=prepared["expected_case_version"] - 1)
    flow.approve(prepared, expect=409, action_payload_hash="sha256:not_the_reviewed_payload")
    flow.approve(prepared, expect=409, approval_challenge_id="challenge_forged")
    flow.advance(minutes=31)
    flow.approve(prepared, expect=410)
    assert flow.view()["actions"][-1]["status"] == "invalidated"


def test_idempotency_key_reuse(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    first = flow.prepare("ins_northwind_a", idempotency_key="idem-1")
    again = flow.prepare("ins_northwind_a", idempotency_key="idem-1")
    assert again["action"]["action_id"] == first["action"]["action_id"]
    flow.prepare("ins_harborline_b", expect=409, idempotency_key="idem-1")


def test_unsuitable_quote_cannot_be_applied_for(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    r = flow.prepare("ins_cedar_c", expect=409)
    assert "not suitable" in r["detail"]


def test_tenant_isolation(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.create()
    r = client.get("/v1/insurance-shopping-cases/%s" % flow.case_id, headers=auth("cus_demo_2"))
    assert r.status_code == 404
    r = client.get("/v1/insurance-shopping-cases", headers=auth("cus_demo_2"))
    assert flow.case_id not in [c["id"] for c in r.json()]
    r = client.get("/v1/operator/cases", headers=auth("cus_demo_1"))
    assert r.status_code == 403
    r = client.get("/v1/insurance-shopping-cases", headers={})
    assert r.status_code == 401


def test_case8_issued_effective_date_mismatch_opens_review_not_completion(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    flow.inject("ins_northwind_a", "effective_date_shift_days", "submit_application", days=7)
    prepared = flow.prepare("ins_northwind_a")
    flow.approve(prepared)
    flow.run_worker()
    final = flow.settle()
    assert final["status"] == "manual_review"
    assert final["policy"]["verified"] is False
    assert final["policy"]["verification"]["mismatches"][0]["field"] == "effective_date"
    assert "does not match" in final["review_reason"]
    # Operator sees redacted evidence and the mismatch event
    op = client.get("/v1/operator/cases/%s" % flow.case_id, headers=auth("operator")).json()
    assert op["needs"]["address"] == "[redacted]"
    assert any(e["type"] == "insurance.policy.mismatch" for e in op["events"])


def test_declined_application(client, ctx):
    flow = Flow(client, ctx, "cus_demo_3")  # home business -> Northwind declines
    flow.create()
    flow.interview(item_classes=("bicycles",))
    flow.request_quotes()
    flow.answer_questions()
    prepared = flow.prepare("ins_northwind_a")
    flow.approve(prepared)
    flow.run_worker()
    final = flow.settle()
    assert final["status"] == "declined"
    assert final["policy"] is None
    assert final["application"]["status"] == "declined"


def test_uncertain_write_is_reconciled_by_request_ref_not_retried_blindly(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    flow.inject("ins_northwind_a", "timeout", "submit_application")
    prepared = flow.prepare("ins_northwind_a")
    flow.approve(prepared)
    flow.run_worker()
    view = flow.view()
    action = view["actions"][-1]
    assert action["status"] == "uncertain"
    assert view["status"] == "submitted"
    flow.advance(seconds=5) if False else flow.advance(minutes=1)
    flow.run_worker()  # reconcile_action finds the recorded submission
    view = flow.view()
    assert view["actions"][-1]["status"] == "executed"
    assert view["status"] == "underwriting"
    with ctx.db.session() as session:
        submits = [p for p in session.query(m.ProviderRequestLog).all() if p.operation == "submit_application"]
    assert len(submits) == 1  # no duplicate side effect


def test_provider_event_signature_dedupe_and_replay(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    prepared = flow.prepare("ins_northwind_a")
    flow.approve(prepared)
    flow.run_worker()
    action_id = prepared["action"]["action_id"]
    payload = {"event_id": "evt-a-1", "type": "underwriting.status", "request_ref": action_id}
    body = json.dumps(payload).encode()
    ts = ctx.now().isoformat()
    bad = client.post("/v1/provider-events/insurer", content=body, headers={"X-Insurer-Id": "ins_northwind_a", "X-Insurer-Signature": "v1=bad", "X-Insurer-Timestamp": ts, "Content-Type": "application/json"})
    assert bad.status_code == 401
    sig = sign_payload("mock-secret-northwind", body, ts)
    headers = {"X-Insurer-Id": "ins_northwind_a", "X-Insurer-Signature": sig, "X-Insurer-Timestamp": ts, "Content-Type": "application/json"}
    first = client.post("/v1/provider-events/insurer", content=body, headers=headers)
    assert first.status_code == 202 and first.json()["duplicate"] is False and first.json()["scheduled"] == "poll_underwriting"
    dup = client.post("/v1/provider-events/insurer", content=body, headers=headers)
    assert dup.json()["duplicate"] is True
    replay = client.post("/v1/operator/replay/%s" % first.json()["inbox_id"], headers=auth("operator"))
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    # A callback alone never issues a policy: underwriting is still pending on the provider side.
    flow.run_worker()
    assert flow.view()["status"] == "underwriting"


def test_quote_expiry_moves_case_to_expired(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    flow.advance(days=31)
    flow.run_worker()
    view = flow.view()
    assert view["status"] == "expired"
    flow.request_quotes()
    flow.answers([{"question_id": "cp_q_high_value", "value": True}])
    assert flow.view()["status"] == "awaiting_selection"


def test_operator_metrics_and_review_resolution(client, ctx):
    flow = Flow(client, ctx, "cus_demo_1")
    flow.until_awaiting_selection()
    flow.inject("ins_northwind_a", "effective_date_shift_days", "submit_application", days=3)
    flow.approve(flow.prepare("ins_northwind_a"))
    flow.run_worker()
    flow.settle()
    metrics = client.get("/v1/operator/metrics", headers=auth("operator")).json()
    assert metrics["cases_by_state"].get("manual_review") == 1
    assert metrics["approvals_recorded"] == 1
    assert len(metrics["capability_matrix"]) == 3
    r = client.post("/v1/operator/cases/%s/resolve-review" % flow.case_id, json={"resolution": "decline", "note": "customer will re-shop"}, headers=auth("operator"))
    assert r.status_code == 200 and r.json()["status"] == "declined"
