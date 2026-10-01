"""Inbound provider callbacks. Signed with the provider's scheme (here HMAC
over the canonical JSON body) and deduplicated by provider + event id."""

from __future__ import annotations

from fastapi import APIRouter, Header

from app.api.schemas import ProviderEvent
from app.workflows.simulation import deliver_provider_event

router = APIRouter(prefix="/v1/provider-events", tags=["provider-events"])


@router.post("/bank")
def bank_event(
    body: ProviderEvent,
    x_provider_id: str = Header(default="bank_harbor", alias="X-Provider-Id"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
):
    return deliver_provider_event(x_provider_id, body.model_dump(), x_signature)
