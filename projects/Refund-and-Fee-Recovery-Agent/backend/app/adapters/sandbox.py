"""Sandbox adapter placeholder.

Implements the same interface as the mocks but advertises only what a real
sandbox would actually offer. No sandbox is wired in this repository; every
call fails loudly rather than pretending. Because `find_action` is not
available, an uncertain write against this adapter must go to manual review.
"""
from __future__ import annotations

from typing import Any, Dict

from .base import Capabilities, ProviderError, ProviderResult


class SandboxMerchantAdapter:
    provider = "merchant_sandbox"
    environment = "sandbox"

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url

    def capabilities(self) -> Capabilities:
        return Capabilities(provider=self.provider, environment=self.environment, open_case=bool(self.base_url), send_followup=False, get_case=bool(self.base_url), find_action=False,
                            notes="Not configured in this repository. Uncertain writes require manual review because status lookup by request reference is unavailable.")

    async def open_case(self, packet: Dict[str, Any], request_ref: str) -> ProviderResult:
        raise ProviderError("sandbox merchant adapter is not configured (set SANDBOX_BASE_URL and implement the client)", code="not_configured")

    async def send_followup(self, case_ref: str, message: Dict[str, Any], request_ref: str) -> ProviderResult:
        raise ProviderError("sandbox merchant adapter does not support follow-ups", code="unsupported")

    async def get_case(self, case_ref: str) -> ProviderResult:
        raise ProviderError("sandbox merchant adapter is not configured", code="not_configured")

    async def find_action(self, request_ref: str) -> ProviderResult:
        raise ProviderError("sandbox merchant adapter cannot look up actions by request reference", code="unsupported")
