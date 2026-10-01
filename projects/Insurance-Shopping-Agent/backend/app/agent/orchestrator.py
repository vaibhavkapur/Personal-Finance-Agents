"""Agent orchestrator: bounded tool-calling loop over durable case state.

A resumed conversation loads the current case (via tools), not the chat history, to
decide what to do next. The orchestrator escalates when the tool budget is exhausted or
when confirmed answers contradict each other.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..context import AppContext
from ..persistence import models as m
from ..persistence import repositories as repo
from ..persistence.db import new_id
from ..workflows.case_service import CaseError
from ..workflows.states import record_event
from .llm import LLMProvider, ScriptedProvider
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS, AgentTools, openai_tool_specs


class AgentOrchestrator:
    def __init__(self, ctx: AppContext, provider: Optional[LLMProvider] = None) -> None:
        self.ctx = ctx
        self.provider = provider or ScriptedProvider()

    async def handle_message(self, case_id: str, customer_id: str, text: str) -> Dict[str, Any]:
        now = self.ctx.now()
        with self.ctx.db.session() as session:
            case = repo.get_case_for_customer(session, case_id, customer_id)
            history = repo.messages_for_case(session, case.id)
            session.add(m.ConversationMessage(id=new_id("msg"), case_id=case.id, role="customer", content=text, created_at=now))
        tools = AgentTools(self.ctx, customer_id, model_version=self.provider.name)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": "case_id=%s customer_id=%s environment=%s" % (case_id, customer_id, self.ctx.environment)},
        ]
        for msg in history[-10:]:
            if msg.role in ("customer", "agent"):
                messages.append({"role": "user" if msg.role == "customer" else "assistant", "content": msg.content})
        messages.append({"role": "user", "content": text})

        budget = self.ctx.settings.tool_call_budget
        calls_made: List[Dict[str, Any]] = []
        reply: Optional[str] = None
        escalated = False
        specs = openai_tool_specs()
        while True:
            response = await self.provider.complete(messages, specs)
            if response.usage.get("escalate"):
                escalated = True
            if not response.tool_calls:
                reply = response.text or ""
                break
            if len(calls_made) + len(response.tool_calls) > budget:
                escalated = True
                reply = ("I reached my tool-call budget without resolving this. I have escalated the case with the evidence gathered so far; "
                         "an operator will follow up. Nothing has been submitted or approved.")
                break
            messages.append({"role": "assistant", "content": response.text, "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls]})
            for call in response.tool_calls:
                result = await tools.call(call.name, call.arguments)
                calls_made.append({"name": call.name, "arguments": call.arguments, "ok": "error" not in result})
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result, default=str)})

        with self.ctx.db.session() as session:
            case = repo.get_case(session, case_id)
            session.add(m.ConversationMessage(id=new_id("msg"), case_id=case.id, role="agent", content=reply or "", tool_calls_json=calls_made, created_at=self.ctx.now()))
            if escalated:
                record_event(session, case, "insurance.agent.escalated", "agent", self.ctx.now(), self.ctx.environment,
                             {"reason": "tool budget exhausted" if len(calls_made) >= budget else "contradictory confirmed answers", "tool_calls": calls_made})
            status = case.state
        return {"reply": reply, "tool_calls": calls_made, "escalated": escalated, "case_status": status, "model_version": self.provider.name}

    def history(self, case_id: str, customer_id: str) -> List[Dict[str, Any]]:
        with self.ctx.db.session() as session:
            repo.get_case_for_customer(session, case_id, customer_id)
            return [{"id": msg.id, "role": msg.role, "content": msg.content, "tool_calls": msg.tool_calls_json, "at": msg.created_at.isoformat()} for msg in repo.messages_for_case(session, case_id)]
