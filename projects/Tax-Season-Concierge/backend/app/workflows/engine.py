import copy
import json
import os
import secrets
import time
from datetime import datetime, timezone
from uuid import uuid4
from app.domain.documents import digest, reconcile
from app.domain.scope import check_profile
from app.domain.tax import calculate, load_rules
from app.persistence.store import Conflict
from app.adapters.mock import MockTaxFilingAdapter
from app import seed


def uid(prefix):
    return prefix + '_' + uuid4().hex[:20]


def now():
    return datetime.now(timezone.utc).isoformat()


EDITABLE = {'intake', 'collecting', 'reconciling', 'calculating', 'review_required', 'awaiting_approval', 'rejected', 'out_of_scope'}
ACCEPTED = {'accepted', 'refund_pending', 'refund_verified', 'balance_due_followup', 'no_balance_due'}
ALLOWED = {
    'intake': {'collecting', 'out_of_scope'},
    'collecting': {'reconciling', 'intake', 'out_of_scope'},
    'reconciling': {'calculating', 'collecting', 'intake', 'out_of_scope'},
    'calculating': {'review_required'},
    'review_required': {'awaiting_approval', 'reconciling', 'intake', 'out_of_scope'},
    'awaiting_approval': {'submitted', 'reconciling', 'intake', 'manual_review', 'out_of_scope'},
    'submitted': {'accepted', 'rejected', 'manual_review'},
    'accepted': {'refund_pending', 'balance_due_followup', 'no_balance_due', 'out_of_scope'},
    'refund_pending': {'refund_verified', 'out_of_scope'},
    'refund_verified': {'out_of_scope'},
    'balance_due_followup': {'out_of_scope'},
    'no_balance_due': {'out_of_scope'},
    'rejected': {'reconciling', 'intake', 'out_of_scope'},
    'out_of_scope': {'intake'},
    'manual_review': {'submitted', 'accepted', 'rejected', 'out_of_scope', 'reconciling'},
}


class Engine:
    def __init__(self, store):
        self.store = store
        self.provider = MockTaxFilingAdapter(store)
        if os.getenv('MOCK_PROVIDER_URL'):
            from app.adapters.http_mock import HTTPMockTaxFilingAdapter
            self.provider = HTTPMockTaxFilingAdapter(os.environ['MOCK_PROVIDER_URL'])

    def _event(self, db, case, event_type, message, target=None, actor='customer', evidence=None):
        previous = case['state']
        target = target or previous
        if target != previous and target not in ALLOWED.get(previous, set()):
            raise Conflict(f'Cannot transition from {previous} to {target}')
        case['state'] = target
        case['version'] += 1
        case['updated_at'] = now()
        event = {'id': uid('evt'), 'sequence': case['version'], 'case_id': case['id'], 'type': event_type, 'message': message, 'previous_state': previous, 'next_state': target, 'expected_case_version': case['version'] - 1, 'actor': actor, 'occurred_at': now(), 'environment': 'mock', 'evidence': evidence}
        case['events'].append(event)
        db.execute('INSERT INTO case_events(id,case_id,sequence,data) VALUES (?,?,?,?)', (event['id'], case['id'], event['sequence'], json.dumps(event)))
        db.execute('INSERT INTO outbox(id,case_id,data) VALUES (?,?,?)', (event['id'], case['id'], json.dumps(event)))

    def _case(self, db, case_id, tenant, expected=None):
        case = self.store.get_case(db, case_id, tenant)
        if expected is not None and case['version'] != expected:
            raise Conflict('Case changed. Refresh and review the latest version.')
        return case

    def _invalidate(self, db, case):
        for row in db.execute('SELECT data FROM actions WHERE case_id=?', (case['id'],)).fetchall():
            action = json.loads(row['data'])
            if action['status'] in {'proposed', 'approved'}:
                action['status'] = 'invalidated'
                if action.get('approval'):
                    action['approval']['revoked_at'] = now()
                self.store.save_action(db, action)
                db.execute("UPDATE jobs SET status='cancelled' WHERE action_id=?", (action['id'],))
        if case.get('package'):
            case['package']['invalidated_at'] = now()
            case['package_history'].append(case['package'])
        case['package'], case['calculation'], case['reconciliation'] = None, None, None
        case['completeness_confirmed'] = False

    def _require_editable_action(self, db, case):
        active = db.execute("SELECT id FROM actions WHERE case_id=? AND status='executing'", (case['id'],)).fetchone()
        if active:
            raise Conflict('Submission is in flight. Resolve its outcome before changing the case.')

    def create(self, tenant, request):
        if (request.tax_year, request.jurisdiction, request.filing_status) != (2025, 'US_FEDERAL', 'single'):
            raise ValueError('Only 2025 US federal single-filer cases are supported.')
        case_id = uid('taxcase')
        case = {'id': case_id, 'tenant_id': tenant, 'customer_id': 'cus_demo_10', 'customer_name': 'Alex Morgan', 'taxpayer_ref': 'SYNTHETIC-ALEX-2025', 'tax_year': request.tax_year, 'state': 'intake', 'version': 1, 'scenario': request.scenario, 'clock_day': 0, 'created_at': now(), 'updated_at': now(), 'environment': 'mock', 'profile': {'tax_year': request.tax_year, 'jurisdiction': request.jurisdiction, 'filing_status': request.filing_status, 'answers': {}}, 'eligibility': None, 'forms': [], 'reconciliation': None, 'completeness_confirmed': False, 'calculation': None, 'calculation_history': [], 'package': None, 'package_history': [], 'filing': {'submission': 'not_submitted', 'acceptance': 'not_accepted', 'financial': 'not_started'}, 'events': [], 'tool_runs': []}
        with self.store.transaction() as db:
            db.execute('INSERT INTO cases(id,tenant_id,version,state,data) VALUES (?,?,?,?,?)', (case_id, tenant, 1, 'intake', json.dumps(case)))
            self._event(db, case, 'case.created', '2025 federal tax workspace created with synthetic identity.')
            self.store.save_case(db, case, 1)
        return case

    def get(self, tenant, case_id):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant)
            case['actions'] = [json.loads(r['data']) for r in db.execute('SELECT data FROM actions WHERE case_id=?', (case_id,)).fetchall()]
            return case

    def list(self, tenant):
        with self.store.transaction() as db:
            return [json.loads(r['data']) for r in db.execute('SELECT data FROM cases WHERE tenant_id=? ORDER BY id', (tenant,)).fetchall()]

    def profile_check(self, tenant, case_id, expected, profile):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            self._require_editable_action(db, case)
            if case['state'] not in EDITABLE or case['filing']['acceptance'] == 'accepted':
                raise Conflict('The filed profile is immutable; amendments require specialist review.')
            if profile['tax_year'] != case['tax_year']:
                raise ValueError('The profile tax year must match the immutable case tax year.')
            self._invalidate(db, case)
            if case['state'] != 'intake':
                self._event(db, case, 'profile.changed', 'Previous draft invalidated by profile change.', 'intake')
            case['profile'] = profile
            case['eligibility'] = check_profile(profile)
            target = 'out_of_scope' if case['eligibility']['reasons'] else 'collecting' if case['eligibility']['supported'] else 'intake'
            self._event(db, case, 'profile.checked', 'Profile is supported.' if target == 'collecting' else 'Scope answers need review.', target)
            self.store.save_case(db, case, expected)
        return self.get(tenant, case_id)

    def confirm_form(self, tenant, case_id, form_id, expected):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            self._require_editable_action(db, case)
            if case['state'] not in {'collecting', 'reconciling', 'review_required', 'awaiting_approval'}:
                raise Conflict('Document confirmation is unavailable in this state.')
            form = next((f for f in case['forms'] if f['id'] == form_id), None)
            if not form:
                raise KeyError('Form not found')
            if form['taxpayer_ref'] != case['taxpayer_ref']:
                raise Conflict('Identity conflicts require corrected source evidence and specialist review.')
            if form['confirmed']:
                return self._public(case, None)
            form['original_extraction'] = copy.deepcopy(form)
            form['confirmed'] = True
            form['confirmation_evidence'] = {'actor': tenant, 'confirmed_at': now()}
            self._invalidate(db, case)
            target = 'collecting' if case['state'] == 'collecting' else 'reconciling'
            self._event(db, case, 'document.confirmed', 'Customer confirmed the extracted fields without changing the original document.', target, evidence=form_id)
            self.store.save_case(db, case, expected)
        return self.get(tenant, case_id)

    def add_form(self, tenant, case_id, expected, form):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            self._require_editable_action(db, case)
            if case['state'] in {'submitted', 'manual_review'}:
                raise Conflict('Resolve the pending submission before changing documents.')
            if not case['eligibility'] or not case['eligibility']['supported']:
                raise Conflict('Complete the supported-profile questionnaire before importing records.')
            if form['tax_year'] != 2025:
                raise ValueError('The form year must match tax year 2025.')
            if case['state'] not in EDITABLE | ACCEPTED:
                raise Conflict('Documents cannot be changed in this state.')
            form = {**form, 'id': uid('form'), 'content_hash': digest(form), 'captured_at': now()}
            case['forms'].append(form)
            if case['filing']['acceptance'] == 'accepted':
                case['correction_review'] = {'form_id': form['id'], 'reason': 'New evidence after acceptance requires amended-return review. The original filed package is preserved.'}
                self._event(db, case, 'amendment.review_required', case['correction_review']['reason'], 'out_of_scope', evidence=form['id'])
            else:
                self._invalidate(db, case)
                target = 'reconciling' if case['state'] != 'collecting' else 'collecting'
                self._event(db, case, 'document.received', f"{form['form_type']} from {form['issuer_name']} received; draft approval invalidated.", target, evidence=form['id'])
            self.store.save_case(db, case, expected)
        return self.get(tenant, case_id)

    def reconcile(self, tenant, case_id, expected, complete):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            if case['state'] not in {'collecting', 'reconciling', 'rejected'}:
                raise Conflict('Reconciliation is unavailable in the current state.')
            eligibility = check_profile(case['profile'])
            if not eligibility['supported']:
                raise Conflict('Profile eligibility must be confirmed.')
            result = reconcile(case['forms'], case['profile'], complete)
            case['reconciliation'], case['completeness_confirmed'] = result, complete
            target = 'out_of_scope' if result['exclusions'] else 'reconciling'
            self._event(db, case, 'documents.reconciled', 'Documents reconciled; every amount has source evidence.' if result['ready'] else 'Reconciliation needs attention.', target, evidence={'facts_hash': result['facts_hash'], 'issues': result['issues'], 'exclusions': result['exclusions']})
            self.store.save_case(db, case, expected)
        return self.get(tenant, case_id)

    def calculate(self, tenant, case_id, expected, rule_pack_id, facts_hash):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            if case['state'] != 'reconciling' or not case['reconciliation'] or not case['reconciliation']['ready']:
                raise Conflict('Resolve scope, missing documents and confirmations before calculating.')
            current = reconcile(case['forms'], case['profile'], case['completeness_confirmed'])
            if not check_profile(case['profile'])['supported'] or not current['ready'] or current['facts_hash'] != facts_hash:
                raise Conflict('The source facts changed or are not complete.')
            calculation = calculate(current['facts'], rule_pack_id)
            calculation.update(id=uid('calc'), created_at=now())
            case['calculation'] = calculation
            case['calculation_history'].append(copy.deepcopy(calculation))
            self._event(db, case, 'calculation.started', 'Pinned 2025 rule pack selected.', 'calculating')
            self._event(db, case, 'calculation.completed', 'Federal worksheet calculated using the IRS Tax Table.', 'review_required', actor='rule_engine', evidence=calculation['id'])
            self.store.save_case(db, case, expected)
        return self.get(tenant, case_id)

    def prepare(self, tenant, case_id, expected):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            self._require_editable_action(db, case)
            if case['state'] not in {'review_required', 'awaiting_approval'} or not case['calculation']:
                raise Conflict('A current calculated draft is required.')
            if case['package']:
                prior = self.store.get_action(db, case['package']['action_id'])
                if prior['status'] == 'approved':
                    raise Conflict('The current package is already approved and queued.')
                prior['status'] = 'invalidated'
                self.store.save_action(db, prior)
                case['package']['invalidated_at'] = now()
                case['package_history'].append(case['package'])
            package = {'id': uid('pkg'), 'case_id': case_id, 'environment': 'mock', 'destination': 'Mock Federal Filing Service', 'refund_destination': 'Fixed synthetic account •••• DEMO', 'taxpayer_ref': case['taxpayer_ref'], 'profile': copy.deepcopy(case['profile']), 'documents': copy.deepcopy(case['forms']), 'calculation': copy.deepcopy(case['calculation']), 'scenario': case['scenario'], 'clock_day': case['clock_day'], 'revision': len(case['package_history']) + 1, 'effect': 'Records a simulated original federal return. No IRS submission, bank transfer or payment occurs.'}
            payload_hash = digest(package)
            action = {'id': uid('action'), 'case_id': case_id, 'tenant_id': tenant, 'request_ref': uid('request'), 'payload': package, 'payload_hash': payload_hash, 'status': 'proposed', 'challenge_id': secrets.token_urlsafe(24), 'challenge_expires_at': time.time() + 900, 'approval': None, 'provider_reference': None}
            case['package'] = {'id': package['id'], 'content_hash': payload_hash, 'action_id': action['id'], 'created_at': now(), 'revision': package['revision']}
            db.execute('INSERT INTO actions(id,case_id,tenant_id,request_ref,payload_hash,status,data) VALUES (?,?,?,?,?,?,?)', (action['id'], case_id, tenant, action['request_ref'], payload_hash, 'proposed', json.dumps(action)))
            self._event(db, case, 'package.prepared', 'Exact return package ready for your review and approval.', 'awaiting_approval', evidence=payload_hash)
            self.store.save_case(db, case, expected)
        return self.get(tenant, case_id)

    def approve(self, tenant, action_id, request):
        with self.store.transaction() as db:
            action = self.store.get_action(db, action_id, tenant)
            if request.action_payload_hash != action['payload_hash'] or request.approval_challenge_id != action['challenge_id']:
                raise Conflict('Approval does not match the exact package and challenge.')
            case = self._case(db, action['case_id'], tenant)
            if action['status'] in {'approved', 'executing', 'submitted'} and action.get('approval'):
                return self._public(case, action)
            if case['version'] != request.expected_case_version:
                raise Conflict('Case changed. Refresh and review the latest version.')
            if action['status'] != 'proposed' or case['state'] != 'awaiting_approval' or action['challenge_expires_at'] < time.time():
                raise Conflict('Approval has expired or the draft is no longer current. Prepare a new review package.')
            self._verify_package(case, action)
            expected = case['version']
            action['status'] = 'approved'
            self._event(db, case, 'action.approved', 'You approved this exact package for mock submission.', evidence=action['payload_hash'])
            action['approval'] = {'id': uid('approval'), 'approver': tenant, 'action_hash': action['payload_hash'], 'scope': 'mock_original_return', 'expires_at': time.time() + 900, 'approved_case_version': case['version'], 'consumed_at': None, 'revoked_at': None}
            self.store.save_action(db, action)
            db.execute('INSERT INTO jobs(id,action_id,status) VALUES (?,?,?)', (uid('job'), action_id, 'pending'))
            self.store.save_case(db, case, expected)
        return self.get(tenant, case['id'])

    def _public(self, case, action):
        return {**case, 'actions': [action] if action else []}

    def _verify_package(self, case, action):
        pack, _ = load_rules()
        if digest(action['payload']) != action['payload_hash'] or not case['package'] or case['package']['content_hash'] != action['payload_hash']:
            raise Conflict('The approved package is no longer current.')
        if digest(pack) != action['payload']['calculation']['rule_manifest_hash']:
            raise Conflict('The pinned rules changed; a new calculation and approval are required.')
        result = reconcile(case['forms'], case['profile'], case['completeness_confirmed'])
        if not check_profile(case['profile'])['supported'] or not result['ready'] or result['facts_hash'] != action['payload']['calculation']['facts_hash']:
            raise Conflict('The approved source facts are no longer current.')

    def process_one(self):
        # Claim durably, consume authority, commit; only then contact the adapter.
        with self.store.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE status IN ('pending','running') AND lease_until<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if not job:
                for event in db.execute('SELECT id,case_id,data FROM outbox WHERE delivered=0 LIMIT 100').fetchall():
                    db.execute('INSERT INTO event_deliveries(event_id,case_id,event_hash,delivered_at) VALUES (?,?,?,?) ON CONFLICT(event_id) DO NOTHING', (event['id'], event['case_id'], digest(json.loads(event['data'])), now()))
                    db.execute('UPDATE outbox SET delivered=1 WHERE id=?', (event['id'],))
                return False
            action = self.store.get_action(db, job['action_id'])
            case = self._case(db, action['case_id'], action['tenant_id'])
            if action['status'] in {'invalidated', 'cancelled', 'submitted'}:
                db.execute("UPDATE jobs SET status='done' WHERE id=?", (job['id'],))
                return True
            old = case['version']
            if action['status'] != 'executing':
                approval = action.get('approval')
                try:
                    if not approval or approval['revoked_at'] or approval['expires_at'] < time.time() or approval['approved_case_version'] != case['version'] or approval['action_hash'] != action['payload_hash']:
                        raise Conflict('Approval expired or inputs changed before execution.')
                    self._verify_package(case, action)
                except (Conflict, ValueError) as exc:
                    action['status'] = 'invalidated'
                    self.store.save_action(db, action)
                    db.execute("UPDATE jobs SET status='done' WHERE id=?", (job['id'],))
                    self._event(db, case, 'approval.invalidated', str(exc), 'reconciling', actor='worker')
                    case['completeness_confirmed'] = False
                    self.store.save_case(db, case, old)
                    return True
                approval['consumed_at'] = now()
                action['status'] = 'executing'
                self.store.save_action(db, action)
            db.execute("UPDATE jobs SET status='running',lease_until=?,attempts=attempts+1 WHERE id=?", (time.time()+30, job['id']))
        try:
            result = self.provider.find_submission(action['request_ref'])
            if result is None:
                result = self.provider.submit_mock_return(action['payload'], action['request_ref'])
            if not isinstance(result, dict) or result.get('environment') != 'mock' or not result.get('submission_ref') or result.get('package_hash') != action['payload_hash']:
                raise ValueError('Malformed provider response; outcome must be looked up before retrying.')
        except (TimeoutError, ValueError) as exc:
            with self.store.transaction() as db:
                case = self._case(db, action['case_id'], action['tenant_id'])
                old = case['version']
                case['filing']['submission'] = 'outcome_unknown'
                self._event(db, case, 'submission.uncertain', str(exc), 'manual_review', actor='worker', evidence=action['request_ref'])
                if job['attempts'] >= 3:
                    db.execute("UPDATE jobs SET status='held',lease_until=0 WHERE id=?", (job['id'],))
                else:
                    db.execute("UPDATE jobs SET status='pending',lease_until=? WHERE id=?", (time.time()+min(2 ** job['attempts'], 8), job['id']))
                self.store.save_case(db, case, old)
            return True
        with self.store.transaction() as db:
            action = self.store.get_action(db, action['id'])
            case = self._case(db, action['case_id'], action['tenant_id'])
            old = case['version']
            action.update(status='submitted', provider_reference=result['submission_ref'])
            self.store.save_action(db, action)
            db.execute("UPDATE jobs SET status='done',lease_until=0 WHERE id=?", (job['id'],))
            case['filing'].update(submission='received', submission_ref=result['submission_ref'], acceptance='pending', financial='not_started')
            self._event(db, case, 'return.submitted', 'Mock provider acknowledged receipt; acceptance is still separate.', 'submitted', actor='worker', evidence=result['submission_ref'])
            self.store.save_case(db, case, old)
        if result['status'] == 'accepted':
            self.apply_event({'id': result['submission_ref']+':accepted', 'case_id': case['id'], 'submission_ref': result['submission_ref'], 'type': 'accepted', 'environment': 'mock', 'amount_minor': result['refund_minor'], 'account_event_ref': None, 'code': 'MOCK-ACCEPTED'})
        return True

    def apply_event(self, event):
        if event['environment'] != 'mock':
            raise ValueError('Only mock provider events are accepted')
        with self.store.transaction() as db:
            prior = db.execute('SELECT payload_hash FROM event_inbox WHERE id=?', (event['id'],)).fetchone()
            if prior:
                if prior['payload_hash'] != digest(event):
                    raise Conflict('Provider event ID reused with different content.')
                return {'duplicate': True}
            case = self.store.get_case(db, event['case_id'])
            old = case['version']
            if case['filing'].get('submission_ref') != event['submission_ref']:
                raise Conflict('Event does not match the current submission.')
            kind = event['type']
            db.execute('INSERT INTO event_inbox(id,payload_hash,data) VALUES (?,?,?)', (event['id'], digest(event), json.dumps(event)))
            if kind in {'accepted', 'rejected'}:
                if case['filing']['acceptance'] == kind:
                    return {'duplicate': True}
                if case['state'] not in {'submitted', 'manual_review'}:
                    raise Conflict('Filing decision is out of order or contradicts a final decision.')
                case['filing']['acceptance'] = kind
                case['filing']['acceptance_code'] = event.get('code')
                case['filing']['acceptance_event_ref'] = event['id']
                self._event(db, case, 'return.'+kind, 'Provider accepted the simulated return.' if kind == 'accepted' else 'Mock identity rejection. Review identity evidence before preparing another draft.', kind, actor='mock_provider', evidence=event['id'])
                if kind == 'accepted':
                    calc = case['calculation']
                    financial = 'refund_pending' if calc['refund_minor'] else 'balance_due_followup' if calc['amount_due_minor'] else 'no_balance_due'
                    case['filing']['financial'] = financial
                    self._event(db, case, 'outcome.pending', 'Acceptance does not verify a refund or payment.', financial, actor='mock_provider')
            elif kind in {'refund_notice', 'account_credit'}:
                if case['filing']['acceptance'] != 'accepted' or not case['calculation']['refund_minor']:
                    raise Conflict('A refund event requires an accepted return with an expected refund.')
                if event['amount_minor'] != case['calculation']['refund_minor']:
                    raise Conflict('Refund amount does not match the approved return.')
                if kind == 'refund_notice':
                    case['filing']['refund_notice_ref'] = event['id']
                    self._event(db, case, 'refund.notice', 'Simulated refund notice received; account credit is still unverified.', actor='mock_provider', evidence=event['id'])
                else:
                    if not event.get('account_event_ref'):
                        raise ValueError('A verified refund requires a matched account credit reference.')
                    if case['filing']['financial'] == 'refund_verified':
                        if case['filing']['account_event_ref'] != event['account_event_ref']:
                            raise Conflict('A different account event cannot replace the verified financial outcome.')
                        return {'duplicate': True}
                    existing_credit = db.execute('SELECT case_id FROM account_credits WHERE reference=?', (event['account_event_ref'],)).fetchone()
                    if existing_credit:
                        raise Conflict('An account credit cannot verify more than one return.')
                    db.execute('INSERT INTO account_credits(reference,case_id,amount_minor) VALUES (?,?,?)', (event['account_event_ref'], case['id'], event['amount_minor']))
                    case['filing'].update(account_event_ref=event['account_event_ref'], verified_amount_minor=event['amount_minor'], verified_at=now(), financial='refund_verified')
                    self._event(db, case, 'refund.verified', 'Separate simulated account credit matched the approved refund.', 'refund_verified' if not case.get('correction_review') else None, actor='mock_provider', evidence=event['account_event_ref'])
            self.store.save_case(db, case, old)
        return {'duplicate': False}

    def advance(self, tenant, case_id, expected, days):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            case['clock_day'] += days
            self._event(db, case, 'clock.advanced', f'Simulation advanced by {days} day(s).', actor='demo_operator')
            self.store.save_case(db, case, expected)
            action_rows = db.execute("SELECT data FROM actions WHERE case_id=? AND status='submitted'", (case_id,)).fetchall()
        for row in action_rows:
            action = json.loads(row['data'])
            submission = self.provider.find_submission(action['request_ref'])
            if submission and submission['submission_ref'] == case['filing'].get('submission_ref'):
                for event in self.provider.events(submission, case['clock_day']):
                    self.apply_event(event)
        return self.get(tenant, case_id)

    def resolve_rejection(self, tenant, case_id, expected):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            if case['state'] != 'rejected':
                raise Conflict('There is no rejection to review.')
            self._invalidate(db, case)
            case['filing_history'] = case.get('filing_history', []) + [copy.deepcopy(case['filing'])]
            case['filing'] = {'submission': 'not_submitted', 'acceptance': 'not_accepted', 'financial': 'not_started'}
            self._event(db, case, 'rejection.reviewed', 'Synthetic identity evidence confirmed. A new draft and approval are required.', 'reconciling', evidence='SYNTHETIC-ALEX-2025')
            self.store.save_case(db, case, expected)
        return self.get(tenant, case_id)

    def replay(self, tenant, case_id, expected):
        with self.store.transaction() as db:
            case = self._case(db, case_id, tenant, expected)
            if case['state'] != 'manual_review':
                raise Conflict('Only an uncertain submission can be replayed.')
            actions = db.execute("SELECT id FROM actions WHERE case_id=? AND status='executing'", (case_id,)).fetchall()
            for action in actions:
                db.execute("UPDATE jobs SET status='pending',lease_until=0 WHERE action_id=? AND status='held'", (action['id'],))
            self._event(db, case, 'operator.replay', 'Requeued status reconciliation using the original request reference and consumed approval.', actor='demo_operator')
            self.store.save_case(db, case, expected)
        return self.get(tenant, case_id)

    def metrics(self, tenant):
        with self.store.transaction() as db:
            result = {'pending_jobs': 0, 'held_jobs': 0, 'undelivered_events': 0, 'delivered_events': 0, 'invalidated_actions': 0}
            for row in db.execute('SELECT j.status FROM jobs j JOIN actions a ON j.action_id=a.id WHERE a.tenant_id=?', (tenant,)).fetchall():
                if row['status'] in {'running', 'pending'}: result['pending_jobs'] += 1
                if row['status'] == 'held': result['held_jobs'] += 1
            result['undelivered_events'] = db.execute('SELECT COUNT(*) AS n FROM outbox o JOIN cases c ON o.case_id=c.id WHERE c.tenant_id=? AND o.delivered=0', (tenant,)).fetchone()['n']
            result['delivered_events'] = db.execute('SELECT COUNT(*) AS n FROM event_deliveries e JOIN cases c ON e.case_id=c.id WHERE c.tenant_id=?', (tenant,)).fetchone()['n']
            result['invalidated_actions'] = db.execute("SELECT COUNT(*) AS n FROM actions WHERE tenant_id=? AND status='invalidated'", (tenant,)).fetchone()['n']
            return result

    def seed_forms(self, tenant, case_id, expected):
        case = self.get(tenant, case_id)
        if case['version'] != expected:
            raise Conflict('Case changed. Refresh first.')
        if case['forms']:
            raise Conflict('Use document intake for additional or corrected records.')
        records = seed.forms(case['scenario'])
        if case['scenario'] == 'missing_interest':
            records = records[:2]
        for record in records:
            case = self.add_form(tenant, case_id, case['version'], record)
        return case

    def assist(self, tenant, case_id, expected, budget=8):
        case = self.get(tenant, case_id)
        if case['version'] != expected:
            raise Conflict('Case changed. Refresh first.')
        calls = []
        for _ in range(min(budget, 8)):
            if case['state'] == 'intake':
                message = 'Confirm the scope questionnaire before importing tax records.'
                break
            if case['state'] in {'collecting', 'reconciling'}:
                if not case['completeness_confirmed']:
                    message = 'Review the documents and confirm that all income and withholding are present.'
                    break
                if not case['reconciliation'] or not case['reconciliation']['ready']:
                    case = self.reconcile(tenant, case_id, case['version'], True)
                    calls.append('reconcile_tax_documents')
                    if not case['reconciliation']['ready']:
                        message = 'Resolve the listed evidence gaps before calculation.'
                        break
                else:
                    case = self.calculate(tenant, case_id, case['version'], load_rules()[0]['id'], case['reconciliation']['facts_hash'])
                    calls.append('calculate_federal_return')
            elif case['state'] == 'review_required':
                case = self.prepare(tenant, case_id, case['version'])
                calls.append('prepare_return_package')
            else:
                message = 'Review and approve the exact draft.' if case['state'] == 'awaiting_approval' else 'The next step is shown in your case status.'
                break
        else:
            message = 'Tool budget reached. Resume after reviewing the evidence.'
        with self.store.transaction() as db:
            fresh = self._case(db, case_id, tenant)
            old = fresh['version']
            run = {'id': uid('run'), 'tools': calls, 'source': 'deterministic rules-only orchestrator', 'retrieved_at': now(), 'authority': 'simulated', 'model': None, 'prompt_version': 'rules-only-v1', 'cost_usd': 0, 'message': message}
            fresh['tool_runs'].append(run)
            self._event(db, fresh, 'agent.completed', message, actor='concierge', evidence=run['id'])
            self.store.save_case(db, fresh, old)
        return self.get(tenant, case_id)
