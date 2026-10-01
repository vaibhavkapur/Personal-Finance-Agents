import logging
import os
import time
from app.workflows.engine import Engine


def main():
    if os.getenv("TRADING_ENVIRONMENT", "mock") != "mock":
        raise RuntimeError("Only mock execution is implemented")
    engine = Engine(os.getenv("DESK_DATA_DIR", "data"))
    while True:
        try:
            engine.tick()
        except Exception:
            logging.exception("Worker failed; durable lease will allow recovery")
        time.sleep(1)


if __name__ == "__main__":
    main()
