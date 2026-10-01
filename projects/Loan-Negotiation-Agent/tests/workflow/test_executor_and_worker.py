"""Failure cases: timeouts, malformed responses, declines, expiry, income policy, recovery."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.app.adapters.mock_lender import FAULT_DECLINED, FAULT_MALFORMED, FAULT_TIMEOUT_AFTER_ACCEPT
from backend.app.persistence.models import Action, Job
from backend.app.workflows.worker import Worker
from tests.conftest import BORROWER, OPERATOR, approve_screen, create_ready_case, offer_of


def _drain(container):
    return asyncio.get_event_loop().run_until_complete(container.worker.drain())


def _apply(client, view, lender_id):
    offer = offer_of(view, lender_id)
    r = client.post(f"/v1/loan-cases/{view['id']}/application-drafts", json={"offer_id": offer["id"]}, headers=BORROWER)
    assert r.status_code == 201, r.text
    screen = r.json()
    assert approve_screen(client, screen).status_code == 200
    return screen


def test_timeout_after_acceptance_is_found_by_original_reference(client, container):
    view = create_ready_case(client)
    container.network.inject_fault(FAULT_TIMEOUT_AFTER_ACCEPT, lender_id="lender_mock_a", operation="submit_application")
    screen = _apply(client, view, "lender_mock_a")
    results = _drain(container)
    assert results[0]["outcome"] == "uncertain"
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    pending = [a for a in v["actions"] if a["id"] == screen["action_id"]][0]
    assert pending["status"] == "uncertain" and v["status"] == "awaiting_approval"
    # The lender did store the application; nothing should be re-submitted.
    assert len(container.network.applications) == 1
    container.clock.advance(seconds=31)
    results = _drain(container)
    assert any(r["outcome"] == "completed" for r in results), results
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert v["status"] in ("submitted", "conditions_outstanding")
    assert v["applications"][0]["external_application_ref"] == "app_mock_0001"
    assert len(container.network.applications) == 1
    ops = [r for r in container.network.request_log if r["operation"] == "submit_application"]
    assert len(ops) == 1  # exactly one write


def test_malformed_response_never_creates_second_side_effect(client, container):
    view = create_ready_case(client)
    container.network.inject_fault(FAULT_MALFORMED, lender_id="lender_mock_a", operation="submit_application")
    _apply(client, view, "lender_mock_a")
    results = _drain(container)
    assert results[0]["outcome"] == "uncertain"
    container.clock.advance(seconds=31)
    _drain(container)
    container.clock.advance(seconds=121)
    _drain(container)
    container.clock.advance(seconds=601)
    _drain(container)
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert v["status"] == "manual_review"
    assert len([r for r in container.network.request_log if r["operation"] == "submit_application"]) == 1
    assert len(container.network.applications) == 0


def test_declined_application_moves_case_to_declined(client, container):
    view = create_ready_case(client)
    container.network.inject_fault(FAULT_DECLINED, lender_id="lender_mock_a", operation="submit_application")
    _apply(client, view, "lender_mock_a")
    _drain(container)
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert v["status"] == "declined" and v["applications"][0]["status"] == "declined"


def test_expired_offer_cannot_be_submitted_without_refresh(client, container):
    view = create_ready_case(client, docs=("offer_doc_a", "offer_doc_expired"))
    expired = offer_of(view, "lender_mock_a", status=("expired",))
    r = client.post(f"/v1/loan-cases/{view['id']}/application-drafts", json={"offer_id": expired["id"]}, headers=BORROWER)
    assert r.status_code == 422 and "expired" in r.json()["detail"]
    # Offer expiry after the draft: clock passes the expiry before execution; the simulator declines too.
    live = offer_of(view, "lender_mock_a")
    r = client.post(f"/v1/loan-cases/{view['id']}/application-drafts", json={"offer_id": live["id"]}, headers=BORROWER)
    screen = r.json()
    container.clock.advance(days=20)
    assert approve_screen(client, screen).status_code == 409  # challenge expired -> must redraft
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert v["status"] == "awaiting_approval"


def test_unsupported_income_assertion_blocks_application(client, container):
    view = create_ready_case(client)
    offer = offer_of(view, "lender_mock_a")
    r = client.post(f"/v1/loan-cases/{view['id']}/application-drafts", json={"offer_id": offer["id"], "income_assertion_minor": 25_000_000}, headers=BORROWER)
    assert r.status_code == 422 and "income" in r.json()["detail"]
    assert client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()["applications"] == []


def test_incomplete_offer_cannot_be_applied_for(client):
    view = create_ready_case(client)
    offer = offer_of(view, "lender_mock_c")
    r = client.post(f"/v1/loan-cases/{view['id']}/application-drafts", json={"offer_id": offer["id"]}, headers=BORROWER)
    assert r.status_code == 422 and "rate_lock" in r.json()["detail"]


def test_changed_final_costs_invalidate_approval_and_refresh_comparison(client, container):
    view = create_ready_case(client)
    harbor = offer_of(view, "lender_mock_b")
    client.post(f"/v1/loan-cases/{view['id']}/facts", json={"finance_costs": {harbor["id"]: True}}, headers=BORROWER)
    client.post(f"/v1/loan-cases/{view['id']}/compare", json={}, headers=BORROWER)
    view = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    _apply(client, view, "lender_mock_b")
    _drain(container)
    container.clock.advance(days=3)
    _drain(container)
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert v["status"] == "final_review"
    review = v["term_reviews"][-1]
    assert review["requires_reapproval"] is True
    fields = {d["field"] for d in review["differences"]["differences"] if d["material"]}
    assert {"financed_costs_minor", "principal_minor"} <= fields
    # The comparison now contains the final-offer version with the increased costs.
    final_offer = offer_of(v, "lender_mock_b", status=("final_offer",))
    assert final_offer["normalized"]["incremental_costs_net_minor"] == 600_000 + 60_000
    # Closing needs a fresh approval bound to the final terms hash; no old approval can be reused.
    r = client.post(f"/v1/loan-cases/{view['id']}/applications/{v['applications'][0]['id']}/closing-requests", headers=BORROWER)
    assert r.status_code == 201
    screen = r.json()
    assert screen["review"]["requires_reapproval"] is True
    assert screen["payload"]["final_terms_hash"].startswith("sha256:")
    old_action = [a for a in v["actions"] if a["type"] == "submit_application"][0]
    r = client.post(f"/v1/actions/{old_action['id']}/approve", json={"expected_case_version": screen["expected_case_version"], "action_payload_hash": old_action["payload_hash"], "approval_challenge_id": screen["approval_challenge_id"]}, headers=BORROWER)
    assert r.status_code == 409
    assert approve_screen(client, screen).status_code == 200
    _drain(container)
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert v["status"] == "mock_closed"
    assert v["applications"][0]["closing_evidence"]["closing_record_id"].startswith("closing_mock")
    assert v["applications"][0]["is_funded"] is False


def test_duplicate_execution_is_prevented(client, container):
    view = create_ready_case(client)
    screen = _apply(client, view, "lender_mock_a")
    # Enqueue a second job for the same action (e.g. a retried approval request).
    with container.db.session() as s:
        container.service.enqueue(s, "execute_action", view["id"], {"action_id": screen["action_id"]})
    _drain(container)
    assert len([r for r in container.network.request_log if r["operation"] == "submit_application"]) == 1
    # A second approve call for a consumed action is rejected.
    assert approve_screen(client, screen).status_code == 409


def test_worker_restart_recovers_leased_job(client, container):
    view = create_ready_case(client)
    _apply(client, view, "lender_mock_a")
    # Simulate a crashed worker holding the lease.
    dead = Worker(container.db, container.service, container.adapter, owner="worker-dead")
    ids = dead.lease_jobs()
    assert ids
    with container.db.session() as s:
        job = s.get(Job, ids[0])
        assert job.status == "leased" and job.lease_owner == "worker-dead"
    # Before the lease expires nobody else can take it.
    assert container.worker.lease_jobs() == []
    container.clock.advance(seconds=61)
    results = _drain(container)
    assert any(r["outcome"] == "completed" for r in results)
    v = client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()
    assert v["status"] in ("submitted", "conditions_outstanding")
    # Approval and evidence survived: exactly one approval consumed, one provider write.
    with container.db.session() as s:
        actions = s.execute(select(Action).where(Action.case_id == view["id"])).scalars().all()
        assert [a.status for a in actions] == ["completed"]
    assert len(container.network.applications) == 1


def test_cross_customer_access_is_denied(client):
    view = create_ready_case(client)
    from tests.conftest import OTHER

    assert client.get(f"/v1/loan-cases/{view['id']}", headers=OTHER).status_code == 404
    r = client.post("/v1/loan-cases", json={"customer_id": "cus_other_9", "mortgage_id": "mortgage_demo_1", "offer_document_ids": []}, headers=OTHER)
    assert r.status_code == 404
    r = client.post("/v1/loan-cases", json={"customer_id": "cus_other_9", "mortgage_id": "mortgage_other_1", "offer_document_ids": ["offer_doc_a"]}, headers=OTHER)
    assert r.status_code == 404  # document belongs to another customer
    screen = client.post(f"/v1/loan-cases/{view['id']}/lender-request-drafts", json={"lender_id": "lender_mock_servicer"}, headers=BORROWER).json()
    assert approve_screen(client, screen, headers=OTHER).status_code == 404


def test_operator_cannot_complete_a_case_or_bypass_authorization(client, container):
    view = create_ready_case(client)
    container.network.inject_fault(FAULT_MALFORMED, lender_id="lender_mock_a", operation="submit_application")
    _apply(client, view, "lender_mock_a")
    _drain(container)
    for delta in (31, 121, 601):
        container.clock.advance(seconds=delta)
        _drain(container)
    assert client.get(f"/v1/loan-cases/{view['id']}", headers=BORROWER).json()["status"] == "manual_review"
    r = client.post(f"/v1/ops/cases/{view['id']}/resume", json={"next_state": "mock_closed", "reason": "x"}, headers=OPERATOR)
    assert r.status_code == 403
    r = client.post(f"/v1/ops/cases/{view['id']}/resume", json={"next_state": "awaiting_decision", "reason": "provider confirmed nothing was created"}, headers=OPERATOR)
    assert r.status_code == 200 and r.json()["status"] == "awaiting_decision"
    assert client.get("/v1/ops/cases", headers=BORROWER).status_code == 403
