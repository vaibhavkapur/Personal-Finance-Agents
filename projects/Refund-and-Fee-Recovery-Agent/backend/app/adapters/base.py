"""Provider adapter interface.

Every adapter that performs writes must either support `find_action` (status
lookup by the original client request reference) or advertise
`find_action=False`, in which case an uncertain write sends the case to manual
review instead of generating a new side effect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from ..domain.models import Authority


class ProviderError(Exception):
    """Base provider failure. `outcome_known` tells the caller whether the write definitely did not happen."""

    outcome_known = True

    def __init__(self, message: str, *, code: str = "provider_error") -> None:
        super().__init__(message)
        self.code = code


class ProviderDeclined(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="declined")


class ProviderMalformed(ProviderError):
    """The provider answered, but not in the agreed schema. Treat as unknown."""

    outcome_known = False

    def __init__(self, message: str) -> None:
        super().__init__(message, code="malformed_response")


class ProviderTimeout(ProviderError):
    outcome_known = False

    def __init__(self, message: str = "provider timed out") -> None:
        super().__init__(message, code="timeout")


class ProviderNotFound(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="not_found")


@dataclass(frozen=True)
class Capabilities:
    provider: str
    environment: str  # mock | sandbox | production
    open_case: bool
    send_followup: bool
    get_case: bool
    find_action: bool
    notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ProviderResult:
    provider: str
    environment: str
    operation: str
    data: Dict[str, Any]
    retrieved_at: str
    source: str
    authority: Authority = Authority.simulated

    def as_dict(self) -> Dict[str, Any]:
        d = dict(self.data)
        d.update({
            "provider": self.provider, "environment": self.environment, "operation": self.operation,
            "retrieved_at": self.retrieved_at, "source": self.source, "authority": self.authority.value,
        })
        return d


@dataclass
class ProviderCallback:
    """An asynchronous provider event (webhook) produced by a simulator."""

    provider: str
    event_id: str
    event_type: str
    occurred_at: str
    environment: str
    data: Dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> Dict[str, Any]:
        return {
            "provider": self.provider, "event_id": self.event_id, "type": self.event_type,
            "occurred_at": self.occurred_at, "environment": self.environment, "data": self.data,
        }


class RecoveryProviderAdapter(Protocol):
    provider: str

    def capabilities(self) -> Capabilities: ...
    async def open_case(self, packet: Dict[str, Any], request_ref: str) -> ProviderResult: ...
    async def send_followup(self, case_ref: str, message: Dict[str, Any], request_ref: str) -> ProviderResult: ...
    async def get_case(self, case_ref: str) -> ProviderResult: ...
    async def find_action(self, request_ref: str) -> ProviderResult: ...


class Simulator(Protocol):
    """Extra surface only simulators expose."""

    def due_callbacks(self) -> List[ProviderCallback]: ...
    def set_fault(self, order_ref: str, fault: Optional[str]) -> None: ...
