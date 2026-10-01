import json
from pathlib import Path
import pytest
from backend.app.agent.evaluate import evaluate_fixture

FIXTURES = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/evaluation.json').read_text())['cases']


@pytest.mark.parametrize('fixture', FIXTURES, ids=[f['id'] for f in FIXTURES])
def test_labeled_fixture(fixture):
    result = evaluate_fixture(fixture)
    assert result['passed'], result
