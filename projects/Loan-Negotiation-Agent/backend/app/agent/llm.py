"""Optional LLM planner behind the same ``Planner`` interface.

Uses an OpenAI-compatible chat-completions endpoint with structured tool
calling. It is only enabled when ``LLM_API_KEY`` is set; otherwise the
deterministic ``RulesPlanner`` runs. The LLM never receives credentials,
never computes payments, and can only choose among the typed tools. Any
failure falls back to the rules planner for that step.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from .orchestrator import PlanStep, RulesPlanner, TurnContext
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS

# Illustrative pricing per 1M tokens; override with LLM_PRICE_IN/LLM_PRICE_OUT if needed.
DEFAULT_PRICE_IN = 0.15
DEFAULT_PRICE_OUT = 0.60


class LLMPlanner:
    name = f"llm-planner-{PROMPT_VERSION}"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 30.0, price_in: float = DEFAULT_PRICE_IN, price_out: float = DEFAULT_PRICE_OUT):
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.fallback = RulesPlanner()
        self.tokens_in = 0
        self.tokens_out = 0
        self.price_in = price_in
        self.price_out = price_out
        self.errors: List[str] = []

    def cost_usd(self) -> float:
        return (self.tokens_in * self.price_in + self.tokens_out * self.price_out) / 1_000_000

    def _messages(self, ctx: TurnContext) -> List[Dict[str, Any]]:
        view = ctx.case_view
        compact = {
            "case_id": view["id"],
            "status": view["status"],
            "version": view["version"],
            "missing_fields": view["missing_fields"],
            "outstanding_questions": [q["question"] for q in view["outstanding_questions"]],
            "offers": [{"id": o["id"], "lender_id": o["lender_id"], "version": o["version"], "status": o["status"], "rate": o["note_rate_decimal"], "term": o["term_months"]} for o in view["offers"]],
            "recommendation": (view.get("comparison") or {}).get("recommendation"),
            "pending_actions": [{"id": a["action_id"], "type": a["action_type"], "status": a["status"]} for a in view.get("pending_actions", [])],
            "applications": [{"id": a["id"], "status": a["status"]} for a in view.get("applications", [])],
            "lenders": {lid: l["name"] for lid, l in ctx.lenders.items()},
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Case context (authoritative):\n{json.dumps(compact, default=str)}\n\nBorrower says: {ctx.message or '(no message)'}\nStructured intent: {ctx.intent} params: {json.dumps(ctx.params, default=str)}"},
        ]
        for r in ctx.tool_results:
            messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{len(messages)}", "type": "function", "function": {"name": r.tool, "arguments": "{}"}}]})
            messages.append({"role": "tool", "tool_call_id": f"call_{len(messages) - 1}", "content": json.dumps({"ok": r.ok, "error": r.error, "authority": r.authority, "data": r.data}, default=str)[:6000]})
        return messages

    def next_step(self, ctx: TurnContext) -> PlanStep:
        if ctx.resolved_intent is None:
            ctx.resolved_intent = self.fallback.resolve_intent(ctx)
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": self._messages(ctx),
                    "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}} for t in TOOL_SCHEMAS],
                    "tool_choice": "auto",
                    "temperature": 0,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
            usage = body.get("usage", {})
            self.tokens_in += int(usage.get("prompt_tokens", 0))
            self.tokens_out += int(usage.get("completion_tokens", 0))
            choice = body["choices"][0]["message"]
            tool_calls = choice.get("tool_calls") or []
            if tool_calls:
                fn = tool_calls[0]["function"]
                args = json.loads(fn.get("arguments") or "{}")
                args.pop("case_id", None)
                return PlanStep(kind="tool", tool=fn["name"], args=args)
            content = (choice.get("content") or "").strip()
            if not content:
                return self.fallback.next_step(ctx)
            return PlanStep(kind="reply", reply=content)
        except Exception as exc:  # noqa: BLE001 - degrade to rules
            self.errors.append(str(exc))
            return self.fallback.next_step(ctx)


def build_planner(settings) -> Any:
    if settings.llm_api_key:
        return LLMPlanner(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
    return RulesPlanner()
