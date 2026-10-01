"""Synthetic records. These fictional checklists are not jurisdictional legal rules."""
import hashlib
from copy import deepcopy
from backend.app.persistence.store import uid

INSTITUTIONS = {
    'harbor': {'id': 'harbor', 'name': 'Harbor National Bank', 'short_name': 'Harbor', 'product': 'bank', 'recipient': 'mock://harbor/estate-services', 'version': 1, 'effective_at': '2026-09-01', 'required': ['death_certificate', 'letters_of_authority', 'bank_statement'], 'amount_minor': 4286500},
    'cedar': {'id': 'cedar', 'name': 'Cedar Life Assurance', 'short_name': 'Cedar Life', 'product': 'insurance', 'recipient': 'mock://cedar/beneficiary-services', 'version': 1, 'effective_at': '2026-09-01', 'required': ['death_certificate', 'letters_of_authority', 'insurance_policy'], 'amount_minor': 15000000},
    'summit': {'id': 'summit', 'name': 'Summit Retirement', 'short_name': 'Summit', 'product': 'retirement', 'recipient': 'mock://summit/estate-claims', 'version': 1, 'effective_at': '2026-09-01', 'required': ['death_certificate', 'letters_of_authority', 'retirement_statement', 'retirement_claim_form'], 'amount_minor': 8674025},
}
LABELS = {'death_certificate': 'Death certificate', 'letters_of_authority': 'Letters of authority', 'bank_statement': 'Bank statement', 'insurance_policy': 'Life insurance policy', 'retirement_statement': 'Retirement statement', 'retirement_claim_form': 'Summit estate claim form', 'certified_authority': 'Certified authority supplement'}


def document(name, kind, content, **extra):
    return {'id': uid('doc'), 'name': name, 'kind': kind, 'content': content, 'hash': 'sha256:' + hashlib.sha256(content.encode()).hexdigest(), 'version': 1, 'source': 'synthetic_fixture', 'captured_at': '2026-09-25T10:00:00+00:00', 'extraction_version': 'fixture-text-v1', **extra}


def seed_case(tenant='demo', user='alex_demo'):
    docs = [
        document('Harbor · March statement', 'bank_statement', 'SYNTHETIC RECORD\nInstitution: harbor\nReference: SYN-HNB-8821\nOwner ID: syn_evelyn_morgan\nOwner: Evelyn Morgan\nBalance minor: 4286500\nCurrency: USD\nDate: 2025-03-31'),
        document('Harbor · June statement', 'bank_statement', 'SYNTHETIC RECORD\nInstitution: harbor\nReference: SYN-HNB-8821\nOwner ID: syn_evelyn_morgan\nOwner: Evelyn Morgan\nBalance minor: 4286500\nCurrency: USD\nDate: 2025-06-30'),
        document('Cedar Life · Policy record', 'insurance_policy', 'SYNTHETIC RECORD\nInstitution: cedar\nReference: SYN-CLA-4107\nOwner ID: syn_evelyn_morgan\nOwner: Evelyn Morgan\nBeneficiary: Jordan Morgan\nBalance minor: 15000000\nCurrency: USD\nDate: 2025-04-15'),
        document('Summit · Annual statement', 'retirement_statement', 'SYNTHETIC RECORD\nInstitution: summit\nReference: SYN-SRT-6024\nOwner ID: syn_evelyn_morgan\nOwner: Evelyn Morgan\nBalance minor: 8674025\nCurrency: USD\nDate: 2025-12-31'),
        document('Harbor · Similar-name record', 'bank_statement', 'SYNTHETIC RECORD\nInstitution: harbor\nReference: SYN-OTHER-8821\nOwner ID: syn_evelyn_r_morgan\nOwner: Evelyn R. Morgan\nBalance minor: 920000\nCurrency: USD\nDate: 2025-02-01'),
        document('Evelyn Morgan · Death certificate', 'death_certificate', 'SYNTHETIC RECORD\nSubject: syn_evelyn_morgan\nMock certificate of death. No legal effect.'),
        document('Alex Morgan · Letters of authority', 'letters_of_authority', 'SYNTHETIC RECORD\nSubject: syn_evelyn_morgan\nRepresentative: alex_demo\nRole: personal_representative\nExpires: 2027-09-25\nMock authority evidence. Institution review required.'),
    ]
    return {'id': uid('estate'), 'tenant': tenant, 'owner_user_id': user, 'customer_id': 'cus_demo_11', 'decedent_record_id': 'syn_evelyn_morgan', 'decedent': 'Evelyn Morgan', 'representative': 'Alex Morgan', 'claimed_role': 'personal_representative', 'jurisdiction_profile': 'US_CA_MOCK', 'status': 'collecting', 'version': 1, 'created_at': '2026-09-25T10:00:00+00:00', 'access_policy_version': 1, 'documents': docs, 'assets': [], 'duplicates': [], 'authorities': {}, 'actions': [], 'approvals': [], 'events': [], 'tool_runs': [], 'requirements': deepcopy(INSTITUTIONS), 'inventory_scope': 'Only supplied records have been reviewed. Other assets may exist. Closing known claims does not settle the estate.', 'environment': 'mock'}


def supplement(kind):
    if kind not in ('certified_authority', 'retirement_claim_form'):
        raise ValueError('Unsupported synthetic supplement')
    return document(LABELS[kind], kind, f'SYNTHETIC RECORD\nSubject: syn_evelyn_morgan\nRepresentative: alex_demo\nForm: {kind}\nMock reviewed supplement. No legal effect.')
