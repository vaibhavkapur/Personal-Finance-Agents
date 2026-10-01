"""One-command HTTP walkthrough; each run creates a fresh synthetic tenant/case."""
import argparse
import json
import time
from pathlib import Path
import httpx


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:8096')
    parser.add_argument('--scenario',default='two_jobs',choices=['two_jobs','corrected_form','balance_due','rejection','timeout','malformed','delayed','missing_interest'])
    args=parser.parse_args()
    client=httpx.Client(base_url=args.base_url,timeout=15)
    def request(path,body=None):
        r=client.get('/v1'+path) if body is None else client.post('/v1'+path,json=body)
        r.raise_for_status();return r.json()
    request('/session/demo',{})
    config=request('/config')
    c=request('/tax-cases',{'scenario':args.scenario})
    def step(path,extra=None):
        nonlocal c
        c=request(f"/tax-cases/{c['id']}/{path}",{'expected_case_version':c['version'],**(extra or {})})
        return c
    step('profile-check',{'profile':config['sample_profile']})
    step('sample-documents')
    if args.scenario=='missing_interest':
        step('reconcile',{'completeness_confirmed':True})
        assert not c['reconciliation']['ready']
        step('forms',{'form':config['sample_forms'][2]})
    step('reconcile',{'completeness_confirmed':True})
    step('assist')
    if args.scenario=='corrected_form':
        original=c['forms'][0]
        step('forms',{'form':{'form_type':'W-2c','issuer_ref':original['issuer_ref'],'issuer_name':original['issuer_name'],'supersedes_form_id':original['id'],'federal_withholding_minor':530050,'confirmed':True}})
        assert c['actions'][0]['status']=='invalidated'
        step('reconcile',{'completeness_confirmed':True})
        step('assist')
    def approve_and_wait():
        nonlocal c
        action=next(a for a in c['actions'] if a['id']==c['package']['action_id'])
        c=request('/actions/'+action['id']+'/approve',{'expected_case_version':c['version'],'action_payload_hash':action['payload_hash'],'approval_challenge_id':action['challenge_id']})
        for _ in range(30):
            time.sleep(.5)
            c=request('/tax-cases/'+c['id'])
            if c['state'] in {'submitted','refund_pending'}:break
        else:raise RuntimeError('Worker did not reconcile submission within 15 seconds')
    approve_and_wait()
    if args.scenario=='rejection':
        step('clock',{'days':1});assert c['state']=='rejected'
        step('resolve-rejection');step('reconcile',{'completeness_confirmed':True});step('assist');approve_and_wait()
    step('clock',{'days':1})
    if args.scenario not in {'delayed','balance_due'}:assert c['filing']['financial']=='refund_pending'
    step('clock',{'days':7})
    expected='balance_due_followup' if args.scenario=='balance_due' else 'refund_verified'
    assert c['state']==expected,(c['state'],expected)
    print(json.dumps({'scenario':args.scenario,'case_id':c['id'],'state':c['state'],'tax_minor':c['calculation']['tax_minor'],'refund_minor':c['calculation']['refund_minor'],'amount_due_minor':c['calculation']['amount_due_minor'],'submission_ref':c['filing']['submission_ref'],'account_event_ref':c['filing'].get('account_event_ref'),'environment':'mock'},indent=2))

if __name__=='__main__':main()
