#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
if ! .venv/bin/python -c 'import fastapi, sqlalchemy, pydantic, uvicorn' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.lock
fi
if [ ! -d frontend/node_modules ]; then (cd frontend && npm ci --no-audit --no-fund); fi
(cd frontend && npm run build)
exec .venv/bin/python -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port "${PORT:-8088}"
