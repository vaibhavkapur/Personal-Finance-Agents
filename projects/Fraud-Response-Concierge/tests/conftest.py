import pytest
from fastapi.testclient import TestClient
from backend.app.api.main import create_app
from backend.app.persistence.store import Store
from backend.app.domain.engine import Service
from backend.app.domain.registry import CUSTOMER

@pytest.fixture
def store(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'test.db'))
    yield db
    db.engine.dispose()
@pytest.fixture
def service(store): return Service(store)
@pytest.fixture
def client(store):
    with TestClient(create_app(store)) as client:
        client.post('/v1/demo/session')
        yield client
@pytest.fixture
def case(service):
    case=service.create(CUSTOMER,'2026-09-25T13:30:00Z',['credit_card_demo','debit_card_demo'],['txn_demo_91','txn_demo_92'])
    return service.verify(case['id'],CUSTOMER,1)
