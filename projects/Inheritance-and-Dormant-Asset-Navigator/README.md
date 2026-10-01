# Inheritance and Dormant-Asset Navigator

A working, local US estate-administration prototype based on [the supplied plan](docs/plan.md). React + TypeScript provides the estate workspace; FastAPI runs the deterministic authorization and claim workflow. All people, institutions, requirements, records, approvals and distributions in the demo are synthetic.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Inheritance-and-Dormant-Asset-Navigator
bash scripts/start.sh
```

Open [http://127.0.0.1:8731](http://127.0.0.1:8731). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run separate institution claim flows, authority checks, and verified simulated distributions in a temporary database.

```bash
.venv/bin/python -m scripts.demo
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
