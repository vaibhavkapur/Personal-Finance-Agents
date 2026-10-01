---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Loan Negotiation Agent

Borrower-side mortgage agent that normalizes Loan Estimates, compares amortization and horizon costs, coordinates approved lender negotiations, and checks final terms against the selected offer.

**US-first prototype.** Customer records, dollar amounts, quotes, deadlines and lender names are synthetic fixtures. External actions run against an in-process mock lender network. Nothing here pulls credit, locks a rate, funds a loan, or contacts a real institution.

> Check whether refinancing my mortgage is worth it if I expect to stay for four more years, and ask my lender whether it can offer better terms.

The product is a **mortgage comparison engine + borrower negotiation assistant + application coordinator**. It ranks keep / reprice / refinance by total cost at a stated holding horizon, not by the smallest monthly payment.

## What is built

| Layer | What it does |
| --- | --- |
| Domain engine | Independent amortization schedules (cent rounding + final-payment adjustment), Loan Estimate normalization (CFPB A/B/C/E/H vs F/G), horizon economic cost, fee sensitivity, final-term diffs |
| Workflow | Case lifecycle, optimistic versioning, exact-action approvals, persisted worker with leases, pending-first executor, reference lookup after timeout |
| Mock lenders | One that refuses, one that matches rate but raises fees, one that requests income proof; quote expiry, changed closing costs, signed callbacks |
| Agent | Typed tools (`read_loan_terms`, `compare_loan_scenarios`, `prepare_lender_request`, `prepare_refinance_application`, `diff_final_terms`), MCP server, optional A2A lender agents, rules planner (optional LLM) |
| UI | Borrower journey (comparison, approval screens, conversation) and operator view (jobs, adapter log, clock, faults) |

See [docs/architecture.md](architecture.md), [docs/api.md](api.md), [docs/provider-capabilities.md](provider-capabilities.md), [docs/demo.md](demo.md) and [docs/tradeoffs.md](tradeoffs.md).

## Quick start

Python 3.9+. SQLite is the default store; PostgreSQL is optional.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python scripts/seed.py          # optional; serve.py also seeds
python scripts/serve.py         # http://127.0.0.1:8000/  (UI)  /docs  (OpenAPI)
```

Fixture tokens (also pre-filled in the UI):

- Borrower: `demo-borrower-token` → customer `cus_demo_5`
- Other borrower: `other-borrower-token` (cross-tenant reads return 404)
- Operator: `demo-operator-token`

```bash
pytest                          # domain, workflow, API, agent, MCP, 30 labelled eval cases
python scripts/demo.py          # three documented scenarios, fixture clock
python scripts/eval.py          # agent vs rules-only; writes docs/eval-report.json
```

Docker (API + Postgres; worker runs inside the API process so it shares the in-process simulator):

```bash
docker compose up --build
```

MCP stdio server (same tools, scoped to one borrower token):

```bash
python -m backend.app.agent.mcp_server
```

Environment knobs: `DATABASE_URL`, `USE_FIXTURE_CLOCK`, `FIXTURE_CLOCK_START`, `APPROVAL_TTL_SECONDS`, `TOOL_CALL_BUDGET`, `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` (optional planner), `WEBHOOK_SIGNING_SECRET`, `OPERATOR_TOKEN`, `PROVIDER_ENV`.

## Seed fixtures

Existing loan (`fixtures/mortgages.json`): **$300,000** balance, **7.00%**, **300** months remaining, P&I **$2,120.34**, escrow **$650**. Statement date 2026-09-01.

Offers (`fixtures/offers/`):

| Id | Lender | Rate / term | Net incremental costs | Notes |
| --- | --- | --- | --- | --- |
| `offer_doc_a` | Northstar Mortgage | 6.50% / 300 | $4,500 | Rankable |
| `offer_doc_b` | Harbor Home Loans | 6.625% / 360 | $6,000 | Longer term; lower payment, higher horizon cost |
| `offer_doc_c` | Summit Lending | 6.75% / 300 | $1,500 | Missing rate-lock section → not ranked until supplied |
| `offer_doc_expired` | — | — | — | Expired; cannot be submitted |
| `offer_doc_incomplete` | — | — | — | Missing fields; never ranked |

At a **48-month** horizon, Northstar is about **$1,494** cheaper than keeping (break-even month 37). Harbor’s payment is **$199/month** lower because of the longer term, and costs about **$2,373** more. At **18 months** no offer recovers its costs (`keep_current`).

These numbers are fixture calculations, not customer or market results.

## Lifecycle

```text
collecting → comparing → awaiting_decision
awaiting_decision → keep_current
awaiting_decision → awaiting_approval → negotiation_pending → revised_offer → comparing
awaiting_decision → application_review → awaiting_approval → submitted
submitted → conditions_outstanding → submitted
submitted → approved_offer → final_review → mock_closed
submitted → declined | withdrawn
```

`approved_offer` is not funded. `mock_closed` requires a simulator closing record plus a separate payoff record; the existing mortgage stays active. `manual_review` is operational, not a successful outcome. A provider timeout after submission is reconciled by the original request reference — never by a second application.

Lender messages, applications, document releases and closing instructions each need an approval bound to the action hash, lender, offer version, disclosed documents and case version.

## Measured outcomes (prototype)

Captured 26 September 2026 against fixture version `2026-09-26.1`.

| Gate | Result |
| --- | --- |
| Reference schedules vs independent Fraction calculator | Agree to **1 cent** on all seeded and awkward cases (`tests/domain/test_amortization.py`) |
| pytest | **72** passed (amortization, comparison, workflow, approvals, executor/worker, end-to-end demos, MCP, labelled eval) |
| Labelled eval (30 cases: completion, missing info, conflict, refusal, uncertain provider; 8 held-out) | Agent and rules-only **100%** task completion; **0** unapproved writes, unsupported claims, or unnecessary questions |
| Ranked offers | Always include stated horizon and fee assumption; incomplete/expired quotes are reported, not ranked |
| Indicative quote / approved application labelled funded | Never |
| Duplicate side effects after timeout | Found by original `client_request_ref`; no second write |
| Worker restart mid-case | Leases expire and are reclaimed; approvals and evidence remain |

Do not present these simulator rates as customer or market results.

## Repository

```text
backend/app/
  api/           customer, operator and provider-event endpoints
  agent/         prompts, typed tools, MCP, A2A, planner, eval harness
  domain/        loan terms, amortization, cost comparison, final-term diffs
  adapters/      mock lender network and sandbox stub
  workflows/     states, approvals, executor, worker
  persistence/   SQLAlchemy models, seed, outbox/inbox
frontend/        borrower + operator views (static HTML/JS)
fixtures/        synthetic customer, mortgage, offers, expected schedules, eval cases
tests/           domain, workflow, API, agent
scripts/         serve, seed, worker, demo, eval
migrations/      0001_initial.sql (PostgreSQL)
docs/            API, architecture, demo script, capability matrix, trade-offs
```

The first worker uses persisted jobs and leases. Temporal is not required. Redis is not the source of truth.

## Scope that stays out

Adjustable-rate, cash-out and government-backed products; live credit applications and hard inquiries; automatic acceptance of lender offers; claims that a prequalification is an approval; compliant APR; real closing or disbursement.

Real mortgage brokerage, origination and disclosures need supported providers and a review of applicable US responsibilities. This prototype does neither.
