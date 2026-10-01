"""Minimal MCP server (stdio, JSON-RPC 2.0) exposing the customer-scoped tools.

Pinned protocol revision: 2025-06-18. Implements initialize, ping, tools/list and
tools/call, which is all a tool-only server needs. The customer scope comes from
MCP_CUSTOMER_TOKEN; there is no way to reach another customer's case.

Run:  MCP_CUSTOMER_TOKEN=tok_cus_demo_1 python -m app.agent.mcp_server
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

from ..context import AppContext
from ..fixtures import customer_tokens
from .tools import TOOL_SCHEMAS, AgentTools

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "insurance-shopping-agent-tools", "version": "0.1.0"}


class MCPToolServer:
    def __init__(self, tools: AgentTools) -> None:
        self.tools = tools
        self.initialized = False

    async def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}
        if method == "initialize":
            self.initialized = True
            return self._result(req_id, {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                                          "instructions": "Tools are scoped to one customer. They read and prepare; approval and submission happen outside the model."})
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._result(req_id, {})
        if method == "tools/list":
            return self._result(req_id, {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]} for t in TOOL_SCHEMAS]})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name not in self.tools.names():
                return self._error(req_id, -32602, "Unknown tool: %s" % name)
            result = await self.tools.call(name, arguments)
            is_error = "error" in result
            return self._result(req_id, {"content": [{"type": "text", "text": json.dumps(result, default=str)}], "structuredContent": result, "isError": is_error})
        if req_id is None:
            return None
        return self._error(req_id, -32601, "Method not found: %s" % method)

    @staticmethod
    def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def serve_stdio(server: MCPToolServer) -> None:  # pragma: no cover - process entry point
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    while True:
        line = await reader.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        response = await server.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, default=str) + "\n")
            sys.stdout.flush()


def main() -> None:  # pragma: no cover
    token = os.environ.get("MCP_CUSTOMER_TOKEN")
    customer_id = customer_tokens().get(token or "")
    if not customer_id:
        sys.stderr.write("MCP_CUSTOMER_TOKEN must be a valid customer token\n")
        sys.exit(2)
    ctx = AppContext()
    server = MCPToolServer(AgentTools(ctx, customer_id, model_version="mcp-client"))
    asyncio.run(serve_stdio(server))


if __name__ == "__main__":  # pragma: no cover
    main()
