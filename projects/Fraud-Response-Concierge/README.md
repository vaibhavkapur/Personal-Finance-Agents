# Fraud-Response Concierge

A working customer-side lost-wallet prototype for two fictional US institutions. It coordinates explicit approvals, independently verified card protection, charge reports, bank questions, replacement delivery, and recovery. The original brief is preserved in `docs/plan.md`.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Fraud-Response-Concierge
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci --prefix frontend
npm run build --prefix frontend
.venv/bin/python -m scripts.dev
```

Open [http://127.0.0.1:8093](http://127.0.0.1:8093). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run two-bank containment, merchant clarification, and long-investigation recovery in a temporary database.

```bash
.venv/bin/python -m scripts.demo
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
