"""Three isolated, credential-free end-to-end demonstrations."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.persistence.store import Store
from backend.app.adapters.mock import MockCustodian
from backend.app.workflows.service import WealthService


def run(name):
    with TemporaryDirectory(prefix='northstar-demo-') as d:
        svc=WealthService(Store(d+'/wealth.db'),MockCustodian(d+'/provider.db'))
        before=svc.state()['goal_status'][0]
        svc.revise_goal(svc.state()['version'],'2027-09-25',100000 if name=='insufficient' else 200000,'flat_return_fixture')
        after=svc.state()['goal_status'][0]
        result={'demo':name,'original_monthly_minor':before['required_monthly_minor'],'revised_monthly_minor':after['required_monthly_minor'],'shortfall_minor':after['shortfall_minor']}
        if name!='insufficient':
            svc.configure(svc.state()['version'],mode='partial' if name=='partial' else 'normal')
            p=svc.propose(svc.state()['version'],200000)
            svc.approve(p['id'],svc.state()['version'],p['payload_hash'],p['challenge_id']);svc.tick()
            s=svc.state();result.update(status=s['case']['status'],actions=s['proposals'][-1]['actions'],reconciliation=s['proposals'][-1]['reconciliation'])
            if name=='partial':
                svc.configure(s['version'],mode='normal')
                remainder=svc.propose(svc.state()['version'],0)
                svc.approve(remainder['id'],svc.state()['version'],remainder['payload_hash'],remainder['challenge_id']);svc.tick()
                result['remainder_status']=svc.state()['case']['status']
                assert svc.state()['goal_status'][0]['current_minor']==3800000
        return result

if __name__=='__main__':
    names=sys.argv[1:] or ['earlier','insufficient','partial']
    for name in names:
        if name not in ('earlier','insufficient','partial'):raise SystemExit('Choose earlier, insufficient or partial')
        print(json.dumps(run(name),indent=2))
