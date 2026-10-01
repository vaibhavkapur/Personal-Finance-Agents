"""Provider adapter contract.

Every adapter result carries ``environment`` (mock|sandbox|production), the
``source`` and ``retrieved_at`` timestamp, and an ``authority`` label so the
agent and UI can distinguish authoritative provider facts from simulated ones.
Adapters that perform writes must support lookup by the original client
request reference or advertise that an uncertain outcome needs manual review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Protocol


class ProviderError(Exception):
    """Base class for provider failures."""


class ProviderTimeout(ProviderError):
    """The call did not complete; the outcome is unknown until checked by reference."""


class ProviderMalformedResponse(ProviderError):
    """The provider returned something the adapter could not parse."""


class ProviderDeclined(ProviderError):
    """The provider explicitly declined the action (a known outcome)."""


@dataclass(frozen=True)
class AdapterCapabilities:
    environment: str
    request_offer: bool = False
    send_negotiation: bool = False
    submit_application: bool = False
    provide_documents: bool = False
    get_application_by_request_ref: bool = False
    get_negotiation_by_request_ref: bool = False
    request_closing: bool = False
    webhooks: bool = False
    uncertain_outcome_requires_manual_review: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ProviderResult:
    ok: bool
    environment: str
    source: str
    retrieved_at: datetime
    authority: str  # authoritative|estimated|simulated
    data: Dict[str, Any] = field(default_factory=dict)
    provider_reference: Optional[str] = None
    request_ref: Optional[str] = None
    error: Optional[str] = None
    latency_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "environment": self.environment,
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat(),
            "authority": self.authority,
            "provider_reference": self.provider_reference,
            "request_ref": self.request_ref,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "data": self.data,
        }


class LenderAdapter(Protocol):
    capabilities: AdapterCapabilities

    async def request_offer(self, request: Dict[str, Any], request_ref: str) -> ProviderResult: ...

    async def send_negotiation(self, message: Dict[str, Any], request_ref: str) -> ProviderResult: ...

    async def submit_application(self, packet: Dict[str, Any], request_ref: str) -> ProviderResult: ...

    async def get_application(self, request_ref: str) -> ProviderResult: ...

    async def get_negotiation(self, request_ref: str) -> ProviderResult: ...

    async def provide_documents(self, application_ref: str, documents: Dict[str, Any], request_ref: str) -> ProviderResult: ...

    async def request_closing(self, application_ref: str, request_ref: str) -> ProviderResult: ...
