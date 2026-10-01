"""Repeatable labeled state/evidence evaluation; no external model is configured."""
import asyncio,json,tempfile
from pathlib import Path
from fastapi import HTTPException
from backend.app.persistence.store import *
from backend.app.domain.engine import Service,PROTECTED
from backend.app.domain.registry import CUSTOMER
from backend.app.workflows.worker import Worker
from backend.app.workflows.events import EventProcessor
from backend.app.adapters.banks import MockBankAdapter
from backend.app.agent.tools import Tools,Orchestrator

def evaluate_fixture(item,directory):
    store=Store('sqlite:///'+str(Path(directory)/(item['id']+'.db')));service=Service(store)
    case=service.create(CUSTOMER,store.now(),['credit_card_demo','debit_card_demo'],['txn_demo_91','txn_demo_92']);worker=Worker(store)
    def current():return service.resolution(case['id'],CUSTOMER)
    setup=item['setup']
    if setup!='reported':service.verify(case['id'],CUSTOMER,1)
    def execute(kind,target):
        a=service.draft(case['id'],CUSTOMER,kind,target,current()['version'],uid('eval'))
        service.approve(a['id'],CUSTOMER,current()['version'],a['payload_hash'],a['challenge_id'])
        asyncio.run(worker.run_one(a['id']));return a
    if setup in {'unknown_credit','malformed','declined'}:
        with store.tx() as c:
            data=service.get(c,case['id'],CUSTOMER);data['data']['scenario']={'unknown_credit':'timeout_after_acceptance','malformed':'malformed','declined':'declined'}[setup]
            service.save(c,data,'simulator.scenario_changed',CUSTOMER)
        execute('lock','credit_card_demo')
    elif setup not in {'reported','verified'}:
        execute('lock','credit_card_demo')
        if setup!='credit_locked':
            debit=execute('lock','debit_card_demo')
            if setup!='partly_contained':
                asyncio.run(MockBankAdapter(store).complete_handoff(debit['id']))
                asyncio.run(worker.reconcile(debit['id'],CUSTOMER))
        if setup in {'reported_credit','provisional','information_required','reversal'}:
            service.confirm(case['id'],CUSTOMER,'txn_demo_91',current()['version'],'unauthorized','I did not authorize this purchase.')
            execute('report','txn_demo_91')
            def event(kind):
                t=current()['data']['transactions'][0]
                EventProcessor(store).apply({'event_id':uid('evt'),'case_id':case['id'],'customer_id':CUSTOMER,'provider_id':'cedar','instrument_id':'credit_card_demo','transaction_id':'txn_demo_91','provider_reference':t['provider_case_ref'],'type':kind,'revision':t.get('provider_revision',0)+1,'occurred_at':store.now(),'environment':'mock'})
            if setup in {'provisional','reversal'}:event('provisional_credit')
            if setup=='reversal':event('credit_reversed')
            if setup=='information_required':event('information_required')
    before=current()
    with store.tx() as c: before_writes=len(rows(c,provider_actions))
    expected_protected=sum(i['protection_status'] in PROTECTED for i in before['data']['instruments'])
    error=None;response=None
    try:response=Orchestrator(Tools(store,CUSTOMER)).respond(item['message'],case['id'])
    except HTTPException as exc:error=exc.status_code
    after=current()
    with store.tx() as c: after_writes=len(rows(c,provider_actions))
    passed=(error==item.get('expected_error') and before['status']==item['expected_state'] and after['status']==item['expected_state'] and before_writes==after_writes)
    if response:
        passed=passed and response['tool_calls']<=4
        if response['tool_calls']:passed=passed and f'{expected_protected} of 2 cards have verified protection.' in response['message']
        if setup in {'unknown_credit','malformed'} and response['tool_calls']:passed=passed and 'uncertain' in response['message']
    store.engine.dispose()
    return {'id':item['id'],'split':item['split'],'category':item['category'],'passed':passed,'baseline_state':before['status'],'agent_state':after['status'],'unapproved_provider_writes':after_writes-before_writes,'tool_calls':response['tool_calls'] if response else 0,'cost_usd':0,'expected_error':item.get('expected_error'),'actual_error':error}

def main():
    root=Path(__file__).resolve().parents[1]
    corpus=json.loads((root/'fixtures/evaluations.json').read_text())
    with tempfile.TemporaryDirectory() as directory: results=[evaluate_fixture(item,directory) for item in corpus['cases']]
    report={'fixture_version':corpus['version'],'mode':'rules-only-v1','comparison':'Canonical domain-state baseline; no external model benchmark','total':len(results),'passed':sum(r['passed'] for r in results),'held_out':sum(r['split']=='held-out' for r in results),'cost_usd':0,'results':results}
    (root/'docs/evaluation-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f"Evaluation: {report['passed']}/{report['total']} labeled cases pass; {report['held_out']} held out; $0 external-model cost.")
    if report['passed']!=report['total']:raise SystemExit(1)
if __name__=='__main__':main()
