"""Inbound provider events: signature verification, deduplication and safe processing.

A callback never changes case state directly. It only schedules a status check against
the provider, so a forged or replayed event cannot mark a policy as issued.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select

from ..context import AppContext
from ..persistence import models as m
from ..persistence.inbox import get_inbox_event, record_inbound
from ..persistence.jobs import enqueue_job
from ..persistence.outbox import verify_signature
from .case_service import CaseError


class ProviderEventService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _secret_for(self, provider: str) -> Optional[str]:
        cfg = self.ctx.registry.configs.get(provider)
        return cfg.get("webhook_secret") if cfg else None

    def receive(self, provider: str, body: bytes, payload: Dict[str, Any], signature: Optional[str], timestamp: Optional[str]) -> Dict[str, Any]:
        secret = self._secret_for(provider)
        if secret is None:
            raise CaseError("unknown provider", 404)
        if not signature or not timestamp or not verify_signature(secret, body, timestamp, signature):
            raise CaseError("invalid provider signature", 401)
        event_id = payload.get("event_id") or payload.get("id")
        if not event_id:
            raise CaseError("event_id is required", 422)
        now = self.ctx.now()
        with self.ctx.db.session() as session:
            row, is_new = record_inbound(session, provider, str(event_id), payload, now)
            if not is_new:
                return {"inbox_id": row.id, "duplicate": True, "status": row.status}
            result = self._process(session, row)
            return {"inbox_id": row.id, "duplicate": False, **result}

    def _process(self, session, row: m.InboxEvent) -> Dict[str, Any]:
        payload = row.payload_json
        now = self.ctx.now()
        request_ref = payload.get("request_ref")
        submission_ref = payload.get("submission_ref")
        case_id: Optional[str] = None
        if request_ref:
            action = session.get(m.Action, request_ref)
            if action is not None:
                case_id = action.case_id
            else:
                task = session.scalars(select(m.QuoteTask).where(m.QuoteTask.request_ref == request_ref)).first()
                if task is not None:
                    enqueue_job(session, "poll_quote_task", {"task_id": task.id}, now, dedupe_key="poll_quote_task:%s:%s" % (task.id, row.id))
                    row.status, row.processed_at = "processed", now
                    return {"scheduled": "poll_quote_task", "case_id": task.case_id}
        if case_id is None and submission_ref:
            app = session.scalars(select(m.Application).where(m.Application.submission_ref == submission_ref)).first()
            if app is not None:
                case_id = app.case_id
        if case_id is None:
            row.status, row.processed_at, row.error = "ignored", now, "no matching case"
            return {"scheduled": None, "reason": "no matching case"}
        enqueue_job(session, "poll_underwriting", {"case_id": case_id}, now, dedupe_key="poll_underwriting:%s:%s" % (case_id, row.id))
        row.status, row.processed_at = "processed", now
        return {"scheduled": "poll_underwriting", "case_id": case_id}

    def replay(self, inbox_id: str) -> Dict[str, Any]:
        """Operator replay: re-run processing for a stored event. Cannot bypass approvals."""
        with self.ctx.db.session() as session:
            row = get_inbox_event(session, inbox_id)
            if row is None:
                raise CaseError("inbox event not found", 404)
            result = self._process(session, row)
            return {"inbox_id": row.id, "replayed": True, **result}
