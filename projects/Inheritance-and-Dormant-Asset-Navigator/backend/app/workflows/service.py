from datetime import datetime, timedelta
from sqlalchemy import select
from backend.app.domain.engine import check_authority, digest, discover, find, packet_payload, requirements, summary, transition
from backend.app.domain.fixtures import document, seed_case, supplement
from backend.app.persistence.store import DomainError, inbox, jobs, uid


class EstateService:
    def __init__(self, store):
        self.store = store

    def create(self, tenant, actor):
        return self.store.create(seed_case(tenant, actor))

    def view(self, case_id, tenant):
        case = self.store.load(case_id, tenant)
        case['summary'] = summary(case)
        case['clock'] = self.store.now()
        for asset in case['assets']:
            asset['requirements'] = requirements(case, asset)
        return case

    def discover(self, case_id, tenant, actor, expected=None):
        with self.store.edit(case_id, tenant, expected) as (con, case):
            discover(self.store, con, case, actor)
        return self.view(case_id, tenant)

    def review_authority(self, case_id, tenant, actor, expected=None):
        with self.store.edit(case_id, tenant, expected) as (con, case):
            evidence = [d for d in case['documents'] if d['kind'] == 'letters_of_authority' and d['source'] == 'synthetic_fixture']
            if not evidence or case['claimed_role'] != 'personal_representative':
                raise DomainError('Mock reviewer cannot verify the supplied authority evidence.', 403)
            for institution_id in case['requirements']:
                old = case['authorities'].get(institution_id)
                # Re-running a review cannot clear a known contested authority.
                if old and old['review_status'] == 'contested':
                    continue
                case['authorities'][institution_id] = {'id': old['id'] if old else uid('authority'), 'version': (old['version'] if old else 0) + 1, 'user_id': case['owner_user_id'], 'claimed_role': case['claimed_role'], 'subject': case['decedent_record_id'], 'evidence_ids': [evidence[0]['id']], 'verified_by': 'mock_institution_reviewer', 'review_status': 'verified', 'permitted_actions': ['view_records', 'inquire', 'receive_documents'] + ([] if institution_id == 'cedar' else ['submit_claim']), 'expires_at': '2027-09-25T00:00:00+00:00'}
            for asset in case['assets']:
                if asset['status'] == 'authority_review':
                    transition(self.store, con, case, asset, 'requirements_ready', 'mock_institution_reviewer', 'Fixture authority reviewed for this institution and action; no entitlement decision.')
            self.store.event(con, case, 'authority.reviewed', actor, detail='Mock institution reviewed synthetic letters. Insurance permission is inquiry only. Distribution authority is not granted.')
        return self.view(case_id, tenant)

    def add_document(self, case_id, tenant, actor, expected, kind, name=None, content=None):
        with self.store.edit(case_id, tenant, expected) as (con, case):
            if content is None:
                if any(d['kind'] == kind for d in case['documents']):
                    raise DomainError('This synthetic supplement is already in the workspace.')
                doc = supplement(kind)
            else:
                if not content.startswith('SYNTHETIC RECORD\n'):
                    raise DomainError('This prototype accepts synthetic text records only.', 422)
                doc = document(name, kind, content, source='user_supplied_synthetic')
            if any(d['hash'] == doc['hash'] for d in case['documents']):
                raise DomainError('An identical document already exists.')
            case['documents'].append(doc)
            for asset in case['assets']:
                if asset['status'] == 'evidence_requested' and not requirements(case, asset)['missing']:
                    transition(self.store, con, case, asset, 'requirements_ready', actor, 'Requested evidence added; a new packet and approval are required.')
            self.store.event(con, case, 'document.added', actor, detail=f'{doc["name"]} · {doc["hash"][:19]}')
        return self.view(case_id, tenant)

    def prepare(self, case_id, tenant, actor, asset_id, expected, idempotency_key):
        with self.store.edit(case_id, tenant, expected) as (con, case):
            asset = find(case['assets'], asset_id)
            prior = next((a for a in case['actions'] if a['idempotency_key'] == idempotency_key), None)
            if prior:
                if prior['asset_id'] != asset_id:
                    raise DomainError('Idempotency key was used for different content.')
                return prior
            if asset['status'] not in ('requirements_ready', 'awaiting_approval'):
                raise DomainError('This asset is not ready for a packet draft.')
            payload = packet_payload(case, asset, self.store.now(con))
            for old in case['actions']:
                if old['asset_id'] == asset_id and old['status'] in ('queued', 'dispatching', 'unknown'):
                    raise DomainError('An approved action is still pending. Reconcile it before drafting again.')
            for old in case['actions']:
                if old['asset_id'] == asset_id and old['status'] == 'draft':
                    old['status'] = 'superseded'
            action = {'id': uid('action'), 'asset_id': asset_id, 'type': payload['action_type'], 'payload': payload, 'payload_hash': digest(payload), 'status': 'draft', 'approval_id': None, 'provider_reference': None, 'idempotency_key': idempotency_key, 'request_ref': uid('request'), 'challenge_id': uid('challenge'), 'challenge_expires_at': (datetime.fromisoformat(self.store.now(con)) + timedelta(minutes=15)).isoformat(), 'submission_version': asset['claim_version'] + 1, 'created_at': self.store.now(con)}
            case['actions'].append(action)
            if asset['status'] != 'awaiting_approval':
                transition(self.store, con, case, asset, 'awaiting_approval', actor, 'Recipient and minimal document manifest prepared for review.')
            self.store.event(con, case, 'packet.prepared', actor, asset_id, detail=action['payload_hash'])
        return action

    def approve(self, case_id, tenant, actor, action_id, expected, action_hash, challenge):
        with self.store.edit(case_id, tenant, expected) as (con, case):
            action = find(case['actions'], action_id)
            if action['payload_hash'] != action_hash or action['challenge_id'] != challenge:
                raise DomainError('Approval challenge or exact payload does not match.', 403)
            if action['status'] in ('queued', 'dispatching', 'unknown', 'done') and action['approval_id']:
                return action
            if action['status'] != 'draft':
                raise DomainError('This draft is no longer available for approval.')
            now = self.store.now(con)
            if datetime.fromisoformat(now) >= datetime.fromisoformat(action['challenge_expires_at']):
                raise DomainError('Approval challenge expired. Prepare a new packet.')
            asset = find(case['assets'], action['asset_id'])
            if digest(packet_payload(case, asset, now)) != action_hash:
                raise DomainError('Material inputs changed. Prepare and review a new packet.')
            approval = {'id': uid('approval'), 'approver': actor, 'action_hash': action_hash, 'scope': action['payload']['recipient'], 'created_at': now, 'expires_at': action['challenge_expires_at'], 'revoked_at': None, 'consumed_at': None}
            case['approvals'].append(approval)
            action.update(status='queued', approval_id=approval['id'])
            con.execute(jobs.insert().values(id=uid('job'), case_id=case_id, tenant=tenant, action_id=action_id, status='pending', lease_until=None, attempts=0))
            self.store.event(con, case, 'action.approved', actor, action['asset_id'], detail=f'Exact manifest approved for {approval["scope"]}.')
        return action

    def revoke(self, case_id, tenant, actor, action_id, expected):
        with self.store.edit(case_id, tenant, expected) as (con, case):
            action = find(case['actions'], action_id)
            if action['status'] not in ('draft', 'queued'):
                raise DomainError('Action may already have reached the provider. Reconcile its outcome.')
            if action['approval_id']:
                approval = find(case['approvals'], action['approval_id'])
                if approval['consumed_at']:
                    raise DomainError('Approval was already consumed.')
                approval['revoked_at'] = self.store.now(con)
            action['status'] = 'revoked'
            con.execute(jobs.update().where(jobs.c.action_id == action_id).values(status='cancelled'))
            asset = find(case['assets'], action['asset_id'])
            transition(self.store, con, case, asset, 'requirements_ready', actor, 'Disclosure approval revoked before dispatch.')
        return self.view(case_id, tenant)

    def apply_result(self, case_id, tenant, action_id, result):
        with self.store.edit(case_id, tenant) as (con, case):
            action = find(case['actions'], action_id)
            asset = find(case['assets'], action['asset_id'])
            valid = result.get('environment') == 'mock' and result.get('asset_id') == asset['id'] and result.get('identity') == asset['identity_key'] and result.get('request_ref') == action['request_ref'] and result.get('institution_id') == asset['institution_id'] and result.get('event_id') and result.get('case_ref')
            if not valid:
                if asset['status'] == 'submitted':
                    transition(self.store, con, case, asset, 'manual_review', 'reconciler', 'Provider response failed identity or provenance validation.')
                action['status'] = 'manual_review'
                return
            seen = con.execute(select(inbox).where(inbox.c.provider == asset['institution_id'], inbox.c.event_id == result['event_id'])).first()
            if seen:
                return
            if action['status'] == 'done':
                raise DomainError('A completed action cannot accept a different terminal event.')
            con.execute(inbox.insert().values(provider=asset['institution_id'], event_id=result['event_id'], case_id=case_id))
            action['provider_reference'] = result['case_ref']
            asset['provider_case_ref'] = result['case_ref']
            if result['decision'] == 'pending':
                action['status'] = 'unknown'
                return
            if result['decision'] == 'evidence_requested':
                asset['extra_requirements'] = ['certified_authority']
                transition(self.store, con, case, asset, 'evidence_requested', 'mock_provider', 'Harbor requires a certified authority supplement before it can finish the claim.')
            elif result['decision'] == 'disputed_authority':
                case['authorities'][asset['institution_id']]['review_status'] = 'contested'
                transition(self.store, con, case, asset, 'disputed_authority', 'mock_provider', 'Policy names Jordan Morgan. Estate representative authority does not establish beneficiary entitlement.')
                transition(self.store, con, case, asset, 'human_review', 'reconciler', 'Handoff: named-beneficiary process and professional review required. No distribution recorded.')
            elif result['decision'] in ('claim_denied', 'no_asset_found'):
                transition(self.store, con, case, asset, result['decision'], 'mock_provider', 'This decision applies only to this inquiry at this institution.')
            elif result['decision'] == 'accepted':
                resolution = result.get('resolution', {})
                verified = resolution.get('amount_minor') == case['requirements'][asset['institution_id']]['amount_minor'] and type(resolution.get('amount_minor')) is int and resolution.get('currency') == 'USD' and resolution.get('destination_authority_ref') == f'mock_verified_estate:{case["decedent_record_id"]}' and resolution.get('evidence_id') and resolution.get('institution_reference') == result['case_ref']
                if not verified:
                    transition(self.store, con, case, asset, 'manual_review', 'reconciler', 'Distribution evidence did not match the institution amount, currency or authorized estate destination.')
                    action['status'] = 'manual_review'
                    return
                asset['ownership_status'] = 'institution_confirmed'
                transition(self.store, con, case, asset, 'institution_confirmed', 'mock_provider', 'Institution confirmed the synthetic asset identity.')
                transition(self.store, con, case, asset, 'resolution_pending', 'reconciler', 'Checking institution decision and distribution evidence.')
                asset['resolution'] = {**resolution, 'decision_type': 'simulated_distribution', 'resolved_at': self.store.now(con), 'environment': 'mock'}
                transition(self.store, con, case, asset, 'resolved', 'reconciler', 'Verified simulated distribution to institution-approved estate destination. No real money moved.')
            else:
                transition(self.store, con, case, asset, 'manual_review', 'reconciler', 'Unsupported provider decision.')
                action['status'] = 'manual_review'
                return
            action['status'] = 'done'
            # Estate completeness is never inferred from individual asset resolution.
            case['status'] = 'active'
