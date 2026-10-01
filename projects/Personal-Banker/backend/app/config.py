"""Application settings.

Everything here is safe to run without external credentials. The default
database is a local SQLite file so the prototype starts with one command;
set ``DATABASE_URL`` to a PostgreSQL DSN (see docker-compose.yml) for a
multi-process deployment.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PB_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{REPO_ROOT / 'backend' / 'personal_banker.db'}"
    environment: str = "mock"  # mock | sandbox | production (only mock is implemented)
    # Simulation clock start. All timers, expiries and maturity checks use the
    # simulation clock so demos and tests are deterministic.
    clock_start: str = "2026-09-26T09:00:00Z"
    # Approval challenges expire after this many simulated minutes.
    approval_challenge_ttl_minutes: int = 15
    # Balance snapshots older than this (simulated minutes) are stale at submission.
    snapshot_max_age_minutes: int = 60
    # Maximum tool calls per agent turn before the orchestrator escalates.
    agent_tool_budget: int = 12
    # Shared secret for signing/verifying provider webhook deliveries (mock).
    webhook_secret: str = "mock-webhook-secret-not-for-production"
    # Optional OpenAI-compatible LLM. When unset the deterministic rules policy is used.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    # Serve the built frontend (frontend/dist) from the API process when present.
    serve_frontend: bool = True
    worker_lease_seconds: int = 30
    worker_max_attempts: int = 5


settings = Settings()
