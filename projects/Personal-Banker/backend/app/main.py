"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import actions, agent as agent_api, banking_cases, operator, provider_events
from app.config import REPO_ROOT, settings
from app.persistence import db
from app.persistence.seed import seed
from app.workflows.errors import CaseError
from app.workflows.states import IllegalTransition, StaleVersion

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.create_schema()
    with db.session_scope() as session:
        from app.persistence.models import Customer

        if session.get(Customer, "cus_demo_1") is None:
            seed(session)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Personal Banker",
        version="0.1.0",
        description="CD maturity concierge: cash projection, offer comparison, exact-action approval and mock bank execution.",
        lifespan=lifespan,
    )

    @app.exception_handler(CaseError)
    async def _case_error(_: Request, exc: CaseError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(StaleVersion)
    async def _stale(_: Request, exc: StaleVersion):
        return JSONResponse(status_code=409, content={"error": "stale_case_version", "message": str(exc)})

    @app.exception_handler(IllegalTransition)
    async def _illegal(_: Request, exc: IllegalTransition):
        return JSONResponse(status_code=409, content={"error": "illegal_transition", "message": str(exc)})

    app.include_router(banking_cases.router)
    app.include_router(actions.router)
    app.include_router(provider_events.router)
    app.include_router(operator.router)
    app.include_router(agent_api.router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "environment": settings.environment}

    if settings.serve_frontend and FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            candidate = FRONTEND_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
