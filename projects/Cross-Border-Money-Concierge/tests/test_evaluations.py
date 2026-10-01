import json
from pathlib import Path
import pytest
from scripts.demo import run_demo

@pytest.mark.parametrize('fixture',json.loads(Path('fixtures/evaluation-cases.json').read_text()),ids=lambda f:f['id'])
def test_labeled_case(fixture):
    f=fixture
    r=run_demo(f['provider'],None if f['scenario']=='refusal' else f['scenario'],f['amount'],f['mode'],f['scenario']=='refusal')
    assert r['status']==f['expected_state']
    assert r['provider_transfer_count']==f['expected_provider_writes']
    if r['status']=='reconciled':assert r['receipt']['provider_reference'] and r['receipt']['reconciliation_status']=='matched'
    if f['scenario']=='short_payment':assert r['receipt']['differences']['recipient_minor']==-10000
    if f['scenario']=='refusal':assert 'cannot authorize' in r['concierge_response']
