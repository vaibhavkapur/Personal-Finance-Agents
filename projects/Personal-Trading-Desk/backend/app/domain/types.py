"""All money is USD cents; quantities are whole shares in this first release."""
from datetime import datetime, timezone
import hashlib
import json
import uuid


class DomainError(Exception):
    def __init__(self, message: str, status: int = 422, reasons=None):
        self.message, self.status, self.reasons = message, status, reasons or []
        super().__init__(message)


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise DomainError("Timestamp must include a timezone")
    return dt.astimezone(timezone.utc)


def wall_time():
    return datetime.now(timezone.utc).isoformat()


TERMINAL = {"filled", "cancelled", "rejected", "expired", "blocked"}
TRANSITIONS = {
    "awaiting_approval": {"submitting", "expired", "cancelled", "blocked"},
    "submitting": {"accepted", "partially_filled", "filled", "rejected", "outcome_unknown", "manual_review", "cancelled"},
    "outcome_unknown": {"accepted", "partially_filled", "filled", "rejected", "cancelled", "manual_review"},
    "accepted": {"partially_filled", "filled", "cancel_requested", "cancelled", "manual_review"},
    "partially_filled": {"filled", "cancel_requested", "cancelled", "manual_review"},
    "cancel_requested": {"partially_filled", "filled", "cancelled", "manual_review"},
    "manual_review": {"accepted", "partially_filled", "filled", "rejected", "cancelled"},
}


def transition(order, state):
    if state == order["status"]:
        return False
    if state not in TRANSITIONS.get(order["status"], set()):
        raise DomainError(f"Invalid transition: {order['status']} → {state}", 409)
    order["status"] = state
    order["version"] += 1
    return True
