---
title: "Getting Started"
layout: default
nav_order: 2
---

# Getting Started

Use Python 3.12. Run these commands from a fresh clone:

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Loan-Negotiation-Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python scripts/serve.py
```

Open [the application](http://127.0.0.1:8000). The API reference is at [OpenAPI](http://127.0.0.1:8000/docs).

The repository includes the application source, synthetic fixtures, tests, migrations, and dependency declarations. Installed environments, generated database state, and local secret files are created on your machine. The default implementation uses mock providers; setup installs packages but does not require live-provider credentials.

## Run a complete example

Run the keep-current, negotiate, and changed-final-terms mortgage scenarios.

```bash
.venv/bin/python scripts/demo.py
```

See [Configuration](configuration.md), [Architecture](architecture.md), and [Testing](testing.md) for the runtime options, source layout, and regression commands.
