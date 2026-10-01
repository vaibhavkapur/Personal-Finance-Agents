import os
import secrets
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from backend.app.adapters.mock import MockAdapter
from backend.app.persistence.store import Store

app = FastAPI(title='Independent synthetic estate institution simulator')
adapter = MockAdapter(Store())


def authorize(authorization):
    token = os.getenv('MOCK_PROVIDER_TOKEN')
    if not token or not secrets.compare_digest(authorization or '', 'Bearer ' + token):
        raise HTTPException(401, 'Provider token required.')


class Submission(BaseModel):
    packet: dict
    request_ref: str


@app.post('/submit')
def submit(body: Submission, authorization: str | None = Header(default=None)):
    authorize(authorization)
    try:
        return adapter.submit_claim(body.packet, body.request_ref)
    except TimeoutError:
        raise HTTPException(504, 'Accepted, response unavailable; look up original request.')


@app.get('/requests/{request_ref}')
def lookup(request_ref: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    result = adapter.lookup_request(request_ref)
    if result is None:
        raise HTTPException(404, 'Not found')
    return result


@app.get('/resolutions/{case_ref}')
def resolution(case_ref: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    result = adapter.get_resolution(case_ref)
    if result is None:
        raise HTTPException(404, 'Not found')
    return result
