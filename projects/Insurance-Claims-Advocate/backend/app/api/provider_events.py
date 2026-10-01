"""Provider callbacks (claims and payments). Signed by the provider, verified here, deduplicated in the inbox."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request

from ..context import AppContext
from .deps import get_ctx

router = APIRouter(prefix="/v1/provider-events", tags=["provider-events"])


@router.post("/claims")
async def claims_event(request: Request, x_mock_signature: Optional[str] = Header(default=None), ctx: AppContext = Depends(get_ctx)):
    payload: Dict[str, Any] = await request.json()
    return ctx.events.handle_claim_event(payload, x_mock_signature)


@router.post("/payments")
async def payments_event(request: Request, x_mock_signature: Optional[str] = Header(default=None), ctx: AppContext = Depends(get_ctx)):
    payload: Dict[str, Any] = await request.json()
    return ctx.events.handle_payment_event(payload, x_mock_signature)
