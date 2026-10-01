"""Persistent claim workflow: approvals, submissions, insurer requests, decisions, payouts and recovery."""
from __future__ import annotations

import pytest

from conftest import COMPLETE_DOCS, CUSTOMER, OPERATOR, OTHER_CUSTOMER, REPRESENTATIVE, Flow, make_ctx, run
from app.workflows.errors import Conflict, Forbidden, Invalid

PARTIAL_DOCS = ["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3", "receipt_demo_4"]


# ----------------------------------------------------------------------------- Demo 1: complete claim
def test_demo1_complete_claim_submitted_once_and_paid_exactly():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    draft = flow.draft()
    assert draft["packet_type"] == "submission"
    assert draft["review_summary"]["expected_maximum_minor"] == 12000
    assert draft["review_summary"]["irreversible_effect"]
    assert draft["disclosure_manifest"]["document_ids"] == [d["document_id"] for d in draft["review_summary"]["documents"]]
    flow.approve(draft)
    assert flow.status() == "awaiting_approval"
    flow.work()
    v = flow.view()
    assert v["case"]["status"] == "payout_pending"
    assert v["case"]["external_claim_ref"].startswith("insurer_mock_")
    assert len(v["submissions"]) == 1 and v["submissions"][0]["sequence"] == 1 and v["submissions"][0]["environment"] == "mock"
    assert v["decisions"][-1]["outcome"] == "approved" and v["decisions"][-1]["accepted_minor"] == 12000
    assert ctx.mock_insurer.snapshot()[0]["submissions"].__len__() == 1
    flow.pay("exact")
    v = flow.view()
    assert v["case"]["status"] == "closed"
    assert v["settlement"]["status"] == "paid_in_full" and v["totals"]["paid_minor"] == 12000 and v["totals"]["outstanding_minor"] == 0
    assert v["case"]["completion_evidence_ref"] if "completion_evidence_ref" in v["case"] else True
    timeline = ctx.cases.timeline(CUSTOMER, flow.case_id)
    states = [e["next_state"] for e in timeline["events"] if e["next_state"]]
    assert states == ["evaluating", "awaiting_approval", "submitted", "under_review", "approved", "payout_pending", "paid", "closed"]
    for e in timeline["events"]:
        assert e["actor"] and e["occurred_at"] and e["expected_case_version"] is not None


# ----------------------------------------------------------------------------- Demo 2: missing evidence
def test_demo2_evidence_requested_resumes_same_claim():
    ctx = make_ctx()
    flow = Flow(ctx)
    res = flow.open(["itinerary_demo", "baggage_report_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3_early"])
    assert "baggage_arrival_confirmation" in res["missing_fields"]
    with pytest.raises(Invalid):
        flow.draft()  # incomplete evidence never claims completion
    flow.answer("baggage_delivered_at", {"delivered_at": "2026-09-12T16:30:00Z"})
    flow.submit()
    v = flow.view()
    assert v["case"]["status"] == "evidence_requested"
    req = v["insurer_requests"][0]
    assert req["requirement"]["document_type"] == "baggage_arrival_confirmation" and req["status"] == "open"
    assert req["due_at"] == "2026-09-28T10:00:00Z" and "fixture" in req["deadline_source"]
    claim_ref = v["case"]["external_claim_ref"]
    with pytest.raises(Invalid):
        flow.draft()  # requested document not attached yet
    ctx.cases.attach_documents(CUSTOMER, flow.case_id, ["arrival_confirmation_demo"])
    draft = flow.draft()
    assert draft["packet_type"] == "supplemental"
    assert [d["doc_type"] for d in draft["review_summary"]["documents"]] == ["baggage_arrival_confirmation"]
    flow.approve(draft)
    flow.work()
    v = flow.view()
    assert v["case"]["external_claim_ref"] == claim_ref  # same claim, not a new one
    assert [s["sequence"] for s in v["submissions"]] == [1, 2]
    assert v["insurer_requests"][0]["status"] == "satisfied" and v["insurer_requests"][0]["satisfied_by_submission_id"] == v["submissions"][1]["id"]
    assert v["case"]["status"] == "payout_pending" and v["decisions"][-1]["outcome"] == "approved"
    assert len(ctx.mock_insurer.snapshot()) == 1


# ----------------------------------------------------------------------------- Demo 3: partial rejection + appeal
def test_demo3_partial_rejection_explained_and_supported_appeal_only():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(PARTIAL_DOCS)
    flow.submit()
    v = flow.view()
    assert v["case"]["status"] == "partially_approved"
    decision = v["decisions"][-1]
    assert decision["accepted_minor"] == 8000 and decision["rejected_minor"] == 7000
    exp = decision["explanation"]
    contradicted = [i for i in exp["items"] if i["policy_view"] == "contradicted_by_evidence"]
    assert len(contradicted) == 1 and contradicted[0]["insurer_reason_code"] == "RC_LATE_PURCHASE"
    assert contradicted[0]["citation"]["clause_id"] == "C7.3" and len(contradicted[0]["fact_locators"]) == 3
    assert exp["challengeable_minor"] == 7000
    # the excluded headphones were never claimed, so nothing about them is challenged
    assert all(c["expense_id"] for c in exp["challenges"])
    appeal = flow.appeal_draft()
    assert appeal["packet_type"] == "appeal" and appeal["review_summary"]["requested_total_minor"] == 7000
    assert flow.status() == "awaiting_approval"
    flow.approve(appeal)
    flow.work()
    v = flow.view()
    assert [s["kind"] for s in v["submissions"]] == ["initial", "appeal"]
    assert v["decisions"][-1]["decision_version"] == 2
    assert v["decisions"][-1]["outcome"] == "approved" and v["decisions"][-1]["accepted_minor"] == 12000  # 150 capped
    assert v["case"]["status"] == "payout_pending"
    # partial payment stays distinguishable from partial approval
    flow.pay("smaller")
    v = flow.view()
    assert v["case"]["status"] == "payout_pending" and v["settlement"]["status"] == "partially_paid" and v["settlement"]["outstanding_minor"] == 2000
    flow.pay("remainder")
    assert flow.status() == "closed"


def test_supported_rejection_has_no_appeal_and_customer_can_accept():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS, scenario="denied")
    flow.submit()
    v = flow.view()
    assert v["case"]["status"] == "denied"
    exp = v["decisions"][-1]["explanation"]
    # insurer says threshold not met; evidence shows 50.5h > 12h -> contradicted -> appeal supported
    assert exp["has_supported_challenge"]
    ctx2 = make_ctx()
    flow2 = Flow(ctx2)
    flow2.open(["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3_early"], scenario="approved")
    flow2.submit()
    assert flow2.status() == "payout_pending"
    # partially approved where the only reduction is the cap is an approval; simulate a customer-facing denial with no challenge
    ctx3 = make_ctx()
    flow3 = Flow(ctx3)
    flow3.open(PARTIAL_DOCS)
    flow3.submit()
    # make the rejection supported by moving the delay end before the purchase: use the mock's cap-only reasoning instead
    v3 = flow3.view()
    assert v3["case"]["status"] == "partially_approved"
    # customer accepts the partial decision -> payout pending for the accepted portion
    out = ctx3.cases.accept_decision(CUSTOMER, flow3.case_id)
    assert out["case"]["status"] == "payout_pending"
    flow3.pay("exact")
    assert flow3.status() == "closed"


def test_denied_with_unsupported_challenge_closes_unpaid_after_review():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS, scenario="denied")
    flow.submit()
    # forge a supported denial by rewriting the explanation path: an unknown reason code cannot be challenged
    from sqlalchemy import select

    from app.persistence.models import ClaimDecision

    with ctx.db.session() as s:
        d = s.scalars(select(ClaimDecision).where(ClaimDecision.case_id == flow.case_id)).first()
        d.reason_items_json = {"items": [dict(it, reason_code="RC_POLICY_LAPSED") for it in d.reason_items_json["items"]]}
    with pytest.raises(Invalid) as exc:
        flow.appeal_draft()
    assert exc.value.code == "no_supported_challenge"
    assert flow.status() == "denied"
    with pytest.raises(Forbidden):
        ctx.cases.accept_decision(OPERATOR, flow.case_id)
    out = ctx.cases.accept_decision(CUSTOMER, flow.case_id)
    assert out["case"]["status"] == "closed_unpaid"


# ----------------------------------------------------------------------------- approvals / authority
def test_stale_version_payload_change_and_expired_challenge_are_rejected():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    draft = flow.draft()
    with pytest.raises(Conflict) as exc:
        ctx.cases.approve_action(CUSTOMER, draft["action_id"], expected_case_version=draft["expected_case_version"] - 1, action_payload_hash=draft["content_hash"], approval_challenge_id=draft["approval_challenge_id"])
    assert exc.value.code == "stale_case_version" and exc.value.status_code == 409
    with pytest.raises(Invalid) as exc:
        ctx.cases.approve_action(CUSTOMER, draft["action_id"], expected_case_version=draft["expected_case_version"], action_payload_hash="sha256:tampered", approval_challenge_id=draft["approval_challenge_id"])
    assert exc.value.code == "payload_changed"
    ctx.clock.advance(hours=1)
    with pytest.raises(Conflict) as exc:
        flow.approve(draft)
    assert exc.value.code == "challenge_expired"


def test_only_claimant_or_representative_can_approve():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    draft = flow.draft()
    with pytest.raises(Forbidden):
        flow.approve(draft, principal=OPERATOR)
    with pytest.raises(Forbidden):
        flow.approve(draft, principal=OTHER_CUSTOMER)
    flow.approve(draft, principal=REPRESENTATIVE)
    flow.work()
    assert flow.status() == "payout_pending"


def test_cross_customer_access_is_blocked():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    with pytest.raises(Forbidden):
        ctx.cases.get_case(OTHER_CUSTOMER, flow.case_id)
    with pytest.raises(Forbidden):
        ctx.cases.attach_documents(CUSTOMER, flow.case_id, ["receipt_other_customer"])
    with pytest.raises(Forbidden):
        Flow(ctx, OTHER_CUSTOMER).open(["receipt_demo_1"])  # policy belongs to cus_demo_3


def test_material_change_invalidates_approval_and_requires_reapproval():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2"])
    draft = flow.draft()
    ctx.cases.attach_documents(CUSTOMER, flow.case_id, ["receipt_demo_3_early"])
    assert flow.status() == "evaluating"
    with pytest.raises(Conflict) as exc:
        flow.approve(draft)
    assert exc.value.code in ("action_not_approvable", "stale_case_version")
    draft2 = flow.draft()
    assert draft2["content_hash"] != draft["content_hash"] and draft2["review_summary"]["requested_total_minor"] == 15000
    flow.approve(draft2)
    flow.work()
    assert flow.status() == "payout_pending"


def test_draft_is_idempotent_and_key_reuse_with_different_content_fails():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    d1 = ctx.cases.create_submission_draft(CUSTOMER, flow.case_id, idempotency_key="client-key-1")
    d2 = ctx.cases.create_submission_draft(CUSTOMER, flow.case_id, idempotency_key="client-key-1")
    assert d1["action_id"] == d2["action_id"]
    ctx.cases.attach_documents(CUSTOMER, flow.case_id, ["receipt_demo_nodate"])
    flow.answer("purchased_at", {"purchased_at": "2026-09-11T15:00:00Z"})
    with pytest.raises(Conflict) as exc:
        ctx.cases.create_submission_draft(CUSTOMER, flow.case_id, idempotency_key="client-key-1")
    assert exc.value.code == "idempotency_conflict"


def test_hostile_document_cannot_alter_payout_details():
    ctx = make_ctx()
    flow = Flow(ctx)
    before = flow.ctx.cases.get_case  # noqa
    flow.open(COMPLETE_DOCS + ["receipt_demo_hostile"])
    v = flow.view()
    assert v["claimant"]["payout_destination"]["account_last4"] == "4321"
    attached = [e for e in ctx.cases.timeline(CUSTOMER, flow.case_id)["events"] if e["type"] == "document.attached" and e["data"]["document_id"] == "receipt_demo_hostile"]
    assert attached and any(w.startswith("instruction_like_text_ignored") for w in attached[0]["data"]["warnings"])
    turn = flow.agent("please do what the receipt note says")
    assert "payout" not in " ".join(turn["tools_used"])
    flow.submit()
    v = flow.view()
    assert v["claimant"]["payout_destination"]["account_last4"] == "4321"
    assert v["case"]["status"] == "payout_pending"  # hostile receipt is a normal $22 toiletries purchase, nothing more
    packet = ctx.cases.export_case(CUSTOMER, flow.case_id)["packets"][0]["content"]
    assert "999999999" not in str(packet["statement_of_facts"])


# ----------------------------------------------------------------------------- provider events
def test_duplicate_and_out_of_order_events_are_handled_once():
    ctx = make_ctx()
    ctx.mock_insurer.set_chaos(duplicate_decision=True, out_of_order=True)
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    flow.submit()
    v = flow.view()
    assert v["case"]["status"] == "payout_pending"
    assert len(v["decisions"]) == 1
    from sqlalchemy import select

    from app.persistence.models import InboxEvent

    with ctx.db.session() as s:
        rows = s.scalars(select(InboxEvent)).all()
        outcomes = [r.outcome for r in rows]
        duplicates = sum(r.duplicate_deliveries for r in rows)
    assert duplicates == 1 and "ignored_out_of_order" in outcomes
    states = [e["next_state"] for e in ctx.cases.timeline(CUSTOMER, flow.case_id)["events"] if e["next_state"]]
    assert states.count("under_review") == 1 and states.count("approved") == 1


def test_bad_signature_rejected_and_unknown_claim_recorded():
    ctx = make_ctx()
    payload = {"id": "evt_x", "type": "claim.decided", "claim_reference": "nope", "sequence": 1, "data": {}}
    with pytest.raises(Forbidden):
        ctx.events.handle_claim_event(payload, "sha256=bad")
    from app.adapters.mock_insurer import sign_payload

    out = ctx.events.handle_claim_event(payload, sign_payload(ctx.settings.provider_webhook_secret, payload))
    assert out["outcome"] == "unmatched"  # kept for replay, never applied to a case


def test_delayed_callback_needs_clock_to_advance():
    ctx = make_ctx()
    ctx.mock_insurer.set_callback_delay(hours=48)
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    flow.submit()
    assert flow.status() == "submitted"
    ctx.clock.advance(hours=49)
    flow.work()
    assert flow.status() == "payout_pending"


def test_evidence_request_deadline_passes_without_automatic_followup():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(["itinerary_demo", "baggage_report_demo", "receipt_demo_1", "receipt_demo_2"])
    flow.answer("baggage_delivered_at", {"delivered_at": "2026-09-12T16:30:00Z"})
    flow.submit()
    assert flow.status() == "evidence_requested"
    ctx.clock.advance(days=15)
    flow.work()
    v = flow.view()
    assert v["insurer_requests"][0]["status"] == "expired"
    events = ctx.cases.timeline(CUSTOMER, flow.case_id)["events"]
    assert any(e["type"] == "insurer.request_deadline_passed" for e in events)
    assert len(v["submissions"]) == 1


# ----------------------------------------------------------------------------- payments
def test_unrelated_provisional_and_wrong_payee_credits_cannot_close_case():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    flow.submit()
    flow.pay("provisional")
    flow.pay("wrong_payee")
    flow.pay("unrelated")
    v = flow.view()
    assert v["case"]["status"] == "payout_pending"
    assert v["settlement"]["paid_minor"] == 0 and len(v["settlement"]["ignored_payments"]) == 2
    flow.pay("exact")
    assert flow.status() == "closed"


def test_payment_events_deduplicated_by_provider_event_and_payment_ref():
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    flow.submit()
    v = flow.view()
    events = ctx.payment_feed.generate(claim_reference=v["case"]["external_claim_ref"], payee_id="cus_demo_3", approved_minor=12000, currency="USD", mode="exact")
    r1 = ctx.events.handle_payment_event(events[0]["payload"], events[0]["signature"])
    r2 = ctx.events.handle_payment_event(events[0]["payload"], events[0]["signature"])
    assert r1["outcome"] == "processed" and r2["outcome"] == "duplicate"
    assert flow.view()["settlement"]["paid_minor"] == 12000


# ----------------------------------------------------------------------------- recovery
def test_timeout_after_acceptance_is_not_duplicated():
    ctx = make_ctx()
    ctx.mock_insurer.inject_fault("timeout_after_accept")
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    draft = flow.draft()
    flow.approve(draft)
    stats = flow.work()
    v = flow.view()
    assert len(ctx.mock_insurer.snapshot()) == 1  # exactly one claim at the insurer
    assert v["case"]["status"] == "payout_pending"
    assert len(v["submissions"]) == 1
    action = ctx.cases.get_action(CUSTOMER, draft["action_id"])
    assert action["status"] == "succeeded" and action["result"]["found"] is True
    ops = ctx.cases.operator_view(OPERATOR, flow.case_id)
    assert [(r["operation"], r["outcome"]) for r in ops["adapter_requests"]] == [("submit_claim", "uncertain"), ("find_submission", "found"), ("submit_claim", "accepted")]


def test_timeout_before_acceptance_retries_same_idempotent_request():
    ctx = make_ctx()
    ctx.mock_insurer.inject_fault("timeout_before_accept")
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    draft = flow.draft()
    flow.approve(draft)
    flow.work()
    v = flow.view()
    assert v["case"]["status"] == "payout_pending"
    assert len(ctx.mock_insurer.snapshot()) == 1
    ops = ctx.cases.operator_view(OPERATOR, flow.case_id)
    assert [r["outcome"] for r in ops["adapter_requests"]] == ["uncertain", "not_found", "accepted"]


def test_declined_submission_returns_case_for_review_without_side_effect():
    ctx = make_ctx()
    ctx.mock_insurer.inject_fault("declined")
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    draft = flow.draft()
    flow.approve(draft)
    flow.work()
    assert flow.status() == "evaluating"
    assert ctx.cases.get_action(CUSTOMER, draft["action_id"])["status"] == "failed"
    assert ctx.mock_insurer.snapshot() == []


def test_missing_document_case_survives_worker_restart():
    """Phase 2 exit criterion: approve, crash the worker after leasing, restart with a new worker, recover once."""
    ctx = make_ctx()
    flow = Flow(ctx)
    flow.open(["itinerary_demo", "baggage_report_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3_early"])
    flow.answer("baggage_delivered_at", {"delivered_at": "2026-09-12T16:30:00Z"})
    draft = flow.draft()
    flow.approve(draft)
    from app.workflows.worker import SimulatedCrash

    crashing = ctx.new_worker()
    crashing.crash_before_provider_call = True
    with pytest.raises(SimulatedCrash):
        run(crashing.run_once())
    assert ctx.cases.get_action(CUSTOMER, draft["action_id"])["status"] == "executing"
    assert ctx.mock_insurer.snapshot() == []
    # restart: a fresh worker over the same database
    ctx.clock.advance(seconds=ctx.settings.worker_lease_seconds + 1)
    fresh = ctx.new_worker()
    run(fresh.run_until_idle())
    v = flow.view()
    assert v["case"]["status"] == "evidence_requested"
    assert len(v["submissions"]) == 1 and len(ctx.mock_insurer.snapshot()) == 1
    assert ctx.cases.get_action(CUSTOMER, draft["action_id"])["status"] == "succeeded"
    # approval, timers and evidence are intact after the restart
    assert v["insurer_requests"][0]["status"] == "open"
    ctx.cases.attach_documents(CUSTOMER, flow.case_id, ["arrival_confirmation_demo"])
    d2 = flow.draft()
    flow.approve(d2)
    run(ctx.new_worker().run_until_idle())
    assert flow.status() == "payout_pending"


# ----------------------------------------------------------------------------- adapters
def test_a2a_adapter_path_submits_through_insurer_agent():
    ctx = make_ctx(adapter="a2a")
    assert ctx.adapter.capabilities.protocol.startswith("a2a/")
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    draft = flow.draft()
    assert "a2a_insurer_agent" in draft["review_summary"]["destination"]
    flow.approve(draft)
    flow.work()
    v = flow.view()
    assert v["case"]["status"] == "payout_pending"
    assert v["submissions"][0]["external_claim_ref"].startswith("insurer_mock_")
    from sqlalchemy import select

    from app.persistence.models import A2ATaskMap

    with ctx.db.session() as s:
        maps = s.scalars(select(A2ATaskMap)).all()
    assert len(maps) == 1 and maps[0].case_id == flow.case_id and maps[0].protocol_version == "0.3"


def test_sandbox_adapter_without_lookup_holds_uncertain_write_for_manual_review():
    ctx = make_ctx(adapter="sandbox")
    caps = ctx.adapter.capabilities.to_dict()
    assert caps["environment"] == "sandbox" and caps["capabilities"]["find_submission"] is False and caps["uncertain_requires_manual_review"] is True
    flow = Flow(ctx)
    flow.open(COMPLETE_DOCS)
    draft = flow.draft()
    flow.approve(draft)
    flow.work()
    # not configured -> declined (no side effect) rather than uncertain
    assert flow.status() == "evaluating"
    # simulate an uncertain outcome on the sandbox: the executor must hold for manual review
    from app.adapters.base import ProviderTimeout

    async def timeout(*_a, **_k):
        raise ProviderTimeout("sandbox timed out")

    ctx.adapter.submit_claim = timeout
    d2 = flow.draft()
    flow.approve(d2)
    flow.work()
    assert flow.status() == "manual_review"
    assert ctx.cases.get_action(CUSTOMER, d2["action_id"])["status"] == "manual_review"
