"""Planners decide which tool to call next. The scripted planner is deterministic (also the rules-only baseline for
evaluation); the LLM planner talks to any OpenAI-compatible chat-completions endpoint through one structured interface."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

import httpx

from .prompts import SYSTEM_PROMPT


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]


@dataclass
class FinalAnswer:
    message: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnState:
    case_id: str
    customer_message: str
    tool_results: List[Dict[str, Any]] = field(default_factory=list)

    def last(self, tool: str) -> Optional[Dict[str, Any]]:
        for r in reversed(self.tool_results):
            if r.get("tool") == tool:
                return r
        return None

    def called(self, tool: str) -> bool:
        return any(r.get("tool") == tool for r in self.tool_results)


class Planner(Protocol):
    name: str

    def next(self, state: TurnState, tool_schemas: List[Dict[str, Any]]):  # -> ToolCall | FinalAnswer
        ...


def _fmt(minor: Optional[int], currency: str = "USD") -> str:
    if minor is None:
        return "n/a"
    return f"{minor / 100:.2f} {currency}"


class ScriptedPlanner:
    """Deterministic policy over the case state. Asks only for fields the evaluation reports as missing."""

    name = "scripted"

    def next(self, state: TurnState, tool_schemas: List[Dict[str, Any]]):
        status_result = state.last("get_case_status")
        if not status_result:
            return ToolCall("get_case_status", {"case_id": state.case_id})
        if "error" in status_result:
            return FinalAnswer(f"I could not load this case: {status_result['error']}", {"error": status_result["error"]})
        view = status_result["data"]
        case = view["case"]
        status = case["status"]
        currency = view["totals"].get("currency", "USD")
        evaluation = view.get("evaluation") or {}

        # contradictions are escalated, not resolved by guessing
        unsupported = [c for c in view.get("checklist", []) if c["status"] == "unsupported"]
        if unsupported and status in ("collecting", "evaluating"):
            details = "; ".join(f"{c['id']}: {c['detail']} (evidence: {', '.join(c['evidence']) or 'none'})" for c in unsupported)
            return FinalAnswer(f"The evidence does not support the claim as it stands, so I am not preparing a submission. {details}. If you believe the documents are wrong, upload corrected evidence and I will re-check.", {"escalation": "unsupported_conditions", "conditions": unsupported})

        if status in ("collecting", "evaluating") and not state.called("evaluate_policy_facts"):
            return ToolCall("evaluate_policy_facts", {"case_id": state.case_id, "policy_version": case.get("policy_version")})
        eval_result = state.last("evaluate_policy_facts")
        if eval_result and "data" in eval_result:
            # refresh the view with the fresh evaluation
            view = dict(view, evaluation=eval_result["data"]["evaluation"], open_questions=eval_result["data"]["open_questions"])
            status = eval_result["data"]["status"]
            evaluation = eval_result["data"]["evaluation"]

        open_questions = view.get("open_questions", [])
        pending = view.get("pending_action")

        if status == "collecting":
            missing = [m for m in evaluation.get("missing_fields", []) if not m.startswith("question:")]
            parts = []
            if open_questions:
                parts.append("I need a few claim-specific answers: " + " ".join(f"[{q['id']}] {q['question']}" for q in open_questions))
            docs_missing = [m for m in missing if m in ("baggage_delay_report", "receipt", "baggage_arrival_confirmation", "itinerary")]
            if docs_missing:
                parts.append("Please upload: " + ", ".join(docs_missing) + ".")
            if not parts:
                parts.append("The claim is not ready yet: " + ", ".join(missing or ["no supported expenses"]) + ".")
            totals = evaluation.get("totals", {})
            parts.append(f"So far the fixture rules support {_fmt(totals.get('supported_minor'), currency)} (cap {_fmt(totals.get('cap_minor'), currency)}); this is an estimate, not the insurer's decision.")
            return FinalAnswer(" ".join(parts), {"questions": open_questions, "missing_fields": missing})

        if status == "evaluating":
            if pending and pending["status"] == "proposed":
                return self._review_answer(view, pending, currency)
            if not state.called("prepare_claim_action"):
                return ToolCall("prepare_claim_action", {"case_id": state.case_id, "action_type": "submission"})
            prepared = state.last("prepare_claim_action")
            if prepared and "error" in prepared:
                return FinalAnswer(f"I could not prepare the packet: {prepared['error']}", {"error": prepared["error"]})
            return self._review_answer(view, prepared["data"], currency)

        if status == "awaiting_approval":
            if pending and pending["status"] == "proposed":
                return self._review_answer(view, pending, currency)
            if pending and pending["status"] in ("approved", "executing"):
                return FinalAnswer("Your approved packet is being submitted by the background worker. I will report the insurer reference once it is stored.", {"pending_action": pending})
            if pending and pending["status"] == "uncertain":
                return FinalAnswer("The insurer's response was lost after we sent the packet. I am not resending anything until the worker confirms, by our request reference, whether it was received.", {"pending_action": pending})
            return FinalAnswer("A packet was drafted but its approval is no longer valid. Ask me to prepare it again.", {})

        if status == "evidence_requested":
            reqs = [r for r in view.get("insurer_requests", []) if r["status"] == "open"]
            req = reqs[-1] if reqs else None
            needed = req["requirement"]["document_type"] if req else "requested document"
            have = any(d["doc_type"] == needed for d in view.get("documents", []))
            if have and not state.called("prepare_claim_action"):
                return ToolCall("prepare_claim_action", {"case_id": state.case_id, "action_type": "supplemental"})
            if have:
                prepared = state.last("prepare_claim_action")
                if prepared and "data" in prepared:
                    return self._review_answer(view, prepared["data"], currency, supplemental=True)
                return FinalAnswer(f"I could not prepare the supplemental packet: {prepared.get('error') if prepared else 'unknown error'}", {})
            due = f" by {req['due_at']}" if req and req.get("due_at") else ""
            return FinalAnswer(f"The insurer (claim {case.get('external_claim_ref')}) asked for your {needed.replace('_', ' ')}{due}. Upload it and I will add it to the same claim; no new claim will be opened.", {"insurer_request": req})

        if status in ("submitted", "under_review"):
            return FinalAnswer(f"Your claim {case.get('external_claim_ref')} is with the insurer ({'under review' if status == 'under_review' else 'submitted'}). Nothing is needed from you right now; I will resume when they respond.", {})

        if status in ("partially_approved", "denied", "appeal_review"):
            decision = (view.get("decisions") or [None])[-1]
            explanation = (decision or {}).get("explanation") or {}
            if pending and pending["status"] == "proposed":
                return self._review_answer(view, pending, currency, appeal=True, explanation=explanation)
            if explanation.get("has_supported_challenge") and (decision or {}).get("appeal_status") != "appealed" and not state.called("prepare_claim_action"):
                return ToolCall("prepare_claim_action", {"case_id": state.case_id, "action_type": "appeal"})
            prepared = state.last("prepare_claim_action")
            if prepared and "data" in prepared:
                return self._review_answer(view, prepared["data"], currency, appeal=True, explanation=explanation)
            summary = explanation.get("summary", "The insurer decided the claim.")
            items = "; ".join(f"{i.get('insurer_reason_code')}: {i.get('explanation', '')}" for i in explanation.get("items", []) if i.get("rejected_minor"))
            note = "No rejection is contradicted by evidence and policy text, so I am not drafting an appeal." if not explanation.get("has_supported_challenge") else ""
            return FinalAnswer(f"{summary} {items} {note} You can accept the decision to move on (accepted {_fmt((decision or {}).get('accepted_minor'), currency)}).".strip(), {"decision": decision})

        if status == "payout_pending":
            if not state.called("reconcile_claim_payment"):
                return ToolCall("reconcile_claim_payment", {"case_id": state.case_id})
            rec = state.last("reconcile_claim_payment")
            settle = (rec or {}).get("data", {}).get("settlement", {})
            return FinalAnswer(f"Approved amount {_fmt(settle.get('accepted_minor'), currency)}; paid so far {_fmt(settle.get('paid_minor'), currency)}; outstanding {_fmt(settle.get('outstanding_minor'), currency)} ({settle.get('status')}). Only a posted payment matching the claim reference, payee and currency counts.", {"settlement": settle})

        if status == "paid" or status == "closed":
            settle = view.get("settlement") or {}
            return FinalAnswer(f"Closed. The insurer's approved {_fmt(settle.get('accepted_minor'), currency)} was matched to posted payment(s) {', '.join(m['payment_ref'] for m in settle.get('matched_payments', []))}.", {"settlement": settle})
        if status == "closed_unpaid":
            return FinalAnswer("The case is closed without payment after your review of the insurer's decision. Remaining options outside this prototype: a human insurance professional or your state insurance department.", {})
        if status == "manual_review":
            return FinalAnswer("An operator must resolve an uncertain provider outcome (the insurer may or may not have received the packet). I will not resend anything until that is settled.", {})
        return FinalAnswer(f"Case is {status}.", {})

    def _review_answer(self, view: Dict[str, Any], action: Dict[str, Any], currency: str, *, supplemental: bool = False, appeal: bool = False, explanation: Optional[Dict[str, Any]] = None) -> FinalAnswer:
        review = action.get("review_summary", {})
        docs = ", ".join(f"{d['doc_type']}({d['document_id']})" for d in review.get("documents", []))
        if appeal:
            challenges = review.get("challenges", [])
            ch = "; ".join(f"{c['reason_code']} on {c['expense_id']}: {c['argument']}" for c in challenges)
            summary = (explanation or {}).get("summary", "")
            msg = f"{summary} I prepared an appeal for {_fmt(review.get('requested_total_minor'), currency)} challenging: {ch}. Destination: {review.get('destination')}. Review and approve action {action['action_id']} (challenge {action.get('approval_challenge_id')}, case version {action.get('expected_case_version')}). {review.get('irreversible_effect')}"
        elif supplemental:
            msg = f"I prepared a supplemental packet for your existing claim with: {docs}. Destination: {review.get('destination')}. Review and approve action {action['action_id']} (challenge {action.get('approval_challenge_id')}, case version {action.get('expected_case_version')}). {review.get('irreversible_effect')}"
        else:
            excluded = "; ".join(f"{x['receipt_id']} {_fmt(x['amount_minor'], currency)}: {x['reason']}" for x in review.get("excluded_items", []))
            msg = (
                f"Your packet claims {_fmt(review.get('requested_total_minor'), currency)}; the policy cap makes {_fmt(review.get('expected_maximum_minor'), currency)} the most the fixture rules support (the insurer decides). "
                f"Documents shared: {docs}. Excluded and disclosed: {excluded or 'none'}. Destination: {review.get('destination')}. "
                f"Review and approve action {action['action_id']} (challenge {action.get('approval_challenge_id')}, case version {action.get('expected_case_version')}). {review.get('irreversible_effect')}"
            )
        return FinalAnswer(msg, {"pending_action": action})


class OpenAICompatibleProvider:
    """Structured tool-calling against an OpenAI-compatible /chat/completions endpoint. Only used when configured."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0},
            )
            resp.raise_for_status()
            return resp.json()


class LLMPlanner:
    name = "llm"

    def __init__(self, provider: OpenAICompatibleProvider):
        self.provider = provider
        self.messages: List[Dict[str, Any]] = []

    def next(self, state: TurnState, tool_schemas: List[Dict[str, Any]]):
        if not self.messages:
            self.messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Case {state.case_id}. Customer says: {state.customer_message or '(no message)'} Start by loading the case status."},
            ]
        # append any tool results not yet in the transcript
        recorded = sum(1 for m in self.messages if m["role"] == "tool")
        for r in state.tool_results[recorded:]:
            call_id = r.get("_call_id", "call")
            self.messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": r["tool"], "arguments": json.dumps(r.get("_arguments", {}))}}]})
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({k: v for k, v in r.items() if not k.startswith("_")})[:20000]})
        response = self.provider.complete(self.messages, tool_schemas)
        choice = response["choices"][0]["message"]
        calls = choice.get("tool_calls") or []
        if calls:
            call = calls[0]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tc = ToolCall(call["function"]["name"], args)
            tc.arguments["_call_id"] = call.get("id", "call")
            return tc
        return FinalAnswer(choice.get("content") or "", {})
