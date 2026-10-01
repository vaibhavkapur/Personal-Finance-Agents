"""Composition root: builds the database, clock, adapters, services and worker."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adapters.base import RecoveryProviderAdapter
from .adapters.mock_issuer import MockIssuerAdapter, MockStatementFeed
from .adapters.mock_merchant import MockMerchantAdapter
from .adapters.pump import SimulatorPump
from .adapters.sandbox import SandboxMerchantAdapter
from .adapters.commerce_events import ingest_commerce_event
from .clock import Clock, FixtureClock, SystemClock
from .config import FIXTURES_DIR, Settings, load_settings
from .domain.deadlines import DeadlineConfig
from .domain.models import Customer, Direction, Document, Merchant, PaymentInstrument, PurchaseRecord, RefundPromise, Transaction, TransactionKind
from .persistence.db import Database
from .persistence.inbox import EventInbox
from .persistence.outbox import Outbox
from .persistence.repositories import Repositories
from .workflows.case_service import CaseService
from .workflows.events import ProviderEventHandler
from .workflows.jobs import JobQueue
from .workflows.worker import Worker


@dataclass
class Container:
    settings: Settings
    db: Database
    clock: Clock
    repos: Repositories
    outbox: Outbox
    inbox: EventInbox
    jobs: JobQueue
    statement: MockStatementFeed
    merchant_mock: MockMerchantAdapter
    issuer_mock: MockIssuerAdapter
    adapters: Dict[str, RecoveryProviderAdapter]
    service: CaseService
    events: ProviderEventHandler
    worker: Worker
    pump: SimulatorPump
    fixtures_dir: Path

    # -- convenience for demos/tests -----------------------------------------
    def advance(self, *, days: float = 0, hours: float = 0, minutes: float = 0) -> Dict[str, Any]:
        """Advance the fixture clock, deliver due simulator callbacks and run the worker until idle."""
        if not isinstance(self.clock, FixtureClock):
            raise RuntimeError("advance() requires the fixture clock")
        self.clock.advance(days=days, hours=hours, minutes=minutes)
        delivered = self.pump.deliver_due()
        handled = self.worker.run_until_idle_sync()
        # Provider callbacks may have been produced by worker actions; deliver anything now due.
        delivered += self.pump.deliver_due()
        handled += self.worker.run_until_idle_sync()
        return {"now": self.clock.now().isoformat(), "callbacks_delivered": len(delivered), "jobs_handled": handled}

    def seed(self, customer_fixture: str = "customer.json") -> Dict[str, Any]:
        return seed_fixtures(self, self.fixtures_dir / customer_fixture)


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


def build_container(settings: Optional[Settings] = None, *, database_path: Optional[str] = None, fixtures_dir: Path = FIXTURES_DIR, clock: Optional[Clock] = None) -> Container:
    settings = settings or load_settings()
    db = Database(database_path or settings.database_path)
    db.migrate()
    if clock is None:
        clock = FixtureClock(settings.fixture_clock_start) if settings.use_fixture_clock else SystemClock()
    repos = Repositories(db)
    outbox = Outbox(db, clock, settings.webhook_signing_secret, settings.environment)
    inbox = EventInbox(db, clock)
    jobs = JobQueue(repos, clock)
    scenarios = _load(fixtures_dir / "provider_scenarios.json")
    customer = _load(fixtures_dir / "customer.json")
    statement = MockStatementFeed(clock, customer_id=customer["customer"]["id"], instrument_ref=customer["payment_instruments"][0]["id"])
    merchant_mock = MockMerchantAdapter(clock, statement, scenarios["merchant_mock"], settings.mock_provider_secret, merchant_id=customer["merchants"][0]["id"])
    issuer_mock = MockIssuerAdapter(clock, statement, scenarios["issuer_mock"], settings.mock_provider_secret)
    adapters: Dict[str, RecoveryProviderAdapter] = {
        merchant_mock.provider: merchant_mock,
        issuer_mock.provider: issuer_mock,
        "merchant_sandbox": SandboxMerchantAdapter(),
    }
    deadline_config = DeadlineConfig.load(fixtures_dir / "issuer_config.json")
    service = CaseService(repos=repos, clock=clock, outbox=outbox, jobs=jobs, settings=settings, deadline_config=deadline_config, adapters=adapters)
    events = ProviderEventHandler(service, inbox, clock, settings.mock_provider_secret)
    worker = Worker(jobs=jobs, service=service, outbox=outbox, clock=clock, lease_seconds=settings.worker_lease_seconds)
    pump = SimulatorPump([statement, merchant_mock, issuer_mock], events, settings.mock_provider_secret)
    return Container(settings=settings, db=db, clock=clock, repos=repos, outbox=outbox, inbox=inbox, jobs=jobs, statement=statement, merchant_mock=merchant_mock,
                     issuer_mock=issuer_mock, adapters=adapters, service=service, events=events, worker=worker, pump=pump, fixtures_dir=fixtures_dir)


def seed_fixtures(c: Container, path: Path) -> Dict[str, Any]:
    """Load the synthetic customer bundle. Safe to run twice (upserts / unique constraints)."""
    data = _load(path)
    counts: Dict[str, int] = {"documents": 0, "purchases": 0, "promises": 0, "transactions": 0, "commerce_events": 0}
    with c.db.transaction():
        c.repos.upsert_customer(Customer(**data["customer"]))
        for pi in data["payment_instruments"]:
            c.repos.upsert_instrument(PaymentInstrument(**pi))
        for m in data["merchants"]:
            c.repos.upsert_merchant(Merchant(**m))
        for d in data.get("documents", []):
            if c.repos.db.fetch_one("SELECT id FROM documents WHERE id = ?", (d["id"],)) is None:
                c.repos.add_document(Document(**d))
                counts["documents"] += 1
        for p in data.get("purchases", []):
            if c.repos.db.fetch_one("SELECT id FROM purchase_records WHERE id = ?", (p["id"],)) is None:
                c.repos.add_purchase(PurchaseRecord(**p))
                counts["purchases"] += 1
        for p in data.get("promises", []):
            if c.repos.db.fetch_one("SELECT id FROM refund_promises WHERE id = ?", (p["id"],)) is None:
                c.repos.add_promise(RefundPromise(**p))
                counts["promises"] += 1
        for t in data.get("transactions", []):
            if c.repos.add_transaction(Transaction(**t)):
                counts["transactions"] += 1
        for name in data.get("commerce_events", []):
            payload = _load(c.fixtures_dir / "commerce_events" / name)
            doc = ingest_commerce_event(data["customer"]["id"], payload, c.clock)
            if c.repos.db.fetch_one("SELECT id FROM documents WHERE content_hash = ?", (doc.content_hash,)) is None:
                c.repos.add_document(doc)
                counts["commerce_events"] += 1
    return counts
