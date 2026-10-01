"""Run the API (and, by default, the worker) locally.

Usage: python scripts/serve.py [--host 127.0.0.1] [--port 8000] [--no-worker]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from backend.app.api.main import create_app  # noqa: E402
from backend.app.container import Container  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--no-worker", action="store_true", help="do not run the background worker inside the API process")
    args = parser.parse_args()
    container = Container()
    container.seed()
    app = create_app(container, run_worker=not args.no_worker)
    print(f"Loan Negotiation Agent on http://{args.host}:{args.port}  (UI at /, docs at /docs, environment={container.settings.provider_environment})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
