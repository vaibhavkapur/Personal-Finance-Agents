import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Dict, Literal, Optional
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, ValidationError
from ..persistence.store import Store
from ..adapters.mock import MockCustodian
from ..workflows.service import WealthService
from ..domain.engine import RuleError, require
from ..agent.tools import ToolGateway, RulesPlanner

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Revision(Input):
    expected_case_version: int
    target_date: str
    monthly_minor: int=Field(ge=0,le=10000000)
    scenario_id: Literal['flat_return_fixture','stress_fixture']='flat_return_fixture'

class Preview(Input):
    target_date: str
    monthly_minor: int=Field(ge=0,le=10000000)
    scenario_id: Literal['flat_return_fixture','stress_fixture']='flat_return_fixture'

class Proposal(Input):
    expected_case_version: int
    contribution_minor: int=Field(ge=0,le=10000000)
    account_id: Literal['taxable','retirement']='taxable'

class Approval(Input):
    expected_case_version: int
    action_payload_hash: str
    approval_challenge_id: str

class Version(Input):
    expected_case_version: int

class Mandate(Version):
    weights_bps: Dict[str,int]
    cash_floor_minor: int=Field(ge=0,le=10000000)

class MandateApproval(Version):
    action_payload_hash: str

class Config(Version):
    mode: Optional[Literal['normal','partial','timeout_after_acceptance','late_contribution','delayed_callback','malformed']]=None
    advance_minutes: int=Field(default=0,ge=0,le=525600)
    refresh: bool=False
    revoke: bool=False

class Message(Input):
    message: str=Field(min_length=1,max_length=2000)

class CreateCase(Input):
    customer_id: Literal['cus_demo_7']='cus_demo_7'
    goal_id: Literal['house']='house'
    revised_target_date: Optional[str]=None
    scenario_id: Literal['flat_return_fixture','stress_fixture']='flat_return_fixture'
    mandate_id: Literal['wealth_mandate_demo']='wealth_mandate_demo'


def create_app(db_path=None,provider_path=None):
    store=Store(db_path)
    service=WealthService(store,MockCustodian(provider_path or os.getenv('CUSTODIAN_DB','var/custodian.db')))
    gateway=ToolGateway(service)
    app=FastAPI(title='Northstar · Goal-Based Wealth Manager',version='1.0.0',description='Local synthetic USD planning. All provider execution is simulated.')
    app.state.service=service
    origins={'http://127.0.0.1:5177','http://localhost:5177','http://127.0.0.1:8017','http://localhost:8017','http://testserver'}

    @app.exception_handler(RuleError)
    async def rule_error(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=exc.status)

    @app.middleware('http')
    async def session(request,call_next):
        path=request.url.path
        if path.startswith('/v1') or path=='/mcp':
            if path=='/v1/provider-events/custodian':
                return await call_next(request)
            if request.method not in ('GET','HEAD'):
                if request.headers.get('x-wealth-client')!='northstar' or (request.headers.get('origin') and request.headers['origin'] not in origins):
                    return JSONResponse({'detail':'Invalid local application origin.'},status_code=403)
            if path!='/v1/session':
                token=request.cookies.get('wealth_session','')
                with store.connect() as db:
                    row=db.execute('SELECT expires FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
                if not row or row['expires']<time.time():
                    return JSONResponse({'detail':'Start a local demo session.'},status_code=401)
        return await call_next(request)

    @app.post('/v1/session')
    def login(response:Response):
        token=secrets.token_urlsafe(32)
        with store.transaction() as db:
            db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?)',(hashlib.sha256(token.encode()).hexdigest(),time.time()+86400))
        response.set_cookie('wealth_session',token,httponly=True,samesite='strict',max_age=86400)
        return {'customer_id':'cus_demo_7','environment':'mock','authentication':'local demo session'}

    @app.get('/health')
    def health():
        return {'status':'ok','environment':'mock'}

    @app.get('/v1/portfolio')
    def portfolio():
        return service.state()

    def case_check(case_id):
        require(case_id==service.state()['case']['id'],'Case not found.',404)

    @app.post('/v1/wealth-cases')
    def create_case(body:CreateCase):
        s=service.state()
        if body.revised_target_date:
            s=service.revise_goal(s['version'],body.revised_target_date,s['goals'][0]['monthly_minor'],body.scenario_id)
        return s['case']

    @app.get('/v1/wealth-cases/{case_id}/goal-status')
    def goal_status(case_id:str):
        case_check(case_id)
        return service.state()['goal_status']

    @app.post('/v1/wealth-cases/{case_id}/scenarios')
    def preview(case_id:str,body:Preview):
        case_check(case_id)
        return service.preview(body.target_date,body.monthly_minor,body.scenario_id)

    @app.post('/v1/wealth-cases/{case_id}/goals')
    def revision(case_id:str,body:Revision):
        case_check(case_id)
        return service.revise_goal(body.expected_case_version,body.target_date,body.monthly_minor,body.scenario_id)

    @app.post('/v1/wealth-cases/{case_id}/rebalance-proposals')
    def proposal(case_id:str,body:Proposal,request:Request):
        case_check(case_id)
        return service.propose(body.expected_case_version,body.contribution_minor,body.account_id,request.headers.get('idempotency-key'))

    @app.post('/v1/actions/{proposal_id}/approve')
    def approve(proposal_id:str,body:Approval):
        return service.approve(proposal_id,body.expected_case_version,body.action_payload_hash,body.approval_challenge_id)

    @app.post('/v1/actions/{proposal_id}/reject')
    def reject(proposal_id:str,body:Version):
        return service.reject(proposal_id,body.expected_case_version)

    @app.post('/v1/mandates/preview')
    def mandate(body:Mandate):
        return service.mandate_draft(body.expected_case_version,body.weights_bps,body.cash_floor_minor)

    @app.post('/v1/mandates/approve')
    def mandate_approve(body:MandateApproval):
        return service.approve_mandate(body.expected_case_version,body.action_payload_hash)

    @app.post('/v1/simulator/configure')
    def configure(body:Config):
        return service.configure(body.expected_case_version,body.mode,body.advance_minutes,body.refresh,body.revoke)

    @app.post('/v1/simulator/settle')
    def settle():
        return service.settle()

    @app.post('/v1/simulator/tick')
    def tick():
        service.tick()
        return service.state()

    @app.post('/v1/agent/messages')
    def chat(body:Message):
        reply=RulesPlanner().respond(body.message,gateway)
        with store.transaction() as db:
            s=store.load(db)
            s['messages']=(s['messages']+[{'role':'user','text':body.message},{'role':'assistant',**reply}])[-30:]
            # Conversation is outside the financial case version.
            store.save(db,s)
        return reply

    @app.post('/v1/provider-events/custodian')
    async def provider_event(request:Request):
        raw=await request.body()
        secret=os.getenv('MOCK_WEBHOOK_SECRET','local-demo-webhook-secret')
        signature=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        require(hmac.compare_digest(request.headers.get('x-custodian-signature',''),signature),'Invalid callback signature.',401)
        try:
            data=json.loads(raw)
            require(isinstance(data.get('event_id'),str) and isinstance(data.get('request_ref'),str),'Malformed callback.')
        except (ValueError,AttributeError):
            raise RuleError('Malformed callback.')
        return service.webhook(data['event_id'],data['request_ref'])

    @app.post('/mcp')
    async def mcp(request:Request):
        body=await request.json()
        msg_id=body.get('id')
        method=body.get('method')
        if method=='notifications/initialized':
            return Response(status_code=202)
        try:
            if method=='initialize':
                result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'northstar-wealth','version':'1.0.0'}}
            elif method=='ping':
                result={}
            elif method=='tools/list':
                result={'tools':gateway.definitions()}
            elif method=='tools/call':
                params=body.get('params',{})
                data=gateway.call(params.get('name'),params.get('arguments',{}))
                result={'content':[{'type':'text','text':json.dumps(data)}],'structuredContent':data,'isError':False}
            else:
                return {'jsonrpc':'2.0','id':msg_id,'error':{'code':-32601,'message':'Method not found'}}
            return {'jsonrpc':'2.0','id':msg_id,'result':result}
        except (RuleError,ValidationError) as exc:
            return {'jsonrpc':'2.0','id':msg_id,'result':{'content':[{'type':'text','text':str(exc)}],'isError':True}}

    dist=Path(__file__).resolve().parents[3]/'frontend'/'dist'
    if dist.exists():
        app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
        @app.get('/favicon.svg')
        def favicon():
            return FileResponse(dist/'favicon.svg')
        @app.get('/{path:path}')
        def frontend(path:str):
            return FileResponse(dist/'index.html')
    return app

app=create_app()
