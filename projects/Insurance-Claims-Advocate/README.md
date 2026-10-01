# Insurance Claims Advocate

A delayed-baggage **claims advocate** prototype. It turns source-linked travel documents into a reviewable claim packet, follows a mock insurer’s requests, explains the decision against a fixture policy, and reconciles any simulated payout before closure.

> **[Read the full documentation](docs/index.md)**

The application runs with synthetic fixtures and mock providers. Use Python 3.12.

## Getting Started

```bash
git clone https://github.com/vaibhavkapur/Personal-Finance-Agents.git
cd Personal-Finance-Agents/projects/Insurance-Claims-Advocate
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/seed.py
uvicorn app.main:create_app --factory --app-dir backend --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). See [Getting Started](docs/getting-started.md) for the complete setup and [Testing](docs/testing.md) for regression commands.

## Quick Example

Run the complete claim, missing-evidence, and partial-rejection journeys against the mock insurer.

```bash
.venv/bin/python scripts/demo.py
```

See [Architecture](docs/architecture.md) and [API Reference](docs/api-reference.md) for the implemented workflow, interfaces, and prototype boundaries.
