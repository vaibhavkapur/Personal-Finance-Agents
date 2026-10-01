---
title: "Architecture"
layout: default
nav_order: 3
---


# Architecture

```text
Customer / reviewer (React views)  ──►  FastAPI (backend/app/api)
                                            │  authenticated session = approver
                                            ▼
                     Agent orchestrator (backend/app/agent) ── typed tools ──┐
                        planner behind ToolCallingModel, tool budget,        │
                        claim guardrail; MCP stdio server; A2A gateway       │
                                            │                                │
                                            ▼                                ▼
                     CaseService (backend/app/workflows) ◄── deterministic domain engine
                        create / reconcile / drafts / approvals /        (backend/app/domain)
                        execute / follow-ups / provider events            money, matching, totals,
                                            │                              state machine, policy, deadlines
                                            ▼
                     Repositories + outbox + inbox (backend/app/persistence, SQLite)
                                            │
                        jobs with leases ──► Worker ──► RecoveryProviderAdapter
                                                              │
                                             merchant_mock / issuer_mock / statement feed
                                             (backend/app/adapters, controllable clock)
```

## Components

| Plan component | Where | Notes |
|---|---|---|
| Purchase and evidence matcher | `CaseService.create_case`, `find_refund_evidence`, `adapters/commerce_events.py` | Links receipt, promise, order and UCP/ACP fixture events by `order_ref`; tenant ownership enforced on every lookup. |
| Refund reconciliation engine | `domain/reconciliation.py`, `domain/totals.py` | Pure functions. Exact matches by provider reference; candidates by amount + merchant + instrument + window; every rejection carries reasons. Ambiguity ⇒ customer question. |
| Recovery coordinator | `CaseService.draft_merchant_message`, `draft_issuer_dispute`, `domain/policy.py` | Merchant lane first; issuer lane only after configured eligibility (declined / claimed-but-not-posted / unresponsive) and with a separate approval. |
| Communication and approval service | `workflows/messages.py`, `workflows/approvals.py`, `CaseService.approve_action` | Recipient from the verified registry only; payload hash + challenge + expiry + case version; executor re-verifies immediately before the side effect. |
| Outcome tracker | `domain/totals.py`, `CaseService._derive_state` | Requested / promised / target / final / provisional / store / reversed / outstanding; overlap and provisional-while-covered ⇒ `manual_review`. |

## State

Case lifecycle is enforced in `domain/state_machine.py` exactly as drawn in the plan, plus `manual_review` as a non-terminal hold. Every transition writes a `case_events` row with previous state, next state, source event id, actor, timestamp and the expected case version; the case row uses optimistic concurrency (`version`).

Merchant and issuer channels are separate `recovery_channels` rows under one case, each with its own provider reference, deadline, status and follow-up count.

## Money

Integer minor units with explicit currency everywhere. Statement transactions have an explicit `direction`; amounts are never negative. Totals never mix currencies. Store credit is tracked on the case, not as a card credit match. A provisional issuer credit is a `credit_matches` row of kind `provisional`; it only becomes `final` when the issuer resolves the dispute (recorded as `credit.interpretation_revised`, preserving the original evidence).

## Side effects and retries

1. Approval binds `(action payload hash, challenge id, case version)`.
2. Worker leases the `execute_action` job, re-verifies authority, marks the action `submitting`, commits.
3. Provider call runs outside any transaction with a unique `request_ref`.
4. Success ⇒ `submitted` + provider reference + approval consumed. Declined ⇒ channel declined, case re-routed. Timeout / malformed ⇒ `unknown`; a `resolve_uncertain_write` job calls `find_action(request_ref)`. If the provider has no record the same `request_ref` is retried once; if the outcome is still uncertain, or the adapter lacks `find_action`, the case goes to `manual_review`. No new side effect is ever generated for an uncertain write.
5. Provider callbacks go through `provider_events_inbox` (dedup by provider + event id) and idempotent handlers; the operator replay tool re-runs stored payloads and cannot touch approvals.
6. Our own events go through a transactional `outbox`, signed with HMAC-SHA256.

## Agent boundary

The model sees five typed, customer-scoped tools (`agent/tools.py`). Each response carries `source`, `retrieved_at`, `authority` (`authoritative` for our records, `simulated` for mocks) and `environment`. The orchestrator has a tool-call budget, escalates with evidence when it is exhausted, logs each run to `tool_runs` with redacted input/output hashes, and applies a deterministic guardrail so a recovery claim can only be emitted when the case is `recovered` or `already_refunded`. The planner shipped here is deterministic (`RulesPlanner`); `OpenAICompatibleModel` shows the single structured interface a real model plugs into.

MCP: `agent/mcp_server.py` exposes the same tools over stdio JSON-RPC (pinned to the 2025-06-18 message shapes). A2A: `agent/a2a.py` is a small gateway that pins a protocol version and payload schema and maps external task ids to case ids; it only carries already-approved payloads.

## What is mock vs. real

Everything external is simulated. See `capability-matrix.md`. The database is SQLite for the prototype (schema written in portable SQL); PostgreSQL is the intended production store.
