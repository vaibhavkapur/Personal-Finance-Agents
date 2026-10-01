"""Seed the application and the mock bank from the synthetic fixture."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app import clock
from app.adapters.mock_bank import seed_ledger
from app.config import FIXTURES_DIR
from app.persistence.models import (
    BankAccount,
    Customer,
    DepositContract,
    DepositOffer,
    Document,
    Obligation,
    Provider,
)


def load_fixture(path: Path | None = None) -> dict:
    with open(path or FIXTURES_DIR / "demo_customer.json") as fh:
        return json.load(fh)


def seed(session: Session, fixture: dict | None = None, reset_clock: bool = True) -> dict:
    fixture = fixture or load_fixture()
    if reset_clock:
        existing = session.get(clock.SimClock, 1)
        if existing is not None:
            session.delete(existing)
            session.flush()
        clock.ensure_clock(session, fixture.get("clock_start"))
    now = clock.now(session)

    for c in fixture["customers"]:
        session.merge(Customer(id=c["id"], display_name=c["display_name"], created_at=now))
    for p in fixture["providers"]:
        session.merge(Provider(id=p["id"], display_name=p["display_name"], environment="mock"))
    for d in fixture.get("documents", []):
        session.merge(
            Document(
                id=d["id"],
                owner_id=d["owner_id"],
                object_key=d["object_key"],
                content_hash=d["content_hash"],
                source=d["source"],
                captured_at=clock.parse_iso(d["captured_at"]),
                extraction_version=fixture.get("fixture_version", "fixture-v1"),
                summary=d.get("summary"),
            )
        )
    session.flush()
    for a in fixture["accounts"]:
        session.merge(
            BankAccount(
                id=a["id"],
                customer_id=a["customer_id"],
                provider_id=a["provider_id"],
                account_kind=a["account_kind"],
                display_name=a["display_name"],
                ownership_verified=a["ownership_verified"],
                access_revoked=False,
                currency=a["currency"],
                available_minor=a["available_minor"],
                current_minor=a["current_minor"],
                pending_json=a.get("pending", []),
                snapshot_at=now,
                snapshot_source="fixture",
                evidence_id=a.get("evidence_id"),
            )
        )
    session.flush()
    for cd in fixture["deposit_contracts"]:
        session.merge(
            DepositContract(
                id=cd["id"],
                account_id=cd["account_id"],
                principal_minor=cd["principal_minor"],
                currency=cd["currency"],
                apy_decimal=cd["apy_decimal"],
                maturity_date=date.fromisoformat(cd["maturity_date"]),
                renewal_instruction_deadline=date.fromisoformat(cd["renewal_instruction_deadline"]),
                grace_period_days=cd.get("grace_period_days", 10),
                contract_version=cd["contract_version"],
                default_maturity_behavior=cd["default_maturity_behavior"],
                evidence_id=cd.get("evidence_id"),
            )
        )
    for o in fixture["obligations"]:
        session.merge(
            Obligation(
                id=o["id"],
                customer_id=o["customer_id"],
                description=o["description"],
                amount_minor=o["amount_minor"],
                currency=o["currency"],
                due_date=date.fromisoformat(o["due_date"]),
                certainty=o["certainty"],
                evidence_id=o.get("evidence_id"),
            )
        )
    for off in fixture["offers"]:
        session.merge(
            DepositOffer(
                id=off["id"],
                provider_id=off["provider_id"],
                product_code=off["product_code"],
                product_name=off["product_name"],
                product_version=off["product_version"],
                offer_kind=off["offer_kind"],
                apy_decimal=off["apy_decimal"],
                rate_type=off.get("rate_type", "fixed"),
                term_days=off.get("term_days"),
                fees_minor=off.get("fees_minor", 0),
                fee_description=off.get("fee_description"),
                restrictions_json=off.get("restrictions", {}),
                accrual_method=off.get("accrual_method", "unknown"),
                rounding=off.get("rounding", "ROUND_HALF_EVEN"),
                valid_until=date.fromisoformat(off["valid_until"]),
                eligibility_status=off.get("eligibility_status", "unknown"),
                eligibility_notes=off.get("eligibility_notes"),
                destination_account_id=off.get("destination_account_id"),
                for_deposit_id=off["for_deposit_id"],
                retrieved_at=now,
                environment="mock",
                authoritative=True,
                evidence_id=None,
                superseded=False,
            )
        )
    seed_ledger(session, fixture)
    session.flush()
    return fixture
