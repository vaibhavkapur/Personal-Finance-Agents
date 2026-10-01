"""Agent orchestrator: runs one conversation turn with a bounded tool-call budget and records every tool run.
The orchestrator never mutates case state directly; only tools (which call application code) do."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..clock import Clock
from ..config import Settings
from ..ids import new_id
from ..persistence.models import AgentTurn, ClaimCase
from ..workflows.approvals import PrincipalView
from ..workflows.case_service import CaseService
from ..workflows.errors import Forbidden, NotFound
from .planner import FinalAnswer, LLMPlanner, OpenAICompatibleProvider, ScriptedPlanner, ToolCall, TurnState
from .tools import Toolbox


class AgentOrchestrator:
    def __init__(self, db, clock: Clock, settings: Settings, cases: CaseService, adapter_env: str):
        self.db = db
        self.clock = clock
        self.settings = settings
        self.cases = cases
        self.adapter_env = adapter_env

    def _planner(self, planner_name: Optional[str]):
        name = planner_name or self.settings.planner
        if name == "llm":
            if not (self.settings.llm_base_url and self.settings.llm_api_key and self.settings.llm_model):
                raise Forbidden("LLM planner requested but LLM_BASE_URL/LLM_API_KEY/LLM_MODEL are not configured")
            return LLMPlanner(OpenAICompatibleProvider(self.settings.llm_base_url, self.settings.llm_api_key, self.settings.llm_model))
        return ScriptedPlanner()

    def run_turn(self, principal: PrincipalView, case_id: str, customer_message: str = "", planner_name: Optional[str] = None) -> Dict[str, Any]:
        with self.db.session() as s:
            case = s.get(ClaimCase, case_id)
            if not case:
                raise NotFound("case not found")
            self.cases._authorize(principal, case)
        planner = self._planner(planner_name)
        toolbox = Toolbox(self.db, self.clock, self.cases, principal, self.adapter_env)
        turn_id = new_id("turn")
        state = TurnState(case_id=case_id, customer_message=customer_message)
        model_version = planner.name if planner.name != "llm" else f"llm:{self.settings.llm_model}"
        calls = 0
        final: Optional[FinalAnswer] = None
        while calls < self.settings.tool_call_budget:
            step = planner.next(state, toolbox.schemas())
            if isinstance(step, FinalAnswer):
                final = step
                break
            assert isinstance(step, ToolCall)
            call_id = step.arguments.pop("_call_id", None)
            result = toolbox.call(step.name, step.arguments, case_id=case_id, turn_id=turn_id, model_version=model_version)
            result["_arguments"] = step.arguments
            if call_id:
                result["_call_id"] = call_id
            state.tool_results.append(result)
            calls += 1
        if final is None:
            final = FinalAnswer("I reached my tool-call budget for this turn without finishing. The case is saved; an operator can review the tool log.", {"budget_exhausted": True})
        response = {"turn_id": turn_id, "case_id": case_id, "planner": planner.name, "message": final.message, "data": final.data, "tool_calls": calls, "tools_used": [r.get("tool") for r in state.tool_results]}
        with self.db.session() as s:
            s.add(AgentTurn(id=turn_id, case_id=case_id, actor=principal.id, customer_message=customer_message, response_json={"message": final.message, "tools_used": response["tools_used"]}, tool_calls=calls, planner=planner.name, created_at=self.clock.now_iso()))
        return response
