"""Typed model boundary. Approval, execution and permission changes are never tools."""
import time
from typing import Protocol
from pydantic import Field
from app.api.schemas import StrictModel
from app.domain.types import DomainError, uid, instant
from app.domain.market import market_snapshot
from app.persistence.store import Store as S


class SnapshotInput(StrictModel):
    symbols: list[str] = Field(min_length=1, max_length=5)
    as_of: str


class EvaluateInput(StrictModel):
    mandate_id: str
    market_version: str
    symbols: list[str] = Field(min_length=1, max_length=5)


class PreviewInput(StrictModel):
    signal_id: str


class PrepareInput(StrictModel):
    signal_id: str
    preview_id: str
    idempotency_key: str


class ReportInput(StrictModel):
    order_id: str


class MandateInput(StrictModel):
    mandate_id: str


TOOL_TYPES = {
    "get_market_snapshot": (SnapshotInput, "Read timestamped synthetic quotes and historical bars at a permitted cutoff."),
    "get_mandate": (MandateInput, "Read an approved mandate and its exact limits."),
    "evaluate_strategy": (EvaluateInput, "Evaluate fixed SMA-cross-v1 using a pinned fixture version; no trading authority."),
    "preview_order": (PreviewInput, "Check deterministic risk limits, including open-order reservations."),
    "prepare_order": (PrepareInput, "Reserve an immutable draft for human review. Cannot approve or execute it."),
    "get_execution_report": (ReportInput, "Read individual executions, reserved cash, and local broker evidence."),
}


class StructuredToolProvider(Protocol):
    def choose(self, context: dict) -> dict | None: ...


class ToolService:
    def __init__(self, engine, tenant="demo"):
        self.engine, self.tenant = engine, tenant

    def call(self, name, arguments):
        if name not in TOOL_TYPES:
            raise DomainError("Tool is not in the scoped allowlist", 403)
        args = TOOL_TYPES[name][0].model_validate(arguments).model_dump()
        start, outcome = time.monotonic(), "ok"
        try:
            e = self.engine
            if name == "get_market_snapshot":
                with e.store.tx() as db:
                    if instant(args["as_of"]) > instant(e.runtime(db)["now"]):
                        raise DomainError("Requested cutoff exceeds the replay clock")
                    available = {s["symbol"]: s for s in e.latest_market(db, self.tenant)}
                    if not set(args["symbols"]) <= available.keys():
                        raise DomainError("Missing market data for requested symbol")
                    result = [market_snapshot(available[s], args["as_of"]) for s in args["symbols"]]
            elif name == "get_mandate":
                with e.store.tx() as db:
                    result = S.get(db, "trading_mandates", args["mandate_id"], self.tenant)
            elif name == "evaluate_strategy":
                with e.store.tx() as db:
                    if args["market_version"] != e.runtime(db)["fixture_version"]:
                        raise DomainError("Market fixture version does not match")
                result = e.evaluate_run(args["symbols"], args["mandate_id"], self.tenant)
            elif name == "preview_order":
                result = e.preview(args["signal_id"], self.tenant)
            elif name == "prepare_order":
                result = e.prepare(args["signal_id"], args["preview_id"], "partial", args["idempotency_key"], self.tenant)
            else:
                with e.store.tx() as db:
                    result = e.order_detail(db, args["order_id"], self.tenant)
            with e.store.tx() as db:
                now = e.runtime(db)["now"]
            return {"data": result, "source": "meridian-domain-engine", "retrieved_at": now, "authority": "simulated", "environment": "mock"}
        except Exception:
            outcome = "error"
            raise
        finally:
            with self.engine.store.tx() as db:
                S.put(db, "tool_runs", {"id": uid("tool"), "tool": name, "arguments": args, "outcome": outcome, "latency_ms": round((time.monotonic() - start) * 1000), "model": "rules-only-v1", "prompt_version": "scoped-tools-v1", "cost_minor": 0, "created_at": self.engine.runtime(db)["now"]}, self.tenant)

    def explain(self, symbol, budget=4):
        """Rules-only reference orchestrator. An external LLM is deliberately optional."""
        with self.engine.store.tx() as db:
            account, runtime = self.engine.account(db, self.tenant), self.engine.runtime(db)
        transcript = []
        def call(name, args):
            if len(transcript) >= budget:
                raise DomainError("Tool budget exhausted; review the collected evidence", 409)
            response = self.call(name, args)
            transcript.append({"tool": name, "result": response})
            return response["data"]
        try:
            call("get_market_snapshot", {"symbols": [symbol], "as_of": runtime["now"]})
            signal = call("evaluate_strategy", {"mandate_id": account["active_mandate_id"], "market_version": runtime["fixture_version"], "symbols": [symbol]})[0]
            preview = call("preview_order", {"signal_id": signal["id"]})
            allowed = preview["allowed"]
            explanation = signal["rationale"] + (f" Risk checks permit a draft for {preview['quantity']} shares at ${preview['limit_price_minor'] / 100:.2f}. Exact-order approval is still required." if allowed else " Blocked: " + "; ".join(r["detail"] for r in preview["reasons"]))
            return {"status": "review_ready" if allowed else "blocked", "explanation": explanation, "signal": signal, "preview": preview, "tool_calls": len(transcript), "evidence": transcript, "model": "rules-only-v1", "cost_minor": 0}
        except DomainError as exc:
            return {"status": "manual_review", "explanation": exc.message, "tool_calls": len(transcript), "evidence": transcript, "model": "rules-only-v1", "cost_minor": 0}
