"""Minimal MCP server exposing the loan tools over stdio JSON-RPC.

Implements the subset of the Model Context Protocol needed for tool use:
``initialize``, ``notifications/initialized``, ``ping``, ``tools/list`` and
``tools/call``. The protocol revision is pinned in ``PROTOCOL_VERSION``.

The server is scoped to one authenticated borrower via
``LOAN_AGENT_CUSTOMER_TOKEN``; tools can only touch that customer's cases.
Every tool result includes source, retrieval time and authority.

Run: ``LOAN_AGENT_CUSTOMER_TOKEN=demo-borrower-token python -m backend.app.agent.mcp_server``
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, Dict, Optional

from sqlalchemy import select

from ..container import Container
from ..persistence.models import Customer
from ..workflows.case_service import NotFoundError
from ..workflows.states import WorkflowError
from .tools import TOOL_SCHEMAS, Tools

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "loan-negotiation-agent", "version": "0.1.0"}


class MCPServer:
    def __init__(self, container: Container, customer_token: str):
        self.container = container
        self.tools = Tools(container.service, container.settings.provider_environment)
        self._token_hash = hashlib.sha256(customer_token.encode("utf-8")).hexdigest()
        self.initialized = False

    # ------------------------------------------------------------- helpers
    def _customer(self, session) -> Customer:
        customer = session.execute(select(Customer).where(Customer.api_token_hash == self._token_hash)).scalar_one_or_none()
        if customer is None:
            raise NotFoundError("unknown customer token")
        return customer

    @staticmethod
    def _ok(id_: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": id_, "result": result}

    @staticmethod
    def _err(id_: Any, code: int, message: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        err: Dict[str, Any] = {"code": code, "message": message}
        if data:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": id_, "error": err}

    # ------------------------------------------------------------- handlers
    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        id_ = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if request.get("jsonrpc") != "2.0":
            return self._err(id_, -32600, "invalid request")
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = PROTOCOL_VERSION if requested in (None, PROTOCOL_VERSION) else PROTOCOL_VERSION
            return self._ok(id_, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": "Borrower-side mortgage tools. Tools prepare actions; every external write needs borrower approval through the application API."})
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method == "ping":
            return self._ok(id_, {})
        if method == "tools/list":
            return self._ok(id_, {"tools": TOOL_SCHEMAS})
        if method == "tools/call":
            return self._tools_call(id_, params)
        return self._err(id_, -32601, f"method not found: {method}")

    def _tools_call(self, id_: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        args = dict(params.get("arguments") or {})
        if name not in {t["name"] for t in TOOL_SCHEMAS}:
            return self._err(id_, -32602, f"unknown tool {name}")
        case_id = args.get("case_id")
        if not case_id:
            return self._err(id_, -32602, "case_id is required")
        with self.container.db.session() as session:
            try:
                customer = self._customer(session)
                case = self.container.service.get_case(session, case_id, customer)
            except WorkflowError as exc:
                return self._ok(id_, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
            if name == "prepare_lender_request" and "disclosed_document_ids" in args:
                args["disclosed_document_ids"] = list(args["disclosed_document_ids"])
            response = self.tools.call(name, session, case, customer, args)
        payload = response.as_dict()
        text = json.dumps(payload, default=str)
        return self._ok(id_, {"content": [{"type": "text", "text": text}], "structuredContent": payload, "isError": not response.ok})


def serve_stdio(container: Optional[Container] = None) -> None:  # pragma: no cover - process entry point
    token = os.environ.get("LOAN_AGENT_CUSTOMER_TOKEN", "")
    if not token:
        sys.stderr.write("LOAN_AGENT_CUSTOMER_TOKEN is required\n")
        sys.exit(2)
    container = container or Container()
    container.seed()
    server = MCPServer(container, token)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(MCPServer._err(None, -32700, "parse error")) + "\n")
            sys.stdout.flush()
            continue
        response = server.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, default=str) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    serve_stdio()
