"""End-to-end demo: runs the three documented scenarios against the in-process
API with the fixture clock and mock lender network, printing a transcript.

Usage: python scripts/demo.py [--scenario 1|2|3|all] [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api.main import create_app  # noqa: E402
from backend.app.clock import FixtureClock, parse_iso  # noqa: E402
from backend.app.config import Settings  # noqa: E402
from backend.app.container import Container  # noqa: E402
from backend.app.domain.money import format_minor  # noqa: E402

BORROWER = {"Authorization": "Bearer demo-borrower-token"}
OPERATOR = {"Authorization": "Bearer demo-operator-token"}


class Demo:
    def __init__(self, quiet_json: bool = False):
        settings = Settings()
        settings.database_url = "sqlite:///:memory:"
        self.container = Container(settings=settings, clock=FixtureClock(parse_iso("2026-09-26T12:00:00+00:00")))
        self.container.seed()
        self.client = TestClient(create_app(self.container, run_worker=False))
        self.quiet = quiet_json
        self.log: list = []

    # ------------------------------------------------------------ plumbing
    def say(self, text: str) -> None:
        self.log.append(text)
        if not self.quiet:
            print(text)

    def drain(self) -> None:
        asyncio.get_event_loop().run_until_complete(self.container.worker.drain())

    def view(self, cid: str) -> Dict[str, Any]:
        return self.client.get(f"/v1/loan-cases/{cid}", headers=BORROWER).json()

    def chat(self, cid: str, text: str, **params: Any) -> Dict[str, Any]:
        self.say(f"\n  borrower> {text}")
        r = self.client.post(f"/v1/loan-cases/{cid}/messages", json={"message": text, "params": params}, headers=BORROWER).json()
        self.say("  agent> " + r["reply"].replace("\n", "\n         "))
        if r.get("tool_calls"):
            self.say("  [tools: " + ", ".join(f"{t['tool']}({t['authority']})" for t in r["tool_calls"]) + "]")
        return r

    def approve(self, screen: Dict[str, Any]) -> None:
        rv = screen["review"]
        self.say(f"  [review screen] {rv.get('title')} -> {rv.get('destination', {}).get('lender_name')} ({rv.get('destination', {}).get('environment')}); docs={[d['id'] for d in rv.get('documents_shared', [])]}; hash={screen['action_payload_hash'][:19]}...; case v{screen['expected_case_version']}")
        r = self.client.post(f"/v1/actions/{screen['action_id']}/approve", json={"expected_case_version": screen["expected_case_version"], "action_payload_hash": screen["action_payload_hash"], "approval_challenge_id": screen["approval_challenge_id"]}, headers=BORROWER)
        self.say(f"  [borrower approves exactly this action] -> {r.json().get('status', r.text)}; executor runs in background")
        self.drain()

    def new_case(self, horizon: int, docs, max_cash=600_000) -> str:
        r = self.client.post("/v1/loan-cases", json={"customer_id": "cus_demo_5", "mortgage_id": "mortgage_demo_1", "holding_horizon_months": horizon, "maximum_cash_to_close_minor": max_cash, "offer_document_ids": list(docs)}, headers=BORROWER)
        body = r.json()
        self.say(f"  POST /v1/loan-cases -> {body['id']} status={body['status']} v{body['version']} missing={body['missing_fields']}")
        return body["id"]

    def show_state(self, cid: str) -> None:
        v = self.view(cid)
        self.say(f"  [case {cid}] state={v['status']} v{v['version']} offers=" + ", ".join(f"{o['lender_id']}@v{o['version']}:{o['status']}" for o in v["offers"]))

    # ------------------------------------------------------------ scenarios
    def demo_1(self) -> Dict[str, Any]:
        self.say("\n=== Demo 1: Keep the current loan (horizon too short to recover costs) ===")
        cid = self.new_case(18, ["offer_doc_a", "offer_doc_b"])
        self.chat(cid, "Is refinancing worth it if I only stay another 18 months?")
        self.chat(cid, "Yes, the balance is correct and my payment includes escrow.")
        self.chat(cid, "OK, I'll keep my current loan.")
        v = self.view(cid)
        self.show_state(cid)
        return {"case_id": cid, "state": v["status"], "decision": v["comparison"]["recommendation"]["decision"], "evidence": v["completion_evidence_ref"]}

    def demo_2(self) -> Dict[str, Any]:
        self.say("\n=== Demo 2: Negotiate comparable terms with a competing offer ===")
        cid = self.new_case(48, ["offer_doc_a", "offer_doc_b", "offer_doc_c"])
        self.chat(cid, "Balance is correct and my payment includes escrow. I expect to stay four more years.")
        r = self.chat(cid, "Ask my lender whether it can offer better terms.")
        self.approve(r["review_screen"])
        v = self.view(cid)
        self.say(f"  lender reply: {v['lender_requests'][-1]['response'].get('outcome')} - {v['lender_requests'][-1]['response'].get('reason')}")
        r = self.chat(cid, "Ask Harbor to match Northstar.")
        self.approve(r["review_screen"])
        v = self.view(cid)
        self.say(f"  lender reply: {v['lender_requests'][-1]['response'].get('outcome')} - {v['lender_requests'][-1]['response'].get('reason')}")
        self.show_state(cid)
        self.chat(cid, "What does the comparison look like now?")
        rec = v["comparison"]["recommendation"]
        return {"case_id": cid, "state": v["status"], "best_offer": rec["best_offer_id"], "harbor_versions": [o["version"] for o in v["offers"] if o["lender_id"] == "lender_mock_b"], "reason": rec["reason"]}

    def demo_3(self) -> Dict[str, Any]:
        self.say("\n=== Demo 3: Review final changes before mock closing ===")
        cid = self.new_case(48, ["offer_doc_a", "offer_doc_b"])
        self.chat(cid, "Balance is correct, payment includes escrow, horizon 48 months.")
        r = self.chat(cid, "Ask Harbor to match Northstar.")
        self.approve(r["review_screen"])
        self.chat(cid, "Roll the closing costs into the loan.")
        r = self.chat(cid, "Apply with Harbor.")
        self.approve(r["review_screen"])
        self.show_state(cid)
        self.say("  [clock] +3 days; worker polls the application by its original reference")
        self.container.clock.advance(days=3)
        self.client.post("/v1/ops/mock/deliver-callbacks", headers=OPERATOR)
        self.drain()
        v = self.view(cid)
        review = v["term_reviews"][-1]["differences"]
        self.say(f"  final-term review: {review['summary']}")
        for d in review["differences"]:
            if d["material"]:
                self.say(f"    - {d['field']}: {d['earlier']} -> {d['final']}")
        r = self.chat(cid, "Accept the final terms and close.")
        self.approve(r["review_screen"])
        v = self.view(cid)
        self.show_state(cid)
        app = v["applications"][0]
        self.say(f"  closing evidence: {app['closing_evidence']['closing_record_id']} payoff={app['closing_evidence']['payoff_record_id']} funded={app['is_funded']}")
        return {"case_id": cid, "state": v["status"], "requires_reapproval": v["term_reviews"][-1]["requires_reapproval"], "closing_record_id": app["closing_evidence"]["closing_record_id"], "is_funded": app["is_funded"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="all", choices=["1", "2", "3", "all"])
    parser.add_argument("--json", action="store_true", help="print only the JSON summary")
    args = parser.parse_args()
    demo = Demo(quiet_json=args.json)
    results: Dict[str, Any] = {}
    if args.scenario in ("1", "all"):
        results["demo_1"] = demo.demo_1()
    if args.scenario in ("2", "all"):
        results["demo_2"] = demo.demo_2()
    if args.scenario in ("3", "all"):
        results["demo_3"] = demo.demo_3()
    metrics = demo.client.get("/v1/ops/metrics", headers=OPERATOR).json()
    results["metrics"] = {k: metrics[k] for k in ("cases_by_state", "tool_errors", "duplicate_actions_prevented", "approvals_abandoned_or_invalidated")}
    print("\n" + json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
