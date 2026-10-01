"""Run one mock insurer as an independently deployable A2A agent.

  python scripts/run_insurer.py --insurer a   # Northwind on :9001
  python scripts/run_insurer.py --insurer b   # Harborline on :9002
  python scripts/run_insurer.py --insurer c   # Cedar & Pine on :9003
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.adapters.mock.a2a_server import run_from_cli  # noqa: E402

if __name__ == "__main__":
    run_from_cli()
