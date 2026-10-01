---
title: "Getting Started"
layout: default
nav_order: 2
---

# Getting Started

Use Python 3.12 and Node.js 22. Run these commands from a fresh clone:

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Personal-Trading-Desk
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci --prefix frontend
npm run build --prefix frontend
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [the application](http://127.0.0.1:8000). The API reference is at [OpenAPI](http://127.0.0.1:8000/docs).

The repository includes the application source, synthetic fixtures, tests, migrations, and dependency declarations. Installed environments, generated database state, and local secret files are created on your machine. The default implementation uses mock providers; setup installs packages but does not require live-provider credentials.

## Run a complete example

Run permitted paper execution, exposure rejection, and partial-fill/cancellation recovery in isolated temporary databases.

```bash
.venv/bin/python scripts/demo.py
```

See [Configuration](configuration.md), [Architecture](architecture.md), and [Testing](testing.md) for the runtime options, source layout, and regression commands.

Sign in with the fixture password `paper-demo`. The default runtime uses two SQLite files in `data/` and an embedded worker.
