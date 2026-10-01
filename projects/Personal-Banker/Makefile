# Local development shortcuts. Python 3.12 and Node 22 are expected on PATH;
# see README.md for the uv-based setup used during development.

# Paths are relative to the directory each target cds into (backend/ or frontend/).
PY ?= .venv/bin/python
UV ?= uv

.PHONY: setup backend frontend seed api worker test eval demo schema build clean

setup: backend frontend

backend:
	cd backend && $(UV) sync --extra dev

frontend:
	cd frontend && npm install --no-audit --no-fund

seed:
	cd backend && $(PY) scripts/seed.py --reset

api:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

worker:
	cd backend && $(PY) -m app.workflows.worker

test:
	cd backend && $(PY) -m pytest -q

eval:
	cd backend && $(PY) scripts/run_eval.py

demo:
	cd backend && $(PY) scripts/demo.py

schema:
	cd backend && $(PY) scripts/dump_schema.py

build:
	cd frontend && npm run build

clean:
	rm -f backend/personal_banker.db backend/personal_banker.db-wal backend/personal_banker.db-shm
