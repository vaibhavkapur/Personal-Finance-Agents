---
title: "Application API contract"
layout: default
nav_order: 22
---



# Application API contract

Endpoints owned by this application. They are not endpoints of any insurer, protocol or government service. OpenAPI is served at `/docs` when the API runs.

Authentication: `Authorization: Bearer <token>`. Fixture tokens: `tok_cus_demo_1`, `tok_cus_demo_2`, `tok_cus_demo_3` (customers), `tok_operator` (operator). Customers only see their own cases (`404` otherwise).

## Customer endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/me` | Authenticated customer profile (synthetic) |
| `GET` | `/v1/catalog` | State profile, insurer list with their exact questions, environment, adapter mode, fixture clock |
| `GET` | `/v1/insurance-shopping-cases` | List own cases |
| `POST` | `/v1/insurance-shopping-cases` | Create the working case → `201 {id, status:"collecting", version, missing_fields, needs_version}` |
| `GET` | `/v1/insurance-shopping-cases/{id}` | Full case view: needs, missing fields, outstanding insurer questions (verbatim), quote tasks, quotes, application, pending action, policy, timeline, `next_decision` |
| `POST` | `/v1/insurance-shopping-cases/{id}/answers` | `{answers:[{field,value}|{question_id,value}]}`; `value:"unknown"` allowed. Material changes bump the needs version, invalidate approvals and re-quote; `409` after submission |
| `POST` | `/v1/insurance-shopping-cases/{id}/quote-requests` | `202` with task references and per-provider statuses (`quoted`, `input_required`, `timeout`, `declined`, `failed`) |
| `GET` | `/v1/insurance-shopping-cases/{id}/comparison` | `renters-comparison/v1`: `suitable` (ranked), `excluded`, `undetermined`, `missing_responses`, `differences` (with citations), `trade_offs`, `ranking.disclosure`, `complete` |
| `POST` | `/v1/insurance-shopping-cases/{id}/selection` | `{quote_id}` — record a selection (suitable quotes only) |
| `POST` | `/v1/insurance-shopping-cases/{id}/applications` | `{quote_id, answers_version?, idempotency_key?}` → `201` review screen, `approval_challenge_id`, `action_payload_hash`, `expected_case_version`. `409` if unsuitable, incomplete, stale quote or key reused with different content |
| `GET/POST` | `/v1/insurance-shopping-cases/{id}/messages` | Agent conversation; `POST {text}` → `{reply, tool_calls, escalated, case_status}` |
| `POST` | `/v1/actions/{action_id}/approve` | `{expected_case_version, action_payload_hash, approval_challenge_id}` → approval bound to the exact payload. `409` stale version / changed payload / wrong challenge, `410` expired challenge |
| `POST` | `/v1/actions/{action_id}/reject` | `{reason?}` |

### Example: create case

Request

```json
{"customer_id": "cus_demo_2", "state_code": "CA", "product": "renters", "desired_effective_date": "2026-11-01",
 "property_limit_minor": 3000000, "liability_limit_minor": 10000000, "replacement_cost_required": true}
```

Response `201`

```json
{"id": "shopcase_…", "status": "collecting", "version": 1, "missing_fields": ["address", "deductible_preference"], "needs_version": 1}
```

## Provider events

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/provider-events/insurer` | Headers `X-Insurer-Id`, `X-Insurer-Signature` (`v1=<hmac-sha256(ts + "." + body)>`), `X-Insurer-Timestamp`. Deduplicated on `(provider, event_id)`. Never changes state directly: schedules a status check by request reference. `401` bad signature |

## Operator endpoints (operator token)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/operator/cases` | All cases with state and review reason |
| `GET` | `/v1/operator/cases/{id}` | Redacted case view plus `events`, `provider_requests`, `tool_runs`, `jobs` |
| `GET` | `/v1/operator/metrics` | Cases by state, waiting on customer vs provider, tool errors, queue depth/age, approval abandonment, duplicate events seen, provider outcomes, capability matrix |
| `POST` | `/v1/operator/worker/run-once` | Process due jobs once (useful with a fixture clock) |
| `POST` | `/v1/operator/clock/advance` | `{minutes, hours, days}` (mock/fixture clock only; propagated to remote mock insurers) |
| `POST` | `/v1/operator/faults` | `{insurer_id, kind, operation?, params}` — `timeout`, `timeout_before`, `malformed`, `decline_next_application`, `effective_date_shift_days`, `delayed_callback` |
| `POST` | `/v1/operator/replay/{inbox_id}` | Re-process a stored provider event (cannot bypass approvals) |
| `POST` | `/v1/operator/cases/{id}/resolve-review` | `{resolution: resume_underwriting|back_to_selection|decline|back_to_collecting}` |

## Domain schemas (versioned)

- `renters-quote-request/v1` — normalized requirements sent to every insurer (`domain/needs.py:to_requirements`)
- `renters-quote/v1` — comparable quote artifact (`domain/quotes.py`)
- `renters-application/v1` — exact application payload (`domain/application.py`)
- `renters-comparison/v1`, `policy-verification/v1`, `renters-declarations/v1`

## A2A surface of each mock insurer agent

`GET /.well-known/agent-card.json` (protocolVersion `0.3.0`), `POST /a2a` JSON-RPC `message/send` and `tasks/get`. Domain operations travel in a DataPart: `request_quote`, `answer_question`, `submit_application`, `accept_revised_offer`, `get_policy_status`. Mock-only: `POST /mock/faults`, `POST /mock/clock/advance`, `GET /healthz`.

## MCP surface

`python -m app.agent.mcp_server` (stdio, protocol `2025-06-18`). Tools: `get_confirmed_needs`, `record_customer_answer`, `request_quotes`, `compare_coverage`, `prepare_application`, `verify_policy`, `get_policy_form`. Scoped by `MCP_CUSTOMER_TOKEN`.
