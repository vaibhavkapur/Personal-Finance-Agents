PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
export PYTHONPATH := backend

.PHONY: venv install test eval demo demo-a2a seed api worker insurers frontend clean

venv:
	python3 -m venv .venv

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest -q

eval:
	$(PY) scripts/evaluate.py

demo:
	$(PY) scripts/demo.py

demo-a2a:
	$(PY) scripts/demo.py --mode a2a

seed:
	$(PY) scripts/seed.py --reset

api:
	$(PY) -m app.main

worker:
	EMBEDDED_WORKER=false $(PY) -m app.workflows.worker

insurers:
	$(PY) scripts/run_insurer.py --insurer a & \
	$(PY) scripts/run_insurer.py --insurer b & \
	$(PY) scripts/run_insurer.py --insurer c & \
	wait

frontend:
	cd frontend && npm install && npm run dev

clean:
	rm -rf data .pytest_cache backend/app/__pycache__ backend/app/*/__pycache__ backend/app/*/*/__pycache__ tests/__pycache__
