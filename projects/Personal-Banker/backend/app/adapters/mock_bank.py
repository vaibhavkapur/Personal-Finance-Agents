"""Bank simulator (plan §12).

The mock bank keeps its own ledger (``mock_bank_*`` tables) independent of the
application's snapshots, runs on the shared simulation clock and exposes
controllable failure modes:

``normal``                   accept; becomes effective on the effective date
``accepted_not_effective``   accept; never becomes effective on its own
``changed_rate``             (offer mode) the selected product is re-issued with a new version/APY
``expired_offer``            (offer mode) the selected product's validity is moved into the past
``insufficient_available``   decline: insufficient available balance
``declined``                 decline: generic provider decline
``accepted_before_timeout``  record acceptance, then time out on the caller
``malformed_response``       record acceptance, answer with an unreadable body
``delayed_callback``         accept; the effective callback arrives two days late
``completed_amount_mismatch`` accept; credit one dollar less than instructed

No external credentials are required. Provider calls are recorded in
``adapter_requests`` (redacted) for the operator view.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import clock
from app.adapters.base import (
    AdapterCapabilities,
    ProviderAccessRevoked,
    ProviderDeclined,
    ProviderMalformedResponse,
    ProviderNotFound,
    ProviderTimeout,
    meta,
)
from app.config import settings
from app.ids import new_id
from app.persistence.models import (
    AdapterRequest,
    MockBankAccount,
    MockBankConfig,
    MockBankDeposit,
    MockBankInstruction,
    MockBankOffer,
)

SUBMIT_MODES = {
    "normal",
    "accepted_not_effective",
    "insufficient_available",
    "declined",
    "accepted_before_timeout",
    "malformed_response",
    "delayed_callback",
    "completed_amount_mismatch",
}
OFFER_MODES = {"normal", "changed_rate", "expired_offer"}
ENVIRONMENT = "mock"
SOURCE = "mock_bank"


def sign_payload(payload: dict, secret: str | None = None) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new((secret or settings.webhook_secret).encode(), body, hashlib.sha256).hexdigest()


def _redact(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: ("<redacted>" if k in {"customer_id", "owner_id"} else _redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v) for v in data]
    return data


def get_config(session: Session) -> MockBankConfig:
    cfg = session.get(MockBankConfig, 1)
    if cfg is None:
        cfg = MockBankConfig(id=1, next_submit_mode="normal", offer_mode="normal")
        session.add(cfg)
        session.flush()
    return cfg


class MockBankAdapter:
    """Adapter for one provider backed by the shared mock ledger."""

    def __init__(self, provider_id: str, session_factory: sessionmaker[Session], case_id: str | None = None):
        self.provider_id = provider_id
        self._sf = session_factory
        self.case_id = case_id
        self.capabilities = AdapterCapabilities(
            provider_id=provider_id,
            environment=ENVIRONMENT,
            can_read_snapshots=True,
            can_read_offers=True,
            can_submit_instructions=True,
            can_lookup_by_request_ref=True,
            can_cancel_after_acceptance=True,
            supports_callbacks=True,
            notes="Simulator. Controllable clock and failure modes; no real money moves.",
        )

    # ------------------------------------------------------------------ util
    def _record(self, session: Session, operation: str, request: dict, response: dict, outcome: str, started: float, request_ref: str | None = None) -> None:
        session.add(
            AdapterRequest(
                id=new_id("areq"),
                provider_id=self.provider_id,
                operation=operation,
                request_ref=request_ref,
                case_id=self.case_id,
                request_redacted_json=_redact(request),
                response_redacted_json=_redact(response),
                environment=ENVIRONMENT,
                latency_ms=int((time.perf_counter() - started) * 1000),
                outcome=outcome,
                created_at=clock.now(session),
            )
        )

    def _meta(self, session: Session, authoritative: bool = True) -> dict:
        return meta(ENVIRONMENT, SOURCE, clock.iso(clock.now(session)), authoritative)

    # ----------------------------------------------------------------- reads
    async def get_snapshot(self, account_id: str) -> dict:
        started = time.perf_counter()
        with self._sf() as session:
            acct = session.get(MockBankAccount, account_id)
            if acct is None:
                self._record(session, "get_snapshot", {"account_id": account_id}, {"error": "not_found"}, "error", started)
                session.commit()
                raise ProviderNotFound(account_id)
            if acct.access_revoked:
                self._record(session, "get_snapshot", {"account_id": account_id}, {"error": "access_revoked"}, "error", started)
                session.commit()
                raise ProviderAccessRevoked(account_id)
            deposits = session.scalars(select(MockBankDeposit).where(MockBankDeposit.account_id == account_id)).all()
            result = {
                "account_id": acct.id,
                "provider_id": acct.provider_id,
                "owner_id": acct.owner_id,
                "kind": acct.kind,
                "currency": acct.currency,
                "available_minor": acct.available_minor,
                "current_minor": acct.current_minor,
                "pending": list(acct.pending_json or []),
                "deposits": [self._deposit_dict(d) for d in deposits],
                "_meta": self._meta(session),
            }
            self._record(session, "get_snapshot", {"account_id": account_id}, result, "ok", started)
            session.commit()
            return result

    @staticmethod
    def _deposit_dict(d: MockBankDeposit) -> dict:
        return {
            "id": d.id,
            "account_id": d.account_id,
            "principal_minor": d.principal_minor,
            "currency": d.currency,
            "apy_decimal": d.apy_decimal,
            "term_days": d.term_days,
            "opened_on": d.opened_on.isoformat(),
            "maturity_date": d.maturity_date.isoformat(),
            "product_version": d.product_version,
            "status": d.status,
            "matured_from_id": d.matured_from_id,
        }

    async def get_offers(self, deposit_id: str) -> list[dict]:
        started = time.perf_counter()
        with self._sf() as session:
            rows = session.scalars(
                select(MockBankOffer).where(MockBankOffer.for_deposit_id == deposit_id, MockBankOffer.active.is_(True))
            ).all()
            retrieved = clock.iso(clock.now(session))
            offers = []
            for row in rows:
                payload = dict(row.payload_json)
                payload.update({"retrieved_at": retrieved, "environment": ENVIRONMENT, "authoritative": True})
                offers.append(payload)
            self._record(session, "get_offers", {"deposit_id": deposit_id}, {"count": len(offers), "versions": [o["product_version"] for o in offers]}, "ok", started)
            session.commit()
            return offers

    async def find_instruction(self, request_ref: str) -> dict:
        started = time.perf_counter()
        with self._sf() as session:
            row = session.get(MockBankInstruction, request_ref)
            if row is None:
                self._record(session, "find_instruction", {"request_ref": request_ref}, {"found": False}, "ok", started, request_ref)
                session.commit()
                raise ProviderNotFound(request_ref)
            result = self._instruction_dict(session, row)
            self._record(session, "find_instruction", {"request_ref": request_ref}, result, "ok", started, request_ref)
            session.commit()
            return result

    def _instruction_dict(self, session: Session, row: MockBankInstruction) -> dict:
        payload = row.payload_json
        return {
            "request_ref": row.request_ref,
            "provider_reference": row.provider_reference,
            "status": row.status,
            "decline_reason": row.decline_reason,
            "effective_on": row.effective_on.isoformat() if row.effective_on else None,
            "applied": row.applied,
            "credited_amount_minor": row.credited_amount_minor,
            "amount_minor": payload.get("amount_minor"),
            "currency": payload.get("currency"),
            "source_account_id": payload.get("source_account_id"),
            "destination_account_id": payload.get("destination_account_id"),
            "instruction_type": payload.get("instruction_type"),
            "product_version": payload.get("product_version"),
            "accepted_at": clock.iso(row.accepted_at),
            "_meta": self._meta(session),
        }

    # ---------------------------------------------------------------- writes
    async def submit_instruction(self, payload: dict, request_ref: str) -> dict:
        started = time.perf_counter()
        with self._sf() as session:
            existing = session.get(MockBankInstruction, request_ref)
            if existing is not None:
                # Idempotent: the same client reference returns the original record.
                result = self._instruction_dict(session, existing)
                result["duplicate_of_original"] = True
                self._record(session, "submit_instruction", {"request_ref": request_ref}, result, "ok", started, request_ref)
                session.commit()
                return result

            cfg = get_config(session)
            mode = cfg.next_submit_mode or "normal"
            today = clock.today(session)

            decline = self._validate(session, payload, mode)
            if decline is not None:
                row = MockBankInstruction(
                    request_ref=request_ref,
                    provider_reference=new_id("bankref"),
                    provider_id=self.provider_id,
                    payload_json=payload,
                    status="declined",
                    decline_reason=decline,
                    accepted_at=clock.now(session),
                )
                session.add(row)
                result = self._instruction_dict(session, row)
                self._record(session, "submit_instruction", _redact(payload), result, "declined", started, request_ref)
                session.commit()
                raise ProviderDeclined(decline, detail=json.dumps({"provider_reference": row.provider_reference}))

            effective_on = date.fromisoformat(payload["effective_at"])
            row = MockBankInstruction(
                request_ref=request_ref,
                provider_reference=new_id("bankref"),
                provider_id=self.provider_id,
                payload_json=payload,
                status="accepted",
                effective_on=None if mode == "accepted_not_effective" else max(effective_on, today),
                callback_delay_days=2 if mode == "delayed_callback" else 0,
                credited_amount_minor=None,
                accepted_at=clock.now(session),
            )
            if mode == "completed_amount_mismatch":
                row.payload_json = {**payload, "_sim_credit_shortfall_minor": 100}
            session.add(row)
            result = self._instruction_dict(session, row)

            if mode == "accepted_before_timeout":
                self._record(session, "submit_instruction", _redact(payload), {"note": "accepted, then connection dropped"}, "timeout", started, request_ref)
                session.commit()
                raise ProviderTimeout("connection dropped after acceptance")
            if mode == "malformed_response":
                self._record(session, "submit_instruction", _redact(payload), {"raw": "<html>502 Bad Gateway</html>"}, "malformed", started, request_ref)
                session.commit()
                raise ProviderMalformedResponse("unreadable provider response")

            self._record(session, "submit_instruction", _redact(payload), result, "ok", started, request_ref)
            session.commit()
            return result

    def _validate(self, session: Session, payload: dict, mode: str) -> str | None:
        if mode == "declined":
            return "provider_declined"
        if mode == "insufficient_available":
            return "insufficient_available_balance"
        source = session.get(MockBankAccount, payload.get("source_account_id"))
        if source is None:
            return "source_account_unknown"
        if source.access_revoked:
            return "access_revoked"
        if source.owner_id != payload.get("customer_id"):
            return "source_not_owned_by_customer"
        dest_id = payload.get("destination_account_id")
        if payload.get("instruction_type") == "same_owner_transfer":
            dest = session.get(MockBankAccount, dest_id)
            if dest is None:
                return "destination_account_unknown"
            if dest.owner_id != source.owner_id:
                return "destination_not_same_owner"
        deposits = session.scalars(
            select(MockBankDeposit).where(MockBankDeposit.account_id == source.id, MockBankDeposit.status == "open")
        ).all()
        if payload.get("instruction_type") in ("cd_renewal", "same_owner_transfer") and source.kind == "fixed_term_deposit":
            if not deposits:
                return "no_open_deposit"
            dep = deposits[0]
            if int(payload.get("amount_minor", 0)) > dep.principal_minor:
                return "insufficient_available_balance"
            if date.fromisoformat(payload["effective_at"]) != dep.maturity_date:
                return "effective_date_must_equal_maturity"
        elif int(payload.get("amount_minor", 0)) > source.available_minor:
            return "insufficient_available_balance"
        # Product version must still be the active version at this provider.
        product_code = payload.get("product_code")
        if product_code:
            rows = session.scalars(select(MockBankOffer).where(MockBankOffer.active.is_(True))).all()
            row = next((r for r in rows if r.payload_json.get("product_code") == product_code and r.provider_id == payload.get("provider_id")), None)
            if row is None:
                return "product_not_available"
            if str(row.payload_json.get("product_version")) != str(payload.get("product_version")):
                return "product_version_superseded"
            if date.fromisoformat(row.payload_json["valid_until"]) < clock.today(session):
                return "offer_expired"
        return None

    async def cancel_instruction(self, request_ref: str) -> dict:
        """Cancellation after acceptance is a separate provider request whose
        success must be confirmed (plan §8)."""
        started = time.perf_counter()
        with self._sf() as session:
            row = session.get(MockBankInstruction, request_ref)
            if row is None:
                session.commit()
                raise ProviderNotFound(request_ref)
            if row.status == "accepted" and not row.applied:
                row.status = "cancelled"
                outcome = "ok"
            else:
                outcome = "declined"
            result = self._instruction_dict(session, row)
            self._record(session, "cancel_instruction", {"request_ref": request_ref}, result, outcome, started, request_ref)
            session.commit()
            if outcome != "ok":
                raise ProviderDeclined("cannot_cancel_in_current_state")
            return result


# ---------------------------------------------------------------------------
# Ledger operations (provider side, driven by the clock)
# ---------------------------------------------------------------------------


def set_submit_mode(session: Session, mode: str) -> None:
    if mode not in SUBMIT_MODES:
        raise ValueError(f"unknown submit mode {mode!r}; choose one of {sorted(SUBMIT_MODES)}")
    get_config(session).next_submit_mode = mode


def set_offer_mode(session: Session, mode: str, offer_id: str = "off_harbor_12m") -> dict | None:
    """Mutate the provider's product catalogue to simulate changed terms."""
    if mode not in OFFER_MODES:
        raise ValueError(f"unknown offer mode {mode!r}; choose one of {sorted(OFFER_MODES)}")
    cfg = get_config(session)
    cfg.offer_mode = mode
    row = session.get(MockBankOffer, offer_id)
    if row is None or mode == "normal":
        return None
    payload = dict(row.payload_json)
    if mode == "changed_rate":
        current = payload["product_version"]
        payload["product_version"] = f"{current}-revised"
        new_apy = str((Decimal(payload["apy_decimal"]) - Decimal("0.0035")).quantize(Decimal("0.0001")))
        payload["apy_decimal"] = new_apy
        payload["valid_until"] = (clock.today(session) + timedelta(days=14)).isoformat()
    elif mode == "expired_offer":
        payload["valid_until"] = (clock.today(session) - timedelta(days=1)).isoformat()
    row.payload_json = payload
    session.flush()
    return payload


def revoke_access(session: Session, account_id: str, revoked: bool = True) -> None:
    acct = session.get(MockBankAccount, account_id)
    if acct is None:
        raise KeyError(account_id)
    acct.access_revoked = revoked


def process_due_instructions(session: Session) -> list[dict]:
    """Apply every accepted instruction whose effective date has arrived and
    return the callback events the bank would deliver. Also handles the
    contract's default maturity behaviour when no instruction exists."""
    today = clock.today(session)
    now = clock.now(session)
    events: list[dict] = []

    rows = session.scalars(select(MockBankInstruction).where(MockBankInstruction.status == "accepted")).all()
    for row in rows:
        if row.effective_on is None or row.effective_on > today or row.applied:
            continue
        _apply_instruction(session, row, now)
        row.applied = True
        row.status = "effective"
        if row.callback_delay_days == 0:
            row.callback_sent = True
            events.append(_effective_event(row, now))

    # Late callbacks
    rows = session.scalars(
        select(MockBankInstruction).where(MockBankInstruction.status == "effective", MockBankInstruction.callback_sent.is_(False))
    ).all()
    for row in rows:
        if row.effective_on and row.effective_on + timedelta(days=row.callback_delay_days) <= today:
            row.callback_sent = True
            events.append(_effective_event(row, now))

    # Default maturity behaviour: deposits past maturity + grace with no instruction auto-renew.
    deposits = session.scalars(select(MockBankDeposit).where(MockBankDeposit.status == "open")).all()
    for dep in deposits:
        if dep.maturity_date + timedelta(days=10) <= today:
            has_instruction = session.scalars(
                select(MockBankInstruction).where(
                    MockBankInstruction.status.in_(["accepted", "effective"]),
                )
            ).all()
            if any(i.payload_json.get("source_account_id") == dep.account_id for i in has_instruction):
                continue
            dep.status = "renewed"
            renewed = MockBankDeposit(
                id=new_id("dep"),
                account_id=dep.account_id,
                principal_minor=dep.principal_minor,
                currency=dep.currency,
                apy_decimal=dep.apy_decimal,
                term_days=dep.term_days,
                opened_on=dep.maturity_date,
                maturity_date=dep.maturity_date + timedelta(days=dep.term_days),
                product_version="default-auto-renewal",
                status="open",
                matured_from_id=dep.id,
            )
            session.add(renewed)
            events.append(
                {
                    "id": new_id("bevt"),
                    "type": "deposit.auto_renewed",
                    "occurred_at": clock.iso(now),
                    "environment": ENVIRONMENT,
                    "data": {"deposit_id": dep.id, "renewed_deposit_id": renewed.id},
                }
            )
    session.flush()
    return events


def _effective_event(row: MockBankInstruction, now: datetime) -> dict:
    return {
        "id": new_id("bevt"),
        "type": "bank_instruction.effective",
        "occurred_at": clock.iso(now),
        "environment": ENVIRONMENT,
        "data": {
            "request_ref": row.request_ref,
            "provider_reference": row.provider_reference,
            "effective_on": row.effective_on.isoformat() if row.effective_on else None,
            "credited_amount_minor": row.credited_amount_minor,
        },
    }


def _apply_instruction(session: Session, row: MockBankInstruction, now: datetime) -> None:
    payload = row.payload_json
    amount = int(payload["amount_minor"])
    shortfall = int(payload.get("_sim_credit_shortfall_minor", 0))
    credited = amount - shortfall
    source = session.get(MockBankAccount, payload["source_account_id"])
    assert source is not None
    deposit = session.scalars(
        select(MockBankDeposit).where(MockBankDeposit.account_id == source.id, MockBankDeposit.status == "open")
    ).first()
    payout_account = None
    if deposit is not None:
        deposit.status = "matured"
        # Proceeds not covered by the instruction go to the customer's payout account.
        payout_account = _payout_account(session, source.owner_id, deposit.maturity_proceeds_account_id)
        remainder = deposit.principal_minor - amount
        source.current_minor -= deposit.principal_minor
        if payout_account is not None and remainder > 0:
            payout_account.available_minor += remainder
            payout_account.current_minor += remainder
    else:
        source.available_minor -= amount
        source.current_minor -= amount

    if payload["instruction_type"] == "cd_renewal":
        term_days = int(payload["term_days"])
        renewed = MockBankDeposit(
            id=new_id("dep"),
            account_id=source.id,
            principal_minor=credited,
            currency=payload["currency"],
            apy_decimal=str(payload["apy_decimal"]),
            term_days=term_days,
            opened_on=row.effective_on,
            maturity_date=row.effective_on + timedelta(days=term_days),
            product_version=str(payload["product_version"]),
            status="open",
            matured_from_id=deposit.id if deposit else None,
            maturity_proceeds_account_id=deposit.maturity_proceeds_account_id if deposit else None,
        )
        session.add(renewed)
        source.current_minor += credited
    else:
        dest = session.get(MockBankAccount, payload["destination_account_id"])
        assert dest is not None
        dest.available_minor += credited
        dest.current_minor += credited
    row.credited_amount_minor = credited


def _payout_account(session: Session, owner_id: str, preferred_id: str | None) -> MockBankAccount | None:
    if preferred_id:
        acct = session.get(MockBankAccount, preferred_id)
        if acct is not None:
            return acct
    return session.scalars(
        select(MockBankAccount).where(MockBankAccount.owner_id == owner_id, MockBankAccount.kind == "checking")
    ).first()


def seed_ledger(session: Session, fixture: dict) -> None:
    """Populate the provider-side ledger from the synthetic fixture."""
    for acct in fixture["accounts"]:
        session.merge(
            MockBankAccount(
                id=acct["id"],
                provider_id=acct["provider_id"],
                owner_id=acct["customer_id"],
                kind=acct["account_kind"],
                currency=acct["currency"],
                available_minor=acct["available_minor"],
                current_minor=acct["current_minor"],
                pending_json=acct.get("pending", []),
                access_revoked=False,
            )
        )
    checking = next((a for a in fixture["accounts"] if a["account_kind"] == "checking"), None)
    for cd in fixture["deposit_contracts"]:
        session.merge(
            MockBankDeposit(
                id=cd["id"],
                account_id=cd["account_id"],
                principal_minor=cd["principal_minor"],
                currency=cd["currency"],
                apy_decimal=cd["apy_decimal"],
                term_days=cd["term_days"],
                opened_on=date.fromisoformat(cd["opened_on"]),
                maturity_date=date.fromisoformat(cd["maturity_date"]),
                product_version=cd["contract_version"],
                status="open",
                maturity_proceeds_account_id=checking["id"] if checking else None,
            )
        )
    for offer in fixture["offers"]:
        session.merge(
            MockBankOffer(
                id=offer["id"],
                provider_id=offer["provider_id"],
                for_deposit_id=offer["for_deposit_id"],
                payload_json=offer,
                active=True,
            )
        )
    get_config(session)
    session.flush()
