"""MCP stdio, protocol revision 2025-06-18. No model credentials required."""
import json
import os
import sys
from app.agent.tools import TOOLS, ToolArguments, call_tool
from app.persistence.store import Store
from app.workflows.engine import Engine


def main():
    tenant = os.environ.get('TAX_MCP_TENANT')
    if not tenant:
        print('TAX_MCP_TENANT must be set by the trusted local operator.', file=sys.stderr)
        raise SystemExit(2)
    engine = Engine(Store())
    initialized = False
    for raw in sys.stdin:
        request = {}
        try:
            request = json.loads(raw)
            if 'id' not in request:
                continue
            method = request['method']
            if method == 'initialize':
                initialized = True
                result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': {'name': 'tax-season-concierge', 'version': '0.1.0'}}
            elif method == 'ping':
                result = {}
            elif not initialized:
                raise ValueError('Initialize the MCP session first')
            elif method == 'tools/list':
                result = {'tools': [{'name': name, 'description': desc, 'inputSchema': ToolArguments.model_json_schema()} for name, desc in TOOLS.items()]}
            elif method == 'tools/call':
                try:
                    params = request['params']
                    value = call_tool(engine, tenant, params['name'], ToolArguments(**params.get('arguments', {})))
                    result = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'isError': False}
                except Exception as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
        except Exception:
            print(json.dumps({'jsonrpc': '2.0', 'id': request.get('id'), 'error': {'code': -32600, 'message': 'Invalid JSON-RPC request'}}), flush=True)

if __name__ == '__main__':
    main()
