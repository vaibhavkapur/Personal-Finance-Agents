"""Private simulator service for docker-compose. No live filing endpoints."""
import hmac
import os
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app.persistence.store import Store
from app.adapters.mock import MockTaxFilingAdapter

def authorized(authorization: str = Header(default='')):
    token = os.environ.get('MOCK_PROVIDER_TOKEN')
    if not token or not hmac.compare_digest(authorization, 'Bearer '+token):
        raise HTTPException(401, 'Provider authorization required')

class PackageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    package: dict
    request_ref: str | None = None

class EventsRequest(BaseModel):
    submission: dict
    day: int = Field(ge=0)

app = FastAPI(title='Private mock federal filing provider', dependencies=[Depends(authorized)])
adapter = MockTaxFilingAdapter(Store())

@app.post('/validate')
def validate(body: PackageRequest):
    return adapter.validate_package(body.package)

@app.post('/submissions')
def submit(body: PackageRequest):
    if not body.request_ref: raise HTTPException(422, 'A request reference is required')
    try: return adapter.submit_mock_return(body.package, body.request_ref)
    except TimeoutError: raise HTTPException(504, 'Simulated timeout after provider acceptance')

@app.get('/lookups/{ref}')
def lookup(ref: str):
    value = adapter.find_submission(ref)
    if value is None: raise HTTPException(404, 'No submission exists for this reference')
    return value

@app.get('/outcomes/{ref}')
def outcome(ref: str):
    try: return adapter.get_financial_outcome(ref)
    except KeyError: raise HTTPException(404, 'Submission not found')

@app.post('/events')
def events(body: EventsRequest):
    stored = adapter.find_submission(body.submission['request_ref'])
    if not stored: raise HTTPException(404, 'Submission not found')
    return adapter.events(stored, body.day)
