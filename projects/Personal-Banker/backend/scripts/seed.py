"""Create the schema and load the synthetic fixture (plan §16 seed command).

Usage (from backend/):
    python scripts/seed.py            # seed if empty
    python scripts/seed.py --reset    # drop and recreate
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PB_SERVE_FRONTEND", "false")

from app.persistence import db  # noqa: E402
from app.persistence.seed import seed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    if args.reset:
        db.drop_schema()
    db.create_schema()
    with db.session_scope() as session:
        fixture = seed(session)
    print(f"seeded fixture {fixture['fixture_version']} into {db.get_engine().url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
