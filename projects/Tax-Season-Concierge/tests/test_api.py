import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from app.persistence.store import Store
from app import seed
from conftest import draft,approve


def test_http_sessions_schema_and_ownership(tmp_path):
    app=create_app(Store(str(tmp_path/'api.db')),run_worker=False)
    a,b=TestClient(app),TestClient(app)
    assert a.get('/v1/tax-cases').status_code==401
    assert a.post('/v1/session/demo',json={}).status_code==200
    b.post('/v1/session/demo',json={})
    c=a.post('/v1/tax-cases',json={}).json()
    assert a.get('/v1/tax-cases/'+c['id']).status_code==200
    assert b.get('/v1/tax-cases/'+c['id']).status_code==404
    assert b.get('/v1/tax-cases').json()==[]
    assert a.post('/v1/tax-cases',json={'tax_year':2024}).status_code==422
    assert a.post('/v1/tax-cases',json={'scenario':'live_filing'}).status_code==422
    result=a.post(f"/v1/tax-cases/{c['id']}/profile-check",json={'expected_case_version':c['version'],'profile':seed.profile()})
    assert result.status_code==200
    c=result.json()
    assert a.post(f"/v1/tax-cases/{c['id']}/sample-documents",json={'expected_case_version':1}).status_code==409
    f=seed.forms()[0];f['taxpayer_ref']='123-45-6789'
    assert a.post(f"/v1/tax-cases/{c['id']}/forms",json={'expected_case_version':c['version'],'form':f}).status_code==422
    assert 'httponly' in a.post('/v1/session/demo',json={}).headers.get('set-cookie','').lower() or a.cookies.get('tax_session')


def test_origin_body_size_and_host_guards(tmp_path):
    client=TestClient(create_app(Store(str(tmp_path/'api.db')),run_worker=False))
    assert client.post('/v1/session/demo',json={},headers={'origin':'https://evil.example'}).status_code==403
    assert client.post('/v1/session/demo',content='x'*262145,headers={'content-type':'application/json'}).status_code==413
    assert client.post('/v1/session/demo',content='hello',headers={'content-type':'text/plain'}).status_code==415
    assert client.get('/health',headers={'host':'rebind.example'}).status_code==400


def test_signed_callbacks_and_replay(tmp_path):
    store=Store(str(tmp_path/'api.db'));app=create_app(store,run_worker=False);engine=app.state.engine
    c=approve(engine,draft(engine));engine.process_one();c=engine.get('tenant_a',c['id'])
    client=TestClient(app)
    event={'id':'signed_accept','case_id':c['id'],'submission_ref':c['filing']['submission_ref'],'type':'accepted','environment':'mock','amount_minor':86200,'code':'MOCK-ACCEPTED'}
    raw=json.dumps(event).encode();timestamp=str(int(time.time()))
    signature=hmac.new(app.state.webhook_secret.encode(),timestamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    headers={'content-type':'application/json','x-tax-timestamp':timestamp,'x-tax-signature':signature}
    assert client.post('/v1/provider-events/tax',content=raw,headers={'content-type':'application/json'}).status_code==401
    response=client.post('/v1/provider-events/tax',content=raw,headers=headers)
    assert response.status_code==200 and not response.json()['duplicate']
    assert client.post('/v1/provider-events/tax',content=raw,headers=headers).json()['duplicate']
    assert client.post('/v1/provider-events/tax',content=raw+b' ',headers=headers).status_code==401
    assert client.post('/v1/provider-events/tax',content=raw,headers={**headers,'x-tax-timestamp':'0'}).status_code==401


def test_print_and_download_are_tenant_scoped(tmp_path):
    store=Store(str(tmp_path/'api.db'));app=create_app(store,run_worker=False);client=TestClient(app)
    tenant=client.post('/v1/session/demo',json={}).json()['tenant_id']
    c=draft(app.state.engine,tenant=tenant)
    r=client.get(f"/v1/tax-cases/{c['id']}/package.json")
    assert r.status_code==200 and r.json()['content_hash']==c['package']['content_hash']
    worksheet=client.get(f"/v1/tax-cases/{c['id']}/worksheet")
    assert worksheet.status_code==200 and '$862' in worksheet.text and 'Review artifact' in worksheet.text
    other=TestClient(app);other.post('/v1/session/demo',json={})
    assert other.get(f"/v1/tax-cases/{c['id']}/worksheet").status_code==404


def test_mcp_stdio_and_no_approval_tool(tmp_path):
    store=Store(str(tmp_path/'mcp.db'));engine=create_app(store,run_worker=False).state.engine
    c=draft(engine)
    messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{}}},{'jsonrpc':'2.0','id':2,'method':'tools/list'},{'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'check_supported_profile','arguments':{'case_id':c['id']}}},{'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'approve','arguments':{'case_id':c['id']}}}]
    env={**os.environ,'PYTHONPATH':'backend','DATABASE_URL':store.url,'TAX_MCP_TENANT':'tenant_a'}
    result=subprocess.run([sys.executable,'-m','app.agent.mcp'],input='\n'.join(json.dumps(x) for x in messages)+'\n',text=True,capture_output=True,env=env,check=True)
    outputs=[json.loads(x) for x in result.stdout.splitlines()]
    assert outputs[0]['result']['protocolVersion']=='2025-06-18'
    assert len(outputs[1]['result']['tools'])==5
    assert json.loads(outputs[2]['result']['content'][0]['text'])['result']['supported']
    assert outputs[3]['result']['isError']
