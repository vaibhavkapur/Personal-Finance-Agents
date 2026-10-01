"""Seed the configured database with the synthetic fixtures and print the fixture tokens."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.clock import Clock  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.persistence.db import Database  # noqa: E402
from app.persistence.seed import fixture_tokens, seed  # noqa: E402


def main() -> None:
    settings = load_settings()
    db = Database(settings.database_url)
    counts = seed(db, Clock())
    print(json.dumps({"database_url": settings.database_url, "seeded": counts, "tokens": fixture_tokens()}, indent=2))


if __name__ == "__main__":
    main()
