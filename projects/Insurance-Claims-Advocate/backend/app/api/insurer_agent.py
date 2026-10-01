"""Optional A2A boundary: a mock insurer *claims agent* exposed as a JSON-RPC A2A server.

The insurer agent wraps the same simulator. It speaks a pinned A2A protocol version and an agreed domain payload schema.
This is a minimal implementation of `message/send` and `tasks/get`; it is not a full A2A SDK.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

from ..adapters.base import ProviderDeclined, ProviderMalformedResponse, ProviderTimeout
from ..adapters.mock_insurer import MockInsurerAdapter
from ..clock import Clock
from ..ids import new_id

A2A_PROTOCOL_VERSION = "0.3"
DOMAIN_SCHEMA = "claims-advocate/a2a-claims/1.0"


class InsurerAgentServer:
    def __init__(self, insurer: MockInsurerAdapter, clock: Clock):
        self.insurer = insurer
        self.clock = clock
        self.tasks: Dict[str, Dict[str, Any]] = {}

    def agent_card(self, base_url: str = "") -> Dict[str, Any]:
        return {
            "name": "Demo Mutual Claims Agent (mock)",
            "description": "Mock insurer claims intake agent. Accepts delayed-baggage claim packets, supplemental evidence and appeals as A2A tasks.",
            "url": f"{base_url}/insurer-agent/a2a",
            "protocolVersion": A2A_PROTOCOL_VERSION,
            "version": "0.1.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {"id": "submit_claim", "name": "Submit claim", "description": f"Data part schema {DOMAIN_SCHEMA}", "tags": ["claims"]},
                {"id": "add_evidence", "name": "Add evidence / appeal", "description": "Supplemental packet or written review request for an existing claim.", "tags": ["claims"]},
                {"id": "get_claim", "name": "Claim status", "tags": ["claims"]},
                {"id": "find_submission", "name": "Find submission by request reference", "tags": ["claims", "idempotency"]},
            ],
            "environment": "mock",
        }

    async def handle_jsonrpc(self, body: Dict[str, Any]) -> Dict[str, Any]:
        rid = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        try:
            if method == "message/send":
                result = await self._message_send(params)
            elif method == "tasks/get":
                task = self.tasks.get(params.get("id"))
                if not task:
                    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": "Task not found"}}
                result = task
            else:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except ProviderTimeout as exc:
            # a timeout at the agent boundary is modelled as a transport failure
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"timeout: {exc}", "data": {"kind": "timeout"}}}
        except ProviderDeclined as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": f"declined: {exc}", "data": {"kind": "declined"}}}
        except ProviderMalformedResponse as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32003, "message": f"malformed: {exc}", "data": {"kind": "malformed"}}}

    async def _message_send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        message = params.get("message") or {}
        data_parts = [p.get("data") for p in message.get("parts", []) if p.get("kind") == "data"]
        if not data_parts:
            raise ProviderDeclined("message has no data part")
        data = data_parts[0]
        if data.get("schema") != DOMAIN_SCHEMA:
            raise ProviderDeclined(f"unsupported payload schema {data.get('schema')}; expected {DOMAIN_SCHEMA}")
        op = data.get("operation")
        if op == "submit_claim":
            result = await self.insurer.submit_claim(data["packet"], data["request_ref"])
        elif op == "add_evidence":
            result = await self.insurer.add_evidence(data["claim_ref"], data["packet"], data["request_ref"])
        elif op == "get_claim":
            result = await self.insurer.get_claim(data["claim_ref"])
        elif op == "find_submission":
            result = await self.insurer.find_submission(data["request_ref"])
        else:
            raise ProviderDeclined(f"unknown operation {op}")
        task_id = new_id("a2atask")
        task = {
            "id": task_id,
            "contextId": result.get("claim_ref") or data.get("claim_ref") or new_id("ctx"),
            "kind": "task",
            "status": {"state": "completed", "timestamp": self.clock.now_iso()},
            "artifacts": [{"artifactId": new_id("art"), "parts": [{"kind": "data", "data": {"schema": DOMAIN_SCHEMA, "operation": op, "result": result}}]}],
            "metadata": {"protocolVersion": A2A_PROTOCOL_VERSION, "environment": "mock"},
        }
        self.tasks[task_id] = task
        return task


def build_router(server: InsurerAgentServer) -> APIRouter:
    router = APIRouter(prefix="/insurer-agent", tags=["a2a-insurer-agent"])

    @router.get("/.well-known/agent.json")
    async def agent_card(request: Request):
        return server.agent_card(str(request.base_url).rstrip("/"))

    @router.post("/a2a")
    async def a2a(request: Request):
        body = await request.json()
        return await server.handle_jsonrpc(body)

    return router
