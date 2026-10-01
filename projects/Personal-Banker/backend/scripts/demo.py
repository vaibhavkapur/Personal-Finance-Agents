"""One documented end-to-end demo command (plan §16, §20).

Runs the three demo scenarios against a fresh in-process database and prints
what happened at each step, including the evidence references:

  1. Maturity handled: $3,000 reserved, $7,000 renewed, bank confirms.
  2. Offer changes: the approved rate is re-issued; approval is invalidated and
     the customer must approve the revised terms.
  3. Uncertain transfer: the bank accepts then disconnects; the worker
     reconciles by the original request reference and creates no duplicate.

Usage (from backend/):
    python scripts/demo.py            # all three
    python scripts/demo.py --scenario 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PB_SERVE_FRONTEND", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.agent.orchestrator import run_turn  # noqa: E402
from app.domain.money import format_minor  # noqa: E402
from app.persistence import db  # noqa: E402
from app.persistence.seed import seed  # noqa: E402

CUSTOMER = {"X-Customer-Id": "cus_demo_1"}
OPERATOR = {"X-Customer-Id": "ops_demo", "X-Role": "operator"}
STORY = "My $10,000 CD matures next week. Keep $3,000 available for my upcoming expenses and compare what I can do with the rest."


def say(step: str, detail: str = "") -> None:
    print(f"\n== {step}")
    if detail:
        print(detail)


class Demo:
    def __init__(self, client: TestClient):
        self.c = client
        self.case_id: str | None = None

    def turn(self, message: str) -> dict:
        out = asyncio.run(run_turn("cus_demo_1", message, case_id=self.case_id))
        self.case_id = out.case_id or self.case_id
        print(f"\ncustomer> {message}")
        print(f"agent   > {out.reply}")
        print(f"          [tools: {', '.join(c['tool'] for c in out.tool_calls) or 'none'} | state: {out.state}]")
        return out.to_dict()

    def case(self) -> dict:
        return self.c.get(f"/v1/banking-cases/{self.case_id}", headers=CUSTOMER).json()

    def approve(self) -> None:
        detail = self.case()
        action_id = detail["current_action_id"]
        review = detail["review"]
        say("Customer reviews the exact instruction", json.dumps({k: review[k] for k in ("amount_display", "irreversible_effect", "action_payload_hash")}, indent=2))
        ch = self.c.post(f"/v1/actions/{action_id}/challenge", headers=CUSTOMER).json()
        r = self.c.post(
            f"/v1/actions/{action_id}/approve",
            json={"expected_case_version": ch["expected_case_version"], "action_payload_hash": ch["action_payload_hash"], "approval_challenge_id": ch["approval_challenge_id"]},
            headers=CUSTOMER,
        )
        r.raise_for_status()
        print(f"approved: approval {r.json()['approval_id']} bound to {ch['action_payload_hash'][:24]}… expires {r.json()['expires_at']}")

    def worker(self) -> None:
        out = self.c.post("/v1/operator/worker/run", headers=OPERATOR).json()
        for r in out["results"]:
            print(f"worker  > {r['type']}: {r['outcome']}")

    def advance(self, days: int) -> None:
        out = self.c.post("/v1/operator/clock/advance", json={"days": days}, headers=OPERATOR).json()
        print(f"clock   > now {out['now']}; callbacks delivered: {len(out['callbacks_delivered'])}")

    def mode(self, kind: str, mode: str) -> None:
        path = "/v1/operator/mock-bank/submit-mode" if kind == "submit" else "/v1/operator/mock-bank/offer-mode"
        self.c.post(path, json={"mode": mode}, headers=OPERATOR).raise_for_status()
        print(f"sim     > {kind} mode = {mode}")

    def show_outcome(self) -> None:
        d = self.case()
        tl = self.c.get(f"/v1/banking-cases/{self.case_id}/timeline", headers=CUSTOMER).json()
        submits = [r for r in tl["provider_requests"] if r["operation"] == "submit_instruction"]
        ledger = self.c.get("/v1/operator/mock-bank/ledger", headers=OPERATOR).json()
        print(f"\ncase {d['id']} state={d['state']} version={d['version']}")
        if d.get("review_reason"):
            print(f"reason: {d['review_reason']}")
        if d.get("instruction"):
            i = d["instruction"]
            print(f"instruction {i['status']}: {format_minor(i['amount_minor'])} {i['instruction_type']} effective {i['effective_at']} bank ref {i['external_ref']}")
            if i.get("reconciliation"):
                ok = sum(1 for c in i["reconciliation"]["checks"] if c["ok"])
                print(f"reconciliation matched={i['reconciliation']['matched']} ({ok}/{len(i['reconciliation']['checks'])} checks)")
        print(f"completion evidence: {d.get('completion_evidence_ref')}")
        print(f"provider submits: {len(submits)}; instructions at bank: {len(ledger['instructions'])}")
        print("bank ledger: " + ", ".join(f"{a['id']}={format_minor(a['available_minor'])} avail" for a in ledger["accounts"]))


def fresh_client(tmp: str) -> TestClient:
    db.configure(f"sqlite:///{Path(tmp) / 'demo.db'}")
    db.create_schema()
    with db.session_scope() as session:
        seed(session)
    from app.main import create_app

    client = TestClient(create_app())
    client.__enter__()
    return client


def scenario_1(d: Demo) -> None:
    say("Demo 1: maturity handled")
    d.turn(STORY)
    d.turn("Yes, the $3,000 includes the rent. I can lock money up for a year.")
    d.turn("Renew into the 12-month CD.")
    d.turn("Approve it now.")  # refused: the agent cannot approve
    d.approve()
    d.worker()
    d.turn("What's the status?")
    d.advance(7)
    d.worker()
    d.turn("Is it done?")
    d.show_outcome()


def scenario_2(d: Demo) -> None:
    say("Demo 2: offer changes before execution")
    d.turn(STORY)
    d.turn("Yes, includes the rent. A year is fine.")
    d.turn("12-month renewal.")
    d.approve()
    d.mode("offer", "changed_rate")
    d.worker()
    d.turn("Did it go through?")
    d.turn("Show me the revised offers.")
    d.turn("Fine, the revised 12-month renewal.")
    d.approve()
    d.worker()
    d.advance(7)
    d.worker()
    d.turn("Status?")
    d.show_outcome()


def scenario_3(d: Demo) -> None:
    say("Demo 3: uncertain transfer")
    d.turn(STORY)
    d.turn("Yes, includes the rent. Keep it liquid.")
    d.turn("Move the rest to my Northwind savings.")
    d.approve()
    d.mode("submit", "accepted_before_timeout")
    d.worker()
    d.turn("Did the transfer go through?")
    d.advance(7)
    d.worker()
    d.turn("Status?")
    d.show_outcome()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", type=int, choices=[1, 2, 3], default=None)
    args = parser.parse_args()
    scenarios = {1: scenario_1, 2: scenario_2, 3: scenario_3}
    selected = [args.scenario] if args.scenario else [1, 2, 3]
    for n in selected:
        with tempfile.TemporaryDirectory() as tmp:
            client = fresh_client(tmp)
            try:
                scenarios[n](Demo(client))
            finally:
                client.__exit__(None, None, None)
                db.get_engine().dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
