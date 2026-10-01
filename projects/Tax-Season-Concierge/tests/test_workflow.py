import copy
import json
import time
import pytest
from app.api.schemas import ApprovalRequest,Form
from app.persistence.store import Conflict,Store
from app.workflows.engine import Engine
from app.domain.tax import load_rules
from app import seed
from conftest import draft,approve


def correction(c):
    f=next(f for f in c['forms'] if f['issuer_ref']=='fixture_northstar')
    return Form(form_type='W-2c',issuer_ref=f['issuer_ref'],issuer_name=f['issuer_name'],supersedes_form_id=f['id'],federal_withholding_minor=530050,confirmed=True).model_dump()


def test_approval_submission_acceptance_and_refund_separate(engine):
    c=draft(engine)
    assert engine.process_one() is False
    assert c['state']=='awaiting_approval'
    c=approve(engine,c)
    engine.process_one()
    c=engine.get('tenant_a',c['id']);assert c['state']=='submitted'
    c=engine.advance('tenant_a',c['id'],c['version'],1)
    assert c['state']=='refund_pending' and not c['filing'].get('account_event_ref')
    c=engine.advance('tenant_a',c['id'],c['version'],1)
    assert c['state']=='refund_pending' and c['filing']['refund_notice_ref']
    c=engine.advance('tenant_a',c['id'],c['version'],1)
    assert c['state']=='refund_verified' and c['filing']['verified_amount_minor']==86200
    event={'id':c['filing']['submission_ref']+':account_credit','case_id':c['id'],'submission_ref':c['filing']['submission_ref'],'type':'account_credit','environment':'mock','amount_minor':86200,'account_event_ref':c['filing']['account_event_ref'],'code':None}
    assert engine.apply_event(event)=={'duplicate':True}
    engine.advance('tenant_a',c['id'],c['version'],5)
    assert len([e for e in engine.get('tenant_a',c['id'])['events'] if e['type']=='refund.verified'])==1


def test_stale_facts_and_versions(engine):
    c=draft(engine)
    with pytest.raises(Conflict):engine.add_form('tenant_a',c['id'],1,seed.forms()[2])
    c=engine.add_form('tenant_a',c['id'],c['version'],correction(c))
    c=engine.reconcile('tenant_a',c['id'],c['version'],True)
    with pytest.raises(Conflict):engine.calculate('tenant_a',c['id'],c['version'],load_rules()[0]['id'],'stale')


def test_correction_invalidates_approved_unsubmitted_draft(engine):
    c=approve(engine,draft(engine));old_hash=c['package']['content_hash']
    c=engine.add_form('tenant_a',c['id'],c['version'],correction(c))
    assert c['actions'][0]['status']=='invalidated'
    assert c['actions'][0]['approval']['revoked_at']
    engine.process_one()
    assert c['state']=='reconciling' and c['calculation'] is None
    c=engine.reconcile('tenant_a',c['id'],c['version'],True)
    c=engine.assist('tenant_a',c['id'],c['version'])
    assert c['calculation']['refund_minor']==136200
    assert c['package']['content_hash']!=old_hash
    assert c['actions'][-1]['approval'] is None


def test_after_acceptance_preserves_original_package(engine):
    c=approve(engine,draft(engine));engine.process_one();c=engine.get('tenant_a',c['id'])
    c=engine.advance('tenant_a',c['id'],c['version'],1)
    original=copy.deepcopy(c['calculation']);package=c['package']['id']
    c=engine.add_form('tenant_a',c['id'],c['version'],correction(c))
    assert c['state']=='out_of_scope' and c['correction_review']
    assert c['calculation']==original and c['package']['id']==package
    with pytest.raises(Conflict):engine.prepare('tenant_a',c['id'],c['version'])
    c=engine.advance('tenant_a',c['id'],c['version'],3)
    assert c['state']=='out_of_scope' and c['filing']['financial']=='refund_verified'


def test_balance_due(engine):
    c=approve(engine,draft(engine,'balance_due'));engine.process_one();c=engine.get('tenant_a',c['id'])
    c=engine.advance('tenant_a',c['id'],c['version'],5)
    assert c['state']=='balance_due_followup'
    assert c['calculation']['refund_minor']==0 and c['calculation']['amount_due_minor']==233900
    assert not c['filing'].get('account_event_ref')


def test_rejection_new_approval_required(engine):
    c=approve(engine,draft(engine,'rejection'));engine.process_one();c=engine.get('tenant_a',c['id'])
    c=engine.advance('tenant_a',c['id'],c['version'],1)
    assert c['state']=='rejected'
    old_ref=c['filing']['submission_ref']
    c=engine.resolve_rejection('tenant_a',c['id'],c['version'])
    c=engine.reconcile('tenant_a',c['id'],c['version'],True)
    c=engine.assist('tenant_a',c['id'],c['version'])
    assert c['package']['revision']==2
    engine.process_one();assert engine.get('tenant_a',c['id'])['state']=='awaiting_approval'
    c=approve(engine,c);engine.process_one();c=engine.get('tenant_a',c['id'])
    c=engine.advance('tenant_a',c['id'],c['version'],1)
    assert c['state']=='refund_pending' and c['filing']['submission_ref']!=old_ref


@pytest.mark.parametrize('scenario',['timeout','malformed'])
def test_uncertain_write_looks_up_original_after_restart(engine,scenario):
    c=approve(engine,draft(engine,scenario))
    engine.process_one()
    c=engine.get('tenant_a',c['id']);assert c['state']=='manual_review'
    with engine.store.transaction() as db:
        db.execute('UPDATE jobs SET lease_until=0')
    restarted=Engine(Store(engine.store.url));restarted.process_one()
    c=restarted.get('tenant_a',c['id'])
    assert c['state'] in {'submitted','refund_pending'}
    with engine.store.transaction() as db:
        assert db.execute('SELECT COUNT(*) AS n FROM provider_submissions').fetchone()['n']==1
    assert c['actions'][0]['approval']['consumed_at']


def test_lease_recovers_after_crash(engine):
    c=approve(engine,draft(engine))
    with engine.store.transaction() as db:
        db.execute("UPDATE jobs SET status='running',lease_until=0")
    restarted=Engine(Store(engine.store.url));restarted.process_one()
    assert restarted.get('tenant_a',c['id'])['state']=='submitted'


def test_approval_tampering_expiry_and_idempotency(engine):
    c=draft(engine);a=c['actions'][0]
    with pytest.raises(Conflict):engine.approve('tenant_a',a['id'],ApprovalRequest(expected_case_version=c['version'],action_payload_hash='changed',approval_challenge_id=a['challenge_id']))
    with engine.store.transaction() as db:
        a['challenge_expires_at']=0;engine.store.save_action(db,a)
    with pytest.raises(Conflict):approve(engine,c)
    c=engine.prepare('tenant_a',c['id'],c['version'])
    c=approve(engine,c);count=len(c['events']);c=approve(engine,c)
    assert len(c['events'])==count
    with engine.store.transaction() as db:
        a=engine.store.get_action(db,c['package']['action_id']);a['approval']['expires_at']=0;engine.store.save_action(db,a)
    engine.process_one();c=engine.get('tenant_a',c['id'])
    assert c['state']=='reconciling'
    assert not c['filing'].get('submission_ref')


def test_inflight_document_edit_blocked(engine):
    c=approve(engine,draft(engine))
    with engine.store.transaction() as db:
        a=engine.store.get_action(db,c['package']['action_id']);a['status']='executing';engine.store.save_action(db,a)
    with pytest.raises(Conflict):engine.add_form('tenant_a',c['id'],c['version'],correction(c))
    with pytest.raises(Conflict):engine.profile_check('tenant_a',c['id'],c['version'],seed.profile())


def test_out_of_order_and_unmatched_credit_rejected(engine):
    c=approve(engine,draft(engine));engine.process_one();c=engine.get('tenant_a',c['id'])
    event={'id':'credit_1','case_id':c['id'],'submission_ref':c['filing']['submission_ref'],'type':'account_credit','environment':'mock','amount_minor':86200,'account_event_ref':'credit'}
    with pytest.raises(Conflict):engine.apply_event(event)
    c=engine.advance('tenant_a',c['id'],c['version'],1)
    with pytest.raises(Conflict):engine.apply_event({**event,'amount_minor':1})
    with pytest.raises(ValueError):engine.apply_event({**event,'account_event_ref':None})
    with pytest.raises(Conflict):engine.apply_event({**event,'submission_ref':'other'})
    assert engine.get('tenant_a',c['id'])['state']=='refund_pending'


def test_missing_info_orchestrator_no_invention(engine):
    c=draft(engine,'missing_interest');c=engine.assist('tenant_a',c['id'],c['version'])
    assert c['calculation'] is None
    assert c['reconciliation']['issues']
    assert c['tool_runs'][-1]['cost_usd']==0


def test_cross_tenant_resources(engine):
    c=draft(engine)
    with pytest.raises(KeyError):engine.get('other_tenant',c['id'])
    with pytest.raises(KeyError):approve(engine,c,'other_tenant')
    assert not engine.list('other_tenant')


def test_crash_after_provider_write_recovers_by_original_reference(engine):
    c=approve(engine,draft(engine));real=engine.provider.submit_mock_return
    def interrupted(package,ref):
        real(package,ref)
        raise SystemExit('simulated worker termination')
    engine.provider.submit_mock_return=interrupted
    with pytest.raises(SystemExit):engine.process_one()
    with engine.store.transaction() as db:db.execute('UPDATE jobs SET lease_until=0')
    restarted=Engine(Store(engine.store.url));restarted.process_one()
    assert restarted.get('tenant_a',c['id'])['state']=='submitted'
    with engine.store.transaction() as db:assert db.execute('SELECT COUNT(*) AS n FROM provider_submissions').fetchone()['n']==1


def test_unconfirmed_document_can_be_confirmed_without_overwriting_evidence(engine):
    from app.api.schemas import CreateCase
    c=engine.create('tenant_a',CreateCase());c=engine.profile_check('tenant_a',c['id'],c['version'],seed.profile())
    f=seed.forms()[0];f['confirmed']=False
    c=engine.add_form('tenant_a',c['id'],c['version'],f);original_hash=c['forms'][0]['content_hash']
    c=engine.confirm_form('tenant_a',c['id'],c['forms'][0]['id'],c['version'])
    assert c['forms'][0]['confirmed'] and c['forms'][0]['original_extraction']['confirmed'] is False
    assert c['forms'][0]['content_hash']==original_hash
    assert c['forms'][0]['confirmation_evidence']['actor']=='tenant_a'


def test_outbox_delivery_is_durable_and_idempotent(engine):
    c=draft(engine);assert engine.metrics('tenant_a')['undelivered_events']>0
    engine.process_one();first=engine.metrics('tenant_a')
    engine.process_one();assert engine.metrics('tenant_a')==first
    assert first['delivered_events']==len(c['events']) and first['undelivered_events']==0
    assert engine.metrics('tenant_b')['delivered_events']==0


def test_unresolvable_write_holds_then_operator_replays_original_authority(engine):
    c=approve(engine,draft(engine));real=engine.provider
    class Unavailable:
        def find_submission(self,ref):return None
        def submit_mock_return(self,package,ref):raise TimeoutError('unknown outcome fixture')
    engine.provider=Unavailable()
    for _ in range(4):
        engine.process_one()
        with engine.store.transaction() as db:db.execute('UPDATE jobs SET lease_until=0')
    c=engine.get('tenant_a',c['id']);assert c['state']=='manual_review'
    assert engine.metrics('tenant_a')['held_jobs']==1
    assert c['actions'][0]['approval']['consumed_at']
    c=engine.replay('tenant_a',c['id'],c['version']);engine.provider=real;engine.process_one()
    assert engine.get('tenant_a',c['id'])['state']=='submitted'
    assert engine.metrics('tenant_a')['held_jobs']==0
