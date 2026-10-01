"""Runtime configuration. Everything defaults to a credential-free local mock environment."""
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
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./claims_advocate.db"))
    environment: str = field(default_factory=lambda: os.environ.get("APP_ENV", "mock"))
    claims_adapter: str = field(default_factory=lambda: os.environ.get("CLAIMS_ADAPTER", "mock"))  # mock | a2a | sandbox
    planner: str = field(default_factory=lambda: os.environ.get("AGENT_PLANNER", "scripted"))  # scripted | llm
    llm_base_url: str = field(default_factory=lambda: os.environ.get("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: os.environ.get("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", ""))
    tool_call_budget: int = field(default_factory=lambda: int(os.environ.get("AGENT_TOOL_BUDGET", "12")))
    approval_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get("APPROVAL_TTL_SECONDS", "900")))
    provider_webhook_secret: str = field(default_factory=lambda: os.environ.get("PROVIDER_WEBHOOK_SECRET", "mock-insurer-shared-secret"))
    outbound_webhook_secret: str = field(default_factory=lambda: os.environ.get("OUTBOUND_WEBHOOK_SECRET", "advocate-webhook-secret"))
    outbound_webhook_url: str = field(default_factory=lambda: os.environ.get("OUTBOUND_WEBHOOK_URL", ""))
    sandbox_base_url: str = field(default_factory=lambda: os.environ.get("SANDBOX_BASE_URL", ""))
    a2a_base_url: str = field(default_factory=lambda: os.environ.get("A2A_BASE_URL", ""))  # empty = in-process insurer agent
    max_auto_followups: int = field(default_factory=lambda: int(os.environ.get("MAX_AUTO_FOLLOWUPS", "3")))
    dev_endpoints_enabled: bool = field(default_factory=lambda: os.environ.get("DEV_ENDPOINTS", "1") == "1")
    worker_lease_seconds: int = field(default_factory=lambda: int(os.environ.get("WORKER_LEASE_SECONDS", "60")))
    cors_origins: List[str] = field(default_factory=lambda: os.environ.get("CORS_ORIGINS", "*").split(","))


def load_settings() -> Settings:
    return Settings()
