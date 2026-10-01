from __future__ import annotations


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, code: str = "domain_error"):
        super().__init__(message)
        self.message = message
        self.code = code


class NotFound(DomainError):
    status_code = 404

    def __init__(self, message: str):
        super().__init__(message, "not_found")


class Forbidden(DomainError):
    status_code = 403

    def __init__(self, message: str):
        super().__init__(message, "forbidden")


class Conflict(DomainError):
    status_code = 409

    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message, code)


class Invalid(DomainError):
    status_code = 422

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message, code)
