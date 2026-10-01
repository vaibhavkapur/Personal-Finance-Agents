"""Small MCP 2025-06-18 tool surface. No approval or execution tool is exposed."""
import time
from pydantic import Field
from ..domain.models import StrictModel, Draft
from ..domain.lifecycle import require
from ..persistence import db as d
from ..workflows.service import uid, digest

class RecipientArgs(StrictModel):
    recipient_id: str
class CaseArgs(StrictModel):
    intent_id: str
class CompareArgs(StrictModel):
    quote_ids: list[str] = Field(min_length=1, max_length=3)
    deadline: str
class PrepareArgs(StrictModel):
    quote_id: str
    recipient_version: int
    recipient_confirmed: bool = False
    expected_case_version: int
    idempotency_key: str
class EvidenceArgs(StrictModel):
    transfer_id: str

TOOLS = {
    'validate_recipient': (RecipientArgs, 'Read masked verified beneficiary details and version.'),
    'get_transfer_quotes': (CaseArgs, 'Collect comparable simulated USD to INR quotes. No funds move.'),
    'compare_delivery_options': (CompareArgs, 'Compare current quotes against the saved intent deadline. Delivery estimates are not guarantees.'),
    'prepare_transfer': (PrepareArgs, 'Prepare an exact action for customer review. Cannot approve or initiate it.'),
    'get_transfer_evidence': (EvidenceArgs, 'Read separate funding, processing and recipient delivery evidence.'),
}

class AgentTools:
    def __init__(self, service):
        self.service = service

    def catalog(self):
        return [{'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
                 'annotations': {'readOnlyHint': name in {'validate_recipient', 'compare_delivery_options', 'get_transfer_evidence'}, 'destructiveHint': False, 'openWorldHint': False}}
                for name, (schema, description) in TOOLS.items()]

    def call(self, user, name, arguments):
        require(name in TOOLS, 'Unknown tool', 404)
        args = TOOLS[name][0].model_validate(arguments)
        start = time.monotonic()
        service = self.service
        case_id = None
        if name == 'validate_recipient':
            with service.db.tx() as conn:
                result = service.owned(conn, d.beneficiaries, args.recipient_id, user)
        elif name == 'get_transfer_quotes':
            case_id = args.intent_id
            result = service.get_quotes(user, case_id)
        elif name == 'compare_delivery_options':
            with service.db.tx() as conn:
                q = service.owned(conn, d.quotes, args.quote_ids[0], user)
                case_id = q['case_id']
                case = service.owned(conn, d.cases, case_id, user)
                require(set(args.quote_ids).issubset(case['quote_ids']), 'Quotes must belong to the current intent')
                require(args.deadline == case['deadline_at'], 'Deadline differs from the saved intent')
            result = service.snapshot(user, case_id)['comparison']
        elif name == 'prepare_transfer':
            with service.db.tx() as conn:
                quote = service.owned(conn, d.quotes, args.quote_id, user)
                case_id = quote['case_id']
            result = service.prepare_transfer(user, case_id, Draft(quote_id=args.quote_id, recipient_version=args.recipient_version,
                    recipient_confirmed=args.recipient_confirmed, expected_case_version=args.expected_case_version), args.idempotency_key)
        else:
            with service.db.tx() as conn:
                transfer = service.owned(conn, d.transfers, args.transfer_id, user)
                case_id = transfer['case_id']
            snapshot = service.snapshot(user, case_id)
            result = {k: snapshot[k] for k in ['status', 'transfer', 'receipt', 'requirements', 'events', 'deadline_risk']}
        with service.db.tx() as conn:
            run = {'id': uid('tool'), 'customer_id': user['customer_id'], 'case_id': case_id, 'tool_name': name,
                   'input_hash': digest(arguments), 'output_hash': digest(result), 'retrieved_at': service.now(conn).isoformat(),
                   'latency_ms': round((time.monotonic() - start) * 1000), 'model_version': 'rules-only-v1', 'prompt_version': '1',
                   'outcome': 'success', 'cost_usd': 0}
            d.put(conn, d.tool_runs, run)
        return {'source': name, 'retrieved_at': service.clock().isoformat(), 'authority': 'simulated', 'environment': 'mock', 'data': result}

    def respond(self, user, case_id, message, budget=5):
        require(0 < budget <= 5, 'Tool budget must be between one and five', 422)
        case = self.service.snapshot(user, case_id)
        # Untrusted chat text only chooses bounded read operations; it cannot change authority.
        if case['transfer'] and case['transfer']['status'] != 'prepared':
            self.call(user, 'get_transfer_evidence', {'transfer_id': case['transfer']['id']})
            if case['receipt']:
                text = ('Delivery is verified and all amounts match the approved quote.' if case['receipt']['reconciliation_status'] == 'matched'
                        else 'Recipient credit is recorded, but the amount differs from the quote. The case needs operator review.')
            elif case['status'] == 'information_required':
                text = 'The provider needs purpose evidence. Review the exact document packet before sharing it. Funding is not proof of recipient delivery.'
            elif case['status'] == 'outcome_unknown':
                text = 'The provider response is missing. I will look up the original request reference. A second transfer is blocked.'
            else:
                text = 'The transfer is ' + case['status'].replace('_', ' ') + '. Recipient delivery is not verified until a payout reference and final amount arrive.'
        else:
            self.call(user, 'validate_recipient', {'recipient_id': case['beneficiary_id']})
            q = case['comparison']['quotes']
            if any(word in message.lower() for word in ['guarantee', 'guaranteed']):
                text = 'These providers give estimated windows, not guaranteed delivery. No quote guarantees the deadline.'
            elif any(word in message.lower() for word in ['send now', 'approve', 'ignore', 'bypass']):
                text = 'Choose a quote, confirm the recipient, and approve the exact review screen. Chat cannot authorize a transfer.'
            elif not case['comparison']['recommended_quote_id']:
                text = case['comparison']['explanation']
            elif 'fast' in message.lower():
                fastest = min([v for v in q if v['eligible']], key=lambda v: v['delivery_window']['hours'][1])
                text = fastest['provider_name'] + ' has the earliest eligible delivery window: ' + str(fastest['delivery_window']['hours'][0]) + '–' + str(fastest['delivery_window']['hours'][1]) + ' hours, estimated.'
            else:
                best = next(v for v in q if v['recommended'])
                text = best['provider_name'] + ' is recommended. ' + case['comparison']['explanation'] + ' Fees and known recipient deductions are already included; the exchange rate includes the FX spread.'
        return {'message': text, 'tool_calls': 1, 'tool_budget': budget, 'mode': 'rules-only', 'cost_usd': 0, 'environment': 'mock'}
