import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select, func
from ..persistence import db as d
from ..domain.lifecycle import require, ALLOWED, TERMINAL, DomainError
from ..domain.quotes import PROVIDERS, normalize, compare
from ..adapters.mock import MockProvider


def uid(prefix):
    return prefix + '_' + uuid4().hex[:16]


def digest(value):
    return 'sha256:' + hashlib.sha256(d.pack(value).encode()).hexdigest()


class Concierge:
    def __init__(self, database):
        self.db = database
        self.provider = MockProvider(database, self.now)
        self.seed()

    def seed(self):
        with self.db.tx() as conn:
            if d.get(conn, d.settings, 'clock'):
                return
            d.put(conn, d.settings, {'id': 'clock', 'now': '2026-09-26T12:00:00+00:00', 'frozen': True})
            for identifier, owner, name, relation, bank, tail in [
                ('beneficiary_demo_1', 'cus_demo_8', 'Ananya Sharma', 'Family', 'HDFC Bank', '4821'),
                ('beneficiary_demo_2', 'cus_demo_8', 'Rohan Sharma', 'Brother', 'ICICI Bank', '0916'),
                ('beneficiary_other', 'cus_other', 'Other customer recipient', 'Family', 'HDFC Bank', '0000')]:
                d.put(conn, d.beneficiaries, {'id': identifier, 'customer_id': owner, 'name': name, 'relationship': relation,
                    'country': 'IN', 'city': 'Mumbai, India', 'payout_type': 'bank_account', 'bank': bank,
                    'account_mask': '•••• ' + tail, 'ifsc_mask': 'HDFC•••0123' if bank == 'HDFC Bank' else 'ICIC•••0421',
                    'verification_status': 'verified', 'version': 1, 'environment': 'mock'})
            d.put(conn, d.documents, {'id': 'doc_purpose_demo', 'customer_id': 'cus_demo_8', 'name': 'Family support declaration',
                'fields': ['purpose_supporting_document'], 'content_hash': digest({'fixture': 'Family support declaration v1'}),
                'source': 'synthetic_fixture', 'captured_at': self.now(conn).isoformat(), 'extraction_version': '1',
                'content': 'SYNTHETIC: funds are intended for household expenses. No identity document is attached.'})

    def now(self, conn):
        return datetime.fromisoformat(d.get(conn, d.settings, 'clock')['now'])

    def clock(self):
        with self.db.tx() as conn:
            return self.now(conn)

    def owned(self, conn, table, identifier, user):
        value = d.get(conn, table, identifier)
        require(value is not None, 'Record not found', 404)
        owner = value.get('customer_id')
        if owner is None and value.get('case_id'):
            owner = d.get(conn, d.cases, value['case_id'])['customer_id']
        require(owner == user['customer_id'], 'Record not found', 404)
        return value

    def check_version(self, case, version):
        require(case['version'] == version, 'This case changed. Refresh and review it again.')

    def record(self, conn, case, kind, message, actor='system', next_state=None, source_event_id=None, evidence=None):
        previous = case['status']
        if next_state is not None and next_state != previous:
            require(next_state in ALLOWED[previous], 'Invalid transition: ' + previous + ' → ' + next_state)
            case['status'] = next_state
        case['version'] += 1
        case['updated_at'] = self.now(conn).isoformat()
        evt = {'id': uid('evt'), 'case_id': case['id'], 'sequence': case['version'], 'expected_case_version': case['version'] - 1,
               'type': kind, 'message': message, 'previous_state': previous, 'next_state': case['status'], 'actor': actor,
               'source_event_id': source_event_id, 'timestamp': case['updated_at'], 'environment': 'mock', 'evidence': evidence}
        d.put(conn, d.events, evt)
        d.put(conn, d.cases, case)
        # Transactional notification outbox, consumed locally by the worker.
        self.enqueue(conn, case['id'], 'notify', {'event_id': evt['id']}, self.now(conn), key='notify_' + evt['id'])
        return evt

    def enqueue(self, conn, case_id, kind, payload, due_at, key=None):
        key = key or uid('job')
        if d.get(conn, d.jobs, key):
            return
        d.put(conn, d.jobs, {'id': key, 'case_id': case_id, 'kind': kind, 'payload': payload, 'status': 'pending',
                           'due_at': due_at.isoformat(), 'lease_until': None, 'attempts': 0, 'last_error': None})

    def create_case(self, user, intent):
        value = intent.model_dump(mode='json') if hasattr(intent, 'model_dump') else dict(intent)
        value['deadline_at'] = value['deadline_at'].replace('Z', '+00:00')
        require(value['customer_id'] == user['customer_id'], 'Customer does not match the authenticated session', 403)
        with self.db.tx() as conn:
            recipient = self.owned(conn, d.beneficiaries, value['beneficiary_id'], user)
            require(recipient['verification_status'] == 'verified', 'Recipient must be verified')
            require(datetime.fromisoformat(value['deadline_at']) > self.now(conn), 'Choose a future deadline', 422)
            value.update(id=uid('case'), version=0, status='collecting', created_at=self.now(conn).isoformat(),
                         updated_at=self.now(conn).isoformat(), quote_ids=[], environment='mock', deadline_risk=False)
            self.record(conn, value, 'case.created', 'Transfer intent saved.', actor=user['id'])
            return value

    def get_quotes(self, user, case_id):
        with self.db.tx() as conn:
            case = self.owned(conn, d.cases, case_id, user)
            require(case['status'] in {'collecting', 'awaiting_approval'}, 'An active transfer cannot be requoted')
            for action in d.rows(conn, d.actions, d.actions.c.case_id == case_id):
                if action['status'] == 'draft':
                    action['status'] = 'invalidated'
                    d.put(conn, d.actions, action)
            prepared = d.rows(conn, d.transfers, d.transfers.c.case_id == case_id)
            if prepared:
                require(prepared[0]['status'] == 'prepared', 'An existing transfer blocks a replacement')
                conn.execute(d.transfers.delete().where(d.transfers.c.id == prepared[0]['id']))
            self.record(conn, case, 'quotes.requested', 'Comparing three simulated providers.', next_state='quoting')
            values = [normalize(p, case, self.now(conn)) for p in PROVIDERS.values()]
            for value in values:
                d.put(conn, d.quotes, value)
            case['quote_ids'] = [q['id'] for q in values]
            self.record(conn, case, 'quotes.received', 'Three quotes captured at the same fixture time.', next_state='awaiting_approval')
            return compare(values, case, self.now(conn))

    def snapshot(self, user, case_id):
        with self.db.tx() as conn:
            case = self.owned(conn, d.cases, case_id, user)
            case['recipient'] = self.owned(conn, d.beneficiaries, case['beneficiary_id'], user)
            case['comparison'] = compare([d.get(conn, d.quotes, q) for q in case['quote_ids']], case, self.now(conn))
            ts = d.rows(conn, d.transfers, d.transfers.c.case_id == case_id)
            case['transfer'] = ts[0] if ts else None
            case['actions'] = d.rows(conn, d.actions, d.actions.c.case_id == case_id)
            case['events'] = sorted(d.rows(conn, d.events, d.events.c.case_id == case_id), key=lambda e: e['sequence'])
            case['requirements'] = d.rows(conn, d.requirements, d.requirements.c.transfer_id == ts[0]['id']) if ts else []
            receipts = d.rows(conn, d.receipts, d.receipts.c.transfer_id == ts[0]['id']) if ts else []
            case['receipt'] = receipts[0] if receipts else None
            case['clock'] = self.now(conn).isoformat()
            case['deadline_risk'] = case['status'] not in TERMINAL and self.now(conn) >= datetime.fromisoformat(case['deadline_at'])
            return case

    def idempotent(self, conn, user, key, fingerprint):
        require(key is not None and 8 <= len(key) <= 128, 'An Idempotency-Key of 8–128 characters is required', 422)
        found = d.rows(conn, d.actions, d.actions.c.customer_id == user['customer_id'], d.actions.c.idem_key == key)
        if found:
            require(found[0]['request_hash'] == fingerprint, 'Idempotency key was reused with different content')
            return found[0]

    def action(self, conn, case, user, kind, payload, key, fingerprint):
        for old in d.rows(conn, d.actions, d.actions.c.case_id == case['id']):
            if old['status'] == 'draft':
                old['status'] = 'invalidated'
                d.put(conn, d.actions, old)
        self.record(conn, case, kind + '.prepared', 'Exact ' + kind.replace('_', ' ') + ' details prepared for review.', user['id'])
        action = {'id': uid('action'), 'case_id': case['id'], 'customer_id': user['customer_id'], 'type': kind,
                  'payload': payload, 'payload_hash': digest(payload), 'status': 'draft', 'idem_key': key, 'request_hash': fingerprint,
                  'challenge_id': secrets.token_urlsafe(24), 'challenge_session_id': user['id'],
                  'expires_at': (self.now(conn) + timedelta(minutes=10)).isoformat(), 'expected_case_version': case['version'],
                  'created_at': self.now(conn).isoformat(), 'approval_id': None, 'request_ref': uid('request')}
        return d.put(conn, d.actions, action)

    def prepare_transfer(self, user, case_id, body, key):
        data = body.model_dump() if hasattr(body, 'model_dump') else body
        fingerprint = digest({'case_id': case_id, 'type': 'initiate', 'body': data})
        with self.db.tx() as conn:
            case = self.owned(conn, d.cases, case_id, user)
            prior = self.idempotent(conn, user, key, fingerprint)
            if prior:
                return prior
            self.check_version(case, data['expected_case_version'])
            require(case['status'] == 'awaiting_approval', 'This case already has an active or completed transfer')
            recipient = self.owned(conn, d.beneficiaries, case['beneficiary_id'], user)
            require(data['recipient_confirmed'] and recipient['version'] == data['recipient_version'], 'Confirm the current recipient details')
            require(recipient['verification_status'] == 'verified', 'Recipient is not verified')
            for other in d.rows(conn, d.cases, d.cases.c.customer_id == user['customer_id']):
                require(other['id'] == case_id or other['beneficiary_id'] != case['beneficiary_id'] or
                        other['status'] in {'collecting', 'quoting', 'awaiting_approval', 'reconciled', 'cancelled', 'rejected'},
                        'An unresolved transfer to this recipient blocks a replacement. Resolve the original first.')
            require(data['quote_id'] in case['quote_ids'], 'Quote does not belong to this comparison')
            quote = d.get(conn, d.quotes, data['quote_id'])
            require(compare([quote], case, self.now(conn))['quotes'][0]['eligible'], 'This quote is expired or does not meet the transfer requirements')
            existing = d.rows(conn, d.transfers, d.transfers.c.case_id == case_id)
            require(not existing or existing[0]['status'] == 'prepared', 'A replacement transfer is blocked')
            transfer_id = existing[0]['id'] if existing else uid('transfer')
            payload = {'transfer_id': transfer_id, 'provider_id': quote['provider_id'], 'provider_name': quote['provider_name'],
                       'quote': quote, 'recipient': recipient, 'beneficiary_version': recipient['version'], 'purpose': case['purpose'],
                       'deadline_at': case['deadline_at'], 'effect': 'Initiate one simulated bank transfer. Cancellation is not guaranteed.'}
            action = self.action(conn, case, user, 'initiate', payload, key, fingerprint)
            d.put(conn, d.transfers, {'id': transfer_id, 'case_id': case_id, 'request_ref': action['request_ref'], 'provider_id': quote['provider_id'],
                   'quote_id': quote['id'], 'quote': quote, 'beneficiary_version': recipient['version'], 'approval_id': None, 'status': 'prepared',
                   'scenario': PROVIDERS[quote['provider_id']]['default_scenario'], 'provider_transfer_ref': None, 'version': 1, 'environment': 'mock'})
            return action

    def prepare_followup(self, user, transfer_id, kind, body, key):
        data = body.model_dump() if hasattr(body, 'model_dump') else body
        fingerprint = digest({'transfer_id': transfer_id, 'type': kind, 'body': data})
        with self.db.tx() as conn:
            transfer = self.owned(conn, d.transfers, transfer_id, user)
            case = self.owned(conn, d.cases, transfer['case_id'], user)
            prior = self.idempotent(conn, user, key, fingerprint)
            if prior:
                return prior
            self.check_version(case, data['expected_case_version'])
            require(transfer['provider_transfer_ref'] is not None, 'Resolve the original provider outcome first')
            payload = {'transfer_id': transfer_id, 'provider_id': transfer['provider_id'], 'provider_name': PROVIDERS[transfer['provider_id']]['name'],
                       'provider_transfer_ref': transfer['provider_transfer_ref'], 'recipient': d.get(conn, d.beneficiaries, case['beneficiary_id']),
                       'quote': transfer['quote']}
            if kind == 'share_documents':
                require(case['status'] == 'information_required', 'No document request is currently active')
                req = d.get(conn, d.requirements, data['requirement_id'])
                require(req is not None and req['transfer_id'] == transfer_id and req['status'] == 'open', 'Document requirement not found', 404)
                docs = [self.owned(conn, d.documents, identifier, user) for identifier in data['document_ids']]
                require(len({v['id'] for v in docs}) == len(docs), 'Duplicate document selection')
                fields = {f for doc in docs for f in doc['fields']}
                require(fields == set(req['requested_fields']), 'Share only the exact fields requested by this provider')
                manifest = [{'id': doc['id'], 'name': doc['name'], 'content_hash': doc['content_hash'], 'fields': doc['fields']} for doc in docs]
                payload.update(requirement_id=req['id'], requested_fields=req['requested_fields'], documents=manifest,
                               manifest_hash=digest(manifest), effect='Share only the listed synthetic documents with this provider.')
            else:
                require(case['status'] in {'funding_pending', 'processing', 'information_required', 'payout_pending', 'delayed', 'manual_review'}, 'Cancellation is unavailable in this state')
                payload.update(effect='Request cancellation. The transfer stays active until the provider confirms. A refund is tracked separately.')
            return self.action(conn, case, user, kind, payload, key, fingerprint)

    def authority(self, conn, action, case):
        require(digest(action['payload']) == action['payload_hash'], 'The approved payload changed')
        require(self.now(conn) < datetime.fromisoformat(action['expires_at']), 'Approval expired; review a new proposal')
        payload = action['payload']
        if action['type'] == 'initiate':
            quote = payload['quote']
            for other in d.rows(conn, d.cases, d.cases.c.customer_id == case['customer_id']):
                require(other['id'] == case['id'] or other['beneficiary_id'] != case['beneficiary_id'] or
                        other['status'] in {'collecting', 'quoting', 'awaiting_approval', 'reconciled', 'cancelled', 'rejected'},
                        'Another unresolved transfer to this recipient blocks initiation.')
            require(self.now(conn) < datetime.fromisoformat(quote['valid_until']), 'Quote expired; refresh and approve again')
            recipient = d.get(conn, d.beneficiaries, case['beneficiary_id'])
            require(recipient['version'] == payload['beneficiary_version'] and recipient['verification_status'] == 'verified', 'Recipient changed; review and approve again')
            require(case['budget_mode'] != 'total_sender_cost' or quote['source_total_minor'] <= case['source_budget_minor'], 'Sender budget would be exceeded')
        elif action['type'] == 'share_documents':
            req = d.get(conn, d.requirements, payload['requirement_id'])
            require(req and req['status'] == 'open', 'Document request is no longer open')
            for doc in payload['documents']:
                current = d.get(conn, d.documents, doc['id'])
                require(current and current['customer_id'] == case['customer_id'] and current['content_hash'] == doc['content_hash'], 'Document content or ownership changed')

    def approve(self, user, action_id, body):
        require(user['role'] == 'customer', 'Only the authenticated customer can approve', 403)
        data = body.model_dump() if hasattr(body, 'model_dump') else body
        with self.db.tx() as conn:
            action = self.owned(conn, d.actions, action_id, user)
            require(action['payload_hash'] == data['action_payload_hash'], 'The action payload changed')
            require(action['challenge_id'] == data['approval_challenge_id'] and action['challenge_session_id'] == user['id'], 'Approval challenge is invalid for this session', 403)
            if action['status'] in {'queued', 'executing', 'completed', 'outcome_unknown'}:
                return action
            require(action['status'] == 'draft', 'This proposal is no longer available')
            case = self.owned(conn, d.cases, action['case_id'], user)
            self.check_version(case, data['expected_case_version'])
            require(action['expected_case_version'] == case['version'], 'Proposal is stale; review again')
            self.authority(conn, action, case)
            state = 'initiating' if action['type'] == 'initiate' else 'cancellation_requested' if action['type'] == 'cancel' else None
            self.record(conn, case, 'action.approved', 'Customer approved the exact ' + action['type'].replace('_', ' ') + ' action.', user['id'], next_state=state)
            approval = {'id': uid('approval'), 'action_id': action_id, 'approver_id': user['id'], 'customer_id': user['customer_id'],
                        'action_hash': action['payload_hash'], 'scope': action['type'], 'expires_at': action['expires_at'], 'revoked_at': None,
                        'consumed_at': None, 'created_at': self.now(conn).isoformat()}
            d.put(conn, d.approvals, approval)
            action.update(status='queued', approval_id=approval['id'], approved_case_version=case['version'])
            d.put(conn, d.actions, action)
            self.enqueue(conn, case['id'], 'execute', {'action_id': action_id}, self.now(conn), 'execute_' + action_id)
            return action

    def revoke(self, user, action_id):
        with self.db.tx() as conn:
            action = self.owned(conn, d.actions, action_id, user)
            require(action['status'] in {'draft', 'queued'}, 'Action has already been dispatched')
            approval = d.get(conn, d.approvals, action['approval_id']) if action['approval_id'] else None
            if approval:
                require(approval['consumed_at'] is None, 'Approval was already consumed')
                approval['revoked_at'] = self.now(conn).isoformat()
                d.put(conn, d.approvals, approval)
            action['status'] = 'revoked'
            d.put(conn, d.actions, action)
            case = d.get(conn, d.cases, action['case_id'])
            # Dispatch has not occurred; do not leave a fictitious financial action in flight.
            if action['type'] == 'initiate' and case['status'] == 'initiating':
                case['status'] = 'awaiting_approval'
            elif action['type'] == 'cancel' and case['status'] == 'cancellation_requested':
                case['status'] = d.get(conn, d.transfers, action['payload']['transfer_id'])['status']
            self.record(conn, case, 'approval.revoked', 'Approval revoked before dispatch.', user['id'])
            return action

    def advance(self, minutes):
        with self.db.tx() as conn:
            value = self.now(conn) + timedelta(minutes=minutes)
            d.put(conn, d.settings, {'id': 'clock', 'now': value.isoformat(), 'frozen': True})
        self.drain()
        return {'now': value.isoformat(), 'environment': 'mock'}

    def claim_job(self):
        with self.db.tx() as conn:
            candidates = d.rows(conn, d.jobs, d.jobs.c.status.in_(['pending', 'running']))
            candidates.sort(key=lambda x: (x['due_at'], x['id']))
            wall_now = datetime.now(timezone.utc)
            for job in candidates:
                if datetime.fromisoformat(job['due_at']) > self.now(conn):
                    continue
                if job['status'] == 'running' and job['lease_until'] and datetime.fromisoformat(job['lease_until']) > wall_now:
                    continue
                job.update(status='running', lease_until=(wall_now + timedelta(seconds=30)).isoformat(), attempts=job['attempts'] + 1)
                d.put(conn, d.jobs, job)
                return job

    def drain(self, limit=200):
        count = 0
        while count < limit:
            job = self.claim_job()
            if job is None:
                break
            count += 1
            try:
                if job['kind'] == 'execute':
                    self.execute_action(job['payload']['action_id'])
                elif job['kind'] == 'poll':
                    self.poll(job['payload']['transfer_id'])
                elif job['kind'] == 'refund':
                    self.refund(job['payload']['transfer_id'])
                with self.db.tx() as conn:
                    job.update(status='done', lease_until=None)
                    d.put(conn, d.jobs, job)
            except Exception as exc:
                with self.db.tx() as conn:
                    job.update(status='failed' if job['attempts'] >= 4 else 'pending', lease_until=None,
                               due_at=(self.now(conn) + timedelta(minutes=min(2 ** job['attempts'], 16))).isoformat(),
                               last_error=type(exc).__name__ + ': ' + str(exc)[:180])
                    d.put(conn, d.jobs, job)
                    if job['status'] == 'failed':
                        case = d.get(conn, d.cases, job['case_id'])
                        self.record(conn, case, 'worker.failed', 'Worker could not resolve the provider outcome. Operator review required.',
                                    next_state='manual_review' if 'manual_review' in ALLOWED[case['status']] else None)
        return count

    def execute_action(self, action_id):
        # A lease prevents concurrent dispatch. Provider writes have their own stable idempotency key.
        with self.db.tx() as conn:
            action = d.get(conn, d.actions, action_id)
            if action['status'] in {'completed', 'invalidated', 'revoked', 'failed', 'outcome_unknown'}:
                return
            case = d.get(conn, d.cases, action['case_id'])
            approval = d.get(conn, d.approvals, action['approval_id'])
            require(approval and approval['revoked_at'] is None and approval['action_hash'] == action['payload_hash'], 'No valid authority exists', 403)
            if approval['consumed_at'] is None:
                try:
                    self.authority(conn, action, case)
                except DomainError as exc:
                    action['status'] = 'invalidated'
                    d.put(conn, d.actions, action)
                    if action['type'] == 'initiate':
                        case['status'] = 'awaiting_approval'
                    elif action['type'] == 'cancel':
                        case['status'] = d.get(conn, d.transfers, action['payload']['transfer_id'])['status']
                    self.record(conn, case, 'action.invalidated', exc.message)
                    return
                approval['consumed_at'] = self.now(conn).isoformat()
                d.put(conn, d.approvals, approval)
            action['status'] = 'executing'
            d.put(conn, d.actions, action)
            transfer = d.get(conn, d.transfers, action['payload']['transfer_id'])
        try:
            if action['type'] == 'initiate':
                result = self.provider.find_transfer(action['request_ref'])
            else:
                result = self.provider.lookup_operation(action['request_ref'])
            if result is None:
                # On recovery, expired authority cannot cause a previously unattempted write.
                require(self.clock() < datetime.fromisoformat(action['expires_at']), 'Authority expired before provider dispatch')
                if action['type'] == 'initiate':
                    result = self.provider.initiate(transfer, action['request_ref'])
                elif action['type'] == 'share_documents':
                    result = self.provider.provide_documents(transfer['provider_transfer_ref'], action['payload'], action['request_ref'])
                else:
                    result = self.provider.request_cancellation(transfer['provider_transfer_ref'], action['request_ref'])
        except TimeoutError:
            with self.db.tx() as conn:
                action = d.get(conn, d.actions, action_id)
                case = d.get(conn, d.cases, action['case_id'])
                action['status'] = 'outcome_unknown'
                d.put(conn, d.actions, action)
                transfer['status'] = 'outcome_unknown'
                d.put(conn, d.transfers, transfer)
                self.record(conn, case, 'provider.timeout', 'Provider response was lost. Looking up the original reference; no replacement will be initiated.', next_state='outcome_unknown')
                self.enqueue(conn, case['id'], 'poll', {'transfer_id': transfer['id']}, self.now(conn) + timedelta(minutes=1))
            return
        with self.db.tx() as conn:
            action = d.get(conn, d.actions, action_id)
            case = d.get(conn, d.cases, action['case_id'])
            transfer = d.get(conn, d.transfers, transfer['id'])
            action.update(status='completed', provider_result=result)
            d.put(conn, d.actions, action)
            if action['type'] == 'initiate':
                transfer.update(provider_transfer_ref=result['id'], status=result['status'], approval_id=action['approval_id'])
                d.put(conn, d.transfers, transfer)
                self.record(conn, case, 'provider.accepted', 'Provider reference recorded. Recipient delivery is not yet verified.' if result['status'] != 'rejected' else 'Provider rejected the transfer.', next_state=result['status'], evidence={'provider_reference': result['id']})
            elif action['type'] == 'share_documents':
                req = d.get(conn, d.requirements, action['payload']['requirement_id'])
                req.update(status='satisfied', satisfied_by_manifest_id=action['payload']['manifest_hash'])
                d.put(conn, d.requirements, req)
                self.record(conn, case, 'documents.shared', 'Approved purpose evidence accepted by the provider.', next_state='processing')
                transfer['status'] = 'processing'
                d.put(conn, d.transfers, transfer)
            else:
                cancelled = result['status'] == 'cancelled'
                transfer['refund_status'] = result['refund_status']
                resume = case['status'] if case['status'] != 'cancellation_requested' else transfer['status']
                if case['status'] == 'cancellation_requested' and resume not in ALLOWED['cancellation_requested']:
                    resume = 'processing'
                self.record(conn, case, 'cancellation.' + result['status'], 'Cancellation confirmed. Source refund is pending.' if cancelled else 'Cancellation was declined. The original transfer remains active.', next_state='cancelled' if cancelled else resume, evidence=result)
                transfer['status'] = case['status']
                d.put(conn, d.transfers, transfer)
                if cancelled:
                    self.enqueue(conn, case['id'], 'refund', {'transfer_id': transfer['id']}, self.now(conn) + timedelta(hours=24))
            if case['status'] not in TERMINAL:
                self.enqueue(conn, case['id'], 'poll', {'transfer_id': transfer['id']}, self.now(conn) + timedelta(minutes=1))

    def poll(self, transfer_id):
        with self.db.tx() as conn:
            transfer = d.get(conn, d.transfers, transfer_id)
            case = d.get(conn, d.cases, transfer['case_id'])
            if case['status'] in TERMINAL:
                return
            now = self.now(conn)
        remote = self.provider.find_transfer(transfer['request_ref'])
        if remote is None:
            with self.db.tx() as conn:
                case = d.get(conn, d.cases, transfer['case_id'])
                self.record(conn, case, 'provider.unresolved', 'Original request could not be resolved. No replacement initiated.', next_state='manual_review')
            return
        with self.db.tx() as conn:
            case = d.get(conn, d.cases, transfer['case_id'])
            transfer = d.get(conn, d.transfers, transfer_id)
            if case['status'] == 'outcome_unknown':
                transfer.update(provider_transfer_ref=remote['id'], status=remote['status'])
                self.record(conn, case, 'provider.recovered', 'Located the original transfer by its stable request reference.', next_state=remote['status'], evidence={'provider_reference': remote['id']})
                for action in d.rows(conn, d.actions, d.actions.c.case_id == case['id']):
                    if action['status'] == 'outcome_unknown':
                        action.update(status='completed', provider_result=remote)
                        transfer['approval_id'] = action['approval_id']
                        d.put(conn, d.actions, action)
                d.put(conn, d.transfers, transfer)
        start = datetime.fromisoformat(remote['accepted_at'])
        elapsed = (now - start).total_seconds() / 60
        if remote['status'] == 'rejected':
            return
        if elapsed >= 2:
            self.sim_event(transfer, remote, 'processing', start + timedelta(minutes=2), {'source_debit_minor': remote['source_total_minor'], 'source_currency': 'USD', 'fee_minor': remote['fee_minor'], 'funding_reference': 'fund_' + remote['id']})
        if remote['scenario'] == 'missing_document' and elapsed >= 3 and not remote['documents_received']:
            self.sim_event(transfer, remote, 'information_required', start + timedelta(minutes=3), {'request_id': 'purpose_' + remote['id'], 'fields': ['purpose_supporting_document']})
        else:
            payout = max(start + timedelta(minutes=4), datetime.fromisoformat(remote.get('documents_received_at', remote['accepted_at'])))
            if now >= payout:
                self.sim_event(transfer, remote, 'payout_pending', payout, {})
            delivery = start + timedelta(hours=remote['delivery_hours'])
            if now >= delivery:
                if remote['scenario'] == 'delay':
                    self.sim_event(transfer, remote, 'delayed', delivery, {})
                else:
                    self.sim_event(transfer, remote, 'delivered', max(delivery, payout), {'currency': 'INR', 'actual_recipient_minor': remote['recipient_minor'] - (10000 if remote['scenario'] == 'short_payment' else 0), 'provider_reference': 'utr_' + remote['id'], 'actual_source_debit_minor': remote['source_total_minor'], 'actual_fee_minor': remote['fee_minor']})
        with self.db.tx() as conn:
            case = d.get(conn, d.cases, transfer['case_id'])
            if case['status'] == 'delayed' and now > start + timedelta(hours=remote['delivery_hours'] + 6):
                self.record(conn, case, 'delay.escalated', 'Delivery evidence is still missing. Operator follow-up required.', next_state='manual_review')
            if case['status'] not in TERMINAL and not (case['status'] == 'manual_review' and d.rows(conn, d.receipts, d.receipts.c.transfer_id == transfer_id)):
                self.enqueue(conn, case['id'], 'poll', {'transfer_id': transfer_id}, now + timedelta(minutes=1))

    def sim_event(self, transfer, remote, kind, at, payload):
        self.ingest({'id': remote['id'] + ':' + kind, 'provider_id': transfer['provider_id'], 'provider_transfer_ref': remote['id'], 'type': kind,
                     'occurred_at': at.isoformat(), 'environment': 'mock', 'data': payload})

    def ingest(self, event, replay_id=None):
        event = dict(event)
        event['occurred_at'] = event['occurred_at'].replace('Z', '+00:00')
        with self.db.tx() as conn:
            duplicates = d.rows(conn, d.inbox, d.inbox.c.provider_id == event['provider_id'], d.inbox.c.event_id == event['id'])
            if duplicates:
                require(duplicates[0]['payload_hash'] == digest(event), 'Conflicting payload for an existing provider event')
                if replay_id != duplicates[0]['id'] or duplicates[0]['status'] != 'deferred':
                    return {'duplicate': True}
            matches = [t for t in d.rows(conn, d.transfers) if t['provider_transfer_ref'] == event['provider_transfer_ref'] and t['provider_id'] == event['provider_id']]
            require(len(matches) == 1, 'Unknown provider transfer reference', 404)
            transfer = matches[0]
            case = d.get(conn, d.cases, transfer['case_id'])
            kind, data = event['type'], event['data']
            require(event['environment'] == 'mock', 'Only mock provider events are accepted', 422)
            event_time = datetime.fromisoformat(event['occurred_at'])
            require(event_time.tzinfo is not None and event_time <= self.now(conn), 'Provider event timestamp is invalid', 422)
            if kind == 'delivered':
                require(isinstance(data.get('provider_reference'), str) and len(data['provider_reference']) > 0, 'Delivery requires a provider payout reference', 422)
                for name in ['actual_recipient_minor', 'actual_source_debit_minor', 'actual_fee_minor']:
                    require(type(data.get(name)) is int and data[name] >= 0, 'Delivery requires actual nonnegative integer amounts', 422)
                require(data.get('currency') == 'INR', 'Delivery currency mismatch', 422)
            if kind == 'information_required':
                require(data.get('fields') == ['purpose_supporting_document'] and data.get('request_id'), 'Unsupported document requirement', 422)
            # Out-of-order events are retained as deferred, not allowed to regress the case.
            actionable = kind in ALLOWED[case['status']]
            inbox = {'id': replay_id or uid('inbox'), 'provider_id': event['provider_id'], 'event_id': event['id'], 'payload': event, 'payload_hash': digest(event),
                     'status': 'applied' if actionable else 'deferred', 'case_id': case['id'], 'received_at': self.now(conn).isoformat()}
            if duplicates:
                inbox['received_at'] = duplicates[0]['received_at']
                inbox['replay_count'] = duplicates[0].get('replay_count', 0) + 1
            d.put(conn, d.inbox, inbox)
            if not actionable:
                return {'deferred': True, 'inbox_id': inbox['id']}
            messages = {'processing': 'Source funding confirmed. Provider is processing; the recipient has not been paid.',
                        'information_required': 'Provider needs a purpose-supporting document.', 'payout_pending': 'Payout is with the recipient bank. Delivery evidence is pending.',
                        'delivered': 'Recipient credit confirmed by a payout reference and final amount.', 'delayed': 'Estimated delivery window was missed. The transfer remains active.',
                        'rejected': 'Provider rejected the transfer.', 'cancelled': 'Provider confirmed cancellation; refund reconciliation is separate.', 'funding_pending': 'Provider is awaiting source funding.'}
            evidence = self.record(conn, case, 'remittance.' + kind, messages[kind], actor='provider:' + event['provider_id'], next_state=kind, source_event_id=event['id'], evidence=data)
            transfer.update(status=case['status'], version=transfer['version'] + 1)
            if kind == 'processing':
                transfer['funding_evidence'] = data
            if kind == 'information_required':
                d.put(conn, d.requirements, {'id': uid('requirement'), 'transfer_id': transfer['id'], 'provider_request_id': data['request_id'],
                       'requested_fields': data['fields'], 'due_at': (self.now(conn) + timedelta(hours=12)).isoformat(), 'status': 'open', 'satisfied_by_manifest_id': None})
            if kind == 'delivered':
                q = transfer['quote']
                differences = {'recipient_minor': data['actual_recipient_minor'] - q['recipient_minor'],
                               'source_debit_minor': data['actual_source_debit_minor'] - q['source_total_minor'],
                               'fee_minor': data['actual_fee_minor'] - q['fee_minor']}
                matched = all(v == 0 for v in differences.values())
                receipt = {'id': uid('receipt'), 'transfer_id': transfer['id'], 'provider_reference': data['provider_reference'],
                           'actual_recipient_minor': data['actual_recipient_minor'], 'quoted_recipient_minor': q['recipient_minor'], 'currency': 'INR',
                           'actual_source_debit_minor': data['actual_source_debit_minor'], 'actual_fee_minor': data['actual_fee_minor'], 'source_currency': 'USD',
                           'delivered_at': event['occurred_at'], 'evidence_id': evidence['id'], 'differences': differences,
                           'reconciliation_status': 'matched' if matched else 'exception', 'environment': 'mock'}
                d.put(conn, d.receipts, receipt)
                case['completion_evidence_ref'] = evidence['id'] if matched else None
                self.record(conn, case, 'delivery.reconciled' if matched else 'delivery.exception',
                            'Recipient amount, sender debit and fee match the approved quote.' if matched else 'Delivered amounts differ from the approved quote. An operator must resolve the discrepancy.',
                            next_state='reconciled' if matched else 'manual_review', evidence=receipt)
                transfer['status'] = case['status']
            d.put(conn, d.transfers, transfer)
            return {'applied': True, 'case_id': case['id'], 'status': case['status']}

    def replay(self, inbox_id):
        with self.db.tx() as conn:
            entry = d.get(conn, d.inbox, inbox_id)
            require(entry is not None, 'Event not found', 404)
            require(entry['status'] == 'deferred', 'Only deferred events can be replayed')
            # Keep original event identity and payload; never construct an action or approval.
            event = entry['payload']
        return self.ingest(event, replay_id=inbox_id)

    def refund(self, transfer_id):
        with self.db.tx() as conn:
            transfer = d.get(conn, d.transfers, transfer_id)
            case = d.get(conn, d.cases, transfer['case_id'])
            if case['status'] != 'cancelled' or transfer.get('refund_status') == 'confirmed':
                return
            transfer.update(refund_status='confirmed', refund_minor=transfer['quote']['source_total_minor'], refund_currency='USD', refund_reference='refund_' + transfer['provider_transfer_ref'])
            d.put(conn, d.transfers, transfer)
            self.record(conn, case, 'refund.confirmed', 'Simulated source refund confirmed separately from cancellation.', evidence={'reference': transfer['refund_reference'], 'amount_minor': transfer['refund_minor'], 'currency': 'USD'})
