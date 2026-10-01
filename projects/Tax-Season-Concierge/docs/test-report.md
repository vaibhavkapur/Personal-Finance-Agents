---
title: "Verification report"
layout: default
nav_order: 25
---


# Verification report

Measured on 26 September 2026 against the delivered prototype. All inputs, provider responses and financial outcomes are synthetic.

## Results

- **61 backend tests passed** in the final native pytest run. One upstream Starlette/AnyIO deprecation warning remains; it does not affect the tests.
- **43 of 43 labeled evaluations passed**, including 10 held-out fixtures. Each outcome and monetary result also matched the direct deterministic baseline. See `evaluation.json` for per-case results, fixture hash and timings. No model was connected; model cost was $0 and no LLM-performance claim is made.
- **2,062 IRS single-filer Tax Table rows verified**: HTML extraction matches the archived 2025 instructions PDF, all intervals are contiguous, and lookup tests cover both endpoints of each interval. The rule pack verifies SHA-256 checksums at calculation and approval execution.
- **Frontend production build passed**, including TypeScript checking.
- **Playwright browser journey passed** against the four-service Docker deployment. It checks profile confirmation, document import, completeness, calculation, line evidence, approval gating, worker submission, separate acceptance/refund notice/account credit, reload persistence and mobile overflow. The final run completed in 8.7 seconds.
- **All eight HTTP demo scenarios passed** through PostgreSQL, FastAPI, the separate worker and the private mock provider: ordinary completion, corrected withholding, balance due, missing interest, rejection and re-review, timeout after acceptance, malformed response, and delayed events.

## Specific invariants tested

No submission without approval; exact content/challenge binding; approval expiry; stale version and fact rejection; correction invalidation before submission; preservation after acceptance; no edits while a write is in flight; no cross-tenant case/action/download access; no approval tool exposed through MCP; signed callback verification, body tampering and replay rejection; no credit verification before acceptance; no mismatched refund amount; duplicate provider events do not complete twice; at most one submission after timeout/restart; original-reference recovery after a crash immediately after the provider write; held-job replay does not bypass authority; and durable idempotent outbox delivery receipts.

Domain checks include original cents, aggregate rounding, payroll-tax exclusion, missing-income attestation, duplicate W-2s, partial W-2c replacement with per-field lineage, confirmation without overwriting original extraction, identity/year conflicts, Schedule B limits, unsupported income/deductions/credits and rule-version drift.

## Reference amounts

The main fixture produces total income $72,247, standard deduction $15,750, taxable income $56,497, tax $7,339, withholding $8,201 and potential refund $862. The correction fixture produces potential refund $1,362. The balance-due fixture produces $2,339 due and no payment event.

These expectations were checked against the pinned IRS table and documented arithmetic during implementation. They are **not independent professional review or certified filing results**.

## Remaining release gates

Independent tax-professional review of supported exclusions and reference returns remains outstanding. The app does not accept real customer documents, perform OCR, hold real taxpayer identifiers, use production identity proofing, connect a hosted model, connect an IRS sandbox, or execute live filing/payments. Production document encryption, user/operator role separation, load testing, provider enrollment and jurisdiction-specific obligations are outside this synthetic prototype.

The 10 held-out fixtures are a regression partition, not evidence of generalization for an AI model. No customer savings, refund guarantee or real filing success rate is claimed.
