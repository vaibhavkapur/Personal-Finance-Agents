from backend.app.persistence.db import Database
from backend.app.workflows.service import Concierge
if __name__=='__main__':
    service=Concierge(Database())
    print('Synthetic recipients, purpose evidence, and fixture clock ready at',service.clock().isoformat())
