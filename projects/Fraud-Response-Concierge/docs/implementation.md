---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Safekeep — Fraud-Response Concierge

A working customer-side lost-wallet prototype for two fictional US institutions. It coordinates explicit approvals, independently verified card protection, charge reports, bank questions, replacement delivery, and recovery. The original brief is preserved in `docs/plan.md`.

**All customer records, institutions, bank actions, deadlines, and financial outcomes are simulated.** No real bank integration or external language model is connected. The concierge is a bounded rules-only tool caller. The default synthetic session is deliberately available without a password and must not be used as production authentication.

## Run the built app

The local environment and production frontend build have been prepared. From this directory:

```sh
.venv/bin/python -m scripts.dev
```

Open <http://127.0.0.1:8093>. This starts the API and a separate durable worker; Ctrl+C stops both. Cases survive a restart in `concierge.db`. A new installation needs Python 3.12 and Node 20:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/python -m scripts.dev
```

For frontend hot reload, run `npm --prefix frontend run dev` alongside the API/worker and open <http://127.0.0.1:5193>. The development proxy sends authenticated API requests to port 8093. Frontend rebuilds are required when using the API-served preview.

## Run the complete demonstrations and checks

```sh
.venv/bin/python -m scripts.demo
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.evaluate
npm --prefix frontend run build
```

The demo command uses an isolated temporary database and runs all three planned journeys: two-bank containment with a direct-authentication handoff, a recognized merchant with no dispute, and a long investigation with provisional credit, final determination, replacement delivery, and closure. It then verifies that a later credit reversal reopens the closed incident. `scripts.seed` creates another synthetic case without deleting existing cases.

Measured local results are in `docs/validation.md` and `docs/evaluation-results.json`. Evaluation covers 36 labeled fixtures, including 12 held-out fixtures, and compares the rules-only orchestrator with canonical domain-state outcomes. These are simulator measurements, not customer or model-performance claims.

## Explore the interface

1. Confirm which cards were lost and the intake statement.
2. Review and approve each card lock. Cedar confirms a temporary lock; Northstar asks for a simulated authentication handoff. No credentials or codes are collected.
3. In **Transactions**, inspect descriptor context, record your own statement, and separately approve any charge report.
4. In **Simulator & operations**, send a bank question, provisional credit, final outcome, or reversal. Each transaction retains its customer assertion and independent provider determination.
5. In **Recovery**, approve lost-card reports and replacements. Simulate delivery, attest that recurring payments were reviewed, and close only when the evidence permits it.

The simulator also supports declined requests, delayed verification, malformed acknowledgments, and acceptance followed by timeout. Reconciliation always looks up the original request reference. An uncertain result never authorizes another write. The fixture clock starts at September 25, 2026, 13:30 UTC and can be advanced in the operator view. Domain approval expiry and deadlines use this clock; session expiry and worker leases use real time.

## PostgreSQL and isolated provider process

```sh
docker compose up --build
```

This configures PostgreSQL 16, an idempotent schema initializer, API, worker, and a separate HTTP mock bank. The app binds to loopback port 8093. Database contents persist in a Docker volume. Docker configuration is supplied; local validation used SQLite, so PostgreSQL/container runtime behavior is not claimed as tested.

For environment overrides, export the values shown in `.env.example`. The local launcher does not automatically load `.env`; Docker Compose's supplied service environment is self-contained. The checked-in webhook secret and database password are fixtures only.

## Architecture and API

- React 19 + TypeScript + Vite customer and operator views.
- FastAPI + Pydantic deterministic application services.
- SQLAlchemy, SQLite/WAL locally and a PostgreSQL-compatible schema for Docker.
- Durable action jobs with persisted approvals, leases, priority, bounded reconciliation, and original provider request references.
- Provider results are stored separately from the local case aggregate. Provider calls occur outside local state transactions.
- HMAC-verified callbacks, provider/event deduplication, per-transaction revisions, ownership checks, optimistic case versions, and evidence-backed closure.
- Five typed MCP tools over authenticated JSON-RPC HTTP and a stdio bridge. Protocol revision: `2025-11-25`; see `docs/api.md`.

See `docs/architecture.md`, `docs/api.md`, and `docs/demo.md`. Interactive API documentation is at <http://127.0.0.1:8093/docs>. The generated contract is `docs/openapi.json`.

## Deliberate prototype boundaries

Production identity, real providers, real institution contact URLs, external LLM integration, A2A, encrypted document uploads, real notifications, account-wide card state across multiple incidents, and regulatory deadline calculation are not implemented. Documents are an empty reviewed list; no upload or object-storage feature is exposed. A single synthetic customer owns the customer/operator workspace. A real deployment needs separate operator authorization and production authentication.

Substates are stored in a versioned incident JSON aggregate instead of separate relational tables. This keeps the prototype small while preserving independent instrument, transaction, and recovery records. Authorization and supported transitions are enforced by application services; SQL uniqueness protects action idempotency, approvals, job ownership, event sequence, and provider event IDs. The worker deliberately escalates to manual review if it crashes after consuming an approval but cannot find a provider result: it does not risk a second side effect.
