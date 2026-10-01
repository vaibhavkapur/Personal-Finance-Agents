"""Runtime configuration (environment driven, no secrets in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
FRONTEND_DIR = REPO_ROOT / "frontend"


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", f"sqlite:///{REPO_ROOT / 'data' / 'loan_agent.db'}"))
    environment: str = field(default_factory=lambda: os.getenv("APP_ENV", "mock"))
    provider_environment: str = field(default_factory=lambda: os.getenv("PROVIDER_ENV", "mock"))
    approval_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("APPROVAL_TTL_SECONDS", "900")))
    tool_call_budget: int = field(default_factory=lambda: int(os.getenv("TOOL_CALL_BUDGET", "12")))
    webhook_signing_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SIGNING_SECRET", "local-dev-webhook-secret"))
    operator_token: str = field(default_factory=lambda: os.getenv("OPERATOR_TOKEN", "demo-operator-token"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    fixture_clock_start: str = field(default_factory=lambda: os.getenv("FIXTURE_CLOCK_START", "2026-09-26T12:00:00+00:00"))
    use_fixture_clock: bool = field(default_factory=lambda: os.getenv("USE_FIXTURE_CLOCK", "1") == "1")
    mcp_protocol_version: str = "2025-06-18"
    a2a_protocol_version: str = "0.3"
    log_redaction_fields: List[str] = field(default_factory=lambda: ["api_token", "ssn", "income_evidence", "webhook_secret"])


def get_settings() -> Settings:
    return Settings()
