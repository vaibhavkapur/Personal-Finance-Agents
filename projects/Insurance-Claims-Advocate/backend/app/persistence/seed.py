"""Load synthetic fixtures (customers, principals, policies, documents) into the database. Idempotent."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import select

from ..clock import Clock
from ..config import FIXTURES_DIR
from ..domain.policy import PolicyFixture
from ..ids import canonical_json, sha256_hex
from .db import Database
from .models import Customer, Document, Policy, Principal


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_policy_fixtures(fixtures_dir: Path = FIXTURES_DIR) -> Dict[str, PolicyFixture]:
    out: Dict[str, PolicyFixture] = {}
    for path in sorted((fixtures_dir / "policies").glob("*.json")):
        fixture = PolicyFixture(json.loads(path.read_text()))
        out[fixture.policy_id] = fixture
    return out


def seed(db: Database, clock: Clock, fixtures_dir: Path = FIXTURES_DIR) -> Dict[str, Any]:
    customers = json.loads((fixtures_dir / "customers.json").read_text())
    documents = json.loads((fixtures_dir / "documents.json").read_text())
    policies = load_policy_fixtures(fixtures_dir)
    counts = {"customers": 0, "principals": 0, "policies": 0, "documents": 0}
    now = clock.now_iso()
    with db.session() as s:
        for c in customers["customers"]:
            if not s.get(Customer, c["id"]):
                s.add(Customer(id=c["id"], tenant_id=c.get("tenant_id", "tenant_demo"), full_name=c["full_name"], email_masked=c.get("email_masked", ""), payout_destination_json=c.get("payout_destination", {}), created_at=now))
                counts["customers"] += 1
        for p in customers["principals"]:
            if not s.get(Principal, p["id"]):
                s.add(Principal(id=p["id"], token_hash=token_hash(p["token"]), role=p["role"], customer_id=p.get("customer_id"), display_name=p["display_name"]))
                counts["principals"] += 1
        for pid, fixture in policies.items():
            if not s.get(Policy, pid):
                raw = json.loads((fixtures_dir / "policies" / f"{pid}.json").read_text())
                s.add(Policy(id=pid, customer_id=fixture.customer_id, insurer_name=fixture.insurer_name, fixture_json=raw))
                counts["policies"] += 1
        for d in documents["documents"]:
            if not s.get(Document, d["id"]):
                content = {"pages": d["pages"]}
                s.add(
                    Document(
                        id=d["id"], owner_customer_id=d["owner_customer_id"], doc_type=d["doc_type"], object_key=f"fixtures/documents/{d['id']}.txt", content_hash="sha256:" + sha256_hex(canonical_json(content)),
                        source=d.get("source", "customer_upload"), captured_at=d["captured_at"], extraction_version="", content_json=content, version=1, created_at=now,
                    )
                )
                counts["documents"] += 1
    return counts


def fixture_tokens(fixtures_dir: Path = FIXTURES_DIR) -> List[Dict[str, str]]:
    customers = json.loads((fixtures_dir / "customers.json").read_text())
    return [{"principal_id": p["id"], "token": p["token"], "role": p["role"], "customer_id": p.get("customer_id")} for p in customers["principals"]]
