"""FastAPI application factory.

    uvicorn backend.app.api.app:create_app --factory --reload
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..agent.llm import OpenAICompatibleModel, RulesPlanner
from ..agent.orchestrator import Orchestrator
from ..container import Container, build_container
from ..domain.errors import DomainError
from .auth import SessionStore
from .routes import router


def create_app(container: Optional[Container] = None, *, seed: bool = True) -> FastAPI:
    container = container or build_container()
    if seed and not container.repos.all_cases() and container.repos.db.fetch_one("SELECT id FROM customers LIMIT 1") is None:
        container.seed()
    app = FastAPI(title="Refund and Fee-Recovery Agent (prototype)", version="0.1.0",
                  description="Consumer refund-recovery prototype. All providers are simulated; amounts and deadlines are fixtures.")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])
    app.state.container = container
    app.state.sessions = SessionStore()
    s = container.settings
    model = OpenAICompatibleModel(s.llm_base_url, s.llm_api_key, s.llm_model) if (s.llm_base_url and s.llm_api_key and s.llm_model) else RulesPlanner()
    app.state.orchestrator = Orchestrator(container.service, model=model, max_tool_calls=s.max_tool_calls)
    app.include_router(router)

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})

    return app
