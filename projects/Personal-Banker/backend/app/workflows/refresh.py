"""Refresh application snapshots and offers from provider adapters.

Provider calls happen outside database transactions; results are written in a
short follow-up transaction. Each stored snapshot keeps its source timestamp
so staleness can be checked at submission time.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.adapters.base import ProviderAccessRevoked, ProviderError
from app.adapters.registry import adapter_for
from app.ids import new_id
from app.persistence.db import session_scope
from app.persistence.models import BankAccount, DepositContract, DepositOffer


async def refresh_snapshots(customer_id: str, case_id: str | None = None) -> dict:
    """Refresh every account of ``customer_id``. Returns a summary including
    accounts whose access is revoked (they block reads and execution)."""
    with session_scope() as session:
        accounts = session.scalars(select(BankAccount).where(BankAccount.customer_id == customer_id)).all()
        targets = [(a.id, a.provider_id) for a in accounts]

    results: dict[str, dict] = {}
    revoked: list[str] = []
    errors: dict[str, str] = {}
    for account_id, provider_id in targets:
        adapter = adapter_for(provider_id, case_id)
        try:
            results[account_id] = await adapter.get_snapshot(account_id)
        except ProviderAccessRevoked:
            revoked.append(account_id)
        except ProviderError as exc:
            errors[account_id] = str(exc)

    with session_scope() as session:
        now = clock.now(session)
        for account_id, snap in results.items():
            acct = session.get(BankAccount, account_id)
            if acct is None:
                continue
            acct.available_minor = int(snap["available_minor"])
            acct.current_minor = int(snap["current_minor"])
            acct.pending_json = list(snap.get("pending") or [])
            acct.snapshot_at = clock.parse_iso(snap["_meta"]["retrieved_at"]) if snap.get("_meta") else now
            acct.snapshot_source = snap.get("_meta", {}).get("source", "provider")
            acct.access_revoked = False
        for account_id in revoked:
            acct = session.get(BankAccount, account_id)
            if acct is not None:
                acct.access_revoked = True
    return {"refreshed": sorted(results), "revoked": revoked, "errors": errors, "snapshots": results}


async def refresh_offers(deposit_id: str, case_id: str | None = None) -> list[DepositOffer]:
    """Fetch provider offers for a deposit and upsert them as product
    versions. Versions no longer offered are marked superseded."""
    with session_scope() as session:
        contract = session.get(DepositContract, deposit_id)
        if contract is None:
            raise ValueError(f"unknown deposit {deposit_id}")
        account = session.get(BankAccount, contract.account_id)
        provider_ids = {account.provider_id} if account else set()
        # Offers may come from several providers in the fixture; ask each known provider.
        known = session.scalars(select(DepositOffer.provider_id).where(DepositOffer.for_deposit_id == deposit_id)).all()
        provider_ids.update(known)

    raw_offers: list[dict] = []
    for provider_id in sorted(provider_ids):
        adapter = adapter_for(provider_id, case_id)
        try:
            for offer in await adapter.get_offers(deposit_id):
                if offer.get("provider_id") == provider_id:
                    raw_offers.append(offer)
        except ProviderError:
            continue
    # De-duplicate offers returned by more than one adapter of the shared mock ledger.
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for offer in raw_offers:
        key = (offer["id"], str(offer["product_version"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(offer)

    with session_scope() as session:
        now = clock.now(session)
        existing = session.scalars(select(DepositOffer).where(DepositOffer.for_deposit_id == deposit_id)).all()
        by_key = {(o.provider_id, o.product_code, o.product_version): o for o in existing}
        current_keys = set()
        stored: list[DepositOffer] = []
        for offer in unique:
            key = (offer["provider_id"], offer["product_code"], str(offer["product_version"]))
            current_keys.add(key)
            row = by_key.get(key)
            if row is None:
                # New product version. Keep the provider's offer id for the first
                # version; later versions get their own id.
                same_id = session.get(DepositOffer, offer["id"])
                row_id = offer["id"] if same_id is None else new_id("off")
                row = DepositOffer(id=row_id, provider_id=offer["provider_id"], product_code=offer["product_code"], product_version=str(offer["product_version"]), for_deposit_id=deposit_id, retrieved_at=now)
                session.add(row)
            row.product_name = offer.get("product_name") or offer["product_code"]
            row.offer_kind = offer.get("offer_kind") or "unknown"
            row.apy_decimal = str(offer["apy_decimal"])
            row.rate_type = offer.get("rate_type") or "fixed"
            row.term_days = offer.get("term_days")
            row.fees_minor = int(offer.get("fees_minor") or 0)
            row.fee_description = offer.get("fee_description")
            row.restrictions_json = offer.get("restrictions") or {}
            row.accrual_method = offer.get("accrual_method") or "unknown"
            row.rounding = offer.get("rounding") or "ROUND_HALF_EVEN"
            row.valid_until = date.fromisoformat(offer["valid_until"])
            row.eligibility_status = offer.get("eligibility_status") or "unknown"
            row.eligibility_notes = offer.get("eligibility_notes")
            row.destination_account_id = offer.get("destination_account_id")
            row.retrieved_at = now
            row.environment = offer.get("environment") or "mock"
            row.authoritative = bool(offer.get("authoritative", True))
            row.superseded = False
            stored.append(row)
        for key, row in by_key.items():
            if key not in current_keys:
                row.superseded = True
        session.flush()
        for row in stored:
            session.refresh(row)
        session.expunge_all()
        return stored


def offer_to_raw(row: DepositOffer) -> dict:
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "product_code": row.product_code,
        "product_name": row.product_name,
        "product_version": row.product_version,
        "offer_kind": row.offer_kind,
        "apy_decimal": row.apy_decimal,
        "rate_type": row.rate_type,
        "term_days": row.term_days,
        "fees_minor": row.fees_minor,
        "fee_description": row.fee_description,
        "restrictions": row.restrictions_json or {},
        "accrual_method": row.accrual_method,
        "rounding": row.rounding,
        "valid_until": row.valid_until.isoformat(),
        "eligibility_status": row.eligibility_status,
        "eligibility_notes": row.eligibility_notes,
        "destination_account_id": row.destination_account_id,
        "retrieved_at": clock.iso(row.retrieved_at),
        "environment": row.environment,
        "authoritative": row.authoritative,
        "evidence_id": row.evidence_id,
    }


def active_offers(session: Session, deposit_id: str) -> list[DepositOffer]:
    return list(
        session.scalars(
            select(DepositOffer).where(DepositOffer.for_deposit_id == deposit_id, DepositOffer.superseded.is_(False)).order_by(DepositOffer.id)
        )
    )
