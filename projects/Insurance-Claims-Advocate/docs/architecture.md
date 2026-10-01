---
title: "Architecture"
layout: default
nav_order: 3
---



# Architecture

Application-owned design for this prototype. Provider names and endpoints in examples are fixtures.

## Runtime

```text
┌──────────────┐   Bearer token    ┌─────────────────────────────┐
│ Customer UI  │ ───────────────► │ FastAPI  /v1/claims…        │
│ Operator UI  │                   │ Agent orchestrator          │
└──────────────┘                   │ CaseService (domain)        │
                                   │ Approvals + state machine   │
                                   └─────────────┬───────────────┘
                                                 │ pending Action
                                   ┌─────────────▼───────────────┐
                                   │ Worker (job leases)         │
                                   │ Executor → ClaimsAdapter    │
                                   └─────────────┬───────────────┘
                                                 │
                      ┌──────────────────────────┼──────────────────────────┐
                      ▼                          ▼                          ▼
               Mock insurer               A2A insurer agent           Sandbox adapter
               (in-process)               /insurer-agent              (not onboarded)
                      │                          │
                      ▼                          ▼
               Signed events ──► POST /v1/provider-events/claims|payments
                                 inbox dedupe by (provider, event_id)
```

One API process and one worker are enough. Compose adds Postgres. The mock insurer also serves the A2A claims agent on the same host so a separately deployed advocate can be pointed at it with `CLAIMS_ADAPTER=a2a`.

## Domain engine (not the model)

| Module | Role |
|---|---|
| `domain/extraction.py` | Page/line locators, uncertainty flags, hostile-instruction warnings. Never invents a missing date. |
| `domain/policy.py` | Clause text + character offsets + reviewer-approved rules. Version chosen by loss date. |
| `domain/calculator.py` | `supported` / `excluded` / `uncertain` / `duplicate`. Integer minor units. Cap applied last. |
| `domain/packet.py` | Itemized packet + disclosure manifest. Building a packet is not submitting a claim. |
| `domain/decision.py` | Compares insurer reasons to policy + facts; decides whether an appeal is supported. |
| `domain/settlement.py` | Accepted, paid and outstanding stay separate. Provisional / wrong-payee / unrelated credits do not close. |

Expense eligibility uses receipt hashes plus merchant/date/amount matching. Duplicates are flagged, not deleted. Repeat purchases can be confirmed as distinct.

## Case lifecycle

```text
collecting -> evaluating -> awaiting_approval -> submitted -> under_review
under_review -> evidence_requested -> awaiting_approval -> submitted
under_review -> partially_approved | approved | denied
approved -> payout_pending -> paid -> closed
partially_approved -> payout_pending | appeal_review
denied -> appeal_review -> awaiting_approval -> submitted
denied -> closed_unpaid
```

`manual_review` is operational, not success. Transitions are enforced in `workflows/state_machine.py`. Each event stores previous/next state, actor, timestamp, source event ID and the case version it expected. Submission sequence numbers keep supplemental packets on the original insurer claim.

A decision object is independent of payment state: an approved portion can be paid while another portion is disputed.

## Approvals and side effects

1. `prepare_claim_action` (or the HTTP draft endpoints) writes an `Action` with an immutable payload hash and an approval challenge.
2. Only the claimant or authorized representative can approve. The request binds `expected_case_version`, `action_payload_hash` and `approval_challenge_id`.
3. Stale case version → `409`. Expired or changed payload → rejected.
4. The worker re-checks authority immediately before the adapter call.
5. Provider writes are idempotent on `request_ref`. Replay with the same key returns the original result; the same key with different content fails.
6. Timeout after accept leaves the outcome **unknown** until `find_submission` (or manual review). A timeout before accept does not create a claim.

The model cannot self-approve.

## Events, outbox, inbox

Local notifications go through a transactional outbox. Incoming provider events are accepted at-least-once and deduplicated by `(provider, provider_event_id)`. Consumers are safe to repeat. Webhook signatures use the mock HMAC scheme (`X-Mock-Signature: sha256=…`).

Deadlines come from dated policy/provider fixtures (for example, the mock evidence-request window is 14 days on the fixture clock). There is no encoded “universal US claim deadline.”

## Agent boundary

Typed tools: `extract_claim_evidence`, `evaluate_policy_facts`, `build_claim_packet`, `prepare_claim_action`, `reconcile_claim_payment`, plus `get_case_status`, `get_policy` and `ask_customer`.

Every tool result includes `source`, `retrieved_at`, `authority` (`authoritative` | `estimated` | `simulated`) and `environment`. Coverage estimates are never treated as the insurer’s decision.

MCP exposes a read/prepare subset over stdio (protocol **2025-06-18**). A2A maps external task IDs to internal case/action IDs (protocol **0.3**). Ordinary function calls are used between in-process modules.

## Persistence

SQLAlchemy models cover the plan tables (`claim_cases`, `claim_facts`, `claimed_expenses`, `claim_submissions`, `insurer_requests`, `claim_decisions`, `claim_payments`) plus shared `cases`/`documents`/`actions`/`approvals`/`case_events`/`tool_runs`, jobs, outbox, inbox, and mock-insurer state. Amounts are integer minor units with a currency. Tenant ownership and unique provider/event references are enforced.

See `migrations/001_initial.sql` (generated from the models).
