from __future__ import annotations
import os, secrets, hashlib, hmac, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import FastAPI, Depends, Request, Response, Header
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func
from backend.app.persistence.store import *
from backend.app.domain.engine import *
from backend.app.domain.registry import *
from backend.app.api.schemas import *
from backend.app.workflows.worker import Worker
from backend.app.workflows.events import EventProcessor
from backend.app.agent.tools import Tools, Orchestrator, PROTOCOL_VERSION


def create_app(store=None):
    app=FastAPI(title="Fraud-Response Concierge",version="1.0.0",description="Synthetic incidents only. No real bank actions.")
    store=store or Store(); service=Service(store); worker=Worker(store); processor=EventProcessor(store)
    app.state.store=store; app.state.worker=worker
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request,error):
        # Never echo rejected authentication secrets into responses/logs.
        return JSONResponse(status_code=422,content={"detail":"Invalid request fields. Use the documented schema and never include authentication secrets."})
    @app.middleware("http")
    async def origin_guard(request,call_next):
        origin=request.headers.get("origin")
        allowed={"http://127.0.0.1:8093","http://localhost:8093","http://127.0.0.1:5193","http://localhost:5193"}
        allowed.update(filter(None,os.getenv("APP_ORIGINS","").split(",")))
        if request.method not in {"GET","HEAD","OPTIONS"} and origin and origin not in allowed:
            return JSONResponse(status_code=403,content={"detail":"Untrusted origin"})
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Frame-Options"]="DENY"
        if request.url.path.startswith(("/v1","/mcp")): response.headers["Cache-Control"]="no-store"
        return response
    def customer(request:Request):
        token=request.cookies.get("concierge_session")
        authorization=request.headers.get("authorization","")
        if authorization.startswith("Bearer "): token=authorization[7:]
        if not token: fail(401,"Open a synthetic demo session first")
        with store.tx() as c: session=row(c,sessions,sessions.c.token_hash==hashlib.sha256(token.encode()).hexdigest())
        if not session or session["expires_at"]<=iso(datetime.now(timezone.utc)): fail(401,"Session expired")
        return session["customer_id"]
    @app.get("/health")
    def health(): return {"status":"ok","environment":"mock"}
    @app.post("/v1/demo/session")
    def demo_session(response:Response):
        if os.getenv("ENABLE_DEMO","1")!="1": fail(403,"Demo sessions are disabled")
        token=secrets.token_urlsafe(32)
        with store.tx() as c:
            c.execute(sessions.insert().values(token_hash=hashlib.sha256(token.encode()).hexdigest(),customer_id=CUSTOMER,expires_at=iso(datetime.now(timezone.utc)+timedelta(hours=8))))
        response.set_cookie("concierge_session",token,httponly=True,samesite="strict",secure=os.getenv("COOKIE_SECURE")=="1",max_age=28800)
        return {"customer_id":CUSTOMER,"name":"Alex Morgan","environment":"mock"}
    @app.get("/v1/instruments")
    def instruments(cust=Depends(customer)): return {"instruments":INSTRUMENTS,"providers":PROVIDERS,"transactions":TRANSACTIONS,"clock":store.now(),"environment":"mock"}
    @app.get("/v1/incidents")
    def list_incidents(cust=Depends(customer)):
        with store.tx() as c: return rows(c,cases,cases.c.customer_id==cust)
    @app.post("/v1/incidents",status_code=201)
    def create(body:Intake,cust=Depends(customer)):
        if body.customer_id!=cust: fail(403,"Customer does not match session")
        return service.create(cust,iso(body.discovered_at),body.instrument_refs,body.unrecognized_transaction_refs,body.narrative)
    @app.get("/v1/incidents/{case_id}/resolution")
    def resolution(case_id:str,cust=Depends(customer)): return service.resolution(case_id,cust)
    @app.post("/v1/incidents/{case_id}/verify")
    def verify(case_id:str,body:Version,cust=Depends(customer)): return service.verify(case_id,cust,body.expected_case_version)
    @app.post("/v1/incidents/{case_id}/affected-instruments")
    def affected(case_id:str,body:Affected,cust=Depends(customer)):
        with store.tx() as c:
            case=service.get(c,case_id,cust); service.check_version(case,body.expected_case_version)
            if case["data"]["identity_verified"]: fail(409,"Affected instruments are fixed once verified in this prototype")
            if len(set(body.instrument_refs))!=len(body.instrument_refs) or not set(body.instrument_refs)<={i["id"] for i in INSTRUMENTS}: fail(422,"Unknown instruments")
            data=fixtures(body.instrument_refs,[t["id"] for t in TRANSACTIONS if t["instrument_id"] in body.instrument_refs]); data["narrative"]=case["data"]["narrative"]
            case["data"]=data; service.save(c,case,"instruments.confirmed",cust)
        return service.resolution(case_id,cust)
    @app.post("/v1/incidents/{case_id}/transactions/{ref}/confirm")
    def confirm(case_id:str,ref:str,body:Confirmation,cust=Depends(customer)): return service.confirm(case_id,cust,ref,body.expected_case_version,body.choice,body.statement)
    @app.post("/v1/incidents/{case_id}/protective-action-drafts")
    @app.post("/v1/incidents/{case_id}/report-drafts")
    def draft(case_id:str,body:Draft,idempotency_key:str=Header(min_length=8,max_length=100),cust=Depends(customer)):
        return service.draft(case_id,cust,body.kind,body.target_id,body.expected_case_version,idempotency_key,body.statement)
    @app.post("/v1/actions/{action_id}/approve")
    def approve(action_id:str,body:Approval,cust=Depends(customer)):
        return service.approve(action_id,cust,body.expected_case_version,body.action_payload_hash,body.approval_challenge_id)
    @app.post("/v1/actions/{action_id}/revoke")
    def revoke(action_id:str,cust=Depends(customer)): return service.revoke(action_id,cust)
    @app.post("/v1/actions/{action_id}/reconcile")
    async def reconcile(action_id:str,cust=Depends(customer)): return await worker.reconcile(action_id,cust)
    @app.post("/v1/actions/{action_id}/demo-handoff")
    async def handoff(action_id:str,cust=Depends(customer)):
        with store.tx() as c:
            a=row(c,actions,actions.c.id==action_id)
            if not a or a["customer_id"]!=cust: fail(404,"Action not found")
            if a["status"]!="direct_customer_action_required": fail(409,"No handoff is pending")
        await worker.bank.complete_handoff(action_id)
        return await worker.reconcile(action_id,cust)
    @app.post("/v1/incidents/{case_id}/tasks/{task_id}/complete")
    def complete_task(case_id:str,task_id:str,body:Note,cust=Depends(customer)):
        safe_text(body.statement)
        with store.tx() as c:
            case=service.get(c,case_id,cust); service.check_version(case,body.expected_case_version)
            task=next((t for t in case["data"]["tasks"] if t["id"]==task_id),None)
            if not task: fail(404,"Task not found")
            if instrument(case,task["instrument_id"])["replacement_status"]!="delivered": fail(409,"Wait for replacement delivery before reviewing recurring payments")
            task.update(status="completed",completion_evidence_id=uid("customer_attestation"),completion_statement=body.statement)
            service.save(c,case,"recovery.customer_confirmed",cust,{"task_id":task_id,"evidence_id":task["completion_evidence_id"]})
        return service.resolution(case_id,cust)
    @app.post("/v1/incidents/{case_id}/close")
    def close(case_id:str,body:Version,cust=Depends(customer)):
        with store.tx() as c:
            case=service.get(c,case_id,cust); service.check_version(case,body.expected_case_version)
            if not service.can_close(case): fail(409,"Protection, reports, replacements, investigations and recovery must all be resolved")
            outstanding=rows(c,actions,(actions.c.case_id==case_id)&actions.c.status.in_(["draft","queued","dispatching","unknown","manual_review","direct_customer_action_required"]))
            if outstanding: fail(409,"An action remains unresolved")
            service.save(c,case,"incident.closed",cust,{"completion_evidence":"provider references and recovery attestations"},force="closed")
        return service.resolution(case_id,cust)
    @app.post("/v1/provider-events/incidents")
    async def provider_event(request:Request,x_provider_signature:str=Header()):
        raw=await request.body()
        secret=os.getenv("PROVIDER_SHARED_SECRET","local-fixture-webhook-secret")
        expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,x_provider_signature): fail(401,"Invalid provider signature")
        try:
            body=ProviderEvent.model_validate_json(raw)
        except ValidationError: fail(422,"Invalid event")
        event=body.model_dump(mode="json"); event["occurred_at"]=iso(body.occurred_at)
        return processor.apply(event)
    @app.post("/v1/incidents/{case_id}/simulate")
    def simulate(case_id:str,body:Simulation,cust=Depends(customer)):
        case=service.resolution(case_id,cust)
        if body.type=="replacement_delivered":
            inst=instrument(case,body.target_id); tx=None; ref=inst.get("replacement_reference")
        else:
            tx=transaction(case,body.target_id); inst=instrument(case,tx["instrument_id"]); ref=tx.get("provider_case_ref")
        if not ref: fail(409,"Submit and verify the corresponding action first")
        return processor.apply({"event_id":uid("mock_event"),"case_id":case_id,"customer_id":cust,"provider_id":inst["provider_id"],"instrument_id":inst["id"],"transaction_id":tx["id"] if tx else None,"provider_reference":ref,"type":body.type,"revision":tx.get("provider_revision",0)+1 if tx else 1,"occurred_at":store.now(),"environment":"mock"})
    @app.post("/v1/incidents/{case_id}/scenario")
    def scenario(case_id:str,body:Scenario,cust=Depends(customer)):
        with store.tx() as c:
            case=service.get(c,case_id,cust); service.check_version(case,body.expected_case_version)
            case["data"]["scenario"]=body.scenario; service.save(c,case,"simulator.scenario_changed",cust,{"scenario":body.scenario})
        return {"scenario":body.scenario}
    @app.post("/v1/demo/clock/advance")
    def advance(body:ClockAdvance,cust=Depends(customer)):
        value=iso(parse(store.now())+timedelta(minutes=body.minutes))
        with store.tx() as c: c.execute(settings.update().where(settings.c.key=="clock").values(value={"now":value}))
        return {"clock":value}
    @app.post("/v1/demo/worker/tick")
    async def tick(cust=Depends(customer)):
        # A convenience for deterministic demos; the independent durable worker is preferred.
        return {"processed":await worker.run_one()}
    @app.get("/v1/incidents/{case_id}/operations")
    def operations(case_id:str,cust=Depends(customer)):
        with store.tx() as c:
            case=service.get(c,case_id,cust)
            action_rows=rows(c,actions,actions.c.case_id==case_id); ids=[a["id"] for a in action_rows]
            job_rows=rows(c,jobs,jobs.c.action_id.in_(ids))
            runs=rows(c,tool_runs,tool_runs.c.case_id==case_id)
            return {"jobs":job_rows,"tool_runs":runs,"metrics":{"pending_jobs":sum(j["status"] in {"running","pending"} for j in job_rows),"tool_errors":sum(r["outcome"]!="success" for r in runs),"unresolved_actions":sum(a["status"] in {"unknown","manual_review","failed"} for a in action_rows),"model_cost_usd":0,"provider_requests":len(rows(c,provider_actions,provider_actions.c.request_ref.in_(ids)))}}
    @app.post("/v1/incidents/{case_id}/chat")
    def chat(case_id:str,body:Chat,cust=Depends(customer)): return Orchestrator(Tools(store,cust)).respond(body.message,case_id)
    @app.post("/mcp")
    async def mcp(request:Request,cust=Depends(customer)):
        try: body=await request.json()
        except ValueError: return JSONResponse(status_code=400,content={"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}})
        if not isinstance(body,dict) or not isinstance(body.get("params",{}),dict):
            return JSONResponse(status_code=400,content={"jsonrpc":"2.0","id":None,"error":{"code":-32600,"message":"Invalid Request"}})
        ident=body.get("id"); method=body.get("method"); t=Tools(store,cust)
        if method=="notifications/initialized": return Response(status_code=202)
        try:
            if body.get("jsonrpc")!="2.0": return {"jsonrpc":"2.0","id":ident,"error":{"code":-32600,"message":"Invalid Request"}}
            if method=="initialize": result={"protocolVersion":PROTOCOL_VERSION,"serverInfo":{"name":"fraud-response-concierge","version":"1.0.0"},"capabilities":{"tools":{"listChanged":False}}}
            elif method=="ping": result={}
            elif method=="tools/list": result={"tools":t.list()}
            elif method=="tools/call":
                params=body.get("params",{}); output=t.call(params.get("name"),params.get("arguments",{}))
                result={"content":[{"type":"text","text":json.dumps(output)}],"structuredContent":output,"isError":False}
            else: return {"jsonrpc":"2.0","id":ident,"error":{"code":-32601,"message":"Method not found"}}
            return {"jsonrpc":"2.0","id":ident,"result":result}
        except (HTTPException,ValidationError) as exc:
            return {"jsonrpc":"2.0","id":ident,"result":{"content":[{"type":"text","text":exc.detail if isinstance(exc,HTTPException) else "Invalid tool arguments"}],"isError":True}}
    dist=Path(__file__).resolve().parents[3]/"frontend"/"dist"
    if dist.exists():
        app.mount("/assets",StaticFiles(directory=dist/"assets"),name="assets")
        @app.get("/{path:path}")
        def spa(path:str):
            if path.startswith(("v1/","mcp")): fail(404,"Route not found")
            return FileResponse(dist/"favicon.svg" if path=="favicon.svg" else dist/"index.html")
    return app

app=create_app()
