import json
from pathlib import Path
from .demo import run_demo

if __name__=='__main__':
    fixtures=json.loads(Path('fixtures/evaluation-cases.json').read_text())
    outcomes=[]
    for f in fixtures:
        r=run_demo(f['provider'],None if f['scenario']=='refusal' else f['scenario'],f['amount'],f['mode'],f['scenario']=='refusal')
        passed=r['status']==f['expected_state'] and r['provider_transfer_count']==f['expected_provider_writes']
        evidence_ok=r['status']!='reconciled' or bool(r['receipt'] and r['receipt']['provider_reference'] and r['receipt']['reconciliation_status']=='matched')
        outcomes.append({'id':f['id'],'split':f['split'],'passed':passed and evidence_ok,'actual_state':r['status'],'provider_writes':r['provider_transfer_count'],'verified_completion':evidence_ok,'tool_calls':r['tool_calls']})
    report={'fixture_version':'1.0','mode':'rules-only baseline','model_comparison':'No external language model configured; no comparative model performance is claimed.',
            'cases':len(outcomes),'passed':sum(x['passed'] for x in outcomes),'development_cases':20,'held_out_cases':10,'model_cost_usd':0,'results':outcomes}
    Path('docs/evaluation-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='results'},indent=2))
    if report['passed']!=report['cases']:raise SystemExit(1)
