"""Pure approval checks. Persistence and state changes live in CaseService."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from ..clock import parse_ts
from ..domain.errors import PolicyViolation, StaleVersion
from ..domain.models import Action, ActionStatus, Approval, RecoveryCase


def check_approval_request(*, action: Action, approval: Approval, case: RecoveryCase, expected_case_version: int, action_payload_hash: str, approval_challenge_id: str, now: datetime) -> None:
    if action.status != ActionStatus.awaiting_approval:
        raise PolicyViolation(f"Action {action.id} is {action.status.value}, not awaiting approval", code="action_not_pending")
    if approval.revoked_at:
        raise PolicyViolation(f"Approval challenge revoked: {approval.revoke_reason}", code="approval_revoked")
    if approval.challenge_id != approval_challenge_id:
        raise PolicyViolation("Approval challenge does not match this action", code="challenge_mismatch")
    if parse_ts(approval.challenge_expires_at) < now:
        raise PolicyViolation("Approval challenge has expired; request a fresh draft", code="challenge_expired")
    if action_payload_hash != action.payload_hash or action_payload_hash != approval.action_hash:
        raise PolicyViolation("Action payload changed since the review screen was produced", code="payload_changed")
    if case.version != expected_case_version:
        raise StaleVersion(f"Case version is {case.version}, expected {expected_case_version}")


def verify_authority_before_side_effect(*, action: Action, approval: Optional[Approval], case_status: str, current_payload_hash: str) -> None:
    """Executor check immediately before the external write."""
    if approval is None or approval.approved_at is None:
        raise PolicyViolation("No approval bound to this action", code="no_approval")
    if approval.revoked_at:
        raise PolicyViolation(f"Approval revoked: {approval.revoke_reason}", code="approval_revoked")
    if approval.consumed_at and approval.consumed_by_action_id != action.id and not _scope_allows_reuse(approval.scope, action):
        raise PolicyViolation("Approval already consumed by another action", code="approval_consumed")
    if action.type.value == "send_merchant_followup":
        # Reminder under the original approval scope: recipient and attachments must be unchanged.
        scope = approval.scope
        if not scope.get("reminders_allowed", 0):
            raise PolicyViolation("Approval scope does not cover reminders", code="scope_exceeded")
        if action.payload.get("recipient", {}).get("recipient_ref") != scope.get("recipient_ref"):
            raise PolicyViolation("Reminder recipient differs from the approved recipient", code="recipient_changed")
        if action.payload.get("attachments_hash") != scope.get("attachments_hash"):
            raise PolicyViolation("Reminder attachments differ from the approved packet", code="attachments_changed")
    elif approval.action_hash != current_payload_hash:
        raise PolicyViolation("Payload hash differs from the approved hash", code="payload_changed")
    if case_status in ("recovered", "unresolved", "already_refunded", "not_supported", "manual_review"):
        raise PolicyViolation(f"Case state {case_status} does not permit external writes", code="case_state_blocks_write")


def _scope_allows_reuse(scope: Dict[str, Any], action: Action) -> bool:
    return action.type.value == "send_merchant_followup" and bool(scope.get("reminders_allowed", 0))
