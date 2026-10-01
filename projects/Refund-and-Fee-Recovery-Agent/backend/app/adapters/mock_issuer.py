"""Mock card issuer and account (statement) feed.

`MockStatementFeed` is the synthetic card statement. Merchant refunds and issuer
credits both land here as `statement.transaction_posted` callbacks, which is
how the application learns that money actually moved. The merchant saying
"refund issued" never writes to this feed directly.

`MockIssuerAdapter` is the separate dispute lane. It can post a provisional
credit, reverse it, and later resolve the dispute.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..clock import Clock, format_ts, parse_ts
from ..domain.models import Authority
from ..ids import canonical_json, new_id
from .base import (
    Capabilities,
    ProviderCallback,
    ProviderDeclined,
    ProviderMalformed,
    ProviderNotFound,
    ProviderResult,
    ProviderTimeout,
)


def sign_callback(secret: str, payload: Dict[str, Any]) -> str:
    return "hmac-sha256=" + hmac.new(secret.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass
class ScheduledCallback:
    due_at: datetime
    callback: ProviderCallback
    delivered: bool = False


class MockStatementFeed:
    provider = "statement_mock"
    environment = "mock"

    def __init__(self, clock: Clock, *, customer_id: str, instrument_ref: str) -> None:
        self.clock = clock
        self.customer_id = customer_id
        self.instrument_ref = instrument_ref
        self._scheduled: List[ScheduledCallback] = []
        self._counter = 0

    def _event_id(self, tag: str) -> str:
        self._counter += 1
        return f"stmt_evt_{tag}_{self._counter:04d}"

    def schedule_posting(
        self,
        *,
        due_at: datetime,
        kind: str,
        amount_minor: int,
        currency: str,
        description: str,
        provider_ref: str,
        merchant_id: Optional[str],
        direction: str = "credit",
        reverses_provider_ref: Optional[str] = None,
        transaction_id: Optional[str] = None,
    ) -> str:
        txn_id = transaction_id or f"txn_{provider_ref}"
        cb = ProviderCallback(
            provider=self.provider,
            event_id=self._event_id(kind),
            event_type="statement.transaction_posted",
            occurred_at=format_ts(due_at),
            environment=self.environment,
            data={
                "transaction_id": txn_id,
                "customer_id": self.customer_id,
                "payment_instrument_ref": self.instrument_ref,
                "merchant_id": merchant_id,
                "direction": direction,
                "kind": kind,
                "amount_minor": amount_minor,
                "currency": currency,
                "posted_at": format_ts(due_at),
                "description": description,
                "provider_ref": provider_ref,
                "reverses_provider_ref": reverses_provider_ref,
            },
        )
        self._scheduled.append(ScheduledCallback(due_at=due_at, callback=cb))
        return txn_id

    def due_callbacks(self) -> List[ProviderCallback]:
        now = self.clock.now()
        out: List[ProviderCallback] = []
        for s in self._scheduled:
            if not s.delivered and s.due_at <= now:
                s.delivered = True
                out.append(s.callback)
        return out

    def pending(self) -> List[Dict[str, Any]]:
        return [{"due_at": format_ts(s.due_at), "type": s.callback.event_type, "data": s.callback.data} for s in self._scheduled if not s.delivered]


@dataclass
class MockDispute:
    dispute_ref: str
    request_ref: str
    order_ref: str
    amount_minor: int
    currency: str
    reason: str
    status: str
    opened_at: str
    provisional_txn_ref: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)


class MockIssuerAdapter:
    """Dispute lane simulator. Scenarios are keyed by order_ref in fixtures."""

    provider = "issuer_mock"
    environment = "mock"

    def __init__(self, clock: Clock, statement: MockStatementFeed, scenarios: Dict[str, Dict[str, Any]], secret: str) -> None:
        self.clock = clock
        self.statement = statement
        self.scenarios = scenarios
        self.secret = secret
        self._disputes: Dict[str, MockDispute] = {}
        self._by_request: Dict[str, str] = {}
        self._scheduled: List[ScheduledCallback] = []
        self._faults: Dict[str, Optional[str]] = {}
        self._counter = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(provider=self.provider, environment=self.environment, open_case=True, send_followup=False, get_case=True, find_action=True,
                            notes="Simulated issuer dispute lane. Provisional credits can reverse.")

    def set_fault(self, order_ref: str, fault: Optional[str]) -> None:
        self._faults[order_ref] = fault

    def _result(self, operation: str, data: Dict[str, Any]) -> ProviderResult:
        return ProviderResult(provider=self.provider, environment=self.environment, operation=operation, data=data,
                              retrieved_at=format_ts(self.clock.now()), source=f"{self.provider}:{operation}", authority=Authority.simulated)

    def _event_id(self, tag: str) -> str:
        self._counter += 1
        return f"issuer_evt_{tag}_{self._counter:04d}"

    def _schedule(self, due_at: datetime, event_type: str, data: Dict[str, Any]) -> None:
        cb = ProviderCallback(provider=self.provider, event_id=self._event_id(event_type.split(".")[-1]), event_type=event_type,
                              occurred_at=format_ts(due_at), environment=self.environment, data=data)
        self._scheduled.append(ScheduledCallback(due_at=due_at, callback=cb))

    async def open_case(self, packet: Dict[str, Any], request_ref: str) -> ProviderResult:
        if request_ref in self._by_request:
            d = self._disputes[self._by_request[request_ref]]
            return self._result("open_case", {"case_ref": d.dispute_ref, "status": d.status, "idempotent_replay": True})
        order_ref = packet.get("order_ref", "")
        scenario = self.scenarios.get(order_ref, self.scenarios.get("default", {}))
        fault = self._faults.get(order_ref, scenario.get("fault"))
        if fault == "declined":
            raise ProviderDeclined("issuer declined to open a dispute for this transaction (fixture)")
        if fault == "malformed_response":
            raise ProviderMalformed("issuer returned an unparseable response (fixture)")
        now = self.clock.now()
        dispute = MockDispute(dispute_ref=new_id("dsp"), request_ref=request_ref, order_ref=order_ref, amount_minor=int(packet["amount_minor"]),
                              currency=packet["currency"], reason=packet["reason_code"], status="received", opened_at=format_ts(now))
        dispute.history.append({"at": dispute.opened_at, "status": "received"})
        self._disputes[dispute.dispute_ref] = dispute
        self._by_request[request_ref] = dispute.dispute_ref
        self._plan(dispute, scenario, now)
        if fault == "timeout_after_accept":
            raise ProviderTimeout("issuer accepted the dispute but the response timed out (fixture)")
        return self._result("open_case", {"case_ref": dispute.dispute_ref, "status": dispute.status, "provisional_credit_expected_days": scenario.get("days_to_provisional")})

    def _plan(self, d: MockDispute, scenario: Dict[str, Any], now: datetime) -> None:
        behavior = scenario.get("behavior", "provisional_then_final")
        if behavior == "declined_after_review":
            self._schedule(now + timedelta(days=scenario.get("days_to_decision", 10)), "issuer.dispute_updated",
                           {"dispute_ref": d.dispute_ref, "status": "declined", "reason": "merchant provided compelling evidence (fixture)"})
            return
        t_prov = now + timedelta(days=scenario.get("days_to_provisional", 2))
        prov_ref = f"{d.dispute_ref}:prov"
        d.provisional_txn_ref = prov_ref
        self.statement.schedule_posting(due_at=t_prov, kind="provisional_credit", amount_minor=d.amount_minor, currency=d.currency,
                                        description=f"PROVISIONAL CREDIT DISPUTE {d.dispute_ref}", provider_ref=prov_ref, merchant_id=None)
        self._schedule(t_prov, "issuer.dispute_updated", {"dispute_ref": d.dispute_ref, "status": "provisional_credit_posted", "transaction_provider_ref": prov_ref, "amount_minor": d.amount_minor, "currency": d.currency})
        cursor = t_prov
        if behavior == "provisional_then_reversed_then_final":
            t_rev = cursor + timedelta(days=scenario.get("days_to_reversal", 5))
            rev_ref = f"{d.dispute_ref}:rev"
            self.statement.schedule_posting(due_at=t_rev, kind="reversal", direction="debit", amount_minor=d.amount_minor, currency=d.currency,
                                            description=f"REVERSAL PROVISIONAL CREDIT {d.dispute_ref}", provider_ref=rev_ref, merchant_id=None, reverses_provider_ref=prov_ref)
            self._schedule(t_rev, "issuer.dispute_updated", {"dispute_ref": d.dispute_ref, "status": "provisional_credit_reversed", "transaction_provider_ref": rev_ref, "reverses_provider_ref": prov_ref})
            cursor = t_rev
            t_final = cursor + timedelta(days=scenario.get("days_to_final", 7))
            final_ref = f"{d.dispute_ref}:final"
            self.statement.schedule_posting(due_at=t_final, kind="refund", amount_minor=d.amount_minor, currency=d.currency,
                                            description=f"DISPUTE CREDIT FINAL {d.dispute_ref}", provider_ref=final_ref, merchant_id=None)
            self._schedule(t_final, "issuer.dispute_updated", {"dispute_ref": d.dispute_ref, "status": "resolved_in_favor", "transaction_provider_ref": final_ref, "final_credit_is_new_transaction": True})
        elif behavior == "provisional_then_final":
            t_final = cursor + timedelta(days=scenario.get("days_to_final", 7))
            self._schedule(t_final, "issuer.dispute_updated", {"dispute_ref": d.dispute_ref, "status": "resolved_in_favor", "transaction_provider_ref": prov_ref, "final_credit_is_new_transaction": False})
        elif behavior == "provisional_then_reversed_declined":
            t_rev = cursor + timedelta(days=scenario.get("days_to_reversal", 5))
            rev_ref = f"{d.dispute_ref}:rev"
            self.statement.schedule_posting(due_at=t_rev, kind="reversal", direction="debit", amount_minor=d.amount_minor, currency=d.currency,
                                            description=f"REVERSAL PROVISIONAL CREDIT {d.dispute_ref}", provider_ref=rev_ref, merchant_id=None, reverses_provider_ref=prov_ref)
            self._schedule(t_rev, "issuer.dispute_updated", {"dispute_ref": d.dispute_ref, "status": "declined", "transaction_provider_ref": rev_ref, "reverses_provider_ref": prov_ref})

    async def send_followup(self, case_ref: str, message: Dict[str, Any], request_ref: str) -> ProviderResult:
        raise ProviderNotFound("issuer lane does not accept free-form follow-ups in this simulator")

    async def get_case(self, case_ref: str) -> ProviderResult:
        d = self._disputes.get(case_ref)
        if not d:
            raise ProviderNotFound(f"unknown dispute {case_ref}")
        return self._result("get_case", {"case_ref": d.dispute_ref, "status": d.status, "history": d.history})

    async def find_action(self, request_ref: str) -> ProviderResult:
        ref = self._by_request.get(request_ref)
        if not ref:
            raise ProviderNotFound(f"no dispute for request {request_ref}")
        d = self._disputes[ref]
        return self._result("find_action", {"found": True, "case_ref": d.dispute_ref, "status": d.status})

    def due_callbacks(self) -> List[ProviderCallback]:
        now = self.clock.now()
        out: List[ProviderCallback] = []
        for s in self._scheduled:
            if not s.delivered and s.due_at <= now:
                s.delivered = True
                d = self._disputes.get(s.callback.data.get("dispute_ref", ""))
                if d is not None:
                    d.status = s.callback.data.get("status", d.status)
                    d.history.append({"at": s.callback.occurred_at, "status": d.status})
                out.append(s.callback)
        return out

    def pending(self) -> List[Dict[str, Any]]:
        return [{"due_at": format_ts(s.due_at), "type": s.callback.event_type, "data": s.callback.data} for s in self._scheduled if not s.delivered]
