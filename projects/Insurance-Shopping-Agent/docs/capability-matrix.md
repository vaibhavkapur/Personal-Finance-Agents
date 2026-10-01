---
title: "Provider capability matrix"
layout: default
nav_order: 21
---



# Provider capability matrix

Labels: **mock** = implemented against the in-repo simulator; **sandbox**/**live** = not implemented in this release.

| Capability | Direct mock adapter | A2A mock adapter | Sandbox | Live |
|---|---|---|---|---|
| Request quote (`renters-quote-request/v1` → `renters-quote/v1`) | mock | mock (JSON-RPC `message/send`) | — | — |
| Insurer follow-up questions (`input_required`) | mock | mock (`input-required` task state) | — | — |
| Resume task with an answer | mock | mock (`taskId` on message) | — | — |
| Task lookup by external id | mock | mock (`tasks/get`) | — | — |
| Submit application | mock | mock | — | — |
| Status lookup by original request reference | yes | yes | required before enabling writes | required |
| Revised offer during underwriting + acceptance | mock | mock | — | — |
| Decline during underwriting | mock | mock | — | — |
| Policy issuance with declarations document | mock | mock | — | — |
| Push callbacks (signed webhooks) | inbound endpoint implemented; simulator can queue `delayed_callback` | same | — | — |
| Fault injection (timeout, timeout-before, malformed, decline, effective-date shift) | yes | yes (`POST /mock/faults`) | n/a | n/a |
| Controllable clock | shared in-process | `POST /mock/clock/advance` per agent, propagated by the operator API | n/a | n/a |
| Credentials required | none | none | provider-specific | provider-specific |
| `environment` stamped on every result | `mock` | `mock` | `sandbox` | `production` |
| `authority` stamped on every result | `simulated` | `simulated` | `estimated` | `authoritative` |

Adapter flags are exposed at runtime by `GET /v1/operator/metrics` → `capability_matrix` (`status_lookup_by_request_ref`, `supports_callbacks`, `supports_revised_offer_acceptance`, `uncertain_outcome_requires_manual_review`, `protocol`).

## Protocol pins

| Protocol | Pinned revision | Where |
|---|---|---|
| A2A | `0.3.0` | `adapters/a2a/client.py` (`PINNED_PROTOCOL_VERSION`, checked against the agent card), `adapters/mock/a2a_server.py` |
| MCP | `2025-06-18` | `agent/mcp_server.py` |
| AP2 | not integrated (optional premium checkout; completing a payment does not establish binding) | — |
