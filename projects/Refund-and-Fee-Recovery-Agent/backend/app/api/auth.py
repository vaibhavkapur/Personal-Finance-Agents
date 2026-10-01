"""Session resolution from fixture bearer tokens.

The approver of any action is always taken from the authenticated session,
never from the request body. Replace this module with a real identity
provider before any non-mock deployment.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from fastapi import Depends, Header, HTTPException, Request

from ..config import FIXTURES_DIR


@dataclass(frozen=True)
class Session:
    subject: str
    role: str  # customer | operator
    customer_id: Optional[str]
    tenant_id: str


class SessionStore:
    def __init__(self, path: Path = FIXTURES_DIR / "sessions.json") -> None:
        raw = json.loads(path.read_text())["tokens"]
        self._tokens: Dict[str, Session] = {tok: Session(**v) for tok, v in raw.items()}

    def resolve(self, token: str) -> Optional[Session]:
        return self._tokens.get(token)


def get_session(request: Request, authorization: Optional[str] = Header(default=None)) -> Session:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthenticated", "message": "Bearer token required"})
    store: SessionStore = request.app.state.sessions
    session = store.resolve(authorization.split(" ", 1)[1].strip())
    if session is None:
        raise HTTPException(status_code=401, detail={"code": "unauthenticated", "message": "unknown token"})
    return session


def require_customer(session: Session = Depends(get_session)) -> Session:
    if session.role != "customer" or not session.customer_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "customer session required"})
    return session


def require_operator(session: Session = Depends(get_session)) -> Session:
    if session.role != "operator":
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "operator session required"})
    return session
