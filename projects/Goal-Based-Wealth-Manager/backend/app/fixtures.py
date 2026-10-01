from copy import deepcopy

FIXTURE_CLOCK = '2026-09-25T16:00:00+00:00'

def seed_state():
    def holding(instrument, cls, quantity):
        return {'id': instrument, 'instrument_id': instrument, 'asset_class': cls,
                'quantity_micro': quantity*1000000, 'price_minor': 10000, 'price_at': FIXTURE_CLOCK}
    return deepcopy({
        'customer_id': 'cus_demo_7', 'customer_name': 'Alex Morgan', 'currency': 'USD',
        'version': 1, 'holdings_version': 1, 'clock': FIXTURE_CLOCK,
        'case': {'id': 'wealthcase_demo_1', 'status': 'collecting', 'version': 1, 'completion_evidence': None, 'next_review_at': None},
        'goals': [
            {'id': 'house', 'name': 'A place to call home', 'kind': 'house', 'target_minor': 6000000,
             'target_date': '2028-09-25', 'original_date': '2028-09-25', 'priority': 1, 'monthly_minor': 100000,
             'version': 1, 'confirmed': True, 'account_ids': ['taxable']},
            {'id': 'retirement', 'name': 'Your next chapter', 'kind': 'retirement', 'target_minor': 100000000,
             'target_date': '2051-09-25', 'original_date': '2051-09-25', 'priority': 2, 'monthly_minor': 150000,
             'version': 1, 'confirmed': True, 'account_ids': ['retirement']}],
        'accounts': [
            {'id': 'taxable', 'name': 'Everyday investing', 'mask': '4821', 'type': 'Individual taxable', 'goal_id': 'house',
             'restricted': False, 'cash_minor': 600000, 'holdings': [holding('SYN-EQ-T','equity',160), holding('SYN-BD-T','bonds',140)]},
            {'id': 'retirement', 'name': 'Retirement account', 'mask': '7390', 'type': 'Traditional IRA', 'goal_id': 'retirement',
             'restricted': True, 'cash_minor': 1400000, 'holdings': [holding('SYN-EQ-R','equity',240), holding('SYN-BD-R','bonds',260)]}],
        'mandate': {'id': 'wealth_mandate_demo', 'version': 1, 'weights_bps': {'equity':4000,'bonds':4000,'cash':2000},
                    'cash_floor_minor':500000, 'approved_at':FIXTURE_CLOCK, 'expires_at':'2027-09-25T16:00:00+00:00',
                    'revoked':False, 'allowed_assets':['equity','bonds','cash']},
        'mandate_draft': None, 'proposals': [], 'applied_execution_refs': [],
        'simulator': {'mode': 'normal'}, 'messages': [], 'scenario_id': 'flat_return_fixture'})
