"""Structured tool-calling model interface.

`RulesPlanner` is a deterministic planner that drives the same tool loop
without any LLM, so the prototype runs offline and the rules-only baseline
for evaluation is the same code path. `OpenAICompatibleModel` shows how a real
model plugs in behind the same interface (chat completions with tools); it is
optional and untested in this repository because no credentials are available.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from ..domain.money import format_minor


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]


@dataclass
class ModelDecision:
    tool_calls: List[ToolCall] = field(default_factory=list)
    final_message: Optional[str] = None
    questions: List[Dict[str, Any]] = field(default_factory=list)
    escalation: Optional[Dict[str, Any]] = None


class ToolCallingModel(Protocol):
    version: str

    def decide(self, *, system_prompt: str, transcript: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelDecision: ...


def _last_result(transcript: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for entry in reversed(transcript):
        if entry.get("role") == "tool" and entry.get("name") == name:
            return entry.get("result")
    return None


def _called(transcript: List[Dict[str, Any]], name: str) -> bool:
    return _last_result(transcript, name) is not None


class RulesPlanner:
    """Deterministic policy over tool results. Every decision is a function of the transcript."""

    version = "rules-planner/v1"

    def decide(self, *, system_prompt: str, transcript: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelDecision:
        case_id = transcript[1]["case_id"]
        status = _last_result(transcript, "get_recovery_status")
        if status is None:
            return ModelDecision(tool_calls=[ToolCall("get_recovery_status", {"case_id": case_id})])
        if "error" in status:
            return ModelDecision(final_message=f"I could not load this case: {status['error']['message']}", escalation={"reason": "status_unavailable", "error": status["error"]})
        st = status["data"]
        state = st["status"]
        next_step = st["next_step"]

        if state in ("detected", "investigating") and not _called(transcript, "find_refund_evidence"):
            return ModelDecision(tool_calls=[ToolCall("find_refund_evidence", {"order_id": st["order_ref"]})])
        if state in ("detected", "investigating") and not _called(transcript, "match_refund_credits"):
            return ModelDecision(tool_calls=[ToolCall("match_refund_credits", {"case_id": case_id})])

        match = _last_result(transcript, "match_refund_credits")
        if match is not None and "error" not in match:
            st_after = match["data"]
            state, next_step = st_after["status"], st_after["next_step"]
            amounts = st_after["amounts"]
        else:
            amounts = st["amounts"]

        if next_step.startswith("answer_customer_question"):
            q = (match or status)["data"].get("pending_question") or st.get("pending_question")
            return ModelDecision(final_message=self._question_text(q, amounts), questions=[q] if q else [])
        if next_step == "provide_refund_promise_evidence":
            q = {"kind": "missing_promise_evidence", "prompt": "I could not find a verified refund promise for this order. What amount was promised, on what date, and by whom? Attach the confirmation if you have it."}
            return ModelDecision(final_message=q["prompt"], questions=[q])
        if next_step == "draft_merchant_message" and not _called(transcript, "prepare_recovery_message"):
            return ModelDecision(tool_calls=[ToolCall("prepare_recovery_message", {"case_id": case_id, "recipient": "merchant"})])
        if next_step == "consider_issuer_dispute" and not _called(transcript, "prepare_dispute_packet"):
            return ModelDecision(tool_calls=[ToolCall("prepare_dispute_packet", {"case_id": case_id})])

        draft = _last_result(transcript, "prepare_recovery_message") or _last_result(transcript, "prepare_dispute_packet")
        if draft is not None and "error" in draft:
            return ModelDecision(final_message=f"I could not prepare the next step: {draft['error']['message']}", escalation={"reason": "draft_blocked", "error": draft["error"]})
        return ModelDecision(final_message=self._summary(st, amounts, state, next_step, draft))

    # -- text composition (facts only; no claims beyond state) -----------------
    def _question_text(self, q: Optional[Dict[str, Any]], amounts: Dict[str, Any]) -> str:
        if not q:
            return "I need one more detail from you before continuing."
        text = q["prompt"]
        if q.get("kind") == "ambiguous_credit_match":
            opts = [f"{format_minor(o['amount_minor'], amounts['currency'])} posted {o['posted_at'][:10]} ({o['transaction_id']})" for o in q.get("options", []) if o.get("transaction_id")]
            text += " Options: " + "; ".join(opts) + "; or none of these."
        return text

    def _summary(self, st: Dict[str, Any], amounts: Dict[str, Any], state: str, next_step: str, draft: Optional[Dict[str, Any]]) -> str:
        cur = amounts["currency"]
        fmt = lambda v: format_minor(v, cur)  # noqa: E731
        if draft and "error" not in draft and draft["data"]["status"] == "awaiting_approval":
            state = "awaiting_approval"  # a successful draft always moves the case here
        parts = [f"Case {st['case_id']} for order {st['order_ref']} is `{state}`."]
        parts.append(f"Target {fmt(amounts['target_minor'])}; final credits posted {fmt(amounts['final_recovered_minor'])}; outstanding {fmt(amounts['outstanding_minor'])}.")
        if amounts["provisional_minor"]:
            parts.append(f"A provisional issuer credit of {fmt(amounts['provisional_minor'])} is shown separately and is not counted as recovered.")
        if amounts["store_credit_minor"]:
            parts.append(f"Store credit of {fmt(amounts['store_credit_minor'])} was issued by the merchant; it is not a card credit and does not reduce the outstanding amount.")
        if amounts["reversed_minor"]:
            parts.append(f"Credits totalling {fmt(amounts['reversed_minor'])} were reversed and are excluded from recovery.")
        merchant = next((c for c in st.get("channels", []) if c["channel_type"] == "merchant"), None)
        if merchant and merchant.get("last_provider_status") in ("refund_issued", "completed") and amounts["outstanding_minor"] > 0:
            if amounts["final_recovered_minor"] == 0:
                parts.append(f"The merchant reports the refund as issued (simulated status, reference {merchant.get('provider_case_ref')}), but no matching credit has posted to the account; the refund is not recovered until it posts.")
            else:
                parts.append(f"The merchant reports the refund as issued (simulated status, reference {merchant.get('provider_case_ref')}); only {fmt(amounts['final_recovered_minor'])} has posted, so {fmt(amounts['outstanding_minor'])} is still not recovered.")
        if state == "recovered":
            parts.append(f"Recovery is verified by posted credits ({st.get('completion_evidence_ref')}).")
        elif state == "already_refunded":
            parts.append("The refund had already posted before any external request was needed.")
        elif state == "unresolved":
            parts.append(f"The case is closed without card recovery: {st.get('outcome_note')}.")
        elif state == "manual_review":
            parts.append("The case is held for operator review; no further external action will be taken automatically.")
        if draft and "error" not in draft:
            d = draft["data"]
            if d["type"] == "submit_issuer_dispute":
                parts.append(f"I prepared a separate issuer dispute packet for {fmt(d['review']['amount_minor'])} ({d['review']['terms']['reason_code']}). It needs your separate approval on the review screen before anything is submitted (action {d['action_id']}).")
            else:
                parts.append(f"I prepared a follow-up to {d['review']['destination']['address']} with {len(d['review']['documents'])} attachment(s) for {fmt(d['review']['amount_minor'])}. Nothing is sent until you approve it on the review screen (action {d['action_id']}).")
        elif next_step == "approve_pending_action":
            parts.append("A drafted action is waiting for your approval on the review screen.")
        elif next_step.startswith("wait_for"):
            parts.append(f"Next: {next_step.replace('_', ' ')}.")
        if st.get("deadline") and st["deadline"].get("alert"):
            parts.append(f"Deadline alert ({st['deadline']['alert']}): configured dispute window (fixture) ends {st['deadline']['deadline_at'][:10]}.")
        return " ".join(parts)


class OpenAICompatibleModel:
    """Optional: OpenAI-compatible chat completions with tool calling via httpx.
    Not exercised in tests; provided to show the single structured interface."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.version = f"openai-compatible/{model}"

    def decide(self, *, system_prompt: str, transcript: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelDecision:
        import httpx  # local import keeps the dependency optional at call time

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for entry in transcript[1:]:
            if entry["role"] == "user":
                messages.append({"role": "user", "content": entry["content"]})
            elif entry["role"] == "tool":
                messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": entry["call_id"], "type": "function", "function": {"name": entry["name"], "arguments": json.dumps(entry["arguments"])}}]})
                messages.append({"role": "tool", "tool_call_id": entry["call_id"], "content": json.dumps(entry["result"], default=str)})
        body = {"model": self.model, "messages": messages, "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}} for t in tools]}
        resp = httpx.post(f"{self.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=60)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        calls = [ToolCall(tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}")) for tc in msg.get("tool_calls", []) or []]
        if calls:
            return ModelDecision(tool_calls=calls)
        return ModelDecision(final_message=msg.get("content") or "")
