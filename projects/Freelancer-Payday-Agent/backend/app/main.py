import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from .api.schemas import Approval, AgentMessage, Confirmations, Draft, NewCase, Policy, PolicyApproval, Simulation
from .agent.tools import assist
from .domain.engine import DomainError, digest
from .persistence.store import ROOT, uid
from .workflows.service import CUSTOMER, TENANT, PaydayService
from .worker import run_once


def create_app(data_dir=None):
    service = PaydayService(data_dir or os.getenv("PAYDAY_DATA_DIR", "data"))
    service.seed()
    app = FastAPI(title="Freelancer Payday Agent", version="0.1.0", description="USD mock-only cash planning and reviewed same-owner payouts.")
    app.state.service = service
    # Persistent secret enables API/worker restart without reissuing demo sessions.
    secret_file = Path(data_dir or os.getenv("PAYDAY_DATA_DIR", "data")) / "session.key"
    if not secret_file.exists():
        try:
            fd = os.open(str(secret_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    token = os.getenv("PAYDAY_DEMO_TOKEN") or secret_file.read_text().strip()
    webhook_secret = os.getenv("PAYDAY_WEBHOOK_SECRET", token)
    allowed_origins = set(os.getenv("PAYDAY_ORIGINS", "http://127.0.0.1:5182,http://localhost:5182,http://127.0.0.1:8012,http://localhost:8012").split(","))

    @app.middleware("http")
    async def origin_guard(request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            return JSONResponse({"detail":"Origin not allowed."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail":exc.message}, status_code=exc.status)

    def auth(request: Request):
        bearer = request.headers.get("authorization", "")
        candidate = bearer[7:] if bearer.startswith("Bearer ") else request.cookies.get("payday_session", "")
        if not candidate or not hmac.compare_digest(candidate, token):
            raise DomainError("Sign in to the local demo first.", 401)
        return {"tenant":TENANT,"customer":CUSTOMER,"actor":"alex_demo"}

    @app.get("/health")
    def health():
        return {"status":"ok","environment":"mock"}

    @app.post("/v1/demo/session")
    def demo_session(request: Request, response: Response):
        if os.getenv("PAYDAY_DEMO_LOGIN", "1") != "1":
            raise DomainError("Demo login disabled.", 403)
        if request.url.hostname not in ("localhost", "127.0.0.1", "testserver"):
            raise DomainError("Demo login is available only on the local host.", 403)
        response.set_cookie("payday_session", token, httponly=True, samesite="strict", secure=False, max_age=86400)
        return {"customer_id":CUSTOMER,"name":"Alex Morgan","environment":"mock"}

    @app.post("/v1/session/logout")
    def logout(response: Response):
        response.delete_cookie("payday_session")
        return {"signed_out":True}

    @app.get("/v1/dashboard")
    def dashboard(who=Depends(auth)):
        return service.dashboard(who["tenant"], who["customer"])

    @app.post("/v1/payday-cases", status_code=201)
    def new_case(body: NewCase, who=Depends(auth)):
        p = service.profile(who["tenant"], body.customer_id)
        if body.policy_id != p["policy"]["id"]:
            raise DomainError("Reserve policy not found.", 404)
        return service.create_case(who["tenant"], body.customer_id, body.requested_payout_minor, body.period)

    @app.get("/v1/payday-cases/{case_id}")
    def get_case(case_id: str, who=Depends(auth)):
        return service.case(who["tenant"], case_id)

    @app.post("/v1/payday-cases/{case_id}/calculate")
    def calculate_case(case_id: str, who=Depends(auth)):
        return service.calculate_case(who["tenant"], case_id)

    @app.post("/v1/payday-cases/{case_id}/receipt-confirmations")
    def confirm(case_id: str, body: Confirmations, who=Depends(auth)):
        return service.confirm(who["tenant"], case_id, body.model_dump()["confirmations"])

    @app.post("/v1/payday-cases/{case_id}/transfer-drafts")
    def draft(case_id: str, body: Draft, idempotency_key: str = Header(..., min_length=1, max_length=100), who=Depends(auth)):
        return service.draft(who["tenant"], case_id, body.proposal_id, idempotency_key, body.simulation_mode)

    @app.post("/v1/actions/{action_id}/approve")
    def approve(action_id: str, body: Approval, who=Depends(auth)):
        return service.approve(who["tenant"], action_id, body.model_dump(), who["actor"])

    @app.post("/v1/actions/{action_id}/cancel")
    def cancel(action_id: str, who=Depends(auth)):
        return service.cancel(who["tenant"], action_id, who["actor"])

    @app.post("/v1/actions/{action_id}/revoke")
    def revoke(action_id: str, who=Depends(auth)):
        now = service.now()
        with service.store.transaction() as db:
            a = service.store.get(db, "actions", action_id, who["tenant"])
            if a["status"] != "reserved":
                raise DomainError("Approval can be revoked only before submission.")
            approval = service.store.get(db, "approvals", a["approval_id"], who["tenant"])
            approval["revoked_at"] = now
            service.store.put(db, "approvals", approval, who["tenant"], a["customer_id"])
        return {"revoked":True}

    @app.get("/v1/payday-cases/{case_id}/cash-buckets")
    def buckets(case_id: str, who=Depends(auth)):
        c = service.case(who["tenant"], case_id)
        p = service.refresh(who["tenant"], c["customer_id"])
        return {"buckets":p["buckets"],"bank":p["bank_snapshot"],"currency":"USD","environment":"mock"}

    @app.get("/v1/payday-cases/{case_id}/timeline")
    def timeline(case_id: str, who=Depends(auth)):
        c = service.case(who["tenant"], case_id)
        return [e for e in service.dashboard(who["tenant"], c["customer_id"])["timeline"] if e["case_id"] == case_id]

    @app.post("/v1/payday-cases/{case_id}/agent")
    def agent(case_id: str, body: AgentMessage, who=Depends(auth)):
        return assist(service, who["tenant"], case_id, body.message, body.tool_budget)

    @app.post("/v1/reserve-policy/reviews")
    def policy_review(body: Policy, who=Depends(auth)):
        return service.policy_review(who["tenant"], who["customer"], body.model_dump(mode="json"))

    @app.post("/v1/reserve-policy/reviews/{review_id}/approve")
    def approve_policy(review_id: str, body: PolicyApproval, who=Depends(auth)):
        return service.approve_policy(who["tenant"], review_id, body.model_dump(), who["actor"])

    @app.post("/v1/operator/run-worker")
    def worker(who=Depends(auth)):
        return {"result":run_once(service)}

    @app.post("/v1/operator/jobs/{job_id}/retry")
    def retry_job(job_id: str, who=Depends(auth)):
        with service.store.transaction() as db:
            job = db.execute("SELECT * FROM outbox WHERE id=? AND tenant=?", (job_id, who["tenant"])).fetchone()
            if not job or job["status"] != "held":
                raise DomainError("Only a held job can be retried after review.")
            # This only queues the original action. The executor rechecks authority.
            db.execute("UPDATE outbox SET status='pending',lease_until=0,attempts=0 WHERE id=?", (job_id,))
        return {"queued":job_id}

    @app.post("/v1/simulator/events")
    def simulate(body: Simulation, who=Depends(auth)):
        tenant, customer, now = who["tenant"], who["customer"], service.now()
        p = service.profile(tenant, customer)
        if body.kind == "advance":
            return service.advance(body.seconds)
        if body.kind == "settle":
            refs = service.bank.settle_pending(now)
            service.refresh(tenant, customer)
            return {"settled":refs}
        if body.kind == "return":
            with service.store.transaction() as db:
                t = service.store.get(db, "payday_transfers", body.transfer_id, tenant)
            service.bank.return_transfer(t["request_ref"], now)
            return service.refresh(tenant, customer)
        if body.kind == "bill_change":
            return service.change_inputs(tenant, customer, lambda profile: profile["bills"][0].update(amount_minor=body.amount_minor))
        with service.store.transaction() as db:
            if service._pending(db, tenant, customer):
                raise DomainError("Resolve the pending payout before changing the demo cash scenario.")
        if body.kind == "late_scenario":
            if any(r["id"] == "rcpt_equipment" for r in p["receipts"]):
                return p
            service.bank.deposit(p["business_account_id"], -250000, "equipment_fixture", now)
            def equipment(profile):
                profile["receipts"].append({"id":"rcpt_equipment","provider_transaction_id":"equipment_fixture","client":"Studio equipment","description":"Posted business purchase","amount_minor":-250000,"category":"expense","confirmed":True,"posted_at":now[:10],"evidence_id":"mock:equipment_fixture"})
            return service.change_inputs(tenant, customer, equipment)
        if body.kind in ("late_receipt", "ambiguous_receipt"):
            ref = "orbit_invoice_043" if body.kind == "late_receipt" else (body.reference or "ambiguous_fixture_1")
            amount = 500000 if body.kind == "late_receipt" else (body.amount_minor or 45000)
            prior = next((r for r in p["receipts"] if r["provider_transaction_id"] == ref), None)
            if prior:
                if prior["amount_minor"] != amount:
                    raise DomainError("Receipt reference reused with different content.")
                return p
            entry = service.bank.deposit(p["business_account_id"], amount, ref, now)
            def incoming(profile):
                if any(r["provider_transaction_id"] == ref for r in profile["receipts"]):
                    return
                profile["receipts"].append({"id":ref,"provider_transaction_id":ref,"client":"Orbit Labs" if body.kind == "late_receipt" else "Unidentified sender", "description":"Posted invoice payment" if body.kind == "late_receipt" else "Receipt needs your confirmation", "amount_minor":amount,"category":"income" if body.kind == "late_receipt" else "unknown","confirmed":body.kind == "late_receipt","posted_at":now[:10],"evidence_id":entry})
                if body.kind == "late_receipt":
                    profile["invoices"][0].update(status="paid", paid_transaction_id=ref)
            return service.change_inputs(tenant, customer, incoming)
        raise DomainError("Unsupported simulator event.", 400)

    @app.post("/v1/provider-events/payday")
    async def webhook(request: Request, x_payday_signature: str = Header(...), x_payday_timestamp: str = Header(...)):
        raw = await request.body()
        if len(raw) > 65536:
            raise DomainError("Event too large.", 413)
        try:
            recent = abs(time.time() - int(x_payday_timestamp)) <= 300
        except ValueError:
            recent = False
        expected = hmac.new(webhook_secret.encode(), x_payday_timestamp.encode()+b"."+raw, hashlib.sha256).hexdigest()
        if not recent or not hmac.compare_digest(expected, x_payday_signature):
            raise DomainError("Invalid callback signature or timestamp.", 401)
        try:
            event = json.loads(raw)
            if event["environment"] != "mock" or event["type"] not in ("payday.transfer.posted", "payday.transfer.returned", "payday.transfer.failed"):
                raise ValueError()
            c = service.case(TENANT, event["case_id"])
            if not isinstance(event["id"], str) or not event["id"]:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise DomainError("Invalid provider event.", 422)
        now = service.now()
        with service.store.transaction() as db:
            existing = db.execute("SELECT payload_hash FROM event_inbox WHERE provider='mock' AND event_id=?", (event["id"],)).fetchone()
            if existing and existing[0] != digest(event):
                raise DomainError("Event ID reused with changed content.")
            db.execute("INSERT OR IGNORE INTO event_inbox VALUES(?,?,?,?,?)", ("mock", event["id"], digest(event), json.dumps(event), now))
        # Treat the event as a signal. Authoritative lookup and matched entries determine state.
        service.refresh(TENANT, c["customer_id"])
        return {"accepted":True,"duplicate":bool(existing)}

    @app.post("/v1/operator/events/{event_id}/replay")
    def replay(event_id: str, who=Depends(auth)):
        with service.store.transaction() as db:
            row = db.execute("SELECT payload FROM event_inbox WHERE provider='mock' AND event_id=?", (event_id,)).fetchone()
            if not row:
                raise DomainError("Event not found.", 404)
            event = json.loads(row[0])
        c = service.case(who["tenant"], event["case_id"])
        service.refresh(who["tenant"], c["customer_id"])
        return {"replayed":event_id}

    dist = ROOT / "frontend/dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        @app.get("/")
        def index():
            return FileResponse(dist / "index.html")
    return app


app = create_app()
