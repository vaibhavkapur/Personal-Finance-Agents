import pytest
from cryptography.fernet import Fernet
from backend.app.persistence.store import Store, settings
from backend.app.workflows.service import EstateService


@pytest.fixture
def workspace(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'case.db'), Fernet.generate_key())
    with store.engine.begin() as con:
        con.execute(settings.insert().values(id='clock', value='2026-09-26T12:00:00+00:00'))
    service = EstateService(store)
    case = service.create('demo', 'alex_demo')
    service.discover(case['id'], 'demo', 'test')
    service.review_authority(case['id'], 'demo', 'mock_reviewer')
    yield store, service, case['id']
    store.engine.dispose()
