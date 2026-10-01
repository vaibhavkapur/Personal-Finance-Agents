"""Durable worker: leased jobs, outbox delivery and quote-expiry timer.

Restart-safe: leases expire, jobs are idempotent, approvals live in the database.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from ..context import AppContext
from ..persistence import models as m
from ..persistence.jobs import backoff_seconds, claim_jobs, complete_job, reschedule_job
from ..persistence.outbox import pending_events, serialize_event, sign_payload
from .approvals import ApplicationService
from .case_service import CaseService

log = logging.getLogger("worker")


class Worker:
    def __init__(self, ctx: AppContext, owner: Optional[str] = None) -> None:
        self.ctx = ctx
        self.owner = owner or "worker-%s" % uuid.uuid4().hex[:8]
        self.cases = CaseService(ctx)
        self.applications = ApplicationService(ctx, self.cases)
        self.handled: int = 0

    async def handle(self, job: m.Job) -> Dict[str, Any]:
        payload = job.payload_json
        if job.type == "execute_action":
            return await self.applications.execute_action(payload["action_id"])
        if job.type == "reconcile_action":
            return await self.applications.reconcile_action(payload["action_id"])
        if job.type == "poll_underwriting":
            return await self.applications.poll_underwriting(payload["case_id"])
        if job.type == "poll_quote_task":
            return await self.cases.poll_quote_task(payload["task_id"])
        raise ValueError("unknown job type %s" % job.type)

    async def run_once(self, limit: int = 20) -> Dict[str, Any]:
        """Process due jobs once. Returns a summary useful for tests and the operator API."""
        now = self.ctx.now()
        summary: Dict[str, Any] = {"owner": self.owner, "jobs": [], "outbox_delivered": 0, "expired_cases": 0}
        with self.ctx.db.session() as session:
            jobs = claim_jobs(session, self.owner, now, self.ctx.settings.worker_lease_seconds, limit=limit)
        for job in jobs:
            try:
                result = await self.handle(job)
                with self.ctx.db.session() as session:
                    row = session.get(m.Job, job.id)
                    complete_job(session, row, self.ctx.now())
                summary["jobs"].append({"id": job.id, "type": job.type, "result": result})
            except Exception as exc:  # noqa: BLE001 - worker must survive any job failure
                log.exception("job %s (%s) failed", job.id, job.type)
                with self.ctx.db.session() as session:
                    row = session.get(m.Job, job.id)
                    reschedule_job(session, row, self.ctx.now(), backoff_seconds(row.attempts), error=str(exc)[:500])
                summary["jobs"].append({"id": job.id, "type": job.type, "error": str(exc)})
            self.handled += 1
        summary["outbox_delivered"] = await self.deliver_outbox()
        summary["expired_cases"] = self.applications.expire_stale_quotes()
        return summary

    async def run_until_idle(self, max_rounds: int = 25) -> int:
        """Keep running while due jobs remain (used by tests and the demo command)."""
        rounds = 0
        while rounds < max_rounds:
            summary = await self.run_once()
            rounds += 1
            if not summary["jobs"]:
                break
        return rounds

    async def deliver_outbox(self) -> int:
        delivered = 0
        url = self.ctx.settings.outbound_webhook_url
        with self.ctx.db.session() as session:
            events = pending_events(session)
            for event in events:
                body = serialize_event(event.payload_json)
                timestamp = self.ctx.now().isoformat()
                signature = sign_payload(self.ctx.settings.webhook_signing_secret, body, timestamp)
                ok = True
                if url:
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            response = await client.post(url, content=body, headers={"Content-Type": "application/json", "X-Signature": signature, "X-Signature-Timestamp": timestamp})
                            ok = response.status_code < 300
                    except httpx.HTTPError:
                        ok = False
                event.attempts += 1
                if ok:
                    event.status = "delivered"
                    event.delivered_at = self.ctx.now()
                    delivered += 1
                elif event.attempts >= 8:
                    event.status = "failed"
        return delivered

    async def run_forever(self, interval_seconds: float = 1.0) -> None:  # pragma: no cover - long running
        log.info("worker %s started", self.owner)
        while True:
            try:
                summary = await self.run_once()
                if summary["jobs"]:
                    log.info("processed %s", json.dumps([{"type": j["type"], "ok": "error" not in j} for j in summary["jobs"]]))
            except Exception:  # noqa: BLE001
                log.exception("worker loop error")
            await asyncio.sleep(interval_seconds)


def main() -> None:  # pragma: no cover - entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ctx = AppContext()
    asyncio.run(Worker(ctx).run_forever())


if __name__ == "__main__":  # pragma: no cover
    main()
