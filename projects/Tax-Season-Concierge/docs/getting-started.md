---
title: "Getting Started"
layout: default
nav_order: 2
---

# Getting Started

Use Python 3.12 and Node.js 22. Run these commands from a fresh clone:

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Tax-Season-Concierge
PYTHON=python3.12 bash scripts/run.sh
```

Open [the application](http://127.0.0.1:8096). The API reference is at [OpenAPI](http://127.0.0.1:8096/docs).

The repository includes the application source, synthetic fixtures, tests, migrations, and dependency declarations. Installed environments, generated database state, and local secret files are created on your machine. The default implementation uses mock providers; setup installs packages but does not require live-provider credentials.

## Run a complete example

With the app running, create a synthetic two-job tax case and exercise review, approval, and mock filing. This demo creates a new fixture session in the running app.

```bash
.venv/bin/python scripts/demo.py --scenario two_jobs
```

See [Configuration](configuration.md), [Architecture](architecture.md), and [Testing](testing.md) for the runtime options, source layout, and regression commands.
