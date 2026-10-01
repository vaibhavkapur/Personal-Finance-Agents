"""One command, fresh isolated database, complete three-institution demo."""
import json
import tempfile
from cryptography.fernet import Fernet
from backend.app.persistence.store import Store, uid
from backend.app.workflows.service import EstateService
from backend.app.workflows.worker import Worker


def run():
    with tempfile.TemporaryDirectory() as temp:
        store = Store('sqlite:///' + temp + '/demo.db', Fernet.generate_key())
        service = EstateService(store)
        cid = service.create('demo', 'alex_demo')['id']
        service.discover(cid, 'demo', 'demo')
        service.review_authority(cid, 'demo', 'mock_institution_reviewer')
        def submit(institution):
            case = store.load(cid, 'demo')
            asset = next(a for a in case['assets'] if a['institution_id'] == institution and a['match_status'] != 'ambiguous')
            action = service.prepare(cid, 'demo', 'demo', asset['id'], case['version'], uid('demo_key'))
            service.approve(cid, 'demo', 'scripted_fixture_human', action['id'], store.load(cid, 'demo')['version'], action['payload_hash'], action['challenge_id'])
            Worker(store).tick()
        submit('harbor')
        assert service.view(cid, 'demo')['assets'][0]['status'] == 'evidence_requested'
        service.add_document(cid, 'demo', 'demo', store.load(cid, 'demo')['version'], 'certified_authority')
        submit('harbor')
        submit('cedar')
        service.add_document(cid, 'demo', 'demo', store.load(cid, 'demo')['version'], 'retirement_claim_form')
        submit('summit')
        case = service.view(cid, 'demo')
        print(json.dumps({'environment': 'mock', 'assets': [{'institution':a['institution'], 'status':a['status'], 'provider_reference':a['provider_case_ref']} for a in case['assets']], 'summary':case['summary'], 'scope':case['inventory_scope']}, indent=2))
        assert case['summary']['verified_recovery_minor'] == 12960525
        assert case['summary']['resolved_assets'] == 2
        assert not case['summary']['inventory_complete']


if __name__ == '__main__':
    run()
