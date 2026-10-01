---
title: "Verification report"
layout: default
nav_order: 22
---


# Verification report

Verified 26 September 2026 against the local synthetic prototype.

- 62 pytest cases passed, including 32 labeled evaluations (24 development, 8 held-out).
- 0 unsupported completion claims in labeled fixtures. No LLM comparison was run.
- TypeScript checking and Vite production build passed.
- Docker build, schema/seed initialization, PostgreSQL health and separate API/worker/provider startup passed.
- HTTP integration against http://127.0.0.1:8733 passed: bank evidence request and follow-up resolution, insurer human review, retirement resolution, ambiguous candidate excluded, 12,960,525 USD minor units in simulated recovery, inventory incomplete.
- Browser sign-in, exact disclosure checkbox, initial bank submission, four-document follow-up packet and new approval were exercised.
- Mobile viewport 390px: document width equaled viewport width; no horizontal page overflow. Default viewport restored after the check.

The reproducible commands are in README.md. The HTTP smoke test creates a separate synthetic estate case and leaves the default browser demo unchanged. The desktop and mobile surfaces are one responsive application. The saved preview is in workspace-preview.jpg.

Known test warning: the pinned Starlette test client uses an anyio alias scheduled for deprecation. Tests pass.
