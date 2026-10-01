---
title: "Application API"
layout: default
nav_order: 21
---



# Application API

These endpoints belong to **this application**. They are not claimed endpoints of any bank, insurer, protocol, or government service.

Authenticate with `Authorization: Bearer <fixture-token>`. Interactive OpenAPI: `/docs` when the API is running.

## Claims

### `POST /v1/claims`

Open a case. Extracts attached documents and evaluates immediately.

```json
{
  "customer_id": "cus_demo_3",
  "policy_id": "travel_policy_demo_1",
  "loss_type": "baggage_delay",
  "loss_at": "2026-09-10T14:00:00Z",
  "document_ids": ["itinerary_demo", "receipt_demo_1"],
  "mock_scenario": null
}
```

`mock_scenario` is mock-adapter only: `approved`, `evidence_requested`, `partial`, `denied`, or omitted to decide from packet content.

Response includes `id`, `status` (starts `collecting` then typically `evaluating`), `version`, `policy_version`, and `missing_fields` (for example `baggage_arrival_confirmation`).

### `POST /v1/claims/{id}/documents`

Attach more documents (same tenant/owner). Used when the insurer requests arrival confirmation.

### `POST /v1/claims/{id}/evaluate`

Re-run extraction + fixture rules. Does not submit.

### `POST /v1/claims/{id}/submission-drafts`

Create a submission or supplemental packet for review. Response includes `packet_id`, `content_hash` / `action_payload_hash`, `packet_type` (`submission` | `supplemental`), `disclosure_manifest`, and `review_summary` (destination, documents, amounts, excluded items, irreversible effect).

A supplemental packet references the existing `external_claim_ref`. It does not open a second insurer claim.

Fails when required evidence is missing.

### `POST /v1/claims/{id}/appeal-drafts`

Create an appeal packet. Fails when there is no supported challenge or no configured review process.

### `GET /v1/claims/{id}/timeline`

Ordered case events (transitions, questions, approvals, provider callbacks).

### `GET /v1/claims/{id}`

Customer view: checklist, expenses, totals (requested / supported / excluded / approved / paid / outstanding), open questions, pending action, decisions with policy citations, insurer requests, settlement, next step.

### `GET /v1/claims/{id}/export`

Exportable case history (JSON).

### `POST /v1/claims/{id}/questions/{question_id}/answer`

Customer confirmation for uncertain fields (delivery time, category, distinct purchase, loss date, and so on).

### `POST /v1/claims/{id}/decision/accept`

Customer accepts a final unsupported or upheld denial → `closed_unpaid` after review.

### `POST /v1/claims/{id}/reconcile`

Recompute settlement from posted mock payments.

### `POST /v1/claims/{id}/agent/turns`

Advocate turn. Body: `{ "message": "…", "planner": "scripted" }`. The orchestrator has a tool-call budget and escalates contradictions rather than inventing facts.

## Approvals

### `POST /v1/actions/{action_id}/approve`

```json
{
  "expected_case_version": 4,
  "action_payload_hash": "sha256:…",
  "approval_challenge_id": "challenge_…"
}
```

The approver is the authenticated principal. Expired challenge or changed payload is rejected. Stale case version returns **409**. A background executor consumes the approval. Repeated requests with the same idempotency key return the original action.

## Provider events

### `POST /v1/provider-events/claims`

### `POST /v1/provider-events/payments`

Signed callbacks. Header `X-Mock-Signature: sha256=<hmac>`. Deduplicated by provider + event ID.

Example application event shape (also emitted on the outbox):

```json
{
  "id": "evt_demo_001",
  "type": "claim.evidence_requested",
  "case_id": "claim_…",
  "occurred_at": "2026-09-25T16:00:00Z",
  "environment": "mock",
  "data": {
    "claim_reference": "insurer_mock_31",
    "request_id": "evidence_req_2",
    "document_type": "baggage_arrival_confirmation"
  }
}
```

## Other

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/me` | Principal from the token |
| GET | `/v1/claims` | Cases visible to the principal |
| GET | `/v1/documents` | Documents owned by the customer (operators see all fixtures) |
| GET | `/v1/policies/{id}?loss_at=` | Policy version for that loss date |
| GET | `/v1/provider/capabilities` | Adapter capability matrix |
| GET | `/v1/claims/{id}/operator` | Operator-only: actions, adapter log, transitions, tool runs |
| GET | `/health` | Environment, adapter, clock |

## Operator / fixture controls (`DEV_ENDPOINTS=1`)

Disabled with `DEV_ENDPOINTS=0`. None of these bypass action authorization.

- `POST /v1/dev/clock` — freeze / advance the fixture clock
- `POST /v1/dev/worker/run-until-idle`
- `POST /v1/dev/mock-insurer/faults` — timeout, malformed, declined, duplicate/out-of-order events
- `POST /v1/dev/mock-payments/emit` — exact, smaller, remainder, provisional, unrelated, wrong payee
- `GET /v1/dev/metrics`, `/v1/dev/jobs`, `/v1/dev/inbox`

## A2A insurer agent (mock)

- `GET /insurer-agent/.well-known/agent-card.json`
- `POST /insurer-agent/a2a` — JSON-RPC `message/send`, `tasks/get`

Protocol version **0.3**. Domain payload schema **`claims-advocate/a2a-claims/1.0`**.
