"""Mock insurer simulator.

Runs without credentials, uses the fixture clock, persists its own state, and delivers signed events asynchronously
(like webhooks). Supports three decision paths (approved / evidence requested / partial approval), duplicate and
out-of-order events, delayed callbacks, malformed responses, declines and a timeout after acceptance.

The mock insurer applies an *internal 24-hour purchase guideline* that is not in the fixture policy. That produces a
realistic, appealable partial rejection (RC_LATE_PURCHASE).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from ..clock import Clock, iso, parse_iso
from ..ids import canonical_json, new_id
from ..persistence.db import Database
from ..persistence.models import MockInsurerClaim, MockRequestRef
from .base import AdapterCapabilities, ProviderDeclined, ProviderMalformedResponse, ProviderTimeout, provider_result

PROVIDER_NAME = "mock_insurer"
EXCLUDED_BY_MOCK = {"electronics", "jewelry", "alcohol", "luxury"}
MOCK_LATE_PURCHASE_HOURS = 24  # insurer-side guideline, deliberately NOT in the policy fixture
EVIDENCE_REQUEST_DUE_DAYS = 14


def sign_payload(secret: str, payload: Dict[str, Any]) -> str:
    digest = hmac.new(secret.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class MockInsurerAdapter:
    def __init__(self, db: Database, clock: Clock, webhook_secret: str):
        self.db = db
        self.clock = clock
        self.secret = webhook_secret
        self.capabilities = AdapterCapabilities(
            name=PROVIDER_NAME,
            environment="mock",
            find_submission=True,
            uncertain_requires_manual_review=False,
            protocol="in-process",
            notes="Deterministic simulator. Decision paths chosen from packet content unless packet.mock.scenario overrides.",
        )
        self.faults: List[str] = []  # one-shot fault queue: timeout_after_accept | timeout_before_accept | malformed | declined
        self.chaos: Dict[str, bool] = {"duplicate_decision": False, "out_of_order": False}
        self.callback_delay: Optional[timedelta] = None

    # ------------------------------------------------------------------ controls
    def inject_fault(self, fault: str) -> None:
        self.faults.append(fault)

    def set_chaos(self, **flags: bool) -> None:
        self.chaos.update(flags)

    def set_callback_delay(self, **kwargs) -> None:
        self.callback_delay = timedelta(**kwargs) if kwargs else None

    def _pop_fault(self) -> Optional[str]:
        return self.faults.pop(0) if self.faults else None

    # ------------------------------------------------------------------ adapter API
    async def submit_claim(self, packet: dict, request_ref: str) -> dict:
        now = self.clock.now_iso()
        with self.db.session() as s:
            existing = s.get(MockRequestRef, request_ref)
            if existing:
                return provider_result(dict(existing.result_json, idempotent_replay=True), environment="mock", authority="simulated", retrieved_at=now, source=PROVIDER_NAME)
        fault = self._pop_fault()
        if fault == "timeout_before_accept":
            raise ProviderTimeout("mock insurer did not respond (nothing recorded)")
        if fault == "declined":
            raise ProviderDeclined("mock insurer declined the submission: policy not found in their system (fixture fault)")
        if fault == "malformed":
            raise ProviderMalformedResponse("<html>502 Bad Gateway</html>")

        claim_ref = "insurer_mock_" + hashlib.sha256(request_ref.encode()).hexdigest()[:8]
        scenario = (packet.get("mock") or {}).get("scenario") or "content"
        with self.db.session() as s:
            row = MockInsurerClaim(
                claim_ref=claim_ref,
                request_ref=request_ref,
                packet_json=packet,
                scenario=scenario,
                status="received",
                submissions_json={"history": [{"request_ref": request_ref, "kind": "initial", "at": now}], "pending_events": [], "evidence_doc_types": self._doc_types(packet)},
                decision_json=None,
                open_request_json=None,
                event_sequence=0,
                created_at=now,
                updated_at=now,
            )
            s.add(row)
            s.flush()
            result = {"status": "accepted", "claim_ref": claim_ref, "submission_ref": f"{claim_ref}/s1", "received_at": now}
            s.add(MockRequestRef(request_ref=request_ref, claim_ref=claim_ref, operation="submit_claim", result_json=result, created_at=now))
            self._queue_event(row, "claim.received", {"submission_ref": result["submission_ref"]})
            self._adjudicate(row, packet, scenario)
        if fault == "timeout_after_accept":
            raise ProviderTimeout("mock insurer accepted the claim but the response was lost")
        return provider_result(result, environment="mock", authority="simulated", retrieved_at=now, source=PROVIDER_NAME)

    async def add_evidence(self, claim_ref: str, packet: dict, request_ref: str) -> dict:
        now = self.clock.now_iso()
        with self.db.session() as s:
            existing = s.get(MockRequestRef, request_ref)
            if existing:
                return provider_result(dict(existing.result_json, idempotent_replay=True), environment="mock", authority="simulated", retrieved_at=now, source=PROVIDER_NAME)
            row = s.get(MockInsurerClaim, claim_ref)
            if not row:
                raise ProviderDeclined(f"unknown claim reference {claim_ref}")
        fault = self._pop_fault()
        if fault == "timeout_before_accept":
            raise ProviderTimeout("mock insurer did not respond (nothing recorded)")
        if fault == "declined":
            raise ProviderDeclined("mock insurer declined the supplemental submission (fixture fault)")
        if fault == "malformed":
            raise ProviderMalformedResponse("{not json")
        with self.db.session() as s:
            row = s.get(MockInsurerClaim, claim_ref)
            subs = dict(row.submissions_json)
            history = list(subs.get("history", []))
            seq = len(history) + 1
            kind = packet.get("packet_type", "supplemental")
            history.append({"request_ref": request_ref, "kind": kind, "at": now})
            subs["history"] = history
            doc_types = set(subs.get("evidence_doc_types", []))
            doc_types.update(self._doc_types(packet))
            subs["evidence_doc_types"] = sorted(doc_types)
            row.submissions_json = subs
            row.updated_at = now
            result = {"status": "accepted", "claim_ref": claim_ref, "submission_ref": f"{claim_ref}/s{seq}", "received_at": now}
            s.add(MockRequestRef(request_ref=request_ref, claim_ref=claim_ref, operation="add_evidence", result_json=result, created_at=now))
            if kind == "appeal":
                self._review_appeal(row, packet)
            else:
                responds = packet.get("responds_to_request") or {}
                open_req = row.open_request_json
                if open_req and responds.get("provider_request_id") == open_req["request_id"] and open_req["document_type"] in doc_types:
                    row.open_request_json = None
                    self._queue_event(row, "claim.request_satisfied", {"request_id": open_req["request_id"]})
                merged = dict(row.packet_json)
                merged["evidence_manifest"] = list(merged.get("evidence_manifest", [])) + list(packet.get("evidence_manifest", []))
                if packet.get("claimed_expenses"):
                    merged["claimed_expenses"] = list(merged.get("claimed_expenses", [])) + list(packet["claimed_expenses"])
                row.packet_json = merged
                self._adjudicate(row, merged, row.scenario if row.scenario != "evidence_requested" else "content")
        if fault == "timeout_after_accept":
            raise ProviderTimeout("mock insurer accepted the evidence but the response was lost")
        return provider_result(result, environment="mock", authority="simulated", retrieved_at=now, source=PROVIDER_NAME)

    async def get_claim(self, claim_ref: str) -> dict:
        now = self.clock.now_iso()
        with self.db.session() as s:
            row = s.get(MockInsurerClaim, claim_ref)
            if not row:
                return provider_result({"found": False, "claim_ref": claim_ref}, environment="mock", authority="simulated", retrieved_at=now, source=PROVIDER_NAME)
            return provider_result(
                {"found": True, "claim_ref": claim_ref, "status": row.status, "decision": row.decision_json, "open_request": row.open_request_json, "submissions": row.submissions_json.get("history", [])},
                environment="mock",
                authority="simulated",
                retrieved_at=now,
                source=PROVIDER_NAME,
            )

    async def find_submission(self, request_ref: str) -> dict:
        now = self.clock.now_iso()
        with self.db.session() as s:
            ref = s.get(MockRequestRef, request_ref)
            if not ref:
                return provider_result({"found": False, "request_ref": request_ref}, environment="mock", authority="simulated", retrieved_at=now, source=PROVIDER_NAME)
            return provider_result(dict(ref.result_json, found=True, operation=ref.operation), environment="mock", authority="simulated", retrieved_at=now, source=PROVIDER_NAME)

    # ------------------------------------------------------------------ event delivery (webhook-like)
    def pending_events(self) -> List[Dict[str, Any]]:
        """Signed events that are due for delivery, in the order the simulator chose to send them."""
        now = self.clock.now()
        out: List[Dict[str, Any]] = []
        with self.db.session() as s:
            rows = s.scalars(select(MockInsurerClaim)).all()
            for row in rows:
                for ev in row.submissions_json.get("pending_events", []):
                    if parse_iso(ev["available_at"]) <= now:
                        out.append({"payload": ev["payload"], "signature": sign_payload(self.secret, ev["payload"]), "claim_ref": row.claim_ref, "local_id": ev["local_id"]})
        return out

    def mark_delivered(self, claim_ref: str, local_id: str) -> None:
        with self.db.session() as s:
            row = s.get(MockInsurerClaim, claim_ref)
            if not row:
                return
            subs = dict(row.submissions_json)
            subs["pending_events"] = [ev for ev in subs.get("pending_events", []) if ev["local_id"] != local_id]
            row.submissions_json = subs

    def snapshot(self) -> List[Dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(MockInsurerClaim)).all()
            return [
                {"claim_ref": r.claim_ref, "status": r.status, "scenario": r.scenario, "decision": r.decision_json, "open_request": r.open_request_json, "pending_events": len(r.submissions_json.get("pending_events", [])), "submissions": r.submissions_json.get("history", [])}
                for r in rows
            ]

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _doc_types(packet: dict) -> List[str]:
        return sorted({d["doc_type"] for d in packet.get("evidence_manifest", [])})

    def _queue_event(self, row: MockInsurerClaim, event_type: str, data: Dict[str, Any], *, sequence: Optional[int] = None, available_in: Optional[timedelta] = None) -> Dict[str, Any]:
        if sequence is None:
            row.event_sequence = int(row.event_sequence or 0) + 1
            sequence = row.event_sequence
        now = self.clock.now()
        delay = available_in if available_in is not None else (self.callback_delay or timedelta(0))
        payload = {
            "id": new_id("mockevt"),
            "type": event_type,
            "claim_reference": row.claim_ref,
            "sequence": sequence,
            "occurred_at": iso(now),
            "environment": "mock",
            "data": data,
        }
        subs = dict(row.submissions_json)
        pending = list(subs.get("pending_events", []))
        pending.append({"local_id": new_id("pend"), "available_at": iso(now + delay), "payload": payload})
        subs["pending_events"] = pending
        row.submissions_json = subs
        return payload

    def _adjudicate(self, row: MockInsurerClaim, packet: dict, scenario: str) -> None:
        now = self.clock.now()
        doc_types = set(row.submissions_json.get("evidence_doc_types", [])) | set(self._doc_types(packet))
        needs_arrival = "baggage_arrival_confirmation" not in doc_types
        if scenario == "evidence_requested" or (scenario == "content" and needs_arrival):
            request = {
                "request_id": f"evidence_req_{row.event_sequence + 1}",
                "document_type": "baggage_arrival_confirmation",
                "message": "Please provide the carrier's baggage delivery confirmation showing when the bag was returned.",
                "due_at": iso(now + timedelta(days=EVIDENCE_REQUEST_DUE_DAYS)),
                "deadline_source": "mock insurer evidence request (fixture: 14 days from request)",
            }
            row.open_request_json = request
            row.status = "evidence_requested"
            self._queue_event(row, "claim.under_review", {})
            self._queue_event(row, "claim.evidence_requested", request)
            return

        decision = self._decide(packet, scenario, existing=row.decision_json)
        row.decision_json = decision
        row.status = decision["outcome"]
        under_review_seq = row.event_sequence + 1
        decision_seq = row.event_sequence + 2
        row.event_sequence = decision_seq
        if self.chaos.get("out_of_order"):
            self._queue_event(row, "claim.decided", decision, sequence=decision_seq)
            self._queue_event(row, "claim.under_review", {}, sequence=under_review_seq)
        else:
            self._queue_event(row, "claim.under_review", {}, sequence=under_review_seq)
            self._queue_event(row, "claim.decided", decision, sequence=decision_seq)
        if self.chaos.get("duplicate_decision"):
            # exact duplicate delivery: same provider event id, delivered a second time
            subs = dict(row.submissions_json)
            pending = list(subs["pending_events"])
            first_decision = [p for p in pending if p["payload"]["type"] == "claim.decided"][-1]
            pending.append({"local_id": new_id("pend"), "available_at": first_decision["available_at"], "payload": json.loads(json.dumps(first_decision["payload"]))})
            subs["pending_events"] = pending
            row.submissions_json = subs

    def _decide(self, packet: dict, scenario: str, existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        currency = packet.get("currency", "USD")
        cap = int(packet.get("policy_cap_minor", 0) or 0)
        delay_start = parse_iso(packet["loss"]["delay_start_at"]) if packet.get("loss", {}).get("delay_start_at") else None
        items: List[Dict[str, Any]] = []
        accepted_total = 0
        rejected_total = 0
        for e in packet.get("claimed_expenses", []):
            amount = int(e["amount_minor"])
            item = {"expense_id": e["expense_id"], "receipt_id": e["receipt_id"], "amount_minor": amount, "accepted_minor": amount, "rejected_minor": 0, "reason_code": "RC_ACCEPTED", "reason_text": "Accepted."}
            if scenario == "denied":
                item.update(accepted_minor=0, rejected_minor=amount, reason_code="RC_DELAY_THRESHOLD_NOT_MET", reason_text="Our records indicate the delay did not exceed the policy threshold.")
            elif e.get("category") in EXCLUDED_BY_MOCK:
                item.update(accepted_minor=0, rejected_minor=amount, reason_code="RC_EXCLUDED_CATEGORY", reason_text=f"{e.get('category')} items are excluded.")
            elif scenario in ("content", "partial") and delay_start and e.get("purchased_at") and parse_iso(e["purchased_at"]) > delay_start + timedelta(hours=MOCK_LATE_PURCHASE_HOURS):
                item.update(accepted_minor=0, rejected_minor=amount, reason_code="RC_LATE_PURCHASE", reason_text="Purchase made more than 24 hours after the delay began (adjuster guideline).")
            items.append(item)
        if scenario == "partial" and items and not any(i["rejected_minor"] for i in items):
            # force a partial: the adjuster misapplies the 24-hour guideline to the largest item
            largest = max(items, key=lambda i: i["amount_minor"])
            largest.update(accepted_minor=0, rejected_minor=largest["amount_minor"], reason_code="RC_LATE_PURCHASE", reason_text="Purchase made more than 24 hours after the delay began (adjuster guideline).")
        accepted_total = sum(i["accepted_minor"] for i in items)
        rejected_total = sum(i["rejected_minor"] for i in items)
        item_rejections = rejected_total
        if cap and accepted_total > cap:
            over = accepted_total - cap
            items.append({"expense_id": None, "receipt_id": None, "amount_minor": over, "accepted_minor": 0, "rejected_minor": over, "reason_code": "RC_CAP_APPLIED", "reason_text": f"Aggregate benefit limited to {cap / 100:.2f} {currency}."})
            accepted_total = cap
            rejected_total += over
        if accepted_total == 0:
            outcome = "denied"
        elif item_rejections == 0:
            outcome = "approved"  # paying up to the policy limit is a full approval
        else:
            outcome = "partially_approved"
        version = (existing or {}).get("decision_version", 0) + 1
        return {
            "decision_version": version,
            "decision_ref": f"dec_mock_{version}",
            "outcome": outcome,
            "accepted_minor": accepted_total,
            "rejected_minor": rejected_total,
            "currency": currency,
            "reason_items": items,
            "decided_at": self.clock.now_iso(),
            "appeal_channel": "written review request",
        }

    def _review_appeal(self, row: MockInsurerClaim, packet: dict) -> None:
        """The mock Claims Review Unit reverses RC_LATE_PURCHASE rejections when the challenge cites the purchase-window clause."""
        existing = row.decision_json or {"reason_items": [], "decision_version": 0, "currency": "USD"}
        challenged = {c.get("expense_id"): c for c in packet.get("challenges", [])}
        items = []
        accepted = 0
        rejected = 0
        cap = int(row.packet_json.get("policy_cap_minor", 0) or 0)
        reversed_any = False
        for item in existing.get("reason_items", []):
            new_item = dict(item)
            ch = challenged.get(item.get("expense_id"))
            if ch and item.get("reason_code") == "RC_LATE_PURCHASE" and ch.get("clause_id"):
                new_item.update(accepted_minor=item["amount_minor"], rejected_minor=0, reason_code="RC_ACCEPTED", reason_text="Accepted on review: purchase within policy window.")
                reversed_any = True
            elif item.get("reason_code") == "RC_CAP_APPLIED":
                continue
            elif ch:
                new_item["reason_text"] = item.get("reason_text", "") + " Upheld on review."
            accepted += new_item["accepted_minor"]
            rejected += new_item["rejected_minor"]
            items.append(new_item)
        item_rejections = rejected
        if cap and accepted > cap:
            over = accepted - cap
            items.append({"expense_id": None, "receipt_id": None, "amount_minor": over, "accepted_minor": 0, "rejected_minor": over, "reason_code": "RC_CAP_APPLIED", "reason_text": f"Aggregate benefit limited to {cap / 100:.2f}."})
            accepted = cap
            rejected += over
        outcome = "denied" if accepted == 0 else ("approved" if item_rejections == 0 else "partially_approved")
        decision = {
            "decision_version": int(existing.get("decision_version", 0)) + 1,
            "decision_ref": f"dec_mock_{int(existing.get('decision_version', 0)) + 1}",
            "outcome": outcome,
            "accepted_minor": accepted,
            "rejected_minor": rejected,
            "currency": existing.get("currency", "USD"),
            "reason_items": items,
            "decided_at": self.clock.now_iso(),
            "appeal_review": "reversed" if reversed_any else "upheld",
            "appeal_channel": "written review request",
        }
        row.decision_json = decision
        row.status = outcome
        self._queue_event(row, "claim.under_review", {"stage": "appeal_review"})
        self._queue_event(row, "claim.decided", decision)
