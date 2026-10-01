---
title: "Application API contract"
layout: default
nav_order: 21
---


# Application API contract

All `/v1` customer/operator routes require the synthetic owner session, except local demo login. Provider callbacks use HMAC instead. OpenAPI is generated at `/openapi.json`; `/docs` provides interactive request schemas. Fiat values are integer USD cents. Unknown input fields are rejected.

- `POST /v1/demo/session`: create the HTTP-only, same-site local demo session. Only localhost hostnames are allowed; disable using `PAYDAY_DEMO_LOGIN=0`.
- `GET /v1/dashboard`: refresh bank evidence, return profile, cases, proposals, actions, transfers, policy reviews, timeline, journals, jobs and tool-run metadata.
- `POST /v1/payday-cases`: create a collecting case with `customer_id`, `policy_id`, `requested_payout_minor`, `currency="USD"`, `period="YYYY-MM"`.
- `GET /v1/payday-cases/{id}`: retrieve durable case state and version.
- `POST /v1/payday-cases/{id}/receipt-confirmations`: submit `{confirmations:[{transaction_id,category}]}`. Allowed positive receipt categories: income, transfer, loan, other. Posted negative debits and linked refunds cannot be relabeled as income.
- `POST /v1/payday-cases/{id}/calculate`: require confirmed receipts and bills, refresh cash, and create a proposal carrying snapshot/policy versions, targets, capacity, feasible amount, shortfall and conditional scenarios.
- `POST /v1/payday-cases/{id}/transfer-drafts`: `{proposal_id,simulation_mode}` with `Idempotency-Key` header. Modes: normal, delay, timeout, decline, malformed. Same key/content returns the same action; changed content fails.
- `POST /v1/actions/{id}/approve`: `{expected_case_version,action_payload_hash,approval_challenge_id}`. Atomically record approval, reserve cash and enqueue a job. The approver comes from the session. Stale, expired or mismatched input returns 409.
- `POST /v1/actions/{id}/cancel`: cancel an unapproved review.
- `POST /v1/actions/{id}/revoke`: revoke an approved action only before submission. The worker verifies non-execution before releasing its reservation.
- `GET /v1/payday-cases/{id}/cash-buckets`: reconciled bank cash and virtual allocations.
- `GET /v1/payday-cases/{id}/timeline`: append-only transitions for this case.
- `POST /v1/payday-cases/{id}/agent`: `{message,tool_budget:4}`. The budget is limited to 1–8 tools. Response includes tool trace, evidence, rules engine version and model cost.
- `POST /v1/reserve-policy/reviews`: new fraction decimal, emergency floor cents, and horizon days. Creates a review without changing the policy.
- `POST /v1/reserve-policy/reviews/{id}/approve`: exact hash and challenge from that review. Requires unchanged input revision; pending transfers must be resolved first.
- `POST /v1/operator/run-worker`: execute one eligible durable job; never bypasses approval.
- `POST /v1/operator/jobs/{id}/retry`: requeue a held original job after operational review; executor authority checks remain mandatory.
- `POST /v1/operator/events/{id}/replay`: repeat reconciliation for an existing signed inbox event.
- `POST /v1/simulator/events`: deterministic event controls, including advance, settle, return, late_receipt, ambiguous_receipt, bill_change, late_scenario. `advance` affects only the fixture clock; `settle` posts accepted bank transfers. Incoming receipts deduplicate provider references.
- `POST /v1/provider-events/payday`: a signed mock callback with event `id`, `case_id`, supported `type`, and `environment="mock"`. Verify `X-Payday-Timestamp` (Unix seconds) and `X-Payday-Signature` (hex HMAC-SHA256 of timestamp, dot, raw request body). Unknown or forged callbacks cannot alter bank cash.

The prototype has one local synthetic identity authorized for both customer and operations views. Its API does not claim production multi-role authentication. All domain lookups still enforce tenant ownership and return 404 across tenants.
