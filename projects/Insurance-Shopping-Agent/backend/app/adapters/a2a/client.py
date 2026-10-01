"""A2A adapter: talks to an insurer agent over JSON-RPC and maps Task objects back to
the adapter result shape used by the rest of the application.

External A2A task IDs are stored as QuoteTask.external_task_id by the caller; the
protocol version is pinned and checked against the agent card.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

import httpx

from ..base import AdapterCapabilities, ProviderError, ProviderMalformedResponse, ProviderTimeout, provider_source

PINNED_PROTOCOL_VERSION = "0.3.0"
REVERSE_STATE = {
    "input-required": "input_required",
    "completed": "quoted",
    "rejected": "declined",
    "working": "pending",
    "unknown": "not_found",
}


class A2AInsurerAdapter:
    def __init__(self, insurer_id: str, base_url: str, environment: str = "mock", timeout_seconds: float = 5.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.insurer_id = insurer_id
        self.base_url = base_url.rstrip("/")
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._card: Optional[Dict[str, Any]] = None

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            environment=self.environment,
            protocol="a2a/%s" % PINNED_PROTOCOL_VERSION,
            status_lookup_by_request_ref=True,
            supports_callbacks=False,
            supports_revised_offer_acceptance=True,
            uncertain_outcome_requires_manual_review=False,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds, transport=self._transport)

    async def agent_card(self) -> Dict[str, Any]:
        if self._card is None:
            async with self._client() as client:
                response = await client.get("/.well-known/agent-card.json")
                response.raise_for_status()
                card = response.json()
            if card.get("protocolVersion") != PINNED_PROTOCOL_VERSION:
                raise ProviderError(
                    "agent %s speaks A2A %s but this adapter is pinned to %s"
                    % (self.insurer_id, card.get("protocolVersion"), PINNED_PROTOCOL_VERSION)
                )
            self._card = card
        return self._card

    async def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        await self.agent_card()
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        try:
            async with self._client() as client:
                response = await client.post("/a2a", json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout("A2A call to %s timed out: %s" % (self.insurer_id, exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("A2A transport error for %s: %s" % (self.insurer_id, exc)) from exc
        if response.status_code == 504:
            raise ProviderTimeout("A2A gateway timeout from %s (outcome unknown)" % self.insurer_id)
        if response.status_code >= 400:
            raise ProviderError("A2A HTTP %d from %s" % (response.status_code, self.insurer_id))
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderMalformedResponse("non-JSON A2A response from %s" % self.insurer_id) from exc
        if "error" in data:
            err = data["error"]
            if err.get("code") == -32001:
                raise KeyError(err.get("message"))
            raise ProviderError("A2A error %s: %s" % (err.get("code"), err.get("message")))
        return data.get("result") or {}

    def _send(self, data: Dict[str, Any], task_id: Optional[str] = None) -> Dict[str, Any]:
        message = {"role": "user", "kind": "message", "messageId": uuid.uuid4().hex, "parts": [{"kind": "data", "data": data}]}
        if task_id:
            message["taskId"] = task_id
        return {"message": message, "configuration": {"acceptedOutputModes": ["application/json"], "blocking": True}}

    # ------------------------------------------------------------------ mapping
    def _source(self) -> Dict[str, Any]:
        from datetime import datetime, timezone

        return provider_source(self.insurer_id, self.environment, "a2a/%s" % PINNED_PROTOCOL_VERSION, datetime.now(timezone.utc))

    @staticmethod
    def _artifact(task: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
        for artifact in task.get("artifacts", []):
            if artifact.get("name") == name:
                for part in artifact.get("parts", []):
                    if part.get("kind") == "data":
                        return part["data"]
        return None

    @staticmethod
    def _message_data(task: Dict[str, Any]) -> Dict[str, Any]:
        message = (task.get("status") or {}).get("message") or {}
        for part in message.get("parts", []):
            if part.get("kind") == "data":
                return part["data"]
        return {}

    def _map_quote_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        if task.get("kind") != "task" or "id" not in task or "status" not in task:
            raise ProviderMalformedResponse("A2A result is not a task object")
        meta = task.get("metadata") or {}
        status = meta.get("status") or REVERSE_STATE.get(task["status"].get("state"), "unknown")
        quote = self._artifact(task, "quote")
        if status == "quoted" and (quote is None or quote.get("schema") != "renters-quote/v1"):
            raise ProviderMalformedResponse("completed A2A task without a renters-quote/v1 artifact")
        message = self._message_data(task)
        return {
            "task_ref": task["id"],
            "context_id": task.get("contextId"),
            "status": status,
            "questions": message.get("questions", []) if status == "input_required" else [],
            "application_questions": meta.get("application_questions", []),
            "answered_question_ids": meta.get("answered_question_ids", []),
            "quote": quote if status == "quoted" else None,
            "decline_reason": meta.get("decline_reason") or message.get("decline_reason"),
            "source": meta.get("source") or self._source(),
        }

    def _map_submission(self, task: Dict[str, Any]) -> Dict[str, Any]:
        if task.get("kind") != "task":
            raise ProviderMalformedResponse("A2A result is not a task object")
        meta = task.get("metadata") or {}
        status = meta.get("status") or "unknown"
        return {
            "submission_ref": meta.get("submission_ref") or task.get("id"),
            "task_ref": task.get("contextId"),
            "status": status,
            "revised_quote": self._artifact(task, "revised_quote") if status == "revised_offer" else None,
            "policy": self._artifact(task, "policy") if status == "issued" else None,
            "decline_reason": meta.get("decline_reason"),
            "underwriting_completes_at": meta.get("underwriting_completes_at"),
            "source": meta.get("source") or self._source(),
        }

    # ------------------------------------------------------------------ mock-only controls
    async def mock_advance_clock(self, **delta: int) -> Optional[Dict[str, Any]]:
        """Advance a remote mock insurer's fixture clock (only exists on mock agents)."""
        if self.environment != "mock":
            return None
        try:
            async with self._client() as client:
                response = await client.post("/mock/clock/advance", json=delta)
                return response.json() if response.status_code == 200 else None
        except httpx.HTTPError:
            return None

    async def mock_inject_fault(self, kind: str, operation: Optional[str], params: Dict[str, Any]) -> bool:
        if self.environment != "mock":
            return False
        try:
            async with self._client() as client:
                response = await client.post("/mock/faults", json={"kind": kind, "operation": operation, "params": params})
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------ operations
    async def request_quote(self, needs: Dict[str, Any], request_ref: str, answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = await self._rpc("message/send", self._send({"operation": "request_quote", "payload": needs, "request_ref": request_ref, "answers": answers or {}}))
        return self._map_quote_task(result)

    async def answer_question(self, task_ref: str, answer: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._rpc("message/send", self._send({"operation": "answer_question", "task_ref": task_ref, "answer": answer}, task_id=task_ref))
        return self._map_quote_task(result)

    async def get_task(self, task_ref: str) -> Dict[str, Any]:
        result = await self._rpc("tasks/get", {"id": task_ref})
        return self._map_quote_task(result)

    async def submit_application(self, payload: Dict[str, Any], request_ref: str) -> Dict[str, Any]:
        result = await self._rpc("message/send", self._send({"operation": "submit_application", "payload": payload, "request_ref": request_ref}))
        return self._map_submission(result)

    async def accept_revised_offer(self, submission_ref: str, quote_ref: str, quote_version: int, request_ref: str) -> Dict[str, Any]:
        result = await self._rpc(
            "message/send",
            self._send({"operation": "accept_revised_offer", "submission_ref": submission_ref, "quote_ref": quote_ref, "quote_version": quote_version, "request_ref": request_ref}),
        )
        return self._map_submission(result)

    async def get_policy_status(self, request_ref: str) -> Dict[str, Any]:
        result = await self._rpc("message/send", self._send({"operation": "get_policy_status", "request_ref": request_ref}))
        meta = result.get("metadata") or {}
        if meta.get("status") == "not_found":
            return {"status": "not_found", "request_ref": request_ref, "source": meta.get("source") or self._source()}
        return self._map_submission(result)
