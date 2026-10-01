"""Delivers due simulator callbacks into the application's provider-event handler.

In a deployment the providers would call `POST /v1/provider-events/recovery`;
here the pump plays the network. Every callback is signed with the mock
provider secret and goes through the same verification, inbox and handlers.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import Simulator
from .mock_issuer import sign_callback


class SimulatorPump:
    def __init__(self, simulators: List[Simulator], handler, secret: str, *, duplicate_deliveries: bool = False) -> None:
        self.simulators = simulators
        self.handler = handler
        self.secret = secret
        self.duplicate_deliveries = duplicate_deliveries  # simulate at-least-once delivery
        self.delivered: List[Dict[str, Any]] = []

    def deliver_due(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        # Statement postings should be visible before provider status updates that refer to them.
        for sim in sorted(self.simulators, key=lambda s: 0 if getattr(s, "provider", "") == "statement_mock" else 1):
            for cb in sim.due_callbacks():
                payload = cb.as_payload()
                signature = sign_callback(self.secret, payload)
                res = self.handler.handle(payload, signature)
                results.append(res)
                self.delivered.append({"payload": payload, "result": res})
                if self.duplicate_deliveries:
                    results.append(self.handler.handle(payload, signature))
        return results
