---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Personal Banker

Prototype personal banker that handles deposit maturity through cash forecasting, product comparison, scoped approval, mock bank execution and balance verification, with replay-safe recovery.

**This is a working mock.** Banks, balances, rates and dates are synthetic fixtures. No real money moves. Simulator success rates and fixture savings are not customer or market results.

User story:

> My $10,000 CD matures next week. Keep $3,000 available for my upcoming expenses and compare what I can do with the rest.

## What is built

A maturity case progresses from a verified account snapshot to a reviewed cash plan, one simulated bank action and a reconciled resulting balance, including recovery from an uncertain submission.

| Surface | What it does |
| --- | --- |
| Customer inbox / case | Dates, bank default behaviour, questions, allocation comparison, exact-instruction review, effective-date tracking |
| Operator view | Clock, worker, failure injection, adapter requests, jobs, mock ledger |
| Agent | Bounded typed tools; cannot approve, cancel or complete |
| Mock bank | Independent ledger, controllable clock, idempotent submit-by-reference |

Docs: [architecture](architecture.md) · [API contract](api.md) · [capability matrix](capabilities.md) · [demo script](demo.md) · plan (kept locally)

## Quick start

Python 3.12 and Node 22 are expected. The Makefile uses `uv` if it is on `PATH`.

```bash
make setup          # backend venv + frontend deps
make test           # 55 pytest cases
make demo           # three end-to-end scenarios
make eval           # 32 labeled agent cases (rules policy)
make build          # frontend/dist, served by the API
make seed           # reset the local SQLite fixture
make api            # http://localhost:8000  (customer + operator UI, OpenAPI at /docs)
make worker         # optional second process; the UI can also run jobs once
```

Local default is SQLite (`backend/personal_banker.db`) with a simulation clock starting **2026-09-26T09:00:00Z**. Optional settings are in `.env.example`. PostgreSQL + API + worker:

```bash
docker compose up --build
```

MCP tools (same schemas as `GET /v1/agent/tools`):

```bash
cd backend && .venv/bin/python -m app.agent.mcp_server
```

## Fixture (hand-checked)

| Fact | Value |
| --- | --- |
| Customer | `cus_demo_1` |
| CD | Harbor `cd_demo_1`, $10,000, matures 2026-10-03, auto-renews if no instruction |
| Checking | $1,200 available / $1,700 current; $500 pending payroll (not spendable) |
| Savings | Northwind HYSA, $0 (verified same-owner destination) |
| Bills | Tuition $1,200 due 2026-10-01; rent $2,000 due 2026-10-05 |
| Reserve | $3,000 that already includes the rent → $1,000 floor, **$7,000** lockable |

Offers compared at a common 365-day horizon on $7,000 (APY compound unless the product documents simple contractual accrual):

| Offer | Comparable | Net at horizon |
| --- | --- | --- |
| Harbor 12-month 4.10% | yes | $287.00 |
| Northwind HYSA 3.90% variable | yes | $273.00 |
| Harbor 6-month 3.85% simple | yes | $134.38 |
| Meridian 9-month 5.00% promo | **no** (`eligibility_unknown`) | not ranked |

Tuition is due before maturity: the locked CD cannot fund it. Pending payroll never enters the base projection. Sources: `fixtures/demo_customer.json`, `fixtures/expected_allocations.json`.

## Measured evaluation (rules policy)

Recorded in `fixtures/eval_results.json` (`fixture-v1`, `rules-v1`). These are prototype harness results on labeled synthetic cases, not production quality metrics.

| Split | Cases | Task completed | Unnecessary questions | Unsupported claims | Tool calls / case |
| --- | --- | --- | --- | --- | --- |
| All | 32 | 32 / 32 | 0 | 0 | 7.25 |
| Dev | 22 | 22 / 22 | 0 | 0 | 7.27 |
| Held-out | 10 | 10 / 10 | 0 | 0 | 7.20 |

Categories: ordinary completion, missing information, conflicting evidence, refusal, uncertain provider. The harness scores **state and evidence**, not wording. Regression: `make test` — 55 passed, covering the eight failure cases in plan §19 and the three demos in §20.

Release gates observed in the suite:

- Liquidity violations rejected (cent-accurate vs hand-checked fixture)
- Displayed offer terms cite provider, product version, retrieval time and environment
- No unapproved external writes, cross-customer reads, or duplicated side effects
- Every completed bank action has a provider/evidence reference
- Worker restart mid-submission looks up the original `request_ref` and does not submit twice

## How a case moves money (mock)

```text
snapshot → collect questions → evaluate (projection + comparison)
  → prepare immutable instruction → customer review + challenge + approve
  → executor re-checks authority, refreshes balance/offer, persists pending, submits once
  → bank callback or poll → reconcile both legs / renewed CD → completed
```

Allowed transitions are enforced in `backend/app/workflows/states.py`. `manual_review` is not a successful outcome. A timeout after bank acceptance stays on the original request reference (`outcome_unknown`); the worker never creates a second transfer.

The model cannot self-approve. Approval binds to the payload hash and case version and is invalidated if amount, destination, term, fee or offer version changes.

## Trade-offs

**Independent mock ledger vs aggregator sandbox.** Sandbox transfers do not update every other product’s data. Execution and verification stay on a coherent mock ledger. The Plaid-shaped adapter advertises read-only capabilities and does not submit.

**Rules policy as the default agent.** The optional OpenAI-compatible policy sits behind the same tool interface. The rules policy is the measured baseline; it decides from the case record, not chat history.

**SQLite for the laptop, PostgreSQL for compose.** One process is enough to demo. Leased jobs and an outbox are already there so a second worker process can take over without changing the domain.

**No live account opening, ACH, tax, insurance or A2A.** Those are later slices. This prototype is not a bank, custodian or deposit-insurance determination service.

## Repository

```text
backend/app/
  api/           customer, approval, provider-event and operator endpoints
  agent/         prompts, typed tools, MCP server, rules/LLM policy, eval harness
  domain/        money, liquidity, offers, instructions, reconciliation
  adapters/      mock bank + sandbox capability stub
  workflows/     state machine, executor, worker, clock, refresh
  persistence/   SQLAlchemy models, outbox/inbox, seed
frontend/        customer journey and operator view (React + TypeScript)
fixtures/        synthetic customer, hand-checked allocations, labeled eval set
migrations/      SQLite and PostgreSQL schema dumps
docs/            architecture, API, capabilities, demo script
```
