#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.lock.txt
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
.venv/bin/python -m backend.app.seed
exec .venv/bin/python -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port "${PORT:-8731}"
