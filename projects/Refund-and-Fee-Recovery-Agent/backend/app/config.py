"""Application settings.

Everything here is read from the environment with prototype-safe defaults.
No real provider credentials exist in this repository; the mock adapters
run without any secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
MIGRATIONS_DIR = REPO_ROOT / "migrations"


@dataclass
class Settings:
    environment: str = field(default_factory=lambda: os.getenv("APP_ENV", "mock"))
    database_path: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", str(REPO_ROOT / "data" / "recovery.db")))
    # Secret used to sign our own outbox/webhook deliveries (HMAC-SHA256).
    webhook_signing_secret: str = field(default_factory=lambda: os.getenv("APP_WEBHOOK_SECRET", "dev-only-outbox-secret"))
    # Secret shared with the mock provider so we can verify its callbacks.
    mock_provider_secret: str = field(default_factory=lambda: os.getenv("MOCK_PROVIDER_SECRET", "dev-only-mock-provider-secret"))
    # Fixture clock start. When set, time is controllable through the ops API.
    fixture_clock_start: str = field(default_factory=lambda: os.getenv("FIXTURE_CLOCK_START", "2026-09-20T12:00:00Z"))
    use_fixture_clock: bool = field(default_factory=lambda: os.getenv("USE_FIXTURE_CLOCK", "1") == "1")
    approval_ttl_hours: int = field(default_factory=lambda: int(os.getenv("APPROVAL_TTL_HOURS", "24")))
    followup_cadence_days: int = field(default_factory=lambda: int(os.getenv("FOLLOWUP_CADENCE_DAYS", "5")))
    max_followups: int = field(default_factory=lambda: int(os.getenv("MAX_FOLLOWUPS", "2")))
    max_tool_calls: int = field(default_factory=lambda: int(os.getenv("MAX_TOOL_CALLS", "8")))
    worker_lease_seconds: int = field(default_factory=lambda: int(os.getenv("WORKER_LEASE_SECONDS", "60")))
    # Optional LLM provider (OpenAI-compatible chat completions with tools).
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", ""))


def load_settings() -> Settings:
    return Settings()
