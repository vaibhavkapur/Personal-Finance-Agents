"""Run the durable worker loop against the configured database.

    python scripts/worker.py [--interval 2]

Delivers due simulator callbacks (mock environment only), executes leased jobs
and dispatches the outbox. Restarting it mid-case is safe: everything it needs
is persisted.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.container import build_container  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    c = build_container()
    print(f"worker {c.worker.worker_id} on {c.settings.database_path} (env={c.settings.environment})")
    while True:
        delivered = c.pump.deliver_due()
        handled = c.worker.run_once_sync()
        if delivered or handled:
            print(f"{c.service._now()} callbacks={len(delivered)} jobs={handled}")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
