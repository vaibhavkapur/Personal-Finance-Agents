import pytest
from backend.app.persistence.store import Store
from backend.app.adapters.mock import MockCustodian
from backend.app.workflows.service import WealthService

@pytest.fixture
def service(tmp_path):
    return WealthService(Store(str(tmp_path/'wealth.db')),MockCustodian(str(tmp_path/'provider.db')))

@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    from backend.app.api.main import create_app
    with TestClient(create_app(str(tmp_path/'api.db'),str(tmp_path/'api-provider.db'))) as client:
        client.headers['X-Wealth-Client']='northstar'
        client.post('/v1/session',json={})
        yield client
