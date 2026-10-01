# Personal Trading Desk

A personal paper-trading desk with versioned mandates, deterministic exposure checks, exact-action approvals, an independent mock broker, and reconciliation of fills, cancellations, positions, and cash.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Personal-Trading-Desk
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci --prefix frontend
npm run build --prefix frontend
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run permitted paper execution, exposure rejection, and partial-fill/cancellation recovery in isolated temporary databases.

```bash
.venv/bin/python scripts/demo.py
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
