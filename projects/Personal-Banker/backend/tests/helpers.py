"""Shared helpers to drive the journey through the HTTP API."""

from __future__ import annotations

from tests.conftest import CUSTOMER, OPERATOR


def create_case(client, buffer_minor=100000, obligation_ids=("bill_demo_1",), lockup_days=365, includes=False, **extra):
    body = {
        "customer_id": "cus_demo_1",
        "deposit_id": "cd_demo_1",
        "currency": "USD",
        "minimum_buffer_minor": buffer_minor,
        "obligation_ids": list(obligation_ids),
        "preferred_lockup_days": lockup_days,
        "buffer_includes_obligations": includes,
    }
    body.update(extra)
    r = client.post("/v1/banking-cases", json=body, headers=CUSTOMER)
    assert r.status_code == 201, r.text
    return r.json()


def evaluate(client, case_id):
    r = client.post(f"/v1/banking-cases/{case_id}/evaluate", headers=CUSTOMER)
    assert r.status_code == 200, r.text
    return r.json()


def prepare(client, case_id, option_id, amount_minor=None):
    body = {"option_id": option_id}
    if amount_minor is not None:
        body["amount_minor"] = amount_minor
    return client.post(f"/v1/banking-cases/{case_id}/instructions", json=body, headers=CUSTOMER)


def approve(client, action_id, expected_version=None, payload_hash=None):
    ch = client.post(f"/v1/actions/{action_id}/challenge", headers=CUSTOMER)
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    return client.post(
        f"/v1/actions/{action_id}/approve",
        json={
            "expected_case_version": expected_version if expected_version is not None else ch["expected_case_version"],
            "action_payload_hash": payload_hash or ch["action_payload_hash"],
            "approval_challenge_id": ch["approval_challenge_id"],
        },
        headers=CUSTOMER,
    )


def run_worker(client):
    r = client.post("/v1/operator/worker/run", headers=OPERATOR)
    assert r.status_code == 200, r.text
    return r.json()


def advance(client, days=0, hours=0, minutes=0):
    r = client.post("/v1/operator/clock/advance", json={"days": days, "hours": hours, "minutes": minutes}, headers=OPERATOR)
    assert r.status_code == 200, r.text
    return r.json()


def set_submit_mode(client, mode):
    r = client.post("/v1/operator/mock-bank/submit-mode", json={"mode": mode}, headers=OPERATOR)
    assert r.status_code == 200, r.text


def set_offer_mode(client, mode, offer_id="off_harbor_12m"):
    r = client.post("/v1/operator/mock-bank/offer-mode", json={"mode": mode, "offer_id": offer_id}, headers=OPERATOR)
    assert r.status_code == 200, r.text
    return r.json()


def case(client, case_id):
    r = client.get(f"/v1/banking-cases/{case_id}", headers=CUSTOMER)
    assert r.status_code == 200, r.text
    return r.json()


def timeline(client, case_id):
    r = client.get(f"/v1/banking-cases/{case_id}/timeline", headers=CUSTOMER)
    assert r.status_code == 200, r.text
    return r.json()


def ledger(client):
    r = client.get("/v1/operator/mock-bank/ledger", headers=OPERATOR)
    assert r.status_code == 200
    return r.json()


def approved_case(client, option_id="off_harbor_12m", **kwargs):
    """Create, evaluate, prepare and approve; returns (case_id, action_id)."""
    c = create_case(client, **kwargs)
    evaluate(client, c["id"])
    review = prepare(client, c["id"], option_id)
    assert review.status_code == 201, review.text
    review = review.json()
    r = approve(client, review["action_id"])
    assert r.status_code == 200, r.text
    return c["id"], review["action_id"]
