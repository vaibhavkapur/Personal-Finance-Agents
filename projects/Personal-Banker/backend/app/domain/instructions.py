"""Bank instruction payloads: typed, canonical and hashable.

An approval binds to ``payload_hash``; any change to a material field yields a
different hash and therefore requires fresh approval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

INSTRUCTION_TYPES = {"cd_renewal", "same_owner_transfer"}


@dataclass(frozen=True)
class InstructionPayload:
    instruction_type: str
    customer_id: str
    source_account_id: str
    destination_account_id: str | None
    amount_minor: int
    currency: str
    effective_at: date
    offer_id: str
    provider_id: str
    product_code: str
    product_version: str
    apy_decimal: str
    term_days: int | None
    fees_minor: int
    deposit_contract_id: str
    contract_version: str
    irreversible_effect: str

    def to_dict(self) -> dict:
        return {
            "instruction_type": self.instruction_type,
            "customer_id": self.customer_id,
            "source_account_id": self.source_account_id,
            "destination_account_id": self.destination_account_id,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "effective_at": self.effective_at.isoformat(),
            "offer_id": self.offer_id,
            "provider_id": self.provider_id,
            "product_code": self.product_code,
            "product_version": self.product_version,
            "apy_decimal": self.apy_decimal,
            "term_days": self.term_days,
            "fees_minor": self.fees_minor,
            "deposit_contract_id": self.deposit_contract_id,
            "contract_version": self.contract_version,
            "irreversible_effect": self.irreversible_effect,
        }

    @staticmethod
    def from_dict(data: dict) -> InstructionPayload:
        return InstructionPayload(
            instruction_type=data["instruction_type"],
            customer_id=data["customer_id"],
            source_account_id=data["source_account_id"],
            destination_account_id=data.get("destination_account_id"),
            amount_minor=int(data["amount_minor"]),
            currency=data["currency"],
            effective_at=date.fromisoformat(data["effective_at"]),
            offer_id=data["offer_id"],
            provider_id=data["provider_id"],
            product_code=data["product_code"],
            product_version=str(data["product_version"]),
            apy_decimal=str(data["apy_decimal"]),
            term_days=data.get("term_days"),
            fees_minor=int(data.get("fees_minor") or 0),
            deposit_contract_id=data["deposit_contract_id"],
            contract_version=str(data["contract_version"]),
            irreversible_effect=data["irreversible_effect"],
        )


def canonical_json(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(payload: InstructionPayload | dict) -> str:
    data = payload.to_dict() if isinstance(payload, InstructionPayload) else payload
    return "sha256:" + hashlib.sha256(canonical_json(data).encode()).hexdigest()


def instruction_type_for_offer_kind(offer_kind: str) -> str:
    if offer_kind in ("cd_renewal", "cd_new"):
        return "cd_renewal"
    if offer_kind == "savings_transfer":
        return "same_owner_transfer"
    raise ValueError(f"unsupported offer kind {offer_kind!r}")


def irreversible_effect_text(instruction_type: str, term_days: int | None, penalty: str | None) -> str:
    if instruction_type == "cd_renewal":
        return (
            f"Funds are locked for {term_days} days from the effective date. "
            f"Early withdrawal penalty: {penalty or 'not recorded'}."
        )
    return "Funds move between your verified accounts on the effective date. Reversal requires a new transfer."
