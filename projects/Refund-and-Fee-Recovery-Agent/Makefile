PY ?= .venv/bin/python

.PHONY: venv seed api worker demo eval test frontend mcp

venv:
	python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e ".[dev]"

seed:
	$(PY) scripts/seed.py --reset

api:
	.venv/bin/uvicorn backend.app.api.app:create_app --factory --reload --port 8000

worker:
	$(PY) scripts/worker.py

demo:
	$(PY) scripts/demo.py --write

eval:
	$(PY) scripts/eval.py --write

test:
	$(PY) -m pytest -q

frontend:
	cd frontend && npm install && npm run dev

mcp:
	MCP_CUSTOMER_ID=cus_demo_4 $(PY) -m backend.app.agent.mcp_server
