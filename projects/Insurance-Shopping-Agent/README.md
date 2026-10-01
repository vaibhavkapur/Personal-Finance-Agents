# Insurance Shopping Agent

Prototype of a US renters-insurance shopping agent: needs interview, multi-insurer quote exchange, source-backed coverage comparison, hash-bound application approval, and issuance verification against **fictional** insurers.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Insurance-Shopping-Agent
make install
make seed
make api

# Frontend, in another terminal
make frontend
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run the renters-insurance quote, underwriting follow-up, revised-term, and issuance-mismatch demonstrations.

```bash
make demo
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
