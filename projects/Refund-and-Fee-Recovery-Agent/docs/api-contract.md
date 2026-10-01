---
title: "Application API contract"
layout: default
nav_order: 22
---


# Application API contract

These endpoints are owned by this application. They are not endpoints of any bank, merchant, protocol or government service. Authentication is a fixture bearer token (`fixtures/sessions.json`): `tok_demo_customer`, `tok_demo_other_customer`, `tok_demo_operator`. The approver of any action is always the authenticated session.

Errors are `{"error": {"code", "message"}}` with `404` not found, `403` forbidden / cross-tenant, `409` stale version or conflict, `422` policy or validation.

## Customer

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/recovery-cases` | Create the working case. Does not contact anyone. |
| `GET` | `/v1/recovery-cases` | Recovery inbox: charge, promise, missing amount, next decision. |
| `GET` | `/v1/recovery-cases/{id}` | Status with amount categories, channels, pending question, deadline alert, next step. |
| `POST` | `/v1/recovery-cases/{id}/reconcile` | Deterministic credit matching; returns exact/candidate/rejected matches with reasons and labelled amount categories. |
| `POST` | `/v1/recovery-cases/{id}/merchant-message-drafts` | Draft a merchant follow-up + approval challenge. `Idempotency-Key` header supported. |
| `POST` | `/v1/recovery-cases/{id}/dispute-drafts` | Separate issuer dispute packet + separate approval challenge (eligibility enforced). |
| `POST` | `/v1/recovery-cases/{id}/answers` | Answer the pending customer question (ambiguous match, store-credit preference, missing promise). |
| `POST` | `/v1/recovery-cases/{id}/agent-turns` | Run one agent turn over the case (loads case state, not chat history). |
| `POST` | `/v1/recovery-cases/{id}/close-unresolved` | Record an explicit unresolved outcome. |
| `GET` | `/v1/recovery-cases/{id}/timeline` | Events, channels, credit matches, actions (payloads omitted), amounts. |
| `GET` | `/v1/recovery-cases/{id}/actions/{action_id}` | Review screen for a drafted action. |
| `POST` | `/v1/actions/{action_id}/approve` | Approve exactly one action. |
| `POST` | `/v1/commerce-events` | Ingest a UCP / ACP fixture event as evidence (grants no authority). |

### `POST /v1/recovery-cases`

```json
{"customer_id": "cus_demo_4", "order_ref": "order_mock_499", "reason_code": "promised_refund_missing", "target_minor": 8499, "currency": "USD", "evidence_ids": ["receipt_mock_1", "promise_mock_1"]}
```

→ `201`

```json
{"id": "recovery_…", "status": "detected", "version": 1, "next_step": "reconcile_account_credits"}
```

Rejections: order not owned by the customer (`404`), evidence owned by another customer (`403`), open case already exists (`409 case_exists`), target above the purchase (`422`). Unsupported reason codes create a case that closes as `not_supported`.

### `POST /v1/recovery-cases/{id}/reconcile` → `200`

```json
{
  "status": "investigating", "version": 3,
  "amounts": {"currency": "USD", "requested_minor": 8499, "promised_minor": 8499, "target_minor": 8499, "final_recovered_minor": 0, "provisional_minor": 0, "store_credit_minor": 0, "reversed_minor": 0, "outstanding_minor": 8499, "final_by_channel": {"merchant": 0, "issuer": 0}, "overlap_flagged": false, "overlap_minor": 0},
  "exact_matches": [], "candidate_matches": [], "rejected": [{"transaction_id": "txn_cred_other_merchant", "amount_minor": 8499, "currency": "USD", "reasons": ["merchant_mismatch"]}],
  "ambiguous": false, "summary": "no matching credit found on the account feed", "pending_question": null,
  "next_step": "draft_merchant_message",
  "labels": {"final_recovered_is_cash": true, "provisional_is_not_recovery": true, "store_credit_is_not_card_credit": true}
}
```

### Draft endpoints → `201`

```json
{
  "action_id": "act_…", "type": "send_merchant_message", "status": "awaiting_approval",
  "payload_hash": "sha256:…", "expected_case_version": 4,
  "approval_challenge_id": "challenge_…", "challenge_expires_at": "2026-09-21T12:00:00Z",
  "review": {"lane": "merchant", "destination": {"recipient_ref": "merchant:mrc_mock_streaming", "address": "refunds@streambox.mock", "source": "merchant_registry", "verified_at": "…"},
             "documents": [{"document_id": "receipt_mock_1", "kind": "receipt", "content_hash": "sha256:…"}], "amount_minor": 8499, "currency": "USD",
             "terms": {"reason_code": "promised_refund_missing", "reminders_covered": 2}, "irreversible_effect": "Sends a message …", "subject": "…", "body": "…"},
  "next_step": "approve_pending_action"
}
```

Same `Idempotency-Key` with the same content returns the original action; the same key with different content is `409 idempotency_key_reuse`. A second draft while one is open is `422 duplicate_request`.

### `POST /v1/actions/{action_id}/approve`

```json
{"expected_case_version": 4, "action_payload_hash": "sha256:…", "approval_challenge_id": "challenge_…"}
```

`409 stale_version` when the case moved; `422 payload_changed`, `challenge_mismatch`, `challenge_expired` (the action becomes `expired`), `action_not_pending`. Success enqueues `execute_action` for the worker; nothing is sent inline.

## Provider

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/provider-events/recovery` | Provider callback. Header `X-Provider-Signature: hmac-sha256=…` over the canonical JSON body using the provider secret. Deduplicated by `(provider, event_id)`; duplicates return `outcome: duplicate_ignored`. |

Event types handled: `statement.transaction_posted`, `merchant.case_updated`, `merchant.store_credit_issued`, `issuer.dispute_updated`.

Application (outbox) event example:

```json
{"id": "evt_…", "type": "recovery.credit_posted", "case_id": "recovery_…", "occurred_at": "2026-09-26T12:00:00Z", "environment": "mock",
 "data": {"transaction_id": "txn_rf_…", "amount_minor": 5000, "credit_kind": "final", "remaining_minor": 3499}}
```

## Operator (operator token)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/ops/overview` | Cases, pending actions, jobs/leases, adapter requests, transitions, inbox, outbox, capabilities, simulator queues, metrics. |
| `GET` | `/v1/ops/metrics` | Tool errors, queue age, cases waiting on customer vs provider, approval abandonment, duplicate events ignored, tool calls per case. |
| `GET` | `/v1/ops/tool-runs` | Redacted tool runs (input/output hashes, latency, model/prompt version). |
| `POST` | `/v1/ops/worker/run` | Deliver due simulator callbacks and run the worker until idle. |
| `POST` | `/v1/ops/clock/advance` | Advance the fixture clock (`{"days": 1}`); mock only. |
| `POST` | `/v1/ops/events/replay` | Replay a stored provider event (idempotent; cannot approve). |
| `POST` | `/v1/ops/cases/{id}/release` | Release a `manual_review` case to a legitimate state; `recovered` requires zero outstanding and no overlap. |
| `POST` | `/v1/ops/simulator/faults` | Inject `declined`, `malformed_response`, `timeout_after_accept`, `delayed_callback` for an order. |
