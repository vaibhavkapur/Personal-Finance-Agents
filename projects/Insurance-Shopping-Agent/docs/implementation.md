---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Insurance Shopping Agent

Prototype of a US renters-insurance shopping agent: needs interview, multi-insurer quote exchange, source-backed coverage comparison, hash-bound application approval, and issuance verification against **fictional** insurers.

> Built an insurance-shopping prototype in which a customer agent uses MCP tools and A2A exchanges with simulated insurers to gather quotes, resolve underwriting questions, compare coverage and verify approved policy issuance.

Dollar amounts, quotes, deadlines and insurer names are **fixtures**. This software is not a licensed producer and binds only fictional policies. Completing a payment would not establish that a policy is bound (AP2 is not integrated).

## What you can run

| Command | What it does |
|---|---|
| `make install` | Create `.venv` and install the package |
| `make test` | Domain, workflow, adapter, A2A, worker-recovery and API tests |
| `make eval` | 33 labeled cases (rules-only + scripted agent) |
| `make demo` | Demos 1–3 plus an issued-policy mismatch (in-process, no server) |
| `make demo-a2a` | Same demos over in-process A2A insurer agents |
| `make seed` | Reset the local SQLite database and seed synthetic households |
| `make api` | API + embedded worker on `:8000` |
| `make insurers` | Three independently runnable mock insurer agents on `:9001–:9003` |
| `make frontend` | Customer + operator UI on `:5173` |
| `docker compose up` | PostgreSQL, API, worker, three A2A insurers, Vite frontend |

No external credentials are required. Optional OpenAI-compatible LLM: set `LLM_PROVIDER=openai_compatible` and `LLM_API_KEY` (see `.env.example`). Without a key the deterministic scripted provider is used.

## User story (MVP)

> Find renters insurance that covers replacing my belongings, includes liability protection and starts when I move next month.

One US state (California fixture), one product class (renters), three fictional insurers:

| Insurer | Fixture premium | Character |
|---|---|---|
| Northwind Mutual | $180 / year | Quick, relatively restrictive, revises premium after a prior-claim file |
| Harborline Insurance Co. | $210 / year | Broader terms, lower deductible |
| Cedar & Pine Assurance | $165 / year | Cheapest; asks a follow-up; excludes jewelry (`CP-EX-4`) |

A and B meet requested limits and replacement-cost; C is excluded from the suitable shortlist even though it is cheapest. Ranking after suitability is a disclosed product rule (premium, then deductible preference), not an actuarial evaluation.

## Architecture

```text
Customer / operator
  → React + TypeScript UI and authenticated FastAPI
  → Agent orchestrator (tool budget) + durable case state
  → Typed tools and deterministic domain engine
  → Exact-action approval (challenge + payload hash + case version)
  → InsurerQuoteAdapter (direct mock | A2A 0.3.0)
  → Mock insurer agents
  → Status events, verification and evidence timeline
```

States (server-enforced): `collecting → quoting → comparing → awaiting_selection → awaiting_approval → submitted → underwriting → bound → issued → completed`, with branches for `needs_information`, `revised_offer`, `declined`, `expired`, and non-terminal `manual_review`.

Provider calls happen **outside** database transactions: persist the pending record, call the adapter, then reconcile. A timeout is an *unknown* outcome until `get_policy_status(request_ref)`.

See [docs/architecture.md](architecture.md), [docs/api-contract.md](api-contract.md), [docs/capability-matrix.md](capability-matrix.md) and [docs/trade-offs.md](trade-offs.md).

## Local walkthrough

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
make test          # 50 tests
make demo          # three plan demos + mismatch case
make seed
make api           # http://127.0.0.1:8000/docs
# in another terminal
make frontend      # http://localhost:5173
```

Fixture sessions in the UI: Avery Demo, Jordan Sample, Riley Fixture, and Operator. The fixture clock starts at `2026-10-01T09:00Z`; desired coverage start is `2026-11-01` (the UI must not imply the policy is effective today).

Multi-process A2A:

```bash
make insurers                      # :9001 :9002 :9003
ADAPTER_MODE=a2a make api          # talks to those agents
python scripts/demo.py --base-url http://127.0.0.1:8000
```

MCP stdio server (protocol `2025-06-18`), scoped by `MCP_CUSTOMER_TOKEN`:

```bash
MCP_CUSTOMER_TOKEN=tok_cus_demo_1 python -m app.agent.mcp_server
```

## Repository layout

```text
backend/app/
  api/              customer, action, provider-event and operator endpoints
  agent/            prompts, typed tools, orchestrator, MCP server, LLM interface
  domain/           needs, renters-quote/v1, comparison, applications, issuance
  adapters/         InsurerQuoteAdapter, direct mock, A2A client + mock servers
  workflows/        state machine, approvals, executor, leased-job worker
  persistence/      SQLAlchemy models, repositories, outbox, inbox, jobs
frontend/           customer journey and operator view
fixtures/           CA state profile, three policy forms, households, 33 eval cases
tests/              domain, workflow, protocol, replay and recovery tests
migrations/         PostgreSQL schema (SQLite is created from models)
docs/               architecture, API contract, capability matrix, demo script, test report
scripts/            demo, seed, evaluate, run_insurer
```

## Acceptance (measured)

Recorded in [docs/test-report.md](test-report.md) against simulators and fixtures, not customer or market outcomes.

- `50 passed` pytest, covering the eight failure cases in the plan
- 33/33 labeled eval cases (9 held out); 0 unsupported claims; 0 duplicate submits
- Worker restart mid-case recovers approvals, timers and evidence
- Every issued-policy completion carries a provider/evidence reference
- A completed A2A quote task never marks a policy issued

Demo script: [docs/demo-script.md](demo-script.md).

## Pins

| Item | Revision |
|---|---|
| Application | 0.1.0 |
| Fixture set | 2026.09.1 |
| A2A | 0.3.0 |
| MCP | 2025-06-18 |
| FastAPI / Pydantic / SQLAlchemy / httpx | see `pyproject.toml` and `requirements.lock` |

Recheck provider access, terms and jurisdiction-specific rules before adding live execution.
