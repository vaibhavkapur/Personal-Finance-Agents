"""Exercise the actual API, persisted worker, and institution adapter over HTTP."""
import argparse
import json
import time
import uuid
import httpx


def run(base_url):
    client = httpx.Client(base_url=base_url, timeout=10, headers={'Origin': base_url})
    def post(path, body):
        response = client.post('/v1' + path, json=body)
        response.raise_for_status()
        return response.json()
    post('/session', {'username': 'alex', 'password': 'everkeep-demo'})
    case = post('/estate-cases', {})
    cid = case['id']
    def current():
        response = client.get('/v1/estate-cases/' + cid)
        response.raise_for_status()
        return response.json()
    post('/estate-cases/' + cid + '/discover-candidates', {'expected_case_version': case['version']})
    post('/estate-cases/' + cid + '/authority-reviews', {'expected_case_version': current()['version']})
    def add(kind):
        post('/estate-cases/' + cid + '/documents', {'expected_case_version': current()['version'], 'kind': kind})
    def submit(institution, expected):
        case = current()
        asset = next(a for a in case['assets'] if a['institution_id'] == institution and a['match_status'] != 'ambiguous')
        action = post('/estate-assets/' + asset['id'] + '/packet-drafts', {'expected_case_version': case['version'], 'idempotency_key': str(uuid.uuid4())})
        post('/actions/' + action['id'] + '/approve', {'expected_case_version': current()['version'], 'action_payload_hash': action['payload_hash'], 'approval_challenge_id': action['challenge_id']})
        for _ in range(40):
            case = current()
            updated = next(a for a in case['assets'] if a['id'] == asset['id'])
            if updated['status'] == expected:
                assert updated['provider_case_ref']
                return updated
            if updated['status'] == 'manual_review':
                raise AssertionError(case['events'][-1])
            time.sleep(.25)
        raise AssertionError('Worker did not produce expected outcome: ' + expected)
    submit('harbor', 'evidence_requested')
    add('certified_authority')
    submit('harbor', 'resolved')
    submit('cedar', 'human_review')
    add('retirement_claim_form')
    submit('summit', 'resolved')
    result = current()
    assert result['summary']['verified_recovery_minor'] == 12960525
    assert result['summary']['resolved_assets'] == 2
    assert result['summary']['ambiguous_candidates'] == 1
    assert not result['summary']['inventory_complete']
    print(json.dumps({'passed': True, 'case_id': cid, 'summary': result['summary'], 'scope': result['inventory_scope']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8731')
    run(parser.parse_args().base_url)
