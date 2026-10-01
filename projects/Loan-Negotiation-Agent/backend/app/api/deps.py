"""FastAPI dependencies: container access, authentication and error mapping."""
from __future__ import annotations

import hashlib
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..container import Container
from ..persistence.models import Customer


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_session(container: Container = Depends(get_container)) -> Iterator[Session]:
    session = container.db.open_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _bearer(authorization: str) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def current_customer(authorization: str = Header(default=""), session: Session = Depends(get_session)) -> Customer:
    token = _bearer(authorization)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    customer = session.execute(select(Customer).where(Customer.api_token_hash == digest)).scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return customer


def require_operator(authorization: str = Header(default=""), container: Container = Depends(get_container)) -> str:
    token = _bearer(authorization)
    if token != container.settings.operator_token:
        raise HTTPException(status_code=403, detail="operator token required")
    return "operator"
