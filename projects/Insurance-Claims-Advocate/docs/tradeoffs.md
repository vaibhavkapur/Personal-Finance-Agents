---
title: "Trade-off note"
layout: default
nav_order: 24
---



# Trade-off note

Short record of choices that matter for the prototype. Not legal advice.

## Invented entitlement

**Risk.** A model that “interprets coverage” can invent a benefit.

**Choice.** A reviewer-approved rule layer in the policy fixture is the only eligibility engine. Tool results mark calculator output as `estimated`. The UI states that the insurer decides. Appeals require a fact locator and a clause; if the insurer’s reason is supported, the case explains that instead of drafting a louder appeal.

## Evidence leakage

**Risk.** Over-sharing documents or logging PII.

**Choice.** Each packet carries a disclosure manifest of exactly the documents sent. Logs keep redacted tool I/O and evidence references, not raw pages or full account numbers. Cross-customer document attach is rejected. Fixture identities use masked emails and last-4 payout fields.

## Endless follow-up

**Risk.** The agent keeps asking or appealing.

**Choice.** Insurer requests have IDs and fixture deadlines. `MAX_AUTO_FOLLOWUPS` caps automatic loops. After an upheld or unsupported appeal, the customer can accept and close unpaid. Incomplete-evidence cases stay open or ask a question; they never claim completion.

## Unknown provider writes

**Risk.** Retrying a timed-out submit creates a second claim.

**Choice.** Persist the pending action first. On timeout, query by the original request reference. If the provider cannot resolve it (sandbox flag), hold for `manual_review`. Do not mint a new side effect.

## Stack

The plan suggested React + TypeScript for the two views. This prototype serves a small SPA from FastAPI so `uvicorn` + fixtures is a complete demo. The views still implement the checklist, itemized amounts, exact-packet approval, timeline, and operator adapter/job surfaces. A later split into a Vite/React app would not change the API or domain engine.

Temporal was deferred: persisted jobs and leases recover mid-case (covered in tests). MCP and A2A are thin pinned implementations because the official MCP Python SDK needs Python ≥ 3.10 and this repo stays on 3.9.

## Honesty

Do not present simulator pass rates or fixture dollar amounts as customer savings or market results. Recheck jurisdiction, authority to represent, and the carrier’s actual filing channel before any live execution.
