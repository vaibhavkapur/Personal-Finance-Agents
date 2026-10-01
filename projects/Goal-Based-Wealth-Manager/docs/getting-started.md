---
title: "Getting Started"
layout: default
nav_order: 2
---

# Getting Started

Use Python 3.12 and Node.js 22. Run these commands from a fresh clone:

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Goal-Based-Wealth-Manager
bash scripts/dev.sh
```

Open [the application](http://127.0.0.1:8017). The API reference is at [OpenAPI](http://127.0.0.1:8017/docs).

The repository includes the application source, synthetic fixtures, tests, migrations, and dependency declarations. Installed environments, generated database state, and local secret files are created on your machine. The default implementation uses mock providers; setup installs packages but does not require live-provider credentials.

## Run a complete example

Run earlier-goal planning, insufficient funding, and recovery from a partial custodian execution in temporary databases.

```bash
.venv/bin/python scripts/demo.py
```

See [Configuration](configuration.md), [Architecture](architecture.md), and [Testing](testing.md) for the runtime options, source layout, and regression commands.
