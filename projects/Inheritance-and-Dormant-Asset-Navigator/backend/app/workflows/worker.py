import os
import time
from datetime import datetime, timedelta
from sqlalchemy import and_, or_, select
from backend.app.adapters.mock import HttpMockAdapter, MockAdapter
from backend.app.domain.engine import digest, find, packet_payload, transition
from backend.app.persistence.store import DomainError, Store, jobs
from backend.app.workflows.service import EstateService


class Worker:
    def __init__(self, store, adapter=None):
        self.store = store
        self.service = EstateService(store)
        self.adapter = adapter or (HttpMockAdapter(os.environ['MOCK_PROVIDER_URL'], os.environ['MOCK_PROVIDER_TOKEN']) if os.getenv('MOCK_PROVIDER_URL') else MockAdapter(store))

    def tick(self):
        now = self.store.now()
        with self.store.engine.begin() as con:
            eligible = or_(jobs.c.status == 'pending', and_(jobs.c.status == 'processing', jobs.c.lease_until < now))
            job = con.execute(select(jobs).where(eligible).order_by(jobs.c.id).limit(1)).mappings().first()
            if not job:
                return False
            claimed = con.execute(jobs.update().where(jobs.c.id == job['id'], eligible).values(status='processing', lease_until=(datetime.fromisoformat(now) + timedelta(seconds=30)).isoformat(), attempts=job['attempts'] + 1))
            if claimed.rowcount != 1:
                return False
        try:
            case = self.store.load(job['case_id'], job['tenant'])
            action = find(case['actions'], job['action_id'])
            if action['status'] in ('done', 'revoked', 'invalidated', 'manual_review'):
                self.finish(job, 'done')
                return True
            # Any restart or uncertain outcome first looks up the ORIGINAL request.
            result = self.adapter.lookup_request(action['request_ref'])
            if result is None:
                with self.store.edit(case['id'], case['tenant']) as (con, current):
                    action = find(current['actions'], job['action_id'])
                    asset = find(current['assets'], action['asset_id'])
                    if action['status'] == 'queued':
                        approval = find(current['approvals'], action['approval_id'])
                        now = self.store.now(con)
                        if approval['revoked_at'] or approval['consumed_at'] or approval['expires_at'] <= now or approval['action_hash'] != digest(action['payload']):
                            raise DomainError('Approval expired, revoked, consumed or changed.')
                        if digest(packet_payload(current, asset, now)) != action['payload_hash']:
                            raise DomainError('Document, requirement or authority changed after approval.')
                        approval['consumed_at'] = now
                        action['status'] = 'dispatching'
                        asset['claim_version'] = action['submission_version']
                        transition(self.store, con, current, asset, 'submitted', 'executor', 'Approval consumed; durable request reference persisted before provider call.')
                    elif action['status'] in ('dispatching', 'unknown'):
                        # The authorization was already consumed for this exact request.
                        # Repeat only through provider-enforced idempotency, rechecking authority.
                        if digest(packet_payload(current, asset, self.store.now(con))) != action['payload_hash']:
                            raise DomainError('Pending request requires review after material input change.')
                    else:
                        raise DomainError('Action is not authorized for execution.')
                    payload = dict(action['payload'])
                # Provider call intentionally outside any case transaction.
                submit = self.adapter.submit_inquiry if action['type'] == 'inquire' else self.adapter.submit_claim
                result = submit(payload, action['request_ref'])
            self.service.apply_result(job['case_id'], job['tenant'], job['action_id'], result)
            latest = self.store.load(job['case_id'], job['tenant'])
            state = find(latest['actions'], job['action_id'])['status']
            self.finish(job, 'processing' if state == 'unknown' else 'done')
        except DomainError as exc:
            self.hold(job, exc.message)
        except Exception as exc:
            # Redacted exception type only. Never discard or replace the request reference.
            with self.store.edit(job['case_id'], job['tenant']) as (con, case):
                action = find(case['actions'], job['action_id'])
                if action['status'] == 'dispatching':
                    action['status'] = 'unknown'
                self.store.event(con, case, 'provider.outcome_unknown', 'executor', action['asset_id'], detail=f'{type(exc).__name__}; status lookup required before retry.')
            if job['attempts'] >= 4:
                self.hold(job, 'Bounded reconciliation attempts exhausted. Operator review required.')
            else:
                self.finish(job, 'processing', type(exc).__name__)
        return True

    def finish(self, job, status, error=None):
        with self.store.engine.begin() as con:
            con.execute(jobs.update().where(jobs.c.id == job['id']).values(status=status, error=error, lease_until=(datetime.fromisoformat(self.store.now(con)) + timedelta(seconds=min(60, 2 ** job['attempts']))).isoformat()))

    def hold(self, job, detail):
        with self.store.edit(job['case_id'], job['tenant']) as (con, case):
            action = find(case['actions'], job['action_id'])
            asset = find(case['assets'], action['asset_id'])
            # A cancelled job may have been leased just before revocation.
            if action['status'] == 'revoked':
                return
            action['status'] = 'manual_review' if asset['status'] == 'submitted' else 'invalidated'
            if asset['status'] == 'submitted':
                transition(self.store, con, case, asset, 'manual_review', 'executor', detail)
            elif asset['status'] == 'awaiting_approval':
                transition(self.store, con, case, asset, 'requirements_ready', 'executor', detail)
            self.store.event(con, case, 'action.held', 'executor', asset['id'], detail=detail)
            con.execute(jobs.update().where(jobs.c.id == job['id']).values(status='held', error=detail))


if __name__ == '__main__':
    worker = Worker(Store())
    while True:
        worker.tick()
        time.sleep(1)
