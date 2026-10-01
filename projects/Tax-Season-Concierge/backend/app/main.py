import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends, Request, Response, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from app.api.schemas import CreateCase, ProfileRequest, FormRequest, ReconcileRequest, CalculationRequest, ApprovalRequest, ClockRequest, Versioned, ProviderEvent
from app.domain.scope import QUESTIONS
from app.domain.tax import load_rules
from app.persistence.store import Store, Conflict
from app.workflows.engine import Engine
from app import seed

ROOT = Path(__file__).resolve().parents[2]


def create_app(store=None, run_worker=None):
    store = store or Store()
    engine = Engine(store)
    enable_worker = run_worker if run_worker is not None else os.getenv('RUN_WORKER', '1') == '1'
    async def worker_loop():
        while True:
            try:
                await asyncio.to_thread(engine.process_one)
            except Exception:
                # Persisted job lease expires after a process crash or failed attempt.
                import logging
                logging.getLogger(__name__).error('Worker tick failed; durable job retained for recovery')
            await asyncio.sleep(1)
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker_loop()) if enable_worker else None
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    app = FastAPI(title='Tax-Season Concierge', version='0.1.0', lifespan=lifespan)
    app.state.engine, app.state.store = engine, store
    app.state.webhook_secret = os.getenv('WEBHOOK_SECRET', secrets.token_hex(32))

    @app.middleware('http')
    async def guards(request, call_next):
        host = request.headers.get('host', '').split(':')[0]
        allowed_hosts = {'localhost', '127.0.0.1', 'testserver', 'api'} | set(os.getenv('ALLOWED_HOSTS', '').split(','))
        if host not in allowed_hosts:
            return JSONResponse({'detail': 'Unknown host'}, status_code=400)
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            origin = request.headers.get('origin')
            allowed = {'http://127.0.0.1:8096', 'http://localhost:8096', 'http://127.0.0.1:5178', 'http://localhost:5178', str(request.base_url).rstrip('/')}
            if origin and origin not in allowed:
                return JSONResponse({'detail': 'Cross-origin writes are not allowed'}, status_code=403)
            body = await request.body()
            if len(body) > 262144:
                return JSONResponse({'detail': 'Synthetic document payload must be below 256 KiB'}, status_code=413)
            if body and not request.headers.get('content-type', '').startswith('application/json'):
                return JSONResponse({'detail': 'Expected application/json'}, status_code=415)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if request.url.path.startswith('/v1'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)
    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=422)
    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': 'Resource not found'}, status_code=404)

    def tenant(request: Request):
        token = request.cookies.get('tax_session')
        if not token:
            raise HTTPException(401, 'Open a demo session first')
        with store.transaction() as db:
            row = db.execute('SELECT tenant_id,expires_at FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['expires_at'] < time.time():
            raise HTTPException(401, 'Demo session expired')
        return row['tenant_id']

    @app.get('/health')
    def health():
        return {'status': 'ok', 'environment': 'mock', 'rules': load_rules()[0]['id']}

    @app.post('/v1/session/demo')
    def session(request: Request, response: Response):
        try:
            existing = tenant(request)
            return {'tenant_id': existing, 'environment': 'mock'}
        except HTTPException:
            pass
        token, tenant_id = secrets.token_urlsafe(32), 'demo_' + secrets.token_hex(12)
        with store.transaction() as db:
            db.execute('INSERT INTO sessions(token_hash,tenant_id,expires_at) VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), tenant_id, time.time()+604800))
        response.set_cookie('tax_session', token, httponly=True, samesite='strict', secure=os.getenv('COOKIE_SECURE') == '1', max_age=604800)
        return {'tenant_id': tenant_id, 'environment': 'mock'}

    @app.get('/v1/config')
    def config(t=Depends(tenant)):
        return {'questions': [{'key': key, 'label': label} for key, (_, label) in QUESTIONS.items()], 'sample_profile': seed.profile(), 'sample_forms': seed.forms(), 'rules': load_rules()[0], 'provider': {'name': 'Mock Federal Filing Service', 'environment': 'mock', 'capabilities': engine.provider.capabilities}}

    @app.get('/v1/tax-cases')
    def list_cases(t=Depends(tenant)):
        return engine.list(t)
    @app.post('/v1/tax-cases')
    def create(body: CreateCase, t=Depends(tenant)):
        return engine.create(t, body)
    @app.get('/v1/tax-cases/{case_id}')
    def get(case_id: str, t=Depends(tenant)):
        return engine.get(t, case_id)
    @app.post('/v1/tax-cases/{case_id}/profile-check')
    def profile(case_id: str, body: ProfileRequest, t=Depends(tenant)):
        return engine.profile_check(t, case_id, body.expected_case_version, body.profile.model_dump())
    @app.post('/v1/tax-cases/{case_id}/forms')
    def forms(case_id: str, body: FormRequest, t=Depends(tenant)):
        return engine.add_form(t, case_id, body.expected_case_version, body.form.model_dump())
    @app.post('/v1/tax-cases/{case_id}/forms/{form_id}/confirm')
    def confirm_form(case_id: str, form_id: str, body: Versioned, t=Depends(tenant)):
        return engine.confirm_form(t, case_id, form_id, body.expected_case_version)
    @app.post('/v1/tax-cases/{case_id}/sample-documents')
    def samples(case_id: str, body: Versioned, t=Depends(tenant)):
        return engine.seed_forms(t, case_id, body.expected_case_version)
    @app.post('/v1/tax-cases/{case_id}/reconcile')
    def recon(case_id: str, body: ReconcileRequest, t=Depends(tenant)):
        return engine.reconcile(t, case_id, body.expected_case_version, body.completeness_confirmed)
    @app.post('/v1/tax-cases/{case_id}/calculate')
    def calc(case_id: str, body: CalculationRequest, t=Depends(tenant)):
        return engine.calculate(t, case_id, body.expected_case_version, body.rule_pack_id, body.facts_hash)
    @app.post('/v1/tax-cases/{case_id}/return-drafts')
    def draft(case_id: str, body: Versioned, t=Depends(tenant)):
        return engine.prepare(t, case_id, body.expected_case_version)
    @app.post('/v1/tax-cases/{case_id}/assist')
    def assist(case_id: str, body: Versioned, t=Depends(tenant)):
        return engine.assist(t, case_id, body.expected_case_version)
    @app.post('/v1/actions/{action_id}/approve')
    def approve(action_id: str, body: ApprovalRequest, t=Depends(tenant)):
        return engine.approve(t, action_id, body)
    @app.get('/v1/tax-cases/{case_id}/filing-status')
    def status(case_id: str, t=Depends(tenant)):
        case = engine.get(t, case_id)
        return {'source': 'Mock Federal Filing Service', 'environment': 'mock', 'authority': 'simulated', 'retrieved_at': case['updated_at'], **case['filing']}
    @app.post('/v1/tax-cases/{case_id}/clock')
    def clock(case_id: str, body: ClockRequest, t=Depends(tenant)):
        return engine.advance(t, case_id, body.expected_case_version, body.days)
    @app.post('/v1/tax-cases/{case_id}/resolve-rejection')
    def resolve(case_id: str, body: Versioned, t=Depends(tenant)):
        return engine.resolve_rejection(t, case_id, body.expected_case_version)
    @app.post('/v1/tax-cases/{case_id}/replay')
    def replay(case_id: str, body: Versioned, t=Depends(tenant)):
        return engine.replay(t, case_id, body.expected_case_version)
    @app.get('/v1/operator/metrics')
    def metrics(t=Depends(tenant)):
        return engine.metrics(t)

    @app.post('/v1/provider-events/tax')
    async def callback(request: Request):
        body = await request.body()
        signed_at = request.headers.get('x-tax-timestamp', '')
        signature = request.headers.get('x-tax-signature', '')
        try:
            fresh = abs(time.time() - int(signed_at)) <= 300
        except ValueError:
            fresh = False
        expected = hmac.new(app.state.webhook_secret.encode(), signed_at.encode()+b'.'+body, hashlib.sha256).hexdigest()
        if not fresh or not hmac.compare_digest(signature, expected):
            raise HTTPException(401, 'Invalid callback signature or timestamp')
        try:
            event = ProviderEvent.model_validate_json(body)
        except ValueError:
            raise HTTPException(422, 'Invalid provider event')
        return engine.apply_event(event.model_dump())

    @app.get('/v1/tax-cases/{case_id}/package.json')
    def package_json(case_id: str, t=Depends(tenant)):
        case = engine.get(t, case_id)
        if not case['package']:
            raise HTTPException(409, 'Prepare a review package first')
        action = next(a for a in case['actions'] if a['id'] == case['package']['action_id'])
        return JSONResponse({'content_hash': action['payload_hash'], 'package': action['payload']}, headers={'Content-Disposition': 'attachment; filename="2025-federal-draft.json"'})

    @app.get('/v1/tax-cases/{case_id}/worksheet')
    def worksheet(case_id: str, t=Depends(tenant)):
        case = engine.get(t, case_id)
        if not case['calculation']:
            raise HTTPException(409, 'Calculate the worksheet first')
        calc = case['calculation']
        rows = ''.join(f"<tr><td>{html.escape(row['line'])}</td><td>{html.escape(row['label'])}<small>{html.escape(row['rule_ref'])}</small></td><td>${row['amount_minor']/100:,.0f}</td><td>{html.escape(', '.join(row['source_form_ids']) or 'Confirmed scope / pinned rule')}</td></tr>" for row in calc['worksheet'])
        sources = ''.join(f"<li>{html.escape(f['id'])} — {html.escape(f['issuer_name'])}, {html.escape(f['form_type'])}, {html.escape(f['content_hash'])}</li>" for f in case['forms'])
        return HTMLResponse(f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>2025 federal draft worksheet</title><style>body{{font:15px system-ui;margin:40px;color:#102631}}h1{{font-size:30px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;border-bottom:1px solid #ddd;text-align:left}}small{{display:block;color:#52646b;margin-top:4px}}li{{overflow-wrap:anywhere;margin:10px 0}}@media print{{body{{margin:0}}tr{{break-inside:avoid}}}}</style><h1>2025 federal draft worksheet</h1><p>Alex Morgan · Synthetic identity · Single · US federal</p><p><strong>Review artifact — not an IRS e-file payload.</strong></p><p>Rule pack: {html.escape(calc['rule_pack_id'])}<br>Facts: {html.escape(calc['facts_hash'])}</p><p>{html.escape(calc['rounding'])}</p><table><thead><tr><th>1040 line</th><th>Description and rule</th><th>USD</th><th>Source forms</th></tr></thead><tbody>{rows}</tbody></table><h2>Original evidence</h2><ul>{sources}</ul><p>{html.escape(calc['notice'])}</p></html>''')

    dist = ROOT / 'frontend/dist'
    if dist.exists():
        app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
    return app

app = create_app()
