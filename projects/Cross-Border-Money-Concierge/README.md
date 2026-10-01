# Cross-Border Money Concierge

A working sender-side USD → INR remittance prototype based on [the supplied plan](docs/plan.md). Compare three fictional providers, approve an exact transfer, respond to document requests, and verify recipient credit independently from source funding.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12 and Node.js 22.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Cross-Border-Money-Concierge
bash scripts/start.sh
```

Open [http://127.0.0.1:8088](http://127.0.0.1:8088). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run compare-and-deliver, document-request, and accepted-before-timeout transfer recovery in temporary databases.

```bash
.venv/bin/python -m scripts.demo
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
