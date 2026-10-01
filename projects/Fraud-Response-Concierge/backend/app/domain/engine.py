from __future__ import annotations
import hashlib, json, re
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, update
from backend.app.persistence.store import *
from backend.app.domain.registry import *

TERMINAL_TX = {"resolved_customer_favor", "resolved_other", "recognized"}
PROTECTED = {"temporarily_locked", "lost_reported"}

def fail(code, detail): raise HTTPException(code, detail)
def digest(payload): return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def safe_text(text):
    if re.search(r"\b(?:\d[ -]?){6,}\b|\b(?:pin|password|passcode|otp|one[- ]time code)\s*(?:[:=]|is\b)\s*\S+", text, re.I):
        fail(422, "Do not include account numbers, passwords, PINs, or authentication codes.")
    return text

def instrument(case, ref):
    return next((i for i in case["data"]["instruments"] if i["id"] == ref), None) or fail(404, "Affected instrument not found")
def transaction(case, ref):
    return next((t for t in case["data"]["transactions"] if t["id"] == ref), None) or fail(404, "Transaction not found")

def state(data):
    if not data["identity_verified"]: return "reported"
    protections = [i["protection_status"] for i in data["instruments"]]
    if any(p == "manual_review" for p in protections): return "manual_review"
    protected = sum(p in PROTECTED for p in protections)
    if protected != len(protections):
        if protected: return "partly_contained"
        if "direct_customer_action_required" in protections: return "direct_customer_action_required"
        if "pending" in protections or "unknown" in protections: return "containment_in_progress"
        return "containment_review"
    txs = data["transactions"]
    if any(t["investigation_status"] == "information_required" for t in txs): return "information_required"
    if any(t["investigation_status"] == "provider_investigating" for t in txs): return "investigation_pending"
    if any(t["evidence_status"] in {"unrecognized", "customer_reports_unauthorized"} and t["investigation_status"] == "not_reported" for t in txs): return "contained"
    if all(t["investigation_status"] in TERMINAL_TX for t in txs): return "recovery_followup"
    return "contained"

class Service:
    def __init__(self, store): self.store = store
    def get(self, c, case_id, customer):
        case = row(c, cases, cases.c.id == case_id)
        if not case or case["customer_id"] != customer: fail(404, "Incident not found")
        return case
    def check_version(self, case, expected):
        if expected != case["version"]: fail(409, "The incident changed. Refresh and review the latest details.")
    def save(self, c, case, event_type, actor, info=None, force=None):
        previous, version = case["status"], case["version"]
        next_state = force or state(case["data"])
        if previous == "closed" and event_type not in {"provider.credit_reversed"}: fail(409, "Closed incidents only reopen on a credit reversal event")
        case.update(status=next_state, version=version + 1)
        result = c.execute(update(cases).where(cases.c.id == case["id"], cases.c.version == version).values(status=next_state, version=version+1, data=case["data"]))
        if result.rowcount != 1: fail(409, "Concurrent case update; refresh and try again")
        c.execute(events.insert().values(id=uid("evt"), case_id=case["id"], sequence=version+1, event_type=event_type, actor=actor, occurred_at=self.store.now(), previous_state=previous, next_state=next_state, data=info or {}))
    def create(self, customer, discovered_at, instrument_refs, tx_refs, narrative="I lost my wallet and noticed purchases I do not recognize."):
        if not instrument_refs or len(set(instrument_refs)) != len(instrument_refs) or not set(instrument_refs) <= {i["id"] for i in INSTRUMENTS}: fail(422, "Select valid affected instruments")
        allowed = {t["id"] for t in TRANSACTIONS if t["instrument_id"] in instrument_refs}
        if not set(tx_refs) <= allowed or len(set(tx_refs)) != len(tx_refs): fail(422, "Transactions must belong to affected instruments")
        if parse(discovered_at) > parse(self.store.now()): fail(422, "Discovery cannot be after the fixture clock")
        data = fixtures(instrument_refs, tx_refs); data["narrative"] = safe_text(narrative)
        case = dict(id=uid("inc"), customer_id=customer, status="reported", version=1, discovered_at=discovered_at, data=data)
        with self.store.tx() as c:
            c.execute(cases.insert().values(**case))
            c.execute(events.insert().values(id=uid("evt"), case_id=case["id"], sequence=1, event_type="incident.reported", actor=customer, occurred_at=self.store.now(), previous_state=None, next_state="reported", data={"source": "customer", "environment": "mock"}))
        return case
    def resolution(self, case_id, customer):
        with self.store.tx() as c:
            case = self.get(c, case_id, customer)
            case["actions"] = rows(c, actions, actions.c.case_id == case_id)
            case["events"] = sorted(rows(c, events, events.c.case_id == case_id), key=lambda x:x["sequence"], reverse=True)
            case["providers"] = PROVIDERS
            case["clock"] = self.store.now()
            case["environment"] = "mock"
            case["can_close"] = self.can_close(case)
            return case
    def can_close(self, case):
        data = case["data"]
        return (bool(data["instruments"]) and all(i["protection_status"] in PROTECTED and i.get("protection_reference") and i["replacement_status"] == "delivered" and i.get("replacement_reference") and i["lost_report_status"] == "reported" for i in data["instruments"]) and all(t["investigation_status"] in TERMINAL_TX and (t["investigation_status"] == "recognized" or t.get("outcome_event_id")) for t in data["transactions"]) and all(t["status"] == "completed" and t.get("completion_evidence_id") for t in data["tasks"]))
    def verify(self, case_id, customer, expected):
        with self.store.tx() as c:
            case = self.get(c, case_id, customer); self.check_version(case, expected)
            case["data"]["identity_verified"] = True
            case["data"]["narrative_confirmed_at"] = self.store.now()
            self.save(c, case, "identity.verified", customer, {"source": "authenticated synthetic session"})
        return self.resolution(case_id, customer)
    def confirm(self, case_id, customer, ref, expected, choice, statement):
        with self.store.tx() as c:
            case=self.get(c,case_id,customer); self.check_version(case,expected)
            if not case["data"]["identity_verified"]: fail(403,"Verify identity first")
            t=transaction(case,ref)
            submitted=[a for a in rows(c,actions,actions.c.case_id==case_id) if a["payload"].get("transaction_id")==ref and a["status"] in {"dispatching","unknown","manual_review","direct_customer_action_required","succeeded"}]
            if submitted: fail(409,"A potentially submitted report cannot be rewritten; reconcile it first")
            if t["investigation_status"] != "not_reported": fail(409,"A submitted report cannot be rewritten")
            t["customer_statement"]=safe_text(statement)
            t["evidence_status"] = "customer_reports_unauthorized" if choice == "unauthorized" else "customer_recognized"
            t["investigation_status"] = "not_reported" if choice == "unauthorized" else "recognized"
            self.invalidate(c, case_id, ref)
            self.save(c,case,"transaction.customer_confirmed",customer,{"transaction_id":ref,"assertion":t["evidence_status"]})
        return self.resolution(case_id,customer)
    def invalidate(self,c,case_id,ref):
        for a in rows(c,actions,actions.c.case_id==case_id):
            if a["payload"].get("transaction_id")==ref and a["status"] in {"draft","queued"}:
                c.execute(actions.update().where(actions.c.id==a["id"]).values(status="invalidated"))
                c.execute(approvals.update().where(approvals.c.action_id==a["id"]).values(revoked_at=self.store.now()))
                c.execute(jobs.update().where(jobs.c.action_id==a["id"]).values(status="cancelled"))
    def draft(self, case_id, customer, kind, ref, expected, key, statement=None):
        with self.store.tx() as c:
            case=self.get(c,case_id,customer)
            existing=row(c,actions,(actions.c.customer_id==customer)&(actions.c.idempotency_key==key))
            if existing:
                if existing["case_id"]!=case_id or existing["kind"]!=kind or existing["payload"].get("target_id")!=ref or (kind=="response" and existing["payload"].get("response_statement")!=statement): fail(409,"Idempotency key reused with different content")
                if existing["status"] in {"invalidated","revoked"}: fail(409,"Action was invalidated; prepare a fresh draft")
                return existing
            self.check_version(case,expected)
            if not case["data"]["identity_verified"]: fail(403,"Verify customer identity first")
            if case["status"]=="closed": fail(409,"Incident is closed")
            tx=transaction(case,ref) if kind in {"report","response"} else None
            inst=instrument(case,tx["instrument_id"] if tx else ref)
            if kind not in {"lock","report_lost","replacement","report","response"}: fail(422,"Unsupported action")
            if kind not in {"report","response"} and not inst["capabilities"].get(kind): fail(422,"Provider does not support this action; use verified handoff")
            if kind=="lock" and inst["protection_status"] in PROTECTED: fail(409,"Protection already verified")
            if kind=="replacement" and (inst["protection_status"] not in PROTECTED or inst["lost_report_status"] != "reported"): fail(409,"Verify protection and report the lost card before replacement")
            if kind=="replacement" and inst["replacement_status"] != "not_requested": fail(409,"Replacement already requested")
            if kind=="report_lost" and inst["lost_report_status"]=="reported": fail(409,"Lost card already reported")
            if kind=="response" and (tx["investigation_status"]!="information_required" or not statement): fail(409,"A provider question and your factual response are required")
            if kind=="response": safe_text(statement)
            if kind=="report" and (tx["evidence_status"]!="customer_reports_unauthorized" or tx["investigation_status"]!="not_reported"): fail(409,"A customer-confirmed unauthorized statement is required")
            for prior in rows(c,actions,actions.c.case_id==case_id):
                if prior["status"]=="draft" and prior["expires_at"]<=self.store.now():
                    c.execute(actions.update().where(actions.c.id==prior["id"]).values(status="invalidated"))
                    continue
                if kind=="response" and prior["status"]=="succeeded": continue
                if prior["kind"]==kind and prior["payload"].get("target_id")==ref and prior["status"] in {"draft","queued","dispatching","unknown","direct_customer_action_required","succeeded","manual_review"}: return prior
            effects={"response":"Submit your factual response to the existing bank case. This does not guarantee a particular investigation outcome.","lock":"Temporarily lock this card. Some recurring, pending, or offline transactions may still process.","report_lost":"Report this card lost. The simulated provider permanently disables this card number.","replacement":"Order a replacement to your verified address on file: 120 Demo Lane, Brooklyn, NY. Simulated fee: $0.00.","report":"Submit your exact statement for investigation. This does not establish fraud or guarantee a credit."}
            payload={"case_id":case_id,"customer_id":customer,"provider_id":inst["provider_id"],"instrument_id":inst["id"],"target_id":ref,"kind":kind,"destination":PROVIDERS[inst["provider_id"]]["channel"],"effect":effects[kind],"environment":"mock","documents":[],"masked_identifier":inst["masked_identifier"],"narrative":case["data"]["narrative"]}
            if kind=="response": payload.update(response_statement=statement,provider_case_ref=tx["provider_case_ref"],provider_revision=tx.get("provider_revision",0))
            if tx: payload.update(transaction_id=ref,amount_minor=tx["amount_minor"],currency=tx["currency"],customer_statement=tx["customer_statement"],merchant=tx["merchant"])
            action=dict(id=uid("act"),case_id=case_id,customer_id=customer,kind=kind,payload=payload,payload_hash=digest(payload),status="draft",idempotency_key=key,challenge_id=uid("challenge"),expires_at=iso(parse(self.store.now())+timedelta(minutes=15)),approval_id=None,provider_ref=None,result=None)
            c.execute(actions.insert().values(**action)); self.save(c,case,"action.prepared",customer,{"action_id":action["id"],"kind":kind})
        return action
    def approve(self, action_id, customer, expected, action_hash, challenge):
        with self.store.tx() as c:
            a=row(c,actions,actions.c.id==action_id)
            if not a or a["customer_id"]!=customer: fail(404,"Action not found")
            case=self.get(c,a["case_id"],customer)
            if a["payload_hash"]!=action_hash or a["challenge_id"]!=challenge: fail(409,"Approval does not match the reviewed action")
            if a["status"] in {"queued","dispatching","succeeded","unknown","direct_customer_action_required"}: return a
            self.check_version(case,expected)
            if a["status"]!="draft" or a["expires_at"]<=self.store.now(): fail(409,"Approval expired or is no longer valid; prepare a fresh action")
            approval_id=uid("approval")
            c.execute(approvals.insert().values(id=approval_id,action_id=a["id"],approver=customer,payload_hash=action_hash,expires_at=a["expires_at"]))
            c.execute(actions.update().where(actions.c.id==a["id"]).values(status="queued",approval_id=approval_id))
            c.execute(jobs.insert().values(id=uid("job"),action_id=a["id"],status="pending",priority={"lock":0,"report_lost":1,"report":2,"response":2,"replacement":3}[a["kind"]],attempts=0))
            if a["kind"]=="lock": instrument(case,a["payload"]["instrument_id"])["protection_status"]="pending"
            self.save(c,case,"action.approved",customer,{"action_id":a["id"],"payload_hash":action_hash})
        return {**a,"status":"queued","approval_id":approval_id}
    def revoke(self, action_id, customer):
        with self.store.tx() as c:
            a=row(c,actions,actions.c.id==action_id)
            if not a or a["customer_id"]!=customer: fail(404,"Action not found")
            if a["status"] not in {"draft","queued"}: fail(409,"Already submitted; a provider action cannot be undone here")
            case=self.get(c,a["case_id"],customer)
            c.execute(approvals.update().where(approvals.c.action_id==a["id"]).values(revoked_at=self.store.now()))
            c.execute(actions.update().where(actions.c.id==a["id"]).values(status="revoked"))
            c.execute(jobs.update().where(jobs.c.action_id==a["id"]).values(status="cancelled"))
            if a["kind"]=="lock" and instrument(case,a["payload"]["instrument_id"])["protection_status"] not in PROTECTED: instrument(case,a["payload"]["instrument_id"])["protection_status"]="unprotected"
            self.save(c,case,"action.revoked",customer,{"action_id":a["id"]})
        return {"status":"revoked"}
