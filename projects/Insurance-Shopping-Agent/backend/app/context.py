"""Application context shared by API, worker, agent tools and MCP server."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

from .adapters.base import ProviderError, ProviderMalformedResponse, ProviderTimeout
from .adapters.registry import AdapterRegistry, build_registry
from .clock import Clock, build_clock
from .config import Settings, settings as default_settings
from .persistence import models as m
from .persistence.db import Database, new_id

T = TypeVar("T")

REDACT_KEYS = {"display_name", "insured_name", "address", "line1", "applicant", "answers"}


def redact(value: Any, depth: int = 0) -> Any:
    """Remove identity-bearing fields before anything is written to logs."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in REDACT_KEYS:
                out[k] = "[redacted]"
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:20]]
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "…"
    return value


class AppContext:
    def __init__(self, settings: Optional[Settings] = None, database_url: Optional[str] = None, clock: Optional[Clock] = None,
                 registry: Optional[AdapterRegistry] = None) -> None:
        self.settings = settings or default_settings
        self.clock = clock or build_clock(self.settings.fixture_clock, self.settings.fixture_clock_start, self.settings.fixture_clock_frozen)
        self.db = Database(database_url or self.settings.database_url)
        self.db.create_all()
        self.registry = registry or build_registry(self.settings, self.clock)

    def now(self) -> datetime:
        return self.clock.now()

    @property
    def environment(self) -> str:
        return self.settings.environment

    async def call_provider(self, case_id: Optional[str], insurer_id: str, operation: str, coro: Awaitable[T],
                            request_ref: Optional[str] = None, request: Optional[Dict[str, Any]] = None) -> T:
        """Run a provider call outside any DB transaction and log it (redacted)."""
        started = self.now()
        t0 = time.perf_counter()
        outcome = "ok"
        response: Optional[Dict[str, Any]] = None
        try:
            result = await coro
            if isinstance(result, dict):
                response = redact(result)
            return result
        except ProviderTimeout as exc:
            outcome = "timeout"
            response = {"error": str(exc), "outcome": "unknown"}
            raise
        except ProviderMalformedResponse as exc:
            outcome = "malformed"
            response = {"error": str(exc)}
            raise
        except ProviderError as exc:
            outcome = "error"
            response = {"error": str(exc)}
            raise
        except (KeyError, ValueError) as exc:
            outcome = "rejected"
            response = {"error": str(exc)}
            raise
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            with self.db.session() as session:
                session.add(
                    m.ProviderRequestLog(
                        id=new_id("preq"),
                        case_id=case_id,
                        insurer_id=insurer_id,
                        operation=operation,
                        request_ref=request_ref,
                        request_redacted=redact(request or {}),
                        response_redacted=response,
                        environment=self.environment,
                        started_at=started,
                        latency_ms=latency_ms,
                        outcome=outcome,
                    )
                )
