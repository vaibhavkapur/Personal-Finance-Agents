"""Provider-facing callback endpoint (signed, deduplicated, at-least-once safe)."""
from __future__ import annotations

import hmac
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.mock_lender import canonical_body, sign_payload
from ..container import Container
from ..persistence.db import new_id
from ..persistence.models import ProviderEvent
from .deps import get_container, get_session

router = APIRouter(prefix="/v1/provider-events", tags=["provider-events"])


@router.post("/lenders", status_code=202)
async def lender_event(
    request: Request,
    x_lender_id: str = Header(default=""),
    x_signature: str = Header(default=""),
    session: Session = Depends(get_session),
    container: Container = Depends(get_container),
) -> Dict[str, Any]:
    payload = await request.json()
    provider = payload.get("provider") or x_lender_id
    secret = container.network.webhook_secret(provider)
    if secret is None:
        raise HTTPException(status_code=401, detail="unknown provider")
    body = canonical_body({k: v for k, v in payload.items() if k not in ("signature", "deliver_at", "delivered")})
    expected = sign_payload(secret, body)
    if not x_signature or not hmac.compare_digest(expected, x_signature):
        raise HTTPException(status_code=401, detail="invalid signature")
    event_id = str(payload.get("id"))
    existing = session.execute(select(ProviderEvent).where(ProviderEvent.provider == provider, ProviderEvent.provider_event_id == event_id)).scalar_one_or_none()
    if existing is not None:
        return {"status": "duplicate", "provider_event_id": existing.id}
    evt = ProviderEvent(
        id=new_id("pevt"),
        provider=provider,
        provider_event_id=event_id,
        event_type=str(payload.get("type")),
        payload_json=payload,
        status="received",
        received_at=container.clock.now(),
    )
    session.add(evt)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return {"status": "duplicate"}
    container.service.enqueue(session, "process_provider_event", None, {"provider_event_id": evt.id}, dedupe_key=f"pevt:{evt.id}")
    return {"status": "accepted", "provider_event_id": evt.id}
