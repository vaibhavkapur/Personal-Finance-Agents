#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  "${PYTHON:-python3}" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11 or newer is required"'
.venv/bin/pip install -r requirements.lock
npm ci --prefix frontend --no-audit --no-fund
npm run build --prefix frontend
export PYTHONPATH=backend
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8096}"
