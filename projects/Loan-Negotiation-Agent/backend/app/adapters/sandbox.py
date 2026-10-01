"""Sandbox adapter placeholder.

A real lender sandbox would implement ``LenderAdapter`` here. Until an actual
integration exists this adapter advertises *no* write capabilities and refuses
every operation, so nothing can be mistaken for a live or sandbox side effect.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from .base import AdapterCapabilities, ProviderDeclined, ProviderResult


class SandboxLenderAdapter:
    capabilities = AdapterCapabilities(
        environment="sandbox",
        request_offer=False,
        send_negotiation=False,
        submit_application=False,
        provide_documents=False,
        get_application_by_request_ref=False,
        get_negotiation_by_request_ref=False,
        request_closing=False,
        webhooks=False,
        uncertain_outcome_requires_manual_review=True,
    )

    def __init__(self, base_url: str = "", api_key: str = ""):
        self.base_url = base_url
        self._api_key = api_key  # never logged or returned

    def _unsupported(self, op: str) -> ProviderResult:
        raise ProviderDeclined(f"sandbox adapter does not support {op}; no provider integration is configured")

    async def request_offer(self, request: Dict[str, Any], request_ref: str) -> ProviderResult:
        return self._unsupported("request_offer")

    async def send_negotiation(self, message: Dict[str, Any], request_ref: str) -> ProviderResult:
        return self._unsupported("send_negotiation")

    async def submit_application(self, packet: Dict[str, Any], request_ref: str) -> ProviderResult:
        return self._unsupported("submit_application")

    async def get_application(self, request_ref: str) -> ProviderResult:
        return ProviderResult(ok=False, environment="sandbox", source="sandbox_adapter", retrieved_at=datetime.now(timezone.utc), authority="authoritative", error="not_supported", request_ref=request_ref)

    async def get_negotiation(self, request_ref: str) -> ProviderResult:
        return ProviderResult(ok=False, environment="sandbox", source="sandbox_adapter", retrieved_at=datetime.now(timezone.utc), authority="authoritative", error="not_supported", request_ref=request_ref)

    async def provide_documents(self, application_ref: str, documents: Dict[str, Any], request_ref: str) -> ProviderResult:
        return self._unsupported("provide_documents")

    async def request_closing(self, application_ref: str, request_ref: str) -> ProviderResult:
        return self._unsupported("request_closing")
