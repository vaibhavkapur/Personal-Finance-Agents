"""Newline-delimited JSON-RPC transport; uses the same authenticated HTTP API.
Start with: CONCIERGE_TOKEN=<session token> python -m backend.app.agent.stdio
The token is never printed. A token must be created through the synthetic API session.
"""
import json, os, sys
import httpx

def main():
    token=os.environ['CONCIERGE_TOKEN']
    with httpx.Client(base_url=os.getenv('CONCIERGE_URL','http://127.0.0.1:8093'),headers={'Authorization':'Bearer '+token},timeout=15) as client:
        for line in sys.stdin:
            message={}
            try:
                message=json.loads(line);response=client.post('/mcp',json=message)
                if 'id' not in message: continue
                response.raise_for_status(); print(json.dumps(response.json()),flush=True)
            except Exception:
                print(json.dumps({'jsonrpc':'2.0','id':message.get('id') if isinstance(message,dict) else None,'error':{'code':-32603,'message':'Transport request failed'}}),flush=True)
if __name__=='__main__':main()
