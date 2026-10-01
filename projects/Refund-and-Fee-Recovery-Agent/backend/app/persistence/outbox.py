"""Transactional outbox for application events.

Events are written in the same transaction as the state change that produced
them and dispatched later by the worker. Deliveries are signed with HMAC-SHA256
so downstream consumers (frontend notifications, operator tooling) can verify
them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable, Dict, List, Optional

from ..clock import Clock, format_ts
from ..ids import canonical_json, new_id
from .db import Database

Subscriber = Callable[[Dict[str, Any]], None]


def sign(secret: str, body: str) -> str:
    return "hmac-sha256=" + hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(secret: str, body: str, signature: str) -> bool:
    return hmac.compare_digest(sign(secret, body), signature or "")


class Outbox:
    def __init__(self, db: Database, clock: Clock, secret: str, environment: str) -> None:
        self.db = db
        self.clock = clock
        self.secret = secret
        self.environment = environment
        self._subscribers: List[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def publish(self, event_type: str, case_id: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
        event = {
            "id": new_id("evt"),
            "type": event_type,
            "case_id": case_id,
            "occurred_at": format_ts(self.clock.now()),
            "environment": self.environment,
            "data": data,
        }
        body = canonical_json(event)
        self.db.insert("outbox", {
            "id": event["id"], "event_type": event_type, "case_id": case_id, "occurred_at": event["occurred_at"],
            "environment": self.environment, "payload_json": body, "signature": sign(self.secret, body), "dispatched_at": None,
        })
        return event

    def dispatch_pending(self, limit: int = 100) -> int:
        rows = self.db.fetch_all("SELECT * FROM outbox WHERE dispatched_at IS NULL ORDER BY rowid LIMIT ?", (limit,))
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["_signature"] = row["signature"]
            for fn in self._subscribers:
                fn(payload)  # subscribers must be idempotent (at-least-once)
            self.db.update("outbox", {"id": row["id"]}, {"dispatched_at": format_ts(self.clock.now())})
        return len(rows)

    def pending(self) -> List[Dict[str, Any]]:
        return [json.loads(r["payload_json"]) for r in self.db.fetch_all("SELECT payload_json FROM outbox WHERE dispatched_at IS NULL ORDER BY rowid")]

    def all_events(self, case_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if case_id:
            rows = self.db.fetch_all("SELECT payload_json FROM outbox WHERE case_id = ? ORDER BY rowid", (case_id,))
        else:
            rows = self.db.fetch_all("SELECT payload_json FROM outbox ORDER BY rowid")
        return [json.loads(r["payload_json"]) for r in rows]
