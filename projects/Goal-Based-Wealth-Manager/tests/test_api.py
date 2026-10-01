import hashlib,hmac,json

def test_session_required(client):
    client.cookies.clear();assert client.get('/v1/portfolio').status_code==401

def test_origin_guard(client):
    assert client.post('/v1/simulator/tick',json={},headers={'Origin':'https://untrusted.example'}).status_code==403

def test_customer_scope(client):
    assert client.post('/v1/wealth-cases',json={'customer_id':'someone_else'}).status_code==422
    assert client.get('/v1/wealth-cases/other/goal-status').status_code==404

def test_api_happy_path(client):
    s=client.get('/v1/portfolio').json();prefix='/v1/wealth-cases/'+s['case']['id']
    s=client.post(prefix+'/goals',json={'expected_case_version':s['version'],'target_date':'2027-09-25','monthly_minor':200000}).json()
    assert s['goal_status'][0]['required_monthly_minor']==200000
    p=client.post(prefix+'/rebalance-proposals',json={'expected_case_version':s['version'],'contribution_minor':200000}).json()
    s=client.get('/v1/portfolio').json()
    r=client.post('/v1/actions/'+p['id']+'/approve',json={'expected_case_version':s['version'],'action_payload_hash':p['payload_hash'],'approval_challenge_id':p['challenge_id']})
    assert r.status_code==200
    assert client.post('/v1/simulator/tick',json={}).json()['case']['status']=='completed'

def test_callback_signature(client):
    assert client.post('/v1/provider-events/custodian',json={'event_id':'e','request_ref':'unknown'}).status_code==401
    raw=json.dumps({'event_id':'e','request_ref':'unknown'}).encode();sig=hmac.new(b'local-demo-webhook-secret',raw,hashlib.sha256).hexdigest()
    assert client.post('/v1/provider-events/custodian',content=raw,headers={'X-Custodian-Signature':sig}).status_code==404

def test_mcp_tool_boundary(client):
    def rpc(method,params={}):return client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params}).json()['result']
    assert rpc('initialize')['protocolVersion']=='2025-06-18'
    assert len(rpc('tools/list')['tools'])==5
    result=rpc('tools/call',{'name':'calculate_goal_gap','arguments':{'goal_id':'house'}})
    assert result['structuredContent']['data']['required_monthly_minor']==100000
    assert rpc('tools/call',{'name':'get_goal_portfolio','arguments':{'customer_id':'another_customer'}})['isError']

def test_assistant_suggests_without_mutation(client):
    before=client.get('/v1/portfolio').json()
    reply=client.post('/v1/agent/messages',json={'message':'I want to buy a house one year earlier'}).json()
    assert reply['suggested_date']=='2027-09-25' and reply['suggested_monthly_minor']==200000
    after=client.get('/v1/portfolio').json();assert before['goals']==after['goals'] and before['mandate']==after['mandate']

def test_negative_amount_rejected(client):
    s=client.get('/v1/portfolio').json()
    assert client.post('/v1/wealth-cases/'+s['case']['id']+'/rebalance-proposals',json={'expected_case_version':s['version'],'contribution_minor':-1}).status_code==422

def test_invalid_target_date_is_validation_error(client):
    s=client.get('/v1/portfolio').json()
    r=client.post('/v1/wealth-cases/'+s['case']['id']+'/scenarios',json={'target_date':'invalid','monthly_minor':100000})
    assert r.status_code==422
