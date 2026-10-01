---
title: "Validation results"
layout: default
nav_order: 22
---


# Validation results

Validated locally on September 26, 2026, with Python 3.12, Node 20, and the SQLite mock-provider configuration.

- **68 pytest cases passed**: 32 workflow/API regressions and 36 labeled evaluation cases.
- **36/36 evaluation fixtures passed**, including 12 held-out prompts. The rules-only orchestrator preserved the canonical domain state and produced no unapproved provider writes. External model cost was $0 because no external model was used. This is not a comparison against an LLM.
- **All three executable demos passed**: partial containment with handoff, recognized descriptor with no report, and long investigation through replacement delivery and evidence-backed closure. A subsequent reversal reopened the closed case.
- **TypeScript and Vite production build passed.**
- **Docker Compose configuration validation passed.** PostgreSQL and the separate HTTP-provider container were not runtime-tested.
- **Browser interaction checks passed**: affected-card confirmation; exact-action review and approval; worker-driven lock verification; Northstar direct-authentication handoff; unauthorized-charge confirmation; separate report approval; recognition of the coffee descriptor; and provisional credit remaining distinct from final determination.
- **Responsive visual checks**: desktop at 1280px and mobile at 390px. The mobile next-step banner was corrected after inspection. Both widths had no document-level horizontal overflow; the simulator remains accessible in the compact navigation.

The regression suite covers expiry at approval and at execution, immutable payload binding, revocation, stale case versions, changed customer assertions, unknown-write reconciliation, acknowledged-but-unverified locks, declined and malformed results, provider/event deduplication, stale revisions, signed callbacks, cross-customer rejection, secret-input rejection, MCP schemas, missing customer authority, tool budgets, restart recovery, provisional/final/reversed credits, provider questions and approved responses, per-bank fixture deadlines, closure evidence, and preservation of lost-card protection after late lock or revocation events.

A Starlette test-client dependency emits one deprecation warning about an AnyIO alias; tests pass. No claim is made that prototype regex rejection can identify every possible secret, that fixture authentication verifies a real person, or that simulator checks establish production readiness. Production integrations and the boundaries listed in the README remain separate work.
