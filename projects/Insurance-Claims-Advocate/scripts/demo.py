"""One documented end-to-end demo command.

    python scripts/demo.py [complete|missing-evidence|partial-rejection|all] [--db sqlite:///demo.db]

Drives the customer journey through the same service layer the API uses, with the fixture clock frozen at
2026-09-14T10:00Z, and prints each step with the evidence that supports it. Nothing real is submitted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.clock import Clock, parse_iso  # noqa: E402
from app.config import Settings  # noqa: E402
from app.context import AppContext  # noqa: E402
from app.workflows.approvals import PrincipalView  # noqa: E402

CUSTOMER = PrincipalView("prn_customer_demo_3", "customer", "cus_demo_3", "Jordan Rivera")
LOSS_AT = "2026-09-10T14:00:00Z"


def money(minor, cur="USD"):
    return f"{minor / 100:.2f} {cur}"


def say(step, text):
    print(f"\n[{step}] {text}")


class Demo:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.case_id = None

    def view(self):
        return self.ctx.cases.get_case(CUSTOMER, self.case_id)

    def work(self):
        return asyncio.run(self.ctx.worker.run_until_idle())

    def open(self, docs, scenario=None):
        res = self.ctx.cases.create_case(CUSTOMER, customer_id="cus_demo_3", policy_id="travel_policy_demo_1", loss_type="baggage_delay", loss_at=LOSS_AT, document_ids=docs, mock_scenario=scenario)
        self.case_id = res["id"]
        say("open", f"case {res['id']} on policy version {res['policy_version']} (chosen from the loss date) · status {res['status']} · missing: {res['missing_fields'] or 'nothing'}")
        return res

    def agent(self, message):
        turn = self.ctx.agent.run_turn(CUSTOMER, self.case_id, message)
        say("advocate", f"(tools: {', '.join(turn['tools_used'])})\n    {turn['message']}")
        return turn

    def answer_all(self, answers):
        for q in self.view()["open_questions"]:
            if q["field"] in answers:
                self.ctx.cases.answer_question(CUSTOMER, self.case_id, q["id"], answers[q["field"]])
                say("customer", f"answers '{q['field']}': {answers[q['field']]}")

    def approve_pending(self):
        a = self.view()["pending_action"]
        r = a["review_summary"]
        say("review", f"{a['action_type']} → {r['destination']}\n    documents: {[d['document_id'] for d in r['documents']]}\n    requested {money(r['requested_total_minor'], r['currency'])}, fixture maximum {money(r['expected_maximum_minor'], r['currency'])}\n    excluded & disclosed: {[(x['receipt_id'], money(x['amount_minor'])) for x in r['excluded_items']]}\n    {r['irreversible_effect']}")
        self.ctx.cases.approve_action(CUSTOMER, a["action_id"], expected_case_version=a["expected_case_version"], action_payload_hash=a["content_hash"], approval_challenge_id=a["approval_challenge_id"])
        say("customer", f"approves exactly packet {a['content_hash'][:24]}… at case version {a['expected_case_version']}")
        self.work()
        v = self.view()
        say("worker", f"submitted with idempotent reference; insurer claim ref {v['case']['external_claim_ref']} · case now {v['case']['status']}")
        return v

    def pay(self, mode):
        v = self.view()
        d = v["decisions"][-1]
        for ev in self.ctx.payment_feed.generate(claim_reference=v["case"]["external_claim_ref"], payee_id="cus_demo_3", approved_minor=d["accepted_minor"], currency=d["currency"], mode=mode):
            self.ctx.events.handle_payment_event(ev["payload"], ev["signature"])
        v = self.view()
        s = v["settlement"]
        say("payout", f"mock feed posted '{mode}' → accepted {money(s['accepted_minor'])}, paid {money(s['paid_minor'])}, outstanding {money(s['outstanding_minor'])} · {s['status']} · case {v['case']['status']}")

    def show_decision(self):
        d = self.view()["decisions"][-1]
        ex = d["explanation"]
        say("decision", f"v{d['decision_version']} {d['outcome']}: accepted {money(d['accepted_minor'])}, not paid {money(d['rejected_minor'])}\n    {ex['summary']}")
        for i in ex["items"]:
            if i["rejected_minor"]:
                cite = f" [{i['citation']['clause_id']} v{i['citation']['policy_version']}]" if i.get("citation") else ""
                print(f"    - {i['insurer_reason_code']} {money(i['rejected_minor'])}: {i['policy_view']}{cite} — {i.get('explanation', '')}")
                if i.get("fact_locators"):
                    print(f"      facts: {', '.join(i['fact_locators'])}")


def demo_complete(ctx):
    print("\n=== Demo 1: complete claim ===")
    d = Demo(ctx)
    d.open(["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3_early", "receipt_demo_4"])
    d.agent("My bag arrived two days late. Help me claim the essential purchases I made.")
    d.approve_pending()
    d.show_decision()
    d.pay("exact")
    d.agent("Where do we stand?")
    return d


def demo_missing(ctx):
    print("\n=== Demo 2: missing evidence ===")
    d = Demo(ctx)
    d.open(["itinerary_demo", "baggage_report_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3_early"])
    d.agent("My bag arrived two days late. Help me claim the essential purchases I made.")
    d.answer_all({"baggage_delivered_at": {"delivered_at": "2026-09-12T16:30:00Z"}})
    d.agent("Here you go.")
    d.approve_pending()
    d.agent("Any news?")
    say("customer", "uploads arrival_confirmation_demo (the insurer's requested document)")
    ctx.cases.attach_documents(CUSTOMER, d.case_id, ["arrival_confirmation_demo"])
    d.agent("I uploaded the delivery confirmation.")
    v = d.approve_pending()
    say("case", f"submissions: {[(s['sequence'], s['kind'], s['external_claim_ref']) for s in v['submissions']]} — same insurer claim, request {v['insurer_requests'][0]['provider_request_id']} {v['insurer_requests'][0]['status']}")
    d.show_decision()
    d.pay("smaller")
    d.pay("remainder")
    return d


def demo_partial(ctx):
    print("\n=== Demo 3: partial rejection ===")
    d = Demo(ctx)
    d.open(["itinerary_demo", "baggage_report_demo", "arrival_confirmation_demo", "receipt_demo_1", "receipt_demo_2", "receipt_demo_3", "receipt_demo_4"])
    d.agent("My bag arrived two days late. Help me claim the essential purchases I made.")
    d.approve_pending()
    d.show_decision()
    d.agent("Can we challenge this?")
    d.approve_pending()
    d.show_decision()
    d.pay("exact")
    return d


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", nargs="?", default="all", choices=["complete", "missing-evidence", "partial-rejection", "all"])
    parser.add_argument("--db", default="sqlite:///:memory:")
    parser.add_argument("--export", help="write the last case's history to this JSON file")
    args = parser.parse_args()
    ctx = AppContext(Settings(database_url=args.db), Clock(parse_iso("2026-09-14T10:00:00Z")))
    print(f"environment={ctx.settings.environment} adapter={ctx.adapter.capabilities.name} clock={ctx.clock.now_iso()} (frozen fixture clock)")
    last = None
    if args.scenario in ("complete", "all"):
        last = demo_complete(ctx)
    if args.scenario in ("missing-evidence", "all"):
        last = demo_missing(ctx)
    if args.scenario in ("partial-rejection", "all"):
        last = demo_partial(ctx)
    if args.export and last:
        Path(args.export).write_text(json.dumps(ctx.cases.export_case(CUSTOMER, last.case_id), indent=2))
        print(f"\nexported case history to {args.export}")


if __name__ == "__main__":
    main()
