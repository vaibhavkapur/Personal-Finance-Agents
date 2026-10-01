import os
import time
from .persistence.store import Store
from .adapters.mock import MockCustodian
from .workflows.service import WealthService

def main():
    service=WealthService(Store(),MockCustodian(os.getenv('CUSTODIAN_DB','var/custodian.db')))
    print('Wealth worker running with persisted jobs and 30-second leases.', flush=True)
    while True:
        try:
            service.tick()
        except Exception as exc:
            print('Worker retry after lease expiry:',type(exc).__name__,flush=True)
        time.sleep(1)

if __name__=='__main__':
    main()
