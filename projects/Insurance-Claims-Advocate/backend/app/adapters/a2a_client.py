"""ClaimsAdapter that talks to the insurer claims agent over A2A JSON-RPC (in-process by default, HTTP if a base URL is set).
External task IDs are mapped to internal case/action IDs; the protocol version and payload schema are pinned."""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from ..clock import Clock
from ..ids import new_id
from ..persistence.models import A2ATaskMap
from .base import AdapterCapabilities, ProviderDeclined, ProviderMalformedResponse, ProviderTimeout, provider_result

A2A_PROTOCOL_VERSION = "0.3"
DOMAIN_SCHEMA = "claims-advocate/a2a-claims/1.0"


class A2AClaimsAdapter:
    def __init__(self, db, clock: Clock, server=None, base_url: str = ""):
        self.db = db
        self.clock = clock
        self.server = server
        self.base_url = base_url.rstrip("/")
        self.capabilities = AdapterCapabilities(name="a2a_insurer_agent", environment="mock", find_submission=True, uncertain_requires_manual_review=False, protocol=f"a2a/{A2A_PROTOCOL_VERSION} json-rpc", notes="Mock insurer agent reached through the A2A boundary; payload schema " + DOMAIN_SCHEMA)

    async def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": new_id("rpc"), "method": method, "params": params}
        if self.base_url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(f"{self.base_url}/insurer-agent/a2a", json=body)
                    resp.raise_for_status()
                    return resp.json()
            except httpx.TimeoutException as exc:
                raise ProviderTimeout(str(exc))
        if self.server is None:
            raise ProviderDeclined("no A2A server configured")
        return await self.server.handle_jsonrpc(body)

    async def _send(self, data: Dict[str, Any], *, action_ref: Optional[str] = None) -> Dict[str, Any]:
        data = dict(data, schema=DOMAIN_SCHEMA)
        response = await self._rpc("message/send", {"message": {"role": "user", "messageId": new_id("msg"), "parts": [{"kind": "data", "data": data}]}, "metadata": {"protocolVersion": A2A_PROTOCOL_VERSION}})
        if "error" in response:
            err = response["error"]
            kind = (err.get("data") or {}).get("kind")
            if kind == "timeout":
                raise ProviderTimeout(err.get("message", "timeout"))
            if kind == "declined":
                raise ProviderDeclined(err.get("message", "declined"))
            raise ProviderMalformedResponse(err.get("message", "error"))
        task = response.get("result") or {}
        if task.get("kind") != "task" or task.get("status", {}).get("state") != "completed":
            raise ProviderMalformedResponse(f"unexpected task state: {task.get('status')}")
        if action_ref:
            with self.db.session() as s:
                s.add(A2ATaskMap(external_task_id=task["id"], case_id=data.get("packet", {}).get("case_id", ""), action_id=action_ref, protocol_version=A2A_PROTOCOL_VERSION, created_at=self.clock.now_iso()))
        parts = [p for a in task.get("artifacts", []) for p in a.get("parts", []) if p.get("kind") == "data"]
        if not parts:
            raise ProviderMalformedResponse("task has no data artifact")
        result = parts[0]["data"].get("result", {})
        result = dict(result, a2a_task_id=task["id"])
        return provider_result(result, environment=result.get("environment", "mock"), authority="simulated", retrieved_at=self.clock.now_iso(), source="a2a_insurer_agent")

    async def submit_claim(self, packet: dict, request_ref: str) -> dict:
        return await self._send({"operation": "submit_claim", "packet": packet, "request_ref": request_ref}, action_ref=request_ref)

    async def add_evidence(self, claim_ref: str, packet: dict, request_ref: str) -> dict:
        return await self._send({"operation": "add_evidence", "claim_ref": claim_ref, "packet": packet, "request_ref": request_ref}, action_ref=request_ref)

    async def get_claim(self, claim_ref: str) -> dict:
        return await self._send({"operation": "get_claim", "claim_ref": claim_ref})

    async def find_submission(self, request_ref: str) -> dict:
        return await self._send({"operation": "find_submission", "request_ref": request_ref})
