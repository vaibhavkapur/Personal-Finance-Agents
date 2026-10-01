import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from backend.app.agent.tools import AgentTools
from backend.app.domain.engine import find, requirements, transition
from backend.app.persistence.store import DomainError, Store, jobs, settings, uid
from backend.app.workflows.service import EstateService
from backend.app.workflows.worker import Worker


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Version(Strict):
    expected_case_version: int = Field(ge=1)


class Login(Strict):
    username: Literal['alex', 'other'] = 'alex'
    password: str


class CreateCase(Strict):
    customer_id: Literal['cus_demo_11'] = 'cus_demo_11'
    decedent_record_id: Literal['syn_evelyn_morgan'] = 'syn_evelyn_morgan'
    jurisdiction_profile: Literal['US_CA_MOCK'] = 'US_CA_MOCK'
    claimed_role: Literal['personal_representative'] = 'personal_representative'


class Draft(Version):
    idempotency_key: str = Field(min_length=8, max_length=100)


class Approve(Version):
    action_payload_hash: str
    approval_challenge_id: str


class AddDocument(Version):
    kind: Literal['bank_statement', 'insurance_policy', 'retirement_statement', 'death_certificate', 'letters_of_authority', 'certified_authority', 'retirement_claim_form']
    name: str | None = Field(default=None, max_length=120)
    content: str | None = Field(default=None, max_length=30000)


class Scenario(Version):
    scenario: Literal['normal', 'timeout_after_acceptance', 'delayed', 'declined', 'no_match', 'malformed', 'wrong_destination']


class Advance(Strict):
    seconds: int = Field(ge=1, le=86400 * 730)


def create_app(store=None, background=True):
    store = store or Store()
    service = EstateService(store)
    worker = Worker(store)
    agent = AgentTools(service)
    # A stable server-only secret shared across restarts; never placed in frontend code.
    secret_path = Path(__file__).resolve().parents[3] / 'data' / 'session.key'
    try:
        fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    secret = os.getenv('SESSION_SECRET', '').encode() or secret_path.read_bytes()

    @asynccontextmanager
    async def lifespan(app):
        stop = threading.Event()
        def loop():
            while not stop.wait(1):
                try:
                    worker.tick()
                except Exception:
                    # Persisted lease allows next iteration or another worker to recover.
                    continue
        thread = None
        if background and os.getenv('EMBEDDED_WORKER', '1') == '1':
            thread = threading.Thread(target=loop, daemon=True)
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=2)

    app = FastAPI(title='Everkeep · Inheritance and Dormant-Asset Navigator', version='1.0.0', lifespan=lifespan)
    app.state.store, app.state.service, app.state.worker = store, service, worker

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'detail': exc.message})

    @app.middleware('http')
    async def origin_guard(request, call_next):
        origin = request.headers.get('origin')
        allowed = set(os.getenv('ALLOWED_ORIGINS', 'http://127.0.0.1:5187,http://localhost:5187,http://127.0.0.1:8731,http://localhost:8731').split(','))
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin not in allowed:
            return JSONResponse(status_code=403, content={'detail': 'Origin not allowed.'})
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def auth(request: Request):
        token = request.cookies.get('estate_session', '')
        try:
            data, signature = token.rsplit('.', 1)
            wanted = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(wanted, signature):
                raise ValueError()
            claims = json.loads(base64.urlsafe_b64decode(data.encode()))
            if claims['exp'] < time.time():
                raise ValueError()
            return claims
        except Exception:
            raise HTTPException(401, 'Sign in to the synthetic workspace.')

    def operator(user=Depends(auth)):
        if not user.get('operator'):
            raise HTTPException(403, 'Mock operator permission required.')
        return user

    def locate_asset(asset_id, user):
        for case in store.list_cases(user['tenant']):
            if any(a['id'] == asset_id for a in case['assets']):
                return case
        raise DomainError('Asset not found.', 404)

    def locate_action(action_id, user):
        for case in store.list_cases(user['tenant']):
            if any(a['id'] == action_id for a in case['actions']):
                return case
        raise DomainError('Action not found.', 404)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'environment': 'mock'}

    @app.post('/v1/session')
    def login(body: Login, response: Response):
        password = os.getenv('DEMO_PASSWORD', 'everkeep-demo') if body.username == 'alex' else os.getenv('SECOND_DEMO_PASSWORD')
        if not password or not secrets.compare_digest(body.password, password):
            raise HTTPException(401, 'Incorrect demo credentials.')
        claims = {'tenant': 'demo' if body.username == 'alex' else 'other_demo', 'user': 'alex_demo' if body.username == 'alex' else 'other_demo', 'operator': body.username == 'alex', 'exp': int(time.time()) + 28800}
        data = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
        token = data + '.' + hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
        response.set_cookie('estate_session', token, httponly=True, samesite='strict', secure=os.getenv('SECURE_COOKIES') == '1', max_age=28800)
        return {'user': claims['user'], 'operator': claims['operator'], 'environment': 'mock'}

    @app.delete('/v1/session')
    def logout(response: Response):
        response.delete_cookie('estate_session')
        return {'signed_out': True}

    @app.get('/v1/session')
    def whoami(user=Depends(auth)):
        return {'user': user['user'], 'operator': user['operator'], 'environment': 'mock'}

    @app.get('/v1/estate-cases')
    def list_cases(user=Depends(auth)):
        return [{'id': c['id'], 'decedent': c['decedent'], 'version': c['version']} for c in store.list_cases(user['tenant'])]

    @app.post('/v1/estate-cases')
    def new_case(body: CreateCase, user=Depends(auth)):
        return service.create(user['tenant'], user['user'])

    @app.post('/v1/demo-workspace')
    def demo(user=Depends(auth)):
        existing = store.list_cases(user['tenant'])
        if existing:
            return service.view(existing[0]['id'], user['tenant'])
        case = service.create(user['tenant'], user['user'])
        service.discover(case['id'], user['tenant'], 'fixture_seed')
        service.review_authority(case['id'], user['tenant'], 'mock_institution_reviewer')
        return service.view(case['id'], user['tenant'])

    @app.get('/v1/estate-cases/{case_id}')
    def get_case(case_id: str, user=Depends(auth)):
        return service.view(case_id, user['tenant'])

    @app.post('/v1/estate-cases/{case_id}/discover-candidates')
    def discover_candidates(case_id: str, body: Version, user=Depends(auth)):
        return service.discover(case_id, user['tenant'], user['user'], body.expected_case_version)

    @app.post('/v1/estate-cases/{case_id}/authority-reviews')
    def authority_review(case_id: str, body: Version, user=Depends(operator)):
        return service.review_authority(case_id, user['tenant'], user['user'], body.expected_case_version)

    @app.post('/v1/estate-cases/{case_id}/documents')
    def add_document(case_id: str, body: AddDocument, user=Depends(auth)):
        if body.content is None and body.kind not in ('certified_authority', 'retirement_claim_form'):
            raise DomainError('Document content is required.', 422)
        return service.add_document(case_id, user['tenant'], user['user'], body.expected_case_version, body.kind, body.name or 'Synthetic record', body.content)

    @app.get('/v1/estate-assets/{asset_id}/requirements')
    def get_requirements(asset_id: str, user=Depends(auth)):
        case = locate_asset(asset_id, user)
        return requirements(case, find(case['assets'], asset_id))

    @app.post('/v1/estate-assets/{asset_id}/packet-drafts')
    def draft_packet(asset_id: str, body: Draft, user=Depends(auth)):
        case = locate_asset(asset_id, user)
        return service.prepare(case['id'], user['tenant'], user['user'], asset_id, body.expected_case_version, body.idempotency_key)

    @app.post('/v1/actions/{action_id}/approve')
    def approve(action_id: str, body: Approve, user=Depends(auth)):
        case = locate_action(action_id, user)
        return service.approve(case['id'], user['tenant'], user['user'], action_id, body.expected_case_version, body.action_payload_hash, body.approval_challenge_id)

    @app.post('/v1/actions/{action_id}/revoke')
    def revoke(action_id: str, body: Version, user=Depends(auth)):
        case = locate_action(action_id, user)
        return service.revoke(case['id'], user['tenant'], user['user'], action_id, body.expected_case_version)

    @app.post('/v1/estate-cases/{case_id}/guide')
    def guide(case_id: str, user=Depends(auth)):
        return agent.guide(case_id, user['tenant'], user['user'])

    @app.post('/v1/estate-assets/{asset_id}/scenario')
    def scenario(asset_id: str, body: Scenario, user=Depends(operator)):
        case = locate_asset(asset_id, user)
        with store.edit(case['id'], user['tenant'], body.expected_case_version) as (con, current):
            asset = find(current['assets'], asset_id)
            if asset['status'] not in ('requirements_ready', 'authority_review'):
                raise DomainError('Choose a simulator scenario before preparing a packet.')
            asset['scenario'] = body.scenario
            store.event(con, current, 'simulator.scenario_set', user['user'], asset_id, detail=body.scenario)
        return service.view(case['id'], user['tenant'])

    @app.post('/v1/operator/clock')
    def advance(body: Advance, user=Depends(operator)):
        with store.engine.begin() as con:
            value = (datetime.fromisoformat(store.now(con)) + timedelta(seconds=body.seconds)).isoformat()
            if con.execute(select(settings).where(settings.c.id == 'clock')).first():
                con.execute(settings.update().where(settings.c.id == 'clock').values(value=value))
            else:
                con.execute(settings.insert().values(id='clock', value=value))
        return {'now': value}

    @app.post('/v1/operator/tick')
    def tick(user=Depends(operator)):
        return {'processed': worker.tick()}

    @app.get('/v1/estate-cases/{case_id}/operations')
    def operations(case_id: str, user=Depends(operator)):
        case = store.load(case_id, user['tenant'])
        with store.engine.connect() as con:
            queue = [dict(row) for row in con.execute(select(jobs).where(jobs.c.case_id == case_id, jobs.c.tenant == user['tenant'])).mappings()]
        return {'jobs': queue, 'tool_runs': case['tool_runs'], 'events': case['events'], 'pending_approvals': sum(a['status'] == 'draft' for a in case['actions']), 'unknown_outcomes': sum(a['status'] == 'unknown' for a in case['actions']), 'model_cost_minor': 0, 'planner': 'rules-only-v1'}

    @app.post('/v1/operator/jobs/{job_id}/replay')
    def replay(job_id: str, user=Depends(operator)):
        with store.engine.connect() as con:
            job = con.execute(select(jobs).where(jobs.c.id == job_id, jobs.c.tenant == user['tenant'])).mappings().first()
        if not job:
            raise DomainError('Job not found.', 404)
        with store.edit(job['case_id'], user['tenant']) as (con, case):
            action = find(case['actions'], job['action_id'])
            if action['status'] not in ('unknown', 'manual_review') or not action['approval_id']:
                raise DomainError('Only an approved uncertain request can be replayed for reconciliation.')
            asset = find(case['assets'], action['asset_id'])
            if asset['status'] == 'manual_review':
                transition(store, con, case, asset, 'submitted', user['user'], 'Reconcile original request; no new approval or request reference.')
            action['status'] = 'unknown'
            con.execute(jobs.update().where(jobs.c.id == job_id).values(status='pending', attempts=0))
        return {'queued': True, 'request_ref': action['request_ref']}

    @app.post('/v1/provider-events/estate')
    async def provider_event(request: Request):
        raw = await request.body()
        if len(raw) > 64000:
            raise HTTPException(413, 'Callback too large.')
        webhook_key = os.getenv('PROVIDER_WEBHOOK_SECRET', '').encode()
        if not webhook_key or not hmac.compare_digest(hmac.new(webhook_key, raw, hashlib.sha256).hexdigest(), request.headers.get('x-estate-signature', '')):
            raise HTTPException(401, 'Invalid provider signature.')
        body = json.loads(raw)
        # Callback is a notification only. Authoritative content is re-read from the ledger.
        result = worker.adapter.lookup_request(body.get('request_ref', ''))
        if not result:
            raise HTTPException(404, 'Unknown provider request.')
        with store.engine.connect() as con:
            pending = con.execute(select(jobs)).mappings().all()
        for job in pending:
            case = store.load(job['case_id'], job['tenant'])
            action = find(case['actions'], job['action_id'])
            if action['request_ref'] == result['request_ref'] and action['approval_id'] and action['status'] in ('dispatching', 'unknown', 'done'):
                service.apply_result(case['id'], case['tenant'], action['id'], result)
                return {'received': True}
        raise HTTPException(409, 'No matching approved provider action.')

    @app.post('/v1/mcp')
    async def mcp(request: Request, user=Depends(auth)):
        body = await request.json()
        method = body.get('method')
        if method == 'notifications/initialized':
            return Response(status_code=202)
        try:
            if method == 'initialize':
                result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'everkeep-estate-tools', 'version': '1.0.0'}}
            elif method == 'tools/list':
                result = {'tools': agent.catalog()}
            elif method == 'tools/call':
                params = body['params']
                value = agent.call(params['name'], params.get('arguments', {}), user['tenant'], user['user'])
                result = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': False}
            elif method == 'ping':
                result = {}
            else:
                return {'jsonrpc': '2.0', 'id': body.get('id'), 'error': {'code': -32601, 'message': 'Method not found'}}
        except (DomainError, ValueError, KeyError) as exc:
            result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
        return {'jsonrpc': '2.0', 'id': body.get('id'), 'result': result}

    dist = Path(__file__).resolve().parents[3] / 'frontend' / 'dist'
    if dist.exists():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')
        @app.get('/favicon.svg')
        def favicon():
            return FileResponse(dist / 'favicon.svg')
        @app.get('/')
        def index():
            return FileResponse(dist / 'index.html')
    return app


app = create_app()
