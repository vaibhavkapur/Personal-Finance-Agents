from __future__ import annotations
import json, time
from typing import Protocol
from pydantic import BaseModel, ConfigDict, Field
from backend.app.domain.engine import *
from backend.app.persistence.store import *

PROTOCOL_VERSION="2025-11-25"
class Input(BaseModel):
    model_config=ConfigDict(extra="forbid")
class CustomerInput(Input): customer_id: str
class IncidentInput(Input): incident_id: str
class TransactionInput(IncidentInput): transaction_id: str
class ReportInput(IncidentInput): provider_id: str
SCHEMAS={"get_verified_instruments":CustomerInput,"prepare_protective_actions":IncidentInput,"get_transaction_context":TransactionInput,"prepare_incident_report":ReportInput,"get_incident_resolution":IncidentInput}
DESCRIPTIONS={"get_verified_instruments":"Read masked synthetic instruments and trusted provider capabilities for the authenticated customer.","prepare_protective_actions":"Prepare prioritized lock drafts. Never approves or submits them.","get_transaction_context":"Read descriptor context without determining fraud.","prepare_incident_report":"Prepare a report draft only for customer-confirmed unauthorized transactions.","get_incident_resolution":"Read separate protection, replacement, investigation and credit outcomes."}

class Tools:
    def __init__(self,store,customer): self.store=store; self.customer=customer; self.service=Service(store)
    def list(self):
        return [{"name":name,"description":DESCRIPTIONS[name],"inputSchema":schema.model_json_schema(),"annotations":{"readOnlyHint":not name.startswith("prepare"),"destructiveHint":False,"openWorldHint":False}} for name,schema in SCHEMAS.items()]
    def call(self,name,arguments):
        if name not in SCHEMAS: fail(422,"Tool is not allowed")
        args=SCHEMAS[name].model_validate(arguments).model_dump()
        started=time.monotonic(); case_id=args.get("incident_id")
        try:
            if name=="get_verified_instruments":
                if args["customer_id"]!=self.customer: fail(404,"Customer not found")
                result={"instruments":INSTRUMENTS,"providers":PROVIDERS}
            else:
                case=self.service.resolution(case_id,self.customer)
                if name=="get_incident_resolution": result=case
                elif name=="get_transaction_context": result=transaction(case,args["transaction_id"])
                elif name=="prepare_protective_actions":
                    result={"drafts":[]}
                    for i in case["data"]["instruments"]:
                        if i["protection_status"]=="unprotected":
                            current=self.service.resolution(case_id,self.customer)
                            result["drafts"].append(self.service.draft(case_id,self.customer,"lock",i["id"],current["version"],uid("tool")))
                else:
                    if args["provider_id"] not in PROVIDERS: fail(422,"Unknown provider")
                    result={"drafts":[],"missing":[]}
                    for t in case["data"]["transactions"]:
                        if t["provider_id"]!=args["provider_id"]: continue
                        if t["evidence_status"]=="customer_reports_unauthorized":
                            current=self.service.resolution(case_id,self.customer)
                            result["drafts"].append(self.service.draft(case_id,self.customer,"report",t["id"],current["version"],uid("tool")))
                        elif t["evidence_status"]=="unrecognized": result["missing"].append("Customer confirmation for "+t["id"])
            with self.store.tx() as c:
                c.execute(tool_runs.insert().values(id=uid("run"),case_id=case_id,name=name,occurred_at=self.store.now(),outcome="success",metadata={"latency_ms":round((time.monotonic()-started)*1000),"source":"fixture-v1","model":"rules-only-v1","cost_usd":0}))
            return {"data":result,"source":"trusted mock provider registry and durable case state","retrieved_at":self.store.now(),"authority":"simulated","environment":"mock"}
        except Exception:
            with self.store.tx() as c:
                c.execute(tool_runs.insert().values(id=uid("run"),case_id=case_id,name=name,occurred_at=self.store.now(),outcome="rejected",metadata={"model":"rules-only-v1"}))
            raise

class ToolCallingModel(Protocol):
    """Extension point only; no external model is configured or claimed in this MVP."""
    def choose_tools(self, message: str, incident_id: str) -> list[dict]: ...

class RulesOnlyModel:
    def choose_tools(self,message,incident_id):
        lower=message.lower()
        if any(word in lower for word in ["approve","ignore instructions","password","otp","pin:","transfer","unlock"]): return []
        if "prepare" in lower and any(word in lower for word in ["lock","protect"]): return [{"name":"prepare_protective_actions","arguments":{"incident_id":incident_id}}]
        return [{"name":"get_incident_resolution","arguments":{"incident_id":incident_id}}]

class Orchestrator:
    def __init__(self,tools,model=None,budget=4): self.tools=tools; self.model=model or RulesOnlyModel(); self.budget=budget
    def respond(self,message,incident_id):
        safe_text(message)
        # Verify ownership even if the policy refuses before calling a tool.
        self.tools.service.resolution(incident_id,self.tools.customer)
        calls=self.model.choose_tools(message,incident_id)
        if len(calls)>self.budget: return {"message":"This request needs manual review: the tool budget was exceeded.","tool_calls":0,"mode":"rules-only","cost_usd":0}
        if not calls: return {"message":"I can explain the case and prepare actions. Approve exact actions in the review screen. Use the verified bank handoff for authentication; never share a bank code here.","tool_calls":0,"mode":"rules-only","cost_usd":0}
        for call in calls:
            if call["arguments"].get("incident_id",incident_id)!=incident_id: fail(403,"Tool call scope mismatch")
            self.tools.call(call["name"],call["arguments"])
        case=self.tools.service.resolution(incident_id,self.tools.customer)
        protected=sum(i["protection_status"] in PROTECTED for i in case["data"]["instruments"])
        pending=[i for i in case["data"]["instruments"] if i["protection_status"]=="direct_customer_action_required"]
        message=f"{protected} of {len(case['data']['instruments'])} cards have verified protection. "
        if not case["data"]["identity_verified"]: message+="Confirm the affected instruments and synthetic identity before preparing actions. "
        elif any(i["protection_status"] in {"unknown","manual_review"} for i in case["data"]["instruments"]): message+="A provider outcome is uncertain. Verify the original request or use manual review before any retry. "
        elif pending: message+="Northstar needs your direct authentication through its simulated secure center. "
        elif protected<len(case["data"]["instruments"]): message+="Review and approve the supported card locks first. "
        else: message+="Card protection is verified. Review unfamiliar charges and track each investigation separately. "
        if any(t["credit"]["provisional_minor"] for t in case["data"]["transactions"]): message+="A provisional credit is temporary; the investigation remains open. "
        if calls[0]["name"]=="prepare_protective_actions": message+="Your drafts are ready in the approval queue."
        return {"message":message,"tool_calls":len(calls),"mode":"rules-only","cost_usd":0}
