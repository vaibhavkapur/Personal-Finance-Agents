---
title: "Application API contract"
layout: default
nav_order: 21
---



# Application API contract

Endpoints owned by this application. None of them are claimed endpoints of any
bank, protocol or government service. Interactive docs: `GET /docs` (OpenAPI).

## Authentication (demo)

The prototype takes the authenticated principal from headers:

| Header | Meaning |
| --- | --- |
| `X-Customer-Id: cus_demo_1` | authenticated customer session |
| `X-Role: operator` | operator session (read-only observability plus simulation controls; cannot approve or execute) |

A deployment replaces `app/api/deps.py::get_principal` with the identity
provider's session. Nothing else reads raw headers.

## Errors

```json
{"error": "liquidity_violation", "message": "…", "details": {"max_lockable_minor": 700000}}
```

| Status | Codes |
| --- | --- |
| 403 | `not_owner`, `destination_not_verified`, `customer_only`, `invalid_challenge`, `obligation_not_owner` |
| 404 | `case_not_found` (also for other tenants' cases), `deposit_not_found`, `offer_not_found`, `action_not_found` |
| 409 | `stale_case_version`, `payload_changed`, `case_already_open`, `invalid_state`, `liquidity_violation`, `offer_not_comparable`, `plan_stale`, `offer_changed`, `offer_expired`, `funds_already_committed`, `access_revoked`, `challenge_used`, `illegal_transition` |
| 410 | `challenge_expired` |
| 422 | validation errors, `invalid_amount`, `invalid_mode` |

## Customer endpoints

### `GET /v1/me/inbox`
Maturity inbox: deposits with maturity date, instruction deadline, bank default behaviour, open case summary; obligations on record.

### `POST /v1/banking-cases` → 201
```json
{"customer_id": "cus_demo_1", "deposit_id": "cd_demo_1", "currency": "USD",
 "minimum_buffer_minor": 100000, "obligation_ids": ["bill_demo_1"],
 "preferred_lockup_days": null, "buffer_includes_obligations": null}
```
```json
{"id": "bankcase_…", "status": "collecting", "version": 2,
 "missing_fields": ["preferred_lockup_days"], "outstanding_questions": [{"field": "…", "question": "…", "why": "…"}]}
```
One open case per deposit. `customer_id` must match the session.

### `GET /v1/banking-cases/{id}`
Full case view: summary, deposit, accounts (available vs current, pending, snapshot source/time, evidence), obligations, current plan (projection, options, assumptions, inputs hash), review screen, instruction with reconciliation.

### `POST /v1/banking-cases/{id}/answers`
Supply information the records cannot: `preferred_lockup_days`, `buffer_includes_obligations`, `minimum_buffer_minor`, `concentration_limit_minor`, `obligation_ids`, `obligations: [{description, amount_minor, due_date, certainty}]`.

### `POST /v1/banking-cases/{id}/evaluate`
Refreshes snapshots and offers through the adapters, runs the projection and comparison, stores a draft plan. Produces options, never a transfer. Moves the case to `evaluating` or `needs_information`. A case in `awaiting_approval` is re-evaluated and its proposed action superseded.

### `GET /v1/banking-cases/{id}/options`
Latest stored plan: `max_lockable_minor`, projection, options (with `comparable`, `exclusion_reasons`, earnings at a common horizon, `locked_until`, `source`), assumptions, warnings, outstanding questions.

### `POST /v1/banking-cases/{id}/instructions` → 201
```json
{"option_id": "off_harbor_12m", "amount_minor": 700000}
```
Creates an immutable proposed action and moves the case to `awaiting_approval`. Rejects amounts that breach the buffer, offers that are not comparable, destinations outside the verified same-owner set and funds already committed by another instruction. Returns the review screen: exact instruction payload, source/destination, terms with source and version, irreversible effect, liquidity after allocation, documents, `action_payload_hash`.

### `GET /v1/banking-cases/{id}/timeline`
Case events (previous/next state, actor, expected version, source event id), redacted provider requests, instruction status and reconciliation, completion evidence reference.

### `POST /v1/banking-cases/{id}/cancel`
Allowed before bank acceptance. Cancellation after acceptance is a separate provider request and is not exposed to the customer in this MVP.

## Actions and approval

### `GET /v1/actions/{action_id}`
Review screen for the action.

### `POST /v1/actions/{action_id}/challenge`
Issues a short-lived approval challenge bound to the action payload hash and current case version.
```json
{"approval_challenge_id": "challenge_…", "action_id": "act_…", "action_payload_hash": "sha256:…", "expected_case_version": 4, "expires_at": "…"}
```

### `POST /v1/actions/{action_id}/approve`
```json
{"expected_case_version": 4, "action_payload_hash": "sha256:…", "approval_challenge_id": "challenge_…"}
```
The approver is the authenticated customer. `409` on stale version or changed payload; `410` on expired challenge. Success records an approval bound to the hash, moves the case to `approved` and enqueues the executor job. The approval expires at the earlier of the offer's validity and the renewal instruction deadline and is invalidated if material terms change before execution.

## Provider callbacks

### `POST /v1/provider-events/bank`
Headers: `X-Provider-Id`, `X-Signature` (HMAC-SHA256 over the canonical JSON body with the shared secret).
```json
{"id": "bevt_…", "type": "bank_instruction.effective", "occurred_at": "…", "environment": "mock",
 "data": {"request_ref": "req_…", "provider_reference": "bankref_…"}}
```
Deduplicated by `(provider, id)`; invalid signatures are stored and ignored. Consumers only schedule verification; they cannot bypass authorization.

## Agent

### `POST /v1/agent/turns`
```json
{"message": "My $10,000 CD matures next week…", "case_id": null}
```
Runs one bounded tool-calling turn for the authenticated customer and returns `reply`, `case_id`, `state`, `tool_calls`, `questions`, `refused`, `escalated`, `budget_exhausted`, `model_version`. Conversation state is the case record, not chat history.

### `GET /v1/agent/tools`
Tool schemas exposed to the model (also served over MCP by `python -m app.agent.mcp_server`).

## Operator (`X-Role: operator`)

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/operator/overview` | cases by state, waiting-on-customer vs provider counts, queue age, duplicate actions prevented, tool errors, pending actions, jobs, adapter requests, tool runs |
| `GET /v1/operator/outbox` | signed application events |
| `GET /v1/operator/inbox`, `POST /v1/operator/inbox/{id}/replay` | provider events and replay (cannot bypass authorization) |
| `GET /v1/operator/capabilities` | provider capability matrix |
| `GET /v1/operator/mock-bank/ledger` | bank-side balances, deposits and instructions |
| `POST /v1/operator/clock/advance` `{days, hours, minutes}` | advance the simulation clock; the bank processes due instructions and delivers callbacks |
| `POST /v1/operator/worker/run` | run due jobs once (the worker process does this continuously) |
| `POST /v1/operator/mock-bank/submit-mode` `{mode}` | `normal`, `accepted_not_effective`, `insufficient_available`, `declined`, `accepted_before_timeout`, `malformed_response`, `delayed_callback`, `completed_amount_mismatch` |
| `POST /v1/operator/mock-bank/offer-mode` `{mode, offer_id}` | `normal`, `changed_rate`, `expired_offer` |
| `POST /v1/operator/mock-bank/revoke-access` `{account_id, revoked}` | simulate revoked account access |
| `POST /v1/operator/reset` | drop and reseed the fixture |

## Case lifecycle

```
discovered -> collecting -> evaluating -> awaiting_approval
awaiting_approval -> approved -> submitted -> verifying -> completed
evaluating -> needs_information -> evaluating
approved -> needs_requote -> evaluating
submitted -> outcome_unknown -> verifying | submitted
submitted -> rejected
awaiting_approval -> cancelled
* -> manual_review (non-terminal; operator resolves)
```
Transitions are enforced in `app/workflows/states.py`; each records previous state, next state, event id, actor, timestamp and expected case version.
