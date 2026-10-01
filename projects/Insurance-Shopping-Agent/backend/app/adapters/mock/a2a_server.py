"""Independently runnable A2A insurer agent wrapping a MockInsurer.

Implements a pinned subset of the A2A protocol (JSON-RPC 2.0 over HTTP):
  GET  /.well-known/agent-card.json
  POST /a2a   methods: message/send, tasks/get, tasks/cancel (rejected)

Domain semantics travel in DataParts using the renters-quote-request/v1 and
renters-quote/v1 schemas. The server is a boundary between independently deployed
agents; it never sees the customer application's database.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...clock import Clock
from ..base import ProviderTimeout
from .insurers import MockInsurer

A2A_PROTOCOL_VERSION = "0.3.0"
STATE_MAP = {
    "pending": "working",
    "input_required": "input-required",
    "quoted": "completed",
    "declined": "rejected",
    "underwriting": "working",
    "revised_offer": "input-required",
    "issued": "completed",
    "not_found": "unknown",
}


def agent_card(insurer: MockInsurer, base_url: str) -> Dict[str, Any]:
    return {
        "name": insurer.display_name,
        "description": "Mock renters insurance quoting agent (fixture data only).",
        "url": base_url.rstrip("/") + "/a2a",
        "version": insurer.config.get("fixture_version", "0"),
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "renters-quote",
                "name": "Renters quote",
                "description": "Return a renters-quote/v1 artifact for a renters-quote-request/v1 payload; may ask underwriting questions.",
                "tags": ["insurance", "renters", "quote"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "renters-application",
                "name": "Renters application",
                "description": "Accept an approved renters-application/v1, run mock underwriting and issue a fictional policy.",
                "tags": ["insurance", "renters", "application"],
            },
        ],
        "metadata": {"environment": insurer.environment, "domain_schemas": ["renters-quote-request/v1", "renters-quote/v1", "renters-application/v1"]},
    }


def _task_object(task_id: str, context_id: str, state: str, data: Dict[str, Any], artifact: Optional[Dict[str, Any]] = None,
                 artifact_name: str = "quote", message_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    task = {
        "id": task_id,
        "contextId": context_id,
        "kind": "task",
        "status": {"state": state},
        "artifacts": [],
        "metadata": data,
    }
    if message_data is not None:
        task["status"]["message"] = {
            "role": "agent",
            "kind": "message",
            "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "data", "data": message_data}],
        }
    if artifact is not None:
        task["artifacts"].append({"artifactId": uuid.uuid4().hex, "name": artifact_name, "parts": [{"kind": "data", "data": artifact}]})
    return task


def quote_task_to_a2a(view: Dict[str, Any]) -> Dict[str, Any]:
    state = STATE_MAP.get(view["status"], "unknown")
    meta = {
        "status": view["status"],
        "application_questions": view.get("application_questions", []),
        "answered_question_ids": view.get("answered_question_ids", []),
        "decline_reason": view.get("decline_reason"),
        "source": view.get("source"),
    }
    message = {"questions": view.get("questions", [])} if view["status"] == "input_required" else None
    if view["status"] == "declined":
        message = {"decline_reason": view.get("decline_reason")}
    return _task_object(view["task_ref"], view.get("context_id", view["task_ref"]), state, meta, view.get("quote"), "quote", message)


def submission_to_a2a(view: Dict[str, Any]) -> Dict[str, Any]:
    state = STATE_MAP.get(view["status"], "unknown")
    meta = {
        "status": view["status"],
        "submission_ref": view.get("submission_ref"),
        "decline_reason": view.get("decline_reason"),
        "underwriting_completes_at": view.get("underwriting_completes_at"),
        "source": view.get("source"),
    }
    artifact = None
    name = "policy"
    message = None
    if view["status"] == "revised_offer":
        artifact, name = view.get("revised_quote"), "revised_quote"
        message = {"revised_offer": True, "quote_ref": (view.get("revised_quote") or {}).get("quote_ref")}
    elif view["status"] == "issued":
        artifact = view.get("policy")
    elif view["status"] == "declined":
        message = {"decline_reason": view.get("decline_reason")}
    return _task_object(view.get("submission_ref") or "unknown", view.get("task_ref") or "unknown", state, meta, artifact, name, message)


def create_insurer_app(insurer: MockInsurer, base_url: str = "http://localhost", callback_url: Optional[str] = None,
                       realtime_latency: bool = False) -> FastAPI:
    app = FastAPI(title="%s A2A agent" % insurer.display_name, version=A2A_PROTOCOL_VERSION)
    app.state.insurer = insurer

    def rpc_error(req_id: Any, code: int, message: str, status_code: int = 200) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}, status_code=status_code)

    @app.get("/.well-known/agent-card.json")
    async def card() -> Dict[str, Any]:
        return agent_card(insurer, base_url)

    @app.get("/.well-known/agent.json")
    async def legacy_card() -> Dict[str, Any]:
        return agent_card(insurer, base_url)

    @app.get("/healthz")
    async def health() -> Dict[str, Any]:
        return {"ok": True, "insurer_id": insurer.insurer_id, "environment": insurer.environment, "now": insurer.now().isoformat()}

    @app.post("/mock/faults")
    async def inject(body: Dict[str, Any]) -> Dict[str, Any]:
        insurer.inject_fault(body["kind"], body.get("operation"), **(body.get("params") or {}))
        return {"ok": True}

    @app.post("/a2a")
    async def rpc(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return rpc_error(None, -32700, "Parse error")
        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        if realtime_latency:
            await asyncio.sleep(insurer.behavior.get("response_latency_ms", 0) / 1000.0)
        try:
            if method == "message/send":
                result = handle_message_send(insurer, params)
            elif method == "tasks/get":
                result = handle_tasks_get(insurer, params)
            elif method == "tasks/cancel":
                return rpc_error(req_id, -32002, "Task cannot be canceled")
            else:
                return rpc_error(req_id, -32601, "Method not found")
        except ProviderTimeout:
            # Simulates a lost response after the insurer recorded the request.
            return JSONResponse({"error": "gateway timeout"}, status_code=504)
        except KeyError as exc:
            return rpc_error(req_id, -32001, "Task not found: %s" % exc)
        except ValueError as exc:
            return rpc_error(req_id, -32602, "Invalid params: %s" % exc)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    return app


def _data_part(params: Dict[str, Any]) -> Dict[str, Any]:
    message = params.get("message") or {}
    for part in message.get("parts", []):
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            return part["data"]
    raise ValueError("message must contain a data part")


def handle_message_send(insurer: MockInsurer, params: Dict[str, Any]) -> Dict[str, Any]:
    data = _data_part(params)
    operation = data.get("operation")
    if operation == "request_quote":
        payload = data.get("payload") or {}
        if payload.get("schema") != "renters-quote-request/v1":
            raise ValueError("unsupported request schema %r" % payload.get("schema"))
        view = insurer.request_quote(payload, data["request_ref"], data.get("answers"))
        if "task_ref" not in view:
            # Malformed fault: return a non-conforming result on purpose.
            return {"kind": "task", "id": "malformed", "status": {"state": "completed"}, "artifacts": [{"parts": [{"kind": "text", "text": "n/a"}]}]}
        return quote_task_to_a2a(view)
    if operation == "answer_question":
        task_id = params.get("message", {}).get("taskId") or data.get("task_ref")
        view = insurer.answer_question(task_id, data["answer"])
        return quote_task_to_a2a(view)
    if operation == "submit_application":
        view = insurer.submit_application(data["payload"], data["request_ref"])
        return submission_to_a2a(view)
    if operation == "accept_revised_offer":
        view = insurer.accept_revised_offer(data["submission_ref"], data["quote_ref"], int(data["quote_version"]), data["request_ref"])
        return submission_to_a2a(view)
    if operation == "get_policy_status":
        view = insurer.get_policy_status(data["request_ref"])
        if view["status"] == "not_found":
            return _task_object("unknown", "unknown", "unknown", {"status": "not_found", "request_ref": data["request_ref"], "source": view["source"]})
        return submission_to_a2a(view)
    raise ValueError("unsupported operation %r" % operation)


def handle_tasks_get(insurer: MockInsurer, params: Dict[str, Any]) -> Dict[str, Any]:
    task_id = params.get("id")
    if not task_id:
        raise ValueError("id is required")
    if "-sub-" in task_id:
        return submission_to_a2a(insurer.get_submission(task_id))
    return quote_task_to_a2a(insurer.get_task(task_id))


def run_from_cli() -> None:  # pragma: no cover - manual entry point
    import argparse

    import uvicorn

    from ...clock import build_clock
    from ...config import settings
    from ...fixtures import load_insurers

    parser = argparse.ArgumentParser(description="Run a mock insurer as an A2A agent")
    parser.add_argument("--insurer", required=True, choices=["a", "b", "c"], help="fixture label")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--realtime-latency", action="store_true")
    args = parser.parse_args()
    config = next(c for c in load_insurers() if c["label"].lower() == args.insurer)
    port = args.port or config["a2a"]["default_port"]
    clock = build_clock(settings.fixture_clock, settings.fixture_clock_start, settings.fixture_clock_frozen)
    insurer = MockInsurer(config, clock, protocol="a2a/%s" % A2A_PROTOCOL_VERSION)
    app = create_insurer_app(insurer, base_url="http://localhost:%d" % port, realtime_latency=args.realtime_latency)

    @app.post("/mock/clock/advance")
    async def advance(body: Dict[str, Any]) -> Dict[str, Any]:
        from ...clock import FixtureClock

        if not isinstance(clock, FixtureClock):
            return {"ok": False, "reason": "system clock in use"}
        return {"ok": True, "now": clock.advance(**{k: int(v) for k, v in body.items()}).isoformat()}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    run_from_cli()
