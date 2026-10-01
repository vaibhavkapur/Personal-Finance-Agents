"""Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0) exposing the claim tools:
policy retrieval, receipt extraction, packet construction and claim status.

Protocol revision pinned to 2025-06-18. The official Python SDK needs Python >= 3.10, so this module implements only the
subset needed (initialize, ping, tools/list, tools/call) without extra dependencies.

Run: python -m app.agent.mcp_server  (env: DATABASE_URL, MCP_PRINCIPAL_TOKEN)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, Dict, Optional

from sqlalchemy import select

from ..config import load_settings
from ..context import AppContext
from ..persistence.models import Principal
from ..workflows.approvals import PrincipalView
from ..workflows.errors import DomainError
from .tools import Toolbox, ToolError

MCP_PROTOCOL_VERSION = "2025-06-18"
EXPOSED_TOOLS = ["get_policy", "extract_claim_evidence", "build_claim_packet", "get_case_status", "evaluate_policy_facts", "reconcile_claim_payment"]


def resolve_principal(ctx: AppContext, token: str) -> PrincipalView:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with ctx.db.session() as s:
        row = s.scalars(select(Principal).where(Principal.token_hash == digest)).first()
        if not row:
            raise PermissionError("unknown MCP principal token")
        return PrincipalView(id=row.id, role=row.role, customer_id=row.customer_id, display_name=row.display_name)


class MCPServer:
    def __init__(self, ctx: AppContext, principal: PrincipalView):
        self.ctx = ctx
        self.principal = principal
        self.toolbox = Toolbox(ctx.db, ctx.clock, ctx.cases, principal, ctx.adapter.capabilities.environment)
        self.initialized = False

    def tools_list(self) -> Dict[str, Any]:
        tools = []
        for name in EXPOSED_TOOLS:
            spec = self.toolbox.specs[name]
            tools.append({"name": spec.name, "description": spec.description + f" (authority: {spec.authority})", "inputSchema": spec.parameters})
        return {"tools": tools}

    def tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in EXPOSED_TOOLS:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        result = self.toolbox.call(name, arguments, case_id=arguments.get("case_id"), turn_id=None, model_version="mcp-client")
        is_error = "error" in result
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result, "isError": is_error}

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = message.get("method")
        rid = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.initialized = True
            return self._result(rid, {
                "protocolVersion": MCP_PROTOCOL_VERSION if requested != MCP_PROTOCOL_VERSION else requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "insurance-claims-advocate", "version": "0.1.0"},
                "instructions": "Claim tools for a delayed-baggage advocate. Results are estimates or simulated unless marked authoritative; nothing here submits a claim.",
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._result(rid, {})
        if method == "tools/list":
            return self._result(rid, self.tools_list())
        if method == "tools/call":
            try:
                return self._result(rid, self.tools_call(params))
            except (DomainError, ToolError) as exc:
                return self._result(rid, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    @staticmethod
    def _result(rid: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def serve_stdio(self) -> None:  # pragma: no cover - exercised via subprocess test
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}) + "\n")
                sys.stdout.flush()
                continue
            response = self.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main() -> None:  # pragma: no cover
    settings = load_settings()
    ctx = AppContext(settings)
    token = os.environ.get("MCP_PRINCIPAL_TOKEN", "tok_customer_demo_3")
    principal = resolve_principal(ctx, token)
    MCPServer(ctx, principal).serve_stdio()


if __name__ == "__main__":  # pragma: no cover
    main()
