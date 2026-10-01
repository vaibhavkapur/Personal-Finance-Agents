"""Seed if absent. Never destroys an existing case."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.persistence.store import Store
from backend.app.adapters.mock import MockCustodian
from backend.app.workflows.service import WealthService
service=WealthService(Store(),MockCustodian('var/custodian.db'))
s=service.state()
print('Seed ready:',s['customer_id'],'case version',s['version'])
