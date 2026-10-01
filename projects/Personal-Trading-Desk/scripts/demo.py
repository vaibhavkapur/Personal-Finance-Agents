"""Isolated synthetic demonstrations. Approvals here represent a test reviewer."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
from app.workflows.engine import Engine
from app.persistence.store import Store as S
from app.domain.types import DomainError, uid


def draft(e, scenario):
    s = e.evaluate_run(["AAPL"])[0]
    p = e.preview(s["id"])
    return e.prepare(s["id"], p["id"], scenario, uid("demo"))


def approve(e, o, a=None):
    a = a or o["action"]
    e.approve(a["id"], o["version"], a["payload_hash"], a["challenge_id"], uid("demo-reviewer"), actor="fixture-reviewer")


def main():
    with TemporaryDirectory(prefix="meridian-demo-") as d:
        e = Engine(d)
        o = draft(e, "full")
        approve(e, o)
        e.tick()
        assert e.dashboard()["orders"][0]["status"] == "filled"
        assert e.reconcile()["status"] == "matched"
        print("1. Permitted order: exact approval → full fill → matched cash and positions.")
    with TemporaryDirectory(prefix="meridian-demo-") as d:
        e = Engine(d)
        with e.store.tx() as db:
            m = S.get(db, "trading_mandates", "mandate_v1")
            m["limits"]["per_symbol_minor"] = 600000
            S.put(db, "trading_mandates", m)
        draft(e, "resting")
        try:
            draft(e, "resting")
            raise AssertionError("Expected a reservation block")
        except DomainError as exc:
            print("2. Exposure blocked: " + exc.reasons[0]["detail"])
    with TemporaryDirectory(prefix="meridian-demo-") as d:
        e = Engine(d)
        o = draft(e, "cancel_race")
        approve(e, o)
        e.tick()
        partial = e.dashboard()["orders"][0]
        a = e.cancel_draft(o["id"], uid("cancel"))
        approve(e, partial, a)
        # Inject a lost response after the broker processes the approved cancellation.
        original = e.broker.cancel_order
        def lost_response(broker_id, request_ref):
            original(broker_id, request_ref)
            raise TimeoutError("fixture connection loss after cancellation")
        e.broker.cancel_order = lost_response
        e.tick()
        assert e.dashboard()["orders"][0]["status"] == "cancel_requested"
        resumed = Engine(d)
        with resumed.store.tx() as db:
            db.execute("UPDATE jobs SET lease_until=0")
        resumed.tick()
        result = resumed.dashboard()["orders"][0]
        assert result["status"] == "cancelled" and result["filled_quantity"] == 3
        assert resumed.reconcile()["status"] == "matched"
        assert resumed.broker.get_positions_and_cash()["order_count"] == 1
        print("3. Partial fill + cancel race + connection loss: restart recovered one broker order, 3 filled shares, zero residual reservation.")
    print("All three demonstrations passed. Mock data only; no external actions.")


if __name__ == "__main__":
    main()
