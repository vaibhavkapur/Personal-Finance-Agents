"""Reconciliation of a completed bank workflow against the approved instruction
(plan §5 and §14).

* transfer: both legs matched by reference, amount, currency and account;
* renewal: principal, term, rate/version and effective date matched.

A mismatch produces a ``ReconciliationResult`` with ``matched=False`` and the
case is held open. The reconciler never fabricates a receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class ReconciliationResult:
    matched: bool
    checks: list[dict] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    def add(self, name: str, expected, actual, ok: bool | None = None) -> None:
        if ok is None:
            ok = expected == actual
        self.checks.append({"check": name, "expected": expected, "actual": actual, "ok": ok})
        if not ok:
            self.matched = False

    def to_dict(self) -> dict:
        return {"matched": self.matched, "checks": self.checks, "evidence_refs": self.evidence_refs}

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]


def reconcile_transfer(
    instruction: dict,
    provider_record: dict,
    source_snapshot_before: dict,
    source_snapshot_after: dict,
    destination_snapshot_before: dict,
    destination_snapshot_after: dict,
) -> ReconciliationResult:
    result = ReconciliationResult(matched=True)
    result.add("provider_reference_present", True, bool(provider_record.get("provider_reference")))
    result.add("request_ref", instruction["request_ref"], provider_record.get("request_ref"))
    result.add("status_effective", "effective", provider_record.get("status"))
    result.add("currency", instruction["currency"], provider_record.get("currency"))
    result.add("amount_minor", instruction["amount_minor"], provider_record.get("credited_amount_minor"))
    result.add("source_account", instruction["source_account_id"], provider_record.get("source_account_id"))
    result.add(
        "destination_account", instruction["destination_account_id"], provider_record.get("destination_account_id")
    )
    result.add("effective_on", instruction["effective_at"], provider_record.get("effective_on"))
    # Ledger legs: destination gained the amount; source lost it (or, for a
    # maturity payout routed onward, the CD account is closed and proceeds
    # arrive at destination).
    dest_delta = destination_snapshot_after["current_minor"] - destination_snapshot_before["current_minor"]
    result.add("destination_credit_leg", instruction["amount_minor"], dest_delta)
    src_delta = source_snapshot_before["current_minor"] - source_snapshot_after["current_minor"]
    result.add("source_debit_leg_covers_amount", True, src_delta >= instruction["amount_minor"])
    for key in ("provider_reference", "request_ref"):
        if provider_record.get(key):
            result.evidence_refs.append(f"{key}:{provider_record[key]}")
    return result


def reconcile_renewal(instruction: dict, provider_record: dict, renewed_deposit: dict | None) -> ReconciliationResult:
    result = ReconciliationResult(matched=True)
    result.add("provider_reference_present", True, bool(provider_record.get("provider_reference")))
    result.add("request_ref", instruction["request_ref"], provider_record.get("request_ref"))
    result.add("status_effective", "effective", provider_record.get("status"))
    result.add("renewed_deposit_recorded", True, renewed_deposit is not None)
    if renewed_deposit is not None:
        result.add("principal_minor", instruction["amount_minor"], renewed_deposit.get("principal_minor"))
        result.add("currency", instruction["currency"], renewed_deposit.get("currency"))
        result.add("term_days", instruction["term_days"], renewed_deposit.get("term_days"))
        result.add("apy_decimal", str(instruction["apy_decimal"]), str(renewed_deposit.get("apy_decimal")))
        result.add("product_version", instruction["product_version"], renewed_deposit.get("product_version"))
        result.add("effective_on", instruction["effective_at"], renewed_deposit.get("opened_on"))
        expected_maturity = None
        if instruction.get("term_days"):
            from datetime import timedelta

            expected_maturity = (date.fromisoformat(instruction["effective_at"]) + timedelta(days=instruction["term_days"])).isoformat()
        result.add("maturity_date", expected_maturity, renewed_deposit.get("maturity_date"))
        if renewed_deposit.get("id"):
            result.evidence_refs.append(f"deposit:{renewed_deposit['id']}")
    for key in ("provider_reference", "request_ref"):
        if provider_record.get(key):
            result.evidence_refs.append(f"{key}:{provider_record[key]}")
    return result
