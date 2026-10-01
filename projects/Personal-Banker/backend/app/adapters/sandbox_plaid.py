"""Account-data sandbox adapter (read-only capability shape).

This adapter implements the same interface as the mock bank but advertises
only the capabilities an account-data aggregator sandbox actually offers:
snapshot reads. It does not offer products, submit instructions or look up
writes, so any case bound to it can never reach ``submitted`` and an
uncertain outcome would go to manual review.

Network calls are not made unless ``PB_PLAID_CLIENT_ID``/``PB_PLAID_SECRET``
are configured *and* a link token has been exchanged out of band; the default
behaviour is to report the capability gap honestly.
"""

from __future__ import annotations

import os

from app.adapters.base import AdapterCapabilities, ProviderError, meta


class SandboxAccountDataAdapter:
    def __init__(self, provider_id: str = "sandbox_aggregator"):
        self.provider_id = provider_id
        configured = bool(os.getenv("PB_PLAID_CLIENT_ID") and os.getenv("PB_PLAID_SECRET"))
        self.capabilities = AdapterCapabilities(
            provider_id=provider_id,
            environment="sandbox",
            can_read_snapshots=configured,
            can_read_offers=False,
            can_submit_instructions=False,
            can_lookup_by_request_ref=False,
            can_cancel_after_acceptance=False,
            supports_callbacks=False,
            notes=(
                "Read-only account data. Sandbox transfers do not update other products' data, "
                "so the application keeps its own coherent mock ledger for execution."
                + ("" if configured else " Credentials not configured: reads disabled.")
            ),
        )

    async def get_snapshot(self, account_id: str) -> dict:
        if not self.capabilities.can_read_snapshots:
            raise ProviderError("sandbox credentials not configured")
        # A real implementation would call /accounts/balance/get here. It is
        # intentionally not implemented in this prototype.
        raise ProviderError("sandbox snapshot retrieval is not implemented in this prototype")

    async def get_offers(self, deposit_id: str) -> list[dict]:
        raise ProviderError("capability not available: offers")

    async def submit_instruction(self, payload: dict, request_ref: str) -> dict:
        raise ProviderError("capability not available: submit_instruction")

    async def find_instruction(self, request_ref: str) -> dict:
        raise ProviderError("capability not available: find_instruction")

    def describe(self) -> dict:
        return {"capabilities": self.capabilities.to_dict(), "_meta": meta("sandbox", "sandbox_aggregator", "", False)}
