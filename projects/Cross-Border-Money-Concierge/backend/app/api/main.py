import asyncio
import hashlib
import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from fastapi import FastAPI, Depends, Request, Response, Header
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError
from ..persistence import db as d
from ..domain.models import Intent, Draft, Approval, DocumentPacket, Versioned, ClockAdvance, ProviderEvent, Fault, StrictModel
from ..domain.lifecycle import DomainError, require
from ..domain.quotes import PROVIDERS
from ..workflows.service import Concierge, digest
from ..agent.tools import AgentTools

class Login(StrictModel):
    persona: Literal['customer', 'operator', 'other_customer'] = 'customer'
class Chat(StrictModel):
    case_id: str
    message: str = Field(min_length=1, max_length=2000)
    tool_budget: int = Field(default=5, ge=1, le=5)


def create_app(database=None, embedded_worker=True):
    service = Concierge(database or d.Database())
    agent = AgentTools(service)
    @asynccontextmanager
    async def lifespan(app):
        task = None
        if embedded_worker:
            async def worker():
                while True:
                    await asyncio.to_thread(service.drain, 50)
                    await asyncio.sleep(1)
            task = asyncio.create_task(worker())
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title='Passage · Cross-Border Money Concierge', version='1.0.0', lifespan=lifespan)
    app.state.service = service
    app.state.agent = agent

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({'detail': exc.message}, status_code=exc.status)

    @app.middleware('http')
    async def origin_guard(request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            allowed = {str(request.base_url).rstrip('/'), 'http://127.0.0.1:5188', 'http://localhost:5188', 'http://127.0.0.1:8088', 'http://localhost:8088'}
            if origin and origin not in allowed:
                return JSONResponse({'detail': 'Untrusted request origin'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/v1') else 'no-cache'
        return response

    def user(request: Request):
        bearer = request.headers.get('authorization', '')
        token = bearer[7:] if bearer.startswith('Bearer ') else request.cookies.get('passage_session')
        require(bool(token), 'Choose a demo session to continue', 401)
        with service.db.tx() as conn:
            session = d.get(conn, d.sessions, hashlib.sha256(token.encode()).hexdigest())
        require(session and datetime.fromisoformat(session['expires_at']) > datetime.now(timezone.utc), 'Session expired', 401)
        return session

    def operator(current=Depends(user)):
        require(current['role'] == 'operator', 'Operator session required', 403)
        return current

    @app.get('/v1/health')
    def health():
        return {'status': 'ok', 'environment': 'mock', 'clock': service.clock().isoformat()}

    @app.post('/v1/demo/session')
    def login(body: Login, response: Response):
        # Explicit local demo identities, never a production authentication mechanism.
        require(os.getenv('DEMO_MODE', '1') == '1', 'Demo sessions disabled', 404)
        token = secrets.token_urlsafe(32)
        session = {'id': hashlib.sha256(token.encode()).hexdigest(), 'customer_id': 'cus_other' if body.persona == 'other_customer' else 'cus_demo_8',
                   'role': 'operator' if body.persona == 'operator' else 'customer', 'name': 'Alex Morgan',
                   'expires_at': (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()}
        with service.db.tx() as conn:
            d.put(conn, d.sessions, session)
        if body.persona == 'customer':
            response.set_cookie('passage_session', token, httponly=True, samesite='strict', max_age=43200)
        return {'access_token': token, 'user': session, 'environment': 'mock'}

    @app.get('/v1/bootstrap')
    def bootstrap(current=Depends(user)):
        with service.db.tx() as conn:
            return {'user': current, 'recipients': d.rows(conn, d.beneficiaries, d.beneficiaries.c.customer_id == current['customer_id']),
                    'documents': d.rows(conn, d.documents, d.documents.c.customer_id == current['customer_id']),
                    'cases': sorted(d.rows(conn, d.cases, d.cases.c.customer_id == current['customer_id']), key=lambda x: x['created_at'], reverse=True),
                    'providers': list(PROVIDERS.values()), 'clock': service.now(conn).isoformat(), 'environment': 'mock'}

    @app.post('/v1/remittance-cases', status_code=201)
    def create(body: Intent, current=Depends(user)):
        return service.create_case(current, body)

    @app.get('/v1/remittance-cases/{case_id}')
    def get_case(case_id: str, current=Depends(user)):
        return service.snapshot(current, case_id)

    @app.post('/v1/remittance-cases/{case_id}/quotes')
    def quotes(case_id: str, current=Depends(user)):
        return service.get_quotes(current, case_id)

    @app.get('/v1/remittance-cases/{case_id}/comparison')
    def comparison(case_id: str, current=Depends(user)):
        return service.snapshot(current, case_id)['comparison']

    @app.post('/v1/remittance-cases/{case_id}/transfer-drafts')
    def draft(case_id: str, body: Draft, idempotency_key: Optional[str] = Header(default=None), current=Depends(user)):
        return service.prepare_transfer(current, case_id, body, idempotency_key)

    @app.post('/v1/remittance-transfers/{transfer_id}/document-packets')
    def documents(transfer_id: str, body: DocumentPacket, idempotency_key: Optional[str] = Header(default=None), current=Depends(user)):
        return service.prepare_followup(current, transfer_id, 'share_documents', body, idempotency_key)

    @app.post('/v1/remittance-transfers/{transfer_id}/cancellation-drafts')
    def cancellation(transfer_id: str, body: Versioned, idempotency_key: Optional[str] = Header(default=None), current=Depends(user)):
        return service.prepare_followup(current, transfer_id, 'cancel', body, idempotency_key)

    @app.post('/v1/actions/{action_id}/approve')
    def approve(action_id: str, body: Approval, current=Depends(user)):
        return service.approve(current, action_id, body)

    @app.post('/v1/actions/{action_id}/revoke')
    def revoke(action_id: str, current=Depends(user)):
        return service.revoke(current, action_id)

    @app.get('/v1/remittance-transfers/{transfer_id}/receipt')
    def receipt(transfer_id: str, current=Depends(user)):
        with service.db.tx() as conn:
            transfer = service.owned(conn, d.transfers, transfer_id, current)
        result = service.snapshot(current, transfer['case_id'])['receipt']
        require(result is not None, 'No verified delivery receipt is available', 404)
        return JSONResponse(result, headers={'Content-Disposition': 'attachment; filename="passage-receipt.json"'})

    @app.post('/v1/demo/advance')
    def advance(body: ClockAdvance, current=Depends(user)):
        return service.advance(body.minutes)

    @app.post('/v1/operator/transfers/{transfer_id}/scenario')
    def fault(transfer_id: str, body: Fault, current=Depends(operator)):
        with service.db.tx() as conn:
            transfer = service.owned(conn, d.transfers, transfer_id, current)
            require(transfer['status'] == 'prepared', 'Set a scenario before approving initiation')
            transfer['scenario'] = body.scenario
            d.put(conn, d.transfers, transfer)
        return transfer

    @app.get('/v1/operator/overview')
    def overview(current=Depends(operator)):
        with service.db.tx() as conn:
            cases = d.rows(conn, d.cases, d.cases.c.customer_id == current['customer_id'])
            ids = [v['id'] for v in cases]
            jobs = d.rows(conn, d.jobs, d.jobs.c.case_id.in_(ids))
            actions = d.rows(conn, d.actions, d.actions.c.customer_id == current['customer_id'])
            pending = [j for j in jobs if j['status'] in {'pending', 'running'} and j['kind'] != 'notify']
            return {'cases': cases, 'actions': actions, 'jobs': jobs, 'inbox': [x for x in d.rows(conn, d.inbox) if x['case_id'] in ids],
                    'tool_runs': d.rows(conn, d.tool_runs, d.tool_runs.c.customer_id == current['customer_id']), 'clock': service.now(conn).isoformat(),
                    'metrics': {'pending_jobs': len(pending), 'failed_jobs': sum(j['status'] == 'failed' for j in jobs),
                                'waiting_customer': sum(c['status'] in {'awaiting_approval', 'information_required'} for c in cases),
                                'waiting_provider': sum(c['status'] in {'processing', 'funding_pending', 'outcome_unknown', 'payout_pending', 'delayed'} for c in cases),
                                'reconciled': sum(c['status'] == 'reconciled' for c in cases), 'approval_abandonment': sum(a['status'] == 'draft' and a['expires_at'] < service.now(conn).isoformat() for a in actions),
                                'oldest_queue_age_minutes': max([max(0, int((service.now(conn) - datetime.fromisoformat(j['due_at'])).total_seconds() / 60)) for j in pending] or [0])},
                    'environment': 'mock'}

    @app.post('/v1/operator/inbox/{inbox_id}/replay')
    def replay(inbox_id: str, current=Depends(operator)):
        with service.db.tx() as conn:
            entry = d.get(conn, d.inbox, inbox_id)
            require(entry is not None, 'Event not found', 404)
            service.owned(conn, d.cases, entry['case_id'], current)
        return service.replay(inbox_id)

    @app.post('/v1/provider-events/remittance')
    async def provider_event(request: Request, x_provider_signature: str = Header(default='')):
        raw = await request.body()
        secret = os.getenv('MOCK_WEBHOOK_SECRET', 'local-simulator-only').encode()
        expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        require(hmac.compare_digest(expected, x_provider_signature), 'Invalid provider signature', 401)
        try:
            body = ProviderEvent.model_validate_json(raw)
        except ValidationError as exc:
            return JSONResponse({'detail': json.loads(exc.json())}, status_code=422)
        return service.ingest(body.model_dump(mode='json'))

    @app.post('/v1/concierge/chat')
    def chat(body: Chat, current=Depends(user)):
        return agent.respond(current, body.case_id, body.message, body.tool_budget)

    @app.post('/mcp')
    async def mcp(request: Request, current=Depends(user)):
        body = await request.json()
        identifier, method = body.get('id'), body.get('method')
        if identifier is None and method == 'notifications/initialized':
            return Response(status_code=202)
        try:
            require(body.get('jsonrpc') == '2.0', 'JSON-RPC 2.0 required', 422)
            if method == 'initialize':
                result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'passage', 'version': '1.0.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': agent.catalog()}
            elif method == 'tools/call':
                params = body.get('params', {})
                try:
                    result = {'content': [{'type': 'text', 'text': json.dumps(agent.call(current, params['name'], params.get('arguments', {})))}], 'isError': False}
                except (DomainError, ValidationError, KeyError) as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
            else:
                return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32601, 'message': 'Method not found'}}
            return {'jsonrpc': '2.0', 'id': identifier, 'result': result}
        except DomainError as exc:
            return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32600, 'message': exc.message}}

    dist = Path(__file__).resolve().parents[3] / 'frontend' / 'dist'
    if dist.exists():
        app.mount('/', StaticFiles(directory=str(dist), html=True), name='frontend')
    return app

app = create_app(embedded_worker=os.getenv('EMBEDDED_WORKER', '1') == '1')
