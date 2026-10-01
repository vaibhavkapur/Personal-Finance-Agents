import pytest
from fastapi.testclient import TestClient
from backend.app.api.main import create_app
from backend.app.persistence.db import Database

@pytest.fixture
def env(tmp_path):
    database = Database('sqlite:///' + str(tmp_path / 'test.db'))
    app = create_app(database, embedded_worker=False)
    with TestClient(app) as client:
        session = client.post('/v1/demo/session', json={}).json()
        yield client, app.state.service, session['user']
    database.engine.dispose()
