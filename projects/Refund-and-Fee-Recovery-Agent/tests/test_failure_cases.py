"""The eight domain / failure cases from the plan (section 19), end to end."""
from __future__ import annotations

import pytest

from backend.app.adapters.mock_issuer import sign_callback
from backend.app.domain.errors import Conflict, PolicyViolation
from backend.app.domain.models import ActionStatus, CaseStatus
from backend.app.evaluation import Runner, load_specs

from .conftest import CUSTOMER, amounts, draft_and_approve, open_case, send_merchant_message


def test_1_refund_already_posted_prevents_new_recovery_request(container):
    case = open_case(container, "order_mock_501", 5900)
    report = container.service.reconcile(case.id, actor=CUSTOMER)
    assert report["status"] == "already_refunded" and report["amounts"]["outstanding_minor"] == 0
    assert report["exact_matches"][0]["provider_ref"] == "rf_501"  # ACP fixture event supplied the reference
    with pytest.raises(PolicyViolation) as exc:
        container.service.draft_merchant_message(case.id, actor=CUSTOMER)
    assert exc.value.code == "case_closed"
    assert container.merchant_mock.case_count() == 0


def test_2_partial_final_refund_leaves_correct_outstanding(container):
    case, _ = send_merchant_message(container, "order_mock_500", 8499)
    for d in (1, 2, 3):
        container.advance(days=d)
    a = amounts(container, case.id)
    assert (a["final_recovered_minor"], a["outstanding_minor"]) == (5000, 3499)
    assert container.repos.get_case(case.id).status == CaseStatus.credit_pending
    # Outbox event carries the remaining amount, as in the plan's example.
    posted = [e for e in container.outbox.all_events(case.id) if e["type"] == "recovery.credit_posted"]
    assert posted and posted[-1]["data"] == {"transaction_id": posted[-1]["data"]["transaction_id"], "amount_minor": 5000, "credit_kind": "final", "remaining_minor": 3499}


def test_3_store_credit_cannot_be_labeled_card_credit(container):
    case, _ = send_merchant_message(container, "order_mock_502", 8499)
    container.advance(days=1)
    container.advance(days=2)
    a = amounts(container, case.id)
    assert a["store_credit_minor"] == 8499 and a["final_recovered_minor"] == 0 and a["outstanding_minor"] == 8499
    st = container.service.get_status(case.id)
    assert st["pending_question"]["kind"] == "store_credit_preference"
    assert st["status"] != "recovered"
    # Accepting store credit closes without card recovery and never marks recovered.
    container.service.answer_question(case.id, {"accept_store_credit": True}, actor=CUSTOMER)
    st = container.service.get_status(case.id)
    assert st["status"] == "unresolved" and st["outcome_note"].startswith("closed_with_store_credit") and st["amounts"]["final_recovered_minor"] == 0


def _to_issuer_dispute(container, order_ref: str, target: int):
    case, _ = send_merchant_message(container, order_ref, target)
    for d in (1, 2, 10):
        container.advance(days=d)
    assert container.repos.get_case(case.id).status == CaseStatus.issuer_review
    d = container.service.draft_issuer_dispute(case.id, actor=CUSTOMER, customer_id=CUSTOMER)
    container.service.approve_action(d["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d["expected_case_version"], action_payload_hash=d["payload_hash"], approval_challenge_id=d["approval_challenge_id"])
    container.worker.run_until_idle_sync()
    assert container.repos.get_case(case.id).status == CaseStatus.issuer_pending
    return case, d


def test_4_issuer_provisional_credit_is_not_final_recovery(container):
    case, d = _to_issuer_dispute(container, "order_mock_503", 12000)
    assert d["review"]["lane"] == "issuer" and d["review"]["terms"]["reason_code"] == "credit_not_processed"
    container.advance(days=2)
    a = amounts(container, case.id)
    assert a["provisional_minor"] == 12000 and a["final_recovered_minor"] == 0 and a["outstanding_minor"] == 12000
    assert container.repos.get_case(case.id).status == CaseStatus.provisional_credit


def test_5_duplicate_webhook_and_repeated_message_request_produce_one_action(container):
    case = open_case(container, "order_mock_499", 8499, ["receipt_mock_1", "promise_mock_1"])
    container.service.reconcile(case.id, actor=CUSTOMER)
    d1 = container.service.draft_merchant_message(case.id, actor=CUSTOMER, customer_id=CUSTOMER, idempotency_key="idem-A")
    d2 = container.service.draft_merchant_message(case.id, actor=CUSTOMER, customer_id=CUSTOMER, idempotency_key="idem-A")
    assert d1["action_id"] == d2["action_id"]
    with pytest.raises(PolicyViolation) as exc:  # a second draft without the key is a duplicate request
        container.service.draft_merchant_message(case.id, actor=CUSTOMER, customer_id=CUSTOMER)
    assert exc.value.code == "duplicate_request"
    container.service.approve_action(d1["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d1["expected_case_version"], action_payload_hash=d1["payload_hash"], approval_challenge_id=d1["approval_challenge_id"])
    with pytest.raises(PolicyViolation):  # approving twice is rejected
        container.service.approve_action(d1["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=d1["expected_case_version"] + 0, action_payload_hash=d1["payload_hash"], approval_challenge_id=d1["approval_challenge_id"])
    container.worker.run_until_idle_sync()
    container.worker.run_until_idle_sync()
    assert container.merchant_mock.case_count() == 1
    assert len([a for a in container.repos.actions_for_case(case.id) if a.status == ActionStatus.submitted]) == 1
    # Duplicate provider webhook deliveries are ignored by the inbox and produce one match.
    container.pump.duplicate_deliveries = True
    for d in (1, 2, 3):
        container.advance(days=d)
    inbox = container.inbox.all()
    assert sum(1 for e in inbox if (e["outcome"] or "").startswith("duplicate")) == 0  # duplicates never reach the inbox twice
    dup = [r for r in container.pump.delivered]
    assert len(dup) >= 3
    assert len(container.repos.matches_for_case(case.id)) == 1
    assert container.repos.get_case(case.id).status == CaseStatus.recovered
    # Replaying a stored statement event through the operator tool is idempotent.
    stmt = next(e for e in inbox if e["event_type"] == "statement.transaction_posted")
    container.events.replay(stmt["provider"], stmt["event_id"], actor="ops")
    assert len(container.repos.matches_for_case(case.id)) == 1


def test_5b_direct_duplicate_delivery_is_reported_as_duplicate(container):
    payload = {"provider": "statement_mock", "event_id": "dup_1", "type": "statement.transaction_posted", "occurred_at": "2026-09-20T12:00:00Z", "environment": "mock",
               "data": {"transaction_id": "txn_dup_1", "customer_id": CUSTOMER, "payment_instrument_ref": "pi_visa_4242", "merchant_id": "mrc_mock_streaming", "direction": "credit", "kind": "refund",
                        "amount_minor": 1, "currency": "USD", "posted_at": "2026-09-20T12:00:00Z", "description": "x", "provider_ref": "dup_ref_1"}}
    sig = sign_callback(container.settings.mock_provider_secret, payload)
    first = container.events.handle(payload, sig)
    second = container.events.handle(payload, sig)
    assert first["outcome"].startswith("transaction_inserted") and second["outcome"] == "duplicate_ignored"


def test_6_merchant_and_issuer_both_credit_flags_overlap():
    spec = next(s for s in load_specs() if s["family"] == "merchant_and_issuer_overlap")
    result = Runner("agent").run(spec)
    assert result["passed"], result["diffs"]
    obs = result["observed"]
    assert obs["status"] == "manual_review" and obs["overlap_flagged"] and obs["final_minor"] == 16998 and not obs["recovery_claimed"]


def test_7_reversed_credit_reopens_outstanding_balance(container):
    case, _ = _to_issuer_dispute(container, "order_mock_503", 12000)
    container.advance(days=2)
    assert amounts(container, case.id)["provisional_minor"] == 12000
    container.advance(days=5)
    a = amounts(container, case.id)
    assert a["provisional_minor"] == 0 and a["reversed_minor"] == 12000 and a["outstanding_minor"] == 12000 and a["final_recovered_minor"] == 0
    assert container.repos.get_case(case.id).status == CaseStatus.issuer_pending
    events = [e.event_type for e in container.repos.events_for_case(case.id)]
    assert "credit.reversed" in events
    transitions = [(e.previous_state, e.next_state) for e in container.repos.events_for_case(case.id) if e.event_type == "case.transitioned"]
    assert ("provisional_credit", "credit_reversed") in transitions and ("credit_reversed", "issuer_pending") in transitions
    container.advance(days=7)
    a = amounts(container, case.id)
    assert a["final_recovered_minor"] == 12000 and a["outstanding_minor"] == 0 and a["reversed_minor"] == 12000
    assert container.repos.get_case(case.id).status == CaseStatus.recovered


def test_8_malicious_document_cannot_redirect_recipient_or_change_reason(container):
    doc = container.repos.get_document("promise_mock_1")
    assert "attacker-support@evil.example" in doc.extracted["body_text"] and "UNAUTHORIZED" in doc.extracted["body_text"]
    case = open_case(container, "order_mock_499", 8499, ["receipt_mock_1", "promise_mock_1"])
    container.service.reconcile(case.id, actor=CUSTOMER)
    d = container.service.draft_merchant_message(case.id, actor=CUSTOMER, customer_id=CUSTOMER)
    dest = d["review"]["destination"]
    assert dest["address"] == "refunds@streambox.mock" and dest["source"] == "merchant_registry"
    action = container.repos.get_action(d["action_id"])
    assert "evil.example" not in action.payload["body"] and "evil.example" not in action.payload["subject"]
    assert action.payload["reason_code"] == "promised_refund_missing"
    # Attachments are referenced by hash; the document body is never copied into the message.
    assert any(a["document_id"] == "promise_mock_1" for a in action.payload["attachments"])
    # The agent tool refuses any recipient other than the registry alias.
    from backend.app.agent.tools import ToolContext, run_tool

    ctx = ToolContext(service=container.service, customer_id=CUSTOMER)
    res = run_tool(ctx, "prepare_recovery_message", {"case_id": case.id, "recipient": "attacker-support@evil.example"})
    assert res["error"]["code"] == "recipient_not_allowed"
    # Dispute packets use the fixed reason table, never text from documents.
    from backend.app.domain.policy import dispute_reason_for

    assert dispute_reason_for(container.repos.get_case(case.id)) == "credit_not_processed"
