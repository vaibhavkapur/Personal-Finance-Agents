"""All amounts use minor units. Rates are target major units / source major unit."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
from datetime import datetime, timedelta
from uuid import uuid4

PROVIDERS = {
    'swift': {'id': 'swift', 'name': 'SwiftSend', 'fee_minor': 500, 'rate': '83.20', 'hours': [1, 4], 'deduction_minor': 0, 'default_scenario': 'normal', 'description': 'The express route'},
    'bridge': {'id': 'bridge', 'name': 'BridgeWay', 'fee_minor': 299, 'rate': '83.65', 'hours': [4, 12], 'deduction_minor': 0, 'default_scenario': 'missing_document', 'description': 'More rupees, a little more time'},
    'lotus': {'id': 'lotus', 'name': 'Lotus Remit', 'fee_minor': 199, 'rate': '83.85', 'hours': [12, 24], 'deduction_minor': 15000, 'default_scenario': 'timeout', 'description': 'A considered, lower-fee option'},
}

def normalize(provider, intent, now):
    rate = Decimal(provider['rate'])
    fee = provider['fee_minor']
    deduction = provider['deduction_minor']
    if intent['budget_mode'] == 'total_sender_cost':
        total = intent['source_budget_minor']
        principal = total - fee
    else:
        principal = int((Decimal(intent['target_required_minor'] + deduction) / rate).quantize(Decimal('1'), rounding=ROUND_CEILING))
        total = principal + fee
    gross = int((Decimal(principal) * rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    lower, upper = [now + timedelta(hours=h) for h in provider['hours']]
    supported = 1000 <= total <= 1000000 and principal > 0
    return {'id': 'quote_' + uuid4().hex[:16], 'provider_id': provider['id'], 'provider_name': provider['name'],
            'case_id': intent['id'], 'source_currency': 'USD', 'target_currency': 'INR',
            'source_total_minor': total, 'fee_minor': fee, 'source_principal_minor': principal,
            'effective_rate_decimal': provider['rate'], 'rate_direction': 'INR_per_USD',
            'recipient_minor': gross - deduction, 'gross_recipient_minor': gross,
            'known_deduction_minor': deduction, 'unknown_deductions': False,
            'original_payload': {'amount_basis': intent['budget_mode'], 'send_cents': principal, 'fee_cents': fee, 'fx_rate': provider['rate'], 'payout_deduction_paise': deduction},
            'valid_until': (now + timedelta(minutes=15)).isoformat(), 'retrieved_at': now.isoformat(),
            'delivery_window': {'earliest': lower.isoformat(), 'latest': upper.isoformat(), 'hours': provider['hours'], 'guaranteed': False},
            'supported': supported, 'meets_deadline': upper <= datetime.fromisoformat(intent['deadline_at']),
            'environment': 'mock', 'authority': 'simulated', 'rounding': 'half_up_once',
            'source': 'mock:' + provider['id']}


def compare(quotes, intent, now):
    values = []
    for q in quotes:
        q = dict(q)
        q['expired'] = datetime.fromisoformat(q['valid_until']) <= now
        q['eligible'] = q['supported'] and q['meets_deadline'] and not q['expired']
        q['reason'] = ('Quote expired. Refresh the comparison.' if q['expired'] else
                       'Amount is outside provider limits.' if not q['supported'] else
                       'Estimated delivery extends past your deadline.' if not q['meets_deadline'] else
                       'Estimated delivery fits your deadline; it is not guaranteed.')
        values.append(q)
    metric = (lambda q: -q['recipient_minor']) if intent['budget_mode'] == 'total_sender_cost' else (lambda q: q['source_total_minor'])
    values.sort(key=lambda q: (not q['eligible'], q['unknown_deductions'], metric(q), q['delivery_window']['latest']))
    best = next((q for q in values if q['eligible']), None)
    for q in values:
        q['recommended'] = best is not None and q['id'] == best['id']
    return {'quotes': values, 'recommended_quote_id': best['id'] if best else None,
            'explanation': ('Best recipient amount within your estimated deadline.' if intent['budget_mode'] == 'total_sender_cost' else 'Lowest total debit that meets your recipient target and estimated deadline.') if best else 'No supported option currently meets your deadline. Adjust the deadline or refresh expired quotes.',
            'environment': 'mock', 'retrieved_at': now.isoformat(), 'source': 'deterministic_quote_comparison', 'authority': 'simulated'}
