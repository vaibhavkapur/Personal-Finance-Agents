from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewCase(StrictModel):
    customer_id: str = "cus_demo_12"
    policy_id: str = "reserve_policy_demo_1"
    requested_payout_minor: int = Field(default=300000, ge=1, le=100000000, strict=True)
    currency: Literal["USD"] = "USD"
    period: str = Field(default="2026-10", pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class Draft(StrictModel):
    proposal_id: str
    simulation_mode: Literal["normal", "delay", "timeout", "decline", "malformed"] = "normal"


class Approval(StrictModel):
    expected_case_version: int
    action_payload_hash: str
    approval_challenge_id: str


class PolicyApproval(StrictModel):
    action_payload_hash: str
    approval_challenge_id: str


class Policy(StrictModel):
    provisional_tax_fraction_decimal: Decimal = Field(ge=0, le=1, decimal_places=4)
    emergency_floor_minor: int = Field(ge=0, le=100000000, strict=True)
    planning_horizon_days: int = Field(ge=1, le=365, strict=True)


class Confirmation(StrictModel):
    transaction_id: str
    category: Literal["income", "transfer", "loan", "other"]


class Confirmations(StrictModel):
    confirmations: list[Confirmation] = Field(min_length=1, max_length=100)


class AgentMessage(StrictModel):
    message: str = Field(min_length=1, max_length=2000)
    tool_budget: int = Field(default=4, ge=1, le=8)


class Simulation(StrictModel):
    kind: Literal["advance", "settle", "return", "late_receipt", "ambiguous_receipt", "bill_change", "late_scenario"]
    seconds: int = Field(default=3600, ge=0, le=2678400)
    transfer_id: str = ""
    amount_minor: int = Field(default=0, ge=0, le=100000000, strict=True)
    reference: str = Field(default="", max_length=100)
