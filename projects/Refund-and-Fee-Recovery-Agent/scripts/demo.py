"""End-to-end demo of the three plan scenarios against the simulators.

    python scripts/demo.py            # runs all three, prints timelines
    python scripts/demo.py --write    # also writes docs/demo-output.md

Everything runs in-memory with the fixture clock; no network, no credentials.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent.orchestrator import Orchestrator  # noqa: E402
from backend.app.container import Container, build_container  # noqa: E402
from backend.app.domain.money import format_minor  # noqa: E402

CUSTOMER = "cus_demo_4"


class Log:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)


def amounts_line(c: Container, case_id: str) -> str:
    st = c.service.get_status(case_id)
    a = st["amounts"]
    f = lambda v: format_minor(v, a["currency"])  # noqa: E731
    return (f"status={st['status']:<18} target={f(a['target_minor'])} final={f(a['final_recovered_minor'])} provisional={f(a['provisional_minor'])} "
            f"store={f(a['store_credit_minor'])} reversed={f(a['reversed_minor'])} outstanding={f(a['outstanding_minor'])} next={st['next_step']}")


def approve(c: Container, proposed: Dict[str, Any], log: Log) -> None:
    r = proposed["review"]
    log(f"  REVIEW SCREEN -> lane={r['lane']} destination={r['destination'].get('address') or r['destination'].get('recipient_ref')} amount={format_minor(r['amount_minor'], r['currency'])} documents={len(r['documents'])}")
    log(f"                   irreversible: {r['irreversible_effect']}")
    c.service.approve_action(proposed["action_id"], approver_id=CUSTOMER, customer_id=CUSTOMER, expected_case_version=proposed["expected_case_version"],
                             action_payload_hash=proposed["payload_hash"], approval_challenge_id=proposed["approval_challenge_id"])
    handled = c.worker.run_until_idle_sync()
    log(f"  customer approved action {proposed['action_id']} (challenge {proposed['approval_challenge_id']}); worker executed {handled} job(s)")


def agent(c: Container, o: Orchestrator, case_id: str, log: Log):
    turn = o.run(case_id=case_id, customer_id=CUSTOMER)
    log(f"  AGENT [{', '.join(t['name'] for t in turn.tool_calls)}]")
    log(f"  > {turn.summary}")
    return turn


def advance(c: Container, days: float, case_id: str, log: Log) -> None:
    r = c.advance(days=days)
    log(f"  +{days:g}d -> {r['now'][:10]} callbacks={r['callbacks_delivered']} jobs={r['jobs_handled']} | {amounts_line(c, case_id)}")


def timeline(c: Container, case_id: str, log: Log) -> None:
    log("  timeline:")
    for e in c.repos.events_for_case(case_id):
        if e.event_type == "case.transitioned":
            log(f"    {e.sequence:>2}. {e.previous_state} -> {e.next_state}  ({e.actor})")
        elif e.event_type in ("credit.matched", "credit.reversed", "merchant.refund_claimed", "merchant.store_credit_issued", "action.submitted", "credit.interpretation_revised", "merchant.claim_unverified"):
            log(f"    {e.sequence:>2}. {e.event_type}: { {k: v for k, v in e.data.items() if k in ('amount_minor', 'credit_kind', 'provider_ref', 'refund_ref', 'from', 'to', 'type')} }")


def demo_1(log: Log) -> None:
    log("=" * 100)
    log("DEMO 1: Missing refund recovered (order_mock_499, $84.99 promised)")
    log("=" * 100)
    c = build_container(database_path=":memory:")
    c.seed()
    o = Orchestrator(c.service)
    case = c.service.create_case(customer_id=CUSTOMER, order_ref="order_mock_499", reason_code="promised_refund_missing", target_minor=8499, currency="USD", evidence_ids=["receipt_mock_1", "promise_mock_1"], actor=CUSTOMER)
    log(f"  created {case.id}: {amounts_line(c, case.id)}")
    turn = agent(c, o, case.id, log)
    approve(c, turn.proposed_action, log)
    log(f"  {amounts_line(c, case.id)}")
    for d in (1, 2, 3):
        advance(c, d, case.id, log)
    agent(c, o, case.id, log)
    timeline(c, case.id, log)
    log(f"  provider calls: {[(p['operation'], p['status'], p['environment']) for p in c.repos.provider_requests()]}")
    log()


def demo_2(log: Log) -> None:
    log("=" * 100)
    log("DEMO 2: Partial recovery (order_mock_500, $50.00 arrives against $84.99)")
    log("=" * 100)
    c = build_container(database_path=":memory:")
    c.seed()
    o = Orchestrator(c.service)
    case = c.service.create_case(customer_id=CUSTOMER, order_ref="order_mock_500", reason_code="promised_refund_missing", target_minor=8499, currency="USD", evidence_ids=["receipt_mock_2", "promise_mock_2"], actor=CUSTOMER)
    turn = agent(c, o, case.id, log)
    approve(c, turn.proposed_action, log)
    for d in (1, 2, 3):
        advance(c, d, case.id, log)
    agent(c, o, case.id, log)
    advance(c, 10, case.id, log)
    turn = agent(c, o, case.id, log)
    log(f"  case stays open for the remaining {format_minor(c.service.get_status(case.id)['amounts']['outstanding_minor'], 'USD')}; issuer dispute drafted for the remainder only, pending separate approval: {turn.proposed_action['type'] if turn.proposed_action else None}")
    timeline(c, case.id, log)
    log()


def demo_3(log: Log) -> None:
    log("=" * 100)
    log("DEMO 3: Provisional reversal (order_mock_503, merchant claims refund, issuer credit appears then reverses)")
    log("=" * 100)
    c = build_container(database_path=":memory:")
    c.seed()
    o = Orchestrator(c.service)
    case = c.service.create_case(customer_id=CUSTOMER, order_ref="order_mock_503", reason_code="promised_refund_missing", target_minor=12000, currency="USD", evidence_ids=["receipt_mock_5", "promise_mock_5"], actor=CUSTOMER)
    turn = agent(c, o, case.id, log)
    approve(c, turn.proposed_action, log)
    for d in (1, 2):
        advance(c, d, case.id, log)
    agent(c, o, case.id, log)
    advance(c, 10, case.id, log)
    turn = agent(c, o, case.id, log)
    approve(c, turn.proposed_action, log)
    log(f"  {amounts_line(c, case.id)}")
    for d in (2, 5, 7):
        advance(c, d, case.id, log)
        agent(c, o, case.id, log)
    timeline(c, case.id, log)
    log()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write docs/demo-output.md")
    args = parser.parse_args()
    log = Log()
    demo_1(log)
    demo_2(log)
    demo_3(log)
    if args.write:
        out = ROOT / "docs" / "demo-output.md"
        out.write_text("# Demo output (generated by `python scripts/demo.py --write`)\n\nAll providers are simulators; amounts, dates and references are fixtures.\n\n```text\n" + "\n".join(log.lines) + "\n```\n")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
