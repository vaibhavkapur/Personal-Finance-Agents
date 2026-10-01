import time
import pytest
from backend.app.domain.engine import RuleError,account_values
from backend.app.workflows.service import WealthService


def proposal(service,amount=200000,mode=None):
    if mode:service.configure(service.state()['version'],mode=mode)
    return service.propose(service.state()['version'],amount)


def approve(service,p):
    return service.approve(p['id'],service.state()['version'],p['payload_hash'],p['challenge_id'])


def test_goal_revision_invalidates_basket_not_mandate(service):
    p=proposal(service);old=service.state()['mandate'].copy()
    service.revise_goal(service.state()['version'],'2027-09-25',200000,'flat_return_fixture')
    s=service.state()
    assert s['proposals'][0]['status']=='stale' and s['mandate']==old
    with pytest.raises(RuleError):approve(service,p)


def test_no_unapproved_execution(service):
    p=proposal(service)
    assert not service.tick()
    assert not service.provider.find_action(p['actions'][0]['request_ref'])


def test_exact_payload_and_case_version(service):
    p=proposal(service)
    with pytest.raises(RuleError):service.approve(p['id'],service.state()['version'],'bad',p['challenge_id'])
    with pytest.raises(RuleError):service.approve(p['id'],1,p['payload_hash'],p['challenge_id'])


def test_expiry_blocks_approval(service):
    p=proposal(service);service.configure(service.state()['version'],advance_minutes=16)
    with pytest.raises(RuleError):approve(service,p)
    assert service.state()['proposals'][0]['status']=='expired'


def test_revoked_after_approval_blocks_execution(service):
    p=proposal(service);approve(service,p)
    service.configure(service.state()['version'],revoke=True);service.tick()
    assert service.state()['case']['status']=='exception_review'
    assert service.provider.find_action(p['actions'][0]['request_ref']) is None


def test_complete_verified_conservation(service):
    p=proposal(service);approve(service,p);service.tick();s=service.state()
    assert s['case']['status']=='completed'
    assert s['case']['completion_evidence']['conserved']
    assert len(s['case']['completion_evidence']['provider_refs'])==2
    assert account_values(s['accounts'][0])=={'equity':1600000,'bonds':1440000,'cash':760000}
    assert s['case']['next_review_at']


def test_partial_preserves_success_and_replans_remainder(service):
    p=proposal(service,mode='partial');approve(service,p);service.tick();s=service.state()
    assert s['case']['status']=='exception_review'
    assert [a['status'] for a in s['proposals'][0]['actions']]==['filled','declined']
    assert account_values(s['accounts'][0])=={'equity':1600000,'bonds':1400000,'cash':800000}
    assert s['proposals'][0]['reconciliation']['conserved']
    service.configure(s['version'],mode='normal');p=proposal(service,amount=0)
    assert all(a['payload']['type']=='buy' for a in p['actions'])
    approve(service,p);service.tick()
    assert service.state()['case']['status']=='completed'
    assert service.state()['goal_status'][0]['current_minor']==3800000


@pytest.mark.parametrize('mode',['timeout_after_acceptance','malformed'])
def test_uncertain_response_resolves_by_original_reference(service,mode):
    p=proposal(service,mode=mode);approve(service,p);service.tick();s=service.state()
    assert s['case']['status']=='completed' and len(s['applied_execution_refs'])==2
    for a in p['actions']:
        assert service.provider.submit_action(a['payload'],a['request_ref'],mode)==service.provider.find_action(a['request_ref'])


def test_late_contribution_waits_for_funds(service):
    p=proposal(service,mode='late_contribution');approve(service,p);service.tick();s=service.state()
    assert s['proposals'][0]['actions'][0]['status']=='pending'
    assert s['accounts'][0]['cash_minor']==600000
    assert service.provider.find_action(p['actions'][1]['request_ref']) is None
    service.settle()
    assert service.state()['case']['status']=='completed'


def test_accepted_action_reconciles_after_expiry(service):
    p=proposal(service,mode='late_contribution');approve(service,p);service.tick()
    service.configure(service.state()['version'],advance_minutes=16);service.settle();s=service.state()
    assert s['goal_status'][0]['current_minor']==3800000
    assert s['case']['status'] in ('manual_review','exception_review')
    assert service.provider.find_action(p['actions'][1]['request_ref']) is None


def test_restart_recovers_approved_job(service):
    p=proposal(service);approve(service,p)
    resumed=WealthService(service.store,service.provider);resumed.tick()
    assert resumed.state()['case']['status']=='completed'


def test_crash_after_provider_accepted_before_local_apply(service):
    p=proposal(service);approve(service,p);a=p['actions'][0]
    service.provider.submit_action(a['payload'],a['request_ref'])
    resumed=WealthService(service.store,service.provider);resumed.tick()
    assert resumed.state()['case']['status']=='completed'
    assert resumed.state()['goal_status'][0]['current_minor']==3800000


def test_expired_worker_lease_recovers(service):
    p=proposal(service);approve(service,p)
    assert service.store.claim() and not service.store.claim()
    with service.store.transaction() as db:db.execute('UPDATE jobs SET lease_until=?',(time.time()-1,))
    service.tick()
    assert service.state()['case']['status']=='completed'


def test_callback_deduplication(service):
    p=proposal(service);approve(service,p);service.tick();before=service.state()['accounts']
    assert service.webhook('event1',p['actions'][0]['request_ref'])=={'accepted':True}
    assert service.webhook('event1',p['actions'][0]['request_ref'])=={'duplicate':True}
    service.tick()
    assert service.state()['accounts']==before
    assert service.state()['metrics']['duplicate_events_prevented']==1


def test_idempotency_content_binding(service):
    v=service.state()['version'];p=service.propose(v,200000,idempotency_key='key')
    assert service.propose(v,200000,idempotency_key='key')['id']==p['id']
    with pytest.raises(RuleError):service.propose(v,300000,idempotency_key='key')


def test_mandate_approval_is_separate(service):
    proposal(service)
    service.mandate_draft(service.state()['version'],{'equity':3000,'bonds':4000,'cash':3000},500000)
    s=service.state();assert s['mandate']['version']==1 and s['proposals'][0]['status']=='stale'
    service.approve_mandate(s['version'],s['mandate_draft']['payload_hash']);s=service.state()
    assert s['mandate']['version']==2 and s['case']['status']=='planning' and not service.tick()


def test_mutated_payload_fails_hash_check(service):
    p=proposal(service)
    with service.store.transaction() as db:
        s=service.store.load(db);s['proposals'][0]['payload']['basket']['actions'][0]['amount_minor']=999999;service.store.save(db,s)
    with pytest.raises(RuleError):approve(service,p)


def test_refresh_invalidates_snapshot(service):
    p=proposal(service);service.configure(service.state()['version'],refresh=True)
    with pytest.raises(RuleError):approve(service,p)

def test_next_review_timer_survives_restart_and_deduplicates(service):
    p=proposal(service);approve(service,p);service.tick()
    service.configure(service.state()['version'],advance_minutes=30*24*60)
    restarted=WealthService(service.store,service.provider)
    restarted.tick();restarted.tick()
    assert len([e for e in restarted.state()['events'] if e['event_type']=='review.due'])==1


def test_independent_provider_holdings_agree(service):
    p=proposal(service);approve(service,p);service.tick()
    assert service.state()['proposals'][0]['reconciliation']['matches_provider_holdings']
    assert service.state()['accounts']==service.provider.get_holdings('cus_demo_7')['accounts']


def test_declined_remainder_after_expiry_can_be_replanned(service):
    p=proposal(service,mode='late_contribution');approve(service,p);service.tick()
    service.configure(service.state()['version'],advance_minutes=16);service.settle()
    assert service.state()['case']['status']=='exception_review'
    service.configure(service.state()['version'],mode='normal')
    assert proposal(service,amount=0)['status']=='awaiting_approval'


def test_action_copy_cannot_bypass_approved_payload(service):
    p=proposal(service)
    with service.store.transaction() as db:
        s=service.store.load(db);s['proposals'][0]['actions'][0]['payload']['amount_minor']=999999;service.store.save(db,s)
    with pytest.raises(RuleError):approve(service,p)
