# Freelancer Payday Agent

A working, US-first, USD freelancer cash-management prototype. It turns posted business cash into a reviewed personal payday, protects approved reserves, executes one approved mock transfer, and reconciles both bank entries and virtual buckets.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Freelancer-Payday-Agent
bash scripts/dev.sh
```

Open [http://127.0.0.1:8012](http://127.0.0.1:8012). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run supported payday, late-client payment, and returned-transfer reconciliation in temporary databases.

```bash
.venv/bin/python -m scripts.demo
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
