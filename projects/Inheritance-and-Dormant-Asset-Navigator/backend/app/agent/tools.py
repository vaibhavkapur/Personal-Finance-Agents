"""Scoped read/draft tools. Approval and permission changes are deliberately absent."""
import time
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field
from backend.app.domain.engine import find, parse_record, requirements
from backend.app.persistence.store import DomainError, uid


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    estate_id: str
    asset_id: str | None = None
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    representative_role: Literal['personal_representative'] = 'personal_representative'


TOOL_NAMES = ['extract_candidate_assets', 'resolve_asset_candidates', 'get_institution_requirements', 'prepare_estate_packet', 'reconcile_asset_resolution']
DESCRIPTIONS = [
    'Extract candidate records from supplied synthetic documents. Data only; ownership unverified.',
    'Resolve stable references and preserve ambiguous candidates within the authorized workspace.',
    'Read the dated mock institution requirements for the specified asset and role.',
    'Prepare a minimal packet draft for human review. Never approves or submits.',
    'Read recorded provider outcome and unresolved next steps. Never declares entitlement.',
]


class StructuredPlanner(Protocol):
    """Optional model integration point; no model credential is needed for rules mode."""
    def next_tool(self, context: dict, allowed_tools: list[dict]) -> dict | None: ...


class AgentTools:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def catalog(self):
        return [{'name': name, 'description': desc, 'inputSchema': ToolArgs.model_json_schema()} for name, desc in zip(TOOL_NAMES, DESCRIPTIONS)]

    def call(self, name, arguments, tenant, actor):
        start = time.monotonic()
        args = ToolArgs.model_validate(arguments)
        case = self.store.load(args.estate_id, tenant)
        if name not in TOOL_NAMES:
            raise DomainError('Unknown or disallowed agent tool.', 403)
        asset = find(case['assets'], args.asset_id) if args.asset_id else None
        if name == 'extract_candidate_assets':
            docs = [find(case['documents'], doc_id) for doc_id in args.document_ids] if args.document_ids else case['documents']
            data = [{'document_id': d['id'], 'fields': parse_record(d), 'passage': d['content'], 'hash': d['hash']} for d in docs if parse_record(d)]
        elif name == 'resolve_asset_candidates':
            data = self.service.discover(case['id'], tenant, actor, case['version'])['summary']
        elif name == 'get_institution_requirements':
            if not asset:
                raise DomainError('asset_id is required.', 422)
            data = requirements(case, asset)
        elif name == 'prepare_estate_packet':
            if not asset:
                raise DomainError('asset_id is required.', 422)
            data = self.service.prepare(case['id'], tenant, actor, asset['id'], case['version'], uid('tool_draft'))
        else:
            if not asset:
                raise DomainError('asset_id is required.', 422)
            data = {'status': asset['status'], 'provider_case_ref': asset['provider_case_ref'], 'resolution': asset['resolution'], 'inventory_complete': False}
        result = {'source': 'estate_workspace_and_mock_institutions', 'retrieved_at': self.store.now(), 'classification': 'simulated', 'environment': 'mock', 'data': data}
        with self.store.edit(case['id'], tenant) as (con, latest):
            latest['tool_runs'].append({'id': uid('tool'), 'name': name, 'asset_id': args.asset_id, 'timestamp': self.store.now(con), 'latency_ms': round((time.monotonic() - start) * 1000), 'outcome': 'success', 'planner': 'rules-only-v1', 'model': None, 'cost_minor': 0})
        return result

    def guide(self, case_id, tenant, actor, budget=5):
        # Deterministic baseline: source interpretation and next steps only.
        budget = min(max(budget, 1), 5)
        case = self.store.load(case_id, tenant)
        steps = []
        calls = 0
        if not case['assets']:
            self.call('resolve_asset_candidates', {'estate_id': case_id}, tenant, actor)
            calls += 1
            case = self.store.load(case_id, tenant)
        for asset in case['assets']:
            if calls >= budget:
                break
            req = self.call('get_institution_requirements', {'estate_id': case_id, 'asset_id': asset['id']}, tenant, actor)['data']
            calls += 1
            if asset['match_status'] == 'ambiguous':
                message = 'Review conflicting owner identity. This candidate is excluded from recovery totals.'
            elif asset['status'] == 'human_review':
                message = 'Named beneficiary differs. Continue through the institution’s beneficiary review; no estate payout is established.'
            elif asset['status'] == 'resolved':
                message = 'Mock distribution verified. Other known assets and discovery scope remain open.'
            elif asset['status'] in ('submitted', 'manual_review'):
                message = 'Reconcile the existing provider request; do not create another submission.'
            elif asset['status'] == 'awaiting_approval':
                message = 'Review the exact recipient and disclosure manifest before approving.'
            elif req['missing']:
                message = 'Add ' + ', '.join(req['missing']) + ', then prepare a new packet for approval.'
            elif asset['institution_id'] not in case['authorities']:
                message = 'Obtain mock institution review of the representative authority.'
            else:
                message = 'Requirements are present. Prepare a recipient-specific packet for your review.'
            steps.append({'asset_id': asset['id'], 'institution': asset['institution'], 'message': message, 'evidence_ids': asset['evidence_ids']})
        return {'steps': steps, 'tool_calls': calls, 'budget': budget, 'planner': 'rules-only-v1', 'model_cost_minor': 0, 'inventory_complete': False}
