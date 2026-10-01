"""Adapter lookup by provider. Credentials (if any) stay inside adapters."""

from __future__ import annotations

from app.adapters.mock_bank import MockBankAdapter
from app.adapters.sandbox_plaid import SandboxAccountDataAdapter
from app.config import settings
from app.persistence.db import session_factory


def adapter_for(provider_id: str, case_id: str | None = None):
    if settings.environment == "mock" or provider_id.startswith("bank_"):
        return MockBankAdapter(provider_id, session_factory(), case_id=case_id)
    return SandboxAccountDataAdapter(provider_id)


def capability_matrix() -> list[dict]:
    return [
        MockBankAdapter("bank_harbor", session_factory()).capabilities.to_dict(),
        MockBankAdapter("bank_northwind", session_factory()).capabilities.to_dict(),
        MockBankAdapter("bank_meridian", session_factory()).capabilities.to_dict(),
        SandboxAccountDataAdapter().capabilities.to_dict(),
    ]
