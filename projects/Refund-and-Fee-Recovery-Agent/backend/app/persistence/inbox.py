"""Provider event inbox: at-least-once delivery, deduplicated by (provider, event_id)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from ..clock import Clock, format_ts
from .db import Database


class EventInbox:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def record(self, provider: str, event_id: str, event_type: str, environment: str, payload: Dict[str, Any]) -> bool:
        """Returns True if this is the first time we see the event."""
        try:
            self.db.insert("provider_events_inbox", {
                "provider": provider, "event_id": event_id, "event_type": event_type, "environment": environment,
                "received_at": format_ts(self.clock.now()), "payload_json": json.dumps(payload, sort_keys=True),
                "processed_at": None, "outcome": None,
            })
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_processed(self, provider: str, event_id: str, outcome: str) -> None:
        self.db.update("provider_events_inbox", {"provider": provider, "event_id": event_id},
                       {"processed_at": format_ts(self.clock.now()), "outcome": outcome})

    def get(self, provider: str, event_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.fetch_one("SELECT * FROM provider_events_inbox WHERE provider = ? AND event_id = ?", (provider, event_id))
        if row:
            row["payload"] = json.loads(row.pop("payload_json"))
        return row

    def all(self) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all("SELECT * FROM provider_events_inbox ORDER BY rowid")
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json"))
        return rows
