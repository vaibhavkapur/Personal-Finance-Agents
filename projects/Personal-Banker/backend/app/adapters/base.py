"""Provider adapter contract (plan §12).

Every adapter result carries ``_meta`` with ``environment`` (mock | sandbox |
production), ``source``, ``retrieved_at`` and ``authoritative``. Adapters
that perform writes must either support ``find_instruction`` by the original
client request reference or advertise ``can_lookup_by_request_ref=False`` so
uncertain outcomes go to manual review instead of being retried blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(Exception):
    """Base class for provider-side failures."""


class ProviderTimeout(ProviderError):
    """The provider did not answer in time. The outcome is unknown until checked."""


class ProviderDeclined(ProviderError):
    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ProviderMalformedResponse(ProviderError):
    """The provider answered but the response cannot be interpreted."""


class ProviderAccessRevoked(ProviderError):
    """Customer revoked access to this account; no reads or writes allowed."""


class ProviderNotFound(ProviderError):
    """Lookup by reference found nothing."""


@dataclass(frozen=True)
class AdapterCapabilities:
    provider_id: str
    environment: str
    can_read_snapshots: bool
    can_read_offers: bool
    can_submit_instructions: bool
    can_lookup_by_request_ref: bool
    can_cancel_after_acceptance: bool
    supports_callbacks: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BankAdapter(Protocol):
    capabilities: AdapterCapabilities

    async def get_snapshot(self, account_id: str) -> dict: ...

    async def get_offers(self, deposit_id: str) -> list[dict]: ...

    async def submit_instruction(self, payload: dict, request_ref: str) -> dict: ...

    async def find_instruction(self, request_ref: str) -> dict: ...


def meta(environment: str, source: str, retrieved_at: str, authoritative: bool) -> dict:
    return {
        "environment": environment,
        "source": source,
        "retrieved_at": retrieved_at,
        "authoritative": authoritative,
    }
