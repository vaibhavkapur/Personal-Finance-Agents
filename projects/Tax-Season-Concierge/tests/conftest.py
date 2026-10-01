import pytest
from app.persistence.store import Store
from app.workflows.engine import Engine
from app.api.schemas import CreateCase, ApprovalRequest
from app import seed
from app.domain.tax import load_rules

@pytest.fixture
def engine(tmp_path):
    return Engine(Store(str(tmp_path/'test.db')))


def draft(engine, scenario='two_jobs', tenant='tenant_a'):
    c=engine.create(tenant,CreateCase(scenario=scenario))
    c=engine.profile_check(tenant,c['id'],c['version'],seed.profile())
    c=engine.seed_forms(tenant,c['id'],c['version'])
    c=engine.reconcile(tenant,c['id'],c['version'],True)
    if not c['reconciliation']['ready']:
        return c
    c=engine.calculate(tenant,c['id'],c['version'],load_rules()[0]['id'],c['reconciliation']['facts_hash'])
    return engine.prepare(tenant,c['id'],c['version'])


def approve(engine,c,tenant='tenant_a'):
    a=next(a for a in c['actions'] if a['id']==c['package']['action_id'])
    return engine.approve(tenant,a['id'],ApprovalRequest(expected_case_version=c['version'],action_payload_hash=a['payload_hash'],approval_challenge_id=a['challenge_id']))
