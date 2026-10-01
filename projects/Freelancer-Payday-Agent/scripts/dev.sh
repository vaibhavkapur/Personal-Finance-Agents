#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.lock
npm ci --prefix frontend
npm run build --prefix frontend
.venv/bin/python -m backend.app.seed
.venv/bin/python -m backend.app.worker &
worker_pid=$!
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8012 &
api_pid=$!
cleanup() { kill "$worker_pid" "$api_pid" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
printf 'Payday is available at http://127.0.0.1:8012\n'
wait "$api_pid"
