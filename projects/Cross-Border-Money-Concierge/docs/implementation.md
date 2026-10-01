---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Passage · Cross-Border Money Concierge

A working sender-side USD → INR remittance prototype based on [the supplied plan](plan.md). Compare three fictional providers, approve an exact transfer, respond to document requests, and verify recipient credit independently from source funding.

**Everything is simulated.** Rates, fees, recipients, documents, provider references, and payouts are fixtures. There are no live remittance integrations, customer funds, or production identity documents.

## Run it

Requires Python 3.9+ and Node 20+. From this directory:

```sh
./scripts/start.sh
```

Open [Passage](http://127.0.0.1:8088). The same process serves the built React app, authenticated API, and embedded durable worker. The first run installs dependencies and builds the frontend. SQLite stores cases in `concierge.db`; restarting preserves the workflow. The simulator starts at **September 26, 2026, 12:00 UTC**. Advance its frozen clock using the transfer page or operations console.

For development, run `.venv/bin/python -m uvicorn backend.app.api.main:app --reload --port 8088` and, separately, `npm run dev` in `frontend`. API documentation is at [localhost:8088/docs](http://127.0.0.1:8088/docs).

Optional PostgreSQL and separate worker:

```sh
docker compose up --build
```

The Compose configuration uses PostgreSQL and runs the API and worker as separate processes sharing durable state. This configuration has been implemented but was not executed in this run. SQLite is the verified local path.

## What works

- Sender-total and recipient-target quote modes; integer currency minor units, decimal rates, explicit fees and known payout deductions.
- Comparable quotes from SwiftSend, BridgeWay, and Lotus Remit; expiry, amount limits, deadline filtering, and visible estimated delivery windows.
- Verified, masked synthetic recipients; review and approval bind the exact quote, recipient version, purpose, and action digest.
- Durable action queue, single-use approvals, revocation before dispatch, stable request references, provider-side idempotency, and bounded retries.
- Normal delivery, purpose-document requests, accepted-before-timeout recovery, delay, rejection, underpayment, and denied/confirmed cancellation scenarios.
- Separate funding evidence, recipient payout reference, actual delivered amount, source debit, fees, and refund confirmation.
- Customer transfer activity and recipient views; operator cases, jobs, webhook inbox replay, and redacted tool-run inspection.
- Five typed MCP tools and a bounded rules-only concierge. The model cannot approve or execute transfers.
- Synthetic API authentication with customer isolation, session-bound approval challenges, request-origin checks, signed mock callbacks, and event deduplication.

## Try the three journeys

1. **Compare and deliver:** confirm Ananya's account, review SwiftSend, approve, then advance five minutes and 24 hours. Funding and payout appear as separate timeline events; the receipt includes the final credit reference.
2. **Missing document:** choose BridgeWay and approve. Advance five minutes. Review the synthetic family-support declaration, approve sharing it with BridgeWay, then advance 24 hours.
3. **Timeout after acceptance:** choose Lotus Remit and approve. The case becomes `outcome_unknown`. Advance five minutes to recover the same provider transfer, then 24 hours to verify arrival.

Before approving, expand **Simulator scenario** in the review dialog to exercise short payment, delay, rejection, malformed response, or cancellation denial. Cancellation needs its own approval; a confirmed cancellation schedules a separate source-refund event 24 hours later.

Run all three API demos without browser interaction:

```sh
.venv/bin/python -m scripts.demo
```

These scripted demos authorize synthetic actions in isolated temporary databases. They do not modify your running app's cases.

## Validation

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.evaluate
cd frontend
npm run build
# Install Chromium once if needed: npx playwright install chromium
npm run test:e2e
```

When Chrome is installed on macOS, use `CHROME_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm run test:e2e` instead of downloading Chromium. Browser tests expect the app running at localhost:8088. They create synthetic transfers in that app's database.

Verified in this run: **56 backend/evaluation tests passed, 30/30 labeled scenarios passed, two desktop/mobile browser tests passed, and the production frontend build passed.**

The regression suite includes quote arithmetic, inverse rounding, stale versions, payload tampering, expiry, recipient changes, unauthorized access, approval revocation, event signature/identity validation, document approval, restart recovery, accepted-before-timeout initiation, cancellation denial, and 30 labeled workflow evaluations (20 development / 10 held out). [Measured evaluation output](evaluation-report.json) records final state, provider write count, and delivery evidence. These are fixture results, not customer or market metrics.

## Architecture and interfaces

- [Architecture and trade-offs](architecture.md)
- [Application and MCP API](api.md)
- [Provider capabilities and fixture behavior](providers.md)
- [Original build plan](plan.md)
- [Synthetic evaluation cases](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Cross-Border-Money-Concierge/fixtures/evaluation-cases.json)

## Prototype boundaries

The concierge uses deterministic rules; no external LLM, paid model API, or A2A integration is configured. The structured-model interface is isolated for a future model adapter. Evaluations measure the rules-only baseline, not a model-versus-baseline comparison.

Local demo login deliberately lets a visitor choose a synthetic customer/operator identity. It is not production authentication. Only synthetic purpose evidence can be shared; real document ingestion and encrypted object storage are not implemented. Provider calls are an in-process adapter with a separate persistent simulator ledger, not a separately deployed provider API. Outbox notifications are consumed locally, not emailed.

PostgreSQL serialization currently uses a coarse advisory lock, appropriate for this prototype; production would use per-case concurrency control, formal migrations, secrets management, real authentication, secure document storage, and provider-specific callback verification. No production or sandbox remittance capability is claimed. Optional A2A, additional corridors, live rails, and money custody remain outside the MVP.
