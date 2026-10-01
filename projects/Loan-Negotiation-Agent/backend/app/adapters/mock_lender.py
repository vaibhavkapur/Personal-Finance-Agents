"""Mock lender network: a deterministic, credential-free simulator.

Behaviours are seeded from ``fixtures/lenders.json``:

* ``refuse``                      - declines to reprice (current servicer)
* ``small_credit``                - returns a versioned counteroffer with a small lender credit
* ``match_rate_increase_fees``    - matches the competing rate/term but adds fees
* ``request_income_proof``        - asks for proof of income before repricing

Application behaviours:

* ``accept_with_income_condition``  - accepts, requests an income document, then issues final terms
* ``accept_with_increased_costs``   - accepts, then issues final terms with higher financed costs
* ``request_income_proof``          - accepts, requests income and employment evidence
* ``not_offered``                   - declines (servicer does not originate through this channel)

The simulator exposes a controllable clock, fault injection (timeout after
acceptance, malformed response, declined action, delayed callback, quote
expiry, changed closing costs) and signed callback events. Every result is
labelled ``environment="mock"`` and ``authority="simulated"``.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..clock import Clock
from ..domain.amortization import monthly_payment_minor
from ..domain.money import normalize_rate
from .base import (
    AdapterCapabilities,
    ProviderDeclined,
    ProviderMalformedResponse,
    ProviderResult,
    ProviderTimeout,
)

ENV = "mock"
SOURCE = "mock_lender_network"

FAULT_TIMEOUT_AFTER_ACCEPT = "timeout_after_accept"
FAULT_MALFORMED = "malformed_response"
FAULT_DECLINED = "declined"
FAULT_DELAYED_CALLBACK = "delayed_callback"
FAULT_QUOTE_EXPIRED = "quote_expired"
FAULT_CHANGED_CLOSING_COSTS = "changed_closing_costs"

INCOME_DOC_KINDS = {"income_evidence", "pay_stub", "w2", "tax_return"}


def sign_payload(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def canonical_body(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


class MockLenderNetwork:
    """In-memory simulator shared by the adapter, the A2A lender agents and the HTTP mock routes."""

    def __init__(self, lenders: List[Dict[str, Any]], clock: Clock, processing_delay_days: int = 2):
        self.clock = clock
        self.lenders: Dict[str, Dict[str, Any]] = {l["id"]: l for l in lenders}
        self.processing_delay = timedelta(days=processing_delay_days)
        self.negotiations: Dict[str, Dict[str, Any]] = {}
        self.applications: Dict[str, Dict[str, Any]] = {}  # by client request_ref
        self.applications_by_ref: Dict[str, Dict[str, Any]] = {}  # by provider application_ref
        self.callbacks: List[Dict[str, Any]] = []
        self.faults: List[Dict[str, Any]] = []
        self.request_log: List[Dict[str, Any]] = []
        self._counter = 0

    # ------------------------------------------------------------------ utilities
    def _ref(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_mock_{self._counter:04d}"

    def inject_fault(self, kind: str, lender_id: Optional[str] = None, operation: Optional[str] = None, once: bool = True, **params: Any) -> None:
        self.faults.append({"kind": kind, "lender_id": lender_id, "operation": operation, "once": once, "params": params})

    def _take_fault(self, kind: str, lender_id: str, operation: str) -> Optional[Dict[str, Any]]:
        for fault in list(self.faults):
            if fault["kind"] != kind:
                continue
            if fault["lender_id"] not in (None, lender_id):
                continue
            if fault["operation"] not in (None, operation):
                continue
            if fault["once"]:
                self.faults.remove(fault)
            return fault
        return None

    def _log(self, operation: str, lender_id: str, request_ref: str, outcome: str) -> None:
        self.request_log.append(
            {"operation": operation, "lender_id": lender_id, "request_ref": request_ref, "outcome": outcome, "at": self.clock.now().isoformat()}
        )

    def _result(self, data: Dict[str, Any], provider_reference: Optional[str], request_ref: str, ok: bool = True, error: Optional[str] = None) -> ProviderResult:
        return ProviderResult(
            ok=ok,
            environment=ENV,
            source=SOURCE,
            retrieved_at=self.clock.now(),
            authority="simulated",
            data=data,
            provider_reference=provider_reference,
            request_ref=request_ref,
            error=error,
        )

    def _queue_callback(self, lender_id: str, event_type: str, data: Dict[str, Any], delay: timedelta = timedelta(0)) -> Dict[str, Any]:
        lender = self.lenders[lender_id]
        event = {
            "id": self._ref("evt"),
            "type": event_type,
            "provider": lender_id,
            "occurred_at": self.clock.now().isoformat(),
            "deliver_at": (self.clock.now() + delay).isoformat(),
            "environment": ENV,
            "data": data,
        }
        body = canonical_body({k: v for k, v in event.items() if k != "deliver_at"})
        event["signature"] = sign_payload(lender["webhook_secret"], body)
        self.callbacks.append(event)
        return event

    def due_callbacks(self) -> List[Dict[str, Any]]:
        now = self.clock.now()
        due = [c for c in self.callbacks if c["deliver_at"] <= now.isoformat() and not c.get("delivered")]
        return due

    def mark_delivered(self, event_id: str) -> None:
        for c in self.callbacks:
            if c["id"] == event_id:
                c["delivered"] = True

    def webhook_secret(self, lender_id: str) -> Optional[str]:
        lender = self.lenders.get(lender_id)
        return lender["webhook_secret"] if lender else None

    # --------------------------------------------------------------- negotiation
    def _revised_offer(self, lender_id: str, base: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
        offer = copy.deepcopy(base)
        offer.update(changes)
        offer["lender_id"] = lender_id
        offer["status"] = "revised_quote"
        offer["version"] = int(base.get("version", 1)) + 1
        offer["issued_at"] = self.clock.now().isoformat()
        offer["expires_at"] = (self.clock.now() + timedelta(days=14)).isoformat()
        if offer.get("note_rate_decimal") and offer.get("term_months") and offer.get("principal_minor"):
            offer["monthly_pi_minor"] = monthly_payment_minor(int(offer["principal_minor"]), normalize_rate(offer["note_rate_decimal"]), int(offer["term_months"]))
        offer["provider_reference"] = self._ref("quote")
        return offer

    def send_negotiation(self, message: Dict[str, Any], request_ref: str) -> Dict[str, Any]:
        lender_id = message["lender_id"]
        lender = self.lenders.get(lender_id)
        if lender is None:
            raise ProviderDeclined(f"unknown lender {lender_id}")
        if request_ref in self.negotiations:
            self._log("send_negotiation", lender_id, request_ref, "duplicate_returned")
            return self.negotiations[request_ref]["response"]

        if self._take_fault(FAULT_MALFORMED, lender_id, "send_negotiation"):
            self._log("send_negotiation", lender_id, request_ref, "malformed")
            raise ProviderMalformedResponse("<html>502 Bad Gateway</html>")

        behaviour = lender["negotiation_behaviour"]
        target = message.get("target_offer") or {}
        competing = message.get("competing_offer") or {}
        disclosed = message.get("disclosed_documents") or []
        provider_ref = self._ref("neg")

        if self._take_fault(FAULT_QUOTE_EXPIRED, lender_id, "send_negotiation"):
            response = {"outcome": "quote_expired", "reason": "The referenced quote has expired; request a refreshed Loan Estimate.", "negotiation_ref": provider_ref}
        elif behaviour == "refuse":
            response = {
                "outcome": "refused",
                "reason": "We do not reprice existing notes. You may apply for a new refinance loan through our origination channel.",
                "negotiation_ref": provider_ref,
            }
        elif behaviour == "small_credit":
            revised = self._revised_offer(
                lender_id,
                target,
                lender_credits_minor=int(target.get("lender_credits_minor", 0)) + 25000,
                rate_lock={"locked": False, "lock_period_days": 45, "note": "Rate lock available on request."},
            )
            response = {"outcome": "counteroffer", "offer": revised, "negotiation_ref": provider_ref, "reason": "We can add a $250 lender credit; the rate is our best pricing at this time."}
        elif behaviour == "match_rate_increase_fees":
            if not competing.get("note_rate_decimal"):
                response = {"outcome": "facts_requested", "requested_facts": ["competing_offer_terms"], "negotiation_ref": provider_ref}
            else:
                items = copy.deepcopy(target.get("cost_items", []))
                items.append({"category": "A", "label": "Rate match pricing adjustment", "amount_minor": 200000, "source": {"page": 2, "section": "A"}})
                revised = self._revised_offer(
                    lender_id,
                    target,
                    note_rate_decimal=str(normalize_rate(competing["note_rate_decimal"])),
                    term_months=int(competing.get("term_months") or target.get("term_months")),
                    cost_items=items,
                )
                response = {
                    "outcome": "counteroffer",
                    "offer": revised,
                    "negotiation_ref": provider_ref,
                    "reason": "We can match the competing rate and term with a pricing adjustment added to origination charges.",
                }
        elif behaviour == "request_income_proof":
            has_income = any(str(d.get("kind", d)).lower() in INCOME_DOC_KINDS for d in disclosed)
            if not has_income:
                response = {"outcome": "facts_requested", "requested_facts": ["income_evidence_document"], "negotiation_ref": provider_ref, "reason": "We need proof of income before improving pricing."}
            else:
                revised = self._revised_offer(
                    lender_id,
                    target,
                    lender_credits_minor=int(target.get("lender_credits_minor", 0)) + 50000,
                    rate_lock={"locked": False, "lock_period_days": 45, "note": "Rate lock available for 45 days at no cost."},
                )
                response = {"outcome": "counteroffer", "offer": revised, "negotiation_ref": provider_ref, "reason": "With verified income we can add a $500 credit and confirm lock terms."}
        else:
            raise ProviderDeclined(f"unsupported behaviour {behaviour}")

        response["environment"] = ENV
        response["request_ref"] = request_ref
        self.negotiations[request_ref] = {"lender_id": lender_id, "message": message, "response": response, "at": self.clock.now().isoformat()}
        self._log("send_negotiation", lender_id, request_ref, response["outcome"])
        return response

    def get_negotiation(self, request_ref: str) -> Optional[Dict[str, Any]]:
        record = self.negotiations.get(request_ref)
        return record["response"] if record else None

    # --------------------------------------------------------------- application
    def _final_terms_for(self, lender_id: str, app: Dict[str, Any]) -> Dict[str, Any]:
        accepted = app["packet"]["offer_terms"]
        final = copy.deepcopy(accepted)
        final["status"] = "final_offer"
        behaviour = self.lenders[lender_id]["application_behaviour"]
        changed_costs = app.pop("_changed_costs_fault", None)
        if behaviour == "accept_with_increased_costs" or changed_costs:
            extra = int(changed_costs["params"].get("amount_minor", 60000)) if changed_costs else 60000
            items = list(final.get("cost_items", []))
            items.append({"category": "A", "label": "Rate lock extension fee", "amount_minor": extra, "source": {"page": 2, "section": "A"}})
            final["cost_items"] = items
            if accepted.get("financed_costs_minor"):
                final["financed_costs_minor"] = int(accepted["financed_costs_minor"]) + extra
                final["principal_minor"] = int(accepted["principal_minor"]) + extra
        else:
            # Non-material prepaid interest change only.
            items = []
            for item in final.get("cost_items", []):
                item = dict(item)
                if item.get("category") == "F" and "interest" in item.get("label", "").lower():
                    item["amount_minor"] = max(0, int(item["amount_minor"]) - 5000)
                items.append(item)
            final["cost_items"] = items
        final["monthly_pi_minor"] = monthly_payment_minor(int(final["principal_minor"]), normalize_rate(final["note_rate_decimal"]), int(final["term_months"]))
        final["conditions"] = []
        final["final_terms_id"] = self._ref("final")
        final["issued_at"] = self.clock.now().isoformat()
        return final

    def submit_application(self, packet: Dict[str, Any], request_ref: str) -> Dict[str, Any]:
        lender_id = packet["lender_id"]
        lender = self.lenders.get(lender_id)
        if lender is None:
            raise ProviderDeclined(f"unknown lender {lender_id}")
        if request_ref in self.applications:
            self._log("submit_application", lender_id, request_ref, "duplicate_returned")
            return self._application_view(self.applications[request_ref])

        if self._take_fault(FAULT_MALFORMED, lender_id, "submit_application"):
            self._log("submit_application", lender_id, request_ref, "malformed")
            raise ProviderMalformedResponse("unexpected content-type text/plain")
        if self._take_fault(FAULT_DECLINED, lender_id, "submit_application") or lender["application_behaviour"] == "not_offered":
            self._log("submit_application", lender_id, request_ref, "declined")
            raise ProviderDeclined("Application declined: product not offered through this channel.")

        offer_terms = packet["offer_terms"]
        expires_at = offer_terms.get("expires_at")
        if expires_at and self.clock.now().isoformat() >= expires_at:
            self._log("submit_application", lender_id, request_ref, "declined_expired")
            raise ProviderDeclined("Application declined: the referenced quote has expired.")

        behaviour = lender["application_behaviour"]
        documents = packet.get("documents", [])
        has_income = any(str(d.get("kind", "")).lower() in INCOME_DOC_KINDS for d in documents)
        conditions: List[Dict[str, Any]] = []
        if behaviour in ("accept_with_income_condition", "request_income_proof") and not has_income:
            conditions.append({"id": "income_doc_missing", "description": "Provide proof of income (recent pay stub, W-2 or tax return).", "status": "open"})
        if behaviour == "request_income_proof":
            conditions.append({"id": "employment_verification", "description": "Employer verification of current employment.", "status": "open"})

        app_ref = self._ref("app")
        app = {
            "application_ref": app_ref,
            "request_ref": request_ref,
            "lender_id": lender_id,
            "packet": packet,
            "status": "submitted",
            "conditions": conditions,
            "submitted_at": self.clock.now(),
            "conditions_cleared_at": None if conditions else self.clock.now(),
            "final_terms": None,
            "closing": None,
        }
        changed = self._take_fault(FAULT_CHANGED_CLOSING_COSTS, lender_id, "submit_application")
        if changed:
            app["_changed_costs_fault"] = changed
        self.applications[request_ref] = app
        self.applications_by_ref[app_ref] = app

        if conditions:
            delay = timedelta(0)
            fault = self._take_fault(FAULT_DELAYED_CALLBACK, lender_id, "submit_application")
            if fault:
                delay = timedelta(hours=int(fault["params"].get("hours", 6)))
            self._queue_callback(
                lender_id,
                "loan.application.conditions_requested",
                {"application_ref": app_ref, "request_ref": request_ref, "condition_ids": [c["id"] for c in conditions]},
                delay=delay,
            )

        if self._take_fault(FAULT_TIMEOUT_AFTER_ACCEPT, lender_id, "submit_application"):
            # The lender accepted and stored the application but the response never arrived.
            self._log("submit_application", lender_id, request_ref, "timeout_after_accept")
            raise ProviderTimeout("request timed out after 30s")

        self._log("submit_application", lender_id, request_ref, "accepted")
        return self._application_view(app)

    def _advance(self, app: Dict[str, Any]) -> None:
        """Progress the application according to the clock."""
        if app["status"] in ("declined", "withdrawn", "mock_closed"):
            return
        open_conditions = [c for c in app["conditions"] if c["status"] == "open"]
        if open_conditions:
            app["status"] = "conditions_outstanding"
            return
        if app["conditions_cleared_at"] is None:
            app["conditions_cleared_at"] = self.clock.now()
        if app["final_terms"] is None and self.clock.now() >= app["conditions_cleared_at"] + self.processing_delay:
            app["final_terms"] = self._final_terms_for(app["lender_id"], app)
            app["status"] = "approved_offer"
            self._queue_callback(
                app["lender_id"],
                "loan.application.approved_offer",
                {"application_ref": app["application_ref"], "request_ref": app["request_ref"], "final_terms_id": app["final_terms"]["final_terms_id"]},
            )
        elif app["final_terms"] is None:
            app["status"] = "submitted"

    def _application_view(self, app: Dict[str, Any]) -> Dict[str, Any]:
        self._advance(app)
        return {
            "application_ref": app["application_ref"],
            "request_ref": app["request_ref"],
            "lender_id": app["lender_id"],
            "status": app["status"],
            "conditions": copy.deepcopy(app["conditions"]),
            "final_terms": copy.deepcopy(app["final_terms"]),
            "closing": copy.deepcopy(app["closing"]),
            "environment": ENV,
            "is_funded": bool(app["closing"]),
        }

    def get_application(self, request_ref: str) -> Optional[Dict[str, Any]]:
        app = self.applications.get(request_ref)
        if app is None:
            by_ref = self.applications_by_ref
            app = by_ref.get(request_ref)
        if app is None:
            return None
        self._log("get_application", app["lender_id"], request_ref, app["status"])
        return self._application_view(app)

    def provide_documents(self, application_ref: str, documents: List[Dict[str, Any]], request_ref: str) -> Dict[str, Any]:
        app = self.applications_by_ref.get(application_ref)
        if app is None:
            raise ProviderDeclined(f"unknown application {application_ref}")
        kinds = {str(d.get("kind", "")).lower() for d in documents}
        for cond in app["conditions"]:
            if cond["status"] != "open":
                continue
            if cond["id"] == "income_doc_missing" and kinds & INCOME_DOC_KINDS:
                cond["status"] = "satisfied"
                cond["satisfied_at"] = self.clock.now().isoformat()
            if cond["id"] == "employment_verification" and "employment_verification" in kinds:
                cond["status"] = "satisfied"
                cond["satisfied_at"] = self.clock.now().isoformat()
        if not any(c["status"] == "open" for c in app["conditions"]):
            app["conditions_cleared_at"] = self.clock.now()
        self._log("provide_documents", app["lender_id"], request_ref, "accepted")
        return self._application_view(app)

    def request_closing(self, application_ref: str, request_ref: str) -> Dict[str, Any]:
        app = self.applications_by_ref.get(application_ref)
        if app is None:
            raise ProviderDeclined(f"unknown application {application_ref}")
        self._advance(app)
        if app["status"] != "approved_offer":
            raise ProviderDeclined(f"application is {app['status']}; closing requires approved_offer")
        app["closing"] = {
            "closing_record_id": self._ref("closing"),
            "closed_at": self.clock.now().isoformat(),
            "payoff_record_id": self._ref("payoff"),
            "final_terms_id": app["final_terms"]["final_terms_id"],
            "note": "Mock closing. No funds moved; the existing loan is not repaid by this record alone.",
        }
        app["status"] = "mock_closed"
        self._queue_callback(app["lender_id"], "loan.application.mock_closed", {"application_ref": application_ref, "request_ref": app["request_ref"], "closing_record_id": app["closing"]["closing_record_id"]})
        self._log("request_closing", app["lender_id"], request_ref, "mock_closed")
        return self._application_view(app)

    def withdraw(self, application_ref: str, request_ref: str) -> Dict[str, Any]:
        app = self.applications_by_ref.get(application_ref)
        if app is None:
            raise ProviderDeclined(f"unknown application {application_ref}")
        app["status"] = "withdrawn"
        app["withdrawal"] = {"confirmed_at": self.clock.now().isoformat(), "confirmation_id": self._ref("withdraw")}
        self._log("withdraw", app["lender_id"], request_ref, "withdrawn")
        view = self._application_view(app)
        view["withdrawal"] = app["withdrawal"]
        return view


class MockLenderAdapter:
    """``LenderAdapter`` implementation over the in-process simulator."""

    capabilities = AdapterCapabilities(
        environment=ENV,
        request_offer=False,
        send_negotiation=True,
        submit_application=True,
        provide_documents=True,
        get_application_by_request_ref=True,
        get_negotiation_by_request_ref=True,
        request_closing=True,
        webhooks=True,
        uncertain_outcome_requires_manual_review=False,
    )

    def __init__(self, network: MockLenderNetwork):
        self.network = network

    def _wrap(self, data: Optional[Dict[str, Any]], request_ref: str, ref_key: Optional[str] = None) -> ProviderResult:
        if data is None:
            return self.network._result({}, None, request_ref, ok=False, error="not_found")
        ref = data.get(ref_key) if ref_key else None
        return self.network._result(data, ref, request_ref)

    async def request_offer(self, request: Dict[str, Any], request_ref: str) -> ProviderResult:
        raise ProviderDeclined("request_offer is not supported by the mock network; supply Loan Estimates as documents")

    async def send_negotiation(self, message: Dict[str, Any], request_ref: str) -> ProviderResult:
        return self._wrap(self.network.send_negotiation(message, request_ref), request_ref, "negotiation_ref")

    async def get_negotiation(self, request_ref: str) -> ProviderResult:
        return self._wrap(self.network.get_negotiation(request_ref), request_ref, "negotiation_ref")

    async def submit_application(self, packet: Dict[str, Any], request_ref: str) -> ProviderResult:
        return self._wrap(self.network.submit_application(packet, request_ref), request_ref, "application_ref")

    async def get_application(self, request_ref: str) -> ProviderResult:
        return self._wrap(self.network.get_application(request_ref), request_ref, "application_ref")

    async def provide_documents(self, application_ref: str, documents: Dict[str, Any], request_ref: str) -> ProviderResult:
        docs = documents.get("documents", []) if isinstance(documents, dict) else list(documents)
        return self._wrap(self.network.provide_documents(application_ref, docs, request_ref), request_ref, "application_ref")

    async def request_closing(self, application_ref: str, request_ref: str) -> ProviderResult:
        return self._wrap(self.network.request_closing(application_ref, request_ref), request_ref, "application_ref")

    async def withdraw(self, application_ref: str, request_ref: str) -> ProviderResult:
        return self._wrap(self.network.withdraw(application_ref, request_ref), request_ref, "application_ref")


def load_mock_network(clock: Clock, fixtures_dir) -> MockLenderNetwork:
    from pathlib import Path

    data = json.loads((Path(fixtures_dir) / "lenders.json").read_text())
    return MockLenderNetwork(data["lenders"], clock)
