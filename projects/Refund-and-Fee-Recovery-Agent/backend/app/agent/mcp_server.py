"""Minimal MCP server (stdio, JSON-RPC 2.0) exposing the typed case tools.

Pinned to MCP protocol revision 2025-06-18 message shapes for `initialize`,
`tools/list` and `tools/call`. The server is bound to one authenticated
customer via MCP_CUSTOMER_ID; it never exposes provider credentials or
approval endpoints.

Run:  MCP_CUSTOMER_ID=cus_demo_4 python -m backend.app.agent.mcp_server
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

from ..container import build_container
from .tools import ToolContext, run_tool, tool_schemas

PROTOCOL_VERSION = "2025-06-18"


class McpServer:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}
        if method == "initialize":
            return self._ok(req_id, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                                     "serverInfo": {"name": "refund-recovery-agent", "version": "0.1.0"}})
        if method == "notifications/initialized" or method == "ping":
            return self._ok(req_id, {}) if req_id is not None else None
        if method == "tools/list":
            return self._ok(req_id, {"tools": tool_schemas()})
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            result = run_tool(self.ctx, name, arguments)
            is_error = "error" in result
            return self._ok(req_id, {"content": [{"type": "text", "text": json.dumps(result, default=str)}], "isError": is_error})
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}}

    @staticmethod
    def _ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}


def serve_stdio() -> None:
    customer_id = os.environ.get("MCP_CUSTOMER_ID")
    if not customer_id:
        sys.stderr.write("MCP_CUSTOMER_ID is required; the MCP session is scoped to one authenticated customer\n")
        sys.exit(2)
    container = build_container()
    server = McpServer(ToolContext(service=container.service, customer_id=customer_id, model_version="mcp-client"))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        response = server.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, default=str) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve_stdio()
