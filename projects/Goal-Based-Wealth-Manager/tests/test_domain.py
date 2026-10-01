from copy import deepcopy
import pytest
from backend.app.domain.engine import *
from backend.app.fixtures import seed_state


def test_reference_calculation_to_cent():
    s=seed_state()
    assert calculate(s,'house')['required_monthly_minor']==100000
    r=calculate(s,'house',target_date='2027-09-25')
    assert r['current_minor']==3600000 and r['required_monthly_minor']==200000 and r['shortfall_minor']==1200000


def test_three_class_fixture_weights():
    s=seed_state()
    values={c:sum(account_values(a)[c] for a in s['accounts']) for c in CLASSES}
    assert values=={'equity':4000000,'bonds':4000000,'cash':2000000}


def test_stress_preserves_cash():
    r=calculate(seed_state(),'house','stress_fixture')
    assert r['adjusted_minor']==3000000 and r['required_monthly_minor']==125000


def test_ceiling_cent():
    s=seed_state();s['goals'][0]['target_minor']+=1
    assert calculate(s,'house')['required_monthly_minor']==100001


def test_contribution_first_and_conserved_value():
    b=plan_basket(seed_state(),200000)
    assert [a['type'] for a in b['actions']]==['contribution','buy']
    assert b['after_values']=={'equity':1600000,'bonds':1440000,'cash':760000}
    assert sum(b['after_values'].values())==sum(b['before_values'].values())+200000
    assert b['tax_impact']=='unknown'


def test_rounding_residual_cash():
    assert plan_basket(seed_state(),200123)['after_values']['cash']==760123


def test_no_double_earmark():
    s=seed_state();s['accounts'].append(deepcopy(s['accounts'][0]))
    with pytest.raises(RuleError):validate_allocations(s)


def test_no_retirement_for_house():
    s=seed_state();s['accounts'][1]['goal_id']='house'
    with pytest.raises(RuleError):validate_allocations(s)


def test_restricted_account():
    with pytest.raises(RuleError):plan_basket(seed_state(),200000,'retirement')


@pytest.mark.parametrize('price,at',[(0,'2026-09-25T16:00:00+00:00'),(10000,'2026-09-23T16:00:00+00:00')])
def test_missing_and_stale_prices(price,at):
    s=seed_state();s['accounts'][0]['holdings'][0].update(price_minor=price,price_at=at)
    with pytest.raises(RuleError):plan_basket(s,200000)


@pytest.mark.parametrize('prop,value',[('revoked',True),('expires_at','2026-09-24T00:00:00+00:00')])
def test_mandate_validity(prop,value):
    s=seed_state();s['mandate'][prop]=value
    with pytest.raises(RuleError):plan_basket(s,200000)


def test_month_end_horizon():
    assert horizon('2026-01-31','2026-02-28')==1
    assert horizon('2026-09-25','2027-09-24')==11
