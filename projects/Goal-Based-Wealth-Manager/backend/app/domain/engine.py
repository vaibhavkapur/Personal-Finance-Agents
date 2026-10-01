"""Pure financial functions. Fiat is USD cents, quantities are millionths."""
import calendar
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

CLASSES = ('equity', 'bonds', 'cash')
SCENARIOS = {
    'flat_return_fixture': {'name': 'Flat return', 'return_bps': 0, 'shock_bps': 0, 'inflation_bps': 0, 'cost_minor': 0,
                            'description': '0% return, 0% inflation, $0 costs. Monthly deposits at month end.'},
    'stress_fixture': {'name': 'Market stress', 'return_bps': 0, 'shock_bps': -2000, 'inflation_bps': 0, 'cost_minor': 0,
                       'description': 'An immediate 20% decline in invested assets; cash unchanged. Then 0% return, 0% inflation, $0 costs.'},
}

class RuleError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def require(condition, message, status=422):
    if not condition:
        raise RuleError(message, status)


def horizon(as_of, target):
    try:
        start, end = date.fromisoformat(as_of[:10]), date.fromisoformat(target)
    except (ValueError, TypeError):
        raise RuleError('Choose a valid target date in YYYY-MM-DD format.')
    require(end > start, 'Choose a target date after the fixture clock.')
    months = (end.year-start.year)*12 + end.month-start.month
    # Count completed monthly deposits; handle month-end anniversaries.
    if end.day < min(start.day, calendar.monthrange(end.year, end.month)[1]):
        months -= 1
    require(months > 0, 'At least one monthly contribution date is required.')
    return months


def holding_value(holding):
    return int((Decimal(holding['quantity_micro']) * holding['price_minor'] / 1000000).to_integral_value(rounding=ROUND_FLOOR))


def account_values(account):
    values = {c: 0 for c in CLASSES}
    values['cash'] = account['cash_minor']
    for holding in account['holdings']:
        values[holding['asset_class']] += holding_value(holding)
    return values


def goal_values(state, goal_id):
    values = {c: 0 for c in CLASSES}
    for account in state['accounts']:
        if account['goal_id'] == goal_id:
            for key, value in account_values(account).items():
                values[key] += value
    return values


def calculate(state, goal_id, scenario_id='flat_return_fixture', target_date=None, monthly_minor=None):
    require(scenario_id in SCENARIOS, 'Unknown scenario.')
    goal = next((g for g in state['goals'] if g['id'] == goal_id), None)
    require(goal is not None, 'Goal not found.', 404)
    values = goal_values(state, goal_id)
    s = SCENARIOS[scenario_id]
    months = horizon(state['clock'], target_date or goal['target_date'])
    current = sum(values.values())
    shock = int(Decimal(current-values['cash']) * Decimal(s['shock_bps']) / 10000)
    adjusted = current + shock
    gap = max(0, goal['target_minor'] - adjusted)
    required = int((Decimal(gap)/months).to_integral_value(rounding=ROUND_CEILING))
    monthly = goal['monthly_minor'] if monthly_minor is None else monthly_minor
    projected = adjusted + months*monthly
    return {'goal_id': goal_id, 'currency': 'USD', 'current_minor': current, 'adjusted_minor': adjusted,
            'target_minor': goal['target_minor'], 'target_date': target_date or goal['target_date'],
            'months': months, 'gap_minor': gap, 'required_monthly_minor': required, 'monthly_minor': monthly,
            'projected_minor': projected, 'shortfall_minor': max(0, goal['target_minor']-projected),
            'scenario_id': scenario_id, 'assumptions': s, 'calculation_version': '1.0.0', 'as_of': state['clock'],
            'funded_percent': round(current/goal['target_minor']*100, 1), 'environment': 'mock'}


def validate_allocations(state):
    ids = [a['id'] for a in state['accounts']]
    require(len(ids) == len(set(ids)), 'An account cannot fund two goals twice.')
    goal_ids = {g['id'] for g in state['goals']}
    for account in state['accounts']:
        require(account['goal_id'] in goal_ids, 'Every account needs a unique goal allocation.')
        require(account['cash_minor'] >= 0, 'Cash cannot be negative.')
        goal = next(g for g in state['goals'] if g['id'] == account['goal_id'])
        require(not (goal['id'] == 'house' and account['restricted']), 'Retirement assets cannot fund the house.')
        require(all(h['quantity_micro'] >= 0 for h in account['holdings']), 'Holdings cannot be negative.')


def validate_prices(state):
    now = datetime.fromisoformat(state['clock'])
    for account in state['accounts']:
        for h in account['holdings']:
            require(h['price_minor'] > 0, 'A price is missing.')
            age = (now-datetime.fromisoformat(h['price_at'])).total_seconds()
            require(0 <= age <= 86400, 'Prices are stale. Refresh fixture valuations before planning.', 409)


def validate_mandate(state):
    mandate = state['mandate']
    require(not mandate['revoked'], 'The allocation mandate is revoked.', 409)
    require(mandate['approved_at'] is not None, 'An approved allocation mandate is required.', 409)
    require(state['clock'] < mandate['expires_at'], 'The allocation mandate has expired.', 409)
    require(sum(mandate['weights_bps'].values()) == 10000, 'Allocation weights must sum to 100%.')


def plan_basket(state, contribution_minor, account_id='taxable'):
    validate_allocations(state)
    validate_prices(state)
    validate_mandate(state)
    account = next((a for a in state['accounts'] if a['id'] == account_id), None)
    require(account is not None, 'Account not found.', 404)
    require(not account['restricted'], 'This account is proposal-only. Retirement transactions are unsupported.')
    require(0 <= contribution_minor <= 10000000, 'Contribution must be between $0 and $100,000.')
    values = account_values(account)
    total = sum(values.values()) + contribution_minor
    mandate = state['mandate']
    target = {c: total * mandate['weights_bps'][c] // 10000 for c in CLASSES}
    cash = account['cash_minor'] + contribution_minor
    cash_reserve = max(mandate['cash_floor_minor'], target['cash'])
    require(cash >= mandate['cash_floor_minor'], 'The cash floor cannot be met with this contribution.')
    available = max(0, cash-cash_reserve)
    actions = []
    if contribution_minor:
        actions.append({'type': 'contribution', 'account_id': account_id, 'amount_minor': contribution_minor,
                        'currency': 'USD', 'source': 'Synthetic checking ••2048', 'destination': account['name']})
    after = dict(values)
    after['cash'] += contribution_minor
    for cls in sorted(('equity', 'bonds'), key=lambda c: target[c]-values[c], reverse=True):
        holding = next(h for h in account['holdings'] if h['asset_class'] == cls)
        spend = min(available, max(0, target[cls]-values[cls]))
        # Whole fixture shares. Any rounding remainder stays in cash.
        quantity = spend // holding['price_minor'] * 1000000
        actual = quantity//1000000 * holding['price_minor']
        if actual:
            actions.append({'type': 'buy', 'account_id': account_id, 'instrument_id': holding['instrument_id'],
                            'asset_class': cls, 'quantity_micro': quantity, 'price_minor': holding['price_minor'],
                            'amount_minor': actual, 'currency': 'USD', 'destination': account['name']})
            after[cls] += actual
            after['cash'] -= actual
            available -= actual
    return {'actions': actions, 'before_values': values, 'after_values': after, 'total_minor': total,
            'after_weights_bps': {c: round(after[c]*10000/total) for c in CLASSES},
            'cash_floor_minor': mandate['cash_floor_minor'], 'tax_impact': 'unknown',
            'rounding': 'Whole fixture shares; residual USD cents remain in cash.',
            'unsupported': ['Taxable sales need additional review; no sale will execute.', 'Retirement withdrawals are unsupported.'],
            'price_tolerance_bps': 0, 'environment': 'mock'}
