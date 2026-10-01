---
title: "Architecture"
layout: default
nav_order: 3
---


# Architecture

```text
Borrower / operator (frontend/, static)  ──►  FastAPI (backend/app/api)
                                                │  bearer auth, tenant checks, error mapping
                                                ▼
                          Agent orchestrator (backend/app/agent)
                          planner (rules | optional LLM) → typed tools → tool_runs
                                                │  tool-call budget, escalation with evidence
                                                ▼
                          Case service + workflows (backend/app/workflows)
                          states · approvals · executor · worker(jobs, leases)
                                                │
                     ┌──────────────────────────┼──────────────────────────┐
                     ▼                          ▼                          ▼
        Domain engine (backend/app/domain)   Persistence (SQLAlchemy)   LenderAdapter (backend/app/adapters)
        amortization · loan_terms ·          cases, offers, actions,    mock network (simulator, faults,
        comparison · final_terms             approvals, events, jobs,   signed callbacks, controllable clock)
                                             outbox, inbox, tool_runs   sandbox stub (no capabilities)
                                                                                   │
                                                                A2A lender agents (backend/app/agent/a2a.py)
```

## Modules

| Module | Responsibility |
| --- | --- |
| `domain/amortization.py` | Level payment `M = P·r/(1-(1+r)^-n)`, zero-rate `P/n`, per-period half-up cent rounding, final-payment adjustment, schedule fingerprints, closed-form cross-check. `CALCULATION_VERSION` is stored on every comparison. |
| `domain/loan_terms.py` | Loan Estimate normalizer: CFPB categories A/B/C/E/H (incremental) vs F/G (pass-through), lender credits, source references, missing-field and contradiction flags. Embedded document text is data only. |
| `domain/comparison.py` | Horizon comparison: cumulative P&I + remaining balance + upfront costs − starting balance; financed costs go into principal and are never added again; break-even (economic, and simple fees/savings as a secondary view); sensitivity over horizons, ±$1,000 fees, financed vs cash; like-for-like principal normalization; incomplete/expired offers are reported, not ranked. |
| `domain/final_terms.py` | Field-by-field diff of approved offer vs final terms; material changes (`principal`, rate, term, financed fees, incremental costs, payment, new conditions) set `requires_reapproval`; prepaid/escrow-only changes are non-material. |
| `workflows/states.py` | State constants, allowed-transition table, server-side `transition()` with optimistic versioning and event recording. The `model` actor cannot transition. |
| `workflows/approvals.py` | Proposed actions with canonical payload hashes, single-use challenges, approvals bound to hash + scope + case version, `verify_authority()` before side effects, invalidation on material change, idempotency keys. |
| `workflows/case_service.py` | Case creation, fact confirmation, offer ingestion/versioning, comparison persistence, lender-request/application/document-release/closing drafts, final-term review, read models. |
| `workflows/executor.py` | Executes approved actions: pending-first, provider call outside the transaction, reconcile after. Timeouts/malformed responses → `uncertain` → lookup by original request reference with bounded backoff → `manual_review` if unresolved. Never re-submits. |
| `workflows/worker.py` | Persisted jobs with leases and retries; expired leases are reclaimed after a crash; housekeeping (offer expiry, outbox delivery). |
| `adapters/base.py` | `LenderAdapter` protocol, `AdapterCapabilities`, `ProviderResult{environment, source, retrieved_at, authority}`. |
| `adapters/mock_lender.py` | Deterministic lender network: refuse / small credit / match-rate-increase-fees / request-income-proof; application acceptance, conditions, final terms (with or without increased costs), mock closing with payoff record; fault injection; HMAC-signed callbacks; controllable clock. |
| `agent/tools.py` | Typed tools (`read_loan_terms`, `compare_loan_scenarios`, `prepare_lender_request`, `prepare_refinance_application`, `diff_final_terms`, plus `record_borrower_facts`, `get_case_state`). Each response carries source, retrieval time and authority and is logged to `tool_runs` with redacted input/output refs. |
| `agent/orchestrator.py` | Turn loop with tool budget, borrower-authorised local decisions (keep), escalation to `manual_review` with evidence, deterministic explanations (monthly relief, fees and remaining principal reported separately). |
| `agent/llm.py` | Optional OpenAI-compatible planner behind the same interface; degrades to rules on any error; tracks token cost. |
| `agent/mcp_server.py` | Minimal MCP stdio server (initialize, tools/list, tools/call), scoped to one borrower token. |
| `agent/a2a.py` | A2A agent cards and JSON-RPC handlers for each mock lender; `A2ALenderAdapter` client with protocol/schema pinning. |
| `agent/evaluation.py` | Labelled-case harness comparing the agent with a rules-only workflow on state and evidence. |

## Lifecycle

```text
collecting -> comparing -> awaiting_decision
awaiting_decision -> keep_current
awaiting_decision -> awaiting_approval -> negotiation_pending -> revised_offer -> comparing
negotiation_pending -> awaiting_decision            (refused / facts requested / quote expired)
awaiting_decision -> application_review -> awaiting_approval -> submitted
submitted -> conditions_outstanding -> submitted
submitted -> approved_offer -> final_review -> mock_closed
final_review -> awaiting_approval -> final_review    (material final-term changes need re-approval)
submitted -> declined | withdrawn
any non-terminal -> manual_review -> (operator) non-terminal state
```

`approved_offer` is not funded. `mock_closed` requires a simulator closing record; the existing mortgage is not marked repaid without a separate payoff record. `manual_review` is operational and non-terminal; operators cannot move a case to `mock_closed` or `keep_current`.

## Data model

See `backend/app/persistence/models.py` and `migrations/0001_initial.sql` (generated). Fiat values are integer minor units with currency; rates are decimal strings. Unique constraints: `(provider, provider_event_id)` on inbound events, `(case_id, idempotency_key)` on actions, `client_request_ref` on actions, `(case_id, sequence)` on case events, `dedupe_key` on jobs. Original documents (`documents.content_json`) are preserved separately from revised interpretations (`loan_offers.normalized_json.supplements`).

## Policy and authority model (implemented)

- Confirmed facts only: `record_borrower_facts` refuses income; application drafts reject income assertions that differ from verified facts.
- Source evidence: offers without rate, term, cost items, expiry or lock information are not ranked and cannot be cited to lenders or applied for.
- No "lower payment = cheaper": recommendations use horizon economic cost; remaining principal is always reported.
- Explicit approval: lender messages, application submission, document release and closing instruction are separate proposed actions with review screens listing destination, documents, terms and irreversible effect.
- Binding: approval = action hash + lender + offer version + documents + case version; expires in `APPROVAL_TTL_SECONDS`; invalidated on material change.
- Re-verification: the executor calls `verify_authority` immediately before the provider call and consumes the approval once.
- The model cannot self-approve, set states or mark completion; documents and provider messages are untrusted data.

## Events, retries, reconciliation

- Transactional outbox (`outbox`) with HMAC signatures for our own notifications; a worker delivers pending rows.
- Inbound provider events (`provider_events`) are verified, deduplicated and processed by a job that re-reads authoritative state by reference.
- Reads retry with bounded backoff; writes are never retried blindly — an uncertain write is looked up by `client_request_ref` (30s, 120s, 600s) then held for manual review.
- Operator replay re-enqueues the normal handler; it cannot bypass approvals.

## Observability

`/v1/ops/metrics` reports cases by state, waiting on customer vs provider, queue depth/age, tool errors, abandoned/invalidated approvals, duplicates prevented and planner cost per completed case. `tool_runs` records model/prompt version, latency, authority and redacted refs. Operator views redact token/secret-like fields; private model reasoning is not stored.
