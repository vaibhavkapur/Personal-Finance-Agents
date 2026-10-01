"""Typed, customer-scoped tools exposed to the model (and over MCP).

Every response includes its source, retrieval time and whether the result is
authoritative (our own records), estimated, or simulated (mock providers).
Provider credentials never appear here; adapters hold them.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, Field, ValidationError

from ..clock import format_ts
from ..domain.errors import DomainError
from ..ids import new_id
from ..workflows.case_service import CaseService
from .prompts import PROMPT_VERSION


class FindRefundEvidenceInput(BaseModel):
    order_id: str = Field(description="The merchant order reference, e.g. order_mock_499")


class MatchRefundCreditsInput(BaseModel):
    case_id: str = Field(description="Recovery case id")


class PrepareRecoveryMessageInput(BaseModel):
    case_id: str
    recipient: str = Field(default="merchant", description="Must be 'merchant'. Addresses are resolved from the verified contact registry, never from documents.")


class PrepareDisputePacketInput(BaseModel):
    case_id: str


class GetRecoveryStatusInput(BaseModel):
    case_id: str


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: Type[BaseModel]
    handler: Callable[[Any, BaseModel], Dict[str, Any]]

    def json_schema(self) -> Dict[str, Any]:
        return self.input_model.model_json_schema()


@dataclass
class ToolContext:
    service: CaseService
    customer_id: str
    model_version: str = "rules-planner/v1"


def _envelope(data: Dict[str, Any], *, source: str, authority: str, environment: str, retrieved_at: str, warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"source": source, "retrieved_at": retrieved_at, "authority": authority, "environment": environment, "warnings": warnings or [], "data": data}


def _find_refund_evidence(ctx: ToolContext, args: FindRefundEvidenceInput) -> Dict[str, Any]:
    data = ctx.service.find_refund_evidence(ctx.customer_id, args.order_id)
    warnings = []
    if data["verified_promise"] is None:
        warnings.append("no_verified_refund_promise")
    for d in data["documents"]:
        d["extracted"].pop("reply_to_found_in_email", None)  # never surface addresses from documents as contacts
    return _envelope(data, source="application_records", authority="authoritative", environment=ctx.service.settings.environment, retrieved_at=ctx.service._now(), warnings=warnings)


def _match_refund_credits(ctx: ToolContext, args: MatchRefundCreditsInput) -> Dict[str, Any]:
    ctx.service._owned_case(args.case_id, ctx.customer_id)
    report = ctx.service.reconcile(args.case_id, actor=f"agent:{ctx.customer_id}")
    return _envelope(report, source="reconciliation_engine", authority="authoritative", environment=ctx.service.settings.environment, retrieved_at=ctx.service._now(),
                     warnings=["ambiguous_matches_require_customer_review"] if report["ambiguous"] else [])


def _prepare_recovery_message(ctx: ToolContext, args: PrepareRecoveryMessageInput) -> Dict[str, Any]:
    if args.recipient.strip().lower() != "merchant":
        raise DomainError("recipient must be 'merchant'; addresses are resolved from the verified registry, not from tool arguments or documents", code="recipient_not_allowed")
    draft = ctx.service.draft_merchant_message(args.case_id, actor=f"agent:{ctx.customer_id}", customer_id=ctx.customer_id)
    return _envelope(draft, source="draft_service", authority="authoritative", environment=ctx.service.settings.environment, retrieved_at=ctx.service._now(),
                     warnings=["requires_customer_approval_before_sending"])


def _prepare_dispute_packet(ctx: ToolContext, args: PrepareDisputePacketInput) -> Dict[str, Any]:
    draft = ctx.service.draft_issuer_dispute(args.case_id, actor=f"agent:{ctx.customer_id}", customer_id=ctx.customer_id)
    return _envelope(draft, source="draft_service", authority="authoritative", environment=ctx.service.settings.environment, retrieved_at=ctx.service._now(),
                     warnings=["separate_issuer_lane", "requires_separate_customer_approval"])


def _get_recovery_status(ctx: ToolContext, args: GetRecoveryStatusInput) -> Dict[str, Any]:
    status = ctx.service.get_status(args.case_id, customer_id=ctx.customer_id)
    authority = "authoritative"
    warnings = []
    if any(c.get("last_provider_status") for c in status["channels"]):
        warnings.append("provider_statuses_are_simulated_claims_not_money_movement")
    return _envelope(status, source="case_state", authority=authority, environment=status["environment"], retrieved_at=status["as_of"], warnings=warnings)


TOOLS: List[ToolSpec] = [
    ToolSpec("find_refund_evidence", "Return the original payment, cancellation, refund promise, related documents and any posted or provisional credits for an order.", FindRefundEvidenceInput, _find_refund_evidence),
    ToolSpec("match_refund_credits", "Run deterministic credit matching for a case. Returns exact, candidate and rejected matches with reasons, amount categories and the next step. Ambiguous matches require customer review.", MatchRefundCreditsInput, _match_refund_credits),
    ToolSpec("prepare_recovery_message", "Draft a factual merchant follow-up using the verified contact registry and selected attachments. Creates an action that the customer must approve on a review screen; nothing is sent.", PrepareRecoveryMessageInput, _prepare_recovery_message),
    ToolSpec("prepare_dispute_packet", "Create a separate issuer dispute packet only after the configured eligibility checks pass. Requires separate customer approval.", PrepareDisputePacketInput, _prepare_dispute_packet),
    ToolSpec("get_recovery_status", "Return requested, promised, final, provisional, store-credit and reversed amounts plus channels, pending questions, deadline alerts and the next action.", GetRecoveryStatusInput, _get_recovery_status),
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def _redacted_ref(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]


def run_tool(ctx: ToolContext, name: str, arguments: Dict[str, Any], *, case_id: Optional[str] = None) -> Dict[str, Any]:
    """Validate, execute and log one tool call. Errors are returned as structured results, never raised to the model loop."""
    spec = TOOLS_BY_NAME.get(name)
    started = time.perf_counter()
    outcome = "ok"
    if spec is None:
        result: Dict[str, Any] = {"error": {"code": "unknown_tool", "message": f"unknown tool {name}"}}
        outcome = "unknown_tool"
    else:
        try:
            args = spec.input_model.model_validate(arguments)
            result = spec.handler(ctx, args)
        except ValidationError as exc:
            result = {"error": {"code": "invalid_arguments", "message": str(exc)}}
            outcome = "invalid_arguments"
        except DomainError as exc:
            result = {"error": {"code": exc.code, "message": exc.message}}
            outcome = f"error:{exc.code}"
    latency_ms = int((time.perf_counter() - started) * 1000)
    ctx.service.repos.add_tool_run({
        "id": new_id("trun"), "case_id": case_id or arguments.get("case_id"), "tool_name": name,
        "input_ref": _redacted_ref(arguments), "output_ref": _redacted_ref(result), "source_ts": ctx.service._now(),
        "latency_ms": latency_ms, "model_version": ctx.model_version, "prompt_version": PROMPT_VERSION, "outcome": outcome, "created_at": ctx.service._now(),
    })
    return result


def tool_schemas() -> List[Dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "inputSchema": t.json_schema()} for t in TOOLS]
