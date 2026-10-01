---
title: "Application API contract"
layout: default
nav_order: 21
---


# Application API contract

All endpoints are owned by this application. None of them is a claimed endpoint of a bank, lender, protocol or government service. Interactive docs: `GET /docs` (OpenAPI).

Authentication: `Authorization: Bearer <token>`. Fixture tokens: `demo-borrower-token` (customer `cus_demo_5`), `other-borrower-token` (`cus_other_9`), `demo-operator-token` (operator). Tenant ownership is enforced on every read; a case that belongs to another customer returns `404`.

Amounts are integer minor units (USD cents) with an explicit currency. Rates are fixed-precision decimal strings (`"0.065000"` = 6.5%).

## Borrower

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/me` | Customer, mortgages, documents, cases, known lenders, clock |
| POST | `/v1/loan-cases` | Create the working case (see below) |
| GET | `/v1/loan-cases/{id}` | Full case view: mortgage, offers (all versions), latest comparison, pending approvals, lender requests, applications, term reviews, next decision |
| GET | `/v1/loan-cases/{id}/timeline` | Ordered case events (previous/next state, actor, expected version, source event id) |
| POST | `/v1/loan-cases/{id}/facts` | Confirm borrower facts: balance, escrow, horizon, cash constraint, financing choice, income evidence document |
| POST | `/v1/loan-cases/{id}/offers` | Attach Loan Estimate documents (`document_ids`) or inline synthetic documents |
| POST | `/v1/loan-cases/{id}/offers/{offer_id}/fields` | Supply a missing offer field with its source (revised interpretation; original document preserved) |
| POST | `/v1/loan-cases/{id}/compare` | Run deterministic schedules and the horizon comparison |
| POST | `/v1/loan-cases/{id}/decisions` | `{"decision":"keep_current"}` |
| POST | `/v1/loan-cases/{id}/lender-request-drafts` | Draft a factual reprice/comparable-offer request → review screen |
| POST | `/v1/loan-cases/{id}/application-drafts` | Draft a mock refinance application for an offer → review screen |
| POST | `/v1/loan-cases/{id}/applications/{app_id}/document-releases` | Propose releasing documents for outstanding conditions → review screen |
| POST | `/v1/loan-cases/{id}/applications/{app_id}/closing-requests` | Propose accepting final terms and requesting mock closing → review screen |
| GET | `/v1/loan-cases/{id}/applications/{app_id}/final-review` | Approved-offer terms vs. final terms with material-change flags |
| POST | `/v1/actions/{action_id}/approve` | Authorize a proposed external action (see below) |
| POST | `/v1/actions/{action_id}/decline` | Cancel a proposed action |
| GET/POST | `/v1/loan-cases/{id}/messages` | Agent conversation (`{"message": "...", "intent"?: "...", "params"?: {}}`) |
| GET | `/v1/lenders` | Lender directory and adapter capability flags |

### Create the working case

```http
POST /v1/loan-cases
{
  "customer_id": "cus_demo_5",
  "mortgage_id": "mortgage_demo_1",
  "holding_horizon_months": 48,
  "maximum_cash_to_close_minor": 600000,
  "offer_document_ids": ["offer_doc_a", "offer_doc_b"]
}
```

```json
{
  "id": "loancase_…",
  "status": "collecting",
  "version": 1,
  "missing_fields": ["current_balance_as_of", "payment_includes_escrow"],
  "outstanding_questions": [ { "id": "q_balance", "field": "current_balance_as_of", "question": "…", "kind": "confirm" } ]
}
```

`missing_fields` blocks the comparison. Offer-level gaps (for example a missing rate lock) do not block it; that offer is simply reported as *not ranked*.

### Comparison response (excerpt)

Every ranked offer states the horizon and fee assumption. The response is labelled `"authority": "estimated"` and `"is_financing_commitment": false`.

```json
{
  "horizon_months": 48,
  "calculation_version": "amort-v1.0-halfup-cents-final-adjust",
  "keep":   { "monthly_pi_minor": 212034, "remaining_balance_at_horizon_minor": 27955384, "economic_cost_at_horizon_minor": 8133016 },
  "offers": [ { "label": "Northstar Mortgage (mock): 6.5% / 300 months", "monthly_change_vs_keep_minor": -9472,
                "upfront_incremental_costs_minor": 450000, "financed_costs_minor": 0,
                "remaining_balance_at_horizon_minor": 27810615, "economic_difference_vs_keep_minor": -149425,
                "economic_break_even_month": 37, "simple_break_even_months": "47.5",
                "cash_to_close": { "total_minor": 860000, "prepaids_minor": 200000, "escrow_minor": 210000 },
                "rankable": true, "sensitivity": { "horizon_difference_minor": { "24": 149913, "60": -298290 } } } ],
  "recommendation": { "decision": "refinance_candidate", "best_offer_id": "offer_…", "unranked_offer_ids": ["offer_…"],
                      "fee_assumption": "incremental costs = categories A+B+C+E+H minus lender credits; F and G excluded" },
  "assumptions": ["…"]
}
```

### Review screen (returned by every `*-drafts` / `*-requests` endpoint)

```json
{
  "action_id": "act_…",
  "action_type": "send_negotiation",
  "status": "proposed",
  "action_payload_hash": "sha256:…",
  "expected_case_version": 5,
  "approval_challenge_id": "challenge_…",
  "challenge_expires_at": "2026-09-26T12:15:00+00:00",
  "review": {
    "title": "Send a reprice request to Harbor Home Loans (mock)",
    "destination": { "lender_id": "lender_mock_b", "lender_name": "…", "environment": "mock" },
    "message_preview": "…exact text…",
    "documents_shared": [],
    "irreversible_effect": "…",
    "does_not_authorize": ["credit inquiry", "rate lock", "application submission", "closing"]
  }
}
```

### Authorize a proposed external action

```http
POST /v1/actions/{action_id}/approve
{
  "expected_case_version": 5,
  "action_payload_hash": "sha256:…",
  "approval_challenge_id": "challenge_…"
}
```

The approver is the authenticated customer. Responses: `200` approval recorded and an `execute_action` job enqueued; `409` stale case version, changed payload, expired or reused challenge, or action no longer proposed; `403` approver does not own the case. A background executor re-verifies the approval immediately before the side effect and consumes it once. Repeated drafting with the same idempotency key returns the original action; reuse with different content fails with `409`.

Approvals are bound to lender, offer id and version, disclosed document set and case version. Any material input change (new offers, changed balance/horizon, changed final terms) invalidates unconsumed approvals.

## Provider callbacks

`POST /v1/provider-events/lenders` — headers `X-Lender-Id`, `X-Signature: sha256=<hmac>` over the canonical JSON body (sorted keys, no whitespace) with the lender's webhook secret. Events are deduplicated by `(provider, id)`; duplicates return `{"status":"duplicate"}`. Processing never trusts the callback body for state: the worker looks the application up by its original reference.

```json
{ "id": "evt_mock_0003", "type": "loan.application.conditions_requested", "provider": "lender_mock_a",
  "occurred_at": "2026-09-26T12:00:00+00:00", "environment": "mock",
  "data": { "application_ref": "app_mock_0001", "request_ref": "req_…", "condition_ids": ["income_doc_missing"] } }
```

## Operator (operator token)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/ops/cases`, `/v1/ops/cases/{id}` | Cases; detail includes timeline, actions, redacted tool runs |
| POST | `/v1/ops/cases/{id}/resume` | Move a `manual_review` case to a non-terminal state (cannot mark completed) |
| GET | `/v1/ops/jobs` | Persisted jobs with leases and errors |
| POST | `/v1/ops/worker/run-once`, `/v1/ops/worker/drain` | Run the worker inline |
| GET/POST | `/v1/ops/clock`, `/v1/ops/clock/advance` | Fixture clock |
| GET | `/v1/ops/adapter` | Adapter capabilities, request log, pending callbacks, armed faults |
| POST | `/v1/ops/faults` | Inject `timeout_after_accept`, `malformed_response`, `declined`, `delayed_callback`, `quote_expired`, `changed_closing_costs` |
| POST | `/v1/ops/mock/deliver-callbacks` | Deliver due simulator callbacks to the signed webhook |
| POST | `/v1/ops/replay/{provider_event_id}` | Replay an inbound event through the normal handler |
| GET | `/v1/ops/metrics` | Cases by state, waiting on customer vs provider, queue depth/age, tool errors, abandoned approvals, duplicates prevented, planner cost |

## Interoperability endpoints

- A2A lender agents: `GET /a2a/lenders/{lender_id}/.well-known/agent-card.json`, `POST /a2a/lenders/{lender_id}` (JSON-RPC `message/send`, `tasks/get`; protocol version `0.3`; payload schema `loan-offer/v1`).
- MCP server (stdio): `LOAN_AGENT_CUSTOMER_TOKEN=demo-borrower-token python -m backend.app.agent.mcp_server` (protocol revision `2025-06-18`; tools `read_loan_terms`, `record_borrower_facts`, `compare_loan_scenarios`, `prepare_lender_request`, `prepare_refinance_application`, `diff_final_terms`, `get_case_state`).
- Mock lender inspection: `GET /mock-lender/applications/{ref}`, `GET /mock-lender/negotiations/{request_ref}`.
