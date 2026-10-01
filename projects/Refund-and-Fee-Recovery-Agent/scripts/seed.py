"""Seed the local database with the synthetic customer bundle.

    python scripts/seed.py [--reset]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.container import build_container  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete the existing database first")
    args = parser.parse_args()
    c = build_container()
    if args.reset:
        c.db.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(c.settings.database_path + suffix)
            if p.exists():
                p.unlink()
        c = build_container()
    counts = c.seed()
    print(f"database: {c.settings.database_path}")
    print(f"seeded: {counts}")
    print(f"fixture clock: {c.clock.now().isoformat()}")


if __name__ == "__main__":
    main()
