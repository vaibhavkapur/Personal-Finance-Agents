---
title: "Application API"
layout: default
nav_order: 21
---


# Application API

The full, generated contract is in `openapi.json` and at `http://127.0.0.1:8731/docs` while running. All `/v1` routes require a signed session except login and the separately HMAC-authenticated provider callback. All data lookups are tenant-scoped. The request models reject undeclared fields.

## Session and estate

- `POST /v1/session` with `{"username":"alex","password":"everkeep-demo"}` sets an HTTP-only, SameSite=Strict, eight-hour demo cookie.
- `GET /v1/session` returns the current identity; `DELETE /v1/session` clears the cookie.
- `POST /v1/demo-workspace` creates the complete synthetic fixture once, including explicit mock authority reviews; otherwise resumes it.
- `POST /v1/estate-cases` creates a collecting workspace with the fixed synthetic decedent and role. It does not automatically verify authority.
- `GET /v1/estate-cases` lists owned workspaces; `GET /v1/estate-cases/{id}` returns source evidence, per-asset requirements, timeline, version and summary.
- `POST /v1/estate-cases/{id}/discover-candidates` reviews supplied text and resolves only stable identities.
- `POST /v1/estate-cases/{id}/authority-reviews` is restricted to the demo operator; it invokes the fixture institution-review procedure.

Discovery and authority-review mutations require `{"expected_case_version":N}`. All stale versions fail with 409.

## Documents and packets

`POST /v1/estate-cases/{id}/documents` accepts `expected_case_version`, `kind`, `name`, and `content`. Content must begin with `SYNTHETIC RECORD\n`, and is limited to 30,000 characters. Original text receives a SHA-256 hash and an immutable document version. For the two trusted built-in missing forms, omit content and use kind `certified_authority` or `retirement_claim_form`.

`GET /v1/estate-assets/{id}/requirements` returns the dated checklist and missing evidence. `POST /v1/estate-assets/{id}/packet-drafts` accepts `expected_case_version` and a unique `idempotency_key`. It returns the exact payload, hash, expiry and challenge; it never submits.

`POST /v1/actions/{id}/approve` accepts:

```json
{
  "expected_case_version": 4,
  "action_payload_hash": "sha256:<actual returned hash>",
  "approval_challenge_id": "<actual returned challenge>"
}
```

The action must still be a valid draft. Invalid challenge/hash is 403; stale version, expired challenge or changed material input is 409. Approval creates one durable execution job. Repeating the same valid action approval returns the original action; a different challenge does not. `POST /v1/actions/{id}/revoke` accepts `expected_case_version`; revocation is permitted before dispatch, not after a provider may have received it.

## Operator and tools

- `POST /v1/estate-cases/{id}/guide` runs bounded source-backed rules guidance without self-approval.
- `GET /v1/estate-cases/{id}/operations` returns owned jobs, tool runs and event summaries.
- `POST /v1/estate-assets/{id}/scenario` accepts a supported `scenario` and expected version before packet drafting.
- `POST /v1/operator/clock` accepts `{"seconds":300}` to advance the mock clock.
- `POST /v1/operator/tick` processes at most one eligible durable job.
- `POST /v1/operator/jobs/{id}/replay` reconciles an already-approved unknown or held request. It cannot issue a fresh approval or request reference.
- `POST /v1/mcp` accepts MCP JSON-RPC; available tool names are extraction, candidate resolution, requirements, draft preparation and recorded resolution review. No submission or permission tools are exposed.

## Provider notification

`POST /v1/provider-events/estate` accepts `{"request_ref":"<original reference>"}` and `X-Estate-Signature`, the lowercase hexadecimal HMAC-SHA256 of the exact body using `PROVIDER_WEBHOOK_SECRET`. It resolves the approved action and rereads provider data. Duplicate events produce no new outcome. The callback is disabled when no secret is configured.

Fiat values use integer minor units plus currency. A submitted amount is `null`: this action discloses evidence and does not authorize a transfer. Recovery totals count only verified mock resolution records; source balances and insurance face value are reported separately.
