"""Seed the configured database with synthetic fixtures.

Usage: python scripts/seed.py [--reset]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.container import Container  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    args = parser.parse_args()
    container = Container()
    if args.reset:
        container.db.drop_all()
        container.db.create_all()
    created = container.seed()
    print(json.dumps({"database_url": container.settings.database_url.split("@")[-1], "created": created}, indent=2))


if __name__ == "__main__":
    main()
