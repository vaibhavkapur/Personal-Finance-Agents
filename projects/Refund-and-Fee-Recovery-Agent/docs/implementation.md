---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Refund and Fee-Recovery Agent — prototype

A consumer money-recovery agent that finds a promised refund missing from a US customer's account, assembles the evidence, follows up with the merchant through an approved message, tracks a separate issuer-dispute lane, and verifies the eventual credit against the account feed.

> I cancelled this service and was promised $84.99 back. Check whether it arrived and follow up if it did not.

**Everything external is simulated.** The customer, merchants, card issuer, statement feed, amounts, dates, deadlines and protocol events are synthetic fixtures. No real refund, message or dispute is ever initiated. Mock, sandbox and live capabilities are labelled separately in [`docs/capability-matrix.md`](capability-matrix.md).

## What it does

- Separates money **requested, promised, merchant-confirmed, provisionally credited and finally posted**. Only posted final credits count as recovered; a merchant saying "refund issued" changes evidence, not the balance; issuer provisional credits are shown separately; store credit is never a card credit.
- Deterministic credit matching with exact (provider reference), candidate (amount + merchant + instrument + time window) and rejected matches, each with reasons. Ambiguity asks the customer instead of guessing.
- Merchant lane first, issuer lane gated by configured eligibility, each with its own provider reference, deadline (versioned fixture config) and approval.
- Exact-action approval: review screen with destination, documents, amount, terms and irreversible effect; payload hash + challenge + expiry + case version; re-verified by the executor immediately before the side effect; revoked when material inputs change.
- Durable worker with persisted jobs and leases; provider calls outside transactions; timeouts resolved by request reference; uncertain outcomes hold for review instead of generating new side effects.
- At-least-once provider events deduplicated in an inbox; signed outbox for our own events; operator replay that cannot approve anything.
- An agent loop over five typed, customer-scoped tools with a tool budget, evidence-bearing escalation and a guardrail that blocks recovery claims the case state does not support. Same tools exposed over MCP (stdio). Optional A2A gateway and UCP/ACP fixture ingestion as evidence.

## Run it

Requires Python 3.9+. No network access or credentials are needed.

```bash
cd Refund-and-Fee-Recovery-Agent
make venv                         # or: python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
make test                         # 69 tests
make demo                         # three demo scenarios; writes docs/demo-output.md
make eval                         # 38 labeled cases, agent vs rules-only; writes docs/eval-report.md
make seed && make api             # API on :8000 with the fixture clock (uvicorn --factory)
make worker                       # in another shell: durable worker + simulator callbacks
make frontend                     # React views on :5173 (proxies /v1 to the API)
make mcp                          # MCP server over stdio for cus_demo_4
docker compose up --build         # API + worker + frontend; set API_PORT / FRONTEND_PORT if 8000 or 5173 are taken
```

Fixture tokens: `tok_demo_customer`, `tok_demo_other_customer`, `tok_demo_operator` (`fixtures/sessions.json`).

Quick API walk-through (see [`docs/api-contract.md`](api-contract.md)):

```bash
H='Authorization: Bearer tok_demo_customer'; O='Authorization: Bearer tok_demo_operator'
curl -s -X POST localhost:8000/v1/recovery-cases -H "$H" -H 'content-type: application/json' \
  -d '{"customer_id":"cus_demo_4","order_ref":"order_mock_499","reason_code":"promised_refund_missing","target_minor":8499,"currency":"USD","evidence_ids":["receipt_mock_1","promise_mock_1"]}'
curl -s -X POST localhost:8000/v1/recovery-cases/<id>/agent-turns -H "$H" -H 'content-type: application/json' -d '{}'
curl -s -X POST localhost:8000/v1/actions/<action_id>/approve -H "$H" -H 'content-type: application/json' \
  -d '{"expected_case_version":4,"action_payload_hash":"sha256:…","approval_challenge_id":"challenge_…"}'
curl -s -X POST localhost:8000/v1/ops/worker/run -H "$O"
curl -s -X POST localhost:8000/v1/ops/clock/advance -H "$O" -H 'content-type: application/json' -d '{"days":3}'
curl -s localhost:8000/v1/recovery-cases/<id>/timeline -H "$H"
```

## Measured outcomes (this repository, 26 Sep 2026)

These are outcomes of the prototype against its own simulators and fixtures, not customer or market results.

| Gate from the plan | Result |
|---|---|
| Final recovered and outstanding amounts match every labeled fixture to one cent | 38/38 labeled cases pass in agent mode and in rules-only mode (25 dev + 13 held-out); see [`docs/eval-report.md`](eval-report.md) |
| Every external message is customer-approved and recipient-verified | Enforced by policy + executor re-check; tested (`test_executor_reverifies_authority_before_side_effect`, `test_8_malicious_document_cannot_redirect_recipient_or_change_reason`) |
| Provisional-credit and overlapping-recovery cases handled explicitly | Tested (`test_4`, `test_6`, `test_7`, eval families `issuer_*`, `merchant_and_issuer_overlap`) |
| No unapproved external writes, cross-customer access or duplicated side effects | Tested (`test_5`, `test_5b`, `test_timeout_after_accept_*`, `test_malformed_*`, `test_auth_and_tenant_isolation`); simulator case count checked after duplicate deliveries and retries |
| Every claimed completion has a provider/evidence reference | `completion_evidence_ref` is required to reach `recovered`; operator release to `recovered` is refused without it |
| Restart the worker mid-case without losing approvals, timers or evidence | Tested (`test_worker_restart_recovers_approvals_timers_and_evidence`) over a real database file |
| Agent explains a merchant/account mismatch without claiming recovery | Tested (`test_agent_explains_merchant_account_mismatch_without_claiming_recovery`); guardrail tested with an over-claiming fake model |
| Only approved messages delivered; retries do not create new cases | `merchant_mock.case_count()` stays 1 across timeout-after-accept, duplicate approvals, duplicate webhooks |

Test suite: 69 tests, all passing (`make test`). Eval unnecessary questions: 0; unsupported recovery claims emitted: 0.

Caveats on the numbers: the planner in this repository is deterministic (`RulesPlanner`) because no model credentials are available, so the "agent" column measures the tool loop, budget, guardrail and workflow rather than a language model. `OpenAICompatibleModel` is the swap-in point for measuring a real model with the same harness and labels. The React inbox, case, and operator views compile (`npm run build`) and were walked through Demo 1 (approve → recovered) and the already-refunded fixture against a local API.

## Repository layout

```text
backend/app/
  api/            FastAPI routes, fixture-token sessions, schemas
  agent/          prompts, typed tools, planner / LLM interface, orchestrator, MCP server, A2A gateway
  domain/         money, models, reconciliation, totals, state machine, policy, deadlines
  adapters/       provider interface, mock merchant, mock issuer + statement feed, sandbox placeholder, UCP/ACP ingestion, callback pump
  workflows/      case service, approvals, messages, jobs, worker, provider event handler
  persistence/    SQLite database, repositories, outbox, inbox
  evaluation.py   labeled-case harness (agent vs rules-only)
  container.py    composition root + fixture seeding
frontend/         React + TypeScript customer and operator views (Vite)
fixtures/         synthetic customer bundle, provider scenarios, issuer deadline config, commerce events, sessions, 38 labeled eval cases
migrations/       portable SQL schema
scripts/          seed, demo, eval, worker
tests/            domain, failure cases, workflow, API, agent/MCP/A2A, evaluation gate
docs/             architecture, API contract, capability matrix, demo script + output, eval report, trade-offs
docker-compose.yml, Dockerfile, Makefile
```

## Documents

- [Architecture](architecture.md) · [API contract](api-contract.md) · [Provider capability matrix](capability-matrix.md)
- [Demo script](demo-script.md) · [Demo output](demo-output.md) · [Evaluation report](eval-report.md) · [Trade-off note](trade-offs.md)
- The original development plan is plan.md (kept locally).

## Scope and limits

Version 1 covers one synthetic US customer, `promised_refund_missing` in USD, a mock merchant desk and a mock issuer dispute lane. Duplicate billing, unauthorized transactions and goodwill fee waivers have reason codes and a fixed dispute-reason table but are recorded as `not_supported`. Real merchant communications and issuer disputes require explicit customer authorization, supported interfaces and current issuer/legal deadline sources; the fixture deadlines here are placeholders.
