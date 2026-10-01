---
title: "Architecture"
layout: default
nav_order: 3
---



# Architecture

```text
Customer / reviewer
  -> React + TypeScript app (frontend/)           customer journey, operator view
  -> FastAPI (backend/app/api)                    authenticated API, demo session headers
  -> Agent orchestrator (backend/app/agent)       bounded tool loop; case record is the state
  -> Typed tools + deterministic domain engine    liquidity, offers, instructions, reconciliation
  -> Workflow services (backend/app/workflows)    state machine, approvals, executor, worker
  -> BankAdapter (backend/app/adapters)           mock bank simulator, sandbox capability stub
  -> Mock provider ledger (mock_bank_* tables)    independent balances, clock-driven callbacks
  -> Events (outbox / inbox), tool runs, adapter requests -> evidence timeline
```

## Modules

| Module | Responsibility |
| --- | --- |
| `app/domain/money.py` | integer minor units, currency, Decimal rounding conventions |
| `app/domain/liquidity.py` | day-by-day available-cash projection; buffer floor; pre-effective vs effective breaches; `max_lockable`; inputs hash |
| `app/domain/offers.py` | offer normalization with recorded missing conditions; like-for-like comparison at a common horizon; APY vs contractual accrual; exclusion reasons; material terms |
| `app/domain/instructions.py` | typed, canonical, hashable instruction payloads |
| `app/domain/reconciliation.py` | transfer legs / renewal record matching; never fabricates success |
| `app/adapters/base.py` | `BankAdapter` protocol, provider exceptions, capability flags, `_meta` provenance |
| `app/adapters/mock_bank.py` | simulator: own ledger, controllable clock, idempotent submit by `request_ref`, lookup by reference, failure modes, callbacks, default maturity behaviour |
| `app/adapters/sandbox_plaid.py` | read-only account-data adapter shape advertising only real capabilities |
| `app/workflows/states.py` | allowed transitions, versioned case events |
| `app/workflows/case_service.py` | create/collect/evaluate/prepare/approve; review screen; views |
| `app/workflows/refresh.py` | snapshot and offer refresh through adapters, outside DB transactions |
| `app/workflows/executor.py` | execute (re-verify authority, persist pending, submit once), reconcile (lookup by original reference), verify (bank evidence, reconciliation), inbox consumer |
| `app/workflows/worker.py` | persisted jobs with leases; retries with backoff; failed jobs hold the case |
| `app/workflows/simulation.py` | clock advance, callback delivery through the signed inbox path, replay |
| `app/persistence/*` | SQLAlchemy models, sessions, outbox/inbox, fixture seed |
| `app/agent/tools.py` | scoped typed tools with audit (`tool_runs`) |
| `app/agent/llm.py` | rules policy (baseline) and OpenAI-compatible policy behind one interface |
| `app/agent/orchestrator.py` | bounded turn; escalation with evidence |
| `app/agent/mcp_server.py` | MCP server exposing the tools (mcp 2.x `MCPServer`) |
| `app/agent/evaluation.py` | labeled-case harness and metrics |

## Key design decisions

**Provider calls outside transactions.** `evaluate_case`, `execute_action`, `reconcile_action` and `verify_action` each open short transactions before and after the adapter call. A pending action (`submitting`) and the consumed approval are committed *before* the provider is called, so a crash leaves an auditable record and the recovery path looks the write up by its original `request_ref` before ever calling submit again.

**Authority is checked twice.** `approve_action` validates version, payload hash and challenge; `execute_action` re-validates the approval (not revoked, not consumed, not expired, hash matches), refreshes the snapshot and offer, and invalidates the approval on any material change (`needs_requote`). The model never holds approval capability: no tool can approve, cancel or complete.

**Simulation clock.** A single DB-backed clock (`sim_clock`) drives approval expiry, snapshot staleness, maturity, job scheduling and the bank's processing, so tests and demos are deterministic and the same journey can be replayed.

**Independent bank ledger.** The mock bank keeps its own accounts, deposits and instructions. Verification reads bank-side evidence and reconciles it against the approved instruction; `completed_amount_mismatch` demonstrates a reconciliation exception holding the case open.

**Case record as conversation state.** Each agent turn loads the case summary (state, version, outstanding questions, references). The rules policy makes decisions from that record and the current message only; the model-backed policy receives the same record as authoritative context.

**Idempotency layers.**
- one open case per deposit; committed funds check across instructions on the same source account;
- `actions.idempotency_key` and `actions.request_ref` unique; the bank de-duplicates by `request_ref` and returns the original record;
- inbox de-duplicates provider events by `(provider, event_id)`; consumers only schedule idempotent verification;
- job enqueue de-duplicates pending jobs per `(type, action)`.

## Data model

See `migrations/0001_initial_*.sql` (generated from `app/persistence/models.py`). Tables follow plan §10: `bank_accounts`, `obligations`, `deposit_contracts`, `deposit_offers`, `cash_plans`, `bank_instructions`, plus shared `cases`, `documents`, `actions`, `approvals`, `approval_challenges`, `case_events`, `tool_runs`, `outbox_messages`, `inbox_events`, `jobs`, `adapter_requests`, `sim_clock`, and the simulator's `mock_bank_*` tables.

## Protocol boundaries

- **MCP**: `python -m app.agent.mcp_server` exposes the tools to any MCP client; the customer scope comes from the process environment, never from the model.
- **A2A**: not enabled. The boundary would sit between this application and independently deployed bank agents; the `BankAdapter` protocol is where such an agent would be wrapped, with external task ids mapped to internal case ids.
- **Aggregator sandbox**: `SandboxAccountDataAdapter` shows the capability shape (reads only, no lookups); execution stays on the coherent mock ledger.
- **FAPI / AP2**: not applicable to the mock provider; noted in the capability matrix.
