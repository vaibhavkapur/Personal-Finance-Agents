"""Agent evaluation harness (plan §19).

Runs labeled cases from ``fixtures/eval_cases.json`` against a fresh database
each, driving customer turns through the orchestrator and the surrounding
workflow (approval, worker, clock, provider modes) through the HTTP API.

Metrics per policy:
* task completion: every step expectation met and final state as labeled;
* unnecessary questions: questions asked outside the case's allowed set;
* unsupported claims: replies asserting acceptance/completion/movement that
  the case state does not support;
* cost: tool calls per case (and model tokens when a model is used).

State and evidence are evaluated, not wording, except for a few required
evidence citations (provider references) marked in the fixture.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent.llm import Policy, RulesPolicy, default_policy
from app.agent.orchestrator import run_turn
from app.config import FIXTURES_DIR
from app.persistence import db
from app.persistence.seed import seed

CUSTOMER = {"X-Customer-Id": "cus_demo_1"}
OPERATOR = {"X-Customer-Id": "ops_eval", "X-Role": "operator"}

# (pattern, states in which the claim is supported)
CLAIM_RULES = [
    (re.compile(r"done and verified|has been completed|case (is )?completed", re.I), {"completed"}),
    (re.compile(r"bank accepted|accepted the instruction", re.I), {"submitted", "verifying", "completed"}),
    (re.compile(r"(?<!no )funds (have )?moved|(?<!no )money (has )?moved|transfer (is )?complete", re.I), {"completed"}),
    (re.compile(r"I've prepared", re.I), {"awaiting_approval"}),
]


@dataclass
class CaseResult:
    case_id: str
    category: str
    split: str
    completed: bool
    failures: list[str] = field(default_factory=list)
    unnecessary_questions: int = 0
    unsupported_claims: int = 0
    tool_calls: int = 0
    turns: int = 0
    final_state: str | None = None
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _load_cases() -> dict:
    with open(FIXTURES_DIR / "eval_cases.json") as fh:
        return json.load(fh)


def _fresh_client(tmpdir: str) -> TestClient:
    db.configure(f"sqlite:///{Path(tmpdir) / 'eval.db'}")
    db.create_schema()
    with db.session_scope() as session:
        seed(session)
    from app.main import create_app

    client = TestClient(create_app())
    client.__enter__()
    return client


def _case_detail(client: TestClient, case_id: str) -> dict:
    return client.get(f"/v1/banking-cases/{case_id}", headers=CUSTOMER).json()


def _submit_count(client: TestClient, case_id: str | None) -> int:
    if not case_id:
        return 0
    tl = client.get(f"/v1/banking-cases/{case_id}/timeline", headers=CUSTOMER).json()
    return sum(1 for r in tl["provider_requests"] if r["operation"] == "submit_instruction")


def run_case(case: dict, story: str, policy_factory) -> CaseResult:
    started = time.perf_counter()
    result = CaseResult(case_id=case["id"], category=case["category"], split=case["split"], completed=True)
    labels = case.get("labels", {})
    allowed = set(labels.get("allowed_questions", []))
    with tempfile.TemporaryDirectory() as tmp:
        client = _fresh_client(tmp)
        try:
            case_id: str | None = None
            for i, step in enumerate(case["steps"]):
                kind = step["type"]
                if kind == "turn":
                    message = step["message"].replace("$story", story)
                    outcome = asyncio.run(run_turn("cus_demo_1", message, case_id=case_id, policy=policy_factory()))
                    result.turns += 1
                    result.tool_calls += len(outcome.tool_calls)
                    case_id = outcome.case_id or case_id
                    state = outcome.state
                    for q in outcome.questions:
                        if q not in allowed:
                            result.unnecessary_questions += 1
                    for pattern, ok_states in CLAIM_RULES:
                        if pattern.search(outcome.reply) and (state not in ok_states):
                            result.unsupported_claims += 1
                    exp = step.get("expect", {})
                    _check(result, i, exp, outcome, client, case_id)
                elif kind == "approve":
                    detail = _case_detail(client, case_id)
                    action_id = detail.get("current_action_id")
                    if not action_id:
                        result.failures.append(f"step {i}: no action to approve")
                        continue
                    ch = client.post(f"/v1/actions/{action_id}/challenge", headers=CUSTOMER).json()
                    r = client.post(f"/v1/actions/{action_id}/approve", json={"expected_case_version": ch["expected_case_version"], "action_payload_hash": ch["action_payload_hash"], "approval_challenge_id": ch["approval_challenge_id"]}, headers=CUSTOMER)
                    if r.status_code != 200:
                        result.failures.append(f"step {i}: approve failed {r.status_code} {r.text}")
                elif kind == "worker":
                    client.post("/v1/operator/worker/run", headers=OPERATOR)
                elif kind == "worker_once":
                    from app.workflows import worker

                    asyncio.run(worker.run_once("eval"))
                elif kind == "advance":
                    client.post("/v1/operator/clock/advance", json={"days": step.get("days", 0), "hours": step.get("hours", 0), "minutes": step.get("minutes", 0)}, headers=OPERATOR)
                elif kind == "submit_mode":
                    client.post("/v1/operator/mock-bank/submit-mode", json={"mode": step["mode"]}, headers=OPERATOR)
                elif kind == "offer_mode":
                    client.post("/v1/operator/mock-bank/offer-mode", json={"mode": step["mode"], "offer_id": step.get("offer_id", "off_harbor_12m")}, headers=OPERATOR)
                elif kind == "revoke":
                    client.post("/v1/operator/mock-bank/revoke-access", json={"account_id": step["account_id"], "revoked": True}, headers=OPERATOR)
                elif kind == "forged_callback":
                    event = {"id": "bevt_forged", "type": "bank_instruction.effective", "occurred_at": "2026-10-03T00:00:00Z", "environment": "mock", "data": {"request_ref": f"req_{case_id}_2", "provider_reference": "bankref_forged"}}
                    client.post("/v1/provider-events/bank", json=event, headers={"X-Provider-Id": "bank_harbor", "X-Signature": "forged"})
                elif kind == "replay_inbox":
                    inbox = client.get("/v1/operator/inbox", headers=OPERATOR).json()
                    for ev in inbox:
                        client.post(f"/v1/operator/inbox/{ev['id']}/replay", headers=OPERATOR)
                else:
                    result.failures.append(f"step {i}: unknown step type {kind}")

            final_state = _case_detail(client, case_id)["state"] if case_id else None
            result.final_state = final_state
            if labels.get("expected_final_state") != final_state:
                result.failures.append(f"final state {final_state} != {labels.get('expected_final_state')}")
            if case_id:
                detail = _case_detail(client, case_id)
                if labels.get("expected_action_type") and (detail.get("instruction") or {}).get("instruction_type") != labels["expected_action_type"]:
                    result.failures.append(f"action type {(detail.get('instruction') or {}).get('instruction_type')} != {labels['expected_action_type']}")
                if labels.get("expected_amount_minor") and (detail.get("instruction") or {}).get("amount_minor") != labels["expected_amount_minor"]:
                    result.failures.append("instruction amount mismatch")
                if "max_submits" in labels and _submit_count(client, case_id) > labels["max_submits"]:
                    result.failures.append("more provider submissions than allowed")
                if final_state == "completed" and not detail.get("completion_evidence_ref"):
                    result.failures.append("completed without evidence reference")
        finally:
            client.__exit__(None, None, None)
            db.get_engine().dispose()
    result.completed = not result.failures and result.unsupported_claims == 0
    result.seconds = round(time.perf_counter() - started, 2)
    return result


def _check(result: CaseResult, i: int, exp: dict, outcome, client: TestClient, case_id: str | None) -> None:
    if "state" in exp and outcome.state != exp["state"]:
        result.failures.append(f"step {i}: state {outcome.state} != {exp['state']}")
    if exp.get("no_case") and outcome.case_id:
        result.failures.append(f"step {i}: a case was opened unexpectedly")
    if "questions" in exp and set(outcome.questions) != set(exp["questions"]):
        result.failures.append(f"step {i}: questions {sorted(outcome.questions)} != {sorted(exp['questions'])}")
    tools = [c["tool"] for c in outcome.tool_calls]
    for t in exp.get("tools_include", []):
        if t not in tools:
            result.failures.append(f"step {i}: expected tool {t} not called")
    for t in exp.get("tools_exclude", []):
        if t in tools:
            result.failures.append(f"step {i}: forbidden tool {t} called")
    for text in exp.get("reply_contains", []):
        if text not in outcome.reply:
            result.failures.append(f"step {i}: reply missing {text!r}")
    if "refused" in exp and outcome.refused != exp["refused"]:
        result.failures.append(f"step {i}: refused={outcome.refused}")
    if "escalated" in exp and outcome.escalated != exp["escalated"]:
        result.failures.append(f"step {i}: escalated={outcome.escalated}")
    if exp.get("no_submit") and _submit_count(client, case_id) > 0:
        result.failures.append(f"step {i}: a submission reached the provider")
    if "action_amount_minor" in exp and case_id:
        detail = _case_detail(client, case_id)
        amount = ((detail.get("review") or {}).get("instruction") or {}).get("amount_minor")
        if amount != exp["action_amount_minor"]:
            result.failures.append(f"step {i}: action amount {amount} != {exp['action_amount_minor']}")


def summarize(results: list[CaseResult]) -> dict:
    def block(rows: list[CaseResult]) -> dict:
        n = len(rows)
        return {
            "cases": n,
            "completed": sum(r.completed for r in rows),
            "completion_rate": round(sum(r.completed for r in rows) / n, 3) if n else None,
            "unnecessary_questions": sum(r.unnecessary_questions for r in rows),
            "unsupported_claims": sum(r.unsupported_claims for r in rows),
            "tool_calls_total": sum(r.tool_calls for r in rows),
            "tool_calls_per_case": round(sum(r.tool_calls for r in rows) / n, 2) if n else None,
            "turns_total": sum(r.turns for r in rows),
        }

    by_category: dict[str, list[CaseResult]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)
    return {
        "all": block(results),
        "dev": block([r for r in results if r.split == "dev"]),
        "heldout": block([r for r in results if r.split == "heldout"]),
        "by_category": {k: block(v) for k, v in sorted(by_category.items())},
        "failed_cases": [{"id": r.case_id, "failures": r.failures} for r in results if not r.completed],
    }


def run_all(policy_name: str = "rules", split: str | None = None) -> dict:
    data = _load_cases()
    story = data["story"]
    if policy_name == "rules":
        factory = RulesPolicy
    else:
        factory = default_policy
    results = [run_case(c, story, factory) for c in data["cases"] if split is None or c["split"] == split]
    return {
        "policy": policy_name,
        "model_version": factory().model_version,
        "fixture_version": data["fixture_version"],
        "summary": summarize(results),
        "cases": [r.to_dict() for r in results],
    }
