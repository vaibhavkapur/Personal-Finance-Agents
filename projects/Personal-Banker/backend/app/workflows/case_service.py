"""Case lifecycle service: create, collect, evaluate, prepare, approve.

All calculations, eligibility rules and state transitions live here and in
``app.domain``; the agent only calls into these functions through typed tools.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import settings
from app.domain.instructions import (
    InstructionPayload,
    instruction_type_for_offer_kind,
    irreversible_effect_text,
    payload_hash,
)
from app.domain.liquidity import (
    DatedFlow,
    ProjectionInputs,
    allocation_flow,
    effective_buffer,
    inputs_hash,
    max_lockable,
    project,
)
from app.domain.money import format_minor
from app.domain.offers import NormalizedOffer, compare_offers, material_terms, normalize_offer
from app.ids import new_id
from app.persistence import outbox
from app.persistence.db import session_scope
from app.persistence.models import (
    Action,
    Approval,
    ApprovalChallenge,
    BankAccount,
    BankInstruction,
    Case,
    CaseEvent,
    CashPlan,
    DepositContract,
    DepositOffer,
    Document,
    Job,
    Obligation,
    AdapterRequest,
)
from app.workflows import states
from app.workflows.errors import CaseError
from app.workflows.refresh import active_offers, offer_to_raw, refresh_offers, refresh_snapshots

LIQUID_KINDS = {"checking", "savings"}
COMPARISON_HORIZON_DAYS = 365
ACTIVE_ACTION_STATUSES = {"proposed", "approved", "submitting", "submitted", "outcome_unknown", "effective"}


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #


def get_case(session: Session, case_id: str, customer_id: str | None = None) -> Case:
    case = session.get(Case, case_id)
    if case is None or (customer_id is not None and case.customer_id != customer_id):
        # Do not reveal existence of other tenants' cases.
        raise CaseError(404, "case_not_found", f"case {case_id} not found")
    return case


def get_action(session: Session, action_id: str, customer_id: str | None = None) -> tuple[Action, Case]:
    action = session.get(Action, action_id)
    if action is None:
        raise CaseError(404, "action_not_found", f"action {action_id} not found")
    case = get_case(session, action.case_id, customer_id)
    return action, case


def _contract_for_case(session: Session, case: Case) -> tuple[DepositContract, BankAccount]:
    contract = session.get(DepositContract, case.deposit_id)
    if contract is None:
        raise CaseError(404, "deposit_not_found", f"deposit {case.deposit_id} not found")
    account = session.get(BankAccount, contract.account_id)
    if account is None:
        raise CaseError(404, "account_not_found", "deposit account not found")
    return contract, account


def current_instruction(session: Session, case: Case) -> BankInstruction | None:
    """The instruction bound to the case's current action, else the most recent
    one that was not superseded (several may share a simulated timestamp)."""
    if case.current_action_id:
        row = session.scalars(select(BankInstruction).where(BankInstruction.action_id == case.current_action_id)).first()
        if row is not None:
            return row
    rows = session.scalars(select(BankInstruction).where(BankInstruction.case_id == case.id).order_by(BankInstruction.created_at.desc())).all()
    live = [r for r in rows if r.status != "superseded"]
    return (live or rows or [None])[0]


def verified_accounts(session: Session, customer_id: str) -> list[BankAccount]:
    return list(
        session.scalars(
            select(BankAccount).where(
                BankAccount.customer_id == customer_id,
                BankAccount.ownership_verified.is_(True),
            )
        )
    )


# --------------------------------------------------------------------------- #
# Create / collect
# --------------------------------------------------------------------------- #


def compute_missing_fields(session: Session, case: Case) -> tuple[list[str], list[dict]]:
    """Information the records cannot supply (plan §7 step 2)."""
    missing: list[str] = []
    questions: list[dict] = []
    if case.preferred_lockup_days is None:
        missing.append("preferred_lockup_days")
        questions.append(
            {
                "field": "preferred_lockup_days",
                "question": "How long are you comfortable locking the remaining funds? (days; 0 for fully liquid)",
                "why": "Fixed-term offers differ in lock-up; the comparison flags terms beyond your preference.",
            }
        )
    obligations = case_obligations(session, case)
    _, account = _contract_for_case(session, case)
    contract = session.get(DepositContract, case.deposit_id)
    post = [o for o in obligations if contract is not None and o.due_date >= contract.maturity_date]
    post_total = sum(o.amount_minor for o in post)
    if case.buffer_includes_obligations is None and post_total > 0 and case.minimum_buffer_minor >= post_total:
        missing.append("buffer_includes_obligations")
        questions.append(
            {
                "field": "buffer_includes_obligations",
                "question": (
                    f"Your reserve of {format_minor(case.minimum_buffer_minor)} is at least the "
                    f"{format_minor(post_total)} of dated bills after maturity. Does the reserve already include those bills?"
                ),
                "why": "The engine must not subtract the same bills twice (plan §9).",
            }
        )
    return missing, questions


def case_obligations(session: Session, case: Case) -> list[Obligation]:
    """Union of user-selected obligations and confirmed obligations on record.
    Estimated obligations count only when the customer lists them."""
    rows = session.scalars(select(Obligation).where(Obligation.customer_id == case.customer_id)).all()
    selected = set(case.obligation_ids_json or [])
    result = [o for o in rows if o.id in selected or o.certainty == "confirmed"]
    result.sort(key=lambda o: (o.due_date, o.id))
    return result


def create_case(
    session: Session,
    *,
    customer_id: str,
    deposit_id: str,
    currency: str,
    minimum_buffer_minor: int,
    obligation_ids: list[str],
    preferred_lockup_days: int | None = None,
    buffer_includes_obligations: bool | None = None,
    concentration_limit_minor: int | None = None,
    provider_mode: str = "normal",
    actor: str | None = None,
) -> Case:
    if minimum_buffer_minor < 0:
        raise CaseError(422, "invalid_buffer", "minimum_buffer_minor must be non-negative")
    contract = session.get(DepositContract, deposit_id)
    if contract is None:
        raise CaseError(404, "deposit_not_found", f"deposit {deposit_id} not found")
    account = session.get(BankAccount, contract.account_id)
    if account is None or account.customer_id != customer_id:
        raise CaseError(403, "not_owner", "deposit does not belong to the authenticated customer")
    if not account.ownership_verified:
        raise CaseError(409, "ownership_unverified", "deposit account ownership is not verified")
    if account.access_revoked:
        raise CaseError(409, "access_revoked", "access to the deposit account has been revoked")
    if contract.currency != currency:
        raise CaseError(422, "currency_mismatch", f"deposit is denominated in {contract.currency}")
    for oid in obligation_ids:
        ob = session.get(Obligation, oid)
        if ob is None or ob.customer_id != customer_id:
            raise CaseError(403, "obligation_not_owner", f"obligation {oid} does not belong to the customer")
    # One open case per deposit: concurrent instructions must not spend the same funds.
    open_case = session.scalars(
        select(Case).where(Case.deposit_id == deposit_id, Case.state.notin_(list(states.TERMINAL)))
    ).first()
    if open_case is not None:
        raise CaseError(409, "case_already_open", f"deposit {deposit_id} already has open case {open_case.id}", {"case_id": open_case.id})

    now = clock.now(session)
    case = Case(
        id=new_id("bankcase"),
        customer_id=customer_id,
        workflow_type="cd_maturity",
        state=states.DISCOVERED,
        version=1,
        deposit_id=deposit_id,
        currency=currency,
        minimum_buffer_minor=minimum_buffer_minor,
        obligation_ids_json=list(obligation_ids),
        preferred_lockup_days=preferred_lockup_days,
        buffer_includes_obligations=buffer_includes_obligations,
        concentration_limit_minor=concentration_limit_minor,
        provider_mode=provider_mode,
        created_at=now,
        updated_at=now,
    )
    session.add(case)
    session.flush()
    states.record_event(session, case, "case.created", actor or customer_id, {"deposit_id": deposit_id, "maturity_date": contract.maturity_date.isoformat()}, next_state=states.DISCOVERED)
    states.transition(session, case, states.COLLECTING, actor or customer_id)
    missing, questions = compute_missing_fields(session, case)
    case.missing_fields_json = missing
    case.outstanding_questions_json = questions
    outbox.emit(session, "banking_case.created", case.id, {"deposit_id": deposit_id, "state": case.state})
    return case


def answer_questions(session: Session, case: Case, answers: dict, actor: str) -> Case:
    if case.state in states.TERMINAL:
        raise CaseError(409, "case_closed", "case is closed")
    if case.state not in (states.COLLECTING, states.NEEDS_INFORMATION, states.EVALUATING, states.NEEDS_REQUOTE):
        raise CaseError(409, "invalid_state", f"cannot change inputs while {case.state}")
    changed: dict = {}
    if "preferred_lockup_days" in answers and answers["preferred_lockup_days"] is not None:
        days = int(answers["preferred_lockup_days"])
        if days < 0:
            raise CaseError(422, "invalid_answer", "preferred_lockup_days must be >= 0")
        case.preferred_lockup_days = days
        changed["preferred_lockup_days"] = days
    if "buffer_includes_obligations" in answers and answers["buffer_includes_obligations"] is not None:
        case.buffer_includes_obligations = bool(answers["buffer_includes_obligations"])
        changed["buffer_includes_obligations"] = case.buffer_includes_obligations
    if "minimum_buffer_minor" in answers and answers["minimum_buffer_minor"] is not None:
        value = int(answers["minimum_buffer_minor"])
        if value < 0:
            raise CaseError(422, "invalid_answer", "minimum_buffer_minor must be >= 0")
        case.minimum_buffer_minor = value
        changed["minimum_buffer_minor"] = value
    if "concentration_limit_minor" in answers and answers["concentration_limit_minor"] is not None:
        case.concentration_limit_minor = int(answers["concentration_limit_minor"])
        changed["concentration_limit_minor"] = case.concentration_limit_minor
    if answers.get("obligations"):
        ids = list(case.obligation_ids_json or [])
        for ob in answers["obligations"]:
            row = Obligation(
                id=new_id("bill"),
                customer_id=case.customer_id,
                description=str(ob["description"])[:128],
                amount_minor=int(ob["amount_minor"]),
                currency=case.currency,
                due_date=date.fromisoformat(ob["due_date"]),
                certainty=ob.get("certainty", "confirmed"),
                evidence_id=None,
            )
            session.add(row)
            ids.append(row.id)
        case.obligation_ids_json = ids
        changed["obligations_added"] = len(answers["obligations"])
    if "obligation_ids" in answers and answers["obligation_ids"] is not None:
        for oid in answers["obligation_ids"]:
            ob = session.get(Obligation, oid)
            if ob is None or ob.customer_id != case.customer_id:
                raise CaseError(403, "obligation_not_owner", f"obligation {oid} does not belong to the customer")
        case.obligation_ids_json = list(answers["obligation_ids"])
        changed["obligation_ids"] = case.obligation_ids_json
    if not changed:
        raise CaseError(422, "no_answers", "no recognised answers supplied")
    case.version += 1
    case.updated_at = clock.now(session)
    missing, questions = compute_missing_fields(session, case)
    case.missing_fields_json = missing
    case.outstanding_questions_json = questions
    states.record_event(session, case, "case.information_provided", actor, {"changed": changed, "missing_fields": missing})
    if case.state == states.NEEDS_INFORMATION and not missing:
        states.transition(session, case, states.EVALUATING, actor, reason="information supplied")
    return case


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #


def build_projection_inputs(session: Session, case: Case, as_of: date | None = None) -> tuple[ProjectionInputs, list[dict], list[Obligation]]:
    contract, cd_account = _contract_for_case(session, case)
    as_of = as_of or clock.today(session)
    assumptions: list[dict] = []
    liquid = [
        a
        for a in verified_accounts(session, case.customer_id)
        if a.account_kind in LIQUID_KINDS and not a.access_revoked and a.currency == case.currency
    ]
    opening = sum(a.available_minor for a in liquid)
    for a in liquid:
        assumptions.append(
            {
                "kind": "balance",
                "text": f"{a.display_name}: available {format_minor(a.available_minor)} (current {format_minor(a.current_minor)}); pending entries excluded",
                "source": {"account_id": a.id, "snapshot_at": clock.iso(a.snapshot_at), "snapshot_source": a.snapshot_source, "evidence_id": a.evidence_id},
            }
        )
    obligations = case_obligations(session, case)
    flows: list[DatedFlow] = [
        DatedFlow(
            date=o.due_date,
            amount_minor=-o.amount_minor,
            kind="obligation",
            label=o.description,
            ref=o.id,
            confirmed=o.certainty == "confirmed",
            source=o.evidence_id or "user_entered",
        )
        for o in obligations
    ]
    flows.append(
        DatedFlow(
            date=contract.maturity_date,
            amount_minor=contract.principal_minor,
            kind="maturity_proceeds",
            label=f"CD {contract.id} principal at maturity",
            ref=contract.id,
            confirmed=True,
            source=contract.evidence_id or "contract",
        )
    )
    assumptions.append(
        {
            "kind": "maturity",
            "text": f"CD principal {format_minor(contract.principal_minor)} becomes available on {contract.maturity_date.isoformat()}; accrued interest is excluded until the bank reports it",
            "source": {"deposit_id": contract.id, "contract_version": contract.contract_version, "evidence_id": contract.evidence_id},
        }
    )
    pending_flows: list[DatedFlow] = []
    for a in liquid:
        for p in a.pending_json or []:
            try:
                p_date = date.fromisoformat(p.get("expected_on"))
            except (TypeError, ValueError):
                p_date = as_of
            pending_flows.append(
                DatedFlow(date=p_date, amount_minor=int(p["amount_minor"]), kind="pending_credit", label=p.get("description", "pending"), ref=a.id, confirmed=False, source="provider_pending")
            )
    if pending_flows:
        assumptions.append({"kind": "pending", "text": "Pending credits are shown in a separate scenario and never counted as spendable cash", "source": {"count": len(pending_flows)}})

    post = [o for o in obligations if o.due_date >= contract.maturity_date]
    post_total = sum(o.amount_minor for o in post)
    buffer_minor = effective_buffer(case.minimum_buffer_minor, post_total, bool(case.buffer_includes_obligations))
    if case.buffer_includes_obligations:
        assumptions.append({"kind": "buffer", "text": f"Stated reserve {format_minor(case.minimum_buffer_minor)} already includes {format_minor(post_total)} of dated bills; enforcing a floor of {format_minor(buffer_minor)}", "source": {"customer_confirmed": True}})
    else:
        assumptions.append({"kind": "buffer", "text": f"Minimum cash buffer {format_minor(buffer_minor)} enforced on every projected day from maturity", "source": {"customer_entered": True}})
    for o in obligations:
        assumptions.append({"kind": "obligation", "text": f"{o.description}: {format_minor(o.amount_minor)} due {o.due_date.isoformat()} ({o.certainty})", "source": {"obligation_id": o.id, "evidence_id": o.evidence_id}})

    last_due = max([o.due_date for o in obligations] + [contract.maturity_date])
    horizon_end = max(contract.maturity_date + timedelta(days=30), last_due + timedelta(days=1))
    inputs = ProjectionInputs(
        as_of=as_of,
        horizon_end=horizon_end,
        opening_available_minor=opening,
        currency=case.currency,
        buffer_minor=buffer_minor,
        effective_date=contract.maturity_date,
        flows=flows,
        pending_flows=pending_flows,
        account_ids=[a.id for a in liquid],
    )
    return inputs, assumptions, obligations


def evaluate_options(session: Session, case: Case) -> dict:
    """Pure computation over stored records: projection, headroom, comparison."""
    contract, cd_account = _contract_for_case(session, case)
    inputs, assumptions, obligations = build_projection_inputs(session, case)
    base = project(inputs)
    with_pending = project(inputs, include_pending=True) if inputs.pending_flows else None
    headroom = max_lockable(inputs, cap_minor=contract.principal_minor)
    if case.concentration_limit_minor is not None:
        headroom = min(headroom, case.concentration_limit_minor)
        assumptions.append({"kind": "policy", "text": f"Customer concentration limit {format_minor(case.concentration_limit_minor)} per placement", "source": {"customer_entered": True}})

    warnings: list[str] = []
    for start, end, lowest in _breach_ranges(base.pre_effective_breaches):
        when = f"on {start.isoformat()}" if start == end else f"from {start.isoformat()} to {end.isoformat()}"
        warnings.append(
            f"Projected available cash is below the buffer {when} (lowest {format_minor(lowest)}), before the CD matures on {contract.maturity_date.isoformat()}; the still-locked CD cannot fund it."
        )
    if clock.today(session) > contract.renewal_instruction_deadline:
        warnings.append(f"The renewal instruction deadline {contract.renewal_instruction_deadline.isoformat()} has passed; default behaviour is {contract.default_maturity_behavior}.")

    verified_ids = {a.id for a in verified_accounts(session, case.customer_id) if not a.access_revoked}
    offers = [normalize_offer(offer_to_raw(o)) for o in active_offers(session, case.deposit_id)]
    post_obligations = [(o.due_date, o.amount_minor, o.description) for o in obligations if o.due_date >= contract.maturity_date]
    comparison = compare_offers(
        offers,
        principal_minor=headroom,
        start_date=contract.maturity_date,
        as_of=clock.today(session),
        horizon_days=COMPARISON_HORIZON_DAYS,
        obligations_after_start=post_obligations,
        preferred_lockup_days=case.preferred_lockup_days,
        verified_destination_accounts=verified_ids,
    )
    return {
        "as_of": inputs.as_of.isoformat(),
        "inputs_hash": inputs_hash(inputs),
        "deposit": {
            "id": contract.id,
            "account_id": cd_account.id,
            "principal_minor": contract.principal_minor,
            "currency": contract.currency,
            "maturity_date": contract.maturity_date.isoformat(),
            "renewal_instruction_deadline": contract.renewal_instruction_deadline.isoformat(),
            "default_maturity_behavior": contract.default_maturity_behavior,
            "contract_version": contract.contract_version,
            "evidence_id": contract.evidence_id,
        },
        "projection": base.to_dict(),
        "projection_with_pending": with_pending.to_dict(include_days=False) if with_pending else None,
        "effective_buffer_minor": inputs.buffer_minor,
        "reserved_minor": contract.principal_minor - headroom,
        "max_lockable_minor": headroom,
        "comparison_horizon_days": COMPARISON_HORIZON_DAYS,
        "options": [c.to_dict() for c in comparison],
        "assumptions": assumptions,
        "warnings": warnings,
        "_inputs": inputs,
    }


async def evaluate_case(case_id: str, actor: str, customer_id: str | None = None) -> dict:
    """Refresh snapshots and offers, then compute options. Produces no transfer."""
    with session_scope() as session:
        case = get_case(session, case_id, customer_id)
        if case.state in states.TERMINAL:
            raise CaseError(409, "case_closed", "case is closed")
        if case.state not in (states.COLLECTING, states.EVALUATING, states.NEEDS_INFORMATION, states.NEEDS_REQUOTE, states.AWAITING_APPROVAL):
            raise CaseError(409, "invalid_state", f"cannot evaluate while {case.state}")
        cust = case.customer_id
        deposit_id = case.deposit_id
        state_before = case.state

    snap = await refresh_snapshots(cust, case_id)
    await refresh_offers(deposit_id, case_id)

    revoked_case_id: str | None = None
    with session_scope() as session:
        case = get_case(session, case_id)
        contract, cd_account = _contract_for_case(session, case)
        if cd_account.id in snap["revoked"]:
            # Hold the case for review in its own committed transaction, then report.
            if case.state != states.MANUAL_REVIEW:
                if case.state == states.AWAITING_APPROVAL:
                    _supersede_open_action(session, case, "access revoked during evaluation", actor)
                    states.transition(session, case, states.EVALUATING, actor)
                if case.state in (states.COLLECTING, states.NEEDS_INFORMATION, states.NEEDS_REQUOTE):
                    states.transition(session, case, states.EVALUATING, actor)
                states.transition(session, case, states.MANUAL_REVIEW, actor, reason="account access revoked; reads and execution blocked")
                outbox.emit(session, "banking_case.manual_review", case.id, {"reason": "access_revoked"})
            revoked_case_id = case.id
    if revoked_case_id:
        raise CaseError(409, "access_revoked", "access to the deposit account has been revoked; the case is held for review", {"case_id": revoked_case_id})

    with session_scope() as session:
        case = get_case(session, case_id)
        contract, cd_account = _contract_for_case(session, case)
        if case.state == states.AWAITING_APPROVAL:
            _supersede_open_action(session, case, "re-evaluation requested", actor)
            states.transition(session, case, states.EVALUATING, actor, reason="re-evaluation")
        elif case.state != states.EVALUATING:
            states.transition(session, case, states.EVALUATING, actor)
        missing, questions = compute_missing_fields(session, case)
        case.missing_fields_json = missing
        case.outstanding_questions_json = questions
        result = evaluate_options(session, case)
        inputs = result.pop("_inputs")
        plan = CashPlan(
            id=new_id("plan"),
            case_id=case.id,
            customer_id=case.customer_id,
            inputs_hash=result["inputs_hash"],
            projection_json=result["projection"],
            lowest_balance_minor=result["projection"]["lowest_from_effective_minor"],
            max_lockable_minor=result["max_lockable_minor"],
            allocation_minor=0,
            offer_id=None,
            offer_product_version=None,
            comparison_json=result["options"],
            assumptions_json=result["assumptions"],
            version=_next_plan_version(session, case.id),
            status="draft",
            created_at=clock.now(session),
        )
        session.add(plan)
        case.plan_id = plan.id
        case.warnings_json = result["warnings"]
        case.version += 1
        case.updated_at = clock.now(session)
        states.record_event(session, case, "case.evaluated", actor, {"plan_id": plan.id, "max_lockable_minor": result["max_lockable_minor"], "comparable_offers": [o["offer_id"] for o in result["options"] if o["comparable"]], "snapshots_refreshed": snap["refreshed"], "state_before": state_before})
        if missing:
            states.transition(session, case, states.NEEDS_INFORMATION, actor, reason="missing customer information", data={"missing_fields": missing})
        outbox.emit(session, "banking_case.evaluated", case.id, {"plan_id": plan.id, "state": case.state})
        result["plan_id"] = plan.id
        result["case"] = case_summary(session, case)
        return result


def _breach_ranges(breaches) -> list[tuple[date, date, int]]:
    """Group consecutive breach days into (start, end, lowest_balance) ranges."""
    ranges: list[tuple[date, date, int]] = []
    for b in sorted(breaches, key=lambda x: x.date):
        if ranges and (b.date - ranges[-1][1]).days == 1:
            start, _, lowest = ranges[-1]
            ranges[-1] = (start, b.date, min(lowest, b.balance_minor))
        else:
            ranges.append((b.date, b.date, b.balance_minor))
    return ranges


def _next_plan_version(session: Session, case_id: str) -> int:
    rows = session.scalars(select(CashPlan.version).where(CashPlan.case_id == case_id)).all()
    return (max(rows) if rows else 0) + 1


def _supersede_open_action(session: Session, case: Case, reason: str, actor: str) -> None:
    if not case.current_action_id:
        return
    action = session.get(Action, case.current_action_id)
    if action is not None and action.status in ("proposed", "approved"):
        action.status = "superseded"
        action.updated_at = clock.now(session)
        for approval in session.scalars(select(Approval).where(Approval.action_id == action.id, Approval.revoked_at.is_(None), Approval.consumed_at.is_(None))):
            approval.revoked_at = clock.now(session)
            approval.revocation_reason = reason
        instr = session.scalars(select(BankInstruction).where(BankInstruction.action_id == action.id)).first()
        if instr is not None:
            instr.status = "superseded"
            instr.updated_at = clock.now(session)
        states.record_event(session, case, "action.superseded", actor, {"action_id": action.id, "reason": reason})
    case.current_action_id = None


def get_options(session: Session, case: Case) -> dict:
    if not case.plan_id:
        raise CaseError(409, "not_evaluated", "case has not been evaluated yet")
    plan = session.get(CashPlan, case.plan_id)
    assert plan is not None
    return {
        "case_id": case.id,
        "state": case.state,
        "version": case.version,
        "plan_id": plan.id,
        "plan_version": plan.version,
        "max_lockable_minor": plan.max_lockable_minor,
        "lowest_balance_minor": plan.lowest_balance_minor,
        "projection": plan.projection_json,
        "options": plan.comparison_json,
        "assumptions": plan.assumptions_json,
        "warnings": case.warnings_json,
        "missing_fields": case.missing_fields_json,
        "outstanding_questions": case.outstanding_questions_json,
    }


# --------------------------------------------------------------------------- #
# Prepare instruction (proposed action)
# --------------------------------------------------------------------------- #


def prepare_instruction(session: Session, case: Case, option_id: str, amount_minor: int | None, actor: str) -> dict:
    if case.state != states.EVALUATING:
        raise CaseError(409, "invalid_state", f"instruction can only be prepared while evaluating (current: {case.state})")
    if case.missing_fields_json:
        raise CaseError(409, "missing_information", "answer outstanding questions before preparing an instruction", {"missing_fields": case.missing_fields_json})
    if not case.plan_id:
        raise CaseError(409, "not_evaluated", "evaluate the case first")
    draft = session.get(CashPlan, case.plan_id)
    assert draft is not None
    contract, cd_account = _contract_for_case(session, case)
    if cd_account.access_revoked:
        raise CaseError(409, "access_revoked", "access to the deposit account has been revoked")

    offer_row = session.get(DepositOffer, option_id)
    if offer_row is None or offer_row.for_deposit_id != case.deposit_id or offer_row.superseded:
        raise CaseError(404, "offer_not_found", f"offer {option_id} is not available for this deposit")
    comparison = next((c for c in draft.comparison_json if c["offer_id"] == option_id), None)
    if comparison is None or comparison["product_version"] != offer_row.product_version:
        raise CaseError(409, "plan_stale", "offer terms changed since evaluation; evaluate again")
    if not comparison["comparable"]:
        raise CaseError(409, "offer_not_comparable", "offer cannot be selected", {"exclusion_reasons": comparison["exclusion_reasons"]})
    offer = normalize_offer(offer_to_raw(offer_row))

    # Fresh projection from current records (not the cached plan) for the exact amount.
    inputs, assumptions, obligations = build_projection_inputs(session, case)
    headroom = max_lockable(inputs, cap_minor=contract.principal_minor)
    if case.concentration_limit_minor is not None:
        headroom = min(headroom, case.concentration_limit_minor)
    amount = headroom if amount_minor is None else int(amount_minor)
    if amount <= 0:
        raise CaseError(422, "invalid_amount", "amount must be positive")
    if amount > headroom:
        raise CaseError(
            409,
            "liquidity_violation",
            f"locking {format_minor(amount)} would breach the cash buffer; at most {format_minor(headroom)} can be allocated",
            {"max_lockable_minor": headroom, "requested_minor": amount},
        )
    min_dep = offer.restrictions.get("minimum_deposit_minor")
    if isinstance(min_dep, int) and amount < min_dep:
        raise CaseError(409, "below_minimum_deposit", f"offer requires at least {format_minor(min_dep)}")

    # Destination must be a verified same-owner account (renewal: the CD account itself).
    instruction_type = instruction_type_for_offer_kind(offer.offer_kind)
    verified_ids = {a.id for a in verified_accounts(session, case.customer_id) if not a.access_revoked}
    destination = offer.destination_account_id if instruction_type == "same_owner_transfer" else cd_account.id
    if destination not in verified_ids:
        raise CaseError(403, "destination_not_verified", "destination is outside the customer's verified ownership set", {"destination_account_id": destination})

    # Concurrent instructions must not spend the same available amount.
    reserved = _reserved_against_source(session, cd_account.id, exclude_case_id=case.id)
    if reserved + amount > contract.principal_minor:
        raise CaseError(409, "funds_already_committed", f"{format_minor(reserved)} of this deposit is already committed by another instruction", {"reserved_minor": reserved})

    alloc = allocation_flow(amount, contract.maturity_date, f"Allocate to {offer.product_name}", ref=offer.id)
    projection = project(inputs, allocations=[alloc])
    if not projection.feasible:
        raise CaseError(409, "liquidity_violation", "allocation breaches the buffer", {"breaches": [b.to_dict() for b in projection.effective_breaches]})

    now = clock.now(session)
    plan = CashPlan(
        id=new_id("plan"),
        case_id=case.id,
        customer_id=case.customer_id,
        inputs_hash=inputs_hash(inputs, [alloc]),
        projection_json=projection.to_dict(),
        lowest_balance_minor=projection.lowest_from_effective_minor,
        max_lockable_minor=headroom,
        allocation_minor=amount,
        offer_id=offer.id,
        offer_product_version=offer.product_version,
        comparison_json=[comparison],
        assumptions_json=assumptions + [{"kind": "offer", "text": f"{offer.product_name} {offer.apy} APY, version {offer.product_version}", "source": offer.source.to_dict()}],
        version=_next_plan_version(session, case.id),
        status="selected",
        created_at=now,
    )
    session.add(plan)
    draft.status = "superseded"

    payload = InstructionPayload(
        instruction_type=instruction_type,
        customer_id=case.customer_id,
        source_account_id=cd_account.id,
        destination_account_id=destination,
        amount_minor=amount,
        currency=case.currency,
        effective_at=contract.maturity_date,
        offer_id=offer.id,
        provider_id=offer.provider_id,
        product_code=offer.product_code,
        product_version=offer.product_version,
        apy_decimal=str(offer.apy),
        term_days=offer.term_days,
        fees_minor=offer.fees_minor,
        deposit_contract_id=contract.id,
        contract_version=contract.contract_version,
        irreversible_effect=irreversible_effect_text(instruction_type, offer.term_days, offer.restrictions.get("early_withdrawal_penalty")),
    )
    digest = payload_hash(payload)
    idem = "idem:" + hashlib.sha256(f"{case.id}|{plan.version}|{digest}".encode()).hexdigest()[:32]
    action = Action(
        id=new_id("act"),
        case_id=case.id,
        type=instruction_type,
        payload_json=payload.to_dict(),
        payload_hash=digest,
        status="proposed",
        idempotency_key=idem,
        request_ref=f"req_{case.id}_{plan.version}",
        case_version_at_creation=case.version,
        created_at=now,
        updated_at=now,
    )
    session.add(action)
    session.flush()  # plan and action must exist before the instruction references them
    instruction = BankInstruction(
        id=new_id("instr"),
        plan_id=plan.id,
        case_id=case.id,
        action_id=action.id,
        instruction_type=instruction_type,
        source_account_id=cd_account.id,
        destination_account_id=destination,
        amount_minor=amount,
        currency=case.currency,
        effective_at=contract.maturity_date,
        offer_id=offer.id,
        product_version=offer.product_version,
        term_days=offer.term_days,
        apy_decimal=str(offer.apy),
        request_ref=action.request_ref,
        status="prepared",
        created_at=now,
        updated_at=now,
    )
    session.add(instruction)
    session.flush()
    case.plan_id = plan.id
    case.selected_offer_id = offer.id
    case.current_action_id = action.id
    states.record_event(session, case, "action.proposed", actor, {"action_id": action.id, "payload_hash": digest, "amount_minor": amount, "offer_id": offer.id, "product_version": offer.product_version})
    states.transition(session, case, states.AWAITING_APPROVAL, actor)
    outbox.emit(session, "banking_case.awaiting_approval", case.id, {"action_id": action.id, "payload_hash": digest})
    return review_screen(session, case, action)


def _reserved_against_source(session: Session, source_account_id: str, exclude_case_id: str | None = None) -> int:
    rows = session.scalars(
        select(BankInstruction).where(
            BankInstruction.source_account_id == source_account_id,
            BankInstruction.status.in_(["prepared", "approved", "submitting", "submitted", "outcome_unknown", "effective"]),
        )
    ).all()
    return sum(r.amount_minor for r in rows if r.case_id != exclude_case_id)


def review_screen(session: Session, case: Case, action: Action) -> dict:
    """Exact destination, documents, amount, terms and irreversible effect (plan §13)."""
    payload = action.payload_json
    plan = session.get(CashPlan, case.plan_id) if case.plan_id else None
    contract, cd_account = _contract_for_case(session, case)
    destination = session.get(BankAccount, payload["destination_account_id"]) if payload.get("destination_account_id") else None
    offer = session.get(DepositOffer, payload["offer_id"])
    docs = []
    for doc_id in {contract.evidence_id, cd_account.evidence_id, destination.evidence_id if destination else None} - {None}:
        d = session.get(Document, doc_id)
        if d is not None:
            docs.append({"id": d.id, "summary": d.summary, "content_hash": d.content_hash, "source": d.source, "captured_at": clock.iso(d.captured_at)})
    approvals = session.scalars(select(Approval).where(Approval.action_id == action.id)).all()
    return {
        "case_id": case.id,
        "case_version": case.version,
        "state": case.state,
        "action_id": action.id,
        "action_status": action.status,
        "action_payload_hash": action.payload_hash,
        "idempotency_key": action.idempotency_key,
        "request_ref": action.request_ref,
        "instruction": payload,
        "amount_display": format_minor(payload["amount_minor"], payload["currency"]),
        "source_account": {"id": cd_account.id, "display_name": cd_account.display_name, "provider_id": cd_account.provider_id},
        "destination_account": (
            {"id": destination.id, "display_name": destination.display_name, "provider_id": destination.provider_id, "ownership_verified": destination.ownership_verified}
            if destination
            else None
        ),
        "offer": (
            {"id": offer.id, "product_name": offer.product_name, "product_version": offer.product_version, "apy_decimal": offer.apy_decimal, "term_days": offer.term_days, "fees_minor": offer.fees_minor, "restrictions": offer.restrictions_json, "valid_until": offer.valid_until.isoformat(), "source": {"provider_id": offer.provider_id, "retrieved_at": clock.iso(offer.retrieved_at), "environment": offer.environment, "authoritative": offer.authoritative}}
            if offer
            else None
        ),
        "irreversible_effect": payload["irreversible_effect"],
        "liquidity": (
            {"lowest_balance_after_allocation_minor": plan.lowest_balance_minor, "buffer_minor": plan.projection_json.get("buffer_minor"), "reserved_minor": contract.principal_minor - payload["amount_minor"], "feasible": plan.projection_json.get("feasible")}
            if plan
            else None
        ),
        "assumptions_requiring_confirmation": (plan.assumptions_json if plan else []),
        "documents": docs,
        "approvals": [{"id": a.id, "approver_id": a.approver_id, "expires_at": clock.iso(a.expires_at), "revoked_at": clock.iso(a.revoked_at) if a.revoked_at else None, "consumed_at": clock.iso(a.consumed_at) if a.consumed_at else None} for a in approvals],
    }


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def create_challenge(session: Session, case: Case, action: Action, customer_id: str) -> ApprovalChallenge:
    if case.state != states.AWAITING_APPROVAL or action.status != "proposed" or case.current_action_id != action.id:
        raise CaseError(409, "not_approvable", f"action {action.id} is not awaiting approval")
    now = clock.now(session)
    challenge = ApprovalChallenge(
        id=new_id("challenge"),
        action_id=action.id,
        customer_id=customer_id,
        payload_hash=action.payload_hash,
        case_version=case.version,
        expires_at=now + timedelta(minutes=settings.approval_challenge_ttl_minutes),
        created_at=now,
    )
    session.add(challenge)
    session.flush()
    states.record_event(session, case, "approval.challenge_issued", customer_id, {"challenge_id": challenge.id, "action_id": action.id, "expires_at": clock.iso(challenge.expires_at)})
    return challenge


def approve_action(
    session: Session,
    case: Case,
    action: Action,
    approver_id: str,
    expected_case_version: int,
    action_payload_hash: str,
    approval_challenge_id: str,
) -> Approval:
    now = clock.now(session)
    if case.customer_id != approver_id:
        raise CaseError(403, "not_owner", "only the case owner can approve")
    if case.state != states.AWAITING_APPROVAL or case.current_action_id != action.id:
        raise CaseError(409, "not_approvable", f"case is {case.state}; action {action.id} is not awaiting approval")
    if case.version != expected_case_version:
        raise CaseError(409, "stale_case_version", f"case version is {case.version}, expected {expected_case_version}", {"current_version": case.version})
    if action.status != "proposed":
        raise CaseError(409, "action_not_proposed", f"action status is {action.status}")
    if action.payload_hash != action_payload_hash:
        raise CaseError(409, "payload_changed", "the action payload hash does not match the proposed action", {"expected": action.payload_hash})
    challenge = session.get(ApprovalChallenge, approval_challenge_id)
    if challenge is None or challenge.action_id != action.id or challenge.customer_id != approver_id:
        raise CaseError(403, "invalid_challenge", "approval challenge not found for this action and approver")
    if challenge.used_at is not None:
        raise CaseError(409, "challenge_used", "approval challenge already consumed")
    if challenge.expires_at < now:
        raise CaseError(410, "challenge_expired", "approval challenge expired; request a new one")
    if challenge.payload_hash != action.payload_hash or challenge.case_version != case.version:
        raise CaseError(409, "challenge_mismatch", "the challenge was issued for a different payload or case version")
    contract, _ = _contract_for_case(session, case)
    offer = session.get(DepositOffer, action.payload_json["offer_id"])
    if offer is None or offer.superseded or offer.product_version != action.payload_json["product_version"]:
        raise CaseError(409, "offer_changed", "the offer version changed; evaluate again")
    if offer.valid_until < clock.today(session):
        raise CaseError(409, "offer_expired", "the selected offer has expired; evaluate again")

    expiry_day = min(offer.valid_until, contract.renewal_instruction_deadline) if contract.maturity_date >= clock.today(session) else offer.valid_until
    expires_at = datetime.combine(expiry_day, time(23, 59, 59), tzinfo=timezone.utc)
    if expires_at < now:
        expires_at = now + timedelta(hours=24)
    challenge.used_at = now
    approval = Approval(
        id=new_id("appr"),
        action_id=action.id,
        approver_id=approver_id,
        action_hash=action.payload_hash,
        scope=f"{action.type}:{action.payload_json['amount_minor']}:{action.payload_json['currency']}:{action.payload_json.get('destination_account_id')}",
        challenge_id=challenge.id,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(approval)
    session.flush()
    action.status = "approved"
    action.approval_id = approval.id
    action.updated_at = now
    instr = session.scalars(select(BankInstruction).where(BankInstruction.action_id == action.id)).first()
    if instr is not None:
        instr.status = "approved"
        instr.updated_at = now
    states.record_event(session, case, "action.approved", approver_id, {"action_id": action.id, "approval_id": approval.id, "action_hash": approval.action_hash, "expires_at": clock.iso(expires_at)}, expected_case_version=expected_case_version)
    states.transition(session, case, states.APPROVED, approver_id, expected_version=expected_case_version)
    enqueue_job(session, "execute_action", case.id, {"action_id": action.id})
    outbox.emit(session, "banking_case.approved", case.id, {"action_id": action.id, "approval_id": approval.id})
    return approval


def cancel_case(session: Session, case: Case, actor: str, reason: str = "cancelled by customer") -> Case:
    if case.state not in (states.COLLECTING, states.EVALUATING, states.NEEDS_INFORMATION, states.AWAITING_APPROVAL, states.NEEDS_REQUOTE, states.MANUAL_REVIEW):
        raise CaseError(409, "cannot_cancel", f"cannot cancel while {case.state}; cancellation after bank acceptance is a separate provider request")
    _supersede_open_action(session, case, reason, actor)
    states.transition(session, case, states.CANCELLED, actor, reason=reason)
    outbox.emit(session, "banking_case.cancelled", case.id, {"reason": reason})
    return case


def enqueue_job(session: Session, job_type: str, case_id: str | None, payload: dict, run_at: datetime | None = None) -> Job:
    now = clock.now(session)
    # Idempotent enqueue: one *pending* job per (type, action). A leased job is
    # running right now and may itself be the one asking to be rescheduled.
    action_id = payload.get("action_id")
    if action_id:
        existing = session.scalars(
            select(Job).where(Job.type == job_type, Job.status == "pending", Job.case_id == case_id)
        ).all()
        for j in existing:
            if j.payload_json.get("action_id") == action_id:
                if run_at is not None and run_at < j.run_at:
                    j.run_at = run_at
                return j
    job = Job(id=new_id("job"), type=job_type, case_id=case_id, payload_json=payload, status="pending", run_at=run_at or now, attempts=0, created_at=now)
    session.add(job)
    session.flush()
    return job


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #


def case_summary(session: Session, case: Case) -> dict:
    contract = session.get(DepositContract, case.deposit_id)
    return {
        "id": case.id,
        "customer_id": case.customer_id,
        "workflow_type": case.workflow_type,
        "status": case.state,
        "state": case.state,
        "version": case.version,
        "deposit_id": case.deposit_id,
        "maturity_date": contract.maturity_date.isoformat() if contract else None,
        "currency": case.currency,
        "minimum_buffer_minor": case.minimum_buffer_minor,
        "obligation_ids": case.obligation_ids_json,
        "preferred_lockup_days": case.preferred_lockup_days,
        "buffer_includes_obligations": case.buffer_includes_obligations,
        "missing_fields": case.missing_fields_json,
        "outstanding_questions": case.outstanding_questions_json,
        "warnings": case.warnings_json,
        "selected_offer_id": case.selected_offer_id,
        "plan_id": case.plan_id,
        "current_action_id": case.current_action_id,
        "review_reason": case.review_reason,
        "completion_evidence_ref": case.completion_evidence_ref,
        "provider_mode": case.provider_mode,
        "created_at": clock.iso(case.created_at),
        "updated_at": clock.iso(case.updated_at),
    }


def case_detail(session: Session, case: Case) -> dict:
    summary = case_summary(session, case)
    contract, cd_account = _contract_for_case(session, case)
    accounts = session.scalars(select(BankAccount).where(BankAccount.customer_id == case.customer_id)).all()
    obligations = case_obligations(session, case)
    action = session.get(Action, case.current_action_id) if case.current_action_id else None
    instruction = current_instruction(session, case)
    plan = session.get(CashPlan, case.plan_id) if case.plan_id else None
    summary.update(
        {
            "deposit": {
                "id": contract.id,
                "account_id": cd_account.id,
                "display_name": cd_account.display_name,
                "principal_minor": contract.principal_minor,
                "currency": contract.currency,
                "apy_decimal": contract.apy_decimal,
                "maturity_date": contract.maturity_date.isoformat(),
                "renewal_instruction_deadline": contract.renewal_instruction_deadline.isoformat(),
                "default_maturity_behavior": contract.default_maturity_behavior,
                "contract_version": contract.contract_version,
                "evidence_id": contract.evidence_id,
            },
            "accounts": [
                {"id": a.id, "display_name": a.display_name, "provider_id": a.provider_id, "account_kind": a.account_kind, "ownership_verified": a.ownership_verified, "access_revoked": a.access_revoked, "currency": a.currency, "available_minor": a.available_minor, "current_minor": a.current_minor, "pending": a.pending_json, "snapshot_at": clock.iso(a.snapshot_at), "snapshot_source": a.snapshot_source, "evidence_id": a.evidence_id}
                for a in accounts
            ],
            "obligations": [
                {"id": o.id, "description": o.description, "amount_minor": o.amount_minor, "currency": o.currency, "due_date": o.due_date.isoformat(), "certainty": o.certainty, "evidence_id": o.evidence_id, "selected": o.id in (case.obligation_ids_json or [])}
                for o in obligations
            ],
            "plan": (
                {"id": plan.id, "version": plan.version, "status": plan.status, "max_lockable_minor": plan.max_lockable_minor, "allocation_minor": plan.allocation_minor, "lowest_balance_minor": plan.lowest_balance_minor, "offer_id": plan.offer_id, "offer_product_version": plan.offer_product_version, "projection": plan.projection_json, "options": plan.comparison_json, "assumptions": plan.assumptions_json, "inputs_hash": plan.inputs_hash}
                if plan
                else None
            ),
            "review": review_screen(session, case, action) if action else None,
            "instruction": (
                {"id": instruction.id, "status": instruction.status, "instruction_type": instruction.instruction_type, "amount_minor": instruction.amount_minor, "currency": instruction.currency, "effective_at": instruction.effective_at.isoformat(), "external_ref": instruction.external_ref, "request_ref": instruction.request_ref, "source_account_id": instruction.source_account_id, "destination_account_id": instruction.destination_account_id, "offer_id": instruction.offer_id, "product_version": instruction.product_version, "reconciliation": instruction.reconciliation_json}
                if instruction
                else None
            ),
            "now": clock.iso(clock.now(session)),
        }
    )
    return summary


def timeline(session: Session, case: Case) -> dict:
    events = session.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.sequence)).all()
    requests = session.scalars(select(AdapterRequest).where(AdapterRequest.case_id == case.id).order_by(AdapterRequest.created_at)).all()
    instruction = current_instruction(session, case)
    return {
        "case_id": case.id,
        "state": case.state,
        "version": case.version,
        "events": [
            {"id": e.id, "sequence": e.sequence, "type": e.event_type, "actor": e.actor, "previous_state": e.previous_state, "next_state": e.next_state, "expected_case_version": e.expected_case_version, "source_event_id": e.source_event_id, "occurred_at": clock.iso(e.occurred_at), "data": e.data_json}
            for e in events
        ],
        "provider_requests": [
            {"id": r.id, "provider_id": r.provider_id, "operation": r.operation, "request_ref": r.request_ref, "environment": r.environment, "latency_ms": r.latency_ms, "outcome": r.outcome, "created_at": clock.iso(r.created_at), "response": r.response_redacted_json}
            for r in requests
        ],
        "instruction": (
            {"id": instruction.id, "status": instruction.status, "external_ref": instruction.external_ref, "request_ref": instruction.request_ref, "effective_at": instruction.effective_at.isoformat(), "amount_minor": instruction.amount_minor, "currency": instruction.currency, "reconciliation": instruction.reconciliation_json}
            if instruction
            else None
        ),
        "completion_evidence_ref": case.completion_evidence_ref,
    }


def maturity_inbox(session: Session, customer_id: str) -> dict:
    today = clock.today(session)
    contracts = session.scalars(select(DepositContract)).all()
    items = []
    for c in contracts:
        acct = session.get(BankAccount, c.account_id)
        if acct is None or acct.customer_id != customer_id:
            continue
        open_case = session.scalars(select(Case).where(Case.deposit_id == c.id).order_by(Case.created_at.desc())).first()
        items.append(
            {
                "deposit_id": c.id,
                "account": {"id": acct.id, "display_name": acct.display_name, "provider_id": acct.provider_id},
                "principal_minor": c.principal_minor,
                "currency": c.currency,
                "apy_decimal": c.apy_decimal,
                "maturity_date": c.maturity_date.isoformat(),
                "days_to_maturity": (c.maturity_date - today).days,
                "renewal_instruction_deadline": c.renewal_instruction_deadline.isoformat(),
                "default_maturity_behavior": c.default_maturity_behavior,
                "contract_version": c.contract_version,
                "case": case_summary(session, open_case) if open_case else None,
            }
        )
    obligations = session.scalars(select(Obligation).where(Obligation.customer_id == customer_id).order_by(Obligation.due_date)).all()
    return {
        "customer_id": customer_id,
        "today": today.isoformat(),
        "deposits": items,
        "obligations": [{"id": o.id, "description": o.description, "amount_minor": o.amount_minor, "currency": o.currency, "due_date": o.due_date.isoformat(), "certainty": o.certainty, "evidence_id": o.evidence_id} for o in obligations],
    }


def offers_by_id(session: Session, deposit_id: str) -> dict[str, NormalizedOffer]:
    return {o.id: normalize_offer(offer_to_raw(o)) for o in active_offers(session, deposit_id)}


def material_terms_for(offer_row: DepositOffer) -> dict:
    return material_terms(normalize_offer(offer_to_raw(offer_row)))
