import hashlib
import json
from datetime import datetime
from backend.app.domain.fixtures import LABELS
from backend.app.persistence.store import DomainError, uid

ALLOWED = {
    'authority_review': {'requirements_ready', 'disputed_authority'},
    'requirements_ready': {'awaiting_approval', 'disputed_authority'},
    'awaiting_approval': {'submitted', 'requirements_ready', 'disputed_authority'},
    'submitted': {'evidence_requested', 'institution_confirmed', 'disputed_authority', 'no_asset_found', 'claim_denied', 'manual_review'},
    'evidence_requested': {'requirements_ready', 'disputed_authority'},
    'institution_confirmed': {'resolution_pending', 'manual_review'},
    'resolution_pending': {'resolved', 'manual_review'},
    'disputed_authority': {'human_review'},
    'human_review': set(), 'resolved': set(), 'manual_review': {'submitted'}, 'candidate_review': set(), 'no_asset_found': set(), 'claim_denied': set(),
}


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def find(items, item_id):
    item = next((item for item in items if item['id'] == item_id), None)
    if not item:
        raise DomainError('Record not found in this workspace.', 404)
    return item


def transition(store, con, case, asset, target, actor, detail=''):
    previous = asset['status']
    if target not in ALLOWED.get(previous, set()):
        raise DomainError(f'Cannot move an asset from {previous} to {target}.')
    asset['status'] = target
    store.event(con, case, 'asset.' + target, actor, asset['id'], previous, target, detail)


def parse_record(doc):
    if not doc['content'].startswith('SYNTHETIC RECORD\n'):
        return None
    fields = dict(line.split(': ', 1) for line in doc['content'].splitlines() if ': ' in line)
    if not all(fields.get(k) for k in ('Institution', 'Reference', 'Owner ID', 'Owner')):
        return None
    return fields


def discover(store, con, case, actor):
    existing = {d for a in case['assets'] for d in a['evidence_ids']} | {d['document_id'] for d in case['duplicates']}
    for doc in case['documents']:
        fields = parse_record(doc)
        if not fields or doc['id'] in existing or fields['Institution'] not in case['requirements']:
            continue
        stable = (fields['Institution'], fields['Reference'], fields['Owner ID'])
        match = next((a for a in case['assets'] if tuple(a['identity_key']) == stable), None)
        if match:
            match['evidence_ids'].append(doc['id'])
            case['duplicates'].append({'id': uid('duplicate'), 'document_id': doc['id'], 'merged_into_id': match['id'], 'reason': 'Same institution, complete synthetic reference and owner ID.'})
            continue
        owner_match = fields['Owner ID'] == case['decedent_record_id']
        try:
            amount = int(fields.get('Balance minor', '0'))
        except ValueError:
            amount = 0
        amount = max(0, min(amount, 10**12))
        inst = case['requirements'][fields['Institution']]
        case['assets'].append({'id': uid('asset'), 'institution_id': inst['id'], 'institution': inst['name'], 'product_type': inst['product'], 'masked_reference': '•••• ' + fields['Reference'][-4:], 'identity_key': list(stable), 'owner': fields['Owner'], 'match_status': 'source_matched' if owner_match else 'ambiguous', 'ownership_status': 'unverified', 'status': 'authority_review' if owner_match else 'candidate_review', 'evidence_ids': [doc['id']], 'confidence': 'High · stable reference + owner ID' if owner_match else 'Low · different owner ID', 'source_amount_minor': amount, 'currency': 'USD', 'beneficiary': fields.get('Beneficiary'), 'note': 'Institution ownership verification still required.' if owner_match else 'Similar name and masked ending; full reference and owner differ. Excluded from recovery.', 'claim_version': 0, 'provider_case_ref': None, 'resolution': None, 'scenario': 'normal'})
    case['status'] = 'authority_review'
    store.event(con, case, 'inventory.reviewed', actor, detail=f'{len(case["assets"])} candidates; {len(case["duplicates"])} duplicate records linked. Scope limited to supplied records.')


def requirements(case, asset):
    inst = case['requirements'][asset['institution_id']]
    kinds = list(inst['required'])
    if asset.get('extra_requirements'):
        kinds += asset['extra_requirements']
    result = []
    for kind in dict.fromkeys(kinds):
        candidates = [d for d in case['documents'] if d['kind'] == kind and (kind not in ('bank_statement', 'insurance_policy', 'retirement_statement') or d['id'] in asset['evidence_ids'])]
        doc = candidates[0] if candidates else None
        result.append({'kind': kind, 'label': LABELS.get(kind, kind), 'present': doc is not None, 'document_id': doc['id'] if doc else None})
    return {'version': inst['version'], 'effective_at': inst['effective_at'], 'recipient': inst['recipient'], 'source': inst['name'] + ' · synthetic checklist', 'retrieved_at': inst['effective_at'], 'environment': 'mock', 'items': result, 'missing': [r['label'] for r in result if not r['present']]}


def check_authority(case, asset, action, now):
    authority = case['authorities'].get(asset['institution_id'])
    if not authority or authority['review_status'] != 'verified' or action not in authority['permitted_actions']:
        raise DomainError('Institution-reviewed authority for this action is required.', 403)
    if datetime.fromisoformat(authority['expires_at']) <= datetime.fromisoformat(now):
        raise DomainError('Authority evidence has expired; reviewer action required.', 403)
    if authority['subject'] != asset['identity_key'][2] or authority['subject'] != case['decedent_record_id']:
        raise DomainError('Authority and asset identity do not match.', 403)
    if not set(authority['evidence_ids']).issubset({d['id'] for d in case['documents']}):
        raise DomainError('Authority evidence is no longer available.', 403)
    return authority


def packet_payload(case, asset, now):
    if asset['match_status'] == 'ambiguous':
        raise DomainError('Ambiguous ownership must be reviewed before any disclosure.', 403)
    action_type = 'inquire' if asset['product_type'] == 'insurance' else 'submit_claim'
    authority = check_authority(case, asset, action_type, now)
    req = requirements(case, asset)
    if req['missing']:
        raise DomainError('Missing evidence: ' + ', '.join(req['missing']))
    docs = [find(case['documents'], item['document_id']) for item in req['items']]
    return {'asset_id': asset['id'], 'institution_id': asset['institution_id'], 'recipient': req['recipient'], 'action_type': action_type, 'requirements_version': req['version'], 'authority_id': authority['id'], 'authority_version': authority['version'], 'manifest': [{'id': d['id'], 'name': d['name'], 'kind': d['kind'], 'hash': d['hash'], 'version': d['version']} for d in docs], 'identity': list(asset['identity_key']), 'amount_minor': None, 'currency': 'USD', 'scenario': asset.get('scenario', 'normal'), 'terms': 'Disclose only listed synthetic records to this mock institution. No transfer or legal determination.', 'environment': 'mock'}


def summary(case):
    resolutions = [a['resolution'] for a in case['assets'] if a['status'] == 'resolved' and a['resolution']]
    return {'known_assets': sum(a['match_status'] != 'ambiguous' for a in case['assets']), 'ambiguous_candidates': sum(a['match_status'] == 'ambiguous' for a in case['assets']), 'duplicates': len(case['duplicates']), 'resolved_assets': len(resolutions), 'verified_recovery_minor': sum(r['amount_minor'] for r in resolutions), 'currency': 'USD', 'source_values_minor': sum(a['source_amount_minor'] for a in case['assets'] if a['match_status'] != 'ambiguous'), 'inventory_complete': False}
