"""Create a new synthetic case without replacing existing cases."""
from backend.app.persistence.store import Store
from backend.app.domain.engine import Service
from backend.app.domain.registry import CUSTOMER

def main():
    store=Store()
    case=Service(store).create(CUSTOMER,store.now(),['credit_card_demo','debit_card_demo'],['txn_demo_91','txn_demo_92'])
    print('Created synthetic case:',case['id'])
if __name__=='__main__': main()
