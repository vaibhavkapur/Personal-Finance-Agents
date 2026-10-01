"""Mock payout feed. Produces signed payment events for a decided claim: exact, smaller, delayed, provisional or unrelated."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List

from ..clock import Clock, iso
from ..ids import new_id
from .mock_insurer import sign_payload

PROVIDER_NAME = "mock_payments"


class MockPaymentFeed:
    def __init__(self, clock: Clock, webhook_secret: str):
        self.clock = clock
        self.secret = webhook_secret

    def generate(self, *, claim_reference: str, payee_id: str, approved_minor: int, currency: str, mode: str) -> List[Dict[str, Any]]:
        now = self.clock.now()
        events: List[Dict[str, Any]] = []

        def event(amount: int, status: str = "posted", ref: str = None, posted_at=None, claim_ref: str = None, payee: str = None):
            payload = {
                "id": new_id("mockpay"),
                "type": "payment.posted" if status == "posted" else "payment.provisional",
                "environment": "mock",
                "occurred_at": iso(posted_at or now),
                "data": {
                    "payment_ref": ref or new_id("pay_mock"),
                    "claim_reference": claim_ref or claim_reference,
                    "payee_id": payee or payee_id,
                    "amount_minor": amount,
                    "currency": currency,
                    "status": status,
                    "posted_at": iso(posted_at or now),
                },
            }
            return {"payload": payload, "signature": sign_payload(self.secret, payload)}

        if mode == "exact":
            events.append(event(approved_minor))
        elif mode == "smaller":
            events.append(event(max(approved_minor - 2000, 0)))
        elif mode == "remainder":
            events.append(event(2000))
        elif mode == "delayed":
            events.append(event(approved_minor, posted_at=now + timedelta(days=3)))
        elif mode == "provisional":
            events.append(event(approved_minor, status="provisional"))
        elif mode == "unrelated":
            events.append(event(approved_minor, claim_ref="insurer_mock_other"))
        elif mode == "wrong_payee":
            events.append(event(approved_minor, payee="cus_demo_4"))
        elif mode == "split":
            events.append(event(approved_minor // 2))
            events.append(event(approved_minor - approved_minor // 2))
        else:
            raise ValueError(f"unknown payment mode {mode}")
        return events
