---
title: "Application API"
layout: default
nav_order: 22
---


# Application API

Interactive schemas are served at `/docs`; machine-readable OpenAPI is `/openapi.json`. All endpoints below require an authenticated synthetic session unless noted. JSON body fields not present in the declared schema are rejected.

## Sessions

`POST /v1/demo/session` accepts `{"persona":"customer"}` (default), `operator`, or `other_customer`. The response includes a bearer token. The default customer also receives the `passage_session` HttpOnly cookie. Tokens expire after 12 wall-clock hours. Demo identity selection is intentionally credential-free and must be replaced for production.

`GET /v1/bootstrap` returns the current customer's masked recipients, fixture documents, cases, and simulator time.

## Transfer journey

1. `POST /v1/remittance-cases`: use `customer_id`, `beneficiary_id`, `budget_mode`, `deadline_at`, and `purpose`. USD/INR are the only supported currencies. `total_sender_cost` accepts `source_budget_minor` and a null recipient target; `recipient_target` accepts `target_required_minor` and a null source budget.
2. `POST /v1/remittance-cases/{id}/quotes`: captures all three dated quotes. Active transfers cannot be requoted.
3. `GET /v1/remittance-cases/{id}/comparison`: ranks supported, unexpired options whose latest estimated arrival meets the saved deadline. No option meeting the deadline produces an explicit empty recommendation.
4. `POST /v1/remittance-cases/{id}/transfer-drafts`: requires an `Idempotency-Key` header, `quote_id`, `recipient_version`, `recipient_confirmed:true`, and `expected_case_version`. Returns the immutable action, digest, expiry, session-bound challenge, and resulting case version.
5. `POST /v1/actions/{id}/approve`: requires `expected_case_version`, `action_payload_hash`, and `approval_challenge_id`. Customer identity comes from the session. Stale state is HTTP 409. A repeated approval returns the original action and cannot create another transfer.
6. `GET /v1/remittance-cases/{id}`: reloads durable state, current quotes, transfer, evidence timeline, requirements, and receipt.
7. `GET /v1/remittance-transfers/{id}/receipt`: returns a downloadable JSON receipt only after delivery evidence is recorded. A mismatch receipt is clearly marked `exception` and the case stays in review.

Reusing an idempotency key with changed content fails. Action payloads cannot be updated. Repricing or changed beneficiary details require a new proposal. An unresolved transfer to the same beneficiary blocks preparing a replacement, including in another case.

## Documents and cancellation

`POST /v1/remittance-transfers/{id}/document-packets` accepts `expected_case_version`, `requirement_id`, and `document_ids`. Only the exact requested fields from owned synthetic documents can be included. The response is an approval draft, not a document transmission.

`POST /v1/remittance-transfers/{id}/cancellation-drafts` accepts `expected_case_version`. The response is an approval draft. Even after approval, cancellation remains a request until the provider confirms.

`POST /v1/actions/{id}/revoke` revokes an unconsumed draft/queued approval. An executing action cannot be revoked locally.

## Simulator and operations

- `POST /v1/demo/advance`, body `{"minutes":5}`: advance the shared frozen demo clock and drain due jobs. The same demo environment's cases share the clock.
- `GET /v1/operator/overview`: operator-only metrics, jobs, inbox, case and action summaries, and redacted tool runs.
- `POST /v1/operator/transfers/{id}/scenario`: operator-only scenario selection before approval: `normal`, `missing_document`, `timeout`, `short_payment`, `delay`, `rejection`, `cancel_denied`, `malformed`.
- `POST /v1/operator/inbox/{id}/replay`: replay the original deferred provider event without changing its identity or authorizing an action.

## Provider callbacks

`POST /v1/provider-events/remittance` uses `X-Provider-Signature`, the hex HMAC-SHA256 of the exact raw body with `MOCK_WEBHOOK_SECRET`. It requires event ID, known provider ID/transfer reference, timezone-aware occurred-at time, `environment:"mock"`, event type, and event-specific data. Delivered events need `provider_reference`, `currency:"INR"`, `actual_recipient_minor`, `actual_source_debit_minor`, and `actual_fee_minor`. Amounts must be nonnegative integers. Duplicate IDs with identical payloads are no-ops; conflicting payloads are rejected.

## MCP

Send JSON-RPC 2.0 requests to `POST /mcp` with the session bearer token. Pin protocol revision `2025-06-18`. Supported methods are `initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call`.

Tools are `validate_recipient`, `get_transfer_quotes`, `compare_delivery_options`, `prepare_transfer`, and `get_transfer_evidence`. Call `tools/list` for generated JSON schemas. There is deliberately no approve/execute tool. Tool results carry source, time, authority, and mock-environment fields.

`POST /v1/concierge/chat` accepts `case_id`, `message`, and a maximum-five `tool_budget`. The included rules-only concierge does one read and returns a concise explanation. No external model is called.

References used: [MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools), [FastAPI features](https://fastapi.tiangolo.com/features/), [Pydantic model validation](https://docs.pydantic.dev/latest/concepts/models/).
