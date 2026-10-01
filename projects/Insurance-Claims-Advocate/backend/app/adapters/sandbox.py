"""Sandbox adapter skeleton. Implements the same interface but advertises only the capabilities actually available.
No real insurer sandbox is wired; calls fail loudly until SANDBOX_BASE_URL and credentials are configured."""
from __future__ import annotations

from typing import Any, Dict

import httpx

from ..clock import Clock
from .base import AdapterCapabilities, ProviderNotConfigured, ProviderTimeout, provider_result


class SandboxClaimsAdapter:
    def __init__(self, clock: Clock, base_url: str, api_key: str = ""):
        self.clock = clock
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key  # stays inside the adapter
        self.capabilities = AdapterCapabilities(
            name="sandbox_insurer",
            environment="sandbox",
            submit_claim=True,
            add_evidence=True,
            get_claim=True,
            find_submission=False,  # not offered by this hypothetical sandbox
            uncertain_requires_manual_review=True,
            protocol="https",
            notes="Skeleton only. No insurer sandbox has been onboarded; configure SANDBOX_BASE_URL to enable.",
        )

    def _client(self) -> httpx.AsyncClient:
        if not self.base_url:
            raise ProviderNotConfigured("SANDBOX_BASE_URL is not configured")
        return httpx.AsyncClient(base_url=self.base_url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=10.0)

    async def _post(self, path: str, payload: Dict[str, Any], request_ref: str) -> Dict[str, Any]:
        async with self._client() as client:
            try:
                resp = await client.post(path, json=payload, headers={"Idempotency-Key": request_ref})
            except httpx.TimeoutException as exc:
                raise ProviderTimeout(str(exc))
            resp.raise_for_status()
            return provider_result(resp.json(), environment="sandbox", authority="authoritative", retrieved_at=self.clock.now_iso(), source="sandbox_insurer")

    async def submit_claim(self, packet: dict, request_ref: str) -> dict:
        return await self._post("/claims", packet, request_ref)

    async def add_evidence(self, claim_ref: str, packet: dict, request_ref: str) -> dict:
        return await self._post(f"/claims/{claim_ref}/evidence", packet, request_ref)

    async def get_claim(self, claim_ref: str) -> dict:
        async with self._client() as client:
            resp = await client.get(f"/claims/{claim_ref}")
            resp.raise_for_status()
            return provider_result(resp.json(), environment="sandbox", authority="authoritative", retrieved_at=self.clock.now_iso(), source="sandbox_insurer")

    async def find_submission(self, request_ref: str) -> dict:
        raise ProviderNotConfigured("sandbox adapter does not support lookup by request reference; uncertain writes require manual review")
