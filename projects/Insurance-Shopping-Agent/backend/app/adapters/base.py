"""InsurerQuoteAdapter protocol and shared result conventions."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Protocol


class ProviderError(Exception):
    """A definite provider failure; the outcome is known (it did not happen)."""


class ProviderTimeout(ProviderError):
    """The outcome is UNKNOWN until checked by request reference."""


class ProviderMalformedResponse(ProviderError):
    """The provider answered with something that does not match the agreed schema."""


@dataclass(frozen=True)
class AdapterCapabilities:
    environment: str  # mock | sandbox | production
    protocol: str  # direct | a2a/0.3.0
    status_lookup_by_request_ref: bool
    supports_callbacks: bool
    supports_revised_offer_acceptance: bool
    uncertain_outcome_requires_manual_review: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def provider_source(insurer_id: str, environment: str, protocol: str, retrieved_at: datetime, authority: str = "simulated") -> Dict[str, Any]:
    """Every provider result carries its source, retrieval time and authority."""
    if environment == "production":
        authority = "authoritative"
    elif environment == "sandbox" and authority == "simulated":
        authority = "estimated"
    return {
        "provider": insurer_id,
        "environment": environment,
        "protocol": protocol,
        "retrieved_at": retrieved_at.isoformat(),
        "authority": authority,
    }


class InsurerQuoteAdapter(Protocol):
    insurer_id: str

    def capabilities(self) -> AdapterCapabilities: ...

    async def request_quote(self, needs: Dict[str, Any], request_ref: str, answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]: ...

    async def answer_question(self, task_ref: str, answer: Dict[str, Any]) -> Dict[str, Any]: ...

    async def get_task(self, task_ref: str) -> Dict[str, Any]: ...

    async def submit_application(self, payload: Dict[str, Any], request_ref: str) -> Dict[str, Any]: ...

    async def accept_revised_offer(self, submission_ref: str, quote_ref: str, quote_version: int, request_ref: str) -> Dict[str, Any]: ...

    async def get_policy_status(self, request_ref: str) -> Dict[str, Any]: ...
