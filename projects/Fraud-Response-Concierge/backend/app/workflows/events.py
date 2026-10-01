from __future__ import annotations
from backend.app.persistence.store import *
from backend.app.domain.engine import *

class EventProcessor:
    def __init__(self,store): self.store=store; self.service=Service(store)
    def apply(self,event):
        with self.store.tx() as c:
            old=row(c,inbox,(inbox.c.provider_id==event["provider_id"])&(inbox.c.event_id==event["event_id"]))
            if old:
                if digest(old["payload"])!=digest(event): fail(409,"Provider event ID reused with different content")
                return {"duplicate":True}
            case=self.service.get(c,event["case_id"],event["customer_id"])
            kind=event["type"]; inst=instrument(case,event["instrument_id"])
            if inst["provider_id"]!=event["provider_id"] or event["environment"]!="mock": fail(422,"Provider event scope mismatch")
            if parse(event["occurred_at"])>parse(self.store.now()): fail(422,"Event timestamp is in the future")
            if kind=="replacement_delivered":
                if inst["replacement_status"]!="ordered" or inst.get("replacement_reference")!=event["provider_reference"]: fail(409,"Replacement reference does not match an ordered replacement")
                inst.update(replacement_status="delivered",replacement_delivery_event_id=event["event_id"])
            else:
                t=transaction(case,event["transaction_id"])
                if t["instrument_id"]!=inst["id"] or t.get("provider_case_ref")!=event["provider_reference"]: fail(422,"Investigation reference does not match the transaction")
                if event["revision"]<=t.get("provider_revision",0): fail(409,"Stale provider revision")
                if t["investigation_status"] in TERMINAL_TX and kind!="credit_reversed": fail(409,"Final outcomes can only reopen through a credit reversal")
                if kind=="provisional_credit":
                    t["credit"]["provisional_minor"]=t["amount_minor"]
                    t["investigation_status"]="provider_investigating"
                elif kind in {"resolved_customer_favor","resolved_other"}:
                    t.update(investigation_status=kind,evidence_status="provider_determined",provider_determination="unauthorized" if kind=="resolved_customer_favor" else "not_upheld",outcome_event_id=event["event_id"],outstanding_requirements=[])
                    t["credit"]["provisional_minor"]=0
                    t["credit"]["final_minor"]=t["amount_minor"] if kind=="resolved_customer_favor" else 0
                elif kind=="credit_reversed":
                    credit=t["credit"]
                    if not credit["provisional_minor"] and not credit["final_minor"]: fail(409,"No credit exists to reverse")
                    credit["reversed_minor"]+=credit["provisional_minor"]+credit["final_minor"]
                    credit.update(provisional_minor=0,final_minor=0)
                    t.update(investigation_status="provider_investigating",evidence_status="provider_investigating",provider_determination=None,outcome_event_id=None)
                elif kind=="information_required":
                    t.update(investigation_status="information_required",outstanding_requirements=["Confirm whether the physical card was with you at the time of the transaction."])
                else: fail(422,"Unknown provider event")
                t["provider_revision"]=event["revision"]
            c.execute(inbox.insert().values(id=uid("inbox"),provider_id=event["provider_id"],event_id=event["event_id"],case_id=case["id"],payload=event))
            self.service.save(c,case,"provider."+kind,event["provider_id"],{"provider_event_id":event["event_id"],"provider_reference":event["provider_reference"],"transaction_id":event.get("transaction_id"),"environment":"mock"})
        return {"duplicate":False,"version":case["version"]}
