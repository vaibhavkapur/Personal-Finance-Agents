"""One command, three isolated end-to-end demos; no server or credentials needed."""
import tempfile
from backend.app.workflows.service import PaydayService, TENANT, CUSTOMER
from backend.app.worker import run_once


def approve(s,case,proposal,mode='normal'):
    a=s.draft(TENANT,case['id'],proposal['id'],case['id'],mode)
    s.approve(TENANT,a['id'],{k:a[k] for k in ('expected_case_version','action_payload_hash','approval_challenge_id')},'demo-script')
    return run_once(s)


def main():
    for scenario in ('supported','late','returned'):
        with tempfile.TemporaryDirectory(prefix='payday-demo-') as directory:
            s=PaydayService(directory);s.seed(scenario='late' if scenario=='late' else 'supported')
            case=s.create_case(TENANT,CUSTOMER,300000)
            proposal=s.calculate_case(TENANT,case['id'])['proposal']
            print(f"\n{scenario.upper()}: requested $3,000.00; feasible ${proposal['feasible_minor']/100:,.2f}; capacity ${proposal['capacity_minor']/100:,.2f}")
            transfer=approve(s,case,proposal)
            assert transfer['status']=='reconciled'
            if scenario=='returned':
                s.bank.return_transfer(transfer['request_ref'],s.now())
            p=s.refresh(TENANT,CUSTOMER)
            assert sum(p['buckets'].values())==p['bank_snapshot']['available_minor']
            print(f"State: {s.case(TENANT,case['id'])['status']}; bank cash ${p['bank_snapshot']['available_minor']/100:,.2f}")
            print('Buckets:', ', '.join(f'{key}=${amount/100:,.2f}' for key,amount in p['buckets'].items()))
            print('Evidence:',transfer['evidence']['provider_ref'])
    print('\nAll three demos reconcile. All money and provider actions are simulated.')

if __name__=='__main__':main()
