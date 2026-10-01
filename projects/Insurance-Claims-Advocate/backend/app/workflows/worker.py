"""Durable worker: persisted jobs with leases, provider event delivery, outbox delivery, stuck-action recovery and
deadline checks. Safe to restart at any point; every step is idempotent."""
from __future__ import annotations

import asyncio
import traceback
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import or_, select

from ..clock import Clock, iso, parse_iso
from ..config import Settings
from ..ids import new_id
from ..persistence.models import Action, ClaimCase, InsurerRequest, Job, OutboxMessage
from .events import ProviderEventProcessor
from .executor import ActionExecutor
from .outbox import enqueue_job, sign_outbound
from .state_machine import record_event


class SimulatedCrash(RuntimeError):
    """Test hook: stands in for the worker process dying mid-job."""


class Worker:
    def __init__(self, db, clock: Clock, settings: Settings, executor: ActionExecutor, events: ProviderEventProcessor, mock_insurer=None, worker_id: Optional[str] = None):
        self.db = db
        self.clock = clock
        self.settings = settings
        self.executor = executor
        self.events = events
        self.mock_insurer = mock_insurer
        self.worker_id = worker_id or new_id("worker")
        self.crash_before_provider_call = False  # test hook: simulate a crash after leasing the action

    # ------------------------------------------------------------------ main loop
    async def run_once(self) -> Dict[str, Any]:
        stats = {"jobs": 0, "provider_events": 0, "replayed_events": 0, "outbox": 0, "recovered_actions": 0, "errors": []}
        stats["provider_events"] = self.deliver_mock_provider_events()
        for job in self._claim_due_jobs():
            stats["jobs"] += 1
            try:
                await self._run_job(job)
                self._finish_job(job.id)
            except SimulatedCrash:
                raise
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"{job.job_type}: {exc}")
                self._fail_job(job.id, f"{exc}\n{traceback.format_exc()}")
        stats["recovered_actions"] = self._recover_stuck_actions()
        stats["replayed_events"] = self.events.replay_unmatched()
        stats["outbox"] = await self.deliver_outbox()
        stats["provider_events"] += self.deliver_mock_provider_events()
        return stats

    async def run_until_idle(self, max_rounds: int = 25) -> List[Dict[str, Any]]:
        rounds = []
        for _ in range(max_rounds):
            stats = await self.run_once()
            rounds.append(stats)
            if not any(stats[k] for k in ("jobs", "provider_events", "replayed_events", "outbox", "recovered_actions")):
                break
        return rounds

    async def run_forever(self, interval_seconds: float = 2.0) -> None:  # pragma: no cover - service entrypoint
        while True:
            await self.run_once()
            await asyncio.sleep(interval_seconds)

    # ------------------------------------------------------------------ jobs
    def _claim_due_jobs(self) -> List[Job]:
        now = self.clock.now()
        lease_until = iso(now + timedelta(seconds=self.settings.worker_lease_seconds))
        claimed: List[Job] = []
        with self.db.session() as s:
            rows = s.scalars(
                select(Job).where(Job.status == "pending", Job.run_at <= iso(now), or_(Job.lease_until.is_(None), Job.lease_until < iso(now))).order_by(Job.run_at)
            ).all()
            for job in rows:
                job.lease_until = lease_until
                job.locked_by = self.worker_id
                job.attempts += 1
                claimed.append(job)
        return claimed

    async def _run_job(self, job: Job) -> None:
        payload = job.payload_json
        if job.job_type == "execute_action":
            if self.crash_before_provider_call:
                self._simulate_crash_after_lease(payload["action_id"])
                raise SimulatedCrash("simulated worker crash after leasing the action")
            await self.executor.execute(payload["action_id"])
        elif job.job_type == "resolve_uncertain_action":
            await self.executor.resolve_uncertain(payload["action_id"])
        elif job.job_type == "deadline_check":
            self._check_deadline(payload["case_id"], payload["request_id"])
        else:
            raise ValueError(f"unknown job type {job.job_type}")

    def _simulate_crash_after_lease(self, action_id: str) -> None:
        """Test hook: mark the action executing (as the executor would) and then die before calling the provider."""
        with self.db.session() as s:
            action = s.get(Action, action_id)
            if action and action.status == "approved":
                action.status = "executing"
                action.attempts += 1
                action.updated_at = self.clock.now_iso()

    def _finish_job(self, job_id: str) -> None:
        with self.db.session() as s:
            job = s.get(Job, job_id)
            job.status = "done"
            job.finished_at = self.clock.now_iso()
            job.lease_until = None

    def _fail_job(self, job_id: str, error: str) -> None:
        with self.db.session() as s:
            job = s.get(Job, job_id)
            job.last_error = error[-2000:]
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                job.finished_at = self.clock.now_iso()
            else:
                # bounded backoff; the lease expires so another worker can pick it up
                job.run_at = iso(self.clock.now() + timedelta(seconds=min(2 ** job.attempts, 60)))
                job.lease_until = None

    # ------------------------------------------------------------------ recovery
    def _recover_stuck_actions(self) -> int:
        """Actions left `executing` by a crashed worker are resolved via lookup by request reference."""
        cutoff = iso(self.clock.now() - timedelta(seconds=self.settings.worker_lease_seconds))
        count = 0
        with self.db.session() as s:
            stuck = s.scalars(select(Action).where(Action.status == "executing", Action.updated_at < cutoff)).all()
            for action in stuck:
                action.status = "uncertain"
                action.result_json = dict(action.result_json or {}, recovered="worker lease expired while executing")
                action.updated_at = self.clock.now_iso()
                case = s.get(ClaimCase, action.case_id)
                record_event(s, self.clock, case, "action.recovered", self.worker_id, {"action_id": action.id, "reason": "stale executing lease"}, bump_version=False)
                enqueue_job(s, self.clock, "resolve_uncertain_action", {"action_id": action.id}, dedupe_key=f"resolve:{action.id}:recover:{action.attempts}")
                count += 1
        return count

    def _check_deadline(self, case_id: str, request_id: str) -> None:
        with self.db.session() as s:
            req = s.get(InsurerRequest, request_id)
            case = s.get(ClaimCase, case_id)
            if not req or not case:
                return
            if req.status == "open" and req.due_at and parse_iso(req.due_at) <= self.clock.now():
                req.status = "expired"
                record_event(s, self.clock, case, "insurer.request_deadline_passed", self.worker_id, {"request_id": req.provider_request_id, "due_at": req.due_at, "deadline_source": req.deadline_source, "handoff": "customer must contact the insurer or a human reviewer; no automatic follow-up"})

    # ------------------------------------------------------------------ delivery
    def deliver_mock_provider_events(self) -> int:
        """Pull due events from the in-process mock insurer and push them through the same inbox as HTTP callbacks."""
        if self.mock_insurer is None:
            return 0
        delivered = 0
        for ev in self.mock_insurer.pending_events():
            try:
                self.events.handle_claim_event(ev["payload"], ev["signature"])
            finally:
                self.mock_insurer.mark_delivered(ev["claim_ref"], ev["local_id"])
            delivered += 1
        return delivered

    async def deliver_outbox(self) -> int:
        delivered = 0
        with self.db.session() as s:
            pending = s.scalars(select(OutboxMessage).where(OutboxMessage.status == "pending").order_by(OutboxMessage.created_at)).all()
            for msg in pending:
                signature = sign_outbound(self.settings.outbound_webhook_secret, msg.payload_json)
                msg.signature = signature
                msg.attempts += 1
                if self.settings.outbound_webhook_url:
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            resp = await client.post(self.settings.outbound_webhook_url, json=msg.payload_json, headers={"X-Advocate-Signature": signature})
                            resp.raise_for_status()
                    except Exception:  # noqa: BLE001
                        if msg.attempts >= 5:
                            msg.status = "failed"
                        continue
                msg.status = "delivered"
                msg.delivered_at = self.clock.now_iso()
                delivered += 1
        return delivered
