"""One structured tool-calling interface in front of the model.

ScriptedProvider is a deterministic rules-only policy (no network) and doubles as the
baseline for evaluation. OpenAICompatibleProvider talks to any /chat/completions API.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

import httpx

from ..domain.money import fmt_minor


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    text: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str

    async def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> LLMResponse: ...


def _tool_results(messages: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Latest result per tool name from this turn's tool messages."""
    out: Dict[str, Dict[str, Any]] = {}
    for msg in messages:
        if msg.get("role") == "tool":
            try:
                out[msg["name"]] = json.loads(msg["content"])
            except (ValueError, KeyError):
                continue
    return out


def _last_customer_text(messages: List[Dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content") or ""
    return ""


def _case_id(messages: List[Dict[str, Any]]) -> Optional[str]:
    for msg in messages:
        if msg.get("role") == "system" and "case_id=" in (msg.get("content") or ""):
            match = re.search(r"case_id=(\S+)", msg["content"])
            if match:
                return match.group(1)
    return None


class ScriptedProvider:
    """Rules-only policy: picks the next tool from the case snapshot and writes plain replies."""

    name = "scripted-rules/1"

    async def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> LLMResponse:
        results = _tool_results(messages)
        case_id = _case_id(messages)
        text = _last_customer_text(messages)
        needs = results.get("get_confirmed_needs")
        if needs is None:
            return LLMResponse(tool_calls=[ToolCall(uuid.uuid4().hex, "get_confirmed_needs", {"case_id": case_id})])
        if "error" in needs:
            return LLMResponse(text="I could not load this case: %s" % needs["error"])

        status = needs["case_status"]
        if needs["contradictions"] and "escalated" not in results:
            lines = ["Before I go further I need to resolve a contradiction in your confirmed answers:"]
            for c in needs["contradictions"]:
                lines.append("- %s" % c["explanation"])
            lines.append("Please tell me which answer is correct; I will not guess and I have flagged this for review.")
            return LLMResponse(text="\n".join(lines), usage={"escalate": 1})

        # Explicit selection: "select quote_xxx" -> prepare an application for review.
        selection = re.search(r"(quote_[0-9a-f]{8,})", text)
        if selection and status in ("awaiting_selection", "comparing") and "prepare_application" not in results:
            return LLMResponse(tool_calls=[ToolCall(uuid.uuid4().hex, "prepare_application", {"case_id": case_id, "quote_id": selection.group(1), "answers_version": needs["answers_version"]})])
        if "prepare_application" in results:
            prep = results["prepare_application"]
            if "error" in prep:
                details = prep.get("details") or {}
                if details.get("missing_questions"):
                    qs = "\n".join("- %s (%s)" % (q["text"], q["question_id"]) for q in details["missing_questions"])
                    return LLMResponse(text="Before I can prepare that application the insurer needs these answers, in its own words:\n%s\nAnswer yes, no, or 'I don't know'." % qs)
                return LLMResponse(text="I could not prepare that application: %s" % prep["error"])
            review = prep["review"]
            return LLMResponse(
                text=(
                    "I prepared the application for %s for your review (nothing has been sent).\n"
                    "Premium: %s. Property %s, liability %s, deductible %s, replacement cost: %s, coverage start %s.\n"
                    "Exclusions: %s.\nIt includes %d confirmed answers. Review the exact terms and approve or reject on the review screen; "
                    "I cannot approve on your behalf. The approval challenge expires at %s."
                )
                % (
                    review["destination"]["insurer_name"], review["amount"]["display"], review["terms"]["property_limit"], review["terms"]["liability_limit"],
                    review["terms"]["deductible"], "yes" if review["terms"]["replacement_cost"] else "no", review["terms"]["effective_date"],
                    ", ".join(review["terms"]["exclusions"]), len(review["answers"]), review["expires_at"],
                )
            )

        if status in ("collecting", "expired"):
            missing = [f for f in needs["missing_fields"] if f != "deductible_preference"]
            if missing:
                return LLMResponse(text="To request quotes I still need: %s. Tell me each value, or say 'unknown' if you are not sure." % ", ".join(missing))
            if "request_quotes" not in results:
                return LLMResponse(tool_calls=[ToolCall(uuid.uuid4().hex, "request_quotes", {"case_id": case_id, "needs_version": needs["needs_version"]})])
            if "error" in results["request_quotes"]:
                return LLMResponse(text="I could not request quotes: %s" % results["request_quotes"]["error"])
            # Quotes were just requested: refresh the case snapshot before explaining what happened.
            return LLMResponse(tool_calls=[ToolCall(uuid.uuid4().hex, "get_confirmed_needs", {"case_id": case_id})])

        if status == "needs_information":
            blocking = [q for q in needs["unanswered_questions"] if q["reason"] == "blocks_quote"]
            lines = ["One insurer needs more information before it can quote. In its own words:"]
            for q in blocking:
                lines.append("- %s asks: \"%s\" (%s)" % (q.get("insurer_name", q["insurer_id"]), q["text"], q["question_id"]))
            lines.append("Answer yes, no, or 'I don't know'. I will not guess for you.")
            return LLMResponse(text="\n".join(lines))

        if status == "quoting":
            waiting = [t for t in needs["quote_tasks"] if t["status"] not in ("quoted", "declined")]
            return LLMResponse(text="Quotes are being requested. Still waiting on: %s. I will compare as soon as at least one quote arrives." % ", ".join("%s (%s)" % (t["insurer_name"], t["status"]) for t in waiting))

        if status in ("awaiting_selection", "comparing"):
            if needs["missing_for_comparison"]:
                return LLMResponse(text="Before I can shortlist quotes I need: %s." % ", ".join(needs["missing_for_comparison"]))
            if "compare_coverage" not in results:
                return LLMResponse(tool_calls=[ToolCall(uuid.uuid4().hex, "compare_coverage", {"case_id": case_id, "needs_version": needs["needs_version"]})])
            return LLMResponse(text=_explain_comparison(results["compare_coverage"], needs))

        if status == "awaiting_approval":
            action = needs.get("pending_action") or {}
            review = action.get("review") or {}
            if action.get("status") == "approved":
                return LLMResponse(text="Your approval is recorded and the executor is submitting the application to %s. I will report the insurer's decision; a submission is not yet a policy." % review.get("destination", {}).get("insurer_name", "the insurer"))
            kind = "revised offer" if action.get("type") == "accept_revised_offer" else "application"
            extra = ""
            if action.get("type") == "accept_revised_offer":
                extra = " Underwriting changed the premium from %s to %s (%s): %s." % (review.get("previous_premium_display"), review.get("amount", {}).get("display"), review.get("premium_change_display"), review.get("revision_reason"))
            return LLMResponse(text="A %s for %s at %s is waiting for your review.%s Approve or reject it on the review screen; I cannot approve for you." % (kind, review.get("destination", {}).get("insurer_name"), review.get("amount", {}).get("display"), extra))

        if status in ("submitted", "underwriting", "bound", "issued", "completed", "declined", "manual_review"):
            app = needs.get("application")
            if app and "verify_policy" not in results:
                return LLMResponse(tool_calls=[ToolCall(uuid.uuid4().hex, "verify_policy", {"application_id": app["application_id"]})])
            verify = results.get("verify_policy") or {}
            if status == "completed" and verify.get("verified"):
                return LLMResponse(text="Your policy %s is issued and verified against the approved terms. Coverage starts %s; it is not in force before that date." % (verify.get("insurer_policy_ref"), verify.get("effective_at")))
            if status == "declined":
                return LLMResponse(text="The insurer declined the application after underwriting. You can select another suitable quote if one is available.")
            if status == "manual_review":
                mism = (verify.get("verification") or {}).get("mismatches") or []
                if mism:
                    return LLMResponse(text="The insurer issued a policy but it does not match what you approved (%s). An operator is reviewing it; I will not present this coverage as active." % "; ".join(x["detail"] for x in mism))
                return LLMResponse(text="This case is with an operator for review: %s" % needs.get("next_decision"))
            return LLMResponse(text="Your application is with the insurer (%s). Nothing is bound yet; I will ask you again if the terms change." % status)

        return LLMResponse(text=needs.get("next_decision") or "Nothing to do right now.")


def _explain_comparison(comparison: Dict[str, Any], needs: Dict[str, Any]) -> str:
    lines: List[str] = []
    if comparison.get("suitable"):
        lines.append("Quotes that meet all of your requirements (ranked by %s; this is a product rule, not an actuarial assessment):" % comparison["ranking"]["preference"].replace("_", " "))
        for s in comparison["suitable"]:
            lines.append("%d. %s — %s/year, deductible %s, form %s, valid until %s (quote id %s)" % (s["rank"], s["insurer_name"], s["annual_premium_display"], fmt_minor(s["deductible_minor"]), s["policy_form_version"], s["valid_until"][:10], s["quote_id"]))
    else:
        lines.append("No quote currently meets all of your requirements.")
    for e in comparison.get("excluded", []):
        cites = [c["citation"]["clause_id"] for c in e["failed_checks"] if c.get("citation")]
        lines.append("Excluded: %s at %s — %s%s" % (e["insurer_name"], e["annual_premium_display"], e["reason"], (" [%s]" % ", ".join(cites)) if cites else ""))
    for u in comparison.get("undetermined", []):
        lines.append("Cannot be shortlisted yet: %s — %s" % (u["insurer_name"], u["reason"]))
    for mr in comparison.get("missing_responses", []):
        lines.append("Missing: %s" % mr["disclosure"])
    blocking = [q for q in needs.get("unanswered_questions", []) if q["reason"] == "blocks_quote"]
    for q in blocking:
        lines.append("%s asks, in its own words: \"%s\" (%s). Answer yes, no, or 'I don't know'." % (q.get("insurer_name", q["insurer_id"]), q["text"], q["question_id"]))
    for t in comparison.get("trade_offs", []):
        lines.append("Trade-off: %s" % t)
    pending = [q for q in needs.get("unanswered_questions", []) if q["reason"] == "required_for_application"]
    if pending:
        lines.append("Before an application can be prepared, insurers still need answers (in their own words): " + "; ".join("%s: \"%s\" (%s)" % (q.get("insurer_name", q["insurer_id"]), q["text"], q["question_id"]) for q in pending[:6]))
    lines.append("Tell me 'select <quote id>' to prepare an application for review.")
    return "\n".join(lines)


class OpenAICompatibleProvider:
    """Minimal client for OpenAI-style /chat/completions with tool calling."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.name = "openai-compatible/%s" % model

    async def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> LLMResponse:
        payload = {"model": self.model, "messages": _to_openai_messages(messages), "tools": tools, "tool_choice": "auto", "temperature": 0}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.base_url + "/chat/completions", json=payload, headers={"Authorization": "Bearer " + self.api_key})
            response.raise_for_status()
            data = response.json()
        choice = data["choices"][0]["message"]
        calls = []
        for tc in choice.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(tc.get("id") or uuid.uuid4().hex, tc["function"]["name"], args))
        return LLMResponse(text=choice.get("content"), tool_calls=calls, usage=data.get("usage") or {})


def _to_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for msg in messages:
        if msg["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": msg["tool_call_id"], "content": msg["content"]})
        elif msg["role"] == "assistant" and msg.get("tool_calls"):
            out.append({"role": "assistant", "content": msg.get("content"), "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in msg["tool_calls"]]})
        else:
            out.append({"role": msg["role"], "content": msg.get("content") or ""})
    return out


def build_provider(settings) -> LLMProvider:
    if settings.llm_provider == "openai_compatible" and settings.llm_api_key:
        return OpenAICompatibleProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    return ScriptedProvider()
