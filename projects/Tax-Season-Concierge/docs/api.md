---
title: "Application API"
layout: default
nav_order: 21
---


# Application API

Open `/docs` for the generated typed OpenAPI contract. All routes are owned by this prototype, not by the IRS or a bank.

`POST /v1/session/demo` creates a random tenant with an HttpOnly, SameSite=Strict cookie (seven-day expiry). All case, draft and action routes use that authenticated synthetic session. No client-provided tenant field grants access. Non-matching IDs return 404. Mutation requests are JSON, at most 256 KiB, with expected case versions; stale versions return 409. Browser writes reject foreign origins. Unknown schema fields are rejected.

## Journey

- `POST /v1/tax-cases`: creates the working case. `scenario` selects the mock fixture. Only 2025, `US_FEDERAL`, `single` is accepted.
- `GET /v1/tax-cases`, `GET /v1/tax-cases/{id}`: tenant-scoped durable state.
- `GET /v1/config`: questionnaire, synthetic fixtures and rule manifest.
- `POST /v1/tax-cases/{id}/profile-check`: `{expected_case_version, profile}` with all explicit scope answers.
- `POST /v1/tax-cases/{id}/forms`: `{expected_case_version, form}`. Form amounts are integer USD cents. `W-2c` requires `supersedes_form_id` and corrected fields; omitted fields inherit prior values.
- `POST /v1/tax-cases/{id}/forms/{form_id}/confirm`: confirms extracted amounts without changing original evidence.
- `POST /v1/tax-cases/{id}/sample-documents`: imports the scenario's synthetic records.
- `POST /v1/tax-cases/{id}/reconcile`: `{expected_case_version, completeness_confirmed}`. The customer must attest completeness.
- `POST /v1/tax-cases/{id}/calculate`: `{expected_case_version, rule_pack_id, facts_hash}`. Rechecks eligibility and immutable facts.
- `POST /v1/tax-cases/{id}/return-drafts`: prepares or renews a review package. New packages invalidate previous unused challenges. An approved queued package cannot be silently replaced.
- `POST /v1/tax-cases/{id}/assist`: bounded rules-only orchestration; cannot confirm missing answers or approve a submission.
- `GET /v1/tax-cases/{id}/worksheet`: print-friendly HTML with line evidence.
- `GET /v1/tax-cases/{id}/package.json`: complete exact package and content hash.

All mutations above carry `expected_case_version`. File examples are in `fixtures/`.

## Authority and outcomes

`POST /v1/actions/{action_id}/approve` takes `expected_case_version`, `action_payload_hash`, `approval_challenge_id`. The approver comes from the session. The challenge and resulting approval each expire after 15 minutes. A repeat approval for the same action/content returns the original authority; changing content fails. The background worker submits exactly that package with one stable request reference. It verifies scope, source facts, rule-pack hash and expiry immediately before claiming the side effect.

`GET /v1/tax-cases/{id}/filing-status` returns separate receipt, acceptance and financial outcome fields plus environment, source and retrieval time.

`POST /v1/tax-cases/{id}/clock` takes `expected_case_version` and `days` (1–30). It replays events on the fixture clock, not on a real filing-deadline clock.

`POST /v1/tax-cases/{id}/resolve-rejection` records explicit synthetic identity review and requires a new reconciliation, draft and approval.

`POST /v1/tax-cases/{id}/replay` requeues a held uncertain write for original-reference reconciliation. It does not create an action or bypass approval.

`GET /v1/operator/metrics` returns tenant-scoped queue, outbox-delivery and invalidated-action counts.

`POST /v1/provider-events/tax` accepts only typed mock events. Supply `X-Tax-Timestamp` (Unix seconds) and `X-Tax-Signature` (hex HMAC-SHA256 of `timestamp + '.' + exact request bytes`, using `WEBHOOK_SECRET`). Replays deduplicate; changed bodies under the same event ID fail. Financial evidence requires a matching return and exact expected amount.

## Local execution modes

Default: SQLite, one worker loop within the API lifespan, in-process simulator. Set `RUN_WORKER=0` to run `python -m app.worker` separately. Compose sets PostgreSQL and `MOCK_PROVIDER_URL`; adapter credentials come from `MOCK_PROVIDER_TOKEN`, outside tool outputs. Set a stable `WEBHOOK_SECRET` for callback testing across restarts.
