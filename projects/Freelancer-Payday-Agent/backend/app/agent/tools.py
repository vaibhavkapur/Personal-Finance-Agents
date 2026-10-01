"""Typed tool surface. Deliberately has no approval or bank-submit capability."""
import json
from typing import Optional, Protocol
from pydantic import Field
from ..api.schemas import StrictModel
from ..domain.engine import DomainError
from ..persistence.store import uid


class CashInput(StrictModel):
    customer_id: str


class ReceiptInput(StrictModel):
    customer_id: str
    transaction_ids: list[str] = Field(min_length=1, max_length=100)


class CalculateInput(StrictModel):
    case_id: str
    policy_version: int
    snapshot_id: str


class PrepareInput(StrictModel):
    proposal_id: str
    idempotency_key: str = Field(min_length=1, max_length=100)


class ReconcileInput(StrictModel):
    proposal_id: str


SCHEMAS = {
    "get_verified_cash_snapshot": (CashInput, "Read posted cash, holds, source timestamp and unpaid invoices separately."),
    "classify_receipts": (ReceiptInput, "Suggest receipt categories with immutable evidence; uncertain receipts require human confirmation."),
    "calculate_payout": (CalculateInput, "Calculate a reserve-constrained payout from an exact cash snapshot and policy version."),
    "prepare_payday_transfer": (PrepareInput, "Prepare an exact same-owner transfer for customer review; never approves or submits it."),
    "reconcile_payday": (ReconcileInput, "Look up bank evidence and reconcile a previously authorized payout."),
}


def list_tools():
    return [{"name":name, "description":description, "inputSchema":schema.model_json_schema(),
             "annotations":{"readOnlyHint":name in ("get_verified_cash_snapshot", "classify_receipts"), "destructiveHint":False}}
            for name, (schema, description) in SCHEMAS.items()]


class ToolGateway:
    def __init__(self, service, tenant):
        self.service, self.tenant = service, tenant

    def call(self, name, arguments):
        if name not in SCHEMAS:
            raise DomainError("Unknown or unauthorized tool.", 400)
        args = SCHEMAS[name][0](**arguments).model_dump()
        s, tenant = self.service, self.tenant
        case_id = args.get("case_id", "context")
        try:
            if name == "get_verified_cash_snapshot":
                p = s.refresh(tenant, args["customer_id"])
                value = {"snapshot_id":p["snapshot_id"], "bank":p["bank_snapshot"], "buckets":p["buckets"], "invoices":p["invoices"], "policy_version":p["policy"]["version"]}
            elif name == "classify_receipts":
                p = s.profile(tenant, args["customer_id"])
                receipts = {r["id"]:r for r in p["receipts"]}
                if any(i not in receipts for i in args["transaction_ids"]):
                    raise DomainError("Receipt not found.", 404)
                value = {"receipts":[{"id":i,"category":receipts[i]["category"],"requires_confirmation":not receipts[i]["confirmed"],"evidence_id":receipts[i]["evidence_id"]} for i in args["transaction_ids"]]}
            elif name == "calculate_payout":
                c = s.case(tenant, args["case_id"])
                p = s.refresh(tenant, c["customer_id"])
                if args["snapshot_id"] != p["snapshot_id"] or args["policy_version"] != p["policy"]["version"]:
                    raise DomainError("Tool inputs are stale; retrieve a fresh snapshot.")
                value = s.calculate_case(tenant, c["id"])
            elif name == "prepare_payday_transfer":
                with s.store.transaction() as db:
                    v = s.store.get(db, "payday_proposals", args["proposal_id"], tenant)
                value = s.draft(tenant, v["case_id"], v["id"], args["idempotency_key"])
            else:
                with s.store.transaction() as db:
                    v = s.store.get(db, "payday_proposals", args["proposal_id"], tenant)
                c = s.case(tenant, v["case_id"])
                p = s.refresh(tenant, c["customer_id"])
                value = {"case":s.case(tenant, c["id"]), "buckets":p["buckets"]}
            result = {"data":value,"source":"payday_domain_engine","retrieved_at":s.now(),"authority":"simulated","environment":"mock","currency":"USD"}
            outcome = "success"
            return result
        except Exception:
            outcome = "error"
            raise
        finally:
            now = s.now()
            with s.store.transaction() as db:
                db.execute("INSERT INTO tool_runs VALUES(?,?,?,?,?,?,?)", (uid("tool"), tenant, case_id, name, now, outcome, json.dumps({"input_fields":sorted(args), "engine_version":"rules-1", "model_cost_usd":"0.00"})))


class StructuredPlanner(Protocol):
    """Future model integrations return this narrow intent, never executable code."""
    def choose_intent(self, message: str) -> str: ...


class RulesPlanner:
    def choose_intent(self, message):
        m = message.lower()
        if any(x in m for x in ("ignore", "bypass", "borrow", "overdraft", "raid", "tax filing", "effective tax", "self-approve", "send now", "approve for me", "guarantee")):
            return "refusal"
        if any(x in m for x in ("reconcile", "status", "timeout", "returned", "return")):
            return "reconcile"
        if any(x in m for x in ("receipt", "classify", "income")):
            return "classify"
        if any(x in m for x in ("plan", "calculate", "payday", "afford", "late", "reserve")):
            return "plan"
        return "cash"


def assist(service, tenant, case_id, message, budget=4, planner: Optional[StructuredPlanner]=None):
    case = service.case(tenant, case_id)
    planner = planner or RulesPlanner()
    intent = planner.choose_intent(message)
    gateway, calls = ToolGateway(service, tenant), []
    def call(name, args):
        if len(calls) >= budget:
            raise DomainError("Tool budget reached; review the available evidence.")
        calls.append(name)
        return gateway.call(name, args)["data"]
    if intent == "refusal":
        return {"intent":intent, "message":"I can plan from posted cash and your approved reserves. I cannot borrow, bypass your review, guarantee a payday, or determine taxes. Transfers need your exact approval.", "tools":[], "engine":"rules-1", "cost_usd":"0.00"}
    try:
        snap = call("get_verified_cash_snapshot", {"customer_id":case["customer_id"]})
        p = service.profile(tenant, case["customer_id"])
        missing = [r["id"] for r in p["receipts"] if not r["confirmed"]]
        if missing or intent == "classify":
            value = call("classify_receipts", {"customer_id":p["id"], "transaction_ids":missing or [r["id"] for r in p["receipts"]]})
            text = "Please review the unclassified receipt in Activity before I calculate your payday." if missing else "Confirmed own-account transfers are excluded from earned income. Linked refunds reduce the original receipt's provisional tax allocation."
            intent = "missing_information" if missing else intent
        elif intent == "plan":
            value = call("calculate_payout", {"case_id":case_id,"snapshot_id":snap["snapshot_id"],"policy_version":snap["policy_version"]})
            v = value["proposal"]
            text = f"Your supported payday is ${v['feasible_minor']/100:,.2f}. ${sum(v['protected'].values())/100:,.2f} stays protected. " + v["explanation"]
        elif intent == "reconcile" and case.get("proposal_id"):
            value = call("reconcile_payday", {"proposal_id":case["proposal_id"]})
            text = "The payout is " + value["case"]["status"].replace("_", " ") + ". Unknown outcomes keep their reservation until the bank confirms what happened."
        else:
            value = snap
            text = f"You have ${snap['bank']['available_minor']/100:,.2f} in posted available business cash. Unpaid invoices are expected cash and cannot fund a transfer. Tax reserves are provisional planning amounts."
        return {"intent":intent,"message":text,"tools":calls,"evidence":value,"engine":"rules-1","cost_usd":"0.00"}
    except DomainError as exc:
        return {"intent":"needs_review","message":exc.message,"tools":calls,"engine":"rules-1","cost_usd":"0.00"}
