from typing import Protocol
from pydantic import Field
from app.api.schemas import Model
from app.domain.scope import check_profile
from app.domain.tax import load_rules
from app.workflows.engine import now

class ToolArguments(Model):
    case_id: str
    expected_case_version: int | None = Field(default=None, ge=1)
    facts_hash: str | None = None
    rule_pack_id: str | None = None

TOOLS = {
    'check_supported_profile': 'Check explicit profile answers and list missing information.',
    'reconcile_tax_documents': 'Reconcile confirmed source forms; cannot attest completeness for the customer.',
    'calculate_federal_return': 'Calculate from a pinned rule pack and immutable fact hash.',
    'prepare_return_package': 'Prepare a review package without approving or submitting it.',
    'get_filing_and_refund_status': 'Read separate submission, acceptance and financial evidence.',
}

class StructuredToolCaller(Protocol):
    """Optional model integration boundary. The shipped implementation is rules-only."""
    def choose_tool(self, context: dict, tools: list[dict]) -> dict: ...


def call_tool(engine, tenant, name, args):
    case = engine.get(tenant, args.case_id)
    if name not in TOOLS:
        raise ValueError('Tool is not available; approval and submission are never agent tools')
    if name == 'check_supported_profile':
        result = check_profile(case['profile'])
    elif name == 'get_filing_and_refund_status':
        result = case['filing']
    else:
        if args.expected_case_version is None:
            raise ValueError('A mutating tool requires expected_case_version')
        if name == 'reconcile_tax_documents':
            result = engine.reconcile(tenant, case['id'], args.expected_case_version, case['completeness_confirmed'])
        elif name == 'calculate_federal_return':
            if not args.facts_hash or not args.rule_pack_id:
                raise ValueError('Calculation requires facts_hash and rule_pack_id')
            result = engine.calculate(tenant, case['id'], args.expected_case_version, args.rule_pack_id, args.facts_hash)
        else:
            result = engine.prepare(tenant, case['id'], args.expected_case_version)
        # Never expose approval challenges or authority tokens to the model.
        result.pop('actions', None)
    return {'source': 'Tax-Season Concierge deterministic tools', 'retrieved_at': now(), 'authority': 'simulated', 'environment': 'mock', 'result': result}
