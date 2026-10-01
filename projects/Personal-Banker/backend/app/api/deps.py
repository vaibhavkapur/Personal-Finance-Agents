"""Request dependencies.

Authentication is a demo stand-in: the authenticated customer comes from the
``X-Customer-Id`` header and operators from ``X-Role: operator``. A real
deployment replaces this with the identity provider's session; nothing else
in the application reads raw headers.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.persistence.db import session_factory


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str  # customer | operator


def get_principal(
    x_customer_id: str | None = Header(default=None, alias="X-Customer-Id"),
    x_role: str | None = Header(default=None, alias="X-Role"),
) -> Principal:
    if not x_customer_id:
        raise HTTPException(status_code=401, detail={"error": "unauthenticated", "message": "X-Customer-Id header required (demo session)"})
    role = "operator" if x_role == "operator" else "customer"
    return Principal(subject=x_customer_id, role=role)


def require_operator(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role != "operator":
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "operator role required"})
    return principal


def get_session() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
