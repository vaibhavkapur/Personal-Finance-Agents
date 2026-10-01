---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Everkeep — Inheritance and Dormant-Asset Navigator

A working, local US estate-administration prototype based on [the supplied plan](plan.md). React + TypeScript provides the estate workspace; FastAPI runs the deterministic authorization and claim workflow. All people, institutions, requirements, records, approvals and distributions in the demo are synthetic.

## Run

Requires Python 3.12+ and Node.js 22+.

```sh
bash scripts/start.sh
```

Open **http://127.0.0.1:8731**. The demo password is **everkeep-demo**. The login creates the seeded workspace once; subsequent sessions resume persisted state. Interactive API documentation is at `/docs`.

After dependencies and the frontend are built, start directly:

```sh
.venv/bin/python -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port 8731
```

The default mode uses a local SQLite database and one embedded durable worker. Original documents, aggregate records, provider responses, and event content are encrypted with Fernet. The key and database live in ignored `data/`; keep the key when backing up the database. The app has no real provider credentials and does not move real money. The optional Google Fonts stylesheet has system-font fallbacks.

## PostgreSQL + separate worker + provider

```sh
docker compose -p everkeep-prototype up --build -d
```

An initialization container creates the schema and fixture before the API, worker and independent HTTP mock provider start. PostgreSQL and provider ports are not published. The API binds only to loopback. To avoid an existing local app, use `ESTATE_PORT=8733 docker compose -p everkeep-prototype up --build -d` The Compose configuration automatically allows the selected loopback origin. Docker demo credentials are explicitly synthetic defaults.

Stop containers without deleting data with `docker compose -p everkeep-prototype stop`. Do not remove the named volumes if you want to retain records and keys.

For frontend development, run `npm --prefix frontend run dev` alongside the API. Vite serves on 5187 and proxies `/v1` to port 8731. Configuration is documented in `.env.example`; export environment variables explicitly (no implicit dotenv loader).

## Included workflow

- **Source-backed inventory:** two Harbor statements merge using full synthetic reference + owner ID. A similar name with a different full reference and owner ID remains separate, despite the same masked ending.
- **Authority by action:** the mock reviewer grants institution-specific inquiry or claim authority from trusted fixture evidence. Cedar grants inquiry permission only. A claimed family role does not establish entitlement.
- **Minimal disclosure:** each packet includes only that institution's required record IDs, versions and hashes. Exact recipient, manifest, challenge expiry and payload fingerprint appear in the approval screen.
- **Durable execution:** approval, action, job and event writes commit together. Workers use leases and optimistic versions. Provider calls occur outside case transactions. Unknown writes retain their original request reference and reconcile through provider lookup before retry.
- **Distinct outcomes:** Harbor requests a certified authority supplement; Cedar records a beneficiary mismatch and human handoff; Summit requires its own claim form. Accepted mock distributions require matching identity, amount, currency, institution reference, receipt and verified estate destination.
- **Navigator tools:** five scoped MCP tools plus a bounded rules-only planner suggest next steps. Neither approval nor permission mutation is exposed as a model tool.
- **Operator view:** durable jobs, redacted tool runs, controlled clock, original-request reconciliation, and delayed, declined, malformed, no-match, timeout-after-acceptance and wrong-destination scenarios.

## Reproduce the demos and verification

```sh
.venv/bin/python -m scripts.demo
.venv/bin/pytest -q
.venv/bin/python -m backend.app.agent.evaluate
npm --prefix frontend run build
# With the API, worker and provider already running:
.venv/bin/python -m scripts.smoke_http --base-url http://127.0.0.1:8731
```

The demo runs in a new temporary database and leaves the browser workspace untouched. Its scripted approvals represent fixture humans, not model authorization. Expected outcome: bank and retirement resolved with **$129,605.25 in verified simulated distributions**, insurer in human review, and the similar-name candidate unresolved. Inventory completeness remains false.

Measured local results: **62 automated tests passed**, including **32 labeled fixture evaluations** (24 development, 8 held-out); **0 unsupported completion claims** in those fixtures. The frontend production build passes. A live HTTP integration test against the Docker PostgreSQL/API/separate-worker/mock-provider stack passed, and the browser disclosure flow and mobile overflow check were verified. [Evaluation results](evaluation-report.json) record individual cases. These are simulator outcomes, not customer results or measurements of an LLM. One dependency deprecation warning is emitted by Starlette's test client.

## Scope and implementation trade-offs

This MVP uses deterministic extraction of explicitly formatted synthetic text, not OCR or arbitrary PDF parsing. The `StructuredPlanner` interface is an integration point; no live LLM is configured and no model-vs-rules comparison is claimed. The mock HTTP adapter is implemented; sandbox and production integrations, A2A, real discovery registries, legal decisions and real payouts are intentionally absent.

The persistence layer stores documents, assets, authorities, actions and approvals together as an encrypted, versioned case aggregate. Events, provider request idempotency, event inbox and durable jobs have separate constrained relational tables. This deliberately simplifies the plan's fully normalized proposed schema while preserving atomic case-level approval and disclosure invariants. `migrations/001_initial.sql` documents the initial PostgreSQL schema; initial setup also uses the same SQLAlchemy metadata. Future schema evolution needs versioned migration tooling.

Authentication is an HTTP-only signed local demo session with tenant-scoped lookups and origin checks. The demo's operator role controls only synthetic providers. Production identity provisioning, key management, permissions, rate limits, independent document/authority verification, append-only archival and institutional integrations require further work. Use synthetic records only.

See [architecture](architecture.md), [API contract](api.md), and [three browser demo scripts](demo.md).
