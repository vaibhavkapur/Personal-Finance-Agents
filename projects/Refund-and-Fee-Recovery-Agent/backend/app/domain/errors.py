from __future__ import annotations


class DomainError(Exception):
    """Base for deterministic domain failures."""

    status_code = 422

    def __init__(self, message: str, *, code: str = "domain_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class NotFound(DomainError):
    status_code = 404

    def __init__(self, message: str) -> None:
        super().__init__(message, code="not_found")


class Forbidden(DomainError):
    status_code = 403

    def __init__(self, message: str = "forbidden") -> None:
        super().__init__(message, code="forbidden")


class StaleVersion(DomainError):
    status_code = 409

    def __init__(self, message: str) -> None:
        super().__init__(message, code="stale_version")


class Conflict(DomainError):
    status_code = 409

    def __init__(self, message: str, *, code: str = "conflict") -> None:
        super().__init__(message, code=code)


class IllegalTransition(DomainError):
    status_code = 409

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"Illegal transition {current} -> {target}", code="illegal_transition")
        self.current = current
        self.target = target


class PolicyViolation(DomainError):
    status_code = 422

    def __init__(self, message: str, *, code: str = "policy_violation") -> None:
        super().__init__(message, code=code)
