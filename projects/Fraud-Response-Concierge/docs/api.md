---
title: "API and MCP contract"
layout: default
nav_order: 21
---


# API and MCP contract

The generated `openapi.json` is the complete application contract. Run the app and open `/docs` to explore it. These are application-owned endpoints, not real institution APIs.

## Authentication

`POST /v1/demo/session` creates an eight-hour synthetic customer session and sets an HTTP-only SameSite Strict cookie. This endpoint is a deliberate demo shortcut. Every customer, action, MCP, and operator endpoint requires that session. It also accepts the same opaque session token as `Authorization: Bearer ...` for the stdio bridge. Tokens are hashed at rest and never included in ordinary API responses. An attacker cannot select the authenticated customer through a request body. Do not expose this single-user fixture service as production authentication.

## Customer workflow

- `GET /v1/instruments` — masked registry records, providers, transaction fixtures and fixture clock.
- `POST /v1/incidents` — create the working case; customer ID must match the session.
- `GET /v1/incidents` — list owned cases.
- `POST /v1/incidents/{id}/affected-instruments` — change affected cards before confirmation.
- `POST /v1/incidents/{id}/verify` — explicitly confirm the synthetic identity and intake narrative.
- `GET /v1/incidents/{id}/resolution` — independent substates, actions, timeline, references and closure eligibility.
- `POST /v1/incidents/{id}/transactions/{ref}/confirm` — customer recognition or unauthorized statement.
- `POST /v1/incidents/{id}/protective-action-drafts` and `/report-drafts` — prepare a lock, lost-card report, replacement, charge report or information response.
- `POST /v1/actions/{id}/approve` — approve the exact action hash and challenge at an expected case version.
- `POST /v1/actions/{id}/revoke` — revoke before dispatch. Already submitted actions cannot be undone locally.
- `POST /v1/actions/{id}/reconcile` — query the original provider request; never grants a new write.
- `POST /v1/actions/{id}/demo-handoff` — explicit simulated Northstar authentication, followed by reconciliation.
- `POST /v1/incidents/{id}/tasks/{task}/complete` — record recovery attestation after delivery.
- `POST /v1/incidents/{id}/close` — evidence-gated closure.

Mutations with an `expected_case_version` return `409` on stale state. Draft creation requires an `Idempotency-Key` header. A repeated key returns its original action; reuse for another case, action kind, target, or response statement fails. Per-target deduplication also prevents multiple unresolved copies with different keys. Expired drafts can be prepared again; consuming an approval after expiry is rejected.

## Simulator and operations

- `POST /v1/incidents/{id}/scenario` selects `normal`, `timeout_after_acceptance`, `delayed`, `declined`, or `malformed` for future provider submissions.
- `POST /v1/demo/clock/advance` advances the fixture clock by bounded whole minutes.
- `POST /v1/demo/worker/tick` processes a queued action; ordinary execution uses the independent worker.
- `POST /v1/incidents/{id}/simulate` emits a scoped synthetic investigation or replacement event.
- `GET /v1/incidents/{id}/operations` exposes redacted jobs, tool runs, provider-write count and unresolved-action metrics.

## Callback verification and replay

`POST /v1/provider-events/incidents` accepts the strict ProviderEvent schema. `X-Provider-Signature` is the lowercase hexadecimal HMAC-SHA256 of the exact raw request bytes using `PROVIDER_SHARED_SECRET`. Customer/provider/instrument/transaction and provider references must match. The environment must be `mock`. Reposting the same signed event is the operator replay mechanism: a duplicate returns `{"duplicate": true}` without repeating its state transition or any external action. The same event ID with changed content returns `409`.

## MCP

`POST /mcp` implements the JSON response mode of stateless MCP HTTP for protocol revision `2025-11-25`: `initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call`. No server-initiated stream, resources, prompts, sampling, A2A or OAuth discovery is advertised. The same API session is required. `GET /mcp` is not a supported event stream.

The five typed tools are:

- `get_verified_instruments(customer_id)`
- `prepare_protective_actions(incident_id)`
- `get_transaction_context(incident_id, transaction_id)`
- `prepare_incident_report(incident_id, provider_id)`
- `get_incident_resolution(incident_id)`

Tools validate Pydantic schemas, return simulated source provenance, and cannot approve or execute actions. Preparation only creates reviewable drafts. `POST /v1/incidents/{id}/chat` runs the bounded rules-only orchestrator using the same tools. An optional stdio transport is available with `python -m backend.app.agent.stdio`; it requires `CONCIERGE_TOKEN` and supports `CONCIERGE_URL` for the local API. Never include a bank credential in these variables.
