"""Claims adapter boundary. Provider credentials never leave the adapter; every result records its environment and authority."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


class ProviderTimeout(Exception):
    """Outcome unknown: the provider may or may not have accepted the write."""


class ProviderDeclined(Exception):
    """The provider explicitly declined the write; no side effect happened."""


class ProviderMalformedResponse(Exception):
    """The provider answered with something we cannot interpret; the outcome is unknown."""


class ProviderNotConfigured(Exception):
    pass


@dataclass
class AdapterCapabilities:
    name: str
    environment: str  # mock | sandbox | production
    submit_claim: bool = True
    add_evidence: bool = True
    get_claim: bool = True
    find_submission: bool = True
    uncertain_requires_manual_review: bool = False
    protocol: str = "in-process"
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter": self.name,
            "environment": self.environment,
            "capabilities": {
                "submit_claim": self.submit_claim,
                "add_evidence": self.add_evidence,
                "get_claim": self.get_claim,
                "find_submission": self.find_submission,
            },
            "uncertain_requires_manual_review": self.uncertain_requires_manual_review,
            "protocol": self.protocol,
            "notes": self.notes,
        }


def provider_result(payload: Dict[str, Any], *, environment: str, authority: str, retrieved_at: str, source: str) -> Dict[str, Any]:
    out = dict(payload)
    out.update({"environment": environment, "authority": authority, "retrieved_at": retrieved_at, "source": source})
    return out


class ClaimsAdapter(Protocol):
    capabilities: AdapterCapabilities

    async def submit_claim(self, packet: dict, request_ref: str) -> dict: ...

    async def add_evidence(self, claim_ref: str, packet: dict, request_ref: str) -> dict: ...

    async def get_claim(self, claim_ref: str) -> dict: ...

    async def find_submission(self, request_ref: str) -> dict: ...
