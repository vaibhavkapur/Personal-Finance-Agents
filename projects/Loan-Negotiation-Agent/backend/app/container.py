"""Composition root: wires settings, clock, database, simulator, service and worker."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .adapters.mock_lender import MockLenderAdapter, MockLenderNetwork
from .adapters.sandbox import SandboxLenderAdapter
from .clock import Clock, FixtureClock, SystemClock, parse_iso
from .config import FIXTURES_DIR, Settings, get_settings
from .persistence.db import Database
from .persistence.seed import seed_fixtures
from .workflows.case_service import CaseService
from .workflows.worker import Worker


class Container:
    def __init__(self, settings: Optional[Settings] = None, clock: Optional[Clock] = None, database_url: Optional[str] = None, fixtures_dir: Optional[Path] = None):
        self.settings = settings or get_settings()
        if database_url:
            self.settings.database_url = database_url
        self.fixtures_dir = fixtures_dir or FIXTURES_DIR
        if clock is not None:
            self.clock = clock
        elif self.settings.use_fixture_clock:
            self.clock = FixtureClock(parse_iso(self.settings.fixture_clock_start))
        else:
            self.clock = SystemClock()
        self.db = Database(self.settings.database_url)
        self.db.create_all()
        lenders = json.loads((self.fixtures_dir / "lenders.json").read_text())["lenders"]
        self.lenders = {l["id"]: l for l in lenders}
        self.network = MockLenderNetwork(lenders, self.clock)
        if self.settings.provider_environment == "sandbox":
            self.adapter = SandboxLenderAdapter()
        else:
            self.adapter = MockLenderAdapter(self.network)
        self.service = CaseService(self.clock, self.settings, self.lenders)
        self.worker = Worker(self.db, self.service, self.adapter)

    def seed(self) -> dict:
        with self.db.session() as session:
            return seed_fixtures(session, self.fixtures_dir)

    def reset(self) -> None:
        self.db.drop_all()
        self.db.create_all()
        self.network = MockLenderNetwork(list(self.lenders.values()), self.clock)
        if self.settings.provider_environment != "sandbox":
            self.adapter = MockLenderAdapter(self.network)
            self.worker = Worker(self.db, self.service, self.adapter)
