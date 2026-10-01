---
title: "Architecture"
layout: default
nav_order: 3
---



# Architecture

Prototype scope: one US state (CA fixture), one product class (renters), three fictional insurers, mock providers only.

```text
Customer / operator (React + TS)  ──►  FastAPI (bearer-token sessions, tenant-scoped)
                                          │
                                          ├── agent/        orchestrator (tool budget) → typed tools → LLM provider (scripted | OpenAI-compatible)
                                          │                 MCP stdio server exposes the same tools (protocol 2025-06-18)
                                          ├── workflows/    case service, state machine, actions + approvals, executor, worker (leased jobs), inbox/outbox
                                          ├── domain/       needs (unknown-aware), renters-quote/v1, comparison engine, application builder, issuance verifier
                                          ├── adapters/     InsurerQuoteAdapter protocol
                                          │      ├── mock/direct   in-process MockInsurer engine (faults, controllable clock)
                                          │      └── a2a/client    JSON-RPC client pinned to A2A 0.3.0  ──►  mock/a2a_server (independently runnable insurer agents)
                                          └── persistence/  SQLAlchemy (SQLite dev / PostgreSQL), outbox, inbox, jobs
```

## Components (plan §4)

| Plan component | Code |
|---|---|
| Needs interview | `domain/needs.py` (`InsuranceNeeds`, `UNKNOWN`, material-field versioning), `CaseService.record_answers` |
| Quote coordinator | `CaseService.request_quotes` / `_dispatch_quote_request` / `_apply_task_result`; `quote_tasks` keep insurer task ids, questions and quote versions under one `correlation_id` |
| Coverage comparison engine | `domain/coverage.py` — hard checks first, then ranking; each check and difference row carries a clause citation |
| Application service | `domain/application.py` + `ApplicationService.prepare_application` (review screen, action + challenge, idempotency key) |
| Issuance verifier | `domain/issuance.py` + `ApplicationService._handle_issued` (bound → issued → verified/completed or manual_review) |

## Case lifecycle

States are exactly those in plan §8 plus `manual_review`. `workflows/states.py` enforces allowed transitions server-side; every transition writes a `case_events` row (previous/next state, actor, timestamp, expected version) and an outbox event in the same transaction. Case `version` increments on every event; approvals compare against it.

A2A task states (`working`, `input-required`, `completed`, `rejected`) live on the insurer side and are mapped to `quote_tasks.status`. A completed A2A task with a quote artifact never changes the application state beyond `awaiting_selection`.

## Provider calls and reconciliation

1. Persist the pending record (`quote_tasks` row, `actions.status=executing` + `approvals.consumed_at`) and commit.
2. Call the adapter outside any transaction (`AppContext.call_provider` logs a redacted `provider_requests` row with latency and outcome).
3. Reconcile in a new transaction.

`ProviderTimeout` means *unknown*: the task goes to `timeout` (poller re-checks by the same request reference), the action goes to `uncertain` (`reconcile_action` asks `get_policy_status(request_ref)` before ever retrying the write). Adapters advertise `status_lookup_by_request_ref`; without it an uncertain write goes straight to `manual_review`.

## Authority model

- The model only sees the tools in `agent/tools.py`; none of them approve, submit, bind or complete.
- `prepare_application` produces an `actions` row with a `challenge_id`, `payload_hash` and `expected_case_version`. `POST /v1/actions/{id}/approve` requires all three and the authenticated owner; approvals bind to insurer, product, premium, limits, deductible and effective date (`approvals.scope_json`).
- The executor (`ApplicationService.execute_action`) re-verifies approval validity, hash, case version/state and application status immediately before the side effect and consumes the approval once.
- Material changes (needs fields, material insurer answers, revised offers, quote expiry) invalidate open actions and revoke unconsumed approvals.
- Documents and provider text are returned as `untrusted_text`; the tool layer never interprets them. Cross-tenant tool calls fail because `AgentTools` is constructed for one customer.

## Worker

`workflows/worker.py` claims `jobs` rows with a lease (`lease_until`, `lease_owner`). Jobs: `execute_action`, `reconcile_action`, `poll_underwriting`, `poll_quote_task`. Each pass also delivers the outbox (HMAC-signed) and runs the quote-expiry timer. Leases expire, jobs are idempotent, and approvals live in the database, so a worker restart mid-case loses nothing (`tests/test_worker_recovery.py`).

## Clock

`FixtureClock` is shared by the app, worker and in-process insurers. `frozen=True` for tests; the running server uses an offset clock so scheduled jobs still become due. `POST /v1/operator/clock/advance` moves it (and, in A2A mode, the remote mock insurers' clocks).
