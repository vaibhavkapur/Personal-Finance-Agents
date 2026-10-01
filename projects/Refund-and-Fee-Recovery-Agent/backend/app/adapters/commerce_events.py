"""Optional UCP / ACP fixture ingestion.

UCP order adjustments and ACP order/refund events are translated into evidence
documents. They never grant execution authority: a refund event is a merchant
claim that a refund was initiated, which still has to be matched to a posted
credit on the account feed.

The mappings below follow the shape of the public fixtures in
fixtures/commerce_events/ and are pinned to the schema versions recorded there.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from ..clock import Clock, format_ts
from ..domain.models import Document
from ..ids import canonical_json, new_id

SUPPORTED = {"ucp": "ucp.order.v1-fixture", "acp": "acp.webhook.v1-fixture"}


class UnsupportedCommerceEvent(ValueError):
    pass


def _doc(customer_id: str, kind: str, source: str, payload: Dict[str, Any], extracted: Dict[str, Any], clock: Clock) -> Document:
    body = canonical_json(payload)
    return Document(
        id=new_id("doc"),
        customer_id=customer_id,
        kind=kind,
        object_key=f"commerce/{kind}/{hashlib.sha256(body.encode()).hexdigest()[:16]}.json",
        content_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        source=source,
        captured_at=format_ts(clock.now()),
        extraction_version=SUPPORTED[source.split(":")[0]],
        extracted=dict(extracted, authority="simulated", grants_execution_authority=False, raw=payload),
    )


def ingest_ucp_order_adjustment(customer_id: str, payload: Dict[str, Any], clock: Clock) -> Document:
    """UCP order with post-purchase adjustments (fixture shape)."""
    if payload.get("schema") != "ucp.order.v1-fixture":
        raise UnsupportedCommerceEvent(f"unsupported UCP schema: {payload.get('schema')!r}")
    order = payload["order"]
    adjustments = [a for a in order.get("adjustments", []) if a.get("type") == "refund"]
    total_minor = sum(int(a["amount"]["value_minor"]) for a in adjustments)
    currency = adjustments[0]["amount"]["currency"] if adjustments else order.get("currency", "USD")
    return _doc(customer_id, "ucp_order_adjustment", "ucp:fixture", payload, {
        "order_ref": order["id"],
        "merchant_ref": order.get("merchant", {}).get("id"),
        "refund_adjustments": [{"adjustment_id": a["id"], "amount_minor": int(a["amount"]["value_minor"]), "currency": a["amount"]["currency"], "status": a.get("status"), "refund_ref": a.get("payment_reference")} for a in adjustments],
        "refund_total_minor": total_minor,
        "currency": currency,
        "refund_refs": [a["payment_reference"] for a in adjustments if a.get("payment_reference")],
    }, clock)


def ingest_acp_refund_event(customer_id: str, payload: Dict[str, Any], clock: Clock) -> Document:
    """ACP `order.refunded`-style webhook (fixture shape)."""
    if payload.get("schema") != "acp.webhook.v1-fixture":
        raise UnsupportedCommerceEvent(f"unsupported ACP schema: {payload.get('schema')!r}")
    if payload.get("type") not in ("order.refunded", "order.refund_initiated"):
        raise UnsupportedCommerceEvent(f"unsupported ACP event type: {payload.get('type')!r}")
    data = payload["data"]
    refunds = data.get("refunds", [])
    return _doc(customer_id, "acp_refund_event", "acp:fixture", payload, {
        "event_id": payload.get("id"),
        "event_type": payload.get("type"),
        "order_ref": data["order_id"],
        "merchant_ref": data.get("merchant_id"),
        "refunds": [{"refund_ref": r.get("id"), "amount_minor": int(r["amount_minor"]), "currency": r["currency"], "status": r.get("status"), "destination": r.get("destination", "original_payment")} for r in refunds],
        "refund_total_minor": sum(int(r["amount_minor"]) for r in refunds),
        "currency": refunds[0]["currency"] if refunds else data.get("currency", "USD"),
        "refund_refs": [r["id"] for r in refunds if r.get("id")],
    }, clock)


def ingest_commerce_event(customer_id: str, payload: Dict[str, Any], clock: Clock) -> Document:
    schema = str(payload.get("schema", ""))
    if schema.startswith("ucp."):
        return ingest_ucp_order_adjustment(customer_id, payload, clock)
    if schema.startswith("acp."):
        return ingest_acp_refund_event(customer_id, payload, clock)
    raise UnsupportedCommerceEvent(f"unknown commerce event schema {schema!r}")


def load_fixture(path: str) -> Dict[str, Any]:
    with open(path) as fh:
        return json.load(fh)


def refund_refs_from_documents(docs: list) -> set:
    refs = set()
    for d in docs:
        if d.kind in ("ucp_order_adjustment", "acp_refund_event"):
            refs.update(d.extracted.get("refund_refs", []))
    return refs


def order_ref_of(doc: Document) -> Optional[str]:
    return doc.extracted.get("order_ref")
