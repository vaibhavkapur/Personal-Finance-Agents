"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..agent.a2a import router as a2a_router
from ..agent.llm import build_planner
from ..agent.orchestrator import Agent
from ..agent.tools import Tools
from ..config import FRONTEND_DIR
from ..container import Container
from ..workflows.states import WorkflowError
from . import routes_cases, routes_ops, routes_provider

_APPS: Dict[int, FastAPI] = {}


def attach_agent(container: Container) -> None:
    if not hasattr(container, "agent"):
        tools = Tools(container.service, container.settings.provider_environment)
        container.agent = Agent(container.service, tools, build_planner(container.settings), container.lenders, tool_budget=container.settings.tool_call_budget)


def create_app(container: Optional[Container] = None, run_worker: Optional[bool] = None) -> FastAPI:
    container = container or Container()
    attach_agent(container)
    if run_worker is None:
        run_worker = os.getenv("RUN_WORKER_IN_API", "0") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if run_worker:
            task = asyncio.create_task(container.worker.run_forever(interval_seconds=float(os.getenv("WORKER_INTERVAL_SECONDS", "2"))))
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Loan Negotiation Agent", version="0.1.0", description="Borrower-side mortgage comparison, negotiation and mock refinance coordination (prototype; mock providers only).", lifespan=lifespan)
    app.state.container = container
    _APPS[id(container)] = app

    @app.exception_handler(WorkflowError)
    async def workflow_error_handler(request: Request, exc: WorkflowError):
        return JSONResponse(status_code=getattr(exc, "status_code", 400), content={"detail": str(exc)})

    app.include_router(routes_cases.router)
    app.include_router(routes_provider.router)
    app.include_router(routes_ops.router)
    app.include_router(a2a_router)

    @app.get("/healthz")
    def healthz() -> Dict[str, Any]:
        return {"status": "ok", "environment": container.settings.provider_environment, "clock_now": container.clock.now().isoformat(), "planner": container.agent.planner.name}

    @app.get("/v1/lenders")
    def lenders() -> Dict[str, Any]:
        return {"lenders": [{"id": l["id"], "name": l["name"], "environment": l["environment"], "is_current_servicer": l.get("is_current_servicer", False), "a2a_agent_card_url": f"/a2a/lenders/{l['id']}/.well-known/agent-card.json"} for l in container.lenders.values()], "adapter_capabilities": container.adapter.capabilities.as_dict()}

    # Mock lender inspection (simulator side; no credentials).
    @app.get("/mock-lender/applications/{application_ref}")
    def mock_application(application_ref: str) -> Dict[str, Any]:
        view = container.network.get_application(application_ref)
        if view is None:
            raise HTTPException(status_code=404, detail="unknown application")
        return view

    @app.get("/mock-lender/negotiations/{request_ref}")
    def mock_negotiation(request_ref: str) -> Dict[str, Any]:
        view = container.network.get_negotiation(request_ref)
        if view is None:
            raise HTTPException(status_code=404, detail="unknown negotiation")
        return view

    if FRONTEND_DIR.exists():
        app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

        @app.get("/")
        def index():
            return FileResponse(str(FRONTEND_DIR / "index.html"))

    return app


def app_for_container(container: Container) -> FastAPI:
    app = _APPS.get(id(container))
    if app is None:
        app = create_app(container, run_worker=False)
    return app


def default_app() -> FastAPI:
    """Factory for ``uvicorn 'backend.app.api.main:default_app' --factory``."""
    container = Container()
    container.seed()
    return create_app(container)
