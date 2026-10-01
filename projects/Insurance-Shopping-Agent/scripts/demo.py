"""One documented end-to-end demo command.

Runs the three demo scenarios from the plan against either an in-process app (default,
no server needed) or a running API (--base-url). Prints a transcript that shows each
milestone as separate evidence: quoted, selected, approved, submitted, bound, issued,
verified, and the coverage start date.

  python scripts/demo.py                     # in-process, direct adapters
  python scripts/demo.py --mode a2a          # in-process A2A insurer agents (ASGI)
  python scripts/demo.py --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.fixtures import household_by_customer  # noqa: E402

TOKENS = {"cus_demo_1": "tok_cus_demo_1", "cus_demo_2": "tok_cus_demo_2", "cus_demo_3": "tok_cus_demo_3", "operator": "tok_operator"}
QUESTION_FACTS = {
    "nw_q_dog": "owns_dog", "nw_q_claims": "prior_claims_5y", "nw_q_smoke": "smoke_detectors", "nw_q_home_business": "home_business",
    "hl_q_animals": "animals_in_household", "hl_q_building": "building_type", "hl_q_claims": "prior_claims_3y",
    "cp_q_high_value": "high_value_items_over_1500", "cp_q_claims": "prior_claims_5y",
}


def say(text: str = "") -> None:
    print(text)


class Demo:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def call(self, method: str, path: str, token: str, expect: int = 200, **kwargs) -> Any:
        r = await self.client.request(method, path, headers={"Authorization": "Bearer " + TOKENS[token]}, **kwargs)
        if r.status_code != expect:
            raise SystemExit("%s %s -> %d %s" % (method, path, r.status_code, r.text))
        return r.json() if r.content else None

    async def chat(self, case_id: str, customer: str, text: str) -> Dict[str, Any]:
        say("  customer> %s" % text)
        turn = await self.call("POST", "/v1/insurance-shopping-cases/%s/messages" % case_id, customer, json={"text": text})
        for line in turn["reply"].splitlines():
            say("  agent   > %s" % line)
        say("  [tools: %s]" % ", ".join(c["name"] for c in turn["tool_calls"]))
        return turn

    async def worker(self) -> Dict[str, Any]:
        return await self.call("POST", "/v1/operator/worker/run-once", "operator")

    async def advance(self, **kwargs) -> None:
        r = await self.call("POST", "/v1/operator/clock/advance", "operator", json=kwargs)
        say("  [fixture clock advanced to %s]" % r["now"])

    def facts(self, customer: str) -> List[Dict[str, Any]]:
        hh = household_by_customer(customer)
        return [{"question_id": q, "value": hh["building_type"] if f == "building_type" else hh["facts"][f]} for q, f in QUESTION_FACTS.items()]

    async def settle(self, case_id: str, customer: str, hours: int) -> Dict[str, Any]:
        await self.worker()
        view = await self.call("GET", "/v1/insurance-shopping-cases/%s" % case_id, customer)
        if view["status"] in ("submitted", "underwriting"):
            await self.advance(hours=hours)
            await self.worker()
            view = await self.call("GET", "/v1/insurance-shopping-cases/%s" % case_id, customer)
        return view

    def print_timeline(self, view: Dict[str, Any]) -> None:
        say("  timeline:")
        for t in view["timeline"]:
            say("    %-28s %s  (state: %s)" % (t["label"], t["at"][:19], t["state"] or "-"))
        if view.get("policy"):
            p = view["policy"]
            say("  policy %s: %s; verified=%s; effective %s..%s" % (p["insurer_policy_ref"], p["coverage_label"], p["verified"], p["effective_at"], p["expires_at"]))

    async def approve_pending(self, case_id: str, customer: str) -> None:
        view = await self.call("GET", "/v1/insurance-shopping-cases/%s" % case_id, customer)
        action = view["pending_action"]
        review = action["review"]
        say("  REVIEW SCREEN -> %s (%s, %s) | %s | property %s, liability %s, deductible %s, start %s" % (
            review["destination"]["insurer_name"], review["destination"]["environment"], review["destination"]["protocol"], review["amount"]["display"],
            review["terms"]["property_limit"], review["terms"]["liability_limit"], review["terms"]["deductible"], review["terms"]["effective_date"]))
        for effect in review["irreversible_effects"]:
            say("    ! %s" % effect)
        say("    answers sent: %s" % ", ".join("%s=%s" % (a["question_id"], a["value"]) for a in review["answers"]))
        say("    payload hash: %s" % action["payload_hash"])
        result = await self.call("POST", "/v1/actions/%s/approve" % action["action_id"], customer,
                                 json={"expected_case_version": view["version"], "action_payload_hash": action["payload_hash"], "approval_challenge_id": action["challenge_id"]})
        say("  customer approved (approval %s, expires %s)" % (result["approval_id"], result["expires_at"]))

    async def demo1_and_2(self) -> None:
        customer = "cus_demo_1"
        hh = household_by_customer(customer)
        say("=" * 78)
        say("DEMO 1 + 2: comparable shopping with an insurer follow-up question (%s)" % hh["display_name"])
        say("=" * 78)
        created = await self.call("POST", "/v1/insurance-shopping-cases", customer, 201, json={
            "state_code": "CA", "product": "renters", "desired_effective_date": "2026-11-01",
            "property_limit_minor": 3000000, "liability_limit_minor": 10000000, "replacement_cost_required": True})
        case_id = created["id"]
        say("  case %s created; missing: %s" % (case_id, created["missing_fields"]))
        await self.chat(case_id, customer, "Find renters insurance that covers replacing my belongings, includes liability protection and starts when I move next month.")
        await self.call("POST", "/v1/insurance-shopping-cases/%s/answers" % case_id, customer, json={"answers": [
            {"field": "address", "value": hh["address"]}, {"field": "deductible_cap_minor", "value": 100000},
            {"field": "required_item_classes", "value": ["jewelry", "bicycles"]}, {"field": "deductible_preference", "value": "lower_premium"}]})
        say("  [interview form submitted: address, deductible cap $1,000, must cover jewelry + bicycles]")
        await self.chat(case_id, customer, "That's everything. Please get quotes.")
        say("  [customer answers Cedar & Pine's question: yes]")
        await self.call("POST", "/v1/insurance-shopping-cases/%s/answers" % case_id, customer, json={"answers": [{"question_id": "cp_q_high_value", "value": True}]})
        turn = await self.chat(case_id, customer, "Yes, I do. What are my options?")
        comparison = await self.call("GET", "/v1/insurance-shopping-cases/%s/comparison" % case_id, customer)
        say("  comparison: suitable=%s excluded=%s complete=%s" % ([s["insurer_id"] for s in comparison["suitable"]], [e["insurer_id"] for e in comparison["excluded"]], comparison["complete"]))
        say("  [customer answers Northwind's and Harborline's remaining questions from the form]")
        await self.call("POST", "/v1/insurance-shopping-cases/%s/answers" % case_id, customer, json={"answers": self.facts(customer)})
        a_quote = next(s["quote_id"] for s in comparison["suitable"] if s["insurer_id"] == "ins_northwind_a")
        await self.chat(case_id, customer, "select %s" % a_quote)
        await self.approve_pending(case_id, customer)
        await self.worker()
        view = await self.call("GET", "/v1/insurance-shopping-cases/%s" % case_id, customer)
        say("  after executor: state=%s (submitted is not issued; policy=%s)" % (view["status"], view["policy"]))
        view = await self.settle(case_id, customer, hours=2)
        await self.chat(case_id, customer, "Is my policy active?")
        self.print_timeline(view)
        say("  FINAL STATE: %s" % view["status"])

    async def demo3(self) -> None:
        customer = "cus_demo_2"
        hh = household_by_customer(customer)
        say("")
        say("=" * 78)
        say("DEMO 3: underwriting changes the premium; customer reviews new terms before binding (%s)" % hh["display_name"])
        say("=" * 78)
        created = await self.call("POST", "/v1/insurance-shopping-cases", customer, 201, json={
            "state_code": "CA", "product": "renters", "desired_effective_date": "2026-11-01",
            "property_limit_minor": 3000000, "liability_limit_minor": 10000000, "replacement_cost_required": True,
            "address": hh["address"], "deductible_cap_minor": 100000, "required_item_classes": ["jewelry"], "deductible_preference": "lower_premium"})
        case_id = created["id"]
        await self.call("POST", "/v1/insurance-shopping-cases/%s/quote-requests" % case_id, customer, 202)
        await self.call("POST", "/v1/insurance-shopping-cases/%s/answers" % case_id, customer, json={"answers": self.facts(customer)})
        comparison = await self.call("GET", "/v1/insurance-shopping-cases/%s/comparison" % case_id, customer)
        a_quote = next(s for s in comparison["suitable"] if s["insurer_id"] == "ins_northwind_a")
        say("  quoted by Northwind at %s; customer prepares an application" % a_quote["annual_premium_display"])
        await self.call("POST", "/v1/insurance-shopping-cases/%s/applications" % case_id, customer, 201, json={"quote_id": a_quote["quote_id"]})
        await self.approve_pending(case_id, customer)
        await self.worker()
        await self.advance(hours=2)
        await self.worker()
        view = await self.call("GET", "/v1/insurance-shopping-cases/%s" % case_id, customer)
        say("  underwriting result: state=%s; pending action=%s" % (view["status"], view["pending_action"]["type"]))
        review = view["pending_action"]["review"]
        say("  REVISED OFFER: %s -> %s (%s). Reason: %s" % (review["previous_premium_display"], review["amount"]["display"], review["premium_change_display"], review["revision_reason"]))
        await self.chat(case_id, customer, "What happened to my application?")
        await self.approve_pending(case_id, customer)
        await self.worker()
        view = await self.settle(case_id, customer, hours=1)
        self.print_timeline(view)
        say("  FINAL STATE: %s (premium on declarations: $%.2f)" % (view["status"], view["policy"]["declarations"]["annual_premium_minor"] / 100))

    async def mismatch_demo(self) -> None:
        customer = "cus_demo_1"
        hh = household_by_customer(customer)
        say("")
        say("=" * 78)
        say("EXTRA: issued policy that does not match the approved start date is never presented as active")
        say("=" * 78)
        created = await self.call("POST", "/v1/insurance-shopping-cases", customer, 201, json={
            "state_code": "CA", "product": "renters", "desired_effective_date": "2026-11-01", "property_limit_minor": 3000000, "liability_limit_minor": 10000000,
            "replacement_cost_required": True, "address": hh["address"], "deductible_cap_minor": 100000, "required_item_classes": ["jewelry"]})
        case_id = created["id"]
        await self.call("POST", "/v1/insurance-shopping-cases/%s/quote-requests" % case_id, customer, 202)
        await self.call("POST", "/v1/insurance-shopping-cases/%s/answers" % case_id, customer, json={"answers": self.facts(customer)})
        r = await self.client.post("/v1/operator/faults", headers={"Authorization": "Bearer tok_operator"},
                                   json={"insurer_id": "ins_northwind_a", "kind": "effective_date_shift_days", "operation": "submit_application", "params": {"days": 7}})
        if r.status_code != 200:
            say("  (fault injection unavailable in this mode: %s) - skipping" % r.text)
            return
        comparison = await self.call("GET", "/v1/insurance-shopping-cases/%s/comparison" % case_id, customer)
        a_quote = next(s for s in comparison["suitable"] if s["insurer_id"] == "ins_northwind_a")
        await self.call("POST", "/v1/insurance-shopping-cases/%s/applications" % case_id, customer, 201, json={"quote_id": a_quote["quote_id"]})
        await self.approve_pending(case_id, customer)
        await self.worker()
        view = await self.settle(case_id, customer, hours=2)
        say("  FINAL STATE: %s" % view["status"])
        say("  verification: %s" % json.dumps(view["policy"]["verification"]["mismatches"]))
        await self.chat(case_id, customer, "Am I covered?")


async def run(base_url: Optional[str], mode: str) -> None:
    if base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
            demo = Demo(client)
            await demo.demo1_and_2()
            await demo.demo3()
            await demo.mismatch_demo()
        return
    from app.clock import FixtureClock
    from app.config import Settings
    from app.context import AppContext
    from app.main import create_app

    with tempfile.TemporaryDirectory() as tmp:
        clock = FixtureClock(datetime(2026, 10, 1, 9, tzinfo=timezone.utc), frozen=True)
        settings = Settings(database_url="sqlite:///%s/demo.db" % tmp, fixture_clock=True, environment="mock", adapter_mode=mode)
        registry = None
        if mode == "a2a":
            from app.adapters.a2a.inprocess import build_inprocess_a2a_registry

            registry = build_inprocess_a2a_registry(clock)
        ctx = AppContext(settings, clock=clock, registry=registry)
        app = create_app(ctx)
        say("running in-process (%s adapters, fixture clock %s, environment=%s)" % (mode, clock.now().isoformat(), ctx.environment))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo.local", timeout=30) as client:
            demo = Demo(client)
            await demo.demo1_and_2()
            await demo.demo3()
            await demo.mismatch_demo()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=None, help="run against a live API instead of in-process")
    parser.add_argument("--mode", choices=["direct", "a2a"], default="direct", help="adapter mode for in-process runs")
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.mode))


if __name__ == "__main__":
    main()
