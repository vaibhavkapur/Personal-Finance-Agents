"""Inbound insurer callbacks (signed, deduplicated, never trusted for state)."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request

from ..context import AppContext
from ..workflows.case_service import CaseError
from ..workflows.events import ProviderEventService
from .deps import get_ctx

router = APIRouter(prefix="/v1/provider-events", tags=["provider-events"])


@router.post("/insurer", status_code=202)
async def insurer_event(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    x_insurer_id: Optional[str] = Header(default=None),
    x_insurer_signature: Optional[str] = Header(default=None),
    x_insurer_timestamp: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    body = await request.body()
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise CaseError("body must be JSON", 400)
    provider = x_insurer_id or payload.get("provider")
    if not provider:
        raise CaseError("X-Insurer-Id header is required", 422)
    return ProviderEventService(ctx).receive(provider, body, payload, x_insurer_signature, x_insurer_timestamp)
