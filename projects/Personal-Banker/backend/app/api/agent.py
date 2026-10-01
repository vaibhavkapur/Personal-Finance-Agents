from __future__ import annotations

from fastapi import APIRouter, Depends

from app.agent.orchestrator import run_turn
from app.agent.tools import tool_schemas
from app.api.deps import Principal, get_principal
from app.api.schemas import AgentTurnRequest
from app.workflows.errors import CaseError

router = APIRouter(prefix="/v1/agent", tags=["agent"])


@router.post("/turns")
async def agent_turn(body: AgentTurnRequest, principal: Principal = Depends(get_principal)):
    if principal.role != "customer":
        raise CaseError(403, "customer_only", "the agent acts only for the authenticated customer")
    outcome = await run_turn(principal.subject, body.message, case_id=body.case_id)
    return outcome.to_dict()


@router.get("/tools")
def tools():
    return tool_schemas()
