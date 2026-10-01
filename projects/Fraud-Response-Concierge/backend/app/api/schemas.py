from __future__ import annotations
from typing import Literal
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

class Strict(BaseModel):
    model_config=ConfigDict(extra="forbid")
class Version(Strict):
    expected_case_version: int=Field(ge=1)
class Intake(Strict):
    customer_id: str="cus_demo_9"
    incident_type: Literal["lost_wallet"]="lost_wallet"
    discovered_at: datetime
    instrument_refs: list[str]=Field(min_length=1,max_length=2)
    unrecognized_transaction_refs: list[str]=Field(default_factory=list,max_length=2)
    narrative: str=Field(default="I lost my wallet and noticed purchases I do not recognize.",min_length=5,max_length=1000)
    @field_validator("discovered_at")
    @classmethod
    def timezone_required(cls,value):
        if value.tzinfo is None: raise ValueError("A timezone is required")
        return value
class Affected(Version):
    instrument_refs: list[str]=Field(min_length=1,max_length=2)
class Draft(Version):
    kind: Literal["lock","report_lost","replacement","report","response"]
    target_id: str
    statement: str | None=Field(default=None,min_length=3,max_length=1000)
class Approval(Version):
    action_payload_hash: str
    approval_challenge_id: str
class Confirmation(Version):
    choice: Literal["unauthorized","recognized"]
    statement: str=Field(min_length=3,max_length=1000)
class Note(Version):
    statement: str=Field(min_length=3,max_length=1000)
class ProviderEvent(Strict):
    event_id: str=Field(min_length=3,max_length=100)
    case_id: str
    customer_id: str
    provider_id: Literal["cedar","northstar"]
    instrument_id: str
    transaction_id: str | None=None
    provider_reference: str
    type: Literal["provisional_credit","resolved_customer_favor","resolved_other","credit_reversed","information_required","replacement_delivered"]
    revision: int=Field(ge=1)
    occurred_at: datetime
    environment: Literal["mock"]="mock"
class Simulation(Strict):
    type: Literal["provisional_credit","resolved_customer_favor","resolved_other","credit_reversed","information_required","replacement_delivered"]
    target_id: str
class ClockAdvance(Strict):
    minutes: int=Field(ge=1,le=43200)
class Scenario(Version):
    scenario: Literal["normal","timeout_after_acceptance","delayed","declined","malformed"]
class Chat(Strict):
    message: str=Field(min_length=1,max_length=1000)
