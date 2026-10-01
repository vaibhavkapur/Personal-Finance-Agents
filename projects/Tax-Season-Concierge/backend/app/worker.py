import time
import logging
from app.persistence.store import Store
from app.workflows.engine import Engine

def main():
    engine = Engine(Store())
    while True:
        try:
            engine.process_one()
        except Exception:
            logging.error('Worker tick failed; job retained for lease recovery')
        time.sleep(1)

if __name__ == '__main__':
    main()
