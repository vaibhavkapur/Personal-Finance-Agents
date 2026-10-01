"""Agent orchestrator: one customer turn = a bounded tool-calling loop.

The case record (not chat history) is the conversation state. Each turn
loads the current case version, outstanding questions and provider
references, lets the policy call typed tools within a budget, and returns a
final answer with the evidence trail. Unresolved contradictions or budget
exhaustion escalate with evidence rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from app.agent.llm import FinalAnswer, Policy, ToolCall, ToolResult, Turn, default_policy
from app.agent.tools import ToolContext, run_tool, tool_schemas
from app.config import settings
from app.persistence.db import session_scope
from app.persistence.models import Case
from app.workflows import case_service, states


@dataclass
class TurnOutcome:
    reply: str
    case_id: str | None
    state: str | None
    tool_calls: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    refused: bool = False
    escalated: bool = False
    budget_exhausted: bool = False
    model_version: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _load_case(customer_id: str, case_id: str | None) -> dict | None:
    with session_scope() as session:
        case = None
        if case_id:
            case = session.get(Case, case_id)
            if case is not None and case.customer_id != customer_id:
                case = None
        if case is None:
            case = session.scalars(
                select(Case).where(Case.customer_id == customer_id, Case.state.notin_(list(states.TERMINAL))).order_by(Case.created_at.desc())
            ).first()
        return case_service.case_summary(session, case) if case else None


async def run_turn(customer_id: str, message: str, case_id: str | None = None, policy: Policy | None = None) -> TurnOutcome:
    policy = policy or default_policy()
    case = _load_case(customer_id, case_id)
    ctx = ToolContext(customer_id=customer_id, case_id=case["id"] if case else None, model_version=policy.model_version)
    turn = Turn(customer_id=customer_id, user_message=message, case=case)
    schemas = tool_schemas()
    calls: list[dict] = []

    for _ in range(settings.agent_tool_budget + 1):
        decision = await policy.decide(turn, schemas)
        if isinstance(decision, FinalAnswer):
            return TurnOutcome(
                reply=decision.text,
                case_id=ctx.case_id,
                state=turn.case["state"] if turn.case else None,
                tool_calls=calls,
                questions=decision.questions,
                refused=decision.refused,
                escalated=decision.escalated,
                model_version=policy.model_version,
            )
        assert isinstance(decision, ToolCall)
        if ctx.calls >= ctx.budget:
            break
        result = await run_tool(ctx, decision.name, decision.arguments)
        calls.append({"tool": decision.name, "arguments": decision.arguments, "outcome": "error" if isinstance(result, dict) and result.get("error") else "ok"})
        turn.tool_results.append(ToolResult(decision.name, decision.arguments, result))
        turn.case = _load_case(customer_id, ctx.case_id)
        if turn.case and not ctx.case_id:
            ctx.case_id = turn.case["id"]

    evidence = "; ".join(f"{c['tool']}({c['outcome']})" for c in calls)
    return TurnOutcome(
        reply=f"I've reached my tool-call limit for this turn without resolving the request. Steps taken: {evidence}. An operator can review the case and tool log.",
        case_id=ctx.case_id,
        state=turn.case["state"] if turn.case else None,
        tool_calls=calls,
        escalated=True,
        budget_exhausted=True,
        model_version=policy.model_version,
    )
