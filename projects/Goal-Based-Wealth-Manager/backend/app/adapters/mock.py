"""Credential-free custodian with an independent durable execution ledger."""
import json
import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4
from ..fixtures import seed_state

class WealthAccountAdapter(Protocol):
    def get_holdings(self, customer_id: str) -> dict: ...
    def submit_action(self, action: dict, request_ref: str, mode: str) -> dict: ...
    def find_action(self, request_ref: str) -> dict: ...
    def preview_action(self, action: dict) -> dict: ...

class MockCustodian:
    environment = 'mock'
    capabilities = {'contributions': True, 'buys': True, 'sales': False, 'withdrawals': False, 'lookup_by_request_ref': True}
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS executions(request_ref TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL)')

    def get_holdings(self, customer_id):
        if customer_id != 'cus_demo_7':
            raise ValueError('Customer not found')
        state=seed_state()
        with sqlite3.connect(self.path) as db:
            results=[json.loads(r[0]) for r in db.execute('SELECT result FROM executions ORDER BY rowid')]
        for result in results:
            if result['status'] != 'filled':
                continue
            action=result['executed']
            account=next(a for a in state['accounts'] if a['id']==action['account_id'])
            if action['type']=='contribution':
                account['cash_minor']+=action['amount_minor']
            else:
                account['cash_minor']-=action['amount_minor']
                holding=next(h for h in account['holdings'] if h['instrument_id']==action['instrument_id'])
                holding['quantity_micro']+=action['quantity_micro']
        return {'accounts':state['accounts'],'source':'mock-custodian-ledger','environment':'mock','authority':'simulated'}

    def find_action(self, request_ref):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT result FROM executions WHERE request_ref=?', (request_ref,)).fetchone()
            return json.loads(row[0]) if row else None

    def preview_action(self, action):
        return {'supported': action['type'] in ('contribution','buy') and action['account_id']=='taxable', 'environment':'mock'}

    def submit_action(self, action, request_ref, mode='normal'):
        with sqlite3.connect(self.path, timeout=20) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload,result FROM executions WHERE request_ref=?', (request_ref,)).fetchone()
            payload = json.dumps(action, sort_keys=True)
            if row:
                if row[0] != payload:
                    raise ValueError('Idempotency reference reused with a different payload')
                return json.loads(row[1])
            if not self.preview_action(action)['supported']:
                raise ValueError('Unsupported provider action')
            status = 'declined' if mode == 'partial' and action['type']=='buy' else 'filled'
            if mode in ('late_contribution','delayed_callback') and action['type']=='contribution':
                status = 'pending'
            result = {'request_ref':request_ref, 'provider_ref':'mock_'+uuid4().hex[:16], 'status':status,
                      'executed':action if status=='filled' else None, 'environment':'mock'}
            db.execute('INSERT INTO executions VALUES(?,?,?)', (request_ref,payload,json.dumps(result)))
        if mode == 'timeout_after_acceptance':
            raise TimeoutError('Accepted; response lost. Resolve with the original request reference.')
        if mode == 'malformed':
            return {'unexpected':'response'}
        return result

    def settle_pending(self):
        with sqlite3.connect(self.path) as db:
            for ref,payload,result in db.execute('SELECT * FROM executions').fetchall():
                parsed = json.loads(result)
                if parsed['status']=='pending':
                    parsed.update(status='filled', executed=json.loads(payload))
                    db.execute('UPDATE executions SET result=? WHERE request_ref=?', (json.dumps(parsed),ref))
