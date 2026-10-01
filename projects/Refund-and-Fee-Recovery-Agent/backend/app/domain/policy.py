"""Policy and authority checks that run in ordinary application code.

The model never decides these. Documents and provider messages are untrusted
input: recipients come from the verified contact registry and the claim reason
is the case's recorded reason code, never text found inside evidence.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

from ..clock import parse_ts
from .errors import PolicyViolation
from .models import (
    Action,
    ActionStatus,
    ActionType,
    CaseStatus,
    ChannelStatus,
    ChannelType,
    Merchant,
    ReasonCode,
    RecoveryCase,
    RecoveryChannel,
    SUPPORTED_REASON_CODES,
)
from .state_machine import is_terminal
from .totals import Totals

TRUSTED_CONTACT_SOURCES = {"merchant_registry", "issuer_registry"}
# Actions that are still in flight. A `submitted` action is finished; whether the
# channel is still open is tracked on the channel itself.
OPEN_ACTION_STATUSES = {ActionStatus.awaiting_approval, ActionStatus.approved, ActionStatus.submitting, ActionStatus.unknown}


def verified_recipient(merchant: Merchant) -> dict:
    """The only allowed source of a merchant recipient. Ignores anything in documents."""
    if merchant.contact_source not in TRUSTED_CONTACT_SOURCES or not merchant.contact_verified_at:
        raise PolicyViolation(f"Merchant {merchant.id} has no verified contact record", code="unverified_contact")
    return {
        "recipient_ref": f"merchant:{merchant.id}",
        "address": merchant.support_email,
        "channel": merchant.support_channel,
        "verified_at": merchant.contact_verified_at,
        "source": merchant.contact_source,
    }


def check_before_merchant_request(
    *,
    case: RecoveryCase,
    totals: Totals,
    merchant: Merchant,
    channels: Iterable[RecoveryChannel],
    actions: Iterable[Action],
    max_followups: int,
    is_followup: bool = False,
) -> List[str]:
    """Raise PolicyViolation when an outbound merchant request is not allowed.
    Returns a list of informational findings otherwise."""
    findings: List[str] = []
    if case.reason_code not in SUPPORTED_REASON_CODES:
        raise PolicyViolation(f"Reason code {case.reason_code.value} is not supported in this release", code="not_supported")
    if is_terminal(case.status):
        raise PolicyViolation(f"Case is closed ({case.status.value}); no further contact allowed", code="case_closed")
    if case.status == CaseStatus.manual_review:
        raise PolicyViolation("Case is held for manual review", code="manual_review_hold")
    if totals.outstanding_minor <= 0:
        raise PolicyViolation("Nothing outstanding: matched final credits already cover the target", code="nothing_outstanding")
    if totals.overlap_flagged:
        raise PolicyViolation("Possible duplicate recovery flagged; hold for review", code="overlap_hold")
    verified_recipient(merchant)
    merchant_channels = [c for c in channels if c.channel_type == ChannelType.merchant]
    for a in actions:
        if a.type in (ActionType.send_merchant_message, ActionType.send_merchant_followup) and a.status in OPEN_ACTION_STATUSES:
            raise PolicyViolation(f"An outbound merchant action is already open ({a.id}, {a.status.value})", code="duplicate_request")
    if is_followup:
        ch = merchant_channels[0] if merchant_channels else None
        if ch is None or ch.status not in (ChannelStatus.pending, ChannelStatus.promised):
            raise PolicyViolation("No open merchant channel to follow up on", code="no_open_channel")
        if ch.followup_count >= max_followups:
            raise PolicyViolation(f"Follow-up cap reached ({max_followups})", code="followup_cap")
    else:
        for ch in merchant_channels:
            if ch.status in (ChannelStatus.pending, ChannelStatus.promised):
                raise PolicyViolation(f"Merchant channel {ch.id} already open with reference {ch.provider_case_ref}", code="duplicate_channel")
    if case.pending_question is not None:
        findings.append("customer_question_pending")
    return findings


def check_dispute_eligibility(
    *,
    case: RecoveryCase,
    totals: Totals,
    channels: Iterable[RecoveryChannel],
    actions: Iterable[Action],
    now: datetime,
    deadline_at: Optional[str],
    min_merchant_wait_days: int,
) -> List[str]:
    """Issuer disputes are a separate lane with their own checks. The reason code
    used in the packet is always the case reason; nothing here can escalate a
    missing refund into an unauthorized-charge claim."""
    findings: List[str] = []
    if case.reason_code not in SUPPORTED_REASON_CODES:
        raise PolicyViolation("Dispute lane not configured for this reason code", code="not_supported")
    if is_terminal(case.status):
        raise PolicyViolation("Case is closed", code="case_closed")
    if totals.outstanding_minor <= 0:
        raise PolicyViolation("Nothing outstanding", code="nothing_outstanding")
    if totals.overlap_flagged:
        raise PolicyViolation("Possible duplicate recovery flagged", code="overlap_hold")
    for a in actions:
        if a.type == ActionType.submit_issuer_dispute and a.status in OPEN_ACTION_STATUSES:
            raise PolicyViolation(f"An issuer dispute action is already open ({a.id})", code="duplicate_request")
    for ch in channels:
        if ch.channel_type == ChannelType.issuer and ch.status in (ChannelStatus.pending, ChannelStatus.provisional, ChannelStatus.reversed):
            raise PolicyViolation("Issuer channel already open", code="duplicate_channel")
    merchant = [c for c in channels if c.channel_type == ChannelType.merchant]
    if not merchant:
        raise PolicyViolation("Merchant follow-up must be attempted before an issuer dispute in this release", code="merchant_first")
    ch = merchant[0]
    eligible = False
    if ch.status == ChannelStatus.declined:
        eligible = True
        findings.append("merchant_declined")
    elif ch.last_provider_status in ("refund_issued", "completed") and totals.final_recovered_minor < totals.target_minor:
        eligible = True
        findings.append("merchant_claims_completed_but_no_credit_posted" if totals.final_recovered_minor == 0 else "merchant_refund_posted_but_outstanding_remains")
    elif ch.last_contact_at and (now - parse_ts(ch.last_contact_at)).days >= min_merchant_wait_days:
        eligible = True
        findings.append("merchant_unresponsive_beyond_wait")
    if not eligible:
        raise PolicyViolation("Merchant channel has not yet met the configured dispute-eligibility conditions", code="not_yet_eligible")
    if deadline_at and parse_ts(deadline_at) < now:
        raise PolicyViolation("Configured dispute deadline (fixture) has passed", code="deadline_passed")
    return findings


def dispute_reason_for(case: RecoveryCase) -> str:
    """Map the case reason to the issuer reason code. Deliberately a fixed table."""
    table = {
        ReasonCode.promised_refund_missing: "credit_not_processed",
        ReasonCode.duplicate_billing: "duplicate_processing",
        ReasonCode.unauthorized_transaction: "fraud_card_absent",
        ReasonCode.goodwill_fee_waiver: "not_a_dispute_goodwill_request",
    }
    return table[case.reason_code]
