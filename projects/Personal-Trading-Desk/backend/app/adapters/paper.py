"""Independent persistent broker ledger. No local projection is used as broker truth."""
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
import json
import sqlite3
from app.domain.types import DomainError, uid

SCENARIOS = {"full", "partial", "resting", "rejected", "timeout", "cancel_race", "malformed", "delayed"}


class PaperBrokerAdapter(Protocol):
    def submit_order(self, order: dict, client_order_id: str) -> dict: ...
    def get_order_by_client_id(self, client_order_id: str) -> dict | None: ...
    def cancel_order(self, broker_order_id: str, request_ref: str) -> dict: ...
    def get_positions_and_cash(self) -> dict: ...


class MockBroker:
    environment = "mock"
    capabilities = {"lookup_by_client_id": True, "cancel_lookup": True, "partial_fills": True, "live_trading": False}

    def __init__(self, path, initial_account):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS broker_orders(client_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS broker_meta(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO broker_meta VALUES('initial',?)", (json.dumps(initial_account),))
            db.execute("INSERT OR IGNORE INTO broker_meta VALUES('kill', 'false')")

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def set_kill_switch(self, enabled):
        with self.tx() as db:
            db.execute("UPDATE broker_meta SET body=? WHERE id='kill'", (json.dumps(enabled),))

    @staticmethod
    def save(db, order):
        db.execute("INSERT INTO broker_orders VALUES(?,?) ON CONFLICT(client_id) DO UPDATE SET body=excluded.body", (order["client_order_id"], json.dumps(order)))

    @staticmethod
    def fill(order, quantity, now):
        remaining = order["quantity"] - sum(f["quantity"] for f in order["fills"])
        quantity = min(quantity, remaining)
        if quantity <= 0:
            return
        # Conservative limit-price fills, no price improvement; $0.01/share, <= $1/order.
        used_fee = sum(f["fee_minor"] for f in order["fills"])
        order["fills"].append({"broker_execution_id": f"{order['id']}_execution_{len(order['fills']) + 1}", "quantity": quantity, "price_minor": order["limit_price_minor"], "fee_minor": min(quantity, 100 - used_fee), "executed_at": now, "currency": "USD"})
        order["status"] = "filled" if quantity == remaining else "partially_filled"

    def submit_order(self, payload, client_order_id):
        with self.tx() as db:
            existing = db.execute("SELECT body FROM broker_orders WHERE client_id=?", (client_order_id,)).fetchone()
            if existing:
                return json.loads(existing[0])
            scenario = payload["scenario"]
            if scenario not in SCENARIOS:
                raise DomainError("Unsupported simulator scenario")
            killed = json.loads(db.execute("SELECT body FROM broker_meta WHERE id='kill'").fetchone()[0])
            order = {**payload, "id": uid("paper"), "client_order_id": client_order_id, "status": "accepted", "fills": [], "cancel_requests": [], "environment": "mock", "source": "meridian-paper-broker", "authority": "simulated", "accepted_at": payload["submitted_at"]}
            if killed or scenario == "rejected":
                order["status"] = "rejected"
                order["reason"] = "Adapter kill switch is enabled" if killed else "Injected broker rejection"
            elif scenario in {"full", "timeout"}:
                self.fill(order, order["quantity"], payload["submitted_at"])
            elif scenario in {"partial", "cancel_race"}:
                self.fill(order, max(1, order["quantity"] // 2), payload["submitted_at"])
            self.save(db, order)
        if scenario == "timeout" and not killed:
            raise TimeoutError("Injected connection loss after broker acceptance")
        if scenario == "malformed" and not killed:
            return {"unexpected": "Injected malformed response", "environment": "mock"}
        return order

    def get_order_by_client_id(self, client_order_id):
        with self.tx() as db:
            row = db.execute("SELECT body FROM broker_orders WHERE client_id=?", (client_order_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def cancel_order(self, broker_order_id, request_ref):
        with self.tx() as db:
            orders = [json.loads(r[0]) for r in db.execute("SELECT body FROM broker_orders")]
            order = next((o for o in orders if o["id"] == broker_order_id), None)
            if not order:
                raise DomainError("Broker order not found", 404)
            if request_ref in order["cancel_requests"]:
                return order
            order["cancel_requests"].append(request_ref)
            if order["status"] not in {"filled", "rejected", "cancelled"}:
                if order["scenario"] == "cancel_race":
                    self.fill(order, 1, order["submitted_at"])
                if order["status"] != "filled":
                    order["status"] = "cancelled"
            self.save(db, order)
            return order

    def advance(self, now):
        """A deterministic next-tick callback for delayed executions."""
        with self.tx() as db:
            for row in db.execute("SELECT body FROM broker_orders").fetchall():
                order = json.loads(row[0])
                if order["scenario"] == "delayed" and order["status"] == "accepted":
                    self.fill(order, order["quantity"], now)
                    self.save(db, order)

    def get_positions_and_cash(self):
        with self.tx() as db:
            initial = json.loads(db.execute("SELECT body FROM broker_meta WHERE id='initial'").fetchone()[0])
            positions = {s: dict(v) for s, v in initial["initial_positions"].items()}
            cash = initial["initial_cash_minor"]
            orders = [json.loads(r[0]) for r in db.execute("SELECT body FROM broker_orders")]
            for order in orders:
                for f in order["fills"]:
                    p = positions.setdefault(order["symbol"], {"quantity": 0, "cost_minor": 0})
                    value = f["quantity"] * f["price_minor"] + f["fee_minor"]
                    p["quantity"] += f["quantity"]
                    p["cost_minor"] += value
                    cash -= value
            return {"cash_minor": cash, "positions": positions, "order_count": len(orders), "environment": "mock", "source": "meridian-paper-broker"}
