"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .agent.llm import build_provider
from .api import actions, cases, operator, provider_events
from .context import AppContext
from .persistence.repositories import NotFound
from .workflows.case_service import CaseError
from .workflows.states import IllegalTransition, StaleVersion
from .workflows.worker import Worker

log = logging.getLogger("app")


def create_app(ctx: Optional[AppContext] = None, embedded_worker: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = ctx or AppContext()
        app.state.llm = build_provider(app.state.ctx.settings)
        task = None
        if embedded_worker:
            task = asyncio.create_task(Worker(app.state.ctx, owner="embedded").run_forever(1.0))
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Insurance Shopping Agent (prototype, mock providers)", version=__version__, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    if ctx is not None:
        # Make the context available before lifespan for TestClient users that skip startup.
        app.state.ctx = ctx
        app.state.llm = build_provider(ctx.settings)

    @app.exception_handler(CaseError)
    async def case_error(_: Request, exc: CaseError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc), **({"details": exc.details} if exc.details else {})})

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(StaleVersion)
    async def stale(_: Request, exc: StaleVersion) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc), "expected": exc.expected, "actual": exc.actual})

    @app.exception_handler(IllegalTransition)
    async def illegal(_: Request, exc: IllegalTransition) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/healthz")
    async def health(request: Request):
        c: AppContext = request.app.state.ctx
        return {"ok": True, "version": __version__, "environment": c.environment, "adapter_mode": c.registry.mode, "now": c.now().isoformat()}

    app.include_router(cases.router)
    app.include_router(actions.router)
    app.include_router(provider_events.router)
    app.include_router(operator.router)
    return app


def run() -> None:  # pragma: no cover - entry point
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    embedded = os.environ.get("EMBEDDED_WORKER", "true").lower() in ("1", "true", "yes")
    uvicorn.run(create_app(embedded_worker=embedded), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))


app = create_app(embedded_worker=True)

if __name__ == "__main__":  # pragma: no cover
    run()
