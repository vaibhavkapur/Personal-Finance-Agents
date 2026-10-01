from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from fastapi import FastAPI, Depends, Request, Response, Header
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from app.api.schemas import Login, Evaluate, Preview, Prepare, Approval, Mandate, Clock, Switch, Run, AgentRequest
from app.agent.tools import ToolService
from app.domain.types import DomainError, instant, uid, wall_time
from app.persistence.store import Store as S
from app.workflows.engine import Engine


def create_app(data_dir=None, run_worker=True):
    if os.getenv("TRADING_ENVIRONMENT", "mock") != "mock":
        raise RuntimeError("This build only supports mock. Live and sandbox execution are disabled.")
    engine = Engine(data_dir or os.getenv("DESK_DATA_DIR", "data"))

    async def worker():
        while True:
            try:
                await asyncio.to_thread(engine.tick)
            except Exception:
                import logging
                logging.getLogger("meridian.worker").exception("Worker tick failed; persisted jobs remain recoverable")
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker()) if run_worker else None
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Meridian Personal Trading Desk", version="1.0.0", lifespan=lifespan)
    app.state.engine = engine

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message, "reasons": exc.reasons})

    @app.middleware("http")
    async def boundaries(request, call_next):
        if request.url.path.startswith("/v1") and request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path != "/v1/provider-events/broker":
            origin = request.headers.get("origin")
            allowed = {"http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173", "http://testserver"}
            if origin and origin not in allowed or request.headers.get("x-desk-request") != "1":
                return JSONResponse(status_code=403, content={"detail": "Untrusted request origin or missing X-Desk-Request header"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/v1"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authenticated(request: Request):
        token = request.cookies.get("desk_session", "")
        if not token:
            raise DomainError("Sign in to the local paper desk", 401)
        with engine.store.tx() as db:
            try:
                session = S.get(db, "settings", "session_" + hashlib.sha256(token.encode()).hexdigest())
            except DomainError:
                raise DomainError("Session is invalid", 401)
        if instant(wall_time()) >= instant(session["expires_at"]):
            raise DomainError("Session has expired", 401)
        return session

    @app.get("/health")
    def health():
        return {"status": "ok", "environment": "mock"}

    @app.post("/v1/session")
    def login(body: Login, response: Response):
        if not secrets.compare_digest(body.password, os.getenv("DESK_DEMO_PASSWORD", "paper-demo")):
            raise DomainError("Incorrect desk password", 401)
        token = secrets.token_urlsafe(32)
        with engine.store.tx() as db:
            S.put(db, "settings", {"id": "session_" + hashlib.sha256(token.encode()).hexdigest(), "actor": "demo-reviewer", "tenant": "demo", "expires_at": (instant(wall_time()) + timedelta(hours=8)).isoformat()})
        response.set_cookie("desk_session", token, httponly=True, samesite="strict", secure=os.getenv("DESK_SECURE_COOKIE") == "1", max_age=28800)
        return {"actor": "demo-reviewer", "environment": "mock"}

    @app.delete("/v1/session")
    def logout(request: Request, response: Response, session=Depends(authenticated)):
        with engine.store.tx() as db:
            db.execute("DELETE FROM settings WHERE id=?", (session["id"],))
        response.delete_cookie("desk_session")
        return {"signed_out": True}

    @app.get("/v1/desk")
    def desk(session=Depends(authenticated)):
        return engine.dashboard(session["tenant"])

    @app.post("/v1/evaluate")
    def evaluate(body: Evaluate, session=Depends(authenticated)):
        return engine.evaluate_run(body.symbols, body.mandate_id, session["tenant"])

    @app.post("/v1/trading-runs")
    def run(body: Run, session=Depends(authenticated)):
        with engine.store.tx() as db:
            S.get(db, "trading_mandates", body.mandate_id, session["tenant"])
            S.get(db, "market_snapshots", body.market_snapshot_id, session["tenant"])
            case = {"id": uid("run"), **body.model_dump(), "status": "signal_created", "version": 1, "next_step": "evaluate_strategy_and_risk", "created_at": engine.runtime(db)["now"]}
            return S.put(db, "cases", case, session["tenant"])

    @app.post("/v1/trading-runs/{id}/evaluate")
    def evaluate_case(id: str, session=Depends(authenticated)):
        with engine.store.tx() as db:
            case = S.get(db, "cases", id, session["tenant"])
            snapshot = S.get(db, "market_snapshots", case["market_snapshot_id"], session["tenant"])
        result = engine.evaluate_run([snapshot["symbol"]], case["mandate_id"], session["tenant"], id, snapshot["id"])
        with engine.store.tx() as db:
            case.update(status="risk_checked" if result[0]["risk"]["allowed"] else "blocked", version=case["version"] + 1, signal_ids=[r["id"] for r in result])
            S.put(db, "cases", case, session["tenant"])
        return {"case": case, "signals": result}

    @app.post("/v1/orders/preview")
    def preview(body: Preview, session=Depends(authenticated)):
        return engine.preview(body.signal_id, session["tenant"])

    @app.post("/v1/orders")
    def prepare(body: Prepare, idempotency_key: str = Header(...), session=Depends(authenticated)):
        return engine.prepare(body.signal_id, body.preview_id, body.scenario, idempotency_key, session["tenant"])

    @app.get("/v1/orders/{id}/executions")
    def report(id: str, session=Depends(authenticated)):
        with engine.store.tx() as db:
            return engine.order_detail(db, id, session["tenant"])

    @app.post("/v1/orders/{id}/cancel-drafts")
    def cancel(id: str, idempotency_key: str = Header(...), session=Depends(authenticated)):
        return engine.cancel_draft(id, idempotency_key, session["tenant"])

    @app.post("/v1/orders/{id}/discard")
    def discard(id: str, session=Depends(authenticated)):
        return engine.discard(id, session["tenant"])

    @app.post("/v1/actions/{id}/approve")
    def approve(id: str, body: Approval, idempotency_key: str = Header(...), session=Depends(authenticated)):
        return engine.approve(id, body.expected_case_version, body.action_payload_hash, body.approval_challenge_id, idempotency_key, session["actor"], session["tenant"])

    @app.post("/v1/trading-mandates")
    def mandate(body: Mandate, session=Depends(authenticated)):
        return engine.create_mandate(body.model_dump(), session["tenant"])

    @app.post("/v1/trading-mandates/{id}/revoke")
    def revoke(id: str, session=Depends(authenticated)):
        return engine.revoke_mandate(id, session["tenant"])

    @app.post("/v1/controls/kill-switch")
    def kill(body: Switch, session=Depends(authenticated)):
        return engine.set_kill(body.enabled)

    @app.post("/v1/replay/advance")
    def clock(body: Clock, session=Depends(authenticated)):
        return engine.advance_clock(body.seconds, body.refresh_quotes)

    @app.post("/v1/reconcile")
    def reconcile(session=Depends(authenticated)):
        return engine.reconcile(session["tenant"])

    @app.post("/v1/worker/tick")
    def tick(session=Depends(authenticated)):
        return engine.tick()

    @app.post("/v1/agent/explain")
    def explain(body: AgentRequest, session=Depends(authenticated)):
        return ToolService(engine, session["tenant"]).explain(body.symbol, body.max_tool_calls)

    @app.post("/v1/provider-events/broker")
    async def callback(request: Request):
        secret = os.getenv("DESK_WEBHOOK_SECRET")
        if not secret:
            raise DomainError("External callback ingestion is disabled until a webhook secret is configured", 503)
        raw = await request.body()
        timestamp = request.headers.get("x-broker-timestamp", "")
        try:
            if abs(time.time() - int(timestamp)) > 300:
                raise ValueError()
        except ValueError:
            raise DomainError("Callback timestamp is missing or outside the replay window", 401)
        expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("x-broker-signature", "")):
            raise DomainError("Invalid callback signature", 401)
        try:
            payload = json.loads(raw)
            event_id, order_id = payload["id"], payload["order_id"]
            if not isinstance(event_id, str) or not isinstance(order_id, str) or payload.get("environment") != "mock":
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise DomainError("Malformed mock provider event")
        with engine.store.tx() as db:
            order = S.get(db, "orders", order_id)
            duplicate = db.execute("SELECT 1 FROM event_inbox WHERE provider='mock' AND event_id=?", (event_id,)).fetchone()
        if duplicate:
            return {"duplicate": True}
        # Callback content is merely a wake-up. Resolve authoritative state by original ID.
        report = engine.broker.get_order_by_client_id(order["client_order_id"])
        if not report:
            raise DomainError("Original broker order cannot be resolved", 409)
        engine.apply_report(order_id, report)
        with engine.store.tx() as db:
            db.execute("INSERT OR IGNORE INTO event_inbox VALUES('mock',?,?)", (event_id, wall_time()))
        return {"duplicate": False, "reconciled": True}

    dist = Path(__file__).parents[2] / "frontend/dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        @app.get("/")
        def index():
            return FileResponse(dist / "index.html")
    return app


app = create_app(run_worker=os.getenv("DESK_EMBEDDED_WORKER", "1") == "1")
