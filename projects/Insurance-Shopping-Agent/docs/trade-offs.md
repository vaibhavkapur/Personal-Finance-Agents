---
title: "Trade-off note"
layout: default
nav_order: 24
---



# Trade-off note

## Deterministic engine, thin model

Suitability, ranking, hashing, authorization and state transitions run in ordinary code. The model chooses among seven read/prepare tools and writes prose. The `ScriptedProvider` implements the same policy without a model, which is why the evaluation can run the rules-only workflow and the agent path over identical fixtures. Cost: the scripted agent cannot extract answers from free text; the UI's forms and `record_customer_answer` (for a real model) carry that.

## Unknowns are first-class

`InsuranceNeeds` fields are `Optional`; `to_requirements()` sends the literal `unknown` to insurers; the comparison returns `undetermined` rather than passing a check; the application builder refuses required questions with `unknown`. This costs an extra state in every consumer but removes the most common hallucination path (defaulting "no").

## Exact-action approvals versus convenience

Approving requires the challenge id, the payload hash and the case version. Any material change (answers, needs, revised offer, expiry) invalidates the action. This makes the review screen authoritative but means customers re-approve more often. A 30-minute challenge TTL was chosen so a stale tab cannot approve yesterday's terms.

## Request references over retries

Every provider write uses a request reference the provider must honor idempotently. Timeouts produce `uncertain` actions that are reconciled by `get_policy_status(request_ref)` before any retry; a provider without that capability sends the case to `manual_review`. This keeps "no duplicated side effects" a property of the workflow rather than of luck, at the price of a mandatory capability on every adapter.

## A2A only at the deployment boundary

Insurer agents are separate processes speaking pinned A2A 0.3.0 with a self-defined `renters-quote/v1` payload. Inside the customer application, modules call each other directly. The A2A server/client are hand-written JSON-RPC (the official SDKs require Python ≥ 3.10; the prototype targets 3.9), covering only `message/send` and `tasks/get`.

## SQLite by default, PostgreSQL by configuration

Tests and the demo run on SQLite files; `docker-compose.yml` uses PostgreSQL with `migrations/001_initial.sql`. Leased jobs use `UPDATE … WHERE lease_until < now` which is safe on both, but PostgreSQL's row locking is the intended production path.

## Fixture clock

A controllable clock makes underwriting delays, quote expiry and approval TTLs testable. In multi-process A2A mode each insurer has its own clock; the operator endpoint propagates advances. A real deployment removes the fixture clock entirely.

## What the prototype does not claim

- No real carrier, rate, form or state rule is represented; all amounts are fixtures.
- Payment (AP2) is not integrated; a payment would not be evidence of binding.
- The software is not a licensed producer; it binds only fictional policies.
