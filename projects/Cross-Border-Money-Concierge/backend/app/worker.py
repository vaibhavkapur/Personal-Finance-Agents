"""Standalone durable worker. Reclaims abandoned leases after 30 real seconds."""
import time
from .persistence.db import Database
from .workflows.service import Concierge

if __name__ == '__main__':
    service = Concierge(Database())
    print('Passage worker running · mock providers only', flush=True)
    while True:
        service.drain()
        time.sleep(1)
