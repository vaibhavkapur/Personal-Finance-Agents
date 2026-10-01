import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.persistence.store import Store
from backend.app.adapters.mock import MockCustodian
from backend.app.workflows.service import WealthService
from backend.app.agent.tools import ToolGateway,RulesPlanner
root=Path(__file__).resolve().parents[1]
fixtures=json.loads((root/'fixtures/evaluation-v1.json').read_text())
results=[]
for case in fixtures['cases']:
    with TemporaryDirectory(prefix='northstar-eval-') as d:
        svc=WealthService(Store(d+'/wealth.db'),MockCustodian(d+'/provider.db'))
        if case['setup_mode']:
            svc.configure(svc.state()['version'],mode=case['setup_mode'])
            p=svc.propose(svc.state()['version'],200000)
            svc.approve(p['id'],svc.state()['version'],p['payload_hash'],p['challenge_id']);svc.tick()
        before=svc.state()
        result=RulesPlanner().respond(case['message'],ToolGateway(svc))
        after=svc.state()
        unchanged=before['goals']==after['goals'] and before['mandate']==after['mandate'] and before['accounts']==after['accounts']
        passed=result['outcome']==case['expected_outcome'] and unchanged and len(result['tool_calls'])<=5
        results.append({'id':case['id'],'split':case['split'],'expected':case['expected_outcome'],'actual':result['outcome'],'passed':passed,'financial_state_unchanged':unchanged,'tool_calls':len(result['tool_calls'])})
report={'fixture_version':fixtures['fixture_version'],'planner':'rules-only baseline','llm_comparison':'Not run; no LLM configured. No model quality claim.','model_cost_usd':0,'total':len(results),'passed':sum(r['passed'] for r in results),'results':results}
(root/'docs/evaluation-report.json').write_text(json.dumps(report,indent=2)+'\n')
print(f"Rules baseline: {report['passed']}/{report['total']} passed; 6 held-out labels; model cost $0.")
if report['passed']!=report['total']:raise SystemExit(1)
