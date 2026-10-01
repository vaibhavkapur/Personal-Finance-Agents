"""Background executor: consumes an approval for its bound action, submits
once, reconciles uncertain outcomes and verifies the result (plan §4, §8, §14).

Invariants:
* authority is re-verified immediately before the side effect;
* a pending action is persisted (``submitting``) *before* the provider call;
* provider calls run outside database transactions;
* a timeout or malformed response yields ``outcome_unknown`` and a lookup by
  the original request reference; the executor never creates a second
  provider action for the same approval;
* completion requires reconciled evidence; a mismatch holds the case open.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app import clock
from app.adapters.base import (
    ProviderAccessRevoked,
    ProviderDeclined,
    ProviderError,
    ProviderMalformedResponse,
    ProviderNotFound,
    ProviderTimeout,
)
from app.adapters.registry import adapter_for
from app.config import settings
from app.domain.offers import material_terms, normalize_offer
from app.domain.reconciliation import reconcile_renewal, reconcile_transfer
from app.persistence import outbox
from app.persistence.db import session_scope
from app.persistence.models import Action, Approval, BankAccount, BankInstruction, Case, CashPlan, DepositOffer
from app.workflows import states
from app.workflows.case_service import enqueue_job
from app.workflows.refresh import offer_to_raw, refresh_offers, refresh_snapshots

ACTOR = "executor"
VERIFY_GRACE_DAYS = 3
RECONCILE_MAX_LOOKUPS = 3


class RetryLater(Exception):
    def __init__(self, run_at: datetime, reason: str):
        super().__init__(reason)
        self.run_at = run_at
        self.reason = reason


def _load(session, action_id: str) -> tuple[Action, Case, BankInstruction, Approval | None]:
    action = session.get(Action, action_id)
    if action is None:
        raise ValueError(f"action {action_id} not found")
    case = session.get(Case, action.case_id)
    instruction = session.scalars(select(BankInstruction).where(BankInstruction.action_id == action.id)).first()
    approval = session.get(Approval, action.approval_id) if action.approval_id else None
    return action, case, instruction, approval


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 5), tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# execute_action
# --------------------------------------------------------------------------- #


async def execute_action(action_id: str) -> str:
    # 1. Load and verify authority.
    with session_scope() as session:
        action, case, instruction, approval = _load(session, action_id)
        now = clock.now(session)
        if action.status in ("submitted", "outcome_unknown", "effective", "verified", "rejected", "superseded", "cancelled"):
            return f"noop: action already {action.status}"
        if action.status == "submitting":
            # Crash recovery: the provider call may or may not have happened.
            mode = "recover"
        elif action.status == "approved":
            mode = "fresh"
        else:
            return f"noop: action status {action.status}"
        if case.state not in (states.APPROVED, states.MANUAL_REVIEW):
            return f"noop: case state {case.state}"
        if case.current_action_id != action.id:
            return "noop: action is no longer the case's current action"
        if approval is None or approval.action_hash != action.payload_hash or approval.revoked_at is not None:
            _hold(session, case, action, "approval missing, revoked or bound to a different payload")
            return "manual_review: invalid approval"
        if mode == "fresh" and approval.consumed_at is not None:
            _hold(session, case, action, "approval already consumed")
            return "manual_review: approval already consumed"
        if mode == "fresh" and approval.expires_at < now:
            approval.revoked_at = now
            approval.revocation_reason = "approval expired before execution"
            action.status = "superseded"
            action.updated_at = now
            instruction.status = "superseded"
            case.current_action_id = None
            states.transition(session, case, states.NEEDS_REQUOTE, ACTOR, reason="approval expired before execution")
            outbox.emit(session, "banking_case.needs_requote", case.id, {"action_id": action.id, "reason": "approval_expired"})
            return "needs_requote: approval expired"
        payload = dict(action.payload_json)
        request_ref = action.request_ref
        provider_id = payload["provider_id"]
        source_provider = session.get(BankAccount, payload["source_account_id"]).provider_id
        customer_id = case.customer_id
        deposit_id = case.deposit_id
        offer_id = payload["offer_id"]
        case_id = case.id

    submit_adapter = adapter_for(source_provider, case_id)

    if mode == "recover":
        # Before retrying a write, query by the original request reference.
        try:
            found = await submit_adapter.find_instruction(request_ref)
        except ProviderNotFound:
            found = None
        except ProviderError as exc:
            with session_scope() as session:
                action, case, instruction, approval = _load(session, action_id)
                _hold(session, case, action, f"could not resolve uncertain write during recovery: {exc}")
            return "manual_review: lookup failed during recovery"
        if found is not None:
            return await _adopt_provider_record(action_id, found, source="recovery_lookup")
        # Provider never saw it: safe to submit with the same reference.
    else:
        # 2. Refresh balances and offer validity (outside any transaction).
        snap = await refresh_snapshots(customer_id, case_id)
        await refresh_offers(deposit_id, case_id)
        with session_scope() as session:
            action, case, instruction, approval = _load(session, action_id)
            now = clock.now(session)
            today = now.date()
            source = session.get(BankAccount, payload["source_account_id"])
            if source.id in snap["revoked"] or source.access_revoked:
                _hold(session, case, action, "account access revoked; execution blocked")
                return "manual_review: access revoked"
            if (now - source.snapshot_at) > timedelta(minutes=settings.snapshot_max_age_minutes):
                _hold(session, case, action, "balance snapshot is stale at submission")
                return "manual_review: stale snapshot"
            offer_row = session.scalars(
                select(DepositOffer).where(DepositOffer.id == offer_id, DepositOffer.superseded.is_(False))
            ).first()
            approved_terms = {
                "offer_id": offer_id,
                "product_version": payload["product_version"],
                "apy_decimal": payload["apy_decimal"],
                "term_days": payload["term_days"],
                "fees_minor": payload["fees_minor"],
                "destination_account_id": payload["destination_account_id"] if payload["instruction_type"] == "same_owner_transfer" else (offer_row.destination_account_id if offer_row else None),
            }
            current_terms = material_terms(normalize_offer(offer_to_raw(offer_row))) if offer_row else None
            if current_terms is not None and payload["instruction_type"] != "same_owner_transfer":
                current_terms["destination_account_id"] = offer_row.destination_account_id
            changed = current_terms is None or current_terms != approved_terms
            expired = offer_row is not None and offer_row.valid_until < today
            if changed or expired:
                reason = "offer expired before execution" if expired and not changed else "material offer terms changed before execution"
                approval.revoked_at = now
                approval.revocation_reason = reason
                action.status = "superseded"
                action.updated_at = now
                instruction.status = "superseded"
                case.current_action_id = None
                states.record_event(session, case, "approval.invalidated", ACTOR, {"action_id": action.id, "approval_id": approval.id, "reason": reason, "approved_terms": approved_terms, "current_terms": current_terms})
                states.transition(session, case, states.NEEDS_REQUOTE, ACTOR, reason=reason, data={"approved_terms": approved_terms, "current_terms": current_terms})
                outbox.emit(session, "banking_case.needs_requote", case.id, {"action_id": action.id, "reason": reason, "current_terms": current_terms})
                return f"needs_requote: {reason}"
            # Available funds check against the refreshed provider snapshot.
            deposit_snapshot = snap["snapshots"].get(source.id, {})
            open_deposits = [d for d in deposit_snapshot.get("deposits", []) if d.get("status") == "open"]
            principal = open_deposits[0]["principal_minor"] if open_deposits else source.available_minor
            if payload["amount_minor"] > principal:
                _hold(session, case, action, f"insufficient available balance at submission: {principal} < {payload['amount_minor']}")
                return "manual_review: insufficient balance"
            # 3. Persist the pending action, consume the approval, then call the provider.
            approval.consumed_at = now
            action.status = "submitting"
            action.updated_at = now
            instruction.status = "submitting"
            instruction.updated_at = now
            states.record_event(session, case, "action.submitting", ACTOR, {"action_id": action.id, "request_ref": request_ref, "approval_id": approval.id})

    # 4. Provider call, outside the transaction.
    try:
        result = await submit_adapter.submit_instruction(payload, request_ref)
        if not isinstance(result, dict) or not result.get("provider_reference") or result.get("status") not in ("accepted", "effective"):
            raise ProviderMalformedResponse("missing provider reference or status")
    except ProviderDeclined as exc:
        with session_scope() as session:
            action, case, instruction, approval = _load(session, action_id)
            now = clock.now(session)
            action.status = "rejected"
            action.last_error = exc.reason
            action.updated_at = now
            instruction.status = "rejected"
            instruction.updated_at = now
            states.transition(session, case, states.REJECTED, ACTOR, reason=f"provider declined: {exc.reason}", data={"action_id": action.id, "decline_reason": exc.reason, "detail": exc.detail})
            outbox.emit(session, "bank_instruction.rejected", case.id, {"action_id": action.id, "reason": exc.reason})
        return f"rejected: {exc.reason}"
    except (ProviderTimeout, ProviderMalformedResponse) as exc:
        with session_scope() as session:
            action, case, instruction, approval = _load(session, action_id)
            now = clock.now(session)
            action.status = "outcome_unknown"
            action.last_error = f"{type(exc).__name__}: {exc}"
            action.updated_at = now
            instruction.status = "outcome_unknown"
            instruction.updated_at = now
            if case.state != states.OUTCOME_UNKNOWN:
                states.transition(session, case, states.OUTCOME_UNKNOWN, ACTOR, reason=f"provider outcome unknown: {type(exc).__name__}", data={"action_id": action.id, "request_ref": request_ref})
            enqueue_job(session, "reconcile_action", case.id, {"action_id": action.id, "lookups": 0})
            outbox.emit(session, "bank_instruction.outcome_unknown", case.id, {"action_id": action.id, "request_ref": request_ref})
        return "outcome_unknown"
    except ProviderAccessRevoked:
        with session_scope() as session:
            action, case, instruction, approval = _load(session, action_id)
            _hold(session, case, action, "access revoked at submission")
        return "manual_review: access revoked"

    return await _adopt_provider_record(action_id, result, source="submit")


async def _adopt_provider_record(action_id: str, record: dict, source: str) -> str:
    """Record the provider's acceptance for this action and schedule verification."""
    with session_scope() as session:
        action, case, instruction, approval = _load(session, action_id)
        now = clock.now(session)
        status = record.get("status")
        if status == "declined":
            action.status = "rejected"
            action.last_error = record.get("decline_reason")
            action.provider_reference = record.get("provider_reference")
            action.updated_at = now
            instruction.status = "rejected"
            instruction.external_ref = record.get("provider_reference")
            if case.state != states.REJECTED:
                states.transition(session, case, states.REJECTED, ACTOR, reason=f"provider declined: {record.get('decline_reason')}", data={"action_id": action.id, "source": source})
            return "rejected"
        if status == "cancelled":
            _hold(session, case, action, "provider reports the instruction was cancelled")
            return "manual_review: cancelled at provider"
        action.status = "submitted"
        action.provider_reference = record["provider_reference"]
        action.updated_at = now
        instruction.status = "submitted"
        instruction.external_ref = record["provider_reference"]
        instruction.updated_at = now
        if case.state in (states.APPROVED, states.OUTCOME_UNKNOWN, states.MANUAL_REVIEW):
            states.transition(session, case, states.SUBMITTED, ACTOR, data={"action_id": action.id, "provider_reference": record["provider_reference"], "request_ref": action.request_ref, "source": source, "duplicate_of_original": bool(record.get("duplicate_of_original"))})
        states.record_event(session, case, "bank_instruction.accepted", ACTOR, {"provider_reference": record["provider_reference"], "effective_on": record.get("effective_on"), "environment": record.get("_meta", {}).get("environment")})
        outbox.emit(session, "bank_instruction.accepted", case.id, {"instruction_id": instruction.id, "provider_reference": record["provider_reference"]})
        run_at = _midnight(instruction.effective_at) if record.get("effective_on") and date.fromisoformat(record["effective_on"]) > now.date() else now
        enqueue_job(session, "verify_action", case.id, {"action_id": action.id}, run_at=run_at)
    return "submitted"


def _hold(session, case: Case, action: Action, reason: str) -> None:
    now = clock.now(session)
    action.last_error = reason
    action.updated_at = now
    if case.state != states.MANUAL_REVIEW and states.can_transition(case.state, states.MANUAL_REVIEW):
        states.transition(session, case, states.MANUAL_REVIEW, ACTOR, reason=reason, data={"action_id": action.id})
    else:
        case.review_reason = reason
        states.record_event(session, case, "case.review_reason", ACTOR, {"reason": reason, "action_id": action.id})
    outbox.emit(session, "banking_case.manual_review", case.id, {"action_id": action.id, "reason": reason})


# --------------------------------------------------------------------------- #
# reconcile_action (uncertain outcome)
# --------------------------------------------------------------------------- #


async def reconcile_action(action_id: str, lookups: int = 0) -> str:
    with session_scope() as session:
        action, case, instruction, approval = _load(session, action_id)
        if action.status != "outcome_unknown":
            return f"noop: action {action.status}"
        provider_id = session.get(BankAccount, action.payload_json["source_account_id"]).provider_id
        request_ref = action.request_ref
        case_id = case.id
    adapter = adapter_for(provider_id, case_id)
    if not adapter.capabilities.can_lookup_by_request_ref:
        with session_scope() as session:
            action, case, instruction, approval = _load(session, action_id)
            _hold(session, case, action, "provider cannot look up writes by request reference; manual review required")
        return "manual_review: no lookup capability"
    try:
        found = await adapter.find_instruction(request_ref)
    except ProviderNotFound:
        found = None
    except ProviderError as exc:
        found = None
        if lookups + 1 >= RECONCILE_MAX_LOOKUPS:
            with session_scope() as session:
                action, case, instruction, approval = _load(session, action_id)
                _hold(session, case, action, f"provider lookup failed repeatedly: {exc}")
            return "manual_review: lookup failing"
    if found is None:
        if lookups + 1 >= RECONCILE_MAX_LOOKUPS:
            with session_scope() as session:
                action, case, instruction, approval = _load(session, action_id)
                _hold(session, case, action, "provider has no record of the request reference after repeated lookups; no new side effect will be created")
            return "manual_review: unresolved"
        with session_scope() as session:
            now = clock.now(session)
            enqueue_job(session, "reconcile_action", case_id, {"action_id": action_id, "lookups": lookups + 1}, run_at=now + timedelta(minutes=5 * (lookups + 1)))
        return "retry_lookup"
    return await _adopt_provider_record(action_id, found, source="reconcile_lookup")


# --------------------------------------------------------------------------- #
# verify_action (effective date tracking and reconciliation)
# --------------------------------------------------------------------------- #


async def verify_action(action_id: str) -> str:
    with session_scope() as session:
        action, case, instruction, approval = _load(session, action_id)
        if action.status not in ("submitted", "effective"):
            return f"noop: action {action.status}"
        if case.state not in (states.SUBMITTED, states.VERIFYING, states.MANUAL_REVIEW):
            return f"noop: case {case.state}"
        payload = dict(action.payload_json)
        request_ref = action.request_ref
        case_id = case.id
        source_provider = session.get(BankAccount, payload["source_account_id"]).provider_id
        before = {
            a.id: {"available_minor": a.available_minor, "current_minor": a.current_minor}
            for a in session.scalars(select(BankAccount).where(BankAccount.customer_id == case.customer_id))
        }
        effective_at = instruction.effective_at
        customer_id = case.customer_id

    adapter = adapter_for(source_provider, case_id)
    try:
        record = await adapter.find_instruction(request_ref)
    except ProviderNotFound:
        with session_scope() as session:
            action, case, instruction, approval = _load(session, action_id)
            _hold(session, case, action, "provider no longer returns the accepted instruction")
        return "manual_review: instruction vanished"
    except ProviderError as exc:
        with session_scope() as session:
            now = clock.now(session)
            enqueue_job(session, "verify_action", case_id, {"action_id": action_id}, run_at=now + timedelta(hours=1))
        return f"retry: {exc}"

    if record.get("status") == "accepted":
        with session_scope() as session:
            action, case, instruction, approval = _load(session, action_id)
            today = clock.today(session)
            if today > effective_at + timedelta(days=VERIFY_GRACE_DAYS):
                _hold(session, case, action, f"instruction accepted on {record.get('accepted_at')} but not effective {VERIFY_GRACE_DAYS} days after {effective_at.isoformat()}")
                return "manual_review: accepted but not effective"
            enqueue_job(session, "verify_action", case_id, {"action_id": action_id}, run_at=_midnight(today + timedelta(days=1)))
        return "waiting_for_effective_date"
    if record.get("status") in ("declined", "cancelled"):
        with session_scope() as session:
            action, case, instruction, approval = _load(session, action_id)
            _hold(session, case, action, f"provider reports {record.get('status')} after acceptance")
        return f"manual_review: {record.get('status')}"

    # Effective: collect bank-side evidence and reconcile.
    snapshots: dict[str, dict] = {}
    for account_id in before:
        with session_scope() as session:
            provider = session.get(BankAccount, account_id).provider_id
        try:
            snapshots[account_id] = await adapter_for(provider, case_id).get_snapshot(account_id)
        except ProviderError:
            continue

    with session_scope() as session:
        action, case, instruction, approval = _load(session, action_id)
        now = clock.now(session)
        instr_dict = {
            "request_ref": instruction.request_ref,
            "amount_minor": instruction.amount_minor,
            "currency": instruction.currency,
            "source_account_id": instruction.source_account_id,
            "destination_account_id": instruction.destination_account_id,
            "effective_at": instruction.effective_at.isoformat(),
            "term_days": instruction.term_days,
            "apy_decimal": instruction.apy_decimal,
            "product_version": instruction.product_version,
        }
        if action.type == "cd_renewal":
            source_snap = snapshots.get(instruction.source_account_id, {})
            renewed = next(
                (
                    d
                    for d in source_snap.get("deposits", [])
                    if d.get("status") == "open" and d.get("product_version") == instruction.product_version and d.get("opened_on") == instruction.effective_at.isoformat()
                ),
                None,
            )
            result = reconcile_renewal(instr_dict, record, renewed)
        else:
            src_after = snapshots.get(instruction.source_account_id, before[instruction.source_account_id])
            dst_after = snapshots.get(instruction.destination_account_id, before[instruction.destination_account_id])
            result = reconcile_transfer(
                instr_dict,
                record,
                before[instruction.source_account_id],
                {"current_minor": src_after["current_minor"]},
                before[instruction.destination_account_id],
                {"current_minor": dst_after["current_minor"]},
            )
        # Update application snapshots from bank evidence regardless of outcome.
        for account_id, snap in snapshots.items():
            acct = session.get(BankAccount, account_id)
            acct.available_minor = int(snap["available_minor"])
            acct.current_minor = int(snap["current_minor"])
            acct.pending_json = list(snap.get("pending") or [])
            acct.snapshot_at = now
            acct.snapshot_source = snap.get("_meta", {}).get("source", "provider")
        instruction.reconciliation_json = result.to_dict()
        instruction.updated_at = now
        action.provider_reference = record.get("provider_reference") or action.provider_reference
        if case.state in (states.SUBMITTED, states.MANUAL_REVIEW):
            states.transition(session, case, states.VERIFYING, ACTOR, data={"action_id": action.id, "provider_reference": action.provider_reference})
        states.record_event(session, case, "bank_instruction.effective", ACTOR, {"instruction_id": instruction.id, "provider_reference": action.provider_reference, "credited_amount_minor": record.get("credited_amount_minor")}, source_event_id=None)
        if result.matched:
            action.status = "verified"
            instruction.status = "verified"
            evidence_ref = f"provider_reference:{action.provider_reference}"
            case.completion_evidence_ref = evidence_ref
            states.record_event(session, case, "case.reconciled", ACTOR, {"instruction_id": instruction.id, "checks": result.checks, "evidence_refs": result.evidence_refs})
            states.transition(session, case, states.COMPLETED, ACTOR, data={"evidence_ref": evidence_ref})
            outbox.emit(session, "banking_case.completed", case.id, {"instruction_id": instruction.id, "provider_reference": action.provider_reference, "evidence_refs": result.evidence_refs})
            return "completed"
        action.status = "effective"
        instruction.status = "reconciliation_exception"
        failures = result.failures
        reason = "reconciliation exception: " + "; ".join(f"{f['check']} expected {f['expected']} got {f['actual']}" for f in failures)
        states.record_event(session, case, "case.reconciliation_exception", ACTOR, {"instruction_id": instruction.id, "failures": failures})
        states.transition(session, case, states.MANUAL_REVIEW, ACTOR, reason=reason, data={"failures": failures})
        outbox.emit(session, "banking_case.manual_review", case.id, {"instruction_id": instruction.id, "reason": reason})
        return "manual_review: reconciliation exception"


# --------------------------------------------------------------------------- #
# Inbox processing
# --------------------------------------------------------------------------- #


def process_inbox_event(session, event) -> str:
    """Idempotent consumer for provider callbacks."""
    payload = event.payload_json
    data = payload.get("data") or {}
    if event.event_type == "bank_instruction.effective":
        request_ref = data.get("request_ref")
        action = session.scalars(select(Action).where(Action.request_ref == request_ref)).first()
        if action is None:
            return "ignored: unknown request_ref"
        if not event.signature_valid:
            return "ignored: invalid signature"
        case = session.get(Case, action.case_id)
        states.record_event(session, case, "provider_event.received", "provider", {"event_type": event.event_type, "provider_reference": data.get("provider_reference")}, source_event_id=f"{event.provider_id}:{event.event_id}")
        if action.status in ("submitted", "effective", "outcome_unknown"):
            if action.status == "outcome_unknown":
                action.status = "submitted"
                action.provider_reference = data.get("provider_reference")
                if case.state == states.OUTCOME_UNKNOWN:
                    states.transition(session, case, states.SUBMITTED, "provider", data={"source": "callback"})
            enqueue_job(session, "verify_action", case.id, {"action_id": action.id})
            return "verify scheduled"
        return f"noop: action {action.status}"
    if event.event_type == "deposit.auto_renewed":
        return "recorded"
    return "ignored: unsupported event type"
