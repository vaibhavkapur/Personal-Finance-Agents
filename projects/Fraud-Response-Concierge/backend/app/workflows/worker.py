from __future__ import annotations
import asyncio, time
from datetime import timedelta, datetime, timezone
from sqlalchemy import or_, and_
from backend.app.persistence.store import *
from backend.app.domain.engine import Service, instrument, transaction, digest, fail
from backend.app.domain.registry import PROVIDERS
from backend.app.adapters.banks import adapter

class Worker:
    def __init__(self,store,bank=None): self.store=store; self.service=Service(store); self.bank=bank or adapter(store)
    def claim(self, action_id=None):
        now=iso(datetime.now(timezone.utc))
        with self.store.tx() as c:
            eligible=or_(jobs.c.status=="pending",and_(jobs.c.status=="running",jobs.c.lease_until<now))
            stmt=select(jobs).where(eligible)
            if action_id: stmt=stmt.where(jobs.c.action_id==action_id)
            j=c.execute(stmt.order_by(jobs.c.priority,jobs.c.id).limit(1)).mappings().first()
            if not j: return None
            result=c.execute(jobs.update().where(jobs.c.id==j["id"],eligible).values(status="running",lease_until=iso(datetime.now(timezone.utc)+timedelta(seconds=30)),attempts=j["attempts"]+1))
            return dict(j) if result.rowcount else None
    async def run_one(self, action_id=None):
        job=self.claim(action_id)
        if not job: return False
        with self.store.tx() as c:
            a=row(c,actions,actions.c.id==job["action_id"])
            approval=row(c,approvals,approvals.c.id==a["approval_id"])
            case=self.service.get(c,a["case_id"],a["customer_id"])
            if a["status"] in {"invalidated","revoked","succeeded"}:
                c.execute(jobs.update().where(jobs.c.id==job["id"]).values(status="done")); return True
            valid=(approval and not approval["revoked_at"] and approval["approver"]==case["customer_id"] and approval["payload_hash"]==digest(a["payload"]) and a["payload"]["customer_id"]==case["customer_id"] and case["data"]["identity_verified"])
            if not valid or (not approval["consumed_at"] and approval["expires_at"]<=self.store.now()):
                c.execute(actions.update().where(actions.c.id==a["id"]).values(status="revoked"))
                c.execute(jobs.update().where(jobs.c.id==job["id"]).values(status="cancelled"))
                if a["kind"]=="lock" and instrument(case,a["payload"]["instrument_id"])["protection_status"] not in {"temporarily_locked","lost_reported"}: instrument(case,a["payload"]["instrument_id"])["protection_status"]="unprotected"
                self.service.save(c,case,"approval.rejected_by_executor","worker",{"action_id":a["id"]})
                return True
            # A consumed approval permits lookup/reconciliation only, never a new write.
            lookup_only=bool(approval["consumed_at"])
            if not lookup_only:
                c.execute(approvals.update().where(approvals.c.id==approval["id"]).values(consumed_at=self.store.now()))
                c.execute(actions.update().where(actions.c.id==a["id"]).values(status="dispatching"))
                self.service.save(c,case,"action.submitting","worker",{"action_id":a["id"]})
        try:
            # No database transaction spans these provider calls.
            found=await self.bank.find_action(a["id"])
            if found is None and lookup_only:
                self.finish(a,job,"manual_review",{"reason":"Provider cannot confirm the prior request. No write was retried."}); return True
            if found is None:
                send=self.bank.protect_instrument if a["kind"] in {"lock","report_lost"} else self.bank.submit_report
                result=await send(a["payload"],a["id"])
                # Acknowledgments are insufficient: query the provider record.
                if "provider_reference" not in result: raise ValueError("Malformed provider response")
                result=await self.bank.find_action(a["id"])
            else: result=found
            required={"provider_reference","provider_id","customer_id","instrument_id","request_ref","status","environment"}
            if not result or not required<=result.keys(): raise ValueError("Malformed provider response")
            if any(result[k]!=a["payload"][k] for k in ["provider_id","customer_id","instrument_id"]) or result["request_ref"]!=a["id"] or result["environment"]!="mock" or result.get("transaction_id")!=a["payload"].get("transaction_id"):
                raise ValueError("Provider response scope mismatch")
            status=result["status"]
            if status=="verified":
                if a["kind"] in {"lock","report_lost"} and result.get("instrument_state") not in {"temporarily_locked","lost_reported"}: raise ValueError("Instrument protection was not verified")
                if a["kind"] in {"report","response"} and result.get("case_status")!="provider_investigating": raise ValueError("Report status was not verified")
                if a["kind"]=="replacement" and result.get("replacement_status")!="ordered": raise ValueError("Replacement was not verified")
                self.finish(a,job,"succeeded",result)
            elif status=="direct_customer_action_required": self.finish(a,job,status,result)
            elif status=="declined": self.finish(a,job,"failed",result)
            elif status=="pending": self.finish(a,job,"unknown",result)
            else: raise ValueError("Unsupported provider status")
        except (TimeoutError, __import__('httpx').TimeoutException, __import__('httpx').HTTPError):
            self.finish(a,job,"unknown",{"reason":"Provider outcome is unknown. Reconcile the original request before any retry."})
        except ValueError:
            self.finish(a,job,"manual_review",{"reason":"Provider response could not be verified. Review the original request."})
        return True
    def finish(self,a,job,status,result):
        with self.store.tx() as c:
            current=row(c,actions,actions.c.id==a["id"])
            if current["status"]=="succeeded": return
            case=self.service.get(c,a["case_id"],a["customer_id"])
            c.execute(actions.update().where(actions.c.id==a["id"]).values(status=status,result=result,provider_ref=result.get("provider_reference")))
            retry=status=="unknown" and job["attempts"]<4
            c.execute(jobs.update().where(jobs.c.id==job["id"]).values(status="running" if retry else "done",lease_until=iso(datetime.now(timezone.utc)+timedelta(seconds=min(2**job["attempts"],30))),last_error=result.get("reason")))
            inst=instrument(case,a["payload"]["instrument_id"])
            if a["kind"] in {"lock","report_lost"}:
                if status=="succeeded":
                    if inst["protection_status"]!="lost_reported" or result["instrument_state"]=="lost_reported":
                        inst.update(protection_status=result["instrument_state"],protection_reference=result["provider_reference"],verified_at=self.store.now())
                    if a["kind"]=="report_lost": inst["lost_report_status"]="reported"
                elif inst["protection_status"] not in {"temporarily_locked","lost_reported"}:
                    inst["protection_status"]={"failed":"unprotected","manual_review":"manual_review"}.get(status,status)
            if a["kind"]=="replacement" and status=="succeeded":
                inst.update(replacement_status="ordered",replacement_reference=result["provider_reference"])
                if not any(t["instrument_id"]==inst["id"] for t in case["data"]["tasks"]):
                    case["data"]["tasks"].append({"id":uid("task"),"instrument_id":inst["id"],"task_type":"recurring_payments","label":"Review recurring payments for "+inst["masked_identifier"],"owner":"customer","due_at":iso(parse(self.store.now())+timedelta(days=7)),"status":"pending","completion_evidence_id":None})
            if a["kind"]=="report" and status=="succeeded":
                tx=transaction(case,a["payload"]["transaction_id"]); p=PROVIDERS[tx["provider_id"]]
                tx.update(evidence_status="provider_investigating",investigation_status="provider_investigating",provider_case_ref=result["provider_reference"],deadline_at=iso(parse(case["discovered_at"])+timedelta(days=p["deadline_days"])),deadline_source=p["source"],deadline_guidance=p["guidance"],provider_revision=0,outstanding_requirements=[])
            if a["kind"]=="response" and status=="succeeded":
                tx=transaction(case,a["payload"]["transaction_id"])
                tx.setdefault("supplemental_evidence",[]).append({"action_id":a["id"],"statement":a["payload"]["response_statement"],"provider_reference":result["provider_reference"]})
                if tx["investigation_status"]=="information_required" and tx.get("provider_revision",0)==a["payload"]["provider_revision"]:
                    tx.update(investigation_status="provider_investigating",outstanding_requirements=[])
            self.service.save(c,case,"action."+status,"worker",{"action_id":a["id"],"kind":a["kind"],"provider_reference":result.get("provider_reference"),"reason":result.get("reason"),"environment":"mock"})
    async def reconcile(self,action_id,customer):
        with self.store.tx() as c:
            a=row(c,actions,actions.c.id==action_id)
            if not a or a["customer_id"]!=customer: fail(404,"Action not found")
            if a["status"] not in {"unknown","manual_review","direct_customer_action_required"}: fail(409,"Action is not awaiting reconciliation")
            j=row(c,jobs,jobs.c.action_id==action_id)
            if j["status"]=="running" and j["lease_until"]>iso(datetime.now(timezone.utc)):
                # Reconciliation must not steal an active worker lease.
                return {"status":"scheduled","detail":"The worker will reconcile after its current lease."}
            c.execute(jobs.update().where(jobs.c.action_id==action_id).values(status="pending"))
        await self.run_one(action_id)
        return {"status":"reconciled"}

async def main():
    worker=Worker(Store())
    while True:
        try:
            if not await worker.run_one(): await asyncio.sleep(1)
        except Exception as exc:
            print("Worker task deferred:",type(exc).__name__,flush=True)
            await asyncio.sleep(2)
if __name__=="__main__": asyncio.run(main())
