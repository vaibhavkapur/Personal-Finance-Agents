"""Labeled simulator evaluation. No model performance is implied by these results."""
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from cryptography.fernet import Fernet
from sqlalchemy import select, func
from backend.app.agent.tools import AgentTools
from backend.app.domain.engine import find
from backend.app.persistence.store import DomainError, Store, provider_requests, settings, uid
from backend.app.workflows.service import EstateService
from backend.app.workflows.worker import Worker

ROOT = Path(__file__).resolve().parents[3]


def evaluate_fixture(fixture):
    with tempfile.TemporaryDirectory() as temp:
        store = Store('sqlite:///' + temp + '/evaluation.db', Fernet.generate_key())
        with store.engine.begin() as con:
            con.execute(settings.insert().values(id='clock', value='2026-09-26T12:00:00+00:00'))
        service = EstateService(store)
        case = service.create('demo', 'alex_demo')
        cid = case['id']
        service.discover(cid, 'demo', 'fixture')
        service.review_authority(cid, 'demo', 'mock_institution_reviewer')
        institution = fixture['institution']
        if fixture.get('form', True) and institution == 'summit':
            service.add_document(cid, 'demo', 'fixture', store.load(cid, 'demo')['version'], 'retirement_claim_form')
        with store.edit(cid, 'demo') as (_, case):
            asset = next(a for a in case['assets'] if a['institution_id'] == institution and a['match_status'] != 'ambiguous')
            asset_id = asset['id']
            asset['scenario'] = fixture.get('scenario', 'normal')
            if fixture.get('authority') == 'missing':
                del case['authorities'][institution]
            if fixture.get('authority') == 'expired':
                case['authorities'][institution]['expires_at'] = '2025-01-01T00:00:00+00:00'
            if fixture.get('authority') == 'contested':
                case['authorities'][institution]['review_status'] = 'contested'
            if fixture.get('ambiguous'):
                asset_id = next(a['id'] for a in case['assets'] if a['match_status'] == 'ambiguous')
        guide = AgentTools(service).guide(cid, 'demo', 'rules_planner')
        observed = None
        try:
            for iteration in range(2 if fixture.get('follow_up') else 1):
                case = store.load(cid, 'demo')
                packet = service.prepare(cid, 'demo', 'fixture', asset_id, case['version'], uid('eval'))
                if fixture.get('changed_requirements'):
                    with store.edit(cid, 'demo') as (_, current):
                        current['requirements'][institution]['version'] += 1
                case = store.load(cid, 'demo')
                service.approve(cid, 'demo', 'fixture_human_approval', packet['id'], case['version'], packet['payload_hash'], packet['challenge_id'])
                worker = Worker(store)
                worker.tick()
                if fixture.get('scenario') in ('delayed', 'timeout_after_acceptance'):
                    with store.engine.begin() as con:
                        con.execute(settings.update().where(settings.c.id == 'clock').values(value=(datetime.fromisoformat(store.now(con)) + timedelta(minutes=6)).isoformat()))
                    Worker(store).tick()
                case = store.load(cid, 'demo')
                observed = find(case['assets'], asset_id)['status']
                if fixture.get('follow_up') and iteration == 0 and observed == 'evidence_requested':
                    service.add_document(cid, 'demo', 'fixture', case['version'], 'certified_authority')
                else:
                    break
        except DomainError:
            observed = 'blocked'
        case = service.view(cid, 'demo')
        unsupported = sum(a['status'] == 'resolved' and (not a['resolution'] or not a['resolution'].get('evidence_id')) for a in case['assets'])
        with store.engine.connect() as con:
            writes = con.execute(select(func.count()).select_from(provider_requests)).scalar()
        result = {'id': fixture['id'], 'split': fixture['split'], 'expected': fixture['expected'], 'observed': observed, 'passed': observed == fixture['expected'] and unsupported == 0 and not case['summary']['inventory_complete'], 'unsupported_completions': unsupported, 'tool_calls': guide['tool_calls'], 'provider_writes': writes, 'model_cost_minor': 0}
        store.engine.dispose()
        return result


def run():
    fixtures = json.loads((ROOT / 'fixtures' / 'evaluation.json').read_text())
    results = [evaluate_fixture(f) for f in fixtures['cases']]
    report = {'fixture_version': fixtures['version'], 'planner': 'rules-only-v1', 'model_comparison': 'Not run; no LLM provider is configured.', 'case_count': len(results), 'passed': sum(r['passed'] for r in results), 'held_out': sum(r['split'] == 'held_out' for r in results), 'unsupported_completions': sum(r['unsupported_completions'] for r in results), 'model_cost_minor': 0, 'results': results}
    (ROOT / 'docs' / 'evaluation-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'results'}, indent=2))
    if report['passed'] != report['case_count']:
        raise SystemExit(1)


if __name__ == '__main__':
    run()
