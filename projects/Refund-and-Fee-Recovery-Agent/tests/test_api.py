from __future__ import annotations

from backend.app.adapters.mock_issuer import sign_callback

from .conftest import CUSTOMER_HEADERS as H, OPERATOR_HEADERS as O, OTHER_HEADERS

CREATE = {"customer_id": "cus_demo_4", "order_ref": "order_mock_499", "reason_code": "promised_refund_missing", "target_minor": 8499, "currency": "USD", "evidence_ids": ["receipt_mock_1", "promise_mock_1"]}


def _create(client):
    r = client.post("/v1/recovery-cases", json=CREATE, headers=H)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"id", "status", "version", "next_step"} and body["status"] == "detected" and body["version"] == 1 and body["next_step"] == "reconcile_account_credits"
    return body["id"]


def test_auth_and_tenant_isolation(client):
    assert client.get("/v1/recovery-cases").status_code == 401
    assert client.get("/v1/recovery-cases", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/v1/ops/overview", headers=H).status_code == 403
    cid = _create(client)
    assert client.get(f"/v1/recovery-cases/{cid}", headers=OTHER_HEADERS).status_code == 403
    assert client.get(f"/v1/recovery-cases/{cid}/timeline", headers=OTHER_HEADERS).status_code == 403
    r = client.post("/v1/recovery-cases", json=dict(CREATE, customer_id="cus_other_9"), headers=H)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert client.get("/v1/recovery-cases", headers=OTHER_HEADERS).json()["items"] == []


def test_full_flow_over_http(client):
    cid = _create(client)
    rec = client.post(f"/v1/recovery-cases/{cid}/reconcile", headers=H).json()
    assert rec["status"] == "investigating" and rec["labels"]["provisional_is_not_recovery"] and rec["next_step"] == "draft_merchant_message"
    d = client.post(f"/v1/recovery-cases/{cid}/merchant-message-drafts", headers={**H, "Idempotency-Key": "k1"})
    assert d.status_code == 201
    d = d.json()
    review = d["review"]
    assert review["destination"]["address"] == "refunds@streambox.mock" and review["amount_minor"] == 8499 and review["irreversible_effect"] and review["documents"]
    again = client.post(f"/v1/recovery-cases/{cid}/merchant-message-drafts", headers={**H, "Idempotency-Key": "k1"}).json()
    assert again["action_id"] == d["action_id"]
    assert client.get(f"/v1/recovery-cases/{cid}/actions/{d['action_id']}", headers=H).json()["payload_hash"] == d["payload_hash"]
    stale = client.post(f"/v1/actions/{d['action_id']}/approve", json={"expected_case_version": 99, "action_payload_hash": d["payload_hash"], "approval_challenge_id": d["approval_challenge_id"]}, headers=H)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_version"
    assert client.post(f"/v1/actions/{d['action_id']}/approve", json={"expected_case_version": d["expected_case_version"], "action_payload_hash": d["payload_hash"], "approval_challenge_id": d["approval_challenge_id"]}, headers=OTHER_HEADERS).status_code == 403
    ok = client.post(f"/v1/actions/{d['action_id']}/approve", json={"expected_case_version": d["expected_case_version"], "action_payload_hash": d["payload_hash"], "approval_challenge_id": d["approval_challenge_id"]}, headers=H)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert client.post("/v1/ops/worker/run", headers=O).json()["jobs_handled"] == 1
    assert client.get(f"/v1/recovery-cases/{cid}", headers=H).json()["status"] == "merchant_pending"
    for days in (1, 2, 3):
        client.post("/v1/ops/clock/advance", json={"days": days}, headers=O)
    status = client.get(f"/v1/recovery-cases/{cid}", headers=H).json()
    assert status["status"] == "recovered" and status["amounts"]["final_recovered_minor"] == 8499 and status["completion_evidence_ref"].startswith("credit_matches:")
    tl = client.get(f"/v1/recovery-cases/{cid}/timeline", headers=H).json()
    assert [e["next_state"] for e in tl["events"] if e["event_type"] == "case.transitioned"][-1] == "recovered"
    assert all("payload" not in a for a in tl["actions"])
    inbox = client.get("/v1/recovery-cases", headers=H).json()["items"]
    assert inbox[0]["status"] == "recovered"


def test_provider_event_signature_and_replay(client, container):
    payload = {"provider": "statement_mock", "event_id": "http_1", "type": "statement.transaction_posted", "occurred_at": "2026-09-20T12:00:00Z", "environment": "mock",
               "data": {"transaction_id": "txn_http_1", "customer_id": "cus_demo_4", "payment_instrument_ref": "pi_visa_4242", "merchant_id": "mrc_mock_streaming", "direction": "credit", "kind": "refund",
                        "amount_minor": 1, "currency": "USD", "posted_at": "2026-09-20T12:00:00Z", "description": "x", "provider_ref": "http_ref_1"}}
    assert client.post("/v1/provider-events/recovery", json=payload).status_code == 403
    assert client.post("/v1/provider-events/recovery", json=payload, headers={"X-Provider-Signature": "bad"}).status_code == 403
    sig = sign_callback(container.settings.mock_provider_secret, payload)
    first = client.post("/v1/provider-events/recovery", json=payload, headers={"X-Provider-Signature": sig}).json()
    second = client.post("/v1/provider-events/recovery", json=payload, headers={"X-Provider-Signature": sig}).json()
    assert first["outcome"].startswith("transaction_inserted") and second["outcome"] == "duplicate_ignored"
    replay = client.post("/v1/ops/events/replay", json={"provider": "statement_mock", "event_id": "http_1"}, headers=O).json()
    assert replay["replayed"] is True
    assert client.post("/v1/ops/events/replay", json={"provider": "statement_mock", "event_id": "http_1"}, headers=H).status_code == 403


def test_agent_turn_question_and_answer_over_http(client):
    r = client.post("/v1/recovery-cases", json={"customer_id": "cus_demo_4", "order_ref": "order_mock_504", "target_minor": 4500, "currency": "USD"}, headers=H)
    cid = r.json()["id"]
    turn = client.post(f"/v1/recovery-cases/{cid}/agent-turns", json={}, headers=H).json()
    assert turn["status"] == "investigating" and turn["questions"][0]["kind"] == "ambiguous_credit_match" and turn["proposed_action"] is None
    assert [c["name"] for c in turn["tool_calls"]] == ["get_recovery_status", "find_refund_evidence", "match_refund_credits"]
    bad = client.post(f"/v1/recovery-cases/{cid}/answers", json={"answer": {"transaction_id": "txn_cred_other_merchant"}}, headers=H)
    assert bad.status_code == 422
    ok = client.post(f"/v1/recovery-cases/{cid}/answers", json={"answer": {"transaction_id": "txn_cred_504a"}}, headers=H).json()
    assert ok["status"] == "already_refunded" and ok["amounts"]["final_recovered_minor"] == 4500
    assert client.get("/v1/ops/tool-runs", headers=O).json()["items"][0]["tool_name"] == "get_recovery_status"


def test_commerce_event_ingestion_and_ops_overview(client):
    payload = {"schema": "acp.webhook.v1-fixture", "id": "acp_http_1", "type": "order.refunded", "created_at": "2026-09-20T00:00:00Z", "data": {"order_id": "order_mock_505", "merchant_id": "mrc_mock_streaming", "refunds": [{"id": "rf_505_http", "amount_minor": 3000, "currency": "USD", "status": "succeeded"}]}}
    r = client.post("/v1/commerce-events", json={"payload": payload}, headers=H)
    assert r.status_code == 201 and r.json()["grants_execution_authority"] is False and r.json()["refund_refs"] == ["rf_505_http"]
    assert client.post("/v1/commerce-events", json={"payload": {"schema": "other.v9"}}, headers=H).status_code == 422
    ov = client.get("/v1/ops/overview", headers=O).json()
    assert set(ov["capabilities"]) == {"merchant_mock", "issuer_mock", "merchant_sandbox"}
    assert ov["capabilities"]["merchant_sandbox"]["find_action"] is False and ov["capabilities"]["merchant_mock"]["environment"] == "mock"
    assert "metrics" in ov and "allowed_transitions" in ov
    assert client.post("/v1/ops/simulator/faults", json={"provider": "merchant_mock", "order_ref": "order_mock_499", "fault": "declined"}, headers=O).status_code == 200


def test_close_unresolved_over_http(client):
    cid = _create(client)
    client.post(f"/v1/recovery-cases/{cid}/reconcile", headers=H)
    r = client.post(f"/v1/recovery-cases/{cid}/close-unresolved", json={"reason": "customer no longer wants to pursue"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "unresolved"
