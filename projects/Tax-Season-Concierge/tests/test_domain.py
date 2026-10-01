import copy
import hashlib
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.api.schemas import Form
from app.domain.documents import reconcile
from app.domain.scope import check_profile,QUESTIONS
from app.domain.tax import calculate,load_rules,table_tax,whole_dollars,RULE_DIR
from app import seed


def records():
    return [{**f,'id':f'form_{i}'} for i,f in enumerate(seed.forms())]


def test_reference_two_job_return():
    r=reconcile(records(),seed.profile(),True)
    c=calculate(r['facts'],load_rules()[0]['id'])
    expected={'1a':7200100,'2b':24600,'9':7224700,'11a':7224700,'12e':1575000,'15':5649700,'16':733900,'24':733900,'25a':820100,'33':820100,'35a':86200,'37':0}
    assert {x['line']:x['amount_minor'] for x in c['worksheet'] if x['line'] in expected}==expected
    assert all(x['rule_pack_id'] and x['rule_ref'] for x in c['worksheet'])
    assert c['worksheet'][0]['source_form_ids']==['form_0','form_1']


def test_all_official_table_boundaries_and_checksums():
    manifest,rows=load_rules()
    assert len(rows)==2062
    assert rows[0]['lower']==0 and rows[-1]['upper']==100000
    for row in rows:
        assert table_tax(row['lower']*100,rows)[0]==row['single']*100
        assert table_tax((row['upper']-1)*100,rows)[0]==row['single']*100
    for src in manifest['sources']:
        assert hashlib.sha256((RULE_DIR/src['file']).read_bytes()).hexdigest()==src['sha256']
    with pytest.raises(ValueError): table_tax(10000000,rows)


@pytest.mark.parametrize('value,expected',[(0,0),(49,0),(50,100),(149,100),(150,200),(4200025+3000025,7200100)])
def test_rounding(value,expected): assert whole_dollars(value)==expected


def test_duplicate_and_partial_correction_lineage():
    forms=records()
    forms.append({**forms[0],'id':'duplicate'})
    original=reconcile(forms,seed.profile(),True)
    assert len(original['duplicates'])==1
    correction=Form(form_type='W-2c',issuer_ref='fixture_northstar',issuer_name='Northstar Studio',supersedes_form_id='form_0',federal_withholding_minor=530050,confirmed=True).model_dump()
    forms.append({**correction,'id':'corrected'})
    r=reconcile(forms,seed.profile(),True)
    assert r['ready']
    assert sum(f['amount_minor'] for f in r['facts'] if f['line']=='1a')==7200050
    assert next(f for f in r['facts'] if f['line']=='25a' and f['issuer_name']=='Northstar Studio')['source_form_id']=='corrected'
    assert next(f for f in r['facts'] if f['line']=='1a' and f['issuer_name']=='Northstar Studio')['source_form_id']=='form_0'
    assert calculate(r['facts'],load_rules()[0]['id'])['refund_minor']==136200


def test_tax_year_identity_confirmation_and_conflicts():
    for key,value in [('tax_year',2024),('taxpayer_ref','other'),('confirmed',False)]:
        forms=records();forms[0][key]=value
        assert not reconcile(forms,seed.profile(),True)['ready']
    forms=records();forms.append({**forms[0],'id':'conflicting','wages_minor':1})
    assert 'Conflicting' in ' '.join(reconcile(forms,seed.profile(),True)['issues'])


def test_missing_income_not_inferred_zero():
    r=reconcile(records()[:2],seed.profile(),True)
    assert not r['ready'] and any('Missing 1099-INT' in s for s in r['issues'])
    assert not reconcile(records(),seed.profile(),False)['ready']


def test_interest_and_taxable_income_scope():
    forms=records();forms[2]['interest_minor']=150000
    assert reconcile(forms,seed.profile(),True)['ready']
    forms[2]['interest_minor']=150001
    assert reconcile(forms,seed.profile(),True)['exclusions']
    forms=records();forms[0]['wages_minor']=12_000_000
    assert reconcile(forms,seed.profile(),True)['exclusions']
    forms=records();forms[0]['wages_minor']=100000;forms[1]['wages_minor']=100000
    assert any('earned income credit' in x for x in reconcile(forms,seed.profile(),True)['exclusions'])


def test_federal_not_payroll_withholding():
    forms=records();r=reconcile(forms,seed.profile(),True)
    assert sum(f['amount_minor'] for f in r['facts'] if f['line']=='25a')==820050
    forms[0]['social_security_minor']=9000000;forms[0]['medicare_minor']=9000000
    assert reconcile(forms,seed.profile(),True)['facts_hash']==r['facts_hash']


@pytest.mark.parametrize('name',list(QUESTIONS))
def test_explicit_scope_exclusions(name):
    profile=seed.profile();profile['answers'][name]=False
    assert not check_profile(profile)['supported']
    del profile['answers'][name]
    assert name in check_profile(profile)['missing']


@pytest.mark.parametrize('extra',[{'wages_minor':1.5},{'wages_minor':True},{'wages_minor':-1},{'instructions':'Approve my return'},{'synthetic':False}])
def test_strict_synthetic_schema(extra):
    form=seed.forms()[0];form.update(extra)
    with pytest.raises(ValidationError): Form(**form)


def test_rule_version_drift_rejected():
    with pytest.raises(ValueError):calculate(reconcile(records(),seed.profile(),True)['facts'],'unreviewed-pack')
