from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(StrictModel):
    password: str = Field(min_length=1, max_length=200)


class Evaluate(StrictModel):
    symbols: list[str] | None = Field(default=None, max_length=5)
    mandate_id: str | None = None


class Preview(StrictModel):
    signal_id: str


class Prepare(StrictModel):
    signal_id: str
    preview_id: str
    scenario: Literal["full", "partial", "resting", "rejected", "timeout", "cancel_race", "malformed", "delayed"] = "partial"


class Approval(StrictModel):
    expected_case_version: int = Field(ge=1)
    action_payload_hash: str
    approval_challenge_id: str


class Limits(StrictModel):
    per_order_minor: int = Field(ge=101, le=10000000)
    per_symbol_minor: int = Field(ge=101, le=100000000)
    aggregate_minor: int = Field(ge=101, le=100000000)
    daily_turnover_minor: int = Field(ge=101, le=100000000)
    max_quote_age_seconds: int = Field(ge=1, le=300)
    limit_tolerance_bps: int = Field(ge=0, le=100)


class Parameters(StrictModel):
    lookback: Literal[5] = 5
    target_order_minor: int = Field(ge=100, le=10000000)


class Mandate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    symbols: list[str] = Field(min_length=1, max_length=5)
    parameters: Parameters
    limits: Limits
    expires_at: str


class Clock(StrictModel):
    seconds: int = Field(ge=1, le=86400)
    refresh_quotes: bool = True


class Switch(StrictModel):
    enabled: bool


class Run(StrictModel):
    customer_id: Literal["cus_demo_6"]
    mandate_id: str
    market_snapshot_id: str
    environment: Literal["mock"]


class AgentRequest(StrictModel):
    symbol: str = Field(min_length=1, max_length=10)
    max_tool_calls: int = Field(default=4, ge=1, le=6)
