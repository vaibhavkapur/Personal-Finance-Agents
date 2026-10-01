import json
from pathlib import Path
import pytest
from scripts.evaluate import evaluate_fixture
CORPUS=json.loads((Path(__file__).parents[1]/'fixtures/evaluations.json').read_text())
@pytest.mark.parametrize('item',CORPUS['cases'],ids=lambda item:item['id'])
def test_labeled_case(item,tmp_path):
    assert evaluate_fixture(item,tmp_path)['passed']
