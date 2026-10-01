"""Workflow, authorization, retry and durability tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.adapters.base import Capabilities, ProviderError, ProviderResult, ProviderTimeout
from backend.app.clock import FixtureClock
from backend.app.container import build_container
from backend.app.domain.errors import Conflict, PolicyViolation, StaleVersion
from backend.app.domain.models import ActionStatus, Authority, CaseStatus, ChannelStatus
from backend.app.workflows.worker import Worker

from .conftest import CUSTOMER, amounts, draft_and_approve, open_case, send_merchant_message


def _ready_draft(container, order="order_mock_499", target=8499):
    case = open_case(container, order, target)
    container.service.reconcile(case.id, actor=CUSTOMER)
    return case, container.service.draft_merchant_message(case.id, actor=CUSTOMER, customer_id=CUSTOMER)


def test_case_creation_does_not_contact_merchant_and_returns_next_step(container):
    case = open_case(container, "order_mock_499", 8499, ["receipt_mock_1", "promise_mock_1"])
    assert case.status == CaseStatus.detected and case.version == 1
    assert container.service.next_step(case) == "reconcile_account_credits"
    assert container.merchant_mock.case_count() == 0 and container.repos.provider_requests() == []
    with pytest.raises(Conflict):
        open_case(container, "order_mock_499", 8499)
    with pytest.raises(PolicyViolation) as exc:
        container.service.draft_merchant_message(case.id, actor=CUSTOMER)
    assert exc.value.code == "reconcile_first"


def test_create_case_validations(container):
    from backend.app.domain.errors import DomainError, Forbidden, NotFound

    with pytest.raises(NotFound):
        open_case(container, "order_does_not_exist", 100)
    with pytest.raises(DomainError):
        open_case(container, "order_mock_499", 99999)
    # Evidence owned by another customer cannot be attached (tenant ownership enforced).
    from backend.app.domain.models import Customer, Document

    container.repos.upsert_customer(Customer(id="cus_other_9", tenant_id="tenant_demo", display_name="Other", email="o@example.test"))
    container.repos.add_document(Document(id="doc_other", customer_id="cus_other_9", kind="receipt", object_key="x", content_hash="sha256:o", source="email_import", captured_at="2026-09-01T00:00:00Z", extraction_version="v1", extracted={}))
    with pytest.raises(Forbidden):
        open_case(container, "order_mock_499", 8499, ["doc_other"])
    # A different customer's purchase is invisible to this customer.
    with pytest.raises(NotFound):
        container.service.create_case(customer_id="cus_other_9", order_ref="order_mock_499", reason_code="promised_refund_missing", target_minor=8499, currency="USD", evidence_ids=[], actor="x")
    case = container.service.create_case(customer_id=CUSTOMER, order_ref="order_mock_499", reason_code="duplicate_billing", target_minor=8499, currency="USD", evidence_ids=[], actor=CUSTOMER)
    assert case.status == CaseStatus.not_supported


def test_approval_requires_matching_challenge_hash_and_version(container):
    case, d = _ready_draft(container)
    with pytest.raises(StaleVersion):
        container.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"] - 1, action_payload_hash=d["payload_hash"], approval_challenge_id=d["approval_challenge_id"])
    with pytest.raises(PolicyViolation) as exc:
        container.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"], action_payload_hash="sha256:other", approval_challenge_id=d["approval_challenge_id"])
    assert exc.value.code == "payload_changed"
    with pytest.raises(PolicyViolation) as exc:
        container.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"], action_payload_hash=d["payload_hash"], approval_challenge_id="challenge_wrong")
    assert exc.value.code == "challenge_mismatch"
    assert container.repos.get_action(d["action_id"]).status == ActionStatus.awaiting_approval


def test_expired_challenge_invalidates_action(container):
    case, d = _ready_draft(container)
    container.clock.advance(hours=25)
    with pytest.raises(PolicyViolation) as exc:
        container.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"], action_payload_hash=d["payload_hash"], approval_challenge_id=d["approval_challenge_id"])
    assert exc.value.code == "challenge_expired"
    assert container.repos.get_action(d["action_id"]).status == ActionStatus.expired
    assert container.worker.run_until_idle_sync() == 0 and container.merchant_mock.case_count() == 0


def test_material_change_revokes_pending_approval(container):
    """A credit posting while a message awaits approval supersedes the draft."""
    case, d = _ready_draft(container)
    from backend.app.adapters.mock_issuer import sign_callback

    payload = {"provider": "statement_mock", "event_id": "late_1", "type": "statement.transaction_posted", "occurred_at": "2026-09-20T12:00:00Z", "environment": "mock",
               "data": {"transaction_id": "txn_late_1", "customer_id": CUSTOMER, "payment_instrument_ref": "pi_visa_4242", "merchant_id": "mrc_mock_streaming", "direction": "credit", "kind": "refund",
                        "amount_minor": 8499, "currency": "USD", "posted_at": "2026-09-20T11:00:00Z", "description": "REFUND", "provider_ref": "rf_late_1"}}
    container.events.handle(payload, sign_callback(container.settings.mock_provider_secret, payload))
    action = container.repos.get_action(d["action_id"])
    assert action.status == ActionStatus.superseded
    assert container.repos.approval_for_action(action.id).revoked_at is not None
    assert container.repos.get_case(case.id).status == CaseStatus.recovered
    with pytest.raises(PolicyViolation):
        container.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"], action_payload_hash=d["payload_hash"], approval_challenge_id=d["approval_challenge_id"])
    assert container.merchant_mock.case_count() == 0


def test_executor_reverifies_authority_before_side_effect(container):
    case, d = _ready_draft(container)
    container.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"], action_payload_hash=d["payload_hash"], approval_challenge_id=d["approval_challenge_id"])
    apr = container.repos.approval_for_action(d["action_id"])
    container.repos.save_approval(apr.model_copy(update={"revoked_at": "2026-09-20T12:00:01Z", "revoke_reason": "customer withdrew consent"}))
    container.worker.run_until_idle_sync()
    action = container.repos.get_action(d["action_id"])
    assert action.status == ActionStatus.failed and "revoked" in action.failure_reason
    assert container.merchant_mock.case_count() == 0
    assert any(e.event_type == "action.blocked" for e in container.repos.events_for_case(case.id))


def test_model_cannot_close_case_or_approve(container):
    from backend.app.agent.tools import tool_schemas

    names = {t["name"] for t in tool_schemas()}
    assert names == {"find_refund_evidence", "match_refund_credits", "prepare_recovery_message", "prepare_dispute_packet", "get_recovery_status"}
    case = open_case(container, "order_mock_499", 8499)
    from backend.app.domain.errors import IllegalTransition

    with pytest.raises(IllegalTransition):
        container.service._transition(case, CaseStatus.recovered, "agent:test")


def test_followups_are_bounded_then_issuer_review(container):
    case, _ = send_merchant_message(container, "order_mock_unresponsive", 6000)
    for _ in range(4):
        container.advance(days=5)
    actions = [a for a in container.repos.actions_for_case(case.id) if a.type.value == "send_merchant_followup"]
    assert len(actions) == container.settings.max_followups == 2
    assert all(a.status == ActionStatus.submitted and a.approval_id for a in actions)
    assert container.repos.get_case(case.id).status == CaseStatus.issuer_review
    ch = container.repos.channels_for_case(case.id)[0]
    assert ch.followup_count == 2
    container.advance(days=30)  # no further contact after the cap
    assert len([a for a in container.repos.actions_for_case(case.id) if a.type.value == "send_merchant_followup"]) == 2


def test_no_contact_after_case_closed(container):
    case, _ = send_merchant_message(container, "order_mock_499", 8499)
    for d in (1, 2, 3):
        container.advance(days=d)
    assert container.repos.get_case(case.id).status == CaseStatus.recovered
    provider_calls_before = len(container.repos.provider_requests())
    container.advance(days=60)
    assert len(container.repos.provider_requests()) == provider_calls_before
    with pytest.raises(PolicyViolation):
        container.service.draft_merchant_message(case.id, actor=CUSTOMER)


def test_timeout_after_accept_is_resolved_by_request_reference_without_duplicate(container):
    case, d = send_merchant_message(container, "order_mock_timeout", 8499)
    action = container.repos.get_action(d["action_id"])
    assert action.status == ActionStatus.unknown
    assert container.service.next_step(container.repos.get_case(case.id)) == "wait_for_provider_confirmation"
    container.advance(minutes=2)
    action = container.repos.get_action(d["action_id"])
    assert action.status == ActionStatus.submitted and action.provider_ref
    assert container.merchant_mock.case_count() == 1
    assert container.repos.get_case(case.id).status == CaseStatus.merchant_pending
    events = [e.event_type for e in container.repos.events_for_case(case.id)]
    assert "action.outcome_unknown" in events and "action.resolved_by_lookup" in events


def test_malformed_response_goes_to_manual_review_after_safe_retry(container):
    case, d = send_merchant_message(container, "order_mock_malformed", 8499)
    container.advance(minutes=2)
    container.advance(minutes=10)
    assert container.repos.get_case(case.id).status == CaseStatus.manual_review
    assert container.merchant_mock.case_count() == 0
    events = [e for e in container.repos.events_for_case(case.id)]
    assert sum(1 for e in events if e.event_type == "action.retry_same_request_ref") == 1
    with pytest.raises(PolicyViolation):
        container.service.draft_merchant_message(case.id, actor=CUSTOMER)
    # Operator can release only to a legitimate state; recovered needs evidence.
    with pytest.raises(PolicyViolation):
        container.service.operator_release(case.id, "recovered", actor="ops", note="nope")
    released = container.service.operator_release(case.id, "investigating", actor="ops", note="merchant endpoint fixed; retry from scratch")
    assert released.status == CaseStatus.investigating


def test_declined_merchant_moves_to_issuer_review(container):
    case, d = send_merchant_message(container, "order_mock_declined", 2500)
    assert container.repos.get_action(d["action_id"]).status == ActionStatus.declined
    assert container.repos.get_case(case.id).status == CaseStatus.issuer_review
    assert container.repos.channels_for_case(case.id)[0].status == ChannelStatus.declined


class _NoLookupAdapter:
    provider = "merchant_mock"
    environment = "sandbox"

    def capabilities(self):
        return Capabilities(provider=self.provider, environment=self.environment, open_case=True, send_followup=False, get_case=False, find_action=False)

    async def open_case(self, packet, request_ref):
        raise ProviderTimeout("timeout")

    async def send_followup(self, *a, **k):
        raise ProviderError("unsupported")

    async def get_case(self, *a, **k):
        raise ProviderError("unsupported")

    async def find_action(self, *a, **k):
        raise ProviderError("unsupported")


def test_uncertain_write_without_lookup_capability_holds_for_review(container):
    container.service.adapters["merchant_mock"] = _NoLookupAdapter()
    case, d = send_merchant_message(container, "order_mock_499", 8499)
    assert container.repos.get_action(d["action_id"]).status == ActionStatus.unknown
    assert container.repos.get_case(case.id).status == CaseStatus.manual_review
    assert not [j for j in container.jobs.all() if j.type == "resolve_uncertain_write"]


def test_dispute_lane_requires_merchant_first_and_separate_approval(container):
    case = open_case(container, "order_mock_499", 8499)
    container.service.reconcile(case.id, actor=CUSTOMER)
    with pytest.raises(PolicyViolation) as exc:
        container.service.draft_issuer_dispute(case.id, actor=CUSTOMER)
    assert exc.value.code == "merchant_first"
    case, d = send_merchant_message(container, "order_mock_503", 12000)
    with pytest.raises(PolicyViolation) as exc:
        container.service.draft_issuer_dispute(case.id, actor=CUSTOMER)
    assert exc.value.code == "not_yet_eligible"
    for day in (1, 2, 10):
        container.advance(days=day)
    dispute = container.service.draft_issuer_dispute(case.id, actor=CUSTOMER, customer_id=CUSTOMER)
    assert dispute["type"] == "submit_issuer_dispute" and dispute["approval_challenge_id"] != d["approval_challenge_id"]
    assert dispute["review"]["deadline"]["fixture"] is True
    packet = container.repos.get_action(dispute["action_id"]).payload["packet"]
    assert packet["reason_code"] == "credit_not_processed" and packet["amount_minor"] == 12000
    assert "unauthorized" in packet["statement"].lower() and "not a claim" in packet["statement"]


def test_worker_restart_recovers_approvals_timers_and_evidence(tmp_path: Path):
    db_path = str(tmp_path / "restart.db")
    c1 = build_container(database_path=db_path)
    c1.seed()
    case = open_case(c1, "order_mock_499", 8499, ["receipt_mock_1", "promise_mock_1"])
    c1.service.reconcile(case.id, actor=CUSTOMER)
    d = draft_and_approve(c1, case.id)  # approved; job persisted but not executed
    pending = [j for j in c1.jobs.all() if j.status == "pending"]
    assert pending and pending[0].type == "execute_action"
    now = c1.clock.now().isoformat()
    c1.db.close()

    # "Restart": a brand new process over the same database file.
    c2 = build_container(database_path=db_path)
    c2.clock.set(now)
    assert c2.repos.get_case(case.id).status == CaseStatus.awaiting_approval
    assert c2.repos.approval_for_action(d["action_id"]).approved_at is not None
    assert len(c2.repos.documents_for_customer(CUSTOMER)) >= 13
    c2.worker.run_until_idle_sync()
    assert c2.repos.get_action(d["action_id"]).status == ActionStatus.submitted
    assert c2.repos.get_case(case.id).status == CaseStatus.merchant_pending
    timer = [j for j in c2.jobs.all() if j.type == "merchant_followup_check"]
    assert timer and timer[0].status == "pending"
    for day in (1, 2, 3):
        c2.advance(days=day)
    assert c2.repos.get_case(case.id).status == CaseStatus.recovered
    c2.db.close()


def test_job_leases_are_exclusive(container):
    case, d = _ready_draft(container)
    container.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"], action_payload_hash=d["payload_hash"], approval_challenge_id=d["approval_challenge_id"])
    job = container.jobs.due()[0]
    assert container.jobs.lease(job, "worker-A", 60) is not None
    other = Worker(jobs=container.jobs, service=container.service, outbox=container.outbox, clock=container.clock, worker_id="worker-B")
    assert other.run_once_sync() == 0
    container.clock.advance(minutes=2)  # lease expired: B may take over
    assert other.run_once_sync() == 1
    assert container.merchant_mock.case_count() == 1


def test_outbox_events_are_signed_and_dispatched_once(container):
    seen = []
    container.outbox.subscribe(seen.append)
    case = open_case(container, "order_mock_501", 5900)
    container.service.reconcile(case.id, actor=CUSTOMER)
    container.outbox.dispatch_pending()
    container.outbox.dispatch_pending()
    types = [e["type"] for e in seen]
    assert types.count("recovery.case_created") == 1 and "recovery.credit_posted" in types
    from backend.app.persistence.outbox import verify
    from backend.app.ids import canonical_json

    ev = dict(seen[0])
    sig = ev.pop("_signature")
    assert verify(container.settings.webhook_signing_secret, canonical_json(ev), sig)


def test_commerce_event_evidence_enables_exact_match_but_grants_no_authority(container):
    docs = [d for d in container.repos.documents_for_customer(CUSTOMER) if d.kind in ("acp_refund_event", "ucp_order_adjustment")]
    assert {d.kind for d in docs} == {"acp_refund_event", "ucp_order_adjustment"}
    assert all(d.extracted["grants_execution_authority"] is False and d.extracted["authority"] == "simulated" for d in docs)
    ucp = next(d for d in docs if d.kind == "ucp_order_adjustment")
    assert ucp.extracted["order_ref"] == "order_mock_499" and ucp.extracted["refund_refs"] == []  # pending adjustment: evidence of a promise only
    case = open_case(container, "order_mock_499", 8499)
    report = container.service.reconcile(case.id, actor=CUSTOMER)
    assert report["status"] == "investigating" and report["amounts"]["final_recovered_minor"] == 0


def test_unresolved_outcome_is_recorded_explicitly(container):
    case, _ = send_merchant_message(container, "order_mock_unresponsive", 6000)
    closed = container.service.record_unresolved(case.id, actor=CUSTOMER, reason="customer chose to stop pursuing", customer_id=CUSTOMER)
    assert closed.status == CaseStatus.unresolved and closed.outcome_note == "unresolved:customer chose to stop pursuing"
    assert amounts(container, case.id)["final_recovered_minor"] == 0
    assert container.repos.channels_for_case(case.id)[0].status == ChannelStatus.closed
