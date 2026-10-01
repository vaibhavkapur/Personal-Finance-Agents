from __future__ import annotations

from datetime import timedelta

import pytest

from backend.app.persistence.models import Action, Case, Customer
from backend.app.workflows import states as st
from backend.app.workflows.approvals import ApprovalError, approve_action, canonical_hash, invalidate_pending_authority, propose_action, verify_authority
from backend.app.workflows.states import IllegalTransitionError, StaleVersionError, transition


def _case(container):
    with container.db.session() as s:
        cust = s.get(Customer, "cus_demo_5")
        case = container.service.create_case(s, cust, "mortgage_demo_1", 48, None, ["offer_doc_a"])
        return case.id


def test_allowed_and_illegal_transitions(container):
    cid = _case(container)
    now = container.clock.now()
    with container.db.session() as s:
        case = s.get(Case, cid)
        transition(s, case, st.COMPARING, "system", now)
        transition(s, case, st.AWAITING_DECISION, "system", now)
        with pytest.raises(IllegalTransitionError):
            transition(s, case, st.MOCK_CLOSED, "system", now)
        with pytest.raises(IllegalTransitionError):
            transition(s, case, st.SUBMITTED, "system", now)
        assert case.version == 3


def test_model_actor_cannot_transition(container):
    cid = _case(container)
    with container.db.session() as s:
        case = s.get(Case, cid)
        with pytest.raises(IllegalTransitionError):
            transition(s, case, st.COMPARING, "model", container.clock.now())


def test_stale_version_is_rejected(container):
    cid = _case(container)
    with container.db.session() as s:
        case = s.get(Case, cid)
        with pytest.raises(StaleVersionError):
            transition(s, case, st.COMPARING, "system", container.clock.now(), expected_version=99)


def test_terminal_states_have_no_exits():
    for state in st.TERMINAL_STATES:
        assert st.ALLOWED_TRANSITIONS[state] == set()
    assert st.MANUAL_REVIEW not in st.TERMINAL_STATES
    assert st.MOCK_CLOSED not in st.ALLOWED_TRANSITIONS[st.MANUAL_REVIEW]


def test_every_transition_records_previous_next_actor_and_version(container):
    cid = _case(container)
    with container.db.session() as s:
        case = s.get(Case, cid)
        ev = transition(s, case, st.COMPARING, "system", container.clock.now())
        assert (ev.previous_state, ev.next_state, ev.actor, ev.expected_case_version) == (st.COLLECTING, st.COMPARING, "system", 1)
        events = container.service.timeline(s, case)
        assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))


# ------------------------------------------------------------------ approvals
def _propose(container, cid, key="k1", payload=None):
    with container.db.session() as s:
        case = s.get(Case, cid)
        action = propose_action(s, case, "send_negotiation", payload or {"lender_id": "lender_mock_a", "x": 1}, {"title": "t"}, key, container.clock.now(), 900)
        from backend.app.workflows.approvals import review_screen

        return review_screen(s, action, case)


def test_approval_binds_hash_version_and_challenge(container):
    cid = _case(container)
    screen = _propose(container, cid)
    with container.db.session() as s:
        case, action = s.get(Case, cid), s.get(Action, screen["action_id"])
        with pytest.raises(ApprovalError):
            approve_action(s, case, action, "cus_demo_5", case.version, "sha256:wrong", screen["approval_challenge_id"], container.clock.now(), 900)
        with pytest.raises(StaleVersionError):
            approve_action(s, case, action, "cus_demo_5", case.version + 1, screen["action_payload_hash"], screen["approval_challenge_id"], container.clock.now(), 900)
        with pytest.raises(ApprovalError):
            approve_action(s, case, action, "cus_demo_5", case.version, screen["action_payload_hash"], "challenge_unknown", container.clock.now(), 900)
        from backend.app.workflows.approvals import ForbiddenError

        with pytest.raises(ForbiddenError):
            approve_action(s, case, action, "cus_other_9", case.version, screen["action_payload_hash"], screen["approval_challenge_id"], container.clock.now(), 900)
        approval = approve_action(s, case, action, "cus_demo_5", case.version, screen["action_payload_hash"], screen["approval_challenge_id"], container.clock.now(), 900)
        assert approval.action_hash == action.payload_hash and action.status == "approved"
        # Challenge is single-use.
        with pytest.raises(ApprovalError):
            approve_action(s, case, action, "cus_demo_5", case.version, screen["action_payload_hash"], screen["approval_challenge_id"], container.clock.now(), 900)


def test_expired_challenge_is_rejected(container):
    cid = _case(container)
    screen = _propose(container, cid)
    container.clock.advance(seconds=901)
    with container.db.session() as s:
        case, action = s.get(Case, cid), s.get(Action, screen["action_id"])
        with pytest.raises(ApprovalError, match="expired"):
            approve_action(s, case, action, "cus_demo_5", case.version, screen["action_payload_hash"], screen["approval_challenge_id"], container.clock.now(), 900)


def test_idempotency_key_reuse_with_different_content_fails(container):
    cid = _case(container)
    first = _propose(container, cid, key="same", payload={"a": 1})
    again = _propose(container, cid, key="same", payload={"a": 1})
    assert first["action_id"] == again["action_id"]
    with pytest.raises(ApprovalError, match="idempotency"):
        _propose(container, cid, key="same", payload={"a": 2})


def test_material_change_invalidates_pending_authority(container):
    cid = _case(container)
    screen = _propose(container, cid)
    with container.db.session() as s:
        case, action = s.get(Case, cid), s.get(Action, screen["action_id"])
        approve_action(s, case, action, "cus_demo_5", case.version, screen["action_payload_hash"], screen["approval_challenge_id"], container.clock.now(), 900)
        n = invalidate_pending_authority(s, case, "inputs changed", container.clock.now())
        assert n == 1 and action.status == "invalidated"
        with pytest.raises(ApprovalError):
            verify_authority(s, action, case, container.clock.now())


def test_executor_reverifies_expiry_before_side_effect(container):
    cid = _case(container)
    screen = _propose(container, cid)
    with container.db.session() as s:
        case, action = s.get(Case, cid), s.get(Action, screen["action_id"])
        approve_action(s, case, action, "cus_demo_5", case.version, screen["action_payload_hash"], screen["approval_challenge_id"], container.clock.now(), 900)
    container.clock.advance(seconds=1000)
    with container.db.session() as s:
        case, action = s.get(Case, cid), s.get(Action, screen["action_id"])
        with pytest.raises(ApprovalError, match="expired"):
            verify_authority(s, action, case, container.clock.now())


def test_canonical_hash_is_order_independent():
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})
