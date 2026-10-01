---
title: "Provider capability matrix"
layout: default
nav_order: 21
---


# Provider capability matrix

Every adapter advertises what it can actually do; the worker reads these flags. `environment` is recorded on every provider result and provider request.

| Capability | `merchant_mock` | `issuer_mock` | `statement_mock` (feed) | `merchant_sandbox` | Live |
|---|---|---|---|---|---|
| Environment | mock | mock | mock | sandbox (placeholder) | not present |
| `open_case` (write) | yes | yes (dispute) | — | not configured | — |
| `send_followup` (write) | yes | no | — | no | — |
| `get_case` (read) | yes | yes | — | not configured | — |
| `find_action(request_ref)` | yes | yes | — | **no** → uncertain writes hold for manual review | — |
| Async callbacks | `merchant.case_updated`, `merchant.store_credit_issued` | `issuer.dispute_updated` | `statement.transaction_posted` | — | — |
| Controllable clock | yes | yes | yes | — | — |
| Fault injection | declined, malformed, timeout-after-accept, delayed callback | declined, malformed, timeout-after-accept | — | — | — |
| Scenarios | complete refund, partial, store credit, claims-completed-never-posts, unresponsive, declined | provisional→final, provisional→reversed→final, provisional→reversed→declined, declined | postings scheduled by the other two | — | — |
| Authority of results | simulated | simulated | simulated | — | — |
| Credentials required | none | none | none | none (not implemented) | — |

Commerce protocols:

| Protocol | Role here | Authority |
|---|---|---|
| UCP order adjustments (fixture shape `ucp.order.v1-fixture`) | Evidence of a merchant-side refund adjustment; supplies refund references when present | simulated, grants no execution authority |
| ACP order/refund webhooks (fixture shape `acp.webhook.v1-fixture`) | Evidence of a merchant refund claim and reference | simulated, grants no execution authority |
| MCP | Tool interoperability: the five case tools over stdio JSON-RPC | our records: authoritative; provider data: simulated |
| A2A | Optional boundary to a simulated merchant support agent; pinned version, mapped task ids | carries approved payloads only |
| AP2 | Not used: recovering an already-paid amount needs no new payment mandate | — |

Simulator success rates are not customer or market results.
