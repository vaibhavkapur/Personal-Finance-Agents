from __future__ import annotations

import hashlib
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select

from ..context import AppContext
from ..persistence.models import Principal
from ..workflows.approvals import PrincipalView


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_principal(authorization: Optional[str] = Header(default=None), ctx: AppContext = Depends(get_ctx)) -> PrincipalView:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthenticated", "message": "Bearer token required"})
    token = authorization.split(" ", 1)[1].strip()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with ctx.db.session() as s:
        row = s.scalars(select(Principal).where(Principal.token_hash == digest)).first()
        if not row:
            raise HTTPException(status_code=401, detail={"code": "unauthenticated", "message": "unknown token"})
        return PrincipalView(id=row.id, role=row.role, customer_id=row.customer_id, display_name=row.display_name)


def require_operator(principal: PrincipalView = Depends(get_principal)) -> PrincipalView:
    if principal.role != "operator":
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "operator role required"})
    return principal
