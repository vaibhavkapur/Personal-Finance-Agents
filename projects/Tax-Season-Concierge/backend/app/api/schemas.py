from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

Money = Annotated[StrictInt, Field(ge=0, le=100_000_000)]

class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Versioned(Model):
    expected_case_version: int = Field(ge=1)

class Profile(Model):
    tax_year: int = 2025
    jurisdiction: str = "US_FEDERAL"
    filing_status: str = "single"
    age_at_year_end: int | None = Field(default=None, ge=0, le=120)
    answers: dict[str, StrictBool | None] = Field(default_factory=dict)
    expected_w2_issuers: list[str] = Field(default_factory=list, max_length=20)
    expected_interest_issuers: list[str] = Field(default_factory=list, max_length=20)

class CreateCase(Model):
    customer_id: Literal["cus_demo_10"] = "cus_demo_10"
    jurisdiction: str = "US_FEDERAL"
    tax_year: int = 2025
    filing_status: str = "single"
    scenario: Literal["two_jobs", "missing_interest", "corrected_form", "balance_due", "rejection", "timeout", "malformed", "delayed"] = "two_jobs"

class ProfileRequest(Versioned):
    profile: Profile

class Form(Model):
    form_type: Literal["W-2", "W-2c", "1099-INT"]
    issuer_ref: str = Field(pattern=r"^fixture_[a-z0-9_]+$", max_length=80)
    issuer_name: str = Field(min_length=1, max_length=80)
    taxpayer_ref: str = Field(default="SYNTHETIC-ALEX-2025", pattern=r"^SYNTHETIC-[A-Z0-9-]+$", max_length=80)
    tax_year: int = 2025
    currency: Literal["USD"] = "USD"
    wages_minor: Money | None = None
    federal_withholding_minor: Money | None = None
    interest_minor: Money | None = None
    social_security_minor: Money = 0
    medicare_minor: Money = 0
    unsupported_flags: list[str] = Field(default_factory=list, max_length=20)
    supersedes_form_id: str | None = None
    confirmed: StrictBool = False
    synthetic: Literal[True] = True
    @model_validator(mode="after")
    def required_fields(self):
        if self.form_type == "W-2" and (self.wages_minor is None or self.federal_withholding_minor is None):
            raise ValueError("W-2 requires boxes 1 and 2, including explicit zero amounts")
        if self.form_type == "1099-INT" and (self.interest_minor is None or self.federal_withholding_minor is None):
            raise ValueError("1099-INT requires boxes 1 and 4, including explicit zeros")
        if self.form_type == "W-2c" and (not self.supersedes_form_id or (self.wages_minor is None and self.federal_withholding_minor is None)):
            raise ValueError("W-2c needs an original form reference and at least one corrected field")
        if self.form_type != "1099-INT" and self.interest_minor is not None:
            raise ValueError("Interest is not a supported W-2 field")
        if self.form_type == "1099-INT" and (self.wages_minor is not None or self.supersedes_form_id):
            raise ValueError("Corrected interest statements require manual review")
        return self

class FormRequest(Versioned):
    form: Form

class ReconcileRequest(Versioned):
    completeness_confirmed: StrictBool

class CalculationRequest(Versioned):
    rule_pack_id: str
    facts_hash: str

class ApprovalRequest(Versioned):
    action_payload_hash: str
    approval_challenge_id: str

class ClockRequest(Versioned):
    days: int = Field(ge=1, le=30)

class ProviderEvent(Model):
    id: str = Field(max_length=100)
    case_id: str
    submission_ref: str
    type: Literal["accepted", "rejected", "refund_notice", "account_credit"]
    environment: Literal["mock"]
    amount_minor: Money = 0
    account_event_ref: str | None = None
    code: str | None = None
