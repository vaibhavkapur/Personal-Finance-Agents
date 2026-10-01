"""MCP server exposing the typed banking tools (plan §5, §6).

The server is scoped to one authenticated customer whose id comes from the
process environment (``PB_MCP_CUSTOMER_ID``), never from the model. Provider
credentials stay inside adapters. Every tool result includes ``_meta`` with
source, retrieval time and authority.

Run with::

    PB_MCP_CUSTOMER_ID=cus_demo_1 python -m app.agent.mcp_server

MCP SDK version is pinned in ``uv.lock`` (mcp 2.x, ``MCPServer`` API).
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.agent.tools import ToolContext, run_tool
from app.persistence.db import create_schema

MCP_PROTOCOL_NOTE = "Tools follow the Model Context Protocol; the SDK's pinned protocol revision is used."


def build_server(customer_id: str | None = None) -> MCPServer:
    customer_id = customer_id or os.environ.get("PB_MCP_CUSTOMER_ID")
    if not customer_id:
        raise RuntimeError("PB_MCP_CUSTOMER_ID must identify the authenticated customer")
    server = MCPServer(
        name="personal-banker",
        instructions=(
            "Typed, scoped banking tools for one customer's CD maturity case. "
            "Tools read data or prepare a proposed action; they never approve or execute."
        ),
        version="0.1.0",
    )

    def ctx() -> ToolContext:
        return ToolContext(customer_id=customer_id, model_version="mcp-client")

    @server.tool(name="read_cash_snapshot", description="Available vs current balances, pending entries, ownership, deposits and obligations with source timestamps.")
    async def read_cash_snapshot(as_of: str | None = None) -> dict[str, Any]:
        return await run_tool(ctx(), "read_cash_snapshot", {"as_of": as_of} if as_of else {})

    @server.tool(name="list_maturity_options", description="Provider-sourced product versions for the deposit on an account, with expiry and eligibility.")
    async def list_maturity_options(account_id: str) -> dict[str, Any]:
        return await run_tool(ctx(), "list_maturity_options", {"account_id": account_id})

    @server.tool(name="project_cash", description="Deterministic date-by-date cash model; returns the lowest projected available balance and buffer breaches.")
    async def project_cash(case_id: str, allocation_minor: int | None = None, offer_id: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"case_id": case_id}
        if allocation_minor is not None:
            args["allocation_minor"] = allocation_minor
        if offer_id:
            args["offer_id"] = offer_id
        return await run_tool(ctx(), "project_cash", args)

    @server.tool(name="open_maturity_case", description="Open a CD maturity case with a minimum buffer and obligations to preserve.")
    async def open_maturity_case(deposit_id: str, minimum_buffer_minor: int, obligation_ids: list[str] | None = None, preferred_lockup_days: int | None = None, buffer_includes_obligations: bool | None = None) -> dict[str, Any]:
        return await run_tool(ctx(), "open_maturity_case", {"deposit_id": deposit_id, "minimum_buffer_minor": minimum_buffer_minor, "obligation_ids": obligation_ids or [], "preferred_lockup_days": preferred_lockup_days, "buffer_includes_obligations": buffer_includes_obligations})

    @server.tool(name="record_customer_answers", description="Record the customer's answers to outstanding questions.")
    async def record_customer_answers(case_id: str, preferred_lockup_days: int | None = None, buffer_includes_obligations: bool | None = None, minimum_buffer_minor: int | None = None) -> dict[str, Any]:
        return await run_tool(ctx(), "record_customer_answers", {"case_id": case_id, "preferred_lockup_days": preferred_lockup_days, "buffer_includes_obligations": buffer_includes_obligations, "minimum_buffer_minor": minimum_buffer_minor})

    @server.tool(name="evaluate_case", description="Refresh balances and offers, project cash and compare options. Produces options, never a transfer.")
    async def evaluate_case(case_id: str) -> dict[str, Any]:
        return await run_tool(ctx(), "evaluate_case", {"case_id": case_id})

    @server.tool(name="prepare_bank_instruction", description="Create an immutable proposed action for customer approval. Cannot approve or execute.")
    async def prepare_bank_instruction(case_id: str, option_id: str, plan_id: str | None = None, amount_minor: int | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"case_id": case_id, "option_id": option_id}
        if plan_id:
            args["plan_id"] = plan_id
        if amount_minor is not None:
            args["amount_minor"] = amount_minor
        return await run_tool(ctx(), "prepare_bank_instruction", args)

    @server.tool(name="get_instruction_status", description="Bank references, state and reconciliation evidence for a case's instruction.")
    async def get_instruction_status(case_id: str | None = None, instruction_id: str | None = None) -> dict[str, Any]:
        return await run_tool(ctx(), "get_instruction_status", {"case_id": case_id, "instruction_id": instruction_id})

    return server


if __name__ == "__main__":  # pragma: no cover
    create_schema()
    build_server().run(transport="stdio")
