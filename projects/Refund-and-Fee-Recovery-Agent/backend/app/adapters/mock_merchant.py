"""Mock merchant support desk.

Scenarios are keyed by order_ref and describe how the merchant responds over
(fixture) time. The merchant can say "refund issued" without anything ever
appearing on the statement feed; only the statement feed moves money.

Faults (per order or injected at runtime): `declined`, `malformed_response`,
`timeout_after_accept`, `delayed_callback`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..clock import Clock, format_ts
from ..domain.models import Authority
from ..ids import new_id
from .base import (
    Capabilities,
    ProviderCallback,
    ProviderDeclined,
    ProviderMalformed,
    ProviderNotFound,
    ProviderResult,
    ProviderTimeout,
)
from .mock_issuer import MockStatementFeed, ScheduledCallback


@dataclass
class MockMerchantCase:
    case_ref: str
    request_ref: str
    order_ref: str
    status: str
    opened_at: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    refund_ref: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)


class MockMerchantAdapter:
    provider = "merchant_mock"
    environment = "mock"

    def __init__(self, clock: Clock, statement: MockStatementFeed, scenarios: Dict[str, Dict[str, Any]], secret: str, merchant_id: str = "mrc_mock_streaming") -> None:
        self.clock = clock
        self.statement = statement
        self.scenarios = scenarios
        self.secret = secret
        self.merchant_id = merchant_id
        self._cases: Dict[str, MockMerchantCase] = {}
        self._by_request: Dict[str, str] = {}
        self._followups_by_request: Dict[str, Dict[str, Any]] = {}
        self._scheduled: List[ScheduledCallback] = []
        self._faults: Dict[str, Optional[str]] = {}
        self._counter = 0

    # -- capabilities --------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return Capabilities(provider=self.provider, environment=self.environment, open_case=True, send_followup=True, get_case=True, find_action=True,
                            notes="Simulated merchant support desk. Statuses are claims, not money movement.")

    def set_fault(self, order_ref: str, fault: Optional[str]) -> None:
        self._faults[order_ref] = fault

    # -- helpers -------------------------------------------------------------
    def _result(self, operation: str, data: Dict[str, Any]) -> ProviderResult:
        return ProviderResult(provider=self.provider, environment=self.environment, operation=operation, data=data,
                              retrieved_at=format_ts(self.clock.now()), source=f"{self.provider}:{operation}", authority=Authority.simulated)

    def _event_id(self, tag: str) -> str:
        self._counter += 1
        return f"merchant_evt_{tag}_{self._counter:04d}"

    def _schedule(self, due_at: datetime, event_type: str, data: Dict[str, Any]) -> None:
        cb = ProviderCallback(provider=self.provider, event_id=self._event_id(event_type.split(".")[-1]), event_type=event_type,
                              occurred_at=format_ts(due_at), environment=self.environment, data=data)
        self._scheduled.append(ScheduledCallback(due_at=due_at, callback=cb))

    def _scenario(self, order_ref: str) -> Dict[str, Any]:
        return self.scenarios.get(order_ref, self.scenarios.get("default", {"behavior": "acknowledge_only"}))

    # -- RecoveryProviderAdapter --------------------------------------------
    async def open_case(self, packet: Dict[str, Any], request_ref: str) -> ProviderResult:
        if request_ref in self._by_request:
            c = self._cases[self._by_request[request_ref]]
            return self._result("open_case", {"case_ref": c.case_ref, "status": c.status, "idempotent_replay": True})
        order_ref = packet.get("order_ref", "")
        scenario = self._scenario(order_ref)
        fault = self._faults.get(order_ref, scenario.get("fault"))
        if fault == "declined":
            raise ProviderDeclined("merchant support desk rejected the request: order not found in their system (fixture)")
        if fault == "malformed_response":
            raise ProviderMalformed("merchant responded with HTML instead of JSON (fixture)")
        now = self.clock.now()
        case = MockMerchantCase(case_ref=new_id("mcase"), request_ref=request_ref, order_ref=order_ref, status="received", opened_at=format_ts(now))
        case.messages.append({"at": case.opened_at, "direction": "inbound", "subject": packet.get("subject"), "hash": packet.get("message_hash")})
        case.history.append({"at": case.opened_at, "status": "received"})
        self._cases[case.case_ref] = case
        self._by_request[request_ref] = case.case_ref
        self._plan(case, scenario, now, delayed=(fault == "delayed_callback"))
        if fault == "timeout_after_accept":
            raise ProviderTimeout("merchant accepted the case but the connection dropped before the response (fixture)")
        return self._result("open_case", {"case_ref": case.case_ref, "status": case.status})

    def _plan(self, case: MockMerchantCase, scenario: Dict[str, Any], now: datetime, *, delayed: bool) -> None:
        behavior = scenario.get("behavior", "acknowledge_only")
        extra = timedelta(days=scenario.get("delay_days", 7)) if delayed else timedelta(0)
        t_ack = now + timedelta(days=scenario.get("days_to_acknowledge", 1)) + extra
        self._schedule(t_ack, "merchant.case_updated", {"case_ref": case.case_ref, "order_ref": case.order_ref, "status": "processing"})
        if behavior == "acknowledge_only":
            return
        if behavior == "declined_after_review":
            self._schedule(t_ack + timedelta(days=scenario.get("days_to_decision", 3)), "merchant.case_updated",
                           {"case_ref": case.case_ref, "order_ref": case.order_ref, "status": "declined", "reason": scenario.get("decline_reason", "outside refund policy window (fixture)")})
            return
        refund_minor = int(scenario.get("refund_minor", 0))
        currency = scenario.get("currency", "USD")
        t_issue = t_ack + timedelta(days=scenario.get("days_to_refund_issued", 2))
        refund_ref = f"rf_{case.case_ref[-6:]}"
        case.refund_ref = refund_ref
        if behavior == "store_credit":
            self._schedule(t_issue, "merchant.store_credit_issued", {"case_ref": case.case_ref, "order_ref": case.order_ref, "status": "store_credit_issued",
                                                                     "store_credit_ref": f"sc_{case.case_ref[-6:]}", "amount_minor": refund_minor, "currency": currency})
            return
        self._schedule(t_issue, "merchant.case_updated", {"case_ref": case.case_ref, "order_ref": case.order_ref, "status": "refund_issued", "refund_ref": refund_ref,
                                                          "amount_minor": refund_minor, "currency": currency, "destination": "original_payment"})
        if behavior in ("refund_after_followup", "partial_refund"):
            t_post = t_issue + timedelta(days=scenario.get("days_to_post", 3))
            self.statement.schedule_posting(due_at=t_post, kind="refund", amount_minor=refund_minor, currency=currency,
                                            description=f"REFUND {scenario.get('merchant_name', 'MOCK MERCHANT')} {case.order_ref}", provider_ref=refund_ref, merchant_id=self.merchant_id)
        # behavior == "claims_completed_never_posts": no statement posting is scheduled.

    async def send_followup(self, case_ref: str, message: Dict[str, Any], request_ref: str) -> ProviderResult:
        if request_ref in self._followups_by_request:
            return self._result("send_followup", dict(self._followups_by_request[request_ref], idempotent_replay=True))
        case = self._cases.get(case_ref)
        if not case:
            raise ProviderNotFound(f"unknown merchant case {case_ref}")
        fault = self._faults.get(case.order_ref)
        if fault == "timeout_after_accept":
            self._followups_by_request[request_ref] = {"case_ref": case_ref, "message_ref": new_id("mmsg"), "status": case.status}
            case.messages.append({"at": format_ts(self.clock.now()), "direction": "inbound", "hash": message.get("message_hash")})
            raise ProviderTimeout("follow-up accepted but response lost (fixture)")
        msg_ref = new_id("mmsg")
        case.messages.append({"at": format_ts(self.clock.now()), "direction": "inbound", "hash": message.get("message_hash"), "ref": msg_ref})
        data = {"case_ref": case_ref, "message_ref": msg_ref, "status": case.status}
        self._followups_by_request[request_ref] = data
        return self._result("send_followup", data)

    async def get_case(self, case_ref: str) -> ProviderResult:
        case = self._cases.get(case_ref)
        if not case:
            raise ProviderNotFound(f"unknown merchant case {case_ref}")
        return self._result("get_case", {"case_ref": case.case_ref, "status": case.status, "refund_ref": case.refund_ref, "history": case.history, "message_count": len(case.messages)})

    async def find_action(self, request_ref: str) -> ProviderResult:
        if request_ref in self._by_request:
            case = self._cases[self._by_request[request_ref]]
            return self._result("find_action", {"found": True, "kind": "open_case", "case_ref": case.case_ref, "status": case.status})
        if request_ref in self._followups_by_request:
            return self._result("find_action", dict(self._followups_by_request[request_ref], found=True, kind="send_followup"))
        raise ProviderNotFound(f"no action recorded for request {request_ref}")

    # -- Simulator surface ---------------------------------------------------
    def due_callbacks(self) -> List[ProviderCallback]:
        now = self.clock.now()
        out: List[ProviderCallback] = []
        for s in self._scheduled:
            if not s.delivered and s.due_at <= now:
                s.delivered = True
                case = self._cases.get(s.callback.data.get("case_ref", ""))
                if case is not None:
                    case.status = s.callback.data.get("status", case.status)
                    case.history.append({"at": s.callback.occurred_at, "status": case.status})
                out.append(s.callback)
        return out

    def pending(self) -> List[Dict[str, Any]]:
        return [{"due_at": format_ts(s.due_at), "type": s.callback.event_type, "data": s.callback.data} for s in self._scheduled if not s.delivered]

    def case_count(self) -> int:
        return len(self._cases)
