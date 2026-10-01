from pathlib import Path
from datetime import timedelta
import json
import time
from app.persistence.store import Store as S
from app.domain.types import DomainError, TERMINAL, digest, instant, transition, uid, wall_time
from app.domain.market import add_seconds, evaluate, is_session, market_snapshot
from app.domain.risk import check_risk
from app.adapters.paper import MockBroker, SCENARIOS
from app.seed import seed


class Engine:
    def __init__(self, directory):
        self.store = S(Path(directory) / "desk.sqlite3")
        seed(self.store)
        with self.store.tx() as db:
            account = S.get(db, "accounts", "account_demo")
        self.broker = MockBroker(Path(directory) / "broker.sqlite3", account)

    def runtime(self, db):
        return S.get(db, "settings", "runtime")

    def account(self, db, tenant="demo"):
        return S.get(db, "accounts", "account_demo", tenant)

    def latest_market(self, db, tenant="demo"):
        latest = {}
        for snapshot in S.all(db, "market_snapshots", tenant):
            latest[snapshot["symbol"]] = snapshot
        return list(latest.values())

    def risk(self, db, signal, tenant="demo", exclude=None):
        mandate = S.get(db, "trading_mandates", signal["mandate_id"], tenant)
        snapshot = S.get(db, "market_snapshots", signal["snapshot_id"], tenant)
        return check_risk(mandate, self.account(db, tenant), self.runtime(db), signal, snapshot, self.latest_market(db, tenant), S.all(db, "exposure_reservations", tenant), S.all(db, "execution_fills", tenant), exclude)

    def change(self, db, order, state, detail=None, actor="engine", tenant="demo"):
        old = order["status"]
        if not transition(order, state):
            return
        now = self.runtime(db)["now"]
        order["updated_at"] = now
        S.put(db, "orders", order, tenant)
        case = S.get(db, "cases", order["case_id"], tenant)
        case.update(status=state, version=order["version"], updated_at=now)
        S.put(db, "cases", case, tenant)
        S.event(db, "order." + state, {"order_id": order["id"], "symbol": order["symbol"], "previous_state": old, "next_state": state, "expected_case_version": order["version"] - 1, "version": order["version"], "message": detail or f"{order['symbol']} order {state.replace('_', ' ')}", "broker_order_id": order.get("broker_order_id")}, now, order["case_id"], actor, tenant)

    def release(self, db, order, tenant="demo"):
        reservation = S.get(db, "exposure_reservations", order["id"], tenant)
        reservation.update(status="released", cash_minor=0, exposure_minor=0, released_at=self.runtime(db)["now"])
        S.put(db, "exposure_reservations", reservation, tenant)

    def idempotent(self, db, tenant, scope, key, payload):
        if not key or len(key) > 160:
            raise DomainError("A nonempty Idempotency-Key of at most 160 characters is required")
        row = db.execute("SELECT payload_hash,result_id FROM idempotency WHERE tenant=? AND scope=? AND key=?", (tenant, scope, key)).fetchone()
        if row and row[0] != digest(payload):
            raise DomainError("Idempotency key was already used with different content", 409)
        return row[1] if row else None

    def remember(self, db, tenant, scope, key, payload, result):
        db.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)", (tenant, scope, key, digest(payload), result))

    def evaluate_run(self, symbols=None, mandate_id=None, tenant="demo", case_id=None, snapshot_id=None):
        with self.store.tx() as db:
            account = self.account(db, tenant)
            mandate = S.get(db, "trading_mandates", mandate_id or account["active_mandate_id"], tenant)
            if symbols is None:
                symbols = mandate["symbols"]
            quotes = {q["symbol"]: q for q in self.latest_market(db, tenant)}
            if snapshot_id:
                bound_snapshot = S.get(db, "market_snapshots", snapshot_id, tenant)
                quotes[bound_snapshot["symbol"]] = bound_snapshot
            now = self.runtime(db)["now"]
            results = []
            for symbol in symbols:
                if symbol not in quotes:
                    raise DomainError(f"No fixture data for {symbol}")
                snapshot = quotes[symbol]
                market_snapshot(snapshot, now)
                signal = {"id": uid("sig"), "mandate_id": mandate["id"], "snapshot_id": snapshot["id"], "symbol": symbol, "created_at": now, **evaluate(snapshot, mandate)}
                S.put(db, "signals", signal, tenant)
                risk = self.risk(db, signal, tenant)
                signal["risk"] = risk
                S.put(db, "signals", signal, tenant)
                S.event(db, "signal.permitted" if risk["allowed"] else "signal.blocked", {"signal_id": signal["id"], "symbol": symbol, "message": signal["rationale"] if risk["allowed"] else "; ".join(c["detail"] for c in risk["reasons"]), "checks": risk["checks"]}, now, case_id, tenant=tenant)
                results.append(signal)
            return results

    def preview(self, signal_id, tenant="demo"):
        with self.store.tx() as db:
            signal = S.get(db, "signals", signal_id, tenant)
            result = {"id": uid("preview"), "signal_id": signal_id, "created_at": self.runtime(db)["now"], "expires_at": add_seconds(self.runtime(db)["now"], 60), **self.risk(db, signal, tenant)}
            S.put(db, "previews", result, tenant)
            return result

    def new_action(self, db, order, kind, payload, tenant):
        now = self.runtime(db)["now"]
        action = {"id": uid("action"), "order_id": order["id"], "case_id": order["case_id"], "type": kind, "payload": payload, "payload_hash": digest(payload), "status": "awaiting_approval", "challenge_id": uid("challenge"), "created_at": now, "expires_at": add_seconds(now, 120), "wall_expires_at": (instant(wall_time()) + timedelta(minutes=10)).isoformat(), "environment": "mock"}
        S.put(db, "actions", action, tenant)
        return action

    def prepare(self, signal_id, preview_id, scenario, key, tenant="demo"):
        payload = {"signal_id": signal_id, "preview_id": preview_id, "scenario": scenario}
        if scenario not in SCENARIOS:
            raise DomainError("Invalid simulator scenario")
        with self.store.tx() as db:
            previous = self.idempotent(db, tenant, "prepare", key, payload)
            if previous:
                return self.order_detail(db, previous, tenant)
            signal = S.get(db, "signals", signal_id, tenant)
            preview = S.get(db, "previews", preview_id, tenant)
            if preview["signal_id"] != signal_id:
                raise DomainError("Preview belongs to another signal", 409)
            now = self.runtime(db)["now"]
            if instant(now) >= instant(preview["expires_at"]):
                raise DomainError("Risk preview has expired", 409)
            existing = next((o for o in S.all(db, "orders", tenant) if o["signal_id"] == signal_id), None)
            if existing:
                raise DomainError("This signal already has an order; evaluate a new signal", 409)
            risk = self.risk(db, signal, tenant)
            if not risk["allowed"]:
                raise DomainError("Risk gateway blocked the order", 409, risk["reasons"])
            if any(preview[f] != risk[f] for f in ["quantity", "limit_price_minor", "cash_reservation_minor"]):
                raise DomainError("Order terms changed; create a new preview", 409)
            case_id, order_id = uid("run"), uid("order")
            case = {"id": case_id, "customer_id": "cus_demo_6", "mandate_id": signal["mandate_id"], "workflow_type": "paper_order", "status": "awaiting_approval", "version": 1, "created_at": now, "updated_at": now}
            S.put(db, "cases", case, tenant)
            order = {"id": order_id, "case_id": case_id, "signal_id": signal_id, "mandate_id": signal["mandate_id"], "client_order_id": "meridian_" + order_id, "symbol": signal["symbol"], "side": "buy", "order_type": "limit", "quantity": risk["quantity"], "limit_price_minor": risk["limit_price_minor"], "fee_buffer_minor": 100, "filled_quantity": 0, "fees_minor": 0, "status": "awaiting_approval", "version": 1, "created_at": now, "updated_at": now, "scenario": scenario, "currency": "USD", "environment": "mock", "risk_at_review": risk, "broker_order_id": None}
            immutable = {k: order[k] for k in ["id", "client_order_id", "symbol", "side", "order_type", "quantity", "limit_price_minor", "fee_buffer_minor", "mandate_id", "scenario", "currency", "environment"]}
            action = self.new_action(db, order, "submit_order", immutable, tenant)
            order["action_id"] = action["id"]
            S.put(db, "orders", order, tenant)
            S.put(db, "exposure_reservations", {"id": order_id, "order_id": order_id, "account_id": "account_demo", "symbol": order["symbol"], "cash_minor": risk["cash_reservation_minor"], "exposure_minor": risk["cash_reservation_minor"], "status": "active", "created_at": now, "valuation": risk["valuation"], "currency": "USD"}, tenant)
            S.event(db, "order.awaiting_approval", {"order_id": order_id, "symbol": order["symbol"], "message": f"Reserved ${risk['cash_reservation_minor'] / 100:,.2f} for {order['quantity']} {order['symbol']} shares. Exact-order review required.", "previous_state": "risk_checked", "next_state": "awaiting_approval", "version": 1}, now, case_id, tenant=tenant)
            self.remember(db, tenant, "prepare", key, payload, order_id)
            return self.order_detail(db, order_id, tenant)

    def order_detail(self, db, order_id, tenant="demo"):
        order = S.get(db, "orders", order_id, tenant)
        return {**order, "action": S.get(db, "actions", order["action_id"], tenant), "cancel_action": S.get(db, "actions", order["cancel_action_id"], tenant) if order.get("cancel_action_id") else None, "fills": [f for f in S.all(db, "execution_fills", tenant) if f["order_id"] == order_id], "reservation": S.get(db, "exposure_reservations", order_id, tenant), "source": "local-projection", "retrieved_at": self.runtime(db)["now"], "authority": "simulated"}

    def approve(self, action_id, version, payload_hash, challenge, key, actor="demo-reviewer", tenant="demo"):
        payload = {"action_id": action_id, "version": version, "payload_hash": payload_hash, "challenge": challenge}
        with self.store.tx() as db:
            previous = self.idempotent(db, tenant, "approve", key, payload)
            if previous:
                return S.get(db, "actions", previous, tenant)
            action = S.get(db, "actions", action_id, tenant)
            order = S.get(db, "orders", action["order_id"], tenant)
            now = self.runtime(db)["now"]
            if order["version"] != version:
                raise DomainError("Case version is stale; reload the review", 409)
            if action["status"] != "awaiting_approval":
                raise DomainError("Action is no longer awaiting approval", 409)
            if instant(now) >= instant(action["expires_at"]) or instant(wall_time()) >= instant(action["wall_expires_at"]):
                raise DomainError("Approval challenge has expired", 409)
            if challenge != action["challenge_id"] or payload_hash != action["payload_hash"] or payload_hash != digest(action["payload"]):
                raise DomainError("Action hash or challenge does not match", 409)
            if action["type"] == "submit_order":
                risk = self.risk(db, S.get(db, "signals", order["signal_id"], tenant), tenant, order["id"])
                if not risk["allowed"]:
                    raise DomainError("Authority or risk checks changed", 409, risk["reasons"])
                if order["status"] != "awaiting_approval":
                    raise DomainError("Order is no longer awaiting approval", 409)
                immutable_matches = all(order.get(k) == v for k, v in action["payload"].items())
                if not immutable_matches:
                    raise DomainError("Immutable order terms changed", 409)
            elif order["status"] not in {"accepted", "partially_filled"}:
                raise DomainError("Order is no longer cancellable", 409)
            approval = {"id": uid("approval"), "action_id": action_id, "action_hash": payload_hash, "approver": actor, "scope": action["type"], "expires_at": action["expires_at"], "revoked_at": None, "consumed_at": None, "created_at": now}
            S.put(db, "approvals", approval, tenant)
            action.update(status="approved", approval_id=approval["id"])
            S.put(db, "actions", action, tenant)
            db.execute("INSERT INTO jobs(id,action_id) VALUES(?,?)", (uid("job"), action_id))
            self.remember(db, tenant, "approve", key, payload, action_id)
            S.event(db, "action.approved", {"order_id": order["id"], "action_id": action_id, "action_hash": payload_hash, "message": f"{action['type'].replace('_', ' ').title()} approved by {actor}"}, now, order["case_id"], actor, tenant)
            return action

    def cancel_draft(self, order_id, key, tenant="demo"):
        payload = {"order_id": order_id}
        with self.store.tx() as db:
            previous = self.idempotent(db, tenant, "cancel", key, payload)
            if previous:
                return S.get(db, "actions", previous, tenant)
            order = S.get(db, "orders", order_id, tenant)
            if order["status"] not in {"accepted", "partially_filled"}:
                raise DomainError("Only accepted or partially filled orders can be cancelled", 409)
            if order.get("cancel_action_id"):
                old = S.get(db, "actions", order["cancel_action_id"], tenant)
                if old["status"] in {"awaiting_approval", "approved", "executing"}:
                    return old
            action = self.new_action(db, order, "cancel_order", {"order_id": order_id, "broker_order_id": order["broker_order_id"], "remaining_quantity_at_review": order["quantity"] - order["filled_quantity"], "filled_quantity_at_review": order["filled_quantity"], "symbol": order["symbol"], "destination": "meridian-paper-broker", "environment": "mock", "effect": "Cancel unfilled shares; fills racing with cancellation remain owned."}, tenant)
            order["cancel_action_id"] = action["id"]
            S.put(db, "orders", order, tenant)
            self.remember(db, tenant, "cancel", key, payload, action["id"])
            return action

    def discard(self, order_id, tenant="demo"):
        with self.store.tx() as db:
            order = S.get(db, "orders", order_id, tenant)
            action = S.get(db, "actions", order["action_id"], tenant)
            if order["status"] != "awaiting_approval" or action["status"] != "awaiting_approval":
                raise DomainError("Only an unapproved draft can be discarded", 409)
            self.change(db, order, "cancelled", "Unapproved local draft discarded", actor="demo-reviewer", tenant=tenant)
            self.release(db, order, tenant)
            action["status"] = "cancelled"
            S.put(db, "actions", action, tenant)
            return order

    def claim_job(self):
        with self.store.tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE state IN ('pending','running') AND lease_until<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',lease_until=?,attempts=attempts+1 WHERE id=?", (time.time() + 30, row["id"]))
            return dict(row)

    def execute_job(self, job):
        action_id = job["action_id"]
        # Persist authority consumption and intent before leaving the transaction.
        with self.store.tx() as db:
            action = S.get(db, "actions", action_id)
            order = S.get(db, "orders", action["order_id"])
            now = self.runtime(db)["now"]
            if action["status"] in {"completed", "expired", "cancelled", "failed"}:
                db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))
                return
            approval = S.get(db, "approvals", action["approval_id"])
            if not approval["consumed_at"]:
                invalid = approval["revoked_at"] or instant(now) >= instant(action["expires_at"]) or instant(wall_time()) >= instant(action["wall_expires_at"]) or action["payload_hash"] != digest(action["payload"]) or approval["action_hash"] != action["payload_hash"]
                reason = "Action authority expired or changed"
                if action["type"] == "submit_order":
                    risk = self.risk(db, S.get(db, "signals", order["signal_id"]), exclude=order["id"])
                    invalid = invalid or not risk["allowed"] or risk["quantity"] != order["quantity"] or risk["limit_price_minor"] != order["limit_price_minor"]
                    invalid = invalid or not all(order.get(k) == v for k, v in action["payload"].items())
                    if not risk["allowed"]:
                        reason = "; ".join(c["detail"] for c in risk["reasons"])
                else:
                    invalid = invalid or order["filled_quantity"] != action["payload"]["filled_quantity_at_review"] or order["status"] not in {"accepted", "partially_filled"}
                if invalid:
                    action.update(status="expired", error=reason)
                    S.put(db, "actions", action)
                    if action["type"] == "submit_order" and order["status"] == "awaiting_approval":
                        self.change(db, order, "expired", reason)
                        self.release(db, order)
                    db.execute("UPDATE jobs SET state='done',error=? WHERE id=?", (reason, job["id"]))
                    return
                approval["consumed_at"] = now
                S.put(db, "approvals", approval)
                action["status"] = "executing"
                S.put(db, "actions", action)
                self.change(db, order, "submitting" if action["type"] == "submit_order" else "cancel_requested")
        # Provider calls are outside the database transaction. Always query before retry.
        try:
            report = self.broker.get_order_by_client_id(order["client_order_id"])
            if action["type"] == "submit_order":
                if report is None:
                    # Recovery without a known order must recheck current authority.
                    with self.store.tx() as db:
                        current_risk = self.risk(db, S.get(db, "signals", order["signal_id"]), exclude=order["id"])
                        still_authorized = current_risk["allowed"] and instant(self.runtime(db)["now"]) < instant(action["expires_at"])
                    if not still_authorized:
                        raise DomainError("Submission unresolved and authority changed; manual review required", 409)
                    report = self.broker.submit_order({**action["payload"], "submitted_at": now}, order["client_order_id"])
            else:
                if report is None:
                    raise DomainError("Cannot resolve the original broker order", 409)
                report = self.broker.cancel_order(report["id"], action["id"])
            self.apply_report(order["id"], report)
            with self.store.tx() as db:
                action = S.get(db, "actions", action_id)
                action.update(status="completed", provider_reference=report["id"], completed_at=self.runtime(db)["now"])
                S.put(db, "actions", action)
                db.execute("UPDATE jobs SET state='done',error=NULL WHERE id=?", (job["id"],))
        except TimeoutError as exc:
            with self.store.tx() as db:
                order = S.get(db, "orders", order["id"])
                if order["status"] == "submitting":
                    self.change(db, order, "outcome_unknown", "Connection lost after submission. Reservation retained; lookup uses the original client order ID.")
                db.execute("UPDATE jobs SET state='pending',lease_until=?,error=? WHERE id=?", (time.time() + min(2 ** job["attempts"], 30), str(exc), job["id"]))
        except (DomainError, KeyError, TypeError, ValueError) as exc:
            with self.store.tx() as db:
                order = S.get(db, "orders", order["id"])
                if order["status"] not in TERMINAL:
                    self.change(db, order, "manual_review", str(exc))
                account = self.account(db)
                account["reconciliation_status"] = "review_required"
                S.put(db, "accounts", account)
                action = S.get(db, "actions", action_id)
                action.update(status="manual_review", error=str(exc))
                S.put(db, "actions", action)
                db.execute("UPDATE jobs SET state='review',error=? WHERE id=?", (str(exc), job["id"]))

    def apply_report(self, order_id, report, tenant="demo"):
        with self.store.tx() as db:
            order = S.get(db, "orders", order_id, tenant)
            if not report or report.get("environment") != "mock" or report.get("client_order_id") != order["client_order_id"]:
                raise DomainError("Broker response identity or environment is invalid")
            if report.get("symbol") != order["symbol"] or report.get("quantity") != order["quantity"] or report.get("limit_price_minor") != order["limit_price_minor"]:
                raise DomainError("Broker report disagrees with immutable order terms")
            fills = report.get("fills")
            if not isinstance(fills, list) or report.get("status") not in {"accepted", "partially_filled", "filled", "cancelled", "rejected"}:
                raise DomainError("Malformed execution report")
            if len({f["broker_execution_id"] for f in fills}) != len(fills):
                raise DomainError("Broker report repeats an execution ID")
            total = sum(f["quantity"] for f in fills)
            fees = sum(f["fee_minor"] for f in fills)
            if total > order["quantity"] or fees > order["fee_buffer_minor"] or (report["status"] == "filled" and total != order["quantity"]) or (report["status"] == "rejected" and total):
                raise DomainError("Execution totals are inconsistent with the order")
            if report["status"] == "accepted" and total or report["status"] == "partially_filled" and not 0 < total < order["quantity"]:
                raise DomainError("Execution state conflicts with filled quantity")
            stored = {f["broker_execution_id"]: f for f in S.all(db, "execution_fills", tenant)}
            for f in fills:
                if type(f["quantity"]) is not int or type(f["price_minor"]) is not int or type(f["fee_minor"]) is not int or f["quantity"] <= 0 or not 0 < f["price_minor"] <= order["limit_price_minor"] or f["fee_minor"] < 0 or instant(f["executed_at"]) > instant(self.runtime(db)["now"]):
                    raise DomainError("Invalid execution precision, price, fee, or timestamp")
                old = stored.get(f["broker_execution_id"])
                if old:
                    if old["order_id"] != order_id or any(old[k] != f[k] for k in ["quantity", "price_minor", "fee_minor", "executed_at"]):
                        raise DomainError("Execution ID reused with different content", 409)
                    continue
                if order["status"] in TERMINAL:
                    raise DomainError("New execution on a terminal order requires review", 409)
                fill = {**f, "id": uid("fill"), "order_id": order_id, "symbol": order["symbol"], "source": "meridian-paper-broker", "environment": "mock"}
                S.put(db, "execution_fills", fill, tenant)
                S.event(db, "execution.recorded", {"order_id": order_id, "execution_id": f["broker_execution_id"], "symbol": order["symbol"], "message": f"Filled {f['quantity']} {order['symbol']} at ${f['price_minor'] / 100:.2f}", "quantity": f["quantity"], "price_minor": f["price_minor"]}, self.runtime(db)["now"], order["case_id"], "broker-reconciler", tenant)
            known = [f for f in S.all(db, "execution_fills", tenant) if f["order_id"] == order_id]
            if sum(f["quantity"] for f in known) != total:
                raise DomainError("Authoritative report omitted a previously recorded execution", 409)
            order.update(broker_order_id=report["id"], filled_quantity=total, fees_minor=fees)
            if order["status"] not in TERMINAL:
                self.change(db, order, report["status"], actor="broker-reconciler", tenant=tenant)
            S.put(db, "orders", order, tenant)
            if report["status"] in TERMINAL:
                self.release(db, order, tenant)
            else:
                reservation = S.get(db, "exposure_reservations", order_id, tenant)
                remaining = (order["quantity"] - total) * order["limit_price_minor"] + order["fee_buffer_minor"] - fees
                reservation.update(cash_minor=remaining, exposure_minor=remaining)
                S.put(db, "exposure_reservations", reservation, tenant)
            self.rebuild_account(db, tenant)
            return order

    def rebuild_account(self, db, tenant="demo"):
        account = self.account(db, tenant)
        positions = {s: dict(p) for s, p in account["initial_positions"].items()}
        cash = account["initial_cash_minor"]
        for f in S.all(db, "execution_fills", tenant):
            p = positions.setdefault(f["symbol"], {"quantity": 0, "cost_minor": 0})
            value = f["quantity"] * f["price_minor"] + f["fee_minor"]
            p["quantity"] += f["quantity"]
            p["cost_minor"] += value
            cash -= value
        account.update(positions=positions, cash_minor=cash)
        S.put(db, "accounts", account, tenant)
        return account

    def reconcile(self, tenant="demo"):
        with self.store.tx() as db:
            orders = S.all(db, "orders", tenant)
        errors = []
        for order in orders:
            if order["status"] == "awaiting_approval":
                continue
            report = self.broker.get_order_by_client_id(order["client_order_id"])
            if report:
                try:
                    self.apply_report(order["id"], report, tenant)
                    # A lookup is completion evidence for a formerly uncertain submission.
                    with self.store.tx() as db:
                        action = S.get(db, "actions", order["action_id"], tenant)
                        if action["status"] in {"executing", "manual_review"}:
                            action.update(status="completed", provider_reference=report["id"], completed_at=self.runtime(db)["now"])
                            S.put(db, "actions", action, tenant)
                            db.execute("UPDATE jobs SET state='done',error=NULL WHERE action_id=?", (action["id"],))
                except DomainError as exc:
                    errors.append({"order_id": order["id"], "error": str(exc)})
        broker = self.broker.get_positions_and_cash()
        with self.store.tx() as db:
            account = self.rebuild_account(db, tenant)
            unresolved = any(o["status"] in {"outcome_unknown", "manual_review", "submitting"} for o in S.all(db, "orders", tenant))
            matched = not errors and not unresolved and account["cash_minor"] == broker["cash_minor"] and account["positions"] == broker["positions"]
            account["reconciliation_status"] = "matched" if matched else "review_required"
            S.put(db, "accounts", account, tenant)
            snapshot = {"id": uid("recon"), "as_of": self.runtime(db)["now"], "positions": account["positions"], "cash_minor": account["cash_minor"], "status": account["reconciliation_status"], "source": broker["source"], "environment": "mock", "errors": errors}
            S.put(db, "position_snapshots", snapshot, tenant)
            S.event(db, "account.reconciled", {"message": "Cash, positions, and unique executions match the broker ledger." if matched else "Ledger discrepancy or unresolved order; new submissions paused.", "evidence_reference": snapshot["id"], "status": snapshot["status"], "errors": errors}, snapshot["as_of"], tenant=tenant)
            return snapshot

    def expire_actions(self):
        with self.store.tx() as db:
            now = self.runtime(db)["now"]
            for action in S.all(db, "actions"):
                if action["status"] not in {"awaiting_approval", "approved"}:
                    continue
                order = S.get(db, "orders", action["order_id"])
                mandate = S.get(db, "trading_mandates", order["mandate_id"])
                expired = instant(now) >= instant(action["expires_at"]) or instant(wall_time()) >= instant(action["wall_expires_at"])
                if action["type"] == "submit_order":
                    expired = expired or mandate["revoked_at"] or instant(now) >= instant(mandate["expires_at"]) or self.account(db)["active_mandate_id"] != mandate["id"]
                if expired:
                    action["status"] = "expired"
                    S.put(db, "actions", action)
                    if action["type"] == "submit_order" and order["status"] == "awaiting_approval":
                        self.change(db, order, "expired", "Approval or mandate expired; unsubmitted reservation released")
                        self.release(db, order)

    def tick(self, max_jobs=10):
        self.expire_actions()
        count = 0
        while count < max_jobs:
            job = self.claim_job()
            if not job:
                break
            self.execute_job(job)
            count += 1
        with self.store.tx() as db:
            now = self.runtime(db)["now"]
        # Outbox's local consumer is the durable UI event journal, never a broker command.
        with self.store.tx() as db:
            db.execute("UPDATE outbox SET delivered_at=? WHERE delivered_at IS NULL", (now,))
        return {"processed": count}

    def set_kill(self, enabled):
        # Adapter first when pausing; local risk first when resuming. Both fail closed.
        if enabled:
            self.broker.set_kill_switch(True)
        with self.store.tx() as db:
            runtime = self.runtime(db)
            runtime["kill_switch"] = enabled
            S.put(db, "settings", runtime)
            S.event(db, "controls.paused" if enabled else "controls.resumed", {"message": "New paper submissions paused; existing orders remain reconcilable." if enabled else "New paper submissions enabled."}, runtime["now"], actor="demo-reviewer")
        if not enabled:
            self.broker.set_kill_switch(False)
        return runtime

    def advance_clock(self, seconds, refresh):
        with self.store.tx() as db:
            runtime = self.runtime(db)
            runtime["now"] = add_seconds(runtime["now"], seconds)
            S.put(db, "settings", runtime)
            if refresh:
                for snapshot in self.latest_market(db):
                    snapshot.update(id=uid("snapshot"), observed_at=runtime["now"], received_at=runtime["now"], market_session="regular" if is_session(runtime["now"]) else "closed")
                    S.put(db, "market_snapshots", snapshot)
            S.event(db, "replay.advanced", {"message": f"Replay advanced {seconds}s. {'Quotes refreshed at unchanged fixture prices.' if refresh else 'Quote timestamps unchanged.'}"}, runtime["now"], actor="demo-reviewer")
        self.broker.advance(runtime["now"])
        self.expire_actions()
        self.reconcile()
        return runtime

    def create_mandate(self, values, tenant="demo"):
        with self.store.tx() as db:
            account = self.account(db, tenant)
            previous = S.get(db, "trading_mandates", account["active_mandate_id"], tenant)
            symbols = {q["symbol"] for q in self.latest_market(db, tenant)}
            if not values["symbols"] or not set(values["symbols"]) <= symbols or len(values["symbols"]) != len(set(values["symbols"])):
                raise DomainError("Choose a nonempty, unique subset of fixture symbols")
            if instant(values["expires_at"]) <= instant(self.runtime(db)["now"]):
                raise DomainError("Mandate expiry must be in the future")
            mandate = {**previous, **values, "id": uid("mandate"), "version": previous["version"] + 1, "revoked_at": None, "created_at": self.runtime(db)["now"]}
            S.put(db, "trading_mandates", mandate, tenant)
            account["active_mandate_id"] = mandate["id"]
            S.put(db, "accounts", account, tenant)
            S.event(db, "mandate.versioned", {"message": f"Mandate v{mandate['version']} activated. Previous unsubmitted orders require a new review.", "mandate_id": mandate["id"]}, mandate["created_at"], actor="demo-reviewer", tenant=tenant)
        self.expire_actions()
        return mandate

    def revoke_mandate(self, mandate_id, tenant="demo"):
        with self.store.tx() as db:
            mandate = S.get(db, "trading_mandates", mandate_id, tenant)
            mandate["revoked_at"] = self.runtime(db)["now"]
            S.put(db, "trading_mandates", mandate, tenant)
            S.event(db, "mandate.revoked", {"message": "Mandate revoked. Existing broker orders remain open until explicitly cancelled or filled.", "mandate_id": mandate_id}, mandate["revoked_at"], actor="demo-reviewer", tenant=tenant)
        self.expire_actions()
        return mandate

    def dashboard(self, tenant="demo"):
        with self.store.tx() as db:
            account = self.account(db, tenant)
            market = self.latest_market(db, tenant)
            reservations = [r for r in S.all(db, "exposure_reservations", tenant) if r["status"] == "active"]
            quotes = {q["symbol"]: q for q in market}
            positions = [{"symbol": s, **p, "market_value_minor": p["quantity"] * quotes[s]["ask_minor"], "mark_minor": quotes[s]["ask_minor"], "unrealized_pnl_minor": p["quantity"] * quotes[s]["ask_minor"] - p["cost_minor"]} for s, p in account["positions"].items()]
            reserved = sum(r["cash_minor"] for r in reservations)
            orders = [self.order_detail(db, o["id"], tenant) for o in S.all(db, "orders", tenant)]
            events = [{**dict(row), "data": json.loads(row["data"])} for row in db.execute("SELECT * FROM case_events WHERE tenant=? ORDER BY seq DESC LIMIT 100", (tenant,))]
            jobs = [dict(row) for row in db.execute("SELECT id,action_id,state,attempts,error FROM jobs ORDER BY rowid DESC LIMIT 30")]
            runtime = self.runtime(db)
            return {"runtime": runtime, "account": account, "mandate": S.get(db, "trading_mandates", account["active_mandate_id"], tenant), "market": market, "positions": positions, "orders": list(reversed(orders)), "signals": list(reversed(S.all(db, "signals", tenant)[-30:])), "events": events, "jobs": jobs, "tool_runs": list(reversed(S.all(db, "tool_runs", tenant)[-20:])), "metrics": {"equity_minor": account["cash_minor"] + sum(p["market_value_minor"] for p in positions), "exposure_minor": sum(p["market_value_minor"] for p in positions), "reserved_minor": reserved, "buying_power_minor": account["cash_minor"] - reserved, "unrealized_pnl_minor": sum(p["unrealized_pnl_minor"] for p in positions), "pending_approvals": sum(a["status"] == "awaiting_approval" for a in S.all(db, "actions", tenant)), "fees_minor": sum(f["fee_minor"] for f in S.all(db, "execution_fills", tenant))}, "source": "meridian-synthetic-replay", "retrieved_at": runtime["now"], "authority": "simulated", "environment": "mock"}
