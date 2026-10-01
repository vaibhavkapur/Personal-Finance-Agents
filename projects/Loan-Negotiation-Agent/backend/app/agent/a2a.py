"""A2A boundary: mock lender agents and an adapter that talks to them.

Each mock lender is exposed as an independent A2A agent (agent card +
JSON-RPC ``message/send`` / ``tasks/get``). The lender agent can request
additional facts (``input-required``) or return an offer artifact using the
versioned loan-offer schema. External A2A task ids are mapped to our internal
client request references; the supported protocol version is pinned.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..adapters.base import AdapterCapabilities, ProviderMalformedResponse, ProviderResult
from ..adapters.mock_lender import MockLenderNetwork

A2A_PROTOCOL_VERSION = "0.3"
OFFER_SCHEMA = "loan-offer/v1"

router = APIRouter(prefix="/a2a", tags=["a2a"])


def agent_card(lender: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    card = lender.get("a2a_agent_card", {})
    return {
        "name": card.get("name", lender["name"]),
        "description": f"Mock pricing agent for {lender['name']}. Accepts reprice/comparable-offer requests and returns loan-offer/v1 artifacts.",
        "url": f"{base_url}/a2a/lenders/{lender['id']}",
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [{"id": s, "name": s.replace("_", " "), "description": s, "tags": ["mortgage", "mock"]} for s in card.get("skills", [])],
        "environment": lender.get("environment", "mock"),
        "domainPayloadSchema": OFFER_SCHEMA,
    }


def _rpc_error(id_: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class A2ALenderAgent:
    """Server-side handler wrapping the simulator for one lender."""

    def __init__(self, network: MockLenderNetwork, lender_id: str):
        self.network = network
        self.lender_id = lender_id
        self.tasks: Dict[str, Dict[str, Any]] = {}

    def handle(self, body: Dict[str, Any]) -> Dict[str, Any]:
        id_ = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        if body.get("jsonrpc") != "2.0":
            return _rpc_error(id_, -32600, "invalid request")
        if method == "message/send":
            return self._message_send(id_, params)
        if method == "tasks/get":
            task = self.tasks.get(str(params.get("id")))
            if task is None:
                return _rpc_error(id_, -32001, "task not found")
            return {"jsonrpc": "2.0", "id": id_, "result": task}
        return _rpc_error(id_, -32601, f"method not found: {method}")

    def _message_send(self, id_: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        message = params.get("message") or {}
        parts = message.get("parts") or []
        data_part = next((p.get("data") for p in parts if p.get("kind") == "data"), None)
        if data_part is None:
            return _rpc_error(id_, -32602, "a data part with the negotiation payload is required")
        request_ref = str(message.get("taskId") or message.get("messageId") or data_part.get("request_ref") or f"a2a_{len(self.tasks) + 1}")
        payload = dict(data_part)
        payload["lender_id"] = self.lender_id
        response = self.network.send_negotiation(payload, request_ref)
        outcome = response.get("outcome")
        state = {"counteroffer": "completed", "refused": "completed", "quote_expired": "completed", "facts_requested": "input-required"}.get(outcome, "failed")
        artifacts = []
        if response.get("offer"):
            artifacts.append({"artifactId": f"art_{request_ref}", "name": "loan-offer", "parts": [{"kind": "data", "data": {"schema": OFFER_SCHEMA, **response["offer"]}}]})
        task = {
            "id": request_ref,
            "contextId": str(data_part.get("case_ref") or request_ref),
            "kind": "task",
            "status": {
                "state": state,
                "message": {"role": "agent", "parts": [{"kind": "text", "text": response.get("reason", outcome)}, {"kind": "data", "data": {k: v for k, v in response.items() if k != "offer"}}]},
            },
            "artifacts": artifacts,
            "metadata": {"environment": "mock", "protocolVersion": A2A_PROTOCOL_VERSION},
        }
        self.tasks[request_ref] = task
        return {"jsonrpc": "2.0", "id": id_, "result": task}


_agents: Dict[str, A2ALenderAgent] = {}


def get_agent(network: MockLenderNetwork, lender_id: str) -> A2ALenderAgent:
    key = f"{id(network)}:{lender_id}"
    if key not in _agents:
        _agents[key] = A2ALenderAgent(network, lender_id)
    return _agents[key]


@router.get("/lenders/{lender_id}/.well-known/agent.json")
@router.get("/lenders/{lender_id}/.well-known/agent-card.json")
def get_agent_card(lender_id: str, request: Request) -> Dict[str, Any]:
    container = request.app.state.container
    lender = container.lenders.get(lender_id)
    if lender is None:
        raise HTTPException(status_code=404, detail="unknown lender agent")
    return agent_card(lender, str(request.base_url).rstrip("/"))


@router.post("/lenders/{lender_id}")
async def rpc(lender_id: str, request: Request) -> Dict[str, Any]:
    container = request.app.state.container
    if lender_id not in container.lenders:
        raise HTTPException(status_code=404, detail="unknown lender agent")
    body = await request.json()
    return get_agent(container.network, lender_id).handle(body)


class A2ALenderAdapter:
    """Client-side adapter: sends negotiations through the A2A boundary.

    Non-negotiation operations are delegated to ``fallback`` (the direct mock
    adapter) because lender APIs remain the execution interface for
    applications.
    """

    def __init__(self, client: httpx.AsyncClient, fallback, protocol_version: str = A2A_PROTOCOL_VERSION):
        self.client = client
        self.fallback = fallback
        self.protocol_version = protocol_version
        self.capabilities = AdapterCapabilities(**{**fallback.capabilities.as_dict(), "send_negotiation": True, "get_negotiation_by_request_ref": True})
        self.task_map: Dict[str, str] = {}  # external task id -> internal request_ref

    async def _check_card(self, lender_id: str) -> None:
        resp = await self.client.get(f"/a2a/lenders/{lender_id}/.well-known/agent-card.json")
        if resp.status_code != 200:
            raise ProviderMalformedResponse("agent card unavailable")
        card = resp.json()
        if card.get("protocolVersion") != self.protocol_version:
            raise ProviderMalformedResponse(f"unsupported A2A protocol version {card.get('protocolVersion')}")
        if card.get("domainPayloadSchema") != OFFER_SCHEMA:
            raise ProviderMalformedResponse("lender agent does not speak the agreed loan-offer schema")

    async def send_negotiation(self, message: Dict[str, Any], request_ref: str) -> ProviderResult:
        lender_id = message["lender_id"]
        await self._check_card(lender_id)
        rpc_body = {"jsonrpc": "2.0", "id": request_ref, "method": "message/send", "params": {"message": {"role": "user", "messageId": request_ref, "taskId": request_ref, "parts": [{"kind": "text", "text": message.get("text", "")}, {"kind": "data", "data": {**message, "request_ref": request_ref}}]}}}
        resp = await self.client.post(f"/a2a/lenders/{lender_id}", json=rpc_body)
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderMalformedResponse(str(exc))
        if "error" in body:
            raise ProviderMalformedResponse(body["error"].get("message", "a2a error"))
        task = body["result"]
        self.task_map[task["id"]] = request_ref
        return self._task_to_result(task, request_ref)

    def _task_to_result(self, task: Dict[str, Any], request_ref: str) -> ProviderResult:
        status_parts = task["status"]["message"]["parts"]
        data = next((p["data"] for p in status_parts if p.get("kind") == "data"), {})
        offer = None
        for art in task.get("artifacts", []):
            for p in art.get("parts", []):
                if p.get("kind") == "data" and p["data"].get("schema") == OFFER_SCHEMA:
                    offer = {k: v for k, v in p["data"].items() if k != "schema"}
        result_data = dict(data)
        if offer:
            result_data["offer"] = offer
        result_data["a2a_task_id"] = task["id"]
        result_data["a2a_state"] = task["status"]["state"]
        from datetime import datetime, timezone

        return ProviderResult(ok=True, environment="mock", source="a2a_lender_agent", retrieved_at=datetime.now(timezone.utc), authority="simulated", data=result_data, provider_reference=data.get("negotiation_ref"), request_ref=request_ref)

    async def get_negotiation(self, request_ref: str) -> ProviderResult:
        # Task ids equal our request refs by construction, so the lender's record can be
        # found by the original reference through the execution interface.
        return await self.fallback.get_negotiation(request_ref)

    async def request_offer(self, request, request_ref):
        return await self.fallback.request_offer(request, request_ref)

    async def submit_application(self, packet, request_ref):
        return await self.fallback.submit_application(packet, request_ref)

    async def get_application(self, request_ref):
        return await self.fallback.get_application(request_ref)

    async def provide_documents(self, application_ref, documents, request_ref):
        return await self.fallback.provide_documents(application_ref, documents, request_ref)

    async def request_closing(self, application_ref, request_ref):
        return await self.fallback.request_closing(application_ref, request_ref)
