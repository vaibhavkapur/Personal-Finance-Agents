"""Run labeled synthetic cases against concierge and direct rules-only baseline."""
import argparse
import copy
import hashlib
import json
import tempfile
import time
from pathlib import Path
from app.api.schemas import CreateCase,ApprovalRequest,Form
from app.domain.tax import load_rules
from app.persistence.store import Store
from app.workflows.engine import Engine
from app import seed

ROOT=Path(__file__).resolve().parents[1]

def evaluate_case(item, path, orchestrated):
    engine=Engine(Store(str(path)))
    tenant='evaluation'
    c=engine.create(tenant,CreateCase(scenario=item.get('scenario','two_jobs')))
    profile=seed.profile()
    for key,value in item.get('profile',{}).items():
        if key=='answers':profile['answers'].update(value)
        else:profile[key]=value
    c=engine.profile_check(tenant,c['id'],c['version'],profile)
    if not c['eligibility']['supported']:
        return {'outcome':'out_of_scope' if c['eligibility']['reasons'] else 'needs_information','questions':len(c['eligibility']['missing']),'tool_calls':0}
    forms=seed.forms(item.get('scenario','two_jobs'))
    if item.get('missing'):forms.pop(item['missing']-1)
    for index,patch in item.get('forms',{}).items():forms[int(index)].update(patch)
    for form in forms:c=engine.add_form(tenant,c['id'],c['version'],Form(**form).model_dump())
    if item.get('duplicate'):c=engine.add_form(tenant,c['id'],c['version'],forms[0])
    if item.get('correction'):
        correction=Form(form_type='W-2c',issuer_ref='fixture_northstar',issuer_name='Northstar Studio',supersedes_form_id=c['forms'][0]['id'],federal_withholding_minor=530050,confirmed=True)
        c=engine.add_form(tenant,c['id'],c['version'],correction.model_dump())
    c=engine.reconcile(tenant,c['id'],c['version'],item.get('complete',True))
    if not c['reconciliation']['ready']:
        return {'outcome':'out_of_scope' if c['reconciliation']['exclusions'] else 'needs_information','questions':len(c['reconciliation']['issues']),'tool_calls':0}
    if orchestrated:c=engine.assist(tenant,c['id'],c['version'])
    else:
        c=engine.calculate(tenant,c['id'],c['version'],load_rules()[0]['id'],c['reconciliation']['facts_hash'])
        c=engine.prepare(tenant,c['id'],c['version'])
    if item.get('submit'):
        a=next(a for a in c['actions'] if a['id']==c['package']['action_id'])
        c=engine.approve(tenant,a['id'],ApprovalRequest(expected_case_version=c['version'],action_payload_hash=a['payload_hash'],approval_challenge_id=a['challenge_id']))
        engine.process_one()
        c=engine.get(tenant,c['id'])
        if item.get('recover'):
            with engine.store.transaction() as db:db.execute('UPDATE jobs SET lease_until=0')
            engine=Engine(Store(str(path)));engine.process_one();c=engine.get(tenant,c['id'])
        if item.get('days'):c=engine.advance(tenant,c['id'],c['version'],item['days'])
    return {'outcome':c['state'],'tax_minor':c['calculation']['tax_minor'],'refund_minor':c['calculation']['refund_minor'],'amount_due_minor':c['calculation']['amount_due_minor'],'questions':0,'tool_calls':sum(len(r['tools']) for r in c['tool_runs'])}


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',default=str(ROOT/'docs/evaluation.json'));args=p.parse_args()
    raw=(ROOT/'fixtures/evaluation-cases.json').read_bytes();fixtures=json.loads(raw)
    results=[]
    with tempfile.TemporaryDirectory() as folder:
        for item in fixtures['cases']:
            started=time.perf_counter()
            actual=evaluate_case(item,Path(folder)/(item['id']+'-agent.db'),True)
            baseline=evaluate_case(item,Path(folder)/(item['id']+'-baseline.db'),False)
            expected=item['expected']
            passed=all(actual.get(k)==v for k,v in expected.items()) and all(actual.get(k)==baseline.get(k) for k in ['outcome','tax_minor','refund_minor','amount_due_minor','questions'])
            results.append({'id':item['id'],'split':item['split'],'passed':passed,'expected':expected,'actual':actual,'baseline':baseline,'latency_ms':round((time.perf_counter()-started)*1000,2)})
    report={'fixture_version':fixtures['version'],'fixture_sha256':hashlib.sha256(raw).hexdigest(),'rule_pack_id':load_rules()[0]['id'],'scope':'Synthetic regression evaluation; not customer outcomes, model evaluation or independent tax review','orchestrator':'rules-only-v1','model_cost_usd':0,'total':len(results),'passed':sum(r['passed'] for r in results),'held_out_total':sum(r['split']=='held_out' for r in results),'results':results}
    Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
    print(f"{report['passed']}/{report['total']} fixtures match expected outcomes and direct rules-only baseline; {report['held_out_total']} held out.")
    if report['passed']!=report['total']:raise SystemExit(1)

if __name__=='__main__':main()
