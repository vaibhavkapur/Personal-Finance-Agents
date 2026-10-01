import os, hmac
from fastapi import FastAPI, Header, Depends, HTTPException
from backend.app.persistence.store import Store
from backend.app.adapters.banks import MockBankAdapter
app=FastAPI(title="Isolated mock bank")
bank=MockBankAdapter(Store())
def auth(x_mock_key:str=Header()):
    if not hmac.compare_digest(x_mock_key,os.getenv("PROVIDER_SHARED_SECRET","local-fixture-webhook-secret")): raise HTTPException(401,"Invalid simulator key")
@app.post("/submit",dependencies=[Depends(auth)])
async def submit(body:dict): return await bank.submit_report(body["action"],body["request_ref"])
@app.post("/find",dependencies=[Depends(auth)])
async def find(body:dict): return await bank.find_action(body["request_ref"])
@app.post("/capabilities",dependencies=[Depends(auth)])
async def capabilities(body:dict): return await bank.get_capabilities(body["instrument_ref"])
@app.post("/case",dependencies=[Depends(auth)])
async def case(body:dict): return await bank.get_case(body["provider_case_ref"])
@app.post("/handoff",dependencies=[Depends(auth)])
async def handoff(body:dict): return await bank.complete_handoff(body["request_ref"])
