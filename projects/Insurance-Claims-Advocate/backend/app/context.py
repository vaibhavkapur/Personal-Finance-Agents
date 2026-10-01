"""Application wiring: database, clock, fixtures, adapter, services, executor and worker."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .adapters.mock_insurer import MockInsurerAdapter
from .adapters.mock_payments import MockPaymentFeed
from .adapters.sandbox import SandboxClaimsAdapter
from .agent.orchestrator import AgentOrchestrator
from .clock import Clock
from .config import Settings, load_settings
from .persistence.db import Database
from .persistence.seed import load_policy_fixtures, seed
from .workflows.case_service import CaseService
from .workflows.events import ProviderEventProcessor
from .workflows.executor import ActionExecutor
from .workflows.worker import Worker


class AppContext:
    def __init__(self, settings: Optional[Settings] = None, clock: Optional[Clock] = None, seed_fixtures: bool = True):
        self.settings = settings or load_settings()
        self.clock = clock or Clock()
        self.db = Database(self.settings.database_url)
        self.policies = load_policy_fixtures()
        # the mock insurer always exists (it also backs the A2A insurer agent); the active adapter may differ
        self.mock_insurer = MockInsurerAdapter(self.db, self.clock, self.settings.provider_webhook_secret)
        self.payment_feed = MockPaymentFeed(self.clock, self.settings.provider_webhook_secret)
        self.adapter = self._build_adapter()
        self.cases = CaseService(self.db, self.clock, self.settings, self.policies, self.adapter)
        self.events = ProviderEventProcessor(self.db, self.clock, self.settings, self.cases)
        self.executor = ActionExecutor(self.db, self.clock, self.settings, self.adapter)
        self.worker = Worker(self.db, self.clock, self.settings, self.executor, self.events, mock_insurer=self.mock_insurer)
        self.agent = AgentOrchestrator(self.db, self.clock, self.settings, self.cases, self.adapter.capabilities.environment)
        if seed_fixtures:
            seed(self.db, self.clock)

    def _build_adapter(self):
        kind = self.settings.claims_adapter
        if kind == "sandbox":
            return SandboxClaimsAdapter(self.clock, self.settings.sandbox_base_url)
        if kind == "a2a":
            from .adapters.a2a_client import A2AClaimsAdapter
            from .api.insurer_agent import InsurerAgentServer

            return A2AClaimsAdapter(self.db, self.clock, InsurerAgentServer(self.mock_insurer, self.clock), base_url=self.settings.a2a_base_url)
        return self.mock_insurer

    def new_worker(self) -> Worker:
        """A fresh worker instance over the same database (used to demonstrate restart recovery)."""
        return Worker(self.db, self.clock, self.settings, self.executor, self.events, mock_insurer=self.mock_insurer)

    def freeze_clock(self, at: str) -> None:
        from .clock import parse_iso

        self.clock.freeze(parse_iso(at))
