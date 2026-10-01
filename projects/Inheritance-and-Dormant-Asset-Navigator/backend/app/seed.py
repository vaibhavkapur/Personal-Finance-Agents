from backend.app.persistence.store import Store
from backend.app.workflows.service import EstateService

if __name__ == '__main__':
    store = Store()
    service = EstateService(store)
    existing = store.list_cases('demo')
    if existing:
        print('Existing synthetic workspace:', existing[0]['id'])
    else:
        case = service.create('demo', 'alex_demo')
        service.discover(case['id'], 'demo', 'fixture_seed')
        service.review_authority(case['id'], 'demo', 'mock_institution_reviewer')
        print('Seeded synthetic workspace:', case['id'])
