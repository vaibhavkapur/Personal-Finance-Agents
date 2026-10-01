# Tax-Season Concierge

A runnable 2025 US federal tax-preparation prototype: explicit eligibility, synthetic document intake, duplicate/W-2c reconciliation, deterministic IRS Tax Table calculations, line-level evidence, exact-package approval, durable mock filing, and separate refund matching.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Tax-Season-Concierge
PYTHON=python3.12 bash scripts/run.sh
```

Open [http://127.0.0.1:8096](http://127.0.0.1:8096). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

With the app running, create a synthetic two-job tax case and exercise review, approval, and mock filing. This demo creates a new fixture session in the running app.

```bash
.venv/bin/python scripts/demo.py --scenario two_jobs
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
