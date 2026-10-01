"""Typed repositories over the Database.

Each repository converts between Pydantic records and rows. JSON columns are
suffixed `_json` in the schema and exposed as plain attributes on the models.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Type, TypeVar

from pydantic import BaseModel

from ..domain.errors import Conflict, NotFound, StaleVersion
from ..domain.models import (
    Action,
    Approval,
    CaseEvent,
    CreditMatch,
    Customer,
    Document,
    Job,
    Merchant,
    OutboundPacket,
    PaymentInstrument,
    PurchaseRecord,
    RecoveryCase,
    RecoveryChannel,
    RefundPromise,
    Transaction,
)
from .db import Database

T = TypeVar("T", bound=BaseModel)

# model attribute -> json column
JSON_FIELDS: Dict[str, Dict[str, str]] = {
    "documents": {"extracted": "extracted_json"},
    "recovery_cases": {"pending_question": "pending_question_json"},
    "outbound_packets": {"attachment_manifest": "attachment_manifest_json"},
    "actions": {"payload": "payload_json"},
    "approvals": {"scope": "scope_json"},
    "case_events": {"data": "data_json"},
    "jobs": {"payload": "payload_json"},
}


def _to_row(table: str, model: BaseModel) -> Dict[str, Any]:
    data = model.model_dump(mode="json")
    for attr, col in JSON_FIELDS.get(table, {}).items():
        value = data.pop(attr)
        data[col] = None if value is None else json.dumps(value, sort_keys=True)
    return data


def _from_row(table: str, model_cls: Type[T], row: Dict[str, Any]) -> T:
    data = dict(row)
    for attr, col in JSON_FIELDS.get(table, {}).items():
        raw = data.pop(col, None)
        data[attr] = json.loads(raw) if raw else ({} if attr != "pending_question" else None)
        if attr in ("attachment_manifest",) and not data[attr]:
            data[attr] = []
    return model_cls.model_validate(data)


class Repositories:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- generic -------------------------------------------------------------
    def _get(self, table: str, model_cls: Type[T], id_: str) -> Optional[T]:
        row = self.db.fetch_one(f"SELECT * FROM {table} WHERE id = ?", (id_,))
        return _from_row(table, model_cls, row) if row else None

    def _require(self, table: str, model_cls: Type[T], id_: str) -> T:
        obj = self._get(table, model_cls, id_)
        if obj is None:
            raise NotFound(f"{model_cls.__name__} {id_} not found")
        return obj

    def _insert(self, table: str, model: BaseModel) -> None:
        try:
            self.db.insert(table, _to_row(table, model))
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"{table}: {exc}", code="integrity_violation") from exc

    def _list(self, table: str, model_cls: Type[T], where: str = "1=1", params: Iterable[Any] = (), order: str = "rowid") -> List[T]:
        rows = self.db.fetch_all(f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", list(params))
        return [_from_row(table, model_cls, r) for r in rows]

    # -- reference data ------------------------------------------------------
    def upsert_customer(self, c: Customer) -> None:
        self.db.execute("INSERT OR REPLACE INTO customers(id, tenant_id, display_name, email) VALUES (?,?,?,?)", (c.id, c.tenant_id, c.display_name, c.email))

    def get_customer(self, id_: str) -> Customer:
        return self._require("customers", Customer, id_)

    def upsert_instrument(self, p: PaymentInstrument) -> None:
        self.db.execute("INSERT OR REPLACE INTO payment_instruments(id, customer_id, network, last4, issuer_id) VALUES (?,?,?,?,?)", (p.id, p.customer_id, p.network, p.last4, p.issuer_id))

    def get_instrument(self, id_: str) -> PaymentInstrument:
        return self._require("payment_instruments", PaymentInstrument, id_)

    def upsert_merchant(self, m: Merchant) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO merchants(id, name, support_email, support_channel, contact_verified_at, contact_source) VALUES (?,?,?,?,?,?)",
            (m.id, m.name, m.support_email, m.support_channel, m.contact_verified_at, m.contact_source),
        )

    def get_merchant(self, id_: str) -> Merchant:
        return self._require("merchants", Merchant, id_)

    # -- documents -----------------------------------------------------------
    def add_document(self, d: Document) -> None:
        self._insert("documents", d)

    def get_document(self, id_: str) -> Document:
        return self._require("documents", Document, id_)

    def documents_for_customer(self, customer_id: str) -> List[Document]:
        return self._list("documents", Document, "customer_id = ?", (customer_id,))

    # -- purchases / promises ------------------------------------------------
    def add_purchase(self, p: PurchaseRecord) -> None:
        self._insert("purchase_records", p)

    def get_purchase(self, id_: str) -> PurchaseRecord:
        return self._require("purchase_records", PurchaseRecord, id_)

    def find_purchase_by_order(self, customer_id: str, order_ref: str) -> Optional[PurchaseRecord]:
        row = self.db.fetch_one("SELECT * FROM purchase_records WHERE customer_id = ? AND order_ref = ?", (customer_id, order_ref))
        return _from_row("purchase_records", PurchaseRecord, row) if row else None

    def purchases_for_customer(self, customer_id: str) -> List[PurchaseRecord]:
        return self._list("purchase_records", PurchaseRecord, "customer_id = ?", (customer_id,))

    def add_promise(self, p: RefundPromise) -> None:
        self._insert("refund_promises", p)

    def update_promise(self, p: RefundPromise) -> None:
        row = _to_row("refund_promises", p)
        self.db.update("refund_promises", {"id": p.id}, {k: v for k, v in row.items() if k != "id"})

    def get_promise(self, id_: str) -> RefundPromise:
        return self._require("refund_promises", RefundPromise, id_)

    def promises_for_purchase(self, purchase_id: str) -> List[RefundPromise]:
        return self._list("refund_promises", RefundPromise, "purchase_id = ?", (purchase_id,), order="promised_at")

    # -- transactions --------------------------------------------------------
    def add_transaction(self, t: Transaction) -> bool:
        """Insert; returns False when the same (source, provider_ref) already exists."""
        try:
            self.db.insert("transactions", _to_row("transactions", t))
            return True
        except sqlite3.IntegrityError:
            return False

    def get_transaction(self, id_: str) -> Transaction:
        return self._require("transactions", Transaction, id_)

    def transactions_for_instrument(self, customer_id: str, instrument_ref: str) -> List[Transaction]:
        return self._list("transactions", Transaction, "customer_id = ? AND payment_instrument_ref = ?", (customer_id, instrument_ref), order="posted_at, rowid")

    def transactions_for_customer(self, customer_id: str) -> List[Transaction]:
        return self._list("transactions", Transaction, "customer_id = ?", (customer_id,), order="posted_at, rowid")

    # -- cases ---------------------------------------------------------------
    def add_case(self, c: RecoveryCase) -> None:
        self._insert("recovery_cases", c)

    def get_case(self, id_: str) -> RecoveryCase:
        return self._require("recovery_cases", RecoveryCase, id_)

    def save_case(self, c: RecoveryCase, expected_version: int) -> RecoveryCase:
        """Optimistic concurrency: bump version only if it still matches."""
        updated = c.model_copy(update={"version": expected_version + 1})
        row = _to_row("recovery_cases", updated)
        n = self.db.execute(
            "UPDATE recovery_cases SET status=?, version=?, final_recovered_minor=?, provisional_minor=?, store_credit_minor=?, reversed_minor=?, "
            "pending_question_json=?, completion_evidence_ref=?, outcome_note=?, promise_id=?, target_minor=?, updated_at=? WHERE id=? AND version=?",
            (row["status"], row["version"], row["final_recovered_minor"], row["provisional_minor"], row["store_credit_minor"], row["reversed_minor"],
             row["pending_question_json"], row["completion_evidence_ref"], row["outcome_note"], row["promise_id"], row["target_minor"], row["updated_at"], c.id, expected_version),
        ).rowcount
        if n != 1:
            raise StaleVersion(f"Case {c.id} version {expected_version} is stale")
        return updated

    def cases_for_customer(self, customer_id: str) -> List[RecoveryCase]:
        return self._list("recovery_cases", RecoveryCase, "customer_id = ?", (customer_id,), order="created_at, rowid")

    def cases_for_purchase(self, purchase_id: str) -> List[RecoveryCase]:
        return self._list("recovery_cases", RecoveryCase, "purchase_id = ?", (purchase_id,))

    def all_cases(self) -> List[RecoveryCase]:
        return self._list("recovery_cases", RecoveryCase)

    # -- channels ------------------------------------------------------------
    def add_channel(self, ch: RecoveryChannel) -> None:
        self._insert("recovery_channels", ch)

    def save_channel(self, ch: RecoveryChannel) -> None:
        row = _to_row("recovery_channels", ch)
        try:
            self.db.update("recovery_channels", {"id": ch.id}, {k: v for k, v in row.items() if k != "id"})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"recovery_channels: {exc}", code="integrity_violation") from exc

    def get_channel(self, id_: str) -> RecoveryChannel:
        return self._require("recovery_channels", RecoveryChannel, id_)

    def channels_for_case(self, case_id: str) -> List[RecoveryChannel]:
        return self._list("recovery_channels", RecoveryChannel, "case_id = ?", (case_id,))

    def channel_by_provider_ref(self, provider: str, ref: str) -> Optional[RecoveryChannel]:
        row = self.db.fetch_one("SELECT * FROM recovery_channels WHERE provider = ? AND provider_case_ref = ?", (provider, ref))
        return _from_row("recovery_channels", RecoveryChannel, row) if row else None

    # -- matches -------------------------------------------------------------
    def add_match(self, m: CreditMatch) -> None:
        self._insert("credit_matches", m)

    def save_match(self, m: CreditMatch) -> None:
        row = _to_row("credit_matches", m)
        self.db.update("credit_matches", {"id": m.id}, {k: v for k, v in row.items() if k != "id"})

    def matches_for_case(self, case_id: str) -> List[CreditMatch]:
        return self._list("credit_matches", CreditMatch, "case_id = ?", (case_id,))

    def matches_for_transaction(self, transaction_id: str) -> List[CreditMatch]:
        return self._list("credit_matches", CreditMatch, "transaction_id = ?", (transaction_id,))

    # -- packets / actions / approvals -------------------------------------
    def add_packet(self, p: OutboundPacket) -> None:
        self._insert("outbound_packets", p)

    def save_packet(self, p: OutboundPacket) -> None:
        row = _to_row("outbound_packets", p)
        self.db.update("outbound_packets", {"id": p.id}, {k: v for k, v in row.items() if k != "id"})

    def packet_for_action(self, action_id: str) -> Optional[OutboundPacket]:
        row = self.db.fetch_one("SELECT * FROM outbound_packets WHERE action_id = ?", (action_id,))
        return _from_row("outbound_packets", OutboundPacket, row) if row else None

    def add_action(self, a: Action) -> None:
        self._insert("actions", a)

    def save_action(self, a: Action) -> None:
        row = _to_row("actions", a)
        self.db.update("actions", {"id": a.id}, {k: v for k, v in row.items() if k != "id"})

    def get_action(self, id_: str) -> Action:
        return self._require("actions", Action, id_)

    def action_by_idempotency_key(self, key: str) -> Optional[Action]:
        row = self.db.fetch_one("SELECT * FROM actions WHERE idempotency_key = ?", (key,))
        return _from_row("actions", Action, row) if row else None

    def actions_for_case(self, case_id: str) -> List[Action]:
        return self._list("actions", Action, "case_id = ?", (case_id,))

    def actions_with_status(self, statuses: Iterable[str]) -> List[Action]:
        statuses = list(statuses)
        marks = ",".join("?" for _ in statuses)
        return self._list("actions", Action, f"status IN ({marks})", statuses)

    def add_approval(self, a: Approval) -> None:
        self._insert("approvals", a)

    def save_approval(self, a: Approval) -> None:
        row = _to_row("approvals", a)
        self.db.update("approvals", {"id": a.id}, {k: v for k, v in row.items() if k != "id"})

    def get_approval(self, id_: str) -> Approval:
        return self._require("approvals", Approval, id_)

    def approval_for_action(self, action_id: str) -> Optional[Approval]:
        row = self.db.fetch_one("SELECT * FROM approvals WHERE action_id = ? ORDER BY rowid DESC LIMIT 1", (action_id,))
        return _from_row("approvals", Approval, row) if row else None

    # -- events --------------------------------------------------------------
    def next_sequence(self, case_id: str) -> int:
        row = self.db.fetch_one("SELECT COALESCE(MAX(sequence), 0) AS s FROM case_events WHERE case_id = ?", (case_id,))
        return int(row["s"]) + 1 if row else 1

    def add_case_event(self, e: CaseEvent) -> None:
        self._insert("case_events", e)

    def events_for_case(self, case_id: str) -> List[CaseEvent]:
        return self._list("case_events", CaseEvent, "case_id = ?", (case_id,), order="sequence")

    def recent_events(self, limit: int = 100) -> List[CaseEvent]:
        rows = self.db.fetch_all("SELECT * FROM case_events ORDER BY rowid DESC LIMIT ?", (limit,))
        return [_from_row("case_events", CaseEvent, r) for r in rows]

    # -- jobs ----------------------------------------------------------------
    def add_job(self, j: Job) -> bool:
        try:
            self.db.insert("jobs", _to_row("jobs", j))
            return True
        except sqlite3.IntegrityError:
            return False  # dedupe_key already scheduled

    def save_job(self, j: Job) -> None:
        row = _to_row("jobs", j)
        self.db.update("jobs", {"id": j.id}, {k: v for k, v in row.items() if k != "id"})

    def get_job(self, id_: str) -> Job:
        return self._require("jobs", Job, id_)

    def due_jobs(self, now_iso: str) -> List[Job]:
        return self._list(
            "jobs", Job,
            "status IN ('pending','running') AND run_at <= ? AND (lease_until IS NULL OR lease_until < ?)",
            (now_iso, now_iso), order="run_at, rowid",
        )

    def jobs_for_case(self, case_id: str) -> List[Job]:
        return self._list("jobs", Job, "case_id = ?", (case_id,))

    def all_jobs(self) -> List[Job]:
        return self._list("jobs", Job)

    # -- tool runs / provider requests (operator view) ----------------------
    def add_tool_run(self, row: Dict[str, Any]) -> None:
        self.db.insert("tool_runs", row)

    def tool_runs(self, case_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if case_id:
            return self.db.fetch_all("SELECT * FROM tool_runs WHERE case_id = ? ORDER BY rowid", (case_id,))
        return self.db.fetch_all("SELECT * FROM tool_runs ORDER BY rowid")

    def add_provider_request(self, row: Dict[str, Any]) -> None:
        self.db.insert("provider_requests", row)

    def finish_provider_request(self, id_: str, status: str, response_summary: Dict[str, Any], finished_at: str) -> None:
        self.db.update("provider_requests", {"id": id_}, {"status": status, "response_summary_json": json.dumps(response_summary, sort_keys=True, default=str), "finished_at": finished_at})

    def provider_requests(self, case_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if case_id:
            rows = self.db.fetch_all("SELECT * FROM provider_requests WHERE case_id = ? ORDER BY rowid", (case_id,))
        else:
            rows = self.db.fetch_all("SELECT * FROM provider_requests ORDER BY rowid")
        for r in rows:
            r["request_summary"] = json.loads(r.pop("request_summary_json") or "{}")
            r["response_summary"] = json.loads(r.pop("response_summary_json") or "null")
        return rows
