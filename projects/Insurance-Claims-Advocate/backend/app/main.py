"""FastAPI application factory."""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import dev, provider_events, routes
from .api.insurer_agent import InsurerAgentServer, build_router as build_insurer_router
from .config import FRONTEND_DIR
from .context import AppContext
from .workflows.errors import DomainError
from .workflows.state_machine import StaleVersionError, TransitionError


def create_app(ctx: Optional[AppContext] = None) -> FastAPI:
    ctx = ctx or AppContext()
    app = FastAPI(title="Insurance Claims Advocate (prototype)", version="0.1.0", description="Delayed-baggage claims advocate against a mock insurer. All providers are simulated.")
    app.state.ctx = ctx
    app.add_middleware(CORSMiddleware, allow_origins=ctx.settings.cors_origins, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(StaleVersionError)
    async def stale_version(_: Request, exc: StaleVersionError):
        return JSONResponse(status_code=409, content={"code": "stale_case_version", "message": str(exc)})

    @app.exception_handler(TransitionError)
    async def transition_error(_: Request, exc: TransitionError):
        return JSONResponse(status_code=409, content={"code": "invalid_transition", "message": str(exc)})

    app.include_router(routes.router)
    app.include_router(provider_events.router)
    if ctx.settings.dev_endpoints_enabled:
        app.include_router(dev.router)
    insurer_server = getattr(ctx.adapter, "server", None) or InsurerAgentServer(ctx.mock_insurer, ctx.clock)
    app.include_router(build_insurer_router(insurer_server))

    @app.get("/health")
    def health():
        return {"status": "ok", "environment": ctx.settings.environment, "adapter": ctx.adapter.capabilities.to_dict(), "clock": ctx.clock.now_iso(), "clock_frozen": ctx.clock.is_frozen}

    if FRONTEND_DIR.exists():
        app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

        @app.get("/")
        def index():
            return FileResponse(str(FRONTEND_DIR / "index.html"))

    return app


# Run with: uvicorn app.main:create_app --factory --app-dir backend
