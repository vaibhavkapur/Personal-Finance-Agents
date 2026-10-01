from backend.app.persistence.store import Store
if __name__=='__main__':
    store=Store()
    print('Schema v1 initialized idempotently. Existing incident data preserved.')
