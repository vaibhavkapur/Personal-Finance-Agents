"""Thirty labeled state/evidence evaluations; six held out from the development set."""
import json
import tempfile
from pathlib import Path
from backend.app.agent.tools import assist
from backend.app.domain.engine import DomainError
from backend.app.workflows.service import PaydayService, TENANT, CUSTOMER
from backend.app.worker import run_once

ROOT=Path(__file__).resolve().parents[1]


def run():
    cases=json.loads((ROOT/'fixtures/agent-evaluations.json').read_text())
    results=[]
    for fixture in cases:
        with tempfile.TemporaryDirectory() as d:
            s=PaydayService(d);s.seed(scenario='late' if fixture['scenario']=='late' else 'supported')
            c=s.create_case(TENANT,CUSTOMER,300000)
            if fixture['scenario']=='missing':
                s.change_inputs(TENANT,CUSTOMER,lambda p:p['receipts'][0].update(confirmed=False))
            if fixture['scenario']=='conflict':
                s.change_inputs(TENANT,CUSTOMER,lambda p:p['bills'][0].update(confirmed=False))
            if fixture['scenario']=='unknown':
                v=s.calculate_case(TENANT,c['id'])['proposal'];a=s.draft(TENANT,c['id'],v['id'],'eval','timeout')
                s.approve(TENANT,a['id'],{k:a[k] for k in ('expected_case_version','action_payload_hash','approval_challenge_id')},'fixture')
                run_once(s)
            with s.store.transaction() as db:approvals_before=len(s.store.all(db,'approvals',TENANT))
            actual=assist(s,TENANT,c['id'],fixture['message'])
            profile=s.refresh(TENANT,CUSTOMER)
            with s.store.transaction() as db:
                proposals=s.store.all(db,'payday_proposals',TENANT)
                approvals_after=len(s.store.all(db,'approvals',TENANT))
            supported=proposals[-1]['feasible_minor'] if proposals else None
            # Direct deterministic workflow serves as the no-agent baseline.
            baseline=None
            if fixture['scenario'] in ('ordinary','late','missing','conflict'):
                try:baseline=s.calculate_case(TENANT,c['id'])['proposal']['feasible_minor']
                except DomainError:baseline=None
            checks={
                'expected_intent':actual['intent']==fixture['expected_intent'],
                'no_self_approval':approvals_before==approvals_after,
                'tool_budget':len(actual['tools'])<=4,
                'cash_conserved':sum(profile['buckets'].values())==profile['bank_snapshot']['available_minor'],
                'expected_supported_payout':supported==fixture['expected_feasible_minor'],
                'baseline_agrees':baseline==fixture['expected_feasible_minor'] if fixture['scenario']!='unknown' else True,
                'unknown_keeps_reservation':profile['buckets']['reserved']==300000 if fixture['scenario']=='unknown' else True,
                'unexpected_question':actual['intent'] not in ('missing_information','needs_review') if fixture['scenario'] in ('ordinary','late') else True,
            }
            results.append({'id':fixture['id'],'split':fixture['split'],'scenario':fixture['scenario'],'passed':all(checks.values()),'checks':checks,'tool_calls':len(actual['tools']),'model_cost_usd':'0.00'})
    report={'fixture_version':'2026-09-26.1','engine':'rules-1','model_comparison':'Not run; no LLM provider is configured. Baseline is the direct deterministic workflow.','total':len(results),'passed':sum(r['passed'] for r in results),'held_out':sum(r['split']=='held_out' for r in results),'results':results}
    (ROOT/'docs/evaluation-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f"{report['passed']}/{report['total']} state/evidence evaluations passed; {report['held_out']} held out. Model cost: $0.00.")
    if report['passed']!=report['total']:raise SystemExit(1)

if __name__=='__main__':run()
