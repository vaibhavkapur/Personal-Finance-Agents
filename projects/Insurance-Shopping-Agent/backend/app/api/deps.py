"""Authentication and shared dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from ..context import AppContext
from ..fixtures import customer_tokens


@dataclass
class Principal:
    kind: str  # customer | operator
    id: str


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _principal_from_token(ctx: AppContext, token: Optional[str]) -> Principal:
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    customer_id = customer_tokens().get(token)
    if customer_id:
        return Principal("customer", customer_id)
    operator_id = ctx.settings.operator_tokens.get(token)
    if operator_id:
        return Principal("operator", operator_id)
    raise HTTPException(status_code=401, detail="invalid token")


def current_principal(authorization: Optional[str] = Header(default=None), ctx: AppContext = Depends(get_ctx)) -> Principal:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    return _principal_from_token(ctx, token)


def current_customer(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.kind != "customer":
        raise HTTPException(status_code=403, detail="customer session required")
    return principal


def current_operator(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.kind != "operator":
        raise HTTPException(status_code=403, detail="operator session required")
    return principal
