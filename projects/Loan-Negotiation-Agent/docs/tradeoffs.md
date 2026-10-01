---
title: "Trade-off note"
layout: default
nav_order: 24
---


# Trade-off note

**Economic cost, not payment.** The comparison ranks by cumulative P&I + remaining balance + upfront costs at the borrower's horizon. A longer term is therefore never rewarded for its lower payment; the fixture Harbor 30-year offer loses on a 48-month horizon despite a $199/month lower payment. The trade-off is that the recommendation depends on a horizon the borrower must supply, so the agent refuses to compare until it has one and shows sensitivity across horizons.

**Financed vs cash costs.** Financed costs are added to principal and excluded from upfront cash; they show up as extra interest and remaining balance. This avoids double counting but makes "cash to close" and "economic cost" move in opposite directions, which the UI explains rather than hides.

**Prepaids and escrow.** Categories F and G are shown in cash-to-close but excluded from economic cost. This is the right treatment for a like-for-like comparison but slightly understates timing costs such as prepaid interest; the plan's scope ("treat separately") was chosen over modelling escrow refunds from the old servicer.

**Rounding rule.** Interest is rounded half-up to the cent each period and the final payment absorbs the drift. Servicers differ in convention; the rule is documented, versioned (`CALCULATION_VERSION`) and cross-checked against an independent Fraction-based implementation, so a different convention can be added as a separate profile without silently changing stored comparisons.

**Approvals bound to the case version.** Any material input change invalidates unconsumed approvals. This is safe but chatty: adding an offer while a review is open forces the borrower to re-review. We accepted the friction; the alternative (approving stale payloads) is worse.

**Uncertain writes hold the case.** A timeout or malformed response never triggers a second submission. If the provider cannot resolve the outcome by the original reference after three lookups, the case parks in `manual_review`. This trades throughput for zero duplicated side effects.

**Simulator in-process.** The mock lender network runs inside the API/worker process with a controllable clock, which makes tests deterministic and fast. The cost is that a separately deployed worker would not share simulator state; compose therefore runs the worker inside the API process until the simulator is hosted as its own service.

**Rules planner first.** The agent ships with a deterministic planner; the LLM planner sits behind the same `Planner` interface and is enabled only with `LLM_API_KEY`. This keeps the evaluation honest (zero model cost, reproducible) at the expense of natural-language flexibility. Evaluation compares the agent to a rules-only workflow on state and evidence, not prose.

**SQLite by default, PostgreSQL by URL.** Zero-dependency local runs and tests; the generated `migrations/0001_initial.sql` targets PostgreSQL. JSON columns are used for evolving payloads (cost items, review screens); anything queried or constrained is a typed column.

**Static frontend.** The prototype UI is dependency-free HTML/JS served by FastAPI so it runs without a Node toolchain. A React + TypeScript build (the plan's target) can replace it behind the same API.

**Not in this prototype.** Compliant APR, credit inquiries, rate locks, funding/payoff, ARM or government products, live provider integrations and the US regulatory review they require.
