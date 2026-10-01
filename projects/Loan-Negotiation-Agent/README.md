# Loan Negotiation Agent

Borrower-side mortgage agent that normalizes Loan Estimates, compares amortization and horizon costs, coordinates approved lender negotiations, and checks final terms against the selected offer.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Loan-Negotiation-Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python scripts/serve.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run the keep-current, negotiate, and changed-final-terms mortgage scenarios.

```bash
.venv/bin/python scripts/demo.py
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
