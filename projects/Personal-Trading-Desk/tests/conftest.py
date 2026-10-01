import pytest
from app.workflows.engine import Engine
from app.domain.types import uid
from app.persistence.store import Store as S


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path)


def prepare(engine, scenario="full", symbol="AAPL"):
    signal = engine.evaluate_run([symbol])[0]
    preview = engine.preview(signal["id"])
    return engine.prepare(signal["id"], preview["id"], scenario, uid("request"))


def approve(engine, order, action=None):
    action = action or order["action"]
    return engine.approve(action["id"], order["version"], action["payload_hash"], action["challenge_id"], uid("approval-request"))


def get_order(engine, order):
    with engine.store.tx() as db:
        return engine.order_detail(db, order["id"])


def update_record(engine, table, id, fn):
    with engine.store.tx() as db:
        value = S.get(db, table, id)
        fn(value)
        S.put(db, table, value)
