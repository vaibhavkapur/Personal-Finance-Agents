import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4
from ..domain.engine import (RuleError, require, calculate, plan_basket, validate_prices,
                             validate_mandate, validate_allocations, account_values)
from ..fixtures import seed_state


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def later(clock, **kwargs):
    return (datetime.fromisoformat(clock)+timedelta(**kwargs)).isoformat()

ACTIVE = ('approved', 'executing', 'reconciling')
TRANSITIONS = {
    'collecting': {'planning'}, 'planning': {'proposal_ready', 'mandate_review', 'planning'},
    'proposal_ready': {'awaiting_approval', 'stale'}, 'awaiting_approval': {'approved', 'rejected', 'expired', 'stale'},
    'approved': {'executing', 'exception_review'}, 'executing': {'reconciling', 'partially_executed', 'exception_review', 'manual_review'},
    'partially_executed': {'reconciling'}, 'reconciling': {'completed', 'exception_review', 'manual_review'},
    'completed': {'planning'}, 'exception_review': {'planning'}, 'manual_review': {'reconciling', 'planning'},
    'rejected': {'planning'}, 'expired': {'planning'}, 'stale': {'planning'}, 'mandate_review': {'planning'},
}

class WealthService:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider
        with store.transaction() as db:
            if not store.load(db):
                state = seed_state()
                store.save(db, state)
                store.event(db, state, 'portfolio.seeded', {'source':'synthetic-fixture-v1', 'environment':'mock'}, 'system')

    def transition(self, db, state, target, actor='system'):
        before = state['case']['status']
        require(target in TRANSITIONS.get(before, set()), 'Invalid case transition: '+before+' → '+target, 409)
        state['case']['status'] = target
        self.store.event(db, state, 'case.'+target, {'previous_state':before, 'next_state':target,
                        'expected_case_version':state['version']}, actor)

    def commit(self, db, state):
        validate_allocations(state)
        state['version'] += 1
        state['case']['version'] = state['version']
        self.store.save(db, state)

    def expected(self, state, version):
        require(state['version'] == version, 'The case changed. Refresh and review the current version.', 409)

    def idle(self, state):
        require(not any(p['status'] in ACTIVE for p in state['proposals']), 'An approved basket is still being reconciled.', 409)
        require(not any(a['status'] in ('pending','submitting','unknown') for p in state['proposals'] for a in p['actions']),
                'Resolve the uncertain provider action before changing this case.', 409)

    def invalidate(self, db, state, reason):
        for p in state['proposals']:
            if p['status'] in ('awaiting_approval','proposal_ready'):
                p['status'] = 'stale'
                p['approval'] = None
                self.store.event(db, state, 'proposal.invalidated', {'proposal_id':p['id'], 'reason':reason})
        if state['case']['status'] in ('awaiting_approval','proposal_ready'):
            self.transition(db,state,'stale')
        if state['case']['status'] != 'planning':
            self.transition(db,state,'planning')
        state['case']['completion_evidence'] = None

    def state(self):
        with self.store.connect() as db:
            s = self.store.load(db)
            s['goal_status'] = [calculate(s,g['id'],s['scenario_id']) for g in s['goals']]
            s['events'] = [{**dict(row),'data':json.loads(row['data'])} for row in db.execute('SELECT * FROM events WHERE customer_id=? ORDER BY id DESC LIMIT 100',(s['customer_id'],))]
            s['jobs'] = [dict(row) for row in db.execute('SELECT * FROM jobs WHERE customer_id=? ORDER BY rowid DESC LIMIT 20',(s['customer_id'],))]
            s['tool_runs'] = [dict(row) for row in db.execute('SELECT * FROM tool_runs WHERE customer_id=? ORDER BY id DESC LIMIT 30',(s['customer_id'],))]
            s['metrics'] = {'events':db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                            'pending_jobs':db.execute("SELECT COUNT(*) FROM jobs WHERE status!='done'").fetchone()[0],
                            'pending_outbox':db.execute('SELECT COUNT(*) FROM outbox WHERE delivered=0').fetchone()[0],
                            'duplicate_events_prevented':db.execute("SELECT COUNT(*) FROM events WHERE event_type='provider.duplicate_ignored'").fetchone()[0]}
            s['provider_capabilities'] = self.provider.capabilities
            return s

    def preview(self, target_date, monthly_minor, scenario_id):
        with self.store.connect() as db:
            s = self.store.load(db)
        return {'before':calculate(s,'house',scenario_id),
                'after':calculate(s,'house',scenario_id,target_date,monthly_minor)}

    def revise_goal(self, version, target_date, monthly_minor, scenario_id):
        with self.store.transaction() as db:
            s = self.store.load(db)
            self.expected(s,version)
            self.idle(s)
            result = calculate(s,'house',scenario_id,target_date,monthly_minor)
            self.invalidate(db,s,'Goal revision')
            goal = s['goals'][0]
            before = dict(goal)
            goal.update(target_date=target_date, monthly_minor=monthly_minor, version=goal['version']+1)
            s['scenario_id'] = scenario_id
            self.store.event(db,s,'goal.revised',{'before':before,'after':goal,'analysis':result})
            self.commit(db,s)
        return self.state()

    def propose(self, version, contribution_minor, account_id='taxable', idempotency_key=None):
        with self.store.transaction() as db:
            s = self.store.load(db)
            if idempotency_key:
                old = next((p for p in s['proposals'] if p.get('idempotency_key')==idempotency_key),None)
                if old:
                    require(old['contribution_minor']==contribution_minor and old['payload']['account_id']==account_id,'Idempotency key reused with different content.',409)
                    return old
            self.expected(s,version)
            self.idle(s)
            basket = plan_basket(s,contribution_minor,account_id)
            require(basket['actions'], 'No action is required for this contribution and allocation.')
            self.invalidate(db,s,'New proposal supersedes prior draft')
            proposal_id = 'proposal_'+uuid4().hex[:10]
            payload = {'goal_version':s['goals'][0]['version'], 'mandate_version':s['mandate']['version'],
                       'holdings_version':s['holdings_version'], 'holdings_hash':digest(s['accounts']), 'account_id':account_id,
                       'basket':basket, 'expires_at':later(s['clock'],minutes=15)}
            actions = [{'id':proposal_id+'_'+str(i), 'request_ref':proposal_id+'_'+str(i), 'payload':a,
                        'status':'ready', 'provider_ref':None, 'executed':None} for i,a in enumerate(basket['actions'])]
            p = {'id':proposal_id, 'version':1, 'payload':payload, 'payload_hash':digest(payload), 'status':'awaiting_approval',
                 'challenge_id':'challenge_'+uuid4().hex, 'actions':actions, 'approval':None, 'reconciliation':None,
                 'contribution_minor':contribution_minor, 'expected_holdings_version':s['holdings_version'],
                 'idempotency_key':idempotency_key, 'created_at':s['clock']}
            s['proposals'].append(p)
            self.transition(db,s,'proposal_ready')
            self.transition(db,s,'awaiting_approval')
            self.store.event(db,s,'proposal.prepared',{'proposal_id':proposal_id,'payload_hash':p['payload_hash'],'mandate_version':s['mandate']['version']})
            self.commit(db,s)
        return p

    def find_proposal(self, s, proposal_id):
        p = next((p for p in s['proposals'] if p['id']==proposal_id),None)
        require(p is not None,'Proposal not found.',404)
        return p

    def authority(self, s, p):
        require(p['payload_hash']==digest(p['payload']), 'The immutable action basket changed.',409)
        require([a['payload'] for a in p['actions']]==p['payload']['basket']['actions'],'Action payloads differ from the reviewed basket.',409)
        require(p['payload']['goal_version']==s['goals'][0]['version'],'The goal version changed.',409)
        require(p['payload']['mandate_version']==s['mandate']['version'],'The mandate version changed.',409)
        require(p['expected_holdings_version']==s['holdings_version'],'Holdings changed. Replan from verified balances.',409)
        require(s['clock'] < p['payload']['expires_at'],'Approval expired. Prepare a fresh proposal.',409)
        validate_prices(s)
        validate_mandate(s)
        require(all(self.provider.preview_action(a['payload'])['supported'] for a in p['actions']),'Unsupported action.',409)

    def approve(self, proposal_id, version, payload_hash, challenge_id):
        with self.store.transaction() as db:
            s = self.store.load(db)
            p = self.find_proposal(s,proposal_id)
            self.expected(s,version)
            require(p['status']=='awaiting_approval','This basket is not awaiting approval.',409)
            require(challenge_id==p['challenge_id'] and payload_hash==p['payload_hash'],'Approval does not match the reviewed basket.',409)
            self.authority(s,p)
            require(digest(s['accounts'])==p['payload']['holdings_hash'],'Valuations changed. Prepare a fresh preview.',409)
            p['approval'] = {'id':'approval_'+uuid4().hex[:12], 'approver':'cus_demo_7', 'scope':'exact_basket',
                             'payload_hash':payload_hash, 'approved_at':s['clock'], 'expires_at':p['payload']['expires_at'],
                             'revoked_at':None, 'consumed_at':None}
            p['status'] = 'approved'
            self.transition(db,s,'approved','customer')
            self.store.event(db,s,'basket.approved',{'proposal_id':p['id'],'action_payload_hash':payload_hash})
            db.execute('INSERT INTO jobs(id,customer_id,proposal_id,status) VALUES(?,?,?,?)',('job_'+p['id'],s['customer_id'],p['id'],'pending'))
            self.commit(db,s)
        return self.state()

    def reject(self, proposal_id, version):
        with self.store.transaction() as db:
            s=self.store.load(db)
            self.expected(s,version)
            p=self.find_proposal(s,proposal_id)
            require(p['status']=='awaiting_approval','Only an unexecuted draft can be rejected.',409)
            p['status']='rejected'
            self.transition(db,s,'rejected','customer')
            self.commit(db,s)
        return self.state()

    def mandate_draft(self, version, weights, cash_floor_minor):
        require(set(weights)=={'equity','bonds','cash'} and sum(weights.values())==10000 and all(0<=v<=10000 for v in weights.values()),'Weights must sum to 100%.')
        with self.store.transaction() as db:
            s=self.store.load(db)
            self.expected(s,version)
            self.idle(s)
            self.invalidate(db,s,'Mandate under review')
            draft={'weights_bps':weights,'cash_floor_minor':cash_floor_minor,'version':s['mandate']['version']+1,
                   'expires_at':later(s['clock'],minutes=15),'challenge_id':'mandate_'+uuid4().hex}
            draft['payload_hash']=digest(draft)
            s['mandate_draft']=draft
            self.transition(db,s,'mandate_review')
            self.commit(db,s)
        return self.state()

    def approve_mandate(self, version, payload_hash):
        with self.store.transaction() as db:
            s=self.store.load(db)
            self.expected(s,version)
            self.idle(s)
            d=s['mandate_draft']
            require(d and d['payload_hash']==payload_hash,'Mandate review changed.',409)
            require(s['clock']<d['expires_at'],'Mandate review expired.',409)
            s['mandate'].update(weights_bps=d['weights_bps'],cash_floor_minor=d['cash_floor_minor'],version=d['version'],
                                approved_at=s['clock'],expires_at=later(s['clock'],days=365),revoked=False)
            s['mandate_draft']=None
            self.transition(db,s,'planning','customer')
            self.store.event(db,s,'mandate.approved',{'payload_hash':payload_hash,'version':s['mandate']['version']})
            self.commit(db,s)
        return self.state()

    def configure(self, version, mode=None, advance_minutes=0, refresh=False, revoke=False):
        with self.store.transaction() as db:
            s=self.store.load(db)
            self.expected(s,version)
            if mode is not None:
                self.idle(s)
                require(mode in ('normal','partial','timeout_after_acceptance','late_contribution','delayed_callback','malformed'),'Unknown simulator mode.')
                s['simulator']['mode']=mode
            if advance_minutes:
                s['clock']=later(s['clock'],minutes=advance_minutes)
            if refresh:
                self.idle(s)
                for a in s['accounts']:
                    for h in a['holdings']:
                        h['price_at']=s['clock']
                s['holdings_version']+=1
                self.invalidate(db,s,'Valuations refreshed')
            if revoke:
                s['mandate']['revoked']=True
            for p in s['proposals']:
                if p['status']=='awaiting_approval' and s['clock']>=p['payload']['expires_at']:
                    p['status']='expired'
                    self.transition(db,s,'expired')
            self.store.event(db,s,'simulator.configured',{'mode':mode,'advance_minutes':advance_minutes,'refresh':refresh,'revoked':revoke},'operator')
            self.commit(db,s)
        return self.state()

    def reconcile(self, db, s, p, result, action):
        ref=result['provider_ref']
        action.update(status=result['status'],provider_ref=ref,executed=result.get('executed'))
        if result['status']!='filled' or ref in s['applied_execution_refs']:
            return
        executed=result['executed']
        require(executed==action['payload'],'Provider execution contradicts the approved action.',409)
        a=next(a for a in s['accounts'] if a['id']==executed['account_id'])
        if executed['type']=='contribution':
            a['cash_minor']+=executed['amount_minor']
        elif executed['type']=='buy':
            require(a['cash_minor']-executed['amount_minor']>=s['mandate']['cash_floor_minor'],'Execution violates the cash floor.',409)
            h=next(h for h in a['holdings'] if h['instrument_id']==executed['instrument_id'])
            h['quantity_micro']+=executed['quantity_micro']
            a['cash_minor']-=executed['amount_minor']
        s['applied_execution_refs'].append(ref)
        s['holdings_version']+=1
        p['expected_holdings_version']=s['holdings_version']
        self.store.event(db,s,'custodian.action_verified',{'proposal_id':p['id'],'action_id':action['id'],'provider_ref':ref,'execution':executed},'worker')

    def process_job(self, job):
        """Provider calls occur outside local DB transactions. Resume by original request reference."""
        pid=job['proposal_id']
        write_started=False
        try:
            with self.store.transaction() as db:
                s=self.store.load(db)
                p=self.find_proposal(s,pid)
                if p['status'] not in ACTIVE:
                    return False
                approval=p['approval']
                require(approval and not approval['revoked_at'] and approval['payload_hash']==p['payload_hash'],'No valid approval.',409)
                if p['status']=='approved':
                    self.authority(s,p)
                    p['status']='executing'
                    approval['consumed_at']=s['clock']
                    self.transition(db,s,'executing','worker')
                    self.commit(db,s)
            for index in range(len(p['actions'])):
                with self.store.transaction() as db:
                    s=self.store.load(db)
                    p=self.find_proposal(s,pid)
                    action=p['actions'][index]
                    if action['status']=='filled':
                        continue
                    if action['status']=='declined':
                        break
                    # Reconcile any known accepted action even if its approval has since expired.
                    write_started=False
                    action['status']='submitting'
                    request_ref=action['request_ref']
                    payload=action['payload']
                    mode=s['simulator']['mode']
                    self.commit(db,s)
                result=self.provider.find_action(request_ref)
                if result is None:
                    with self.store.transaction() as db:
                        s=self.store.load(db)
                        p=self.find_proposal(s,pid)
                        self.authority(s,p)
                        require(p['approval'] and not p['approval']['revoked_at'],'Approval revoked.',409)
                        # Every write has an immutable, persisted request before submission.
                        self.store.event(db,s,'custodian.submitting',{'action_id':action['id'],'request_ref':request_ref},'worker')
                    try:
                        write_started=True
                        result=self.provider.submit_action(payload,request_ref,mode)
                    except TimeoutError:
                        result=self.provider.find_action(request_ref)
                if not result or 'provider_ref' not in result or 'status' not in result:
                    result=self.provider.find_action(request_ref)
                if not result or result.get('status') not in ('filled','declined','pending'):
                    raise RuleError('Provider outcome is unresolved. Manual review is required.',409)
                with self.store.transaction() as db:
                    s=self.store.load(db)
                    p=self.find_proposal(s,pid)
                    action=p['actions'][index]
                    self.reconcile(db,s,p,result,action)
                    self.commit(db,s)
                if result['status']=='pending':
                    return True
                if result['status']=='declined':
                    break
            fresh_provider_holdings=self.provider.get_holdings('cus_demo_7')
            with self.store.transaction() as db:
                s=self.store.load(db)
                p=self.find_proposal(s,pid)
                filled=[a for a in p['actions'] if a['status']=='filled']
                complete=len(filled)==len(p['actions'])
                if not complete:
                    self.transition(db,s,'partially_executed','worker')
                    for a in p['actions']:
                        if a['status']=='ready':
                            a['status']='frozen'
                self.transition(db,s,'reconciling','worker')
                account=next(a for a in s['accounts'] if a['id']==p['payload']['account_id'])
                actual=account_values(account)
                before=sum(p['payload']['basket']['before_values'].values())
                deposits=sum(a['executed']['amount_minor'] for a in filled if a['executed']['type']=='contribution')
                conserved=sum(actual.values())==before+deposits
                matches=actual==p['payload']['basket']['after_values']
                provider_account=next(a for a in fresh_provider_holdings['accounts'] if a['id']==account['id'])
                provider_matches=account_values(provider_account)==actual and [h['quantity_micro'] for h in provider_account['holdings']]==[h['quantity_micro'] for h in account['holdings']]
                complete=complete and conserved and matches and provider_matches
                p['reconciliation']={'actual_values':actual,'actual_weights_bps':{k:round(v*10000/sum(actual.values())) for k,v in actual.items()},
                                     'conserved':conserved,'matches_preview':matches,'matches_provider_holdings':provider_matches,'verified_at':s['clock'],
                                     'provider_refs':[a['provider_ref'] for a in filled], 'environment':'mock'}
                p['status']='completed' if complete else 'exception_review'
                self.transition(db,s,p['status'],'worker')
                if complete:
                    s['case']['completion_evidence']=p['reconciliation']
                    s['case']['next_review_at']=later(s['clock'],days=30)
                    db.execute('INSERT OR IGNORE INTO review_timers VALUES(?,?,?,?)',('review_'+p['id'],s['customer_id'],s['case']['next_review_at'],'pending'))
                self.store.event(db,s,'portfolio.reconciled',{'proposal_id':p['id'],**p['reconciliation']},'worker')
                self.commit(db,s)
            return False
        except (RuleError,ValueError) as exc:
            with self.store.transaction() as db:
                s=self.store.load(db)
                p=self.find_proposal(s,pid)
                unresolved=False
                for a in p['actions']:
                    if a['status']=='submitting':
                        a['status']='unknown' if write_started else 'frozen'
                        unresolved=unresolved or write_started
                    elif a['status']=='ready':
                        a['status']='frozen'
                target='manual_review' if unresolved else 'exception_review'
                if target not in TRANSITIONS.get(s['case']['status'],set()):
                    target='exception_review'
                p['status']=target
                p['error']=str(exc)
                self.transition(db,s,target,'worker')
                self.store.event(db,s,'execution.blocked',{'proposal_id':pid,'reason':str(exc)},'worker')
                self.commit(db,s)
            return False

    def tick(self):
        with self.store.transaction() as db:
            s=self.store.load(db)
            for timer in db.execute("SELECT * FROM review_timers WHERE status='pending' AND due_at<=?",(s['clock'],)).fetchall():
                self.store.event(db,s,'review.due',{'timer_id':timer['id'],'due_at':timer['due_at']},'worker')
                db.execute("UPDATE review_timers SET status='delivered' WHERE id=?",(timer['id'],))
        job=self.store.claim()
        if job:
            retry=self.process_job(job)
            self.store.finish_job(job['id'],retry)
        self.store.flush_outbox()
        return bool(job)

    def settle(self):
        self.provider.settle_pending()
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET available_at=0 WHERE status='pending'")
        self.tick()
        return self.state()

    def webhook(self, event_id, request_ref):
        with self.store.transaction() as db:
            s=self.store.load(db)
            existing=db.execute('SELECT 1 FROM inbox WHERE provider=? AND event_id=?',('mock',event_id)).fetchone()
            if existing:
                self.store.event(db,s,'provider.duplicate_ignored',{'event_id':event_id},'provider')
                return {'duplicate':True}
            require(any(a['request_ref']==request_ref for p in s['proposals'] for a in p['actions']),'Unknown provider reference.',404)
            db.execute('INSERT INTO inbox VALUES(?,?,?)',('mock',event_id,json.dumps({'request_ref':request_ref})))
            db.execute("UPDATE jobs SET available_at=0 WHERE status='pending'")
            self.store.event(db,s,'provider.event_received',{'event_id':event_id,'request_ref':request_ref},'provider')
        return {'accepted':True}
