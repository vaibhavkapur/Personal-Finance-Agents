from datetime import datetime, timedelta
from typing import Protocol
import httpx
from sqlalchemy import select
from backend.app.domain.engine import digest
from backend.app.persistence.store import DomainError, provider_requests, uid


class EstateInstitutionAdapter(Protocol):
    capabilities: dict
    def get_requirements(self, asset: dict, role: dict) -> dict: ...
    def submit_inquiry(self, packet: dict, request_ref: str) -> dict: ...
    def submit_claim(self, packet: dict, request_ref: str) -> dict: ...
    def lookup_request(self, request_ref: str) -> dict | None: ...
    def get_resolution(self, provider_case_ref: str) -> dict: ...


class MockAdapter:
    capabilities = {'environment': 'mock', 'lookup_by_request': True, 'real_payout': False, 'controlled_clock': True}

    def __init__(self, store):
        self.store = store

    def get_requirements(self, asset, role):
        from backend.app.domain.fixtures import INSTITUTIONS
        return {**INSTITUTIONS[asset['institution_id']], 'environment': 'mock', 'retrieved_at': self.store.now(), 'classification': 'simulated'}

    def lookup_request(self, request_ref):
        with self.store.engine.connect() as con:
            row = con.execute(select(provider_requests.c.sealed).where(provider_requests.c.request_ref == request_ref)).scalar_one_or_none()
        if not row:
            return None
        record = self.store.unseal(row)
        if record.get('available_at') and self.store.now() < record['available_at']:
            return {**record['result'], 'event_id': record['result']['event_id'] + '_pending', 'decision': 'pending'}
        return record['result']

    def get_resolution(self, provider_case_ref):
        with self.store.engine.connect() as con:
            ref = con.execute(select(provider_requests.c.request_ref).where(provider_requests.c.case_ref == provider_case_ref)).scalar_one_or_none()
        return self.lookup_request(ref) if ref else None

    def submit_inquiry(self, packet, request_ref):
        return self.submit_claim(packet, request_ref)

    def submit_claim(self, packet, request_ref):
        existing = self.lookup_request(request_ref)
        if existing:
            with self.store.engine.connect() as con:
                old_hash = con.execute(select(provider_requests.c.payload_hash).where(provider_requests.c.request_ref == request_ref)).scalar_one()
            if old_hash != digest(packet):
                raise DomainError('Provider request reference reused with a different payload.')
            return existing
        inst = packet['institution_id']
        scenario = packet.get('scenario', 'normal')
        kinds = [d['kind'] for d in packet['manifest']]
        decision = 'disputed_authority' if inst == 'cedar' else ('evidence_requested' if inst == 'harbor' and 'certified_authority' not in kinds else 'accepted')
        if scenario == 'declined':
            decision = 'claim_denied'
        if scenario == 'no_match':
            decision = 'no_asset_found'
        case_ref = 'MOCK-' + uid(inst).upper()
        from backend.app.domain.fixtures import INSTITUTIONS
        result = {'event_id': uid('provider_event'), 'case_ref': case_ref, 'request_ref': request_ref, 'asset_id': packet['asset_id'], 'identity': packet['identity'], 'institution_id': inst, 'decision': decision, 'environment': 'mock', 'source': 'independent_mock_ledger', 'retrieved_at': self.store.now(), 'classification': 'simulated', 'resolution': {'amount_minor': INSTITUTIONS[inst]['amount_minor'], 'currency': 'USD', 'institution_reference': case_ref, 'destination_authority_ref': f'mock_verified_estate:{packet["identity"][2]}', 'evidence_id': uid('mock_receipt')}}
        if scenario == 'malformed':
            result.pop('asset_id')
        if scenario == 'wrong_destination':
            result['resolution']['destination_authority_ref'] = 'unverified_personal_account'
        record = {'result': result, 'available_at': (datetime.fromisoformat(self.store.now()) + timedelta(minutes=5)).isoformat() if scenario == 'delayed' else None}
        with self.store.engine.begin() as con:
            con.execute(provider_requests.insert().values(request_ref=request_ref, payload_hash=digest(packet), case_ref=case_ref, sealed=self.store.seal(record)))
        if scenario == 'timeout_after_acceptance':
            raise TimeoutError('Provider accepted the request, but the response was lost.')
        return self.lookup_request(request_ref)


class HttpMockAdapter:
    capabilities = MockAdapter.capabilities
    def __init__(self, url, token):
        self.client = httpx.Client(base_url=url, headers={'Authorization': 'Bearer ' + token}, timeout=8)

    def lookup_request(self, request_ref):
        response = self.client.get('/requests/' + request_ref)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def submit_claim(self, packet, request_ref):
        response = self.client.post('/submit', json={'packet': packet, 'request_ref': request_ref})
        response.raise_for_status()
        return response.json()

    submit_inquiry = submit_claim

    def get_resolution(self, provider_case_ref):
        response = self.client.get('/resolutions/' + provider_case_ref)
        response.raise_for_status()
        return response.json()
