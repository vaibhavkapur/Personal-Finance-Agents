from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pytest
from app.domain.types import DomainError, uid
from app.persistence.store import Store as S
from app.workflows.engine import Engine
from conftest import approve, prepare, get_order, update_record


def test_full_fill_reconciles_exactly(engine):
    order = prepare(engine)
    before = engine.dashboard()["account"]["cash_minor"]
    assert not engine.broker.get_order_by_client_id(order["client_order_id"])
    approve(engine, order)
    engine.tick()
    result = get_order(engine, order)
    assert result["status"] == "filled"
    assert result["filled_quantity"] == order["quantity"]
    assert result["reservation"]["cash_minor"] == 0
    assert engine.dashboard()["account"]["cash_minor"] == before - order["quantity"] * (order["limit_price_minor"] + 1)
    assert engine.reconcile()["status"] == "matched"
    assert result["action"]["provider_reference"] == result["broker_order_id"]


@pytest.mark.parametrize("scenario,extra_fill", [("partial", 0), ("cancel_race", 1)])
def test_partial_then_cancel_keeps_owned_shares(engine, scenario, extra_fill):
    order = prepare(engine, scenario)
    approve(engine, order)
    engine.tick()
    partial = get_order(engine, order)
    assert partial["status"] == "partially_filled"
    assert partial["reservation"]["cash_minor"] == (order["quantity"] - partial["filled_quantity"]) * order["limit_price_minor"] + 100 - partial["fees_minor"]
    action = engine.cancel_draft(order["id"], "cancel")
    engine.tick()
    assert get_order(engine, order)["status"] == "partially_filled"  # No approval, no cancellation.
    approve(engine, partial, action)
    engine.tick()
    result = get_order(engine, order)
    assert result["status"] == "cancelled"
    assert result["filled_quantity"] == partial["filled_quantity"] + extra_fill
    assert result["reservation"]["cash_minor"] == 0
    assert engine.reconcile()["positions"]["AAPL"]["quantity"] == 20 + result["filled_quantity"]
    assert engine.reconcile()["status"] == "matched"


def test_concurrent_reservation_cannot_overspend_symbol_limit(engine):
    # Existing $4,569 + one $1,143.25 reservation fits; the second does not.
    update_record(engine, "trading_mandates", "mandate_v1", lambda m: m["limits"].update(per_symbol_minor=600000))
    def place(_):
        try:
            return prepare(engine, "resting")
        except DomainError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(place, range(8)))
    assert sum(r is not None for r in results) == 1
    assert engine.dashboard()["metrics"]["reserved_minor"] == 114325


def test_concurrent_approval_and_workers_submit_once(engine):
    order = prepare(engine, "resting")
    a = order["action"]
    def approve_once(_):
        return engine.approve(a["id"], order["version"], a["payload_hash"], a["challenge_id"], "same-approval")
    with ThreadPoolExecutor(max_workers=6) as pool:
        actions = list(pool.map(approve_once, range(6)))
        list(pool.map(lambda _: engine.tick(), range(6)))
    assert len({a["approval_id"] for a in actions}) == 1
    assert engine.broker.get_positions_and_cash()["order_count"] == 1
    assert get_order(engine, order)["status"] == "accepted"


def test_prepare_idempotency_binds_content(engine):
    s = engine.evaluate_run(["AAPL"])[0]
    p = engine.preview(s["id"])
    a = engine.prepare(s["id"], p["id"], "full", "key")
    b = engine.prepare(s["id"], p["id"], "full", "key")
    assert a["id"] == b["id"]
    with pytest.raises(DomainError, match="different content"):
        engine.prepare(s["id"], p["id"], "partial", "key")
    with pytest.raises(DomainError, match="already has an order"):
        engine.prepare(s["id"], p["id"], "full", "another-key")


def test_duplicate_execution_is_not_added_twice(engine):
    order = prepare(engine, "partial")
    approve(engine, order)
    engine.tick()
    report = engine.broker.get_order_by_client_id(order["client_order_id"])
    baseline = engine.dashboard()["account"]
    for _ in range(5):
        engine.apply_report(order["id"], report)
    assert engine.dashboard()["account"] == baseline
    assert len(get_order(engine, order)["fills"]) == 1


def test_conflicting_execution_evidence_is_rejected(engine):
    order = prepare(engine, "partial")
    approve(engine, order)
    engine.tick()
    report = engine.broker.get_order_by_client_id(order["client_order_id"])
    report["fills"][0]["price_minor"] -= 1
    with pytest.raises(DomainError, match="different content"):
        engine.apply_report(order["id"], report)


def test_accepted_before_timeout_retains_reservation_and_recovers_on_restart(engine):
    order = prepare(engine, "timeout")
    approve(engine, order)
    engine.tick()
    unknown = get_order(engine, order)
    assert unknown["status"] == "outcome_unknown"
    assert unknown["reservation"]["cash_minor"] == order["reservation"]["cash_minor"]
    restarted = Engine(engine.store.path.rsplit("/", 1)[0])
    with restarted.store.tx() as db:
        db.execute("UPDATE jobs SET lease_until=0")
    restarted.tick()
    assert get_order(restarted, order)["status"] == "filled"
    assert restarted.broker.get_positions_and_cash()["order_count"] == 1
    assert restarted.reconcile()["status"] == "matched"


def test_worker_crash_after_claim_is_recovered(engine):
    order = prepare(engine)
    approve(engine, order)
    job = engine.claim_job()
    assert job
    assert engine.claim_job() is None
    with engine.store.tx() as db:
        db.execute("UPDATE jobs SET lease_until=0")
    restarted = Engine(engine.store.path.rsplit("/", 1)[0])
    restarted.tick()
    assert get_order(restarted, order)["status"] == "filled"


def test_worker_crash_after_provider_acceptance_query_prevents_resubmit(engine, monkeypatch):
    order = prepare(engine, "full")
    approve(engine, order)
    original = engine.apply_report
    def crash(*args, **kwargs):
        raise RuntimeError("process died before projection commit")
    monkeypatch.setattr(engine, "apply_report", crash)
    with pytest.raises(RuntimeError):
        engine.tick()
    assert engine.broker.get_positions_and_cash()["order_count"] == 1
    monkeypatch.setattr(engine, "apply_report", original)
    with engine.store.tx() as db:
        db.execute("UPDATE jobs SET lease_until=0")
    engine.tick()
    assert engine.broker.get_positions_and_cash()["order_count"] == 1
    assert get_order(engine, order)["status"] == "filled"


@pytest.mark.parametrize("change", ["revoke", "stale", "kill", "price", "tamper", "approval_revoke"])
def test_authority_rechecked_immediately_before_write(engine, change):
    order = prepare(engine)
    action = approve(engine, order)
    if change == "revoke":
        engine.revoke_mandate("mandate_v1")
    elif change == "stale":
        engine.advance_clock(121, False)
    elif change == "kill":
        engine.set_kill(True)
    elif change == "price":
        with engine.store.tx() as db:
            q = S.get(db, "market_snapshots", "snapshot_AAPL")
            q.update(id="new-price", ask_minor=25000)
            S.put(db, "market_snapshots", q)
    elif change == "tamper":
        update_record(engine, "orders", order["id"], lambda o: o.update(quantity=o["quantity"] + 1))
    else:
        update_record(engine, "approvals", action["approval_id"], lambda a: a.update(revoked_at="2026-09-25T14:00:00Z"))
    engine.tick()
    assert engine.broker.get_positions_and_cash()["order_count"] == 0
    assert get_order(engine, order)["status"] == "expired"
    assert get_order(engine, order)["reservation"]["cash_minor"] == 0


def test_revoked_mandate_can_cancel_existing_order(engine):
    order = prepare(engine, "resting")
    approve(engine, order)
    engine.tick()
    engine.revoke_mandate("mandate_v1")
    engine.set_kill(True)
    accepted = get_order(engine, order)
    action = engine.cancel_draft(order["id"], "cancel")
    approve(engine, accepted, action)
    engine.tick()
    assert get_order(engine, order)["status"] == "cancelled"


def test_malformed_response_requires_review_and_is_recoverable(engine):
    order = prepare(engine, "malformed")
    approve(engine, order)
    engine.tick()
    assert get_order(engine, order)["status"] == "manual_review"
    assert engine.dashboard()["account"]["reconciliation_status"] == "review_required"
    assert engine.reconcile()["status"] == "matched"
    assert get_order(engine, order)["status"] == "accepted"
    assert get_order(engine, order)["action"]["status"] == "completed"


def test_out_of_order_callback_resolves_current_broker_truth(engine):
    order = prepare(engine, "cancel_race")
    approve(engine, order)
    engine.tick()
    old_report = engine.broker.get_order_by_client_id(order["client_order_id"])
    a = engine.cancel_draft(order["id"], "cancel")
    approve(engine, get_order(engine, order), a)
    engine.tick()
    before = engine.dashboard()["account"]
    with pytest.raises(DomainError, match="omitted"):
        engine.apply_report(order["id"], old_report)
    assert engine.dashboard()["account"] == before
    assert engine.reconcile()["status"] == "matched"


def test_rejection_and_unapproved_draft_release_reservations(engine):
    rejected = prepare(engine, "rejected")
    approve(engine, rejected)
    engine.tick()
    assert get_order(engine, rejected)["status"] == "rejected"
    assert get_order(engine, rejected)["reservation"]["cash_minor"] == 0
    draft = prepare(engine)
    engine.discard(draft["id"])
    assert get_order(engine, draft)["reservation"]["cash_minor"] == 0


def test_cancel_fill_race_invalidates_terms_before_execution(engine):
    order = prepare(engine, "delayed")
    approve(engine, order)
    engine.tick()
    accepted = get_order(engine, order)
    a = engine.cancel_draft(order["id"], "cancel")
    approve(engine, accepted, a)
    engine.advance_clock(1, True)
    engine.tick()
    result = get_order(engine, order)
    assert result["status"] == "filled"
    assert result["cancel_action"]["status"] == "expired"
    assert engine.reconcile()["status"] == "matched"


def test_broker_overfill_rolls_back_projection(engine):
    order = prepare(engine, "resting")
    approve(engine, order)
    engine.tick()
    report = engine.broker.get_order_by_client_id(order["client_order_id"])
    report["status"] = "filled"
    report["fills"] = [{"broker_execution_id": "bad-fill", "quantity": order["quantity"] + 1, "price_minor": order["limit_price_minor"], "fee_minor": 1, "executed_at": "2026-09-25T14:00:00Z"}]
    with pytest.raises(DomainError, match="inconsistent"):
        engine.apply_report(order["id"], report)
    assert not get_order(engine, order)["fills"]


def test_cross_tenant_access_hidden(engine):
    order = prepare(engine)
    with engine.store.tx() as db:
        with pytest.raises(DomainError) as error:
            engine.order_detail(db, order["id"], "other-tenant")
    assert error.value.status == 404


def test_provider_calls_do_not_hold_projection_transaction(engine, monkeypatch):
    order = prepare(engine)
    approve(engine, order)
    submit = engine.broker.submit_order
    def outside_transaction(payload, client_id):
        db = engine.store.connect()
        db.execute("PRAGMA busy_timeout=1")
        db.execute("BEGIN IMMEDIATE")
        db.rollback()
        db.close()
        return submit(payload, client_id)
    monkeypatch.setattr(engine.broker, "submit_order", outside_transaction)
    engine.tick()
    assert get_order(engine, order)["status"] == "filled"
