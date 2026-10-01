"""Case lifecycle. Allowed transitions are enforced server-side.

    detected -> investigating -> awaiting_approval -> merchant_pending
    merchant_pending -> refund_promised -> credit_pending -> recovered
    merchant_pending -> issuer_review -> awaiting_approval -> issuer_pending
    issuer_pending -> provisional_credit -> final_credit -> recovered
    provisional_credit -> credit_reversed -> issuer_pending
    merchant_pending | issuer_pending -> unresolved
    investigating -> already_refunded | not_supported

`manual_review` is a non-terminal operational hold reachable from any active
state; it is never a successful outcome.
"""
from __future__ import annotations

from typing import Dict, Set

from .errors import IllegalTransition
from .models import CaseStatus, TERMINAL_STATES

S = CaseStatus

ALLOWED: Dict[CaseStatus, Set[CaseStatus]] = {
    S.detected: {S.investigating},
    S.investigating: {S.awaiting_approval, S.already_refunded, S.not_supported, S.credit_pending, S.unresolved},
    S.awaiting_approval: {S.merchant_pending, S.issuer_pending, S.investigating, S.issuer_review, S.credit_pending},
    S.merchant_pending: {S.refund_promised, S.credit_pending, S.issuer_review, S.unresolved},
    S.refund_promised: {S.credit_pending, S.issuer_review, S.unresolved},
    S.credit_pending: {S.recovered, S.issuer_review, S.unresolved, S.awaiting_approval},
    S.issuer_review: {S.awaiting_approval, S.unresolved, S.credit_pending, S.recovered},
    S.issuer_pending: {S.provisional_credit, S.final_credit, S.unresolved, S.credit_pending},
    S.provisional_credit: {S.final_credit, S.credit_reversed, S.unresolved},
    S.credit_reversed: {S.issuer_pending, S.unresolved},
    S.final_credit: {S.recovered},
    S.manual_review: {S.investigating, S.merchant_pending, S.credit_pending, S.issuer_pending, S.issuer_review, S.provisional_credit, S.unresolved, S.recovered},
    S.recovered: set(),
    S.unresolved: set(),
    S.already_refunded: set(),
    S.not_supported: set(),
}

ACTIVE_STATES = {s for s in CaseStatus if s not in TERMINAL_STATES}


def can_transition(current: CaseStatus, target: CaseStatus) -> bool:
    if target == CaseStatus.manual_review:
        return current in ACTIVE_STATES and current != CaseStatus.manual_review
    return target in ALLOWED.get(current, set())


def assert_transition(current: CaseStatus, target: CaseStatus) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(str(current.value), str(target.value))


def is_terminal(status: CaseStatus) -> bool:
    return status in TERMINAL_STATES
