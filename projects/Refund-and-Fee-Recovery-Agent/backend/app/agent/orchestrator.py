"""Agent orchestrator.

Runs the model/tool loop for one case under a hard tool-call budget, persists
tool runs, and applies a deterministic claim guardrail to the final message so
that the agent can never announce recovery the case state does not support.
A resumed conversation loads the case, not the chat history.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..domain.errors import DomainError
from ..ids import new_id
from ..workflows.case_service import CaseService
from .llm import ModelDecision, RulesPlanner, ToolCallingModel
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT
from .tools import ToolContext, run_tool, tool_schemas

RECOVERY_CLAIM_PATTERNS = [
    re.compile(r"\b(has been|was|is) (fully )?(refunded|recovered)\b", re.I),
    re.compile(r"\byour (refund|money) (is|has) (back|arrived|been returned)\b", re.I),
    re.compile(r"\brecovery (is )?complete\b", re.I),
]
RECOVERED_STATES = {"recovered", "already_refunded"}


@dataclass
class AgentTurn:
    case_id: str
    status: str
    summary: str
    questions: List[Dict[str, Any]] = field(default_factory=list)
    proposed_action: Optional[Dict[str, Any]] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    escalation: Optional[Dict[str, Any]] = None
    guardrail: Dict[str, Any] = field(default_factory=dict)
    model_version: str = ""
    prompt_version: str = PROMPT_VERSION

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Orchestrator:
    def __init__(self, service: CaseService, *, model: Optional[ToolCallingModel] = None, max_tool_calls: int = 8) -> None:
        self.service = service
        self.model = model or RulesPlanner()
        self.max_tool_calls = max_tool_calls

    def run(self, *, case_id: str, customer_id: str, user_message: Optional[str] = None) -> AgentTurn:
        case = self.service._owned_case(case_id, customer_id)
        ctx = ToolContext(service=self.service, customer_id=customer_id, model_version=self.model.version)
        transcript: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "case_id": case.id, "content": user_message or "Check whether the promised refund arrived and follow up if it did not."},
        ]
        calls_made: List[Dict[str, Any]] = []
        decision = ModelDecision()
        escalation: Optional[Dict[str, Any]] = None
        while True:
            decision = self.model.decide(system_prompt=SYSTEM_PROMPT, transcript=transcript, tools=tool_schemas())
            if not decision.tool_calls:
                break
            for call in decision.tool_calls:
                if len(calls_made) >= self.max_tool_calls:
                    escalation = {"reason": "tool_budget_exhausted", "budget": self.max_tool_calls, "evidence": [c["name"] for c in calls_made]}
                    break
                result = run_tool(ctx, call.name, call.arguments, case_id=case.id)
                call_id = new_id("call")
                calls_made.append({"call_id": call_id, "name": call.name, "arguments": call.arguments, "ok": "error" not in result, "error": result.get("error")})
                transcript.append({"role": "tool", "call_id": call_id, "name": call.name, "arguments": call.arguments, "result": result})
            if escalation:
                break
        status = self.service.get_status(case.id, customer_id=customer_id)
        summary = decision.final_message or ""
        if escalation:
            summary = (summary + " " if summary else "") + f"I stopped after {self.max_tool_calls} tool calls without a resolution; an operator should review the evidence: {escalation['evidence']}."
        guardrail = self._guardrail(summary, status["status"])
        if guardrail["blocked"]:
            summary = guardrail["replacement"]
        proposed = None
        for entry in reversed(transcript):
            if entry.get("role") == "tool" and entry["name"] in ("prepare_recovery_message", "prepare_dispute_packet") and "error" not in entry["result"]:
                d = entry["result"]["data"]
                proposed = {"action_id": d["action_id"], "type": d["type"], "status": d["status"], "review": d["review"], "approval_challenge_id": d["approval_challenge_id"], "payload_hash": d["payload_hash"], "expected_case_version": d["expected_case_version"]}
                break
        self.service._event(case, "agent.turn", f"agent:{customer_id}", {"tool_calls": [c["name"] for c in calls_made], "questions": [q.get("kind") for q in decision.questions], "escalation": escalation, "guardrail_blocked": guardrail["blocked"], "model_version": self.model.version, "prompt_version": PROMPT_VERSION})
        return AgentTurn(case_id=case.id, status=status["status"], summary=summary, questions=decision.questions, proposed_action=proposed, tool_calls=calls_made,
                         escalation=escalation or decision.escalation, guardrail=guardrail, model_version=self.model.version)

    @staticmethod
    def _guardrail(summary: str, status: str) -> Dict[str, Any]:
        """Deterministic check: a recovery claim is only allowed when the state supports it."""
        claims = [p.pattern for p in RECOVERY_CLAIM_PATTERNS if p.search(summary)]
        blocked = bool(claims) and status not in RECOVERED_STATES
        return {
            "blocked": blocked,
            "claims_detected": claims,
            "case_status": status,
            "replacement": f"The case is `{status}`. I cannot confirm recovery until a final credit posts to the account. Please review the timeline for the current evidence." if blocked else None,
        }
