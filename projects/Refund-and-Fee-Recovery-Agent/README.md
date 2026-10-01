# Refund and Fee-Recovery Agent

A consumer money-recovery agent that finds a promised refund missing from a US customer's account, assembles the evidence, follows up with the merchant through an approved message, tracks a separate issuer-dispute lane, and verifies the eventual credit against the account feed.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Refund-and-Fee-Recovery-Agent
make venv
make seed
make api

# Worker and frontend, each in another terminal
make worker
make frontend
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run the missing-refund, partial-recovery, and provisional-credit scenarios against synthetic providers.

```bash
make demo
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
