from __future__ import annotations
from typing import Protocol
from datetime import timedelta
import os
import httpx
from backend.app.persistence.store import *
from backend.app.domain.engine import digest, fail
from backend.app.domain.registry import PROVIDERS, INSTRUMENTS

class IncidentBankAdapter(Protocol):
    async def get_capabilities(self, instrument_ref: str) -> dict: ...
    async def protect_instrument(self, action: dict, request_ref: str) -> dict: ...
    async def submit_report(self, report: dict, request_ref: str) -> dict: ...
    async def get_case(self, provider_case_ref: str) -> dict: ...
    async def find_action(self, request_ref: str) -> dict | None: ...

class MockBankAdapter:
    """Durable synthetic provider ledger; each operation owns its own transaction."""
    def __init__(self, store): self.store=store
    async def get_capabilities(self, instrument_ref):
        return next(i["capabilities"] for i in INSTRUMENTS if i["id"]==instrument_ref)
    async def find_action(self, request_ref):
        with self.store.tx() as c:
            record=row(c,provider_actions,provider_actions.c.request_ref==request_ref)
            if not record: return None
            result=record["result"]
            if result.get("ready_at") and result["ready_at"]<=self.store.now():
                result.update(status="verified",instrument_state="temporarily_locked")
                result.pop("ready_at")
                c.execute(provider_actions.update().where(provider_actions.c.request_ref==request_ref).values(result=result))
            return result
    async def get_case(self, provider_case_ref):
        with self.store.tx() as c:
            return next((r["result"] for r in rows(c,provider_actions) if r["result"]["provider_reference"]==provider_case_ref), {})
    async def protect_instrument(self, action, request_ref): return await self.submit_report(action,request_ref)
    async def submit_report(self, action, request_ref):
        # A retry with the same request reference can only return the original result.
        previous=await self.find_action(request_ref)
        if previous:
            with self.store.tx() as c:
                if row(c,provider_actions,provider_actions.c.request_ref==request_ref)["payload_hash"]!=digest(action): fail(409,"Provider idempotency mismatch")
            return previous
        with self.store.tx() as c:
            case=row(c,cases,cases.c.id==action["case_id"])
            scenario=case["data"]["scenario"]
        kind=action["kind"]
        result={"environment":"mock","source":PROVIDERS[action["provider_id"]]["channel"],"retrieved_at":self.store.now(),"authority":"simulated","provider_id":action["provider_id"],"customer_id":action["customer_id"],"instrument_id":action["instrument_id"],"transaction_id":action.get("transaction_id"),"request_ref":request_ref,"provider_reference":"mock_"+request_ref,"status":"verified","instrument_state":"temporarily_locked"}
        if kind=="report_lost": result["instrument_state"]="lost_reported"
        if kind in {"report","response"}: result["case_status"]="provider_investigating"
        if kind=="response": result["provider_reference"]=action["provider_case_ref"]
        if kind=="replacement": result["replacement_status"]="ordered"
        if scenario=="declined": result.update(status="declined",reason="Simulated provider declined this action. Use its verified support channel.")
        elif kind=="lock" and action["provider_id"]=="northstar": result.update(status="direct_customer_action_required",instrument_state="unprotected",handoff_path="/handoff/northstar")
        elif scenario=="delayed" and kind=="lock": result.update(status="pending",instrument_state="unprotected",ready_at=iso(parse(self.store.now())+timedelta(minutes=5)))
        with self.store.tx() as c:
            c.execute(provider_actions.insert().values(request_ref=request_ref,provider_id=action["provider_id"],payload_hash=digest(action),result=result))
        if scenario=="timeout_after_acceptance": raise TimeoutError("Simulated timeout after provider acceptance")
        if scenario=="malformed": return {"status":"verified"}
        return result
    async def complete_handoff(self, request_ref):
        with self.store.tx() as c:
            record=row(c,provider_actions,provider_actions.c.request_ref==request_ref)
            if not record: fail(404,"Provider action not found")
            result=record["result"]
            if result["status"]!="direct_customer_action_required": fail(409,"No authentication handoff is pending")
            result.update(status="verified",instrument_state="temporarily_locked",retrieved_at=self.store.now())
            c.execute(provider_actions.update().where(provider_actions.c.request_ref==request_ref).values(result=result))
        return result

class HttpMockBankAdapter:
    """Same adapter boundary, isolated simulator process for Docker deployments."""
    def __init__(self):
        self.base=os.environ["MOCK_PROVIDER_URL"]
        self.key=os.environ["PROVIDER_SHARED_SECRET"]
    async def call(self,path,data=None):
        async with httpx.AsyncClient(timeout=5) as client:
            response=await client.post(self.base+path,json=data or {},headers={"X-Mock-Key":self.key})
            response.raise_for_status(); return response.json()
    async def get_capabilities(self,instrument_ref): return await self.call("/capabilities",{"instrument_ref":instrument_ref})
    async def find_action(self,request_ref): return await self.call("/find",{"request_ref":request_ref})
    async def get_case(self,provider_case_ref): return await self.call("/case",{"provider_case_ref":provider_case_ref})
    async def protect_instrument(self,action,request_ref): return await self.submit_report(action,request_ref)
    async def submit_report(self,report,request_ref): return await self.call("/submit",{"action":report,"request_ref":request_ref})
    async def complete_handoff(self,request_ref): return await self.call("/handoff",{"request_ref":request_ref})

def adapter(store): return HttpMockBankAdapter() if os.getenv("MOCK_PROVIDER_URL") else MockBankAdapter(store)
