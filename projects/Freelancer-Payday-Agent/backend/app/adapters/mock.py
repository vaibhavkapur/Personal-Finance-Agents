"""Independent, durable simulated bank. No application transaction encloses a call."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from ..domain.engine import DomainError, digest
from ..persistence.store import uid


class FreelancerBankAdapter(Protocol):
    def get_available_cash(self, account_id: str) -> dict: ...
    def get_posted_transactions(self, account_id: str, cursor: str = "") -> dict: ...
    def submit_same_owner_transfer(self, payload: dict, request_ref: str, now: str) -> dict: ...
    def find_transfer(self, request_ref: str) -> dict: ...


class MockBank:
    environment = "mock"
    capabilities = {"lookup_by_reference": True, "same_owner_transfer": True, "returns": True,
                    "delayed_posting": True, "live_transfers": False}

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, owner TEXT NOT NULL, balance INTEGER NOT NULL, holds INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS transfers(ref TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, provider_ref TEXT NOT NULL UNIQUE, mode TEXT NOT NULL, created_at TEXT NOT NULL, debit_ref TEXT, credit_ref TEXT, return_debit_ref TEXT, return_credit_ref TEXT);
            CREATE TABLE IF NOT EXISTS movements(id TEXT PRIMARY KEY, account TEXT NOT NULL, amount INTEGER NOT NULL, reference TEXT NOT NULL, posted_at TEXT NOT NULL);
            ''')

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seed(self, business, personal, owner, available=1000000, holds=0):
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,1)", (business, owner, available + holds, holds))
            db.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,1)", (personal, owner, 125000, 0))

    def get_available_cash(self, account_id):
        with self.tx() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise DomainError("Bank account not found.", 404)
            return {"account_id": account_id, "available_minor": row["balance"] - row["holds"],
                    "balance_minor": row["balance"], "holds_minor": row["holds"], "version": row["version"],
                    "owner": row["owner"], "currency": "USD", "environment": "mock", "source": "mock_bank",
                    "authority": "simulated"}

    def get_posted_transactions(self, account_id, cursor=""):
        with self.tx() as db:
            rows = db.execute("SELECT * FROM movements WHERE account=? ORDER BY rowid", (account_id,)).fetchall()
            return {"transactions": [dict(r) for r in rows], "environment": "mock", "source": "mock_bank"}

    def find_transfer(self, request_ref):
        with self.tx() as db:
            row = db.execute("SELECT * FROM transfers WHERE ref=?", (request_ref,)).fetchone()
            if row:
                result = dict(row)
                result["payload"] = json.loads(result["payload"])
                return {**result, "environment": "mock", "source": "mock_bank", "authority": "simulated"}
            return {"status": "not_found", "ref": request_ref, "environment": "mock", "authority": "simulated"}

    def submit_same_owner_transfer(self, payload, request_ref, now):
        with self.tx() as db:
            existing = db.execute("SELECT payload FROM transfers WHERE ref=?", (request_ref,)).fetchone()
            if existing:
                if digest(json.loads(existing[0])) != digest(payload):
                    raise DomainError("Provider reference reused with changed content.")
            else:
                src = db.execute("SELECT * FROM accounts WHERE id=?", (payload["source_account_id"],)).fetchone()
                dst = db.execute("SELECT * FROM accounts WHERE id=?", (payload["destination_account_id"],)).fetchone()
                if not src or not dst or src["owner"] != dst["owner"] or src["id"] == dst["id"]:
                    raise DomainError("Same-owner destination verification failed.")
                outstanding = sum(json.loads(r[0])["amount_minor"] for r in db.execute("SELECT payload FROM transfers WHERE status='accepted'").fetchall() if json.loads(r[0])["source_account_id"] == src["id"])
                mode = payload["simulation_mode"]
                status = "failed" if mode == "decline" or payload["amount_minor"] > src["balance"] - src["holds"] - outstanding else "accepted"
                db.execute("INSERT INTO transfers(ref,payload,status,provider_ref,mode,created_at) VALUES(?,?,?,?,?,?)",
                           (request_ref, json.dumps(payload), status, uid("mock_tx"), mode, now))
        if payload["simulation_mode"] == "timeout":
            raise TimeoutError("Simulator accepted the transfer before the connection timed out.")
        if payload["simulation_mode"] == "malformed":
            return {"unexpected": "invalid provider response", "environment": "mock"}
        if payload["simulation_mode"] == "normal":
            self.post(request_ref, now)
        return self.find_transfer(request_ref)

    def _movement(self, db, account, amount, ref, now):
        ident = uid("bank_entry")
        db.execute("INSERT INTO movements VALUES(?,?,?,?,?)", (ident, account, amount, ref, now))
        db.execute("UPDATE accounts SET balance=balance+?,version=version+1 WHERE id=?", (amount, account))
        return ident

    def post(self, ref, now):
        with self.tx() as db:
            row = db.execute("SELECT * FROM transfers WHERE ref=?", (ref,)).fetchone()
            if not row or row["status"] != "accepted":
                return
            p = json.loads(row["payload"])
            debit = self._movement(db, p["source_account_id"], -p["amount_minor"], ref, now)
            credit = self._movement(db, p["destination_account_id"], p["amount_minor"], ref, now)
            db.execute("UPDATE transfers SET status='posted',debit_ref=?,credit_ref=? WHERE ref=?", (debit, credit, ref))

    def return_transfer(self, ref, now):
        with self.tx() as db:
            row = db.execute("SELECT * FROM transfers WHERE ref=?", (ref,)).fetchone()
            if not row or row["status"] != "posted":
                raise DomainError("Only a posted transfer can be returned.")
            p = json.loads(row["payload"])
            debit = self._movement(db, p["destination_account_id"], -p["amount_minor"], ref + ":return", now)
            credit = self._movement(db, p["source_account_id"], p["amount_minor"], ref + ":return", now)
            db.execute("UPDATE transfers SET status='returned',return_debit_ref=?,return_credit_ref=? WHERE ref=?", (debit, credit, ref))
        return self.find_transfer(ref)

    def deposit(self, account, amount, ref, now):
        with self.tx() as db:
            existing = db.execute("SELECT id,amount FROM movements WHERE reference=? AND account=?", (ref, account)).fetchone()
            if existing:
                if existing["amount"] != amount:
                    raise DomainError("Receipt reference reused with changed amount.")
                return existing["id"]
            return self._movement(db, account, amount, ref, now)

    def set_holds(self, account, holds):
        with self.tx() as db:
            db.execute("UPDATE accounts SET holds=?,version=version+1 WHERE id=?", (holds, account))

    def settle_pending(self, now):
        with self.tx() as db:
            refs = [r[0] for r in db.execute("SELECT ref FROM transfers WHERE status='accepted'").fetchall()]
        for ref in refs:
            self.post(ref, now)
        return refs
