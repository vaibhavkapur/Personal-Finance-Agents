"""Run a standalone worker process against the configured database.

Usage: python scripts/worker.py [--interval 2]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.container import Container  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    container = Container()
    print(f"worker {container.worker.owner} polling every {args.interval}s")
    asyncio.run(container.worker.run_forever(interval_seconds=args.interval))


if __name__ == "__main__":
    main()
