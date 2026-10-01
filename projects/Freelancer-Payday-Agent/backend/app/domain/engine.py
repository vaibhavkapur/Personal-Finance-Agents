"""Pure integer-cent accounting. No invoices or prose can authorize a payout."""
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json


class DomainError(Exception):
    def __init__(self, message, status=409):
        self.message, self.status = message, status
        super().__init__(message)


def digest(value):
    return "sha256:" + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tax_target(receipts, fraction):
    """Net confirmed earned receipts; linked refunds reduce their original allocation.

    Recompute a target, never add the same receipt's allocation a second time.
    Own transfers and unconfirmed receipts never accrue tax allocations.
    """
    eligible = {r["id"]: r["amount_minor"] for r in receipts
                if r["category"] == "income" and r["confirmed"]}
    for r in receipts:
        if r["category"] == "refund" and r["confirmed"] and r.get("reverses_receipt_id") in eligible:
            key = r["reverses_receipt_id"]
            eligible[key] = max(0, eligible[key] + r["amount_minor"])
    allocations = {key: int((Decimal(amount) * Decimal(fraction)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                   for key, amount in eligible.items()}
    return sum(allocations.values()), allocations


def calculate(available, tax, operating, emergency, reservations, requested):
    assert all(isinstance(v, int) and v >= 0 for v in
               [available, tax, operating, emergency, reservations, requested])
    protected = tax + operating + emergency
    capacity = max(0, available - protected - reservations)
    feasible = min(requested, capacity)
    return {"currency": "USD", "available_minor": available, "capacity_minor": capacity,
            "requested_minor": requested, "feasible_minor": feasible,
            "shortfall_minor": requested - feasible, "remaining_minor": available - feasible,
            "extra_after_payout_minor": max(0, capacity - feasible),
            "reserve_deficit_minor": max(0, protected + reservations - available),
            "protected": {"tax": tax, "operating": operating, "emergency": emergency},
            "reservations_minor": reservations}


def allocate(available, targets, reserved):
    if reserved > available:
        raise DomainError("Pending transfer exceeds bank cash; reconciliation needs review.")
    result = {"reserved": reserved}
    remaining = available - reserved
    for name in ("tax", "operating", "emergency"):
        result[name] = min(targets[name], remaining)
        remaining -= result[name]
    result["unallocated"] = remaining
    assert sum(result.values()) == available
    return result


ALLOWED = {
    "collecting": {"cash_reconciled", "manual_review"},
    "cash_reconciled": {"planning", "manual_review"},
    "planning": {"shortfall_review", "proposal_ready", "manual_review"},
    "shortfall_review": {"proposal_ready", "planning"},
    "proposal_ready": {"awaiting_approval", "planning", "stale_inputs"},
    "awaiting_approval": {"reserved", "expired", "cancelled", "stale_inputs"},
    "reserved": {"submitted", "stale_inputs", "manual_review"},
    "submitted": {"outcome_unknown", "posted", "failed", "manual_review"},
    "outcome_unknown": {"posted", "failed", "manual_review"},
    "posted": {"reconciled", "returned", "manual_review"},
    "reconciled": {"returned"},
    "returned": {"recovery_review"},
    "recovery_review": {"planning"},
    "stale_inputs": {"planning", "manual_review"},
    "expired": {"planning"}, "cancelled": {"planning"}, "failed": {"planning"},
    "manual_review": {"planning", "submitted", "posted", "failed"},
}
