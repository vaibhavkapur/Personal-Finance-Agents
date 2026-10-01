"""Typed, scoped agent tools (plan §6).

Every tool:
* is bound to the authenticated customer via ``ToolContext`` and refuses to
  read or act outside that scope;
* returns ``_meta`` with source, retrieval time and whether the result is
  authoritative, estimated or simulated;
* is recorded in ``tool_runs`` with redacted input/output references.

Tools cannot approve or execute anything. ``prepare_bank_instruction`` only
creates an immutable proposed action; approval requires the customer's
authenticated challenge/approve calls and execution happens in the worker.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from sqlalchemy import select

from app import clock
from app.config import settings
from app.domain.liquidity import allocation_flow, max_lockable, project
from app.ids import new_id
from app.persistence.db import session_scope
from app.persistence.models import Action, BankAccount, BankInstruction, Case, DepositContract, ToolRun
from app.workflows import case_service, states
from app.workflows.errors import CaseError
from app.workflows.refresh import refresh_offers

PROMPT_VERSION = "2026-09-26.1"


@dataclass
class ToolContext:
    customer_id: str
    case_id: str | None = None
    model_version: str = "rules-v1"
    actor: str = "agent"
    calls: int = 0
    budget: int = field(default_factory=lambda: settings.agent_tool_budget)


class ToolRefused(Exception):
    """The tool declined to run (scope violation, budget, or forbidden action)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], Any]
    is_async: bool = False

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _ref(data: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _redact(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: ("<redacted>" if k in {"customer_id", "owner_id"} else _redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v) for v in data[:20]]
    return data


def _summary(result: Any) -> dict:
    if isinstance(result, dict):
        keys = sorted(result.keys())
        summary = {"keys": keys[:30]}
        for k in ("state", "status", "max_lockable_minor", "case_id", "action_id", "error"):
            if k in result:
                summary[k] = result[k]
        return summary
    if isinstance(result, list):
        return {"count": len(result)}
    return {"value": str(result)[:200]}


def _meta(session, source: str, authority: str) -> dict:
    return {"source": source, "retrieved_at": clock.iso(clock.now(session)), "authority": authority, "environment": settings.environment}


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #


def read_cash_snapshot(ctx: ToolContext, args: dict) -> dict:
    customer_id = args.get("customer_id") or ctx.customer_id
    if customer_id != ctx.customer_id:
        raise ToolRefused("cross-customer data access is not permitted")
    with session_scope() as session:
        as_of = date.fromisoformat(args["as_of"]) if args.get("as_of") else clock.today(session)
        accounts = session.scalars(select(BankAccount).where(BankAccount.customer_id == customer_id).order_by(BankAccount.id)).all()
        contracts = session.scalars(select(DepositContract)).all()
        acct_ids = {a.id for a in accounts}
        deposits = [c for c in contracts if c.account_id in acct_ids]
        inbox = case_service.maturity_inbox(session, customer_id)
        return {
            "as_of": as_of.isoformat(),
            "accounts": [
                {
                    "id": a.id,
                    "display_name": a.display_name,
                    "provider_id": a.provider_id,
                    "account_kind": a.account_kind,
                    "ownership_verified": a.ownership_verified,
                    "access_revoked": a.access_revoked,
                    "currency": a.currency,
                    "available_minor": a.available_minor,
                    "current_minor": a.current_minor,
                    "pending_entries": a.pending_json,
                    "snapshot_at": clock.iso(a.snapshot_at),
                    "snapshot_source": a.snapshot_source,
                    "evidence_id": a.evidence_id,
                }
                for a in accounts
            ],
            "deposits": [
                {"id": c.id, "account_id": c.account_id, "principal_minor": c.principal_minor, "currency": c.currency, "apy_decimal": c.apy_decimal, "maturity_date": c.maturity_date.isoformat(), "renewal_instruction_deadline": c.renewal_instruction_deadline.isoformat(), "default_maturity_behavior": c.default_maturity_behavior, "contract_version": c.contract_version, "evidence_id": c.evidence_id, "open_case": next((d["case"] for d in inbox["deposits"] if d["deposit_id"] == c.id), None)}
                for c in deposits
            ],
            "obligations_on_record": inbox["obligations"],
            "_meta": _meta(session, "application_snapshot_of_provider_data", "authoritative_as_of_snapshot_at"),
        }


async def list_maturity_options(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as session:
        account = session.get(BankAccount, args["account_id"])
        if account is None or account.customer_id != ctx.customer_id:
            raise ToolRefused("account is outside the authenticated customer's scope")
        contract = session.scalars(select(DepositContract).where(DepositContract.account_id == account.id)).first()
        if contract is None:
            raise CaseError(404, "deposit_not_found", "no deposit contract on this account")
        deposit_id = contract.id
    rows = await refresh_offers(deposit_id, ctx.case_id)
    with session_scope() as session:
        today = clock.today(session)
        return {
            "deposit_id": deposit_id,
            "offers": [
                {
                    "id": r.id,
                    "provider_id": r.provider_id,
                    "product_name": r.product_name,
                    "product_version": r.product_version,
                    "offer_kind": r.offer_kind,
                    "apy_decimal": r.apy_decimal,
                    "rate_type": r.rate_type,
                    "term_days": r.term_days,
                    "fees_minor": r.fees_minor,
                    "restrictions": r.restrictions_json,
                    "valid_until": r.valid_until.isoformat(),
                    "expired": r.valid_until < today,
                    "eligibility_status": r.eligibility_status,
                    "eligibility_notes": r.eligibility_notes,
                    "destination_account_id": r.destination_account_id,
                    "accrual_method": r.accrual_method,
                    "source": {"provider_id": r.provider_id, "retrieved_at": clock.iso(r.retrieved_at), "environment": r.environment, "authoritative": r.authoritative},
                }
                for r in rows
            ],
            "_meta": _meta(session, "provider_offers_via_adapter", "authoritative_provider_quote_simulated"),
        }


def project_cash(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as session:
        case = case_service.get_case(session, args["case_id"], ctx.customer_id)
        inputs, assumptions, obligations = case_service.build_projection_inputs(session, case)
        contract = session.get(DepositContract, case.deposit_id)
        headroom = max_lockable(inputs, cap_minor=contract.principal_minor)
        allocations = []
        amount = args.get("allocation_minor")
        if amount is not None:
            allocations.append(allocation_flow(int(amount), contract.maturity_date, "proposed allocation", ref=args.get("offer_id")))
        projection = project(inputs, allocations=allocations)
        return {
            "case_id": case.id,
            "effective_buffer_minor": inputs.buffer_minor,
            "max_lockable_minor": headroom,
            "allocation_minor": int(amount) if amount is not None else 0,
            "lowest_projected_available_minor": projection.lowest_from_effective_minor,
            "lowest_projected_available_date": projection.lowest_from_effective_date.isoformat(),
            "feasible": projection.feasible,
            "breaches_after_maturity": [b.to_dict() for b in projection.effective_breaches],
            "pre_maturity_shortfalls": [b.to_dict() for b in projection.pre_effective_breaches],
            "days": [d.to_dict() for d in projection.days if d.events or d.date in (inputs.as_of, inputs.horizon_end)],
            "assumptions": assumptions,
            "_meta": _meta(session, "deterministic_cash_model", "estimated_from_authoritative_snapshots"),
        }


def open_maturity_case(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as session:
        case = case_service.create_case(
            session,
            customer_id=ctx.customer_id,
            deposit_id=args["deposit_id"],
            currency=args.get("currency", "USD"),
            minimum_buffer_minor=int(args["minimum_buffer_minor"]),
            obligation_ids=list(args.get("obligation_ids") or []),
            preferred_lockup_days=args.get("preferred_lockup_days"),
            buffer_includes_obligations=args.get("buffer_includes_obligations"),
            actor=ctx.actor,
        )
        ctx.case_id = case.id
        summary = case_service.case_summary(session, case)
        summary["_meta"] = _meta(session, "application_case_store", "authoritative")
        return summary


def record_customer_answers(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as session:
        case = case_service.get_case(session, args["case_id"], ctx.customer_id)
        answers = {k: v for k, v in args.items() if k != "case_id" and v is not None}
        case_service.answer_questions(session, case, answers, actor=ctx.actor)
        summary = case_service.case_summary(session, case)
        summary["_meta"] = _meta(session, "application_case_store", "authoritative")
        return summary


async def evaluate_case(ctx: ToolContext, args: dict) -> dict:
    result = await case_service.evaluate_case(args["case_id"], actor=ctx.actor, customer_id=ctx.customer_id)
    with session_scope() as session:
        result["_meta"] = _meta(session, "cash_plan_engine_and_offer_comparison", "estimated_from_authoritative_snapshots")
    # Trim day-level detail for the model; the full projection stays on the plan record.
    result["projection"] = {k: v for k, v in result["projection"].items() if k != "days"}
    return result


def prepare_bank_instruction(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as session:
        case = case_service.get_case(session, args["case_id"], ctx.customer_id)
        if args.get("plan_id") and case.plan_id != args["plan_id"]:
            raise CaseError(409, "plan_stale", "plan_id is not the case's current plan; evaluate again")
        review = case_service.prepare_instruction(session, case, args["option_id"], args.get("amount_minor"), actor=ctx.actor)
        review["_meta"] = _meta(session, "application_case_store", "authoritative")
        review["note"] = "Proposed action created. It cannot execute until the customer approves it through the review screen; the agent cannot approve."
        return review


def get_instruction_status(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as session:
        instruction = None
        if args.get("instruction_id"):
            instruction = session.get(BankInstruction, args["instruction_id"])
        elif args.get("case_id"):
            instruction = case_service.current_instruction(session, case_service.get_case(session, args["case_id"], ctx.customer_id))
        if instruction is None:
            case = case_service.get_case(session, args["case_id"], ctx.customer_id) if args.get("case_id") else None
            return {"case_id": args.get("case_id"), "instruction": None, "state": case.state if case else None, "_meta": _meta(session, "application_case_store", "authoritative")}
        case = case_service.get_case(session, instruction.case_id, ctx.customer_id)
        action = session.get(Action, instruction.action_id)
        tl = case_service.timeline(session, case)
        return {
            "case_id": case.id,
            "state": case.state,
            "review_reason": case.review_reason,
            "instruction": {"id": instruction.id, "status": instruction.status, "amount_minor": instruction.amount_minor, "currency": instruction.currency, "effective_at": instruction.effective_at.isoformat(), "external_ref": instruction.external_ref, "request_ref": instruction.request_ref, "reconciliation": instruction.reconciliation_json},
            "action": {"id": action.id, "status": action.status, "payload_hash": action.payload_hash, "provider_reference": action.provider_reference} if action else None,
            "completion_evidence_ref": case.completion_evidence_ref,
            "recent_events": tl["events"][-8:],
            "provider_requests": tl["provider_requests"][-5:],
            "_meta": _meta(session, "application_case_store_and_provider_lookups", "authoritative"),
        }


TOOLS: dict[str, ToolSpec] = {
    "read_cash_snapshot": ToolSpec(
        "read_cash_snapshot",
        "Read the customer's account snapshots (available vs current balances, pending entries, ownership, source timestamps), deposit contracts and obligations on record.",
        {"type": "object", "properties": {"customer_id": {"type": "string"}, "as_of": {"type": "string", "description": "ISO date"}}, "required": []},
        read_cash_snapshot,
    ),
    "list_maturity_options": ToolSpec(
        "list_maturity_options",
        "Refresh and list provider-sourced product versions for the deposit on an account, with expiry, fees, restrictions and eligibility.",
        {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]},
        list_maturity_options,
        is_async=True,
    ),
    "project_cash": ToolSpec(
        "project_cash",
        "Run the deterministic date-by-date cash model for a case, optionally with a proposed allocation, and return the lowest projected available balance and buffer breaches.",
        {"type": "object", "properties": {"case_id": {"type": "string"}, "allocation_minor": {"type": "integer"}, "offer_id": {"type": "string"}}, "required": ["case_id"]},
        project_cash,
    ),
    "open_maturity_case": ToolSpec(
        "open_maturity_case",
        "Open a CD maturity case for the authenticated customer with a minimum cash buffer and the obligations to preserve. Returns missing fields to ask the customer.",
        {"type": "object", "properties": {"deposit_id": {"type": "string"}, "minimum_buffer_minor": {"type": "integer"}, "obligation_ids": {"type": "array", "items": {"type": "string"}}, "preferred_lockup_days": {"type": "integer"}, "buffer_includes_obligations": {"type": "boolean"}, "currency": {"type": "string"}}, "required": ["deposit_id", "minimum_buffer_minor"]},
        open_maturity_case,
    ),
    "record_customer_answers": ToolSpec(
        "record_customer_answers",
        "Record answers the customer gave to outstanding questions (preferred lock-up days, whether the reserve already includes dated bills, extra obligations).",
        {"type": "object", "properties": {"case_id": {"type": "string"}, "preferred_lockup_days": {"type": "integer"}, "buffer_includes_obligations": {"type": "boolean"}, "minimum_buffer_minor": {"type": "integer"}, "obligations": {"type": "array", "items": {"type": "object"}}}, "required": ["case_id"]},
        record_customer_answers,
    ),
    "evaluate_case": ToolSpec(
        "evaluate_case",
        "Refresh balances and offers, run the cash projection and compare feasible options at a common horizon. Produces options, never a transfer.",
        {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]},
        evaluate_case,
        is_async=True,
    ),
    "prepare_bank_instruction": ToolSpec(
        "prepare_bank_instruction",
        "Create an immutable proposed renewal or same-owner transfer for customer approval. Cannot approve or execute.",
        {"type": "object", "properties": {"case_id": {"type": "string"}, "plan_id": {"type": "string"}, "option_id": {"type": "string"}, "amount_minor": {"type": "integer"}}, "required": ["case_id", "option_id"]},
        prepare_bank_instruction,
    ),
    "get_instruction_status": ToolSpec(
        "get_instruction_status",
        "Retrieve bank references, state and reconciliation evidence for a case's instruction.",
        {"type": "object", "properties": {"instruction_id": {"type": "string"}, "case_id": {"type": "string"}}, "required": []},
        get_instruction_status,
    ),
}


async def run_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    """Execute a tool with scope, budget and audit recording."""
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolRefused(f"unknown tool {name}")
    started = time.perf_counter()
    outcome = "ok"
    result: Any = None
    try:
        if ctx.calls >= ctx.budget:
            raise ToolRefused(f"tool-call budget of {ctx.budget} exhausted")
        ctx.calls += 1
        result = await spec.handler(ctx, args) if spec.is_async else spec.handler(ctx, args)
        if isinstance(result, dict) and result.get("case_id") and not ctx.case_id:
            ctx.case_id = result["case_id"]
        if isinstance(result, dict) and result.get("id", "").startswith("bankcase_") and not ctx.case_id:
            ctx.case_id = result["id"]
        return result
    except ToolRefused as exc:
        outcome = "refused"
        result = {"error": "refused", "message": str(exc)}
        return result
    except CaseError as exc:
        outcome = "error"
        result = exc.to_dict()
        return result
    except states.IllegalTransition as exc:
        outcome = "error"
        result = {"error": "illegal_transition", "message": str(exc)}
        return result
    finally:
        latency = int((time.perf_counter() - started) * 1000)
        with session_scope() as session:
            src = {}
            if isinstance(result, dict) and isinstance(result.get("_meta"), dict):
                src = {"retrieved_at": result["_meta"].get("retrieved_at"), "authority": result["_meta"].get("authority")}
            session.add(
                ToolRun(
                    id=new_id("trun"),
                    case_id=ctx.case_id,
                    customer_id=ctx.customer_id,
                    tool_name=name,
                    input_ref=_ref(args),
                    output_ref=_ref(result),
                    input_redacted_json=_redact(args),
                    output_summary_json=_summary(result),
                    source_timestamps_json=src,
                    latency_ms=latency,
                    model_version=ctx.model_version,
                    prompt_version=PROMPT_VERSION,
                    outcome=outcome,
                    created_at=clock.now(session),
                )
            )


def tool_schemas() -> list[dict]:
    return [spec.schema() for spec in TOOLS.values()]
