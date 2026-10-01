# Personal Banker

Prototype personal banker that handles deposit maturity through cash forecasting, product comparison, scoped approval, mock bank execution and balance verification, with replay-safe recovery.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22. Install `uv` for the Makefile backend setup.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Personal-Banker
make setup
make build
make seed
make api

# Optional separate worker, in another terminal
make worker
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run the maturity, changed-offer, and uncertain-transfer scenarios against the independent mock bank.

```bash
make demo
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
