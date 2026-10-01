"""Runtime settings. Everything defaults to a credential-free local mock environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", REPO_ROOT / "fixtures"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")
    environment: str = os.environ.get("APP_ENVIRONMENT", "mock")  # mock | sandbox | production
    adapter_mode: str = os.environ.get("ADAPTER_MODE", "direct")  # direct | a2a
    a2a_insurer_urls: Dict[str, str] = field(
        default_factory=lambda: {
            "ins_northwind_a": os.environ.get("A2A_URL_NORTHWIND", "http://localhost:9001"),
            "ins_harborline_b": os.environ.get("A2A_URL_HARBORLINE", "http://localhost:9002"),
            "ins_cedar_c": os.environ.get("A2A_URL_CEDAR", "http://localhost:9003"),
        }
    )
    provider_timeout_seconds: float = float(os.environ.get("PROVIDER_TIMEOUT_SECONDS", "5"))
    tool_call_budget: int = int(os.environ.get("TOOL_CALL_BUDGET", "8"))
    approval_ttl_minutes: int = int(os.environ.get("APPROVAL_TTL_MINUTES", "30"))
    fixture_clock: bool = _env_bool("FIXTURE_CLOCK", True)
    fixture_clock_start: str = os.environ.get("FIXTURE_CLOCK_START", "2026-10-01T09:00:00+00:00")
    fixture_clock_frozen: bool = _env_bool("FIXTURE_CLOCK_FROZEN", False)
    webhook_signing_secret: str = os.environ.get("WEBHOOK_SIGNING_SECRET", "local-dev-outbound-secret")
    outbound_webhook_url: Optional[str] = os.environ.get("OUTBOUND_WEBHOOK_URL")
    llm_provider: str = os.environ.get("LLM_PROVIDER", "scripted")  # scripted | openai_compatible
    llm_base_url: str = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: Optional[str] = os.environ.get("LLM_API_KEY")
    llm_model: str = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    prompt_version: str = "renters-agent-prompt/1"
    worker_lease_seconds: int = int(os.environ.get("WORKER_LEASE_SECONDS", "30"))
    operator_tokens: Dict[str, str] = field(default_factory=lambda: {"tok_operator": "op_demo_1"})

    @property
    def is_mock(self) -> bool:
        return self.environment == "mock"


settings = Settings()
