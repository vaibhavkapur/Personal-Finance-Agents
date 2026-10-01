"""Load synthetic fixtures into the database."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..clock import parse_iso
from .models import Customer, Document, Mortgage


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def content_hash(payload: Dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def seed_fixtures(session: Session, fixtures_dir: Path) -> Dict[str, List[str]]:
    """Idempotently insert customers, mortgages and documents from ``fixtures/``."""
    created: Dict[str, List[str]] = {"customers": [], "mortgages": [], "documents": []}

    customers = load_json(fixtures_dir / "customers.json")["customers"]
    for c in customers:
        if session.get(Customer, c["id"]) is None:
            session.add(
                Customer(
                    id=c["id"],
                    tenant_id=c["tenant_id"],
                    display_name=c["display_name"],
                    api_token_hash=token_hash(c["api_token"]),
                    verified_facts_json=c.get("verified_facts", {}),
                    property_json=c.get("property", {}),
                )
            )
            created["customers"].append(c["id"])
    session.flush()

    mort_file = load_json(fixtures_dir / "mortgages.json")
    for d in mort_file.get("documents", []):
        if session.get(Document, d["id"]) is None:
            session.add(
                Document(
                    id=d["id"],
                    owner_customer_id=d["owner_customer_id"],
                    kind=d["kind"],
                    object_key=f"fixtures/{d['id']}.json",
                    content_hash=content_hash(d),
                    source=d.get("source", "fixture"),
                    captured_at=parse_iso(d["captured_at"]) if d.get("captured_at") else None,
                    extraction_version=d.get("extraction_version", "fixture-1"),
                    content_json=d,
                )
            )
            created["documents"].append(d["id"])
    for m in mort_file["mortgages"]:
        if session.get(Mortgage, m["id"]) is None:
            session.add(
                Mortgage(
                    id=m["id"],
                    customer_id=m["customer_id"],
                    currency=m.get("currency", "USD"),
                    balance_minor=m["balance_minor"],
                    note_rate_decimal=m["note_rate_decimal"],
                    remaining_months=m["remaining_months"],
                    monthly_pi_minor=m.get("monthly_pi_minor"),
                    escrow_minor=m.get("escrow_minor", 0),
                    payment_includes_escrow=None,  # must be confirmed by the borrower
                    as_of=parse_iso(m["as_of"]) if m.get("as_of") else None,
                    evidence_id=m.get("evidence_id"),
                    servicer_lender_id="lender_mock_servicer",
                    balance_confirmed_at=None,
                )
            )
            created["mortgages"].append(m["id"])

    for path in sorted((fixtures_dir / "offers").glob("*.json")):
        doc = load_json(path)
        if session.get(Document, doc["document_id"]) is None:
            session.add(
                Document(
                    id=doc["document_id"],
                    owner_customer_id=doc["owner_customer_id"],
                    kind=doc.get("kind", "loan_estimate"),
                    object_key=f"fixtures/offers/{path.name}",
                    content_hash=content_hash(doc),
                    source=doc.get("source", "fixture"),
                    captured_at=parse_iso(doc["captured_at"]) if doc.get("captured_at") else None,
                    extraction_version=doc.get("extraction_version", "fixture-1"),
                    content_json=doc,
                )
            )
            created["documents"].append(doc["document_id"])
    session.flush()
    return created
