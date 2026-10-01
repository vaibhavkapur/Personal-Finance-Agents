"""Three executable demo journeys in a temporary, isolated database."""
import asyncio, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app.api.main import create_app
from backend.app.persistence.store import Store,uid
from backend.app.domain.registry import CUSTOMER
from backend.app.domain.engine import Service
from backend.app.workflows.worker import Worker
from backend.app.adapters.banks import MockBankAdapter

def main():
    with tempfile.TemporaryDirectory() as directory:
        store=Store('sqlite:///'+str(Path(directory)/'demo.db'));service=Service(store);worker=Worker(store)
        case=service.create(CUSTOMER,store.now(),['credit_card_demo','debit_card_demo'],['txn_demo_91','txn_demo_92'])
        def current(): return service.resolution(case['id'],CUSTOMER)
        service.verify(case['id'],CUSTOMER,1)
        def action(kind,target):
            a=service.draft(case['id'],CUSTOMER,kind,target,current()['version'],uid('demo'))
            service.approve(a['id'],CUSTOMER,current()['version'],a['payload_hash'],a['challenge_id'])
            asyncio.run(worker.run_one(a['id']));return a
        action('lock','credit_card_demo');debit=action('lock','debit_card_demo')
        assert current()['status']=='partly_contained'
        print('Demo 1: Cedar protection verified; Northstar direct authentication required.')
        asyncio.run(MockBankAdapter(store).complete_handoff(debit['id']))
        asyncio.run(worker.reconcile(debit['id'],CUSTOMER))
        assert current()['status']=='contained'
        service.confirm(case['id'],CUSTOMER,'txn_demo_92',current()['version'],'recognized','I recognize my coffee purchase.')
        assert not any(a['payload'].get('transaction_id')=='txn_demo_92' for a in current()['actions'])
        print('Demo 2: Customer recognizes the coffee descriptor. No dispute submitted.')
        service.confirm(case['id'],CUSTOMER,'txn_demo_91',current()['version'],'unauthorized','I did not authorize this electronics purchase.')
        action('report','txn_demo_91')
        with TestClient(create_app(store)) as client:
            client.post('/v1/demo/session')
            base='/v1/incidents/'+case['id']
            def simulate(kind,target):
                r=client.post(base+'/simulate',json={'type':kind,'target_id':target});assert r.status_code==200,r.text
            simulate('provisional_credit','txn_demo_91')
            assert current()['status']=='investigation_pending' and not current()['can_close']
            for ref in ['credit_card_demo','debit_card_demo']:
                action('report_lost',ref);action('replacement',ref);simulate('replacement_delivered',ref)
            simulate('resolved_customer_favor','txn_demo_91')
            for task in current()['data']['tasks']:
                r=client.post(base+'/tasks/'+task['id']+'/complete',json={'expected_case_version':current()['version'],'statement':'I reviewed recurring payments and updated the merchants.'});assert r.status_code==200,r.text
            r=client.post(base+'/close',json={'expected_case_version':current()['version']});assert r.status_code==200,r.text
            assert current()['status']=='closed'
            print('Demo 3: Provisional credit remains open; final credit, deliveries, and recovery allow closure.')
            simulate('credit_reversed','txn_demo_91')
            assert current()['status']=='investigation_pending'
            print('Recovery check: A later credit reversal reopens the closed incident with visible history.')
        print('All three end-to-end demos passed. No external providers or model calls.')
        store.engine.dispose()
if __name__=='__main__': main()
